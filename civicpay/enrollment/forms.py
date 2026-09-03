"""Streamlit multi-page enrollment form (Ticket 13 §4).

Run with ``civicpay enroll`` or ``streamlit run civicpay/enrollment/forms.py``.
Captures one enrollment candidate across three steps with constrained
widgets (selectboxes for enum fields, clamped date/number inputs), validates
it live, and only allows submission once :func:`validators.validate` reports
zero blocking errors — dirty data cannot be submitted. On submit, the record
goes through the same dual-source agreement gate as the batch CLI path
(:meth:`EnrollmentPipeline.submit_one`).

Matches the conventions in ``civicpay/dashboard/app.py``: streamlit is
imported lazily inside :func:`render` (so tests don't require it), the store
is injectable for testability, and :func:`run_streamlit_app` launches this
file via ``streamlit run``.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any


def _next_enrollment_id(store: Any) -> str:
    from civicpay.data import models as M

    pending = store.read_table(M.PendingEnrollment.TABLE)
    accepted = store.read_table(M.AcceptedEnrollment.TABLE)
    existing = set(pending["enrollment_id"]) | set(accepted["enrollment_id"])
    n = 1
    while f"ENR-{n:06d}" in existing:
        n += 1
    return f"ENR-{n:06d}"


def render(store: Any = None, as_of: datetime | None = None) -> None:  # noqa: ANN401
    """Render the enrollment form. Imported lazily so tests don't require streamlit."""
    import streamlit as st

    from civicpay.data.synthetic import AS_OF_DATETIME
    from civicpay.enrollment.pipeline import EnrollmentPipeline
    from civicpay.enrollment.validators import load_rules, validate
    from civicpay.storage.duckdb import DuckDBStore, resolve_db_path

    as_of_dt = as_of or AS_OF_DATETIME
    store = store or DuckDBStore(resolve_db_path())
    store.init_schema()
    rules = load_rules()
    programs = list(rules.get("programs", {}))
    regions = rules.get("regions", [])

    st.set_page_config(page_title="CivicPay Enrollment", layout="centered")
    st.title("Enrollment — Constrained Input Form")
    st.caption("Point-of-capture validation: dirty data cannot be submitted.")

    if msg := st.session_state.pop("enroll_last_outcome", None):
        {
            "accepted": st.success,
            "mismatch": st.warning,
            "rejected": st.error,
        }[msg](f"Last submission outcome: **{msg}**.")

    st.session_state.setdefault("enroll_step", 1)
    st.session_state.setdefault("enroll_data", {})
    step = st.session_state["enroll_step"]
    data = st.session_state["enroll_data"]

    st.progress(step / 3, text=f"Step {step} of 3")

    if step == 1:
        st.subheader("Step 1 — Entity & Program")
        data["entity_id"] = st.text_input("Entity id", value=data.get("entity_id", ""))
        data["program_code"] = st.selectbox(
            "Program",
            programs,
            index=programs.index(data["program_code"])
            if data.get("program_code") in programs
            else 0,
        )
        data["region"] = st.selectbox(
            "Region",
            regions,
            index=regions.index(data["region"]) if data.get("region") in regions else 0,
        )
        if st.button("Next", type="primary"):
            st.session_state["enroll_step"] = 2
            st.rerun()

    elif step == 2:
        st.subheader("Step 2 — Terms")
        cfg = rules["programs"][data["program_code"]]
        min_date = datetime.fromisoformat(str(rules["min_enrollment_date"])).date()
        max_date = as_of_dt.date()
        data["enrollment_date"] = st.date_input(
            "Enrollment date",
            value=data.get("enrollment_date") or max_date,
            min_value=min_date,
            max_value=max_date,
        )
        data["term_months"] = st.number_input(
            "Term (months)",
            min_value=cfg["term_months"]["min"],
            max_value=cfg["term_months"]["max"],
            value=data.get("term_months") or cfg["term_months"]["min"],
            step=1,
        )
        data["incentive_amount"] = st.number_input(
            "Incentive amount",
            min_value=0.0,
            max_value=float(cfg["max_incentive_amount"]),
            value=data.get("incentive_amount") or 0.0,
            step=0.01,
            format="%.2f",
        )
        data["submitted_by"] = st.text_input(
            "Submitted by (operator id)", value=data.get("submitted_by", "")
        )
        col1, col2 = st.columns(2)
        if col1.button("Back"):
            st.session_state["enroll_step"] = 1
            st.rerun()
        if col2.button("Next", type="primary"):
            st.session_state["enroll_step"] = 3
            st.rerun()

    elif step == 3:
        st.subheader("Step 3 — Review & Submit")
        data.setdefault("enrollment_id", _next_enrollment_id(store))
        raw = {
            "enrollment_id": data["enrollment_id"],
            "entity_id": data.get("entity_id", ""),
            "program_code": data.get("program_code", ""),
            "enrollment_date": data["enrollment_date"].isoformat()
            if data.get("enrollment_date")
            else "",
            "incentive_amount": f"{data.get('incentive_amount', 0.0):.2f}",
            "term_months": str(int(data.get("term_months", 0))),
            "region": data.get("region", ""),
            "submitted_by": data.get("submitted_by", ""),
        }
        st.table({"field": list(raw.keys()), "value": list(raw.values())})

        result = validate(raw, rules, as_of_dt)
        for issue in result.errors:
            st.error(f"**{issue.field}**: {issue.message}")
        for issue in result.warnings:
            st.warning(f"**{issue.field}**: {issue.message}")
        if result.is_valid:
            st.success("Validation passed — ready to submit.")

        col1, col2 = st.columns(2)
        if col1.button("Back"):
            st.session_state["enroll_step"] = 2
            st.rerun()
        if col2.button("Submit", type="primary", disabled=not result.is_valid):
            outcome = EnrollmentPipeline(store=store, rules=rules, as_of=as_of_dt).submit_one(raw)
            st.session_state["enroll_step"] = 1
            st.session_state["enroll_data"] = {}
            st.session_state["enroll_last_outcome"] = outcome.outcome
            st.rerun()


def run_streamlit_app(target: str | None = None, db_path: str | None = None) -> int:
    """Launch the enrollment form via ``streamlit run`` (reuses the
    dashboard's launcher, which already takes an explicit target path and
    forwards ``db_path`` via the ``CIVICPAY_DB_PATH`` env var)."""
    from civicpay.dashboard.app import run_streamlit_app as _run

    return _run(target or str(Path(__file__).resolve()), db_path=db_path)


if __name__ == "__main__":
    render()
