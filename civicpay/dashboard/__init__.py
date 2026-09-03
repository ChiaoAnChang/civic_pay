"""Streamlit dashboard (spec §17.3 Ticket 8)."""

from civicpay.dashboard.app import render, run_streamlit_app  # noqa: F401
from civicpay.dashboard.extractors import (  # noqa: F401
    dq_dataset_scores,
    dq_scores,
    exception_queue,
    recent_audit_events,
    reconciliation_summary,
)
