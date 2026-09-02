"""Pure data extractors for the Streamlit dashboard.

These functions read from the DuckDB store and return plain pandas DataFrames /
dicts. They are deliberately streamlit-free so they can be unit-tested without a
running dashboard server. ``app.py`` renders their output.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from civicpay.data import models as M
from civicpay.exceptions.queue import ExceptionManager
from civicpay.quality.pipeline import load_config
from civicpay.quality.scoring import anomaly_rate as _anomaly_rate
from civicpay.quality.scoring import dataset_quality_score
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


def dq_dataset_scores(store: DuckDBStore, config_path: Path | str | None = None) -> pd.DataFrame:
    """Per-dataset quality score (check-type-weighted, same formula as the CLI).

    Anomaly checks are excluded from ``quality_score`` by default
    (``type_weights.anomaly: 0.0``) and reported separately in
    ``anomaly_rate`` — see ``civicpay.quality.scoring.anomaly_rate``.
    """
    df = store.read_table(M.DQResult.TABLE)
    if df.empty:
        return pd.DataFrame(columns=["dataset_name", "quality_score", "anomaly_rate", "checks"])
    type_weights = load_config(config_path).type_weights
    rows = []
    for ds_name, group in df.groupby("dataset_name"):
        pairs = list(zip(group["check_type"], group["quality_score"], strict=False))
        rows.append(
            {
                "dataset_name": ds_name,
                "quality_score": dataset_quality_score(pairs, type_weights),
                "anomaly_rate": _anomaly_rate(pairs),
                "checks": len(group),
            }
        )
    return pd.DataFrame(rows).sort_values("dataset_name").reset_index(drop=True)


def exception_queue(
    store: DuckDBStore, as_of: datetime, sla_days: int | None = None
) -> pd.DataFrame:
    """Exception queue with computed aging + priority (most urgent first).

    ``sla_days=None`` (the default) resolves each item's SLA window from its
    severity; pass a value to use the same window for every item.
    """
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
