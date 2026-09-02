"""Data-quality pipeline (spec §14.2 / §17.3 Ticket 5).

Loads datasets from DuckDB, runs configured data-quality checks, writes
``dq_results`` (replaced per run), routes per-record failures to the
``exception_queue`` (capped), emits tamper-evident audit events (``dq_check``
per check + ``exception_open`` per routed item), computes per-dataset quality
scores (spec §12), and prints a summary.

Deterministic: uses a fixed as-of timestamp and a caller-supplied batch id, so
repeated runs over the same data produce identical results.

Scope note: exception priority here is a provisional severity mapping
(high/medium/low). The full priority formula
(``severity_weight * amount_at_risk_factor * age_factor``) and SLA aging belong
to the exception-workflow ticket. This pipeline only opens items in the queue.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from rich.console import Console
from rich.table import Table

from civicpay.audit.evidence import BatchIdAlreadyUsedError, batch_id_in_use
from civicpay.audit.ledger import AuditLedger
from civicpay.data import models as M
from civicpay.data.synthetic import AS_OF_DATETIME
from civicpay.quality.checks import CheckResult, check_accuracy_referential, run_check
from civicpay.quality.scoring import dataset_quality_score
from civicpay.storage.duckdb import DEFAULT_DB_PATH, DuckDBStore

console = Console()

DEFAULT_CONFIG_PATH = Path("config/dq_checks.yml")
DEFAULT_BATCH_ID = "DQ-001"

# Dataset name -> DuckDB table.
DATASET_TABLES: dict[str, str] = {
    "customers": M.Customer.TABLE,
    "accounts": M.Account.TABLE,
    "transactions": M.Transaction.TABLE,
    "payment_records": M.PaymentRecord.TABLE,
}

_SEVERITY_PRIORITY = {"high": "high", "medium": "medium", "low": "low"}


@dataclass
class DQConfig:
    datasets: dict[str, dict[str, Any]] = field(default_factory=dict)
    type_weights: dict[str, float] = field(default_factory=dict)
    max_exceptions_per_check: int = 25


def load_config(path: Path | str | None = None) -> DQConfig:
    """Load data-quality config from YAML."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not path.exists():
        return DQConfig()
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return DQConfig(
        datasets=data.get("datasets", {}),
        type_weights=data.get("type_weights", {}),
        max_exceptions_per_check=int(data.get("max_exceptions_per_check", 25)),
    )


