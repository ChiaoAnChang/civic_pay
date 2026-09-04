"""Reconciliation pipeline (spec §14.1).

Reads payment records and ledger transactions from DuckDB, runs the matcher,
assembles a full ``reconciliation_results`` table (one row per payment plus one
row per unmatched ledger entry), emits tamper-evident audit events, and prints a
summary. Deterministic: uses a fixed as-of timestamp and a caller-supplied
batch id, so repeated runs over the same data produce identical results.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from civicpay.audit.evidence import BatchIdAlreadyUsedError, batch_id_in_use
from civicpay.audit.ledger import AuditLedger
from civicpay.data import models as M
from civicpay.data.synthetic import AS_OF_DATETIME
from civicpay.recon.matcher import ReconConfig, reconcile
from civicpay.storage.duckdb import DEFAULT_DB_PATH, DuckDBStore
from rich.console import Console
from rich.table import Table

# Bundled with the package (civicpay/config/, not the repo-root config/ a
# git checkout also has) so `pip install civicpay` ships a working default
# regardless of the caller's current working directory — a bare relative
# "config/recon.yml" resolved nothing once installed outside a repo clone.
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "recon.yml"

console = Console()


def load_config(path: Path | str | None = None) -> ReconConfig:
    """Load reconciliation config from YAML, falling back to defaults."""
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if path.exists():
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        # Support both the clear names and the spec's original aliases.
        return ReconConfig(
            amount_tolerance=float(
                data.get("amount_tolerance", data.get("match_tolerance_amount", 1.0))
            ),
            date_window_days=int(
                data.get("date_window_days", data.get("match_date_window_days", 1))
            ),
            fuzzy_threshold=float(data.get("fuzzy_threshold", 0.85)),
            stale_days=int(data.get("stale_days", 30)),
        )
    return ReconConfig()


class ReconciliationPipeline:
    """Orchestrates a single reconciliation batch against a DuckDB store."""

    def __init__(
        self,
        store: DuckDBStore,
        config: ReconConfig | None = None,
        as_of: datetime = AS_OF_DATETIME,
    ) -> None:
        self.store = store
        self.config = config or load_config()
        self.as_of = as_of

    def run(
        self,
        batch_id: str = "BATCH-001",
        file_id: str | None = None,
        reconciled_by: str = "system",
    ) -> dict[str, Any]:
        """Run reconciliation and persist results + audit events. Returns summary."""
        store = self.store
        store.init_schema()

        # Pre-flight: the audit log is append-only, so re-running the same
        # batch_id collides on event_id primary keys. Fail fast with a clear
        # message instead of a raw DuckDB constraint error.
        if batch_id_in_use(store, batch_id):
            raise BatchIdAlreadyUsedError(batch_id)

        payments = store.read_table(M.PaymentRecord.TABLE)
        transactions = store.read_table(M.Transaction.TABLE)
        if file_id is not None:
            payments = payments[payments["file_id"] == file_id].reset_index(drop=True)
        if payments.empty:
            raise ValueError("No payment records found to reconcile.")

        results, ledger, matcher_summary = reconcile(
            payments=payments,
            transactions=transactions,
            config=self.config,
            batch_id=batch_id,
            as_of=self.as_of,
        )

        # --- Assemble reconciliation_results rows ---------------------------- #
        recon_rows = [self._payment_row(r, reconciled_by) for r in results]
        # Unmatched ledger entries (ledger transactions never matched).
        unmatched_ledger_rows = self._unmatched_ledger_rows(ledger, batch_id, reconciled_by)
        recon_df = pd.DataFrame(recon_rows + unmatched_ledger_rows)
        store.write_dataframe(M.ReconciliationResult.TABLE, recon_df, mode="replace")

        # --- Audit events --------------------------------------------------- #
        audit_events = self._build_audit_events(results, batch_id)
        ledger_writer = AuditLedger(store=store, actor=reconciled_by, as_of=self.as_of)
        ledger_writer.append_many(audit_events)

        # --- Update payment status for matched/exception records ------------ #
        self._update_payment_status(results)

        summary: dict[str, Any] = {
            **matcher_summary,
            "reconciliation_results_rows": len(recon_df),
            "audit_events": len(audit_events),
            "config": {
                "amount_tolerance": self.config.amount_tolerance,
                "date_window_days": self.config.date_window_days,
                "fuzzy_threshold": self.config.fuzzy_threshold,
                "stale_days": self.config.stale_days,
            },
        }
        self._print_summary(summary)
        return summary

    # -- row builders ------------------------------------------------------- #

    @staticmethod
    def _payment_row(r, reconciled_by: str) -> dict[str, Any]:
        return {
            "recon_id": r.recon_id,
            "batch_id": r.batch_id,
            "payment_id": r.payment_id,
            "ledger_transaction_id": r.ledger_transaction_id,
            "match_status": r.match_status,
            "match_confidence": r.match_confidence,
            "match_method": r.match_method,
            "exception_reason": r.exception_reason,
            "reconciled_at": r.reconciled_at,
            "reconciled_by": reconciled_by,
        }

    def _unmatched_ledger_rows(
        self, ledger, batch_id: str, reconciled_by: str
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seq = 0
        for entry in ledger.all_entries:
            if entry.transaction_id in ledger.consumed:
                continue
            seq += 1
            rows.append(
                {
                    "recon_id": f"RECON-{batch_id}-L{seq:06d}",
                    "batch_id": batch_id,
                    "payment_id": None,
                    "ledger_transaction_id": entry.transaction_id,
                    "match_status": M.MatchStatus.UNMATCHED_LEDGER,
                    "match_confidence": 0.0,
                    "match_method": None,
                    "exception_reason": None,
                    "reconciled_at": self.as_of,
                    "reconciled_by": reconciled_by,
                }
            )
        return rows

    @staticmethod
    def _build_audit_events(results: list, batch_id: str) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for r in results:
            if r.match_status == M.MatchStatus.MATCHED:
                events.append(
                    {
                        "event_type": M.AuditEventType.MATCH,
                        "entity_type": "reconciliation",
                        "entity_id": r.recon_id,
                        "action": "match",
                        "batch_id": batch_id,
                        "payload": {
                            "payment_id": r.payment_id,
                            "ledger_transaction_id": r.ledger_transaction_id,
                            "match_method": r.match_method,
                            "match_confidence": r.match_confidence,
                        },
                    }
                )
            elif r.match_status == M.MatchStatus.EXCEPTION:
                events.append(
                    {
                        "event_type": M.AuditEventType.EXCEPTION_OPEN,
                        "entity_type": "exception",
                        "entity_id": r.recon_id,
                        "action": "exception_open",
                        "batch_id": batch_id,
                        "payload": {
                            "payment_id": r.payment_id,
                            "ledger_transaction_id": r.ledger_transaction_id,
                            "exception_reason": r.exception_reason,
                        },
                    }
                )
        return events

    def _update_payment_status(self, results: list) -> None:
        """Mark payment records as matched, unmatched, or exception in the source table."""
        status_map = {
            M.MatchStatus.MATCHED: M.PaymentStatus.MATCHED,
            M.MatchStatus.UNMATCHED_PAYMENT: M.PaymentStatus.UNMATCHED,
            M.MatchStatus.EXCEPTION: M.PaymentStatus.EXCEPTION,
        }
        rows = [{"payment_id": r.payment_id, "status": status_map[r.match_status]} for r in results]
        if not rows:
            return
        df = pd.DataFrame(rows)
        # Read full payment records, update statuses, rewrite.
        payments = self.store.read_table(M.PaymentRecord.TABLE)
        status_map = dict(zip(df["payment_id"], df["status"], strict=True))
        payments["status"] = payments["payment_id"].map(status_map).fillna(payments["status"])
        self.store.write_dataframe(M.PaymentRecord.TABLE, payments, mode="replace")

    @staticmethod
    def _print_summary(summary: dict[str, Any]) -> None:
        table = Table(title=f"Reconciliation — {summary['batch_id']}")
        table.add_column("Metric", style="bold")
        table.add_column("Value", justify="right")
        for key in [
            "payments_processed",
            "ledger_records_processed",
            "matched_exact",
            "matched_fuzzy",
            "matched_total",
            "unmatched_payment",
            "exception_duplicate",
            "exception_amount_mismatch",
            "exception_stale",
            "exception_total",
            "unmatched_ledger",
            "reconciliation_results_rows",
            "audit_events",
            "total_amount_reconciled",
            "reconciliation_rate",
        ]:
            value = summary.get(key)
            if isinstance(value, float):
                value = f"{value:,.4f}" if key == "reconciliation_rate" else f"{value:,.2f}"
            else:
                value = f"{value:,}"
            table.add_row(key.replace("_", " ").title(), value)
        console.print(table)


def run_recon(
    db_path: str | Path = str(DEFAULT_DB_PATH),
    config_path: Path | str | None = None,
    batch_id: str = "BATCH-001",
    file_id: str | None = None,
    as_of: datetime = AS_OF_DATETIME,
) -> dict[str, Any]:
    """Convenience entry point: open a store, run, close, return summary."""
    store = DuckDBStore(db_path)
    try:
        pipeline = ReconciliationPipeline(store=store, config=load_config(config_path), as_of=as_of)
        return pipeline.run(batch_id=batch_id, file_id=file_id)
    finally:
        store.close()
