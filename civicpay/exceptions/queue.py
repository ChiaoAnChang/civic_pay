"""Exception queue manager (spec §14.3 / §17.3 Ticket 6).

Lists exceptions with computed priority scores (sorted most-urgent first),
assigns owners, and resolves items — capturing root cause and emitting a
tamper-evident ``exception_resolve`` audit event. Priority and SLA aging are
computed on read so they never go stale.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from civicpay.audit.ledger import AuditLedger
from civicpay.data import models as M
from civicpay.data.synthetic import AS_OF_DATETIME
from civicpay.exceptions.workflow import (
    DEFAULT_SLA_DAYS,
    age_days,
    age_factor,
    amount_at_risk_factor,
    compute_priority_score,
)
from civicpay.storage.duckdb import DuckDBStore

# Datasets whose records carry a dollar ``amount`` (for at-risk computation).
_AMOUNT_DATASETS = {
    "transactions": "transaction_id",
    "payment_records": "payment_id",
}


class ExceptionManager:
    """Reads and updates the exception queue against a DuckDB store."""

    def __init__(self, store: DuckDBStore, as_of: datetime = AS_OF_DATETIME) -> None:
        self.store = store
        self.as_of = as_of

    # -- reads ------------------------------------------------------------- #

    def _amount_at_risk(self, reference_id: str) -> float:
        """Look up the referenced record's amount; 0 when not applicable."""
        if not reference_id or ":" not in reference_id:
            return 0.0
        dataset, rid = reference_id.split(":", 1)
        id_field = _AMOUNT_DATASETS.get(dataset)
        if not id_field:
            return 0.0
        table = {"transactions": M.Transaction.TABLE, "payment_records": M.PaymentRecord.TABLE}[
            dataset
        ]
        try:
            df = self.store.read_table(table)
        except Exception:
            return 0.0
        if df.empty or id_field not in df.columns or "amount" not in df.columns:
            return 0.0
        match = df[df[id_field].astype(str) == rid]
        if match.empty:
            return 0.0
        return float(match["amount"].iloc[0])

    def list(
        self,
        status: str | None = None,
        sla_days: int = DEFAULT_SLA_DAYS,
    ) -> list[dict[str, Any]]:
        """Return exceptions with computed priority, sorted most-urgent first."""
        df = self.store.read_table(M.ExceptionItem.TABLE)
        if status:
            df = df[df["status"] == status]
        rows: list[dict[str, Any]] = []
        for _, r in df.iterrows():
            ref = "" if pd.isna(r["reference_id"]) else str(r["reference_id"])
            amt = self._amount_at_risk(ref)
            age = age_days(r["created_at"], self.as_of)
            score = compute_priority_score(r["priority"], amt, age, sla_days)
            rows.append(
                {
                    "exception_id": r["exception_id"],
                    "source": r["source"],
                    "reference_id": ref,
                    "priority": r["priority"],
                    "status": r["status"],
                    "assigned_to": None if pd.isna(r["assigned_to"]) else r["assigned_to"],
                    "age_days": age,
                    "amount_at_risk": round(amt, 2),
                    "amount_at_risk_factor": amount_at_risk_factor(amt),
                    "age_factor": age_factor(age, sla_days),
                    "priority_score": score,
                    "created_at": r["created_at"],
                }
            )
        rows.sort(key=lambda x: x["priority_score"], reverse=True)
        return rows

    # -- writes ------------------------------------------------------------ #

    def assign(self, exception_id: str, owner: str) -> None:
        """Assign an owner and move the item to in_progress."""
        row = self._get(exception_id)
        if row["status"] == "resolved":
            raise ValueError(f"Exception {exception_id} is already resolved.")
        self.store.conn.execute(
            f"UPDATE {M.ExceptionItem.TABLE} SET assigned_to = ?, status = ? "
            f"WHERE exception_id = ?",
            [owner, M.ExceptionStatus.IN_PROGRESS, exception_id],
        )

    def resolve(
        self,
        exception_id: str,
        root_cause: str,
        actor: str = "system",
        resolution_notes: str | None = None,
    ) -> dict[str, Any]:
        """Resolve an exception, capture root cause, and emit an audit event."""
        row = self._get(exception_id)
        if row["status"] == M.ExceptionStatus.RESOLVED:
            raise ValueError(f"Exception {exception_id} is already resolved.")
        self.store.conn.execute(
            f"UPDATE {M.ExceptionItem.TABLE} SET status = ?, resolved_at = ?, "
            f"root_cause = ?, resolution_notes = ? WHERE exception_id = ?",
            [
                M.ExceptionStatus.RESOLVED,
                self.as_of,
                root_cause,
                resolution_notes,
                exception_id,
            ],
        )
        # Unique batch id per exception avoids audit event_id collisions.
        ledger = AuditLedger(store=self.store, actor=actor, as_of=self.as_of)
        ledger.append(
            event_type=M.AuditEventType.EXCEPTION_RESOLVE,
            entity_type="exception",
            entity_id=exception_id,
            action="exception_resolve",
            batch_id=f"EXC-RESOLVE-{exception_id}",
        )
        return {
            "exception_id": exception_id,
            "status": M.ExceptionStatus.RESOLVED,
            "resolved_at": self.as_of,
            "root_cause": root_cause,
        }

    def _get(self, exception_id: str) -> dict[str, Any]:
        df = self.store.read_table(M.ExceptionItem.TABLE)
        match = df[df["exception_id"] == exception_id]
        if match.empty:
            raise ValueError(f"Exception {exception_id} not found.")
        return match.iloc[0].to_dict()
