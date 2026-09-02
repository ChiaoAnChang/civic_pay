"""Pure data extractors for the Streamlit dashboard.

These functions read from the DuckDB store and return plain pandas DataFrames /
dicts. They are deliberately streamlit-free so they can be unit-tested without a
running dashboard server. ``app.py`` renders their output.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pandas as pd

from civicpay.data import models as M
from civicpay.exceptions.queue import ExceptionManager
from civicpay.storage.duckdb import DuckDBStore


def reconciliation_summary(store: DuckDBStore, batch_id: str | None = None) -> dict[str, Any]:
    """Return reconciliation rate + outcome breakdown for a batch (or latest)."""
    df = store.read_table(M.ReconciliationResult.TABLE)
    if batch_id:
        df = df[df["batch_id"] == batch_id]
    if df.empty:
        return {
            "total": 0,
            "matched": 0,
            "exceptions": 0,
            "reconciliation_rate": 0.0,
            "by_status": {},
            "by_method": {},
        }
    total = int(len(df))
    matched = int((df["match_status"] == M.MatchStatus.MATCHED).sum())
    exceptions = total - matched
    rate = round(100.0 * matched / total, 2) if total else 0.0
    by_status = df["match_status"].value_counts().to_dict()
    by_method = df["match_method"].value_counts().to_dict() if "match_method" in df.columns else {}
    return {
        "total": total,
        "matched": matched,
        "exceptions": exceptions,
        "reconciliation_rate": rate,
        "by_status": {str(k): int(v) for k, v in by_status.items()},
        "by_method": {str(k): int(v) for k, v in by_method.items()},
    }


def dq_scores(store: DuckDBStore) -> pd.DataFrame:
    """Return one row per DQ check with its score and pass/fail."""
    df = store.read_table(M.DQResult.TABLE)
    if df.empty:
        return pd.DataFrame(
            columns=[
                "dataset_name",
                "check_name",
                "check_type",
                "passed",
                "failing_records",
                "quality_score",
            ]
        )
    cols = [
        "dataset_name",
        "check_name",
        "check_type",
        "passed",
        "failing_records",
        "quality_score",
    ]
    return df[cols].sort_values(["dataset_name", "check_name"]).reset_index(drop=True)


def dq_dataset_scores(store: DuckDBStore) -> pd.DataFrame:
    """Per-dataset aggregate quality score (mean of check scores)."""
    df = store.read_table(M.DQResult.TABLE)
    if df.empty:
        return pd.DataFrame(columns=["dataset_name", "quality_score", "checks"])
    agg = (
        df.groupby("dataset_name")
        .agg(
            quality_score=("quality_score", "mean"),
            checks=("dq_check_id", "count"),
        )
        .reset_index()
    )
    agg["quality_score"] = agg["quality_score"].round(2)
    return agg.sort_values("dataset_name").reset_index(drop=True)


def exception_queue(store: DuckDBStore, as_of: datetime, sla_days: int = 7) -> pd.DataFrame:
    """Exception queue with computed aging + priority (most urgent first)."""
    items = ExceptionManager(store=store, as_of=as_of).list(sla_days=sla_days)
    if not items:
        return pd.DataFrame()
    return pd.DataFrame(items)


def recent_audit_events(store: DuckDBStore, limit: int = 100) -> pd.DataFrame:
    """Most recent audit events (for the audit-events view).

    Orders by timestamp descending in SQL before limiting, so we get the truly
    most recent events (not just the first ``limit`` rows by insertion order).
    """
    df = store.query(
        f"SELECT event_id, timestamp, event_type, actor, entity_type, entity_id, action "
        f"FROM {M.AuditEvent.TABLE} ORDER BY timestamp DESC, event_id DESC LIMIT ?",
        [limit],
    )
    return df
