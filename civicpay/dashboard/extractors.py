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
    """Return reconciliation rate + outcome breakdown for a batch (or latest).

    ``reconciliation_rate`` is computed over **payment-side** rows only
    (``match_status != unmatched_ledger``) — matching how
    ``civicpay.recon.matcher`` / the CLI's own `run-all` summary compute the
    same-named metric (over `payments_processed`, not the full
    `reconciliation_results` table). This dashboard extractor used to
    recompute it independently over *every* row, including the
    `unmatched_ledger` rows `ReconciliationPipeline` appends for ledger
    entries that were never going to match anything (there are far fewer
    payments than ledger transactions by design) — same metric name, two
    different numbers in one product (0.89 vs. ~0.018 on the default seed).
    Ledger coverage is a real, separate story and is now reported as its own
    field (`ledger_coverage_rate`) rather than blended into the headline.
    """
    df = store.read_table(M.ReconciliationResult.TABLE)
    if batch_id:
        df = df[df["batch_id"] == batch_id]
    if df.empty:
        return {
            "total": 0,
            "matched": 0,
            "exceptions": 0,
            "reconciliation_rate": 0.0,
            "ledger_total": 0,
            "unmatched_ledger": 0,
            "ledger_coverage_rate": 0.0,
            "by_status": {},
            "by_method": {},
        }
    payments_df = df[df["match_status"] != M.MatchStatus.UNMATCHED_LEDGER]
    total = int(len(payments_df))
    matched = int((payments_df["match_status"] == M.MatchStatus.MATCHED).sum())
    exceptions = total - matched
    rate = round(100.0 * matched / total, 2) if total else 0.0

    ledger_total = int(len(df))
    unmatched_ledger = int((df["match_status"] == M.MatchStatus.UNMATCHED_LEDGER).sum())
    ledger_coverage_rate = (
        round(100.0 * (ledger_total - unmatched_ledger) / ledger_total, 2) if ledger_total else 0.0
    )

    by_status = df["match_status"].value_counts().to_dict()
    by_method = (
        payments_df["match_method"].value_counts().to_dict()
        if "match_method" in payments_df.columns
        else {}
    )
    return {
        "total": total,
        "matched": matched,
        "exceptions": exceptions,
        "reconciliation_rate": rate,
        "ledger_total": ledger_total,
        "unmatched_ledger": unmatched_ledger,
        "ledger_coverage_rate": ledger_coverage_rate,
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


def enrollment_summary(store: DuckDBStore) -> dict[str, Any]:
    """Outcome counts for the seeded ``pending_enrollments`` pool (Ticket 13).

    Scoped to that table specifically — the aged backlog cohort seeded by
    ``EnrollmentPipeline`` bypasses it by design (see
    ``EnrollmentPipeline._seed_backlog_cohort``), so those items are visible
    in :func:`enrollment_mismatches` but not counted here.
    """
    df = store.read_table(M.PendingEnrollment.TABLE)
    if df.empty:
        return {"total": 0, "pending": 0, "accepted": 0, "mismatch": 0, "rejected": 0}
    counts = df["status"].value_counts()
    return {
        "total": int(len(df)),
        "pending": int(counts.get(M.EnrollmentStatus.PENDING, 0)),
        "accepted": int(counts.get(M.EnrollmentStatus.ACCEPTED, 0)),
        "mismatch": int(counts.get(M.EnrollmentStatus.MISMATCH, 0)),
        "rejected": int(counts.get(M.EnrollmentStatus.REJECTED, 0)),
    }


def enrollment_mismatches(
    store: DuckDBStore, as_of: datetime, sla_days: int | None = None
) -> pd.DataFrame:
    """Enrollment dual-source-mismatch exceptions with both computed values.

    Joins the shared exception queue (filtered to
    ``source="enrollment_dual_source"``) with ``enrollment_dual_source_results``
    on the enrollment id encoded in ``reference_id``
    (``"pending_enrollments:{enrollment_id}"``).
    """
    items = ExceptionManager(store=store, as_of=as_of).list(sla_days=sla_days)
    rows = [i for i in items if i["source"] == M.ExceptionSource.ENROLLMENT_DUAL_SOURCE]
    if not rows:
        return pd.DataFrame()
    exc_df = pd.DataFrame(rows)
    exc_df["enrollment_id"] = exc_df["reference_id"].str.split(":", n=1).str[1]

    dsr = store.read_table(M.DualSourceResult.TABLE)
    dsr = dsr[["enrollment_id", "method_a_amount", "method_b_amount", "delta"]]

    merged = exc_df.merge(dsr, on="enrollment_id", how="left")
    return merged.sort_values("priority_score", ascending=False).reset_index(drop=True)


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
