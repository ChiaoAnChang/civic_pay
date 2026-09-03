"""Tests for the Streamlit dashboard extractors (spec §17.3 Ticket 8)."""

from __future__ import annotations

import pytest
from civicpay.dashboard.extractors import (
    dq_dataset_scores,
    dq_scores,
    enrollment_mismatches,
    enrollment_summary,
    exception_queue,
    recent_audit_events,
    reconciliation_summary,
)
from civicpay.data import models as M
from civicpay.data.synthetic import AS_OF_DATETIME, generate_all
from civicpay.enrollment.pipeline import EnrollmentPipeline
from civicpay.quality.pipeline import QualityPipeline
from civicpay.recon.pipeline import ReconciliationPipeline
from civicpay.storage.duckdb import DuckDBStore


@pytest.fixture
def seeded_store():
    store = DuckDBStore(":memory:")
    store.init_schema()
    data = generate_all(seed=11, volumes={"customers": 100, "accounts": 50, "transactions": 500})
    store.write_many(data, mode="replace")
    ReconciliationPipeline(store=store).run(batch_id="DASH1")
    QualityPipeline(store=store).run(batch_id="DQDASH")
    return store


# --------------------------------------------------------------------------- #
# Reconciliation summary
# --------------------------------------------------------------------------- #


def test_reconciliation_summary_has_rate_and_counts(seeded_store):
    rec = reconciliation_summary(seeded_store, batch_id="DASH1")
    assert rec["total"] > 0
    assert rec["matched"] >= 0
    assert rec["exceptions"] == rec["total"] - rec["matched"]
    assert 0.0 <= rec["reconciliation_rate"] <= 100.0
    assert rec["matched"] + rec["exceptions"] == rec["total"]


def test_reconciliation_summary_separates_payment_rate_from_ledger_coverage(seeded_store):
    """Regression for a real bug: the dashboard used to recompute
    reconciliation_rate over *every* reconciliation_results row, including
    unmatched_ledger rows the pipeline appends for ledger transactions with
    no corresponding payment -- giving the same-named metric two different
    values in one product (the recon engine itself, civicpay.recon.matcher,
    always computes it payment-side-only). Ledger coverage is now its own,
    separate field instead of being blended into the headline rate.

    Uses a fresh store with a ledger much larger than the payment file (the
    project's actual default-seed shape) rather than the shared
    ``seeded_store`` fixture's small 500-transaction ledger, which the recon
    matcher's exact-match bucket fully consumes (0 unmatched ledger rows) --
    that would defeat the point of this specific assertion.
    """
    store = DuckDBStore(":memory:")
    store.init_schema()
    data = generate_all(seed=11, volumes={"customers": 500, "accounts": 200, "transactions": 2000})
    store.write_many(data, mode="replace")
    ReconciliationPipeline(store=store).run(batch_id="RATE-CHECK")

    dash = reconciliation_summary(store, batch_id="RATE-CHECK")
    assert dash["ledger_total"] > dash["total"]
    assert dash["unmatched_ledger"] == dash["ledger_total"] - dash["total"]
    assert 0.0 <= dash["ledger_coverage_rate"] <= 100.0
    store.close()


def test_reconciliation_summary_by_status_and_method(seeded_store):
    rec = reconciliation_summary(seeded_store, batch_id="DASH1")
    assert "matched" in rec["by_status"]
    # by_status covers every reconciliation_results row (including
    # unmatched_ledger), so it sums to ledger_total, not the payment-side total.
    assert sum(rec["by_status"].values()) == rec["ledger_total"]
    assert sum(rec["by_method"].values()) == rec["matched"]  # only matched rows have a method


