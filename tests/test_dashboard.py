"""Tests for the Streamlit dashboard extractors (spec §17.3 Ticket 8)."""

from __future__ import annotations

import pytest
from civicpay.dashboard.extractors import (
    dq_dataset_scores,
    dq_scores,
    exception_queue,
    recent_audit_events,
    reconciliation_summary,
)
from civicpay.data.synthetic import AS_OF_DATETIME, generate_all
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


def test_reconciliation_summary_by_status_and_method(seeded_store):
    rec = reconciliation_summary(seeded_store, batch_id="DASH1")
    assert "matched" in rec["by_status"]
    assert sum(rec["by_status"].values()) == rec["total"]
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
        "amount_at_risk",
        "priority_score",
    ):
        assert col in eq.columns
    # Sorted most urgent (highest priority_score) first.
    scores = eq["priority_score"].tolist()
    assert scores == sorted(scores, reverse=True)


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

    monkeypatch.setattr(app_module, "run_streamlit_app", lambda target=None: 0)
    result = runner.invoke(cli_app, ["dashboard"])
    assert result.exit_code == 0


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
