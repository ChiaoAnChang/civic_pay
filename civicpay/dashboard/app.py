"""Streamlit dashboard for the CivicPay Open Framework (spec §17.3 Ticket 8).

Run with ``civicpay dashboard`` (which calls :func:`run_streamlit_app`) or
``streamlit run civicpay/dashboard/app.py``.

Views: reconciliation summary, DQ scores, exception queue (with aging), and
recent audit events. All data is read live from the DuckDB store.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from civicpay.audit.evidence import verify_chain
from civicpay.dashboard.extractors import (
    dq_dataset_scores,
    dq_scores,
    exception_queue,
    recent_audit_events,
    reconciliation_summary,
)
from civicpay.storage.duckdb import DEFAULT_DB_PATH, DuckDBStore


def render(store: DuckDBStore | None = None, as_of=None) -> None:  # noqa: ANN001
    """Render the dashboard. Imported lazily so tests don't require streamlit."""
    import streamlit as st

    from civicpay.data.synthetic import AS_OF_DATETIME

    as_of_dt = as_of or AS_OF_DATETIME
    store = store or DuckDBStore(DEFAULT_DB_PATH)

    st.set_page_config(page_title="CivicPay Dashboard", layout="wide")
    st.title("CivicPay Reconciliation Dashboard")

    # -- Reconciliation summary ------------------------------------------ #
    st.header("Reconciliation Summary")
    rec = reconciliation_summary(store)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Reconciliation Rate", f"{rec['reconciliation_rate']}%")
    c2.metric("Matched", rec["matched"])
    c3.metric("Exceptions", rec["exceptions"])
    c4.metric("Total Payments", rec["total"])
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("By Status")
        st.bar_chart(pd_index(rec["by_status"]))
    with col_b:
        st.subheader("By Match Method")
        st.bar_chart(pd_index(rec["by_method"]))

    # -- Data quality ---------------------------------------------------- #
    st.header("Data Quality Scores")
    ds_scores = dq_dataset_scores(store)
    if not ds_scores.empty:
        st.dataframe(ds_scores, use_container_width=True, hide_index=True)
        st.bar_chart(ds_scores.set_index("dataset_name")["quality_score"])
    checks = dq_scores(store)
    if not checks.empty:
        st.subheader("Per-Check Detail")
        st.dataframe(checks, use_container_width=True, hide_index=True)

    # -- Exception queue ------------------------------------------------- #
    st.header("Exception Queue (with SLA aging)")
    exc = exception_queue(store, as_of=as_of_dt)
    if not exc.empty:
        st.dataframe(
            exc[
                [
                    "exception_id",
                    "source",
                    "priority",
                    "status",
                    "age_days",
                    "sla_days",
                    "amount_at_risk",
                    "priority_score",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )
        st.subheader("Exceptions by Priority")
        st.bar_chart(exc["priority"].value_counts())
    else:
        st.info("No exceptions in the queue.")

    # -- Audit log ------------------------------------------------------- #
    st.header("Recent Audit Events")
    verification = verify_chain(store)
    v_color = "verified" if verification["verified"] else "BROKEN"
    st.caption(f"Audit chain: {v_color} — {verification['event_count']} event(s)")
    events = recent_audit_events(store, limit=100)
    if not events.empty:
        st.dataframe(events, use_container_width=True, hide_index=True)
    else:
        st.info("No audit events yet.")


def pd_index(d: dict) -> Any:  # noqa: ANN001
    """Convert a {label: value} dict to a pandas Series for bar_chart."""
    import pandas as pd

    return pd.Series(d)


def run_streamlit_app(target: str | None = None) -> int:
    """Launch the dashboard via ``streamlit run``.

    Returns the streamlit process exit code (0 on clean shutdown). Used by the
    ``civicpay dashboard`` CLI command and the test suite.
    """
    import subprocess
    import sys

    target = target or str(Path(__file__).resolve())
    proc = subprocess.run(
        [sys.executable, "-m", "streamlit", "run", target, "--global.headless", "true"],
        check=False,
    )
    return proc.returncode


if __name__ == "__main__":
    render()