def test_reconciliation_summary_empty_store():
    store = DuckDBStore(":memory:")
    store.init_schema()
    rec = reconciliation_summary(store)
    assert rec == {
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
    store.close()


# --------------------------------------------------------------------------- #
# DQ scores
# --------------------------------------------------------------------------- #


def test_dq_scores_one_row_per_check(seeded_store):
    checks = dq_scores(seeded_store)
    assert len(checks) > 0
    assert set(checks["dataset_name"]).issuperset({"accounts", "transactions"})
    assert (checks["passed"].astype(bool)).any()


def test_dq_dataset_scores_aggregates(seeded_store):
    agg = dq_dataset_scores(seeded_store)
    assert len(agg) == 4  # transactions, accounts, customers, payment_records
    assert (agg["quality_score"] <= 100.0).all()
    assert agg["checks"].sum() == dq_scores(seeded_store).shape[0]


def test_dq_dataset_scores_reports_anomaly_rate_separately(seeded_store):
    agg = dq_dataset_scores(seeded_store)
    row = agg[agg["dataset_name"] == "transactions"].iloc[0]
    # transactions has an anomaly check (config/dq_checks.yml); the default
    # type_weights.anomaly: 0.0 excludes it from quality_score, and its rate
    # is surfaced separately instead of folded into a flat mean.
    assert row["anomaly_rate"] is not None
    other = agg[agg["dataset_name"] != "transactions"]
    assert other["anomaly_rate"].isna().all()  # no anomaly checks configured for these


def test_dq_scores_empty_store():
    store = DuckDBStore(":memory:")
    store.init_schema()
    assert dq_scores(store).empty
    assert dq_dataset_scores(store).empty
    store.close()


# --------------------------------------------------------------------------- #
# Exception queue
# --------------------------------------------------------------------------- #


def test_exception_queue_has_aging_and_priority(seeded_store):
    eq = exception_queue(seeded_store, AS_OF_DATETIME)
    if eq.empty:
        pytest.skip("no exceptions routed in this seed")
    for col in (
        "exception_id",
        "priority",
        "status",
        "age_days",
        "sla_days",
        "amount_at_risk",
        "priority_score",
    ):
        assert col in eq.columns
    # Sorted most urgent (highest priority_score) first.
    scores = eq["priority_score"].tolist()
    assert scores == sorted(scores, reverse=True)


# --------------------------------------------------------------------------- #
# Enrollment (Ticket 13)
# --------------------------------------------------------------------------- #


@pytest.fixture
def enrolled_store():
    store = DuckDBStore(":memory:")
    store.init_schema()
    data = generate_all(
        seed=11,
        volumes={"customers": 100, "accounts": 50, "transactions": 500, "pending_enrollments": 60},
    )
    store.write_many(data, mode="replace")
    EnrollmentPipeline(store=store).run()
    return store


def test_enrollment_summary_counts(enrolled_store):
    summary = enrollment_summary(enrolled_store)
    assert summary["total"] == 60
    assert summary["accepted"] + summary["mismatch"] + summary["rejected"] == 60


def test_enrollment_summary_empty_store():
    store = DuckDBStore(":memory:")
    store.init_schema()
    summary = enrollment_summary(store)
    assert summary == {"total": 0, "pending": 0, "accepted": 0, "mismatch": 0, "rejected": 0}
    store.close()


def test_enrollment_mismatches_has_both_computed_values(enrolled_store):
    df = enrollment_mismatches(enrolled_store, AS_OF_DATETIME)
    assert not df.empty
    for col in (
        "enrollment_id",
        "method_a_amount",
        "method_b_amount",
        "delta",
        "age_days",
        "sla_days",
    ):
        assert col in df.columns
    # Sorted most urgent first.
    scores = df["priority_score"].tolist()
    assert scores == sorted(scores, reverse=True)
    # Backlog cohort is included alongside real mismatches.
    assert any("BACKLOG" in eid for eid in df["exception_id"])


def test_enrollment_mismatches_empty_store():
    store = DuckDBStore(":memory:")
    store.init_schema()
    assert enrollment_mismatches(store, AS_OF_DATETIME).empty
    store.close()


def test_resolve_enrollment_mismatch_accept_a_writes_accepted_row(enrolled_store):
    from civicpay.enrollment.pipeline import resolve_enrollment_mismatch

    df = enrollment_mismatches(enrolled_store, AS_OF_DATETIME)
    real = df[~df["exception_id"].str.contains("BACKLOG")].iloc[0]
    before = enrolled_store.table_count(M.AcceptedEnrollment.TABLE)

    result = resolve_enrollment_mismatch(
        enrolled_store,
        exception_id=real["exception_id"],
        decision="accept_a",
        root_cause="manual review confirmed method A",
        as_of=AS_OF_DATETIME,
    )
    assert result["status"] == M.ExceptionStatus.RESOLVED
    assert enrolled_store.table_count(M.AcceptedEnrollment.TABLE) == before + 1

    accepted = enrolled_store.read_table(M.AcceptedEnrollment.TABLE)
    row = accepted[accepted["enrollment_id"] == real["enrollment_id"]].iloc[0]
    assert row["expected_payout"] == pytest.approx(real["method_a_amount"])


def test_resolve_enrollment_mismatch_reject_marks_pending_rejected(enrolled_store):
    from civicpay.enrollment.pipeline import resolve_enrollment_mismatch

    df = enrollment_mismatches(enrolled_store, AS_OF_DATETIME)
    real = df[~df["exception_id"].str.contains("BACKLOG")].iloc[0]

    resolve_enrollment_mismatch(
        enrolled_store,
        exception_id=real["exception_id"],
        decision="reject",
        root_cause="dual-source disagreement not resolvable",
        as_of=AS_OF_DATETIME,
    )
    pending = enrolled_store.read_table(M.PendingEnrollment.TABLE)
    row = pending[pending["enrollment_id"] == real["enrollment_id"]].iloc[0]
    assert row["status"] == M.EnrollmentStatus.REJECTED


def test_resolve_enrollment_mismatch_backlog_item_has_no_pending_row(enrolled_store):
    """Backlog cohort items bypass pending_enrollments by design; resolving
    one still resolves the exception, just without an accepted_enrollments
    completion (there's no source record to complete)."""
    from civicpay.enrollment.pipeline import resolve_enrollment_mismatch

    df = enrollment_mismatches(enrolled_store, AS_OF_DATETIME)
    backlog = df[df["exception_id"].str.contains("BACKLOG")].iloc[0]
    before = enrolled_store.table_count(M.AcceptedEnrollment.TABLE)

    result = resolve_enrollment_mismatch(
        enrolled_store,
        exception_id=backlog["exception_id"],
        decision="accept_a",
        root_cause="demo backlog item",
        as_of=AS_OF_DATETIME,
    )
    assert result["status"] == M.ExceptionStatus.RESOLVED
    assert (
        enrolled_store.table_count(M.AcceptedEnrollment.TABLE) == before
    )  # no source row to complete


def test_resolve_enrollment_mismatch_unknown_decision_raises(enrolled_store):
    from civicpay.enrollment.pipeline import resolve_enrollment_mismatch

    df = enrollment_mismatches(enrolled_store, AS_OF_DATETIME)
    real = df.iloc[0]
    with pytest.raises(ValueError, match="Unknown decision"):
        resolve_enrollment_mismatch(
            enrolled_store,
            exception_id=real["exception_id"],
            decision="bogus",
            root_cause="x",
        )


def test_exception_queue_empty_store():
    store = DuckDBStore(":memory:")
    store.init_schema()
    assert exception_queue(store, AS_OF_DATETIME).empty
    store.close()


# --------------------------------------------------------------------------- #
# Audit events
# --------------------------------------------------------------------------- #


def test_recent_audit_events_sorted_desc(seeded_store):
    events = recent_audit_events(seeded_store, limit=20)
    assert len(events) <= 20
    if not events.empty:
        # newest first
        ts = events["timestamp"].tolist()
        assert ts == sorted(ts, reverse=True)


def test_recent_audit_events_empty_store():
    store = DuckDBStore(":memory:")
    store.init_schema()
    assert recent_audit_events(store).empty
    store.close()


# --------------------------------------------------------------------------- #
# App import + run_streamlit_app callable
# --------------------------------------------------------------------------- #


def test_dashboard_app_imports():
    from civicpay.dashboard import app as app_module

    assert callable(app_module.run_streamlit_app)
    assert callable(app_module.render)


def test_cli_dashboard_command_registered(monkeypatch):
    """`civicpay dashboard` is wired and launches streamlit (mocked)."""
    from civicpay.cli import app as cli_app
    from typer.testing import CliRunner

    runner = CliRunner()
    # Mock run_streamlit_app so we don't actually start a server.
    import civicpay.dashboard.app as app_module

    monkeypatch.setattr(app_module, "run_streamlit_app", lambda target=None, db_path=None: 0)
    result = runner.invoke(cli_app, ["dashboard"])
    assert result.exit_code == 0


def test_cli_dashboard_db_path_forwarded(monkeypatch):
    import civicpay.dashboard.app as app_module
    from civicpay.cli import app as cli_app
    from typer.testing import CliRunner

    seen = {}

    def fake_run(target=None, db_path=None):
        seen["db_path"] = db_path
        return 0

    monkeypatch.setattr(app_module, "run_streamlit_app", fake_run)
    result = CliRunner().invoke(cli_app, ["dashboard", "--db-path", "some.duckdb"])
    assert result.exit_code == 0
    assert seen["db_path"] == "some.duckdb"


def test_cli_dashboard_missing_db_fails_cleanly():
    """A --db-path that doesn't exist exits 1 with a clean message, not a
    raw traceback (matches the existing pre-flight-error CLI convention)."""
    from civicpay.cli import app as cli_app
    from typer.testing import CliRunner

    result = CliRunner().invoke(cli_app, ["dashboard", "--db-path", "does-not-exist.duckdb"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


def test_cli_enroll_bare_launches_form(monkeypatch):
    """Bare `civicpay enroll` (no subcommand) launches the Streamlit form."""
    import civicpay.enrollment.forms as forms_module
    from civicpay.cli import app as cli_app
    from typer.testing import CliRunner

    seen = {}

    def fake_run(target=None, db_path=None):
        seen["called"] = True
        seen["db_path"] = db_path
        return 0

    monkeypatch.setattr(forms_module, "run_streamlit_app", fake_run)
    result = CliRunner().invoke(cli_app, ["enroll", "--db-path", "some.duckdb"])
    assert result.exit_code == 0
    assert seen == {"called": True, "db_path": "some.duckdb"}


def test_cli_run_all_end_to_end(tmp_path):
    """`civicpay run-all` runs every stage and the audit chain verifies."""
    from civicpay.cli import app as cli_app
    from typer.testing import CliRunner

    db = tmp_path / "e2e.duckdb"
    runner = CliRunner()
    result = runner.invoke(cli_app, ["run-all", "--db-path", str(db)])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "seed" in out and "ok" in out
    assert "reconcile" in out
    assert "data-quality" in out
    assert "exception-list" in out
    assert "audit-verify" in out and "verified" in out
    # The DB now contains artifacts from every stage.
    from civicpay.data import models as M
    from civicpay.storage.duckdb import DuckDBStore

    store = DuckDBStore(str(db))
    assert store.table_count(M.ReconciliationResult.TABLE) > 0
    assert store.table_count(M.DQResult.TABLE) > 0
    assert store.table_count(M.AuditEvent.TABLE) > 0
    store.close()


def test_cli_run_all_re_run_blocked(tmp_path):
    """Re-running `civicpay run-all` with the same --run-id is blocked pre-flight
    (before seed/recon write anything), exits 1, and surfaces a BLOCKED row."""
    from civicpay.cli import app as cli_app
    from typer.testing import CliRunner

    db = tmp_path / "rerun-all.duckdb"
    runner = CliRunner()
    first = runner.invoke(cli_app, ["run-all", "--db-path", str(db), "--run-id", "DUP"])
    assert first.exit_code == 0, first.output

    second = runner.invoke(cli_app, ["run-all", "--db-path", str(db), "--run-id", "DUP"])
    assert second.exit_code == 1
    assert "DUP-RECON" in second.output or "DUP-DQ" in second.output
    assert "BLOCKED" in second.output
    assert "--run-id" in second.output
