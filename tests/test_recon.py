"""Tests for the reconciliation matcher and pipeline (spec §14.1 / Ticket 3)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pandas as pd
import pytest
from civicpay.audit.ledger import canonical_json, compute_event_hash
from civicpay.cli import app
from civicpay.data import models as M
from civicpay.data.synthetic import generate_all
from civicpay.recon.matcher import (
    ReconConfig,
    normalize_reference,
    reconcile,
)
from civicpay.recon.pipeline import ReconciliationPipeline
from civicpay.storage.duckdb import DuckDBStore
from typer.testing import CliRunner

# Expected outcome-class counts for the synthetic dataset (seed=42, defaults).
EXPECTED = {
    "matched_exact": 850,
    "matched_fuzzy": 40,
    "matched_total": 890,
    "unmatched_payment": 80,
    "exception_duplicate": 20,
    "exception_amount_mismatch": 10,
    "exception_total": 30,
    "unmatched_ledger": 49110,
    "reconciliation_results_rows": 50110,
    "audit_events": 920,
}


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def recon_inputs():
    """Deterministic payments + transactions (seed=42) as plain DataFrames."""
    data = generate_all(seed=42)
    return data["payment_records"], data["transactions"]


@pytest.fixture()
def seeded_store():
    """A fresh in-memory store pre-loaded with the full synthetic dataset."""
    store = DuckDBStore(":memory:")
    store.init_schema()
    data = generate_all(seed=42)
    store.write_many(
        {
            M.PaymentRecord.TABLE: data["payment_records"],
            M.Transaction.TABLE: data["transactions"],
        },
        mode="append",
    )
    yield store
    store.close()


# --------------------------------------------------------------------------- #
# Unit: reference normalization
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("REF-M-0001", "REFM0001"),
        (" ref_t_0002 ", "REFT0002"),
        ("ref m 0003", "REFM0003"),
        ("REF_M_0004", "REFM0004"),
        ("", ""),
    ],
)
def test_normalize_reference(raw, expected):
    assert normalize_reference(raw) == expected


# --------------------------------------------------------------------------- #
# Unit: config helpers
# --------------------------------------------------------------------------- #


def test_config_defaults_and_tolerances():
    cfg = ReconConfig()
    assert cfg.amount_tolerance == 1.0
    assert cfg.date_window_days == 1
    assert cfg.fuzzy_threshold == 0.85
    assert cfg.stale_days == 30
    assert cfg.within_amount(100.0, 100.5)
    assert not cfg.within_amount(100.0, 102.0)
    assert cfg.within_date_window(date(2026, 9, 1), date(2026, 9, 2))
    assert not cfg.within_date_window(date(2026, 9, 1), date(2026, 9, 5))


# --------------------------------------------------------------------------- #
# Unit: audit ledger primitives
# --------------------------------------------------------------------------- #


def test_canonical_json_is_deterministic_and_sorted():
    a = canonical_json({"b": 2, "a": 1})
    b = canonical_json({"a": 1, "b": 2})
    assert a == b
    assert a.index('"a"') < a.index('"b"')


def test_canonical_json_forces_utc():

    from civicpay.audit.ledger import canonical_json as cj

    naive = datetime(2026, 9, 1, 0, 0, 0)
    aware_utc = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
    # A naive datetime is treated as UTC, so it canonicalizes identically to the
    # explicit-UTC form (no local-timezone drift).
    assert cj({"ts": naive}) == cj({"ts": aware_utc})
    assert "+00:00" in cj({"ts": aware_utc})


def test_config_aliases_match_spec_names(tmp_path):
    """The spec's original config keys are accepted alongside the clear names."""
    from civicpay.recon.pipeline import load_config

    cfg_file = tmp_path / "recon.yml"
    cfg_file.write_text(
        "match_tolerance_amount: 2.0\n"
        "match_date_window_days: 3\n"
        "fuzzy_threshold: 0.9\n"
        "stale_days: 45\n"
    )
    cfg = load_config(cfg_file)
    assert cfg.amount_tolerance == 2.0
    assert cfg.date_window_days == 3
    assert cfg.fuzzy_threshold == 0.9
    assert cfg.stale_days == 45


def test_compute_event_hash_stable():
    payload = {"event_id": "EVT-1", "action": "match", "previous_hash": ""}
    assert compute_event_hash(payload) == compute_event_hash(payload)