class QualityPipeline:
    """Orchestrates a data-quality run against a DuckDB store."""

    def __init__(
        self,
        store: DuckDBStore,
        config: DQConfig | None = None,
        as_of: datetime = AS_OF_DATETIME,
    ) -> None:
        self.store = store
        self.config = config or load_config()
        self.as_of = as_of

    def run(
        self,
        batch_id: str = DEFAULT_BATCH_ID,
        dataset: str | None = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        """Run configured DQ checks and persist results + audit events."""
        store = self.store
        store.init_schema()

        # Pre-flight: the audit log is append-only, so re-running the same
        # batch_id collides on event_id / exception_id primary keys. Fail fast
        # with a clear message instead of a raw DuckDB constraint error.
        if batch_id_in_use(store, batch_id):
            raise BatchIdAlreadyUsedError(batch_id)

        datasets = self.config.datasets
        if dataset is not None:
            if dataset not in datasets:
                raise ValueError(f"No DQ config for dataset '{dataset}'.")
            datasets = {dataset: datasets[dataset]}

        all_results: list[CheckResult] = []
        exception_rows: list[dict[str, Any]] = []
        audit_events: list[dict[str, Any]] = []
        per_dataset_scores: dict[str, float] = {}
        exc_seq = 0

        for ds_name, ds_cfg in datasets.items():
            table = ds_cfg.get("table", DATASET_TABLES.get(ds_name, ds_name))
            df = store.read_table(table)
            id_field = ds_cfg.get("id_field")
            df.attrs["id_field"] = id_field

            ds_results: list[CheckResult] = []
            for seq, check_cfg in enumerate(ds_cfg.get("checks", []), start=1):
                if check_cfg.get("rule") == "referential":
                    ref_ds = check_cfg.get("reference_dataset")
                    ref_field = check_cfg.get("reference_field", "account_id")
                    ref_table = (
                        self.config.datasets.get(ref_ds, {}).get(
                            "table", DATASET_TABLES.get(ref_ds, ref_ds)
                        )
                        if ref_ds
                        else None
                    )
                    ref_values = (
                        store.read_table(ref_table)[ref_field]
                        if ref_table
                        else pd.Series(dtype=object)
                    )
                    result = self._run_referential(ds_name, df, check_cfg, ref_values, seq)
                else:
                    result = run_check(ds_name, df, check_cfg, self.as_of, seq)
                ds_results.append(result)

            all_results.extend(ds_results)

            # Per-dataset quality score (§12): check-type-weighted average.
            per_dataset_scores[ds_name] = dataset_quality_score(
                [(r.check_type, r.quality_score) for r in ds_results],
                self.config.type_weights,
            )

            # Route per-record failures to the exception queue (capped).
            for r in ds_results:
                route = check_cfg_get_route(r, ds_cfg)
                if not route or not r.failing_ids:
                    continue
                cap = self.config.max_exceptions_per_check
                for rid in r.failing_ids[:cap]:
                    exc_seq += 1
                    exception_rows.append(
                        {
                            "exception_id": f"EXC-{batch_id}-{exc_seq:06d}",
                            "source": M.ExceptionSource.DQ,
                            "reference_id": f"{ds_name}:{rid}",
                            "priority": _SEVERITY_PRIORITY.get(r.severity, "low"),
                            "assigned_to": None,
                            "status": M.ExceptionStatus.OPEN,
                            "created_at": self.as_of,
                            "resolved_at": None,
                            "resolution_notes": None,
                            "root_cause": None,
                        }
                    )
                    audit_events.append(
                        {
                            "event_type": M.AuditEventType.EXCEPTION_OPEN,
                            "entity_type": "exception",
                            "entity_id": f"EXC-{batch_id}-{exc_seq:06d}",
                            "action": f"exception_open:{r.check_name}",
                            "batch_id": batch_id,
                        }
                    )

        # --- Write dq_results (replace per run) ----------------------------- #
        dq_rows = [self._dq_row(r, batch_id) for r in all_results]
        store.write_dataframe(M.DQResult.TABLE, pd.DataFrame(dq_rows), mode="replace")

        # --- Write exception_queue rows (append) ---------------------------- #
        if exception_rows:
            store.write_dataframe(
                M.ExceptionItem.TABLE, pd.DataFrame(exception_rows), mode="append"
            )

        # --- Emit audit events ---------------------------------------------- #
        for r in all_results:
            audit_events.append(
                {
                    "event_type": M.AuditEventType.DQ_CHECK,
                    "entity_type": "dq_check",
                    "entity_id": r.check_id,
                    "action": r.check_name,
                    "batch_id": batch_id,
                }
            )
        ledger_writer = AuditLedger(store=store, actor=actor, as_of=self.as_of)
        ledger_writer.append_many(audit_events)

        summary: dict[str, Any] = {
            "batch_id": batch_id,
            "datasets_checked": len(datasets),
            "checks_run": len(all_results),
            "checks_passed": sum(1 for r in all_results if r.passed),
            "checks_failed": sum(1 for r in all_results if not r.passed),
            "total_failing_records": sum(r.failing_records for r in all_results),
            "exceptions_routed": len(exception_rows),
            "audit_events": len(audit_events),
            "per_dataset_scores": per_dataset_scores,
        }
        self._print_summary(summary, all_results)
        return summary

    # -- helpers ----------------------------------------------------------- #

    def _run_referential(self, ds_name, df, check_cfg, ref_values, seq) -> CheckResult:
        """Run a referential-integrity check using pre-loaded reference values."""
        from civicpay.quality.scoring import check_quality_score

        failing, ids = check_accuracy_referential(df, check_cfg["field"], ref_values)
        checked = len(df)
        return CheckResult(
            check_id=f"DQ-{ds_name}-{seq:04d}",
            dataset_name=ds_name,
            check_type=check_cfg["type"],
            check_name=check_cfg.get("name", "referential"),
            passed=failing == 0,
            checked=checked,
            failing_records=failing,
            quality_score=check_quality_score(checked, failing),
            checked_at=self.as_of,
            failing_ids=ids,
            severity=check_cfg.get("severity", "low"),
        )

    @staticmethod
    def _dq_row(r: CheckResult, batch_id: str) -> dict[str, Any]:
        return {
            "dq_check_id": r.check_id,
            "dataset_name": r.dataset_name,
            "check_type": r.check_type,
            "check_name": r.check_name,
            "passed": r.passed,
            "failing_records": r.failing_records,
            "quality_score": r.quality_score,
            "checked_at": r.checked_at,
        }

    def _print_summary(self, summary: dict[str, Any], results: list[CheckResult]) -> None:
        table = Table(title=f"Data-Quality Run — {summary['batch_id']}")
        table.add_column("Dataset", style="bold")
        table.add_column("Check", style="bold")
        table.add_column("Type")
        table.add_column("Passed", justify="right")
        table.add_column("Failing", justify="right")
        table.add_column("Score", justify="right")
        for r in results:
            table.add_row(
                r.dataset_name,
                r.check_name,
                r.check_type,
                "yes" if r.passed else "no",
                f"{r.failing_records:,}",
                f"{r.quality_score:.2f}",
            )
        table.add_section()
        for ds, score in summary["per_dataset_scores"].items():
            table.add_row(ds, "— dataset score —", "", "", "", f"{score:.2f}")
        console.print(table)
        console.print(
            f"Checks: {summary['checks_run']} run, {summary['checks_passed']} passed, "
            f"{summary['checks_failed']} failed. "
            f"Failing records: {summary['total_failing_records']:,}. "
            f"Exceptions routed: {summary['exceptions_routed']}. "
            f"Audit events: {summary['audit_events']}."
        )


def check_cfg_get_route(r: CheckResult, ds_cfg: dict[str, Any]) -> bool:
    """Resolve whether a check's failures should be routed to the queue.

    Per-check ``route_failures`` overrides; otherwise anomaly checks do not
    route (statistical outliers are informational) and all others do.
    """
    for c in ds_cfg.get("checks", []):
        if c.get("name") == r.check_name:
            if "route_failures" in c:
                return bool(c["route_failures"])
            break
    if r.check_type == "anomaly":
        return False
    return True


def run_dq(
    db_path: str = str(DEFAULT_DB_PATH),
    config_path: Path | str | None = None,
    batch_id: str = DEFAULT_BATCH_ID,
    dataset: str | None = None,
    as_of: datetime = AS_OF_DATETIME,
) -> dict[str, Any]:
    """Convenience entry point: open store, run DQ, close store."""
    store = DuckDBStore(db_path)
    try:
        return QualityPipeline(store=store, config=load_config(config_path), as_of=as_of).run(
            batch_id=batch_id, dataset=dataset
        )
    finally:
        store.close()
