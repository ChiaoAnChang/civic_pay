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
    enrollment_mismatches,
    enrollment_summary,
    exception_queue,
    recent_audit_events,
    reconciliation_summary,
)
from civicpay.storage.duckdb import DuckDBStore, resolve_db_path


def render(store: DuckDBStore | None = None, as_of=None) -> None:  # noqa: ANN001
    """Render the dashboard. Imported lazily so tests don't require streamlit."""
    import streamlit as st

    from civicpay.data.synthetic import AS_OF_DATETIME

    as_of_dt = as_of or AS_OF_DATETIME
    store = store or DuckDBStore(resolve_db_path())

    st.set_page_config(page_title="CivicPay Dashboard", layout="wide")
    st.title("CivicPay Reconciliation Dashboard")

    # -- Reconciliation summary ------------------------------------------ #
    st.header("Reconciliation Summary")
    rec = reconciliation_summary(store)
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Reconciliation Rate", f"{rec['reconciliation_rate']}%")
    c2.metric("Matched", rec["matched"])
    c3.metric("Exceptions", rec["exceptions"])
    c4.metric("Total Payments", rec["total"])
    c5.metric(
        "Ledger Coverage",
        f"{rec['ledger_coverage_rate']}%",
        help=(
            f"{rec['unmatched_ledger']:,} of {rec['ledger_total']:,} ledger transactions "
            "have no corresponding payment in this batch's payment file — a separate "
            "story from the payment-side reconciliation rate above, not blended into it."
        ),
    )
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
                    "amount_basis",
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

    # -- Enrollment & validation (Ticket 13) ------------------------------ #
    st.header("Enrollment & Validation")
    # A resolve action below calls st.rerun() immediately, which aborts the
    # current script pass -- a plain st.success() call right before it would
    # never actually reach the client. Stash the message in session_state
    # and show it on the *next* run instead, then clear it.
    if msg := st.session_state.pop("enrollment_resolve_message", None):
        st.success(msg)
    enroll = enrollment_summary(store)
    e1, e2, e3, e4, e5 = st.columns(5)
    e1.metric("Candidates", enroll["total"])
    e2.metric("Pending", enroll["pending"])
    e3.metric("Accepted", enroll["accepted"])
    e4.metric("Mismatch", enroll["mismatch"])
    e5.metric("Rejected", enroll["rejected"])

    mismatches = enrollment_mismatches(store, as_of=as_of_dt)
    if not mismatches.empty:
        st.subheader("Dual-Source Mismatches")
        st.dataframe(
            mismatches[
                [
                    "exception_id",
                    "enrollment_id",
                    "method_a_amount",
                    "method_b_amount",
                    "delta",
                    "age_days",
                    "sla_days",
                    "priority_score",
                    "status",
                ]
            ],
            use_container_width=True,
            hide_index=True,
        )

        open_mismatches = mismatches[mismatches["status"] == "open"]
        if not open_mismatches.empty:
            st.subheader("Resolve a Mismatch")
            selected = st.selectbox("Exception", open_mismatches["exception_id"].tolist())
            row = open_mismatches[open_mismatches["exception_id"] == selected].iloc[0]
            st.caption(
                f"Method A: {row['method_a_amount']:.2f} · "
                f"Method B: {row['method_b_amount']:.2f} · "
                f"Delta: {row['delta']:.2f}"
            )
            decision = st.radio(
                "Decision",
                ["accept_a", "accept_b", "reject"],
                format_func=lambda d: {
                    "accept_a": "Accept Path A",
                    "accept_b": "Accept Path B",
                    "reject": "Reject & re-enter",
                }[d],
                horizontal=True,
            )
            root_cause = st.text_input("Root cause (required)")
            if st.button("Resolve"):
                if not root_cause.strip():
                    st.error("Root cause is required.")
                else:
                    from civicpay.enrollment.pipeline import resolve_enrollment_mismatch

                    resolve_enrollment_mismatch(
                        store,
                        exception_id=selected,
                        decision=decision,
                        root_cause=root_cause,
                        as_of=as_of_dt,
                    )
                    st.session_state["enrollment_resolve_message"] = (
                        f"Resolved {selected} ({decision})."
                    )
                    st.rerun()
    else:
        st.info("No enrollment mismatches.")

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


def run_streamlit_app(target: str | None = None, db_path: str | None = None) -> int:
    """Launch a dashboard/form script via ``streamlit run``.

    Returns the streamlit process exit code (0 on clean shutdown). Used by
    the ``civicpay dashboard`` / ``civicpay enroll`` CLI commands and the
    test suite. ``db_path``, if given, is passed to the subprocess via the
    ``CIVICPAY_DB_PATH`` env var (see ``civicpay.storage.duckdb.
    resolve_db_path``) — a `streamlit run` child process can't receive a
    Python call argument directly. Fails fast if the file doesn't exist,
    rather than letting ``DuckDBStore`` silently create an empty database and
    render a blank dashboard.
    """
    import os
    import subprocess
    import sys

    from civicpay.storage.duckdb import DB_PATH_ENV_VAR

    target = target or str(Path(__file__).resolve())
    env = os.environ.copy()
    if db_path is not None:
        resolved = Path(db_path).resolve()
        if not resolved.exists():
            raise FileNotFoundError(
                f"Database file not found: {resolved}. Run 'civicpay seed --db-path "
                f"{db_path}' first."
            )
        env[DB_PATH_ENV_VAR] = str(resolved)
    proc = subprocess.run(
        [sys.executable, "-m", "streamlit", "run", target, "--global.headless", "true"],
        check=False,
        env=env,
    )
    return proc.returncode


if __name__ == "__main__":
    render()