def test_hash_chain_links_previous_hash(seeded_store):
    pipeline = ReconciliationPipeline(store=seeded_store)
    summary = pipeline.run(batch_id="CHAIN")
    events = seeded_store.read_table(M.AuditEvent.TABLE)
    events = events.sort_values(["timestamp", "event_id"]).reset_index(drop=True)
    assert len(events) == EXPECTED["audit_events"]
    # First event seeds the chain with an empty previous_hash.
    assert events.iloc[0]["previous_hash"] == ""
    # Every subsequent event's previous_hash equals the prior event's hash.
    for i in range(1, len(events)):
        assert events.iloc[i]["previous_hash"] == events.iloc[i - 1]["event_hash"]
    assert summary["audit_events"] == EXPECTED["audit_events"]


def test_hash_chain_recomputable_from_stored_rows(seeded_store):
    """Every event_hash can be recomputed from the persisted columns, and the
    chain links are intact — the property Ticket 7's verifier will rely on."""
    from civicpay.audit.ledger import event_body_from_row

    pipeline = ReconciliationPipeline(store=seeded_store)
    pipeline.run(batch_id="VERIFY")
    events = seeded_store.read_table(M.AuditEvent.TABLE)
    events = events.sort_values(["timestamp", "event_id"]).reset_index(drop=True)

    prev_hash = ""
    for _, row in events.iterrows():
        body = event_body_from_row(row.to_dict())
        assert compute_event_hash(body) == row["event_hash"]
        assert row["previous_hash"] == prev_hash
        prev_hash = row["event_hash"]


def test_audit_ledger_append_persists_one_event(seeded_store):
    """append() writes a single event immediately and its hash recomputes."""
    from civicpay.audit.ledger import AuditLedger, event_body_from_row

    ledger = AuditLedger(store=seeded_store, actor="tester")
    before = seeded_store.table_count(M.AuditEvent.TABLE)
    row = ledger.append(
        event_type=M.AuditEventType.INGEST,
        entity_type="payment_file",
        entity_id="F-TEST",
        action="ingest",
        batch_id="SINGLE",
    )
    after = seeded_store.table_count(M.AuditEvent.TABLE)
    assert after == before + 1
    # The returned row's hash recomputes from its stored fields.
    assert compute_event_hash(event_body_from_row(row)) == row["event_hash"]
    assert row["previous_hash"] == ""


# --------------------------------------------------------------------------- #
# Integration: pure matcher counts
# --------------------------------------------------------------------------- #


def test_reconcile_outcome_counts(recon_inputs):
    payments, transactions = recon_inputs
    results, ledger, summary = reconcile(
        payments=payments,
        transactions=transactions,
        config=ReconConfig(),
        batch_id="TEST",
    )
    assert len(results) == len(payments)
    for key, expected in EXPECTED.items():
        if key in summary:
            assert summary[key] == expected, f"{key}: {summary[key]} != {expected}"
    # Consumed ledger count == matched_total.
    assert len(ledger.consumed) == EXPECTED["matched_total"]


def test_reconcile_match_status_distribution(recon_inputs):
    payments, transactions = recon_inputs
    results, _, _ = reconcile(
        payments=payments,
        transactions=transactions,
        config=ReconConfig(),
        batch_id="TEST",
    )
    statuses = pd.Series([r.match_status for r in results]).value_counts().to_dict()
    assert statuses[M.MatchStatus.MATCHED] == EXPECTED["matched_total"]
    assert statuses[M.MatchStatus.UNMATCHED_PAYMENT] == EXPECTED["unmatched_payment"]
    assert statuses[M.MatchStatus.EXCEPTION] == EXPECTED["exception_total"]


# --------------------------------------------------------------------------- #
# Integration: full pipeline persistence
# --------------------------------------------------------------------------- #


def test_pipeline_writes_reconciliation_results(seeded_store):
    pipeline = ReconciliationPipeline(store=seeded_store)
    summary = pipeline.run(batch_id="BATCH-001")
    assert summary["reconciliation_results_rows"] == EXPECTED["reconciliation_results_rows"]
    assert summary["unmatched_ledger"] == EXPECTED["unmatched_ledger"]
    assert summary["audit_events"] == EXPECTED["audit_events"]

    recon = seeded_store.read_table(M.ReconciliationResult.TABLE)
    # Row counts by match_status.
    counts = recon["match_status"].value_counts().to_dict()
    assert counts[M.MatchStatus.UNMATCHED_LEDGER] == EXPECTED["unmatched_ledger"]
    assert counts[M.MatchStatus.MATCHED] == EXPECTED["matched_total"]
    assert counts[M.MatchStatus.UNMATCHED_PAYMENT] == EXPECTED["unmatched_payment"]
    assert counts[M.MatchStatus.EXCEPTION] == EXPECTED["exception_total"]
    # unmatched_ledger rows carry no payment_id; payment-side rows do.
    ledger_rows = recon[recon["match_status"] == M.MatchStatus.UNMATCHED_LEDGER]
    assert ledger_rows["payment_id"].isna().all()
    assert ledger_rows["ledger_transaction_id"].notna().all()
    payment_rows = recon[recon["payment_id"].notna()]
    assert len(payment_rows) == 1000


