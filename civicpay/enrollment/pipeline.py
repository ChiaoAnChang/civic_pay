"""Enrollment pipeline orchestration (Ticket 13).

Reads candidate rows (either the seeded ``pending_enrollments`` table, or an
external CSV via ``file_path``), runs each through :mod:`validators` then the
:mod:`dual_source` agreement gate, and writes outcomes:

* validation failure -> rejected, reported, no dual-source evaluation.
* dual-source agreement -> ``accepted_enrollments`` + ``enrollment_accept``
  audit event.
* dual-source disagreement -> the shared ``exception_queue``
  (``source="enrollment_dual_source"``) + ``enrollment_mismatch`` audit
  event, for human review via the existing exception workflow.

Unlike ``ReconciliationPipeline``/``QualityPipeline``, enrollment has no
recon/DQ-style multi-record batch — each enrollment is its own logical batch,
``batch_id = enrollment_id`` (already ``ENR-``-prefixed by convention, so no
further wrapping is needed — the analogous ``EXC-RESOLVE-{exception_id}``
batch id elsewhere in this codebase *does* double up on its prefix, but there
is no need to copy that here). This makes reprocessing inherently safe: a
record already present in the append-only audit log (checked via the same
``batch_id_in_use`` pipelines already use) is skipped, not reprocessed and
not an error — so re-running against the same seeded pool only processes
genuinely-new candidates.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pandas as pd
from rich.console import Console
from rich.table import Table

from civicpay.audit.evidence import batch_id_in_use
from civicpay.audit.ledger import AuditLedger
from civicpay.data import models as M
from civicpay.data.synthetic import AS_OF_DATETIME
from civicpay.enrollment import dual_source
from civicpay.enrollment.validators import (
    DEFAULT_RULES_PATH,
    load_rules,
    to_enrollment_record,
    validate,
)
from civicpay.storage.duckdb import DuckDBStore

console = Console()

# A small, deterministic backlog of already-aged mismatches (mirrors the DQ
# module's OPEN_QUESTIONS §G backlog cohort), seeded once per fresh database
# so SLA escalation is visible without wall-clock timestamps. Amounts/terms
# are chosen so the rounding-divergence dual-source gate deterministically
# disagrees (not a coincidence of random generation).
_BACKLOG_ENTITY_PREFIX = "ENR-BACKLOG"
_BACKLOG_AGES_DAYS = (5, 15, 30)
_BACKLOG_AMOUNTS_TERMS = (
    (Decimal("1000.00"), 7),
    (Decimal("2500.00"), 13),
    (Decimal("750.00"), 11),
)

_EXCEPTION_PRIORITY = "medium"


@dataclass
class EnrollmentOutcome:
    enrollment_id: str
    outcome: str  # "accepted" | "mismatch" | "rejected" | "skipped"
    issues: list[str] = field(default_factory=list)


def _row_to_raw(row: pd.Series) -> dict[str, Any]:
    return {
        "enrollment_id": row["enrollment_id"],
        "entity_id": row["entity_id"],
        "program_code": row["program_code"],
        "enrollment_date": row["enrollment_date"],
        "incentive_amount": row["incentive_amount"],
        "term_months": row["term_months"],
        "region": row["region"],
        "submitted_by": row["submitted_by"],
    }


class EnrollmentPipeline:
    """Orchestrates enrollment validation + dual-source gating against a
    DuckDB store."""

    def __init__(
        self,
        store: DuckDBStore,
        rules: dict[str, Any] | None = None,
        as_of: datetime = AS_OF_DATETIME,
    ) -> None:
        self.store = store
        self.rules = rules or load_rules(DEFAULT_RULES_PATH)
        self.as_of = as_of
        self.tolerance = Decimal(str(self.rules.get("dual_source_tolerance", "0.01")))

    def run(self, file_path: str | Path | None = None, actor: str = "system") -> dict[str, Any]:
        """Process candidate rows and return a summary dict."""
        store = self.store
        store.init_schema()

        seed_backlog = not batch_id_in_use(store, f"{_BACKLOG_ENTITY_PREFIX}-0001")

        if file_path is not None:
            df = pd.read_csv(file_path, dtype=str)
        else:
            df = store.read_table(M.PendingEnrollment.TABLE)
            df = df[df["status"] == M.EnrollmentStatus.PENDING]

        outcomes: list[EnrollmentOutcome] = []
        seen_entity_ids: set[str] = set()
        status_updates: dict[str, str] = {}  # enrollment_id -> new status

        for _, row in df.iterrows():
            outcomes.append(self._process_one(_row_to_raw(row), seen_entity_ids, actor))
            if outcomes[-1].outcome != "skipped":
                status_updates[outcomes[-1].enrollment_id] = (
                    M.EnrollmentStatus.ACCEPTED
                    if outcomes[-1].outcome == "accepted"
                    else M.EnrollmentStatus.MISMATCH
                    if outcomes[-1].outcome == "mismatch"
                    else M.EnrollmentStatus.REJECTED
                )

        if file_path is None:
            for enrollment_id, new_status in status_updates.items():
                store.execute(
                    "UPDATE pending_enrollments SET status = ? WHERE enrollment_id = ?",
                    [new_status, enrollment_id],
                )

        backlog_seeded = self._seed_backlog_cohort(actor) if seed_backlog else 0

        summary = {
            "processed": len(outcomes),
            "accepted": sum(1 for o in outcomes if o.outcome == "accepted"),
            "mismatch": sum(1 for o in outcomes if o.outcome == "mismatch"),
            "rejected": sum(1 for o in outcomes if o.outcome == "rejected"),
            "skipped": sum(1 for o in outcomes if o.outcome == "skipped"),
            "backlog_seeded": backlog_seeded,
        }
        self._print_summary(summary, outcomes)
        return summary

    # -- helpers ------------------------------------------------------------ #

    def _process_one(
        self, raw: dict[str, Any], seen_entity_ids: set[str], actor: str
    ) -> EnrollmentOutcome:
        enrollment_id = str(raw["enrollment_id"])
        batch_id = enrollment_id
        if batch_id_in_use(self.store, batch_id):
            return EnrollmentOutcome(enrollment_id, "skipped")

        result = validate(raw, self.rules, self.as_of, seen_entity_ids)
        seen_entity_ids.add(str(raw.get("entity_id", "")).strip())
        ledger = AuditLedger(store=self.store, actor=actor, as_of=self.as_of)

        if not result.is_valid:
            reasons = [f"{i.field}: {i.message}" for i in result.errors]
            ledger.append(
                event_type=M.AuditEventType.ENROLLMENT_VALIDATE,
                entity_type="enrollment",
                entity_id=enrollment_id,
                action=f"enrollment_rejected:{'; '.join(reasons)}",
                batch_id=batch_id,
            )
            return EnrollmentOutcome(enrollment_id, "rejected", reasons)

        record = to_enrollment_record(raw)
        evaluation = dual_source.evaluate(self.store, record, self.tolerance)
        self._write_dual_source_result(record, evaluation)

        if evaluation.agreed:
            self._write_accepted(record, evaluation, batch_id)
            ledger.append(
                event_type=M.AuditEventType.ENROLLMENT_ACCEPT,
                entity_type="enrollment",
                entity_id=enrollment_id,
                action="enrollment_accepted",
                batch_id=batch_id,
            )
            return EnrollmentOutcome(enrollment_id, "accepted")

        self._write_mismatch_exception(record, evaluation, batch_id)
        ledger.append(
            event_type=M.AuditEventType.ENROLLMENT_MISMATCH,
            entity_type="enrollment",
            entity_id=enrollment_id,
            action=f"enrollment_mismatch:delta={evaluation.delta}",
            batch_id=batch_id,
        )
        return EnrollmentOutcome(enrollment_id, "mismatch")

    def _write_dual_source_result(
        self, record: Any, evaluation: dual_source.DualSourceEvaluation
    ) -> None:
        row = {
            "result_id": f"DSR-{record.enrollment_id}",
            "enrollment_id": record.enrollment_id,
            "method_a_amount": float(evaluation.method_a_amount),
            "method_b_amount": float(evaluation.method_b_amount),
            "delta": float(evaluation.delta),
            "tolerance": float(evaluation.tolerance),
            "agreed": evaluation.agreed,
            "evaluated_at": self.as_of,
        }
        self.store.write_dataframe(M.DualSourceResult.TABLE, pd.DataFrame([row]), mode="append")

    def _write_accepted(
        self, record: Any, evaluation: dual_source.DualSourceEvaluation, batch_id: str
    ) -> None:
        row = {
            "enrollment_id": record.enrollment_id,
            "entity_id": record.entity_id,
            "program_code": record.program_code,
            "enrollment_date": record.enrollment_date,
            "incentive_amount": float(record.incentive_amount),
            "term_months": record.term_months,
            "region": record.region,
            "submitted_by": record.submitted_by,
            "expected_payout": float(evaluation.method_a_amount),
            "accepted_at": self.as_of,
            "batch_id": batch_id,
        }
        self.store.write_dataframe(M.AcceptedEnrollment.TABLE, pd.DataFrame([row]), mode="append")

    def _write_mismatch_exception(
        self, record: Any, evaluation: dual_source.DualSourceEvaluation, batch_id: str
    ) -> None:
        self._write_exception(
            exception_id=f"EXC-{batch_id}-000001",
            reference_id=f"pending_enrollments:{record.enrollment_id}",
            created_at=self.as_of,
        )

    def _write_exception(self, exception_id: str, reference_id: str, created_at: datetime) -> None:
        row = {
            "exception_id": exception_id,
            "source": M.ExceptionSource.ENROLLMENT_DUAL_SOURCE,
            "reference_id": reference_id,
            "priority": _EXCEPTION_PRIORITY,
            "assigned_to": None,
            "status": M.ExceptionStatus.OPEN,
            "created_at": created_at,
            "resolved_at": None,
            "resolution_notes": None,
            "root_cause": None,
        }
        self.store.write_dataframe(M.ExceptionItem.TABLE, pd.DataFrame([row]), mode="append")

    def _seed_backlog_cohort(self, actor: str) -> int:
        """Seed a small, deterministic cohort of already-aged mismatches.

        Mirrors the DQ module's backlog cohort (OPEN_QUESTIONS §G): real
        dual-source evaluations (not fabricated outcomes), backdated
        ``created_at`` on the exception only — the audit event's own
        timestamp stays ``as_of`` like every other event, since it is
        recording *when this was seeded*, not re-litigating when a genuine
        detection happened. See ``QualityPipeline``'s ``BACKLOG_BATCH_ID`` for
        the identical rationale and the chain-fork pitfall it avoids by using
        a separate ``AuditLedger`` instance per logical batch.
        """
        seeded = 0
        for i, (amount, term) in enumerate(_BACKLOG_AMOUNTS_TERMS, start=1):
            enrollment_id = f"{_BACKLOG_ENTITY_PREFIX}-{i:04d}"
            batch_id = enrollment_id
            if batch_id_in_use(self.store, batch_id):
                continue
            age = _BACKLOG_AGES_DAYS[(i - 1) % len(_BACKLOG_AGES_DAYS)]
            created_at = self.as_of - timedelta(days=age)
            record = to_enrollment_record(
                {
                    "enrollment_id": enrollment_id,
                    "entity_id": f"ENT-BACKLOG-{i:04d}",
                    "program_code": "GROWTH",
                    "enrollment_date": created_at,
                    "incentive_amount": str(amount),
                    "term_months": str(term),
                    "region": "WEST",
                    "submitted_by": "seed-backlog",
                }
            )
            evaluation = dual_source.evaluate(self.store, record, self.tolerance)
            self._write_dual_source_result(record, evaluation)
            if evaluation.agreed:
                # Should not happen given the chosen amount/term pairs, but
                # never silently claim a backlog "mismatch" that isn't one.
                continue
            self._write_exception(
                exception_id=f"EXC-{batch_id}-000001",
                reference_id=f"pending_enrollments:{enrollment_id}",
                created_at=created_at,
            )
            AuditLedger(store=self.store, actor=actor, as_of=self.as_of).append(
                event_type=M.AuditEventType.ENROLLMENT_MISMATCH,
                entity_type="enrollment",
                entity_id=enrollment_id,
                action=f"enrollment_mismatch:delta={evaluation.delta}",
                batch_id=batch_id,
            )
            seeded += 1
        return seeded

    def _print_summary(self, summary: dict[str, Any], outcomes: list[EnrollmentOutcome]) -> None:
        table = Table(title="Enrollment Validation Run")
        table.add_column("Metric", style="bold")
        table.add_column("Count", justify="right")
        for key in ("processed", "accepted", "mismatch", "rejected", "skipped", "backlog_seeded"):
            table.add_row(key, str(summary[key]))
        console.print(table)
        rejected = [o for o in outcomes if o.outcome == "rejected"]
        if rejected:
            console.print(f"[yellow]{len(rejected)} record(s) rejected at validation:[/]")
            for o in rejected[:10]:
                console.print(f"  {o.enrollment_id}: {'; '.join(o.issues)}")


def run_enrollment_validate(
    db_path: str,
    file_path: str | Path | None = None,
    rules_path: str | Path | None = None,
    as_of: datetime = AS_OF_DATETIME,
) -> dict[str, Any]:
    """Convenience entry point: open store, run the pipeline, close store."""
    store = DuckDBStore(db_path)
    try:
        rules = load_rules(rules_path)
        return EnrollmentPipeline(store=store, rules=rules, as_of=as_of).run(file_path=file_path)
    finally:
        store.close()