def test_pipeline_reconciliation_rate(seeded_store):
    pipeline = ReconciliationPipeline(store=seeded_store)
    summary = pipeline.run(batch_id="RATE")
    assert summary["reconciliation_rate"] == round(EXPECTED["matched_total"] / 1000, 4)


def test_pipeline_updates_payment_status(seeded_store):
    pipeline = ReconciliationPipeline(store=seeded_store)
    pipeline.run(batch_id="STATUS")
    payments = seeded_store.read_table(M.PaymentRecord.TABLE)
    statuses = payments["status"].value_counts().to_dict()
    assert statuses[M.PaymentStatus.MATCHED] == EXPECTED["matched_total"]
    assert statuses[M.PaymentStatus.UNMATCHED] == EXPECTED["unmatched_payment"]
    assert statuses[M.PaymentStatus.EXCEPTION] == EXPECTED["exception_total"]


# --------------------------------------------------------------------------- #
# Determinism
# --------------------------------------------------------------------------- #


def test_pipeline_is_deterministic():
    """Two independent runs over freshly seeded stores produce identical results."""
    summaries = []
    recon_hashes = []
    for _ in range(2):
        store = DuckDBStore(":memory:")
        store.init_schema()
        data = generate_all(seed=42)
        store.write_many(
            {
                M.PaymentRecord.TABLE: data["payment_records"],
                M.Transaction.TABLE: data["transactions"],
            },
            mode="append",
        )
        pipeline = ReconciliationPipeline(store=store)
        summary = pipeline.run(batch_id="DET")
        recon = store.read_table(M.ReconciliationResult.TABLE).sort_values("recon_id")
        recon_hashes.append(recon.to_csv(index=False))
        summaries.append(summary)
        store.close()
    # Counts identical.
    for key in EXPECTED:
        assert summaries[0][key] == summaries[1][key], f"{key} drifted"
    # Full row content identical.
    assert recon_hashes[0] == recon_hashes[1]


# --------------------------------------------------------------------------- #
# CLI smoke test
# --------------------------------------------------------------------------- #


def test_cli_recon_run_writes_results(tmp_path):
    """`civicpay seed` then `civicpay recon run` end-to-end on a file DB."""
    db_path = tmp_path / "civicpay.duckdb"
    runner = CliRunner()
    result = runner.invoke(app, ["seed", "--db-path", str(db_path)])
    assert result.exit_code == 0, result.output
    result = runner.invoke(
        app,
        ["recon", "run", "--db-path", str(db_path), "--batch-id", "CLI-001"],
    )
    assert result.exit_code == 0, result.output
    assert "Done." in result.output
    # Verify persisted rows.
    store = DuckDBStore(str(db_path))
    count = store.table_count(M.ReconciliationResult.TABLE)
    audit = store.table_count(M.AuditEvent.TABLE)
    store.close()
    assert count == EXPECTED["reconciliation_results_rows"]
    assert audit == EXPECTED["audit_events"]


# --------------------------------------------------------------------------- #
# Re-run idempotency pre-flight guard (OPEN_QUESTIONS §C)
# --------------------------------------------------------------------------- #


def test_recon_re_run_same_batch_id_blocked(seeded_store):
    """Re-running recon with a batch_id already in the audit log must fail
    fast with BatchIdAlreadyUsedError (not a raw DuckDB PK constraint error)."""
    from civicpay.audit.evidence import BatchIdAlreadyUsedError

    pipeline = ReconciliationPipeline(store=seeded_store)
    pipeline.run(batch_id="RECON-RERUN")  # first run: succeeds

    with pytest.raises(BatchIdAlreadyUsedError) as exc:
        pipeline.run(batch_id="RECON-RERUN")  # second run: blocked
    assert "RECON-RERUN" in str(exc.value)
    assert "--batch-id" in str(exc.value)


def test_cli_recon_re_run_blocked_message(tmp_path):
    """The CLI surfaces the pre-flight failure with a clear message + exit 1."""
    db_path = tmp_path / "rerun.duckdb"
    runner = CliRunner()
    runner.invoke(app, ["seed", "--db-path", str(db_path)])
    runner.invoke(app, ["recon", "run", "--db-path", str(db_path), "--batch-id", "CLI-DUP"])
    result = runner.invoke(
        app, ["recon", "run", "--db-path", str(db_path), "--batch-id", "CLI-DUP"]
    )
    assert result.exit_code == 1
    assert "CLI-DUP" in result.output
    assert "--batch-id" in result.output
