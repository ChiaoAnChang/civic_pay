"""Tests for the audit-evidence layer (spec §17.3 Ticket 7)."""

from __future__ import annotations

import json

import pytest
from civicpay.audit.evidence import UnknownBatchIdError, export_evidence, verify_chain
from civicpay.audit.ledger import AuditLedger
from civicpay.data import models as M
from civicpay.data.synthetic import AS_OF_DATETIME
from civicpay.storage.duckdb import DuckDBStore


@pytest.fixture
def ledger_store():
    store = DuckDBStore(":memory:")
    store.init_schema()
    return store


def _seed_chain(store: DuckDBStore, batch_id: str = "AUD1", n: int = 5) -> list[dict]:
    ledger = AuditLedger(store=store, actor="tester", as_of=AS_OF_DATETIME)
    rows = []
    for i in range(n):
        row = ledger.append(
            event_type=M.AuditEventType.MATCH,
            entity_type="payment",
            entity_id=f"P{i:04d}",
            action="reconcile",
            batch_id=batch_id,
        )
        rows.append(row)
    return rows


# --------------------------------------------------------------------------- #
# verify_chain
# --------------------------------------------------------------------------- #


def test_verify_intact_chain(ledger_store):
    _seed_chain(ledger_store, batch_id="AUD1", n=5)
    report = verify_chain(ledger_store, batch_id="AUD1")
    assert report["verified"] is True
    assert report["event_count"] == 5
    assert report["broken_event"] is None


def test_verify_empty_log_is_ok(ledger_store):
    report = verify_chain(ledger_store)
    assert report["verified"] is True
    assert report["event_count"] == 0


def test_verify_full_chain_includes_all_batches(ledger_store):
    _seed_chain(ledger_store, batch_id="AUD1", n=3)
    _seed_chain(ledger_store, batch_id="AUD2", n=2)
    report = verify_chain(ledger_store)  # full chain
    assert report["verified"] is True
    assert report["event_count"] == 5


def test_verify_detects_tampered_field(ledger_store):
    _seed_chain(ledger_store, batch_id="AUD1", n=5)
    # Tamper with a hashed field on event 1 (without recomputing event_hash).
    ledger_store.conn.execute(
        f"UPDATE {M.AuditEvent.TABLE} SET action = 'TAMPERED' WHERE event_id = 'EVT-AUD1-000001'"
    )
    report = verify_chain(ledger_store, batch_id="AUD1")
    assert report["verified"] is False
    assert report["broken_event"]["event_id"] == "EVT-AUD1-000001"
    assert report["broken_event"]["reason"] == "content_hash_mismatch"


def test_verify_full_chain_rejects_deleted_first_event(ledger_store):
    """Deleting the genesis event of a full chain must fail verification."""
    _seed_chain(ledger_store, batch_id="AUD1", n=5)
    ledger_store.conn.execute(
        f"DELETE FROM {M.AuditEvent.TABLE} WHERE event_id = 'EVT-AUD1-000001'"
    )
    report = verify_chain(ledger_store, batch_id=None)
    assert report["verified"] is False
    # Deleting the genesis leaves no event with an empty previous_hash.
    assert report["broken_event"]["reason"] == "no_genesis"


def test_verify_detects_deleted_event(ledger_store):
    _seed_chain(ledger_store, batch_id="AUD1", n=5)
    # Delete a middle event -> event 4's previous_hash now points at a hash that
    # is no longer in the set, so event 4 becomes a second "genesis" candidate.
    ledger_store.conn.execute(
        f"DELETE FROM {M.AuditEvent.TABLE} WHERE event_id = 'EVT-AUD1-000003'"
    )
    report = verify_chain(ledger_store, batch_id="AUD1")
    assert report["verified"] is False
    assert report["broken_event"]["reason"] == "chain_linkage_broken"


def test_verify_batch_boundary_allows_prior_hash(ledger_store):
    """A batch's first event legitimately points at a prior batch's hash."""
    _seed_chain(ledger_store, batch_id="AUD1", n=3)
    _seed_chain(ledger_store, batch_id="AUD2", n=2)
    # Verifying only AUD2 should still pass (boundary allowed).
    report = verify_chain(ledger_store, batch_id="AUD2")
    assert report["verified"] is True
    assert report["event_count"] == 2


def test_new_ledger_resumes_from_true_tip_not_alphabetical_max(ledger_store):
    """A new ``AuditLedger``'s chain-resume must not depend on batch-id string
    ordering.

    Regression for a fork found while implementing the backlog cohort: two
    batches sharing a timestamp, seeded in real order "ZZZ" then "AAA"
    (alphabetically the wrong way round), used to make a *third*, freshly
    created ``AuditLedger`` resume from "ZZZ"'s stale tip instead of "AAA"'s
    true one (the old resume query was ``ORDER BY timestamp DESC, event_id
    DESC``, and "EVT-ZZZ-..." sorts after "EVT-AAA-..." regardless of which
    was actually appended more recently) -- forking the chain, since "AAA"'s
    own first event had already legitimately chained onto "ZZZ"'s tip.
    """
    _seed_chain(ledger_store, batch_id="ZZZ", n=5)
    _seed_chain(ledger_store, batch_id="AAA", n=3)

    third = AuditLedger(store=ledger_store, actor="tester", as_of=AS_OF_DATETIME)
    row = third.append(
        event_type=M.AuditEventType.MATCH,
        entity_type="payment",
        entity_id="P-LAST",
        action="reconcile",
        batch_id="M1",
    )
    aaa_last_hash = ledger_store.query(
        f"SELECT event_hash FROM {M.AuditEvent.TABLE} WHERE event_id = 'EVT-AAA-000003'"
    )["event_hash"].iloc[0]
    assert row["previous_hash"] == aaa_last_hash

    report = verify_chain(ledger_store)  # full chain
    assert report["verified"] is True
    assert report["event_count"] == 9
    assert report["broken_event"] is None


# --------------------------------------------------------------------------- #
# export_evidence
# --------------------------------------------------------------------------- #


def test_export_package_is_well_formed(ledger_store, tmp_path):
    _seed_chain(ledger_store, batch_id="AUD1", n=4)
    out = tmp_path / "evidence.json"
    pkg = export_evidence(ledger_store, batch_id="AUD1", out_path=out)
    assert set(pkg.keys()) == {
        "batch_id",
        "exported_at",
        "scope",
        "verification",
        "audit_events",
        "reconciliation_summary",
        "reconciliation_results",
        "exception_summary",
        "exceptions",
    }
    assert pkg["batch_id"] == "AUD1"
    assert pkg["verification"]["verified"] is True
    assert pkg["verification"]["event_count"] == 4
    assert len(pkg["audit_events"]) == 4
    assert pkg["reconciliation_summary"] == {"total": 0, "matched": 0, "exceptions": 0}
    assert pkg["exception_summary"] == {"total": 0, "open": 0, "in_progress": 0, "resolved": 0}
    assert pkg["exceptions"] == []
    assert pkg["scope"]["audit_events_rows"] == 4
    assert pkg["scope"]["reconciliation_results_included"] is False
    # Event rows carry the hash fields.
    ev = pkg["audit_events"][0]
    assert "event_hash" in ev and "previous_hash" in ev
    # File written and round-trips.
    assert out.exists()
    pkg2 = json.loads(out.read_text(encoding="utf-8"))
    assert pkg2["verification"]["event_count"] == 4


def test_export_reflects_tampering(ledger_store, tmp_path):
    _seed_chain(ledger_store, batch_id="AUD1", n=3)
    ledger_store.conn.execute(
        f"UPDATE {M.AuditEvent.TABLE} SET action = 'TAMPERED' WHERE event_id = 'EVT-AUD1-000002'"
    )
    pkg = export_evidence(ledger_store, batch_id="AUD1")
    assert pkg["verification"]["verified"] is False
    assert pkg["verification"]["broken_event"]["event_id"] == "EVT-AUD1-000002"


def test_export_includes_reconciliation_backing_rows():
    # End-to-end: a real recon batch produces reconciliation_results that the
    # evidence package surfaces as backing rows.
    from civicpay.data.synthetic import generate_all
    from civicpay.recon.pipeline import ReconciliationPipeline

    store = DuckDBStore(":memory:")
    store.init_schema()
    data = generate_all(seed=7, volumes={"customers": 50, "accounts": 30, "transactions": 300})
    store.write_many(
        {M.PaymentRecord.TABLE: data["payment_records"], M.Transaction.TABLE: data["transactions"]},
        mode="replace",
    )
    ReconciliationPipeline(store=store).run(batch_id="AUDX")

    # Default (full=False): summary is populated but full rows are omitted.
    default_pkg = export_evidence(store, batch_id="AUDX")
    assert default_pkg["reconciliation_summary"]["total"] > 0
    assert default_pkg["reconciliation_results"] == []
    assert default_pkg["scope"]["reconciliation_results_included"] is False

    # full=True: the complete backing rows are attached.
    full_pkg = export_evidence(store, batch_id="AUDX", full=True)
    assert full_pkg["verification"]["verified"] is True
    assert full_pkg["reconciliation_summary"]["total"] > 0
    assert full_pkg["reconciliation_summary"]["total"] == len(full_pkg["reconciliation_results"])
    assert full_pkg["scope"]["reconciliation_results_included"] is True
    store.close()


def test_export_includes_exception_backlog_and_summary():
    # End-to-end: a DQ batch has no reconciliation_results (that table carries
    # no DQ activity at all), but its exception_queue rows are still exported
    # and summarized -- this is the whole point of dropping the --mode idea.
    from civicpay.data.synthetic import generate_all
    from civicpay.quality.pipeline import QualityPipeline

    store = DuckDBStore(":memory:")
    store.init_schema()
    data = generate_all(seed=42, volumes={"customers": 500, "accounts": 200, "transactions": 2000})
    store.write_many(data, mode="replace")
    QualityPipeline(store=store).run(batch_id="AUDY")

    pkg = export_evidence(store, batch_id="AUDY")
    assert pkg["verification"]["verified"] is True
    assert pkg["reconciliation_summary"] == {"total": 0, "matched": 0, "exceptions": 0}
    assert pkg["exception_summary"]["total"] > 0
    assert pkg["exception_summary"]["total"] == len(pkg["exceptions"])
    assert all(e["exception_id"].startswith("EXC-AUDY-") for e in pkg["exceptions"])
    store.close()


def test_export_unknown_batch_id_raises(ledger_store):
    with pytest.raises(UnknownBatchIdError, match="No audit events found"):
        export_evidence(ledger_store, batch_id="NOPE")


# --------------------------------------------------------------------------- #
# CLI wiring: civicpay audit export
# --------------------------------------------------------------------------- #


def test_cli_audit_export_default_omits_full_rows_and_flag_includes_them(tmp_path):
    from civicpay.cli import app
    from civicpay.data.synthetic import generate_all
    from civicpay.recon.pipeline import ReconciliationPipeline
    from typer.testing import CliRunner

    db = tmp_path / "cli-export.duckdb"
    store = DuckDBStore(str(db))
    store.init_schema()
    data = generate_all(seed=7, volumes={"customers": 50, "accounts": 30, "transactions": 300})
    store.write_many(
        {M.PaymentRecord.TABLE: data["payment_records"], M.Transaction.TABLE: data["transactions"]},
        mode="replace",
    )
    ReconciliationPipeline(store=store).run(batch_id="CLIX")
    store.close()

    runner = CliRunner()
    out = tmp_path / "evidence.json"
    result = runner.invoke(
        app, ["audit", "export", "--batch", "CLIX", "--out", str(out), "--db-path", str(db)]
    )
    assert result.exit_code == 0, result.output
    assert "exception summary" in result.output
    pkg = json.loads(out.read_text(encoding="utf-8"))
    assert pkg["reconciliation_results"] == []  # default full=False
    assert pkg["reconciliation_summary"]["total"] > 0

    out_full = tmp_path / "evidence-full.json"
    result_full = runner.invoke(
        app,
        [
            "audit",
            "export",
            "--batch",
            "CLIX",
            "--out",
            str(out_full),
            "--full",
            "--db-path",
            str(db),
        ],
    )
    assert result_full.exit_code == 0, result_full.output
    pkg_full = json.loads(out_full.read_text(encoding="utf-8"))
    assert len(pkg_full["reconciliation_results"]) == pkg_full["reconciliation_summary"]["total"]


def test_cli_audit_export_unknown_batch_exits_nonzero(tmp_path):
    from civicpay.cli import app
    from typer.testing import CliRunner

    db = tmp_path / "cli-empty.duckdb"
    store = DuckDBStore(str(db))
    store.init_schema()
    store.close()

    runner = CliRunner()
    result = runner.invoke(app, ["audit", "export", "--batch", "NOPE", "--db-path", str(db)])
    assert result.exit_code == 1
    assert "No audit events found" in result.output


# --------------------------------------------------------------------------- #
# previous_hash UNIQUE constraint + retry (see docs/cloud-backend.md)
# --------------------------------------------------------------------------- #


def test_previous_hash_unique_constraint_enforced(ledger_store):
    """The schema itself, not just application logic, rejects two events
    claiming the same previous_hash — the precondition the retry logic
    below depends on."""
    import duckdb

    _seed_chain(ledger_store, batch_id="AUD1", n=1)
    with pytest.raises(duckdb.ConstraintException):
        ledger_store.conn.execute(
            f"INSERT INTO {M.AuditEvent.TABLE} VALUES "
            f"('EVT-FAKE-000001', TIMESTAMP '2026-09-01', 'ingest', 'x', 'x', 'x', 'x', '', 'fakehash')"
        )


def test_append_retries_on_stale_previous_hash(ledger_store):
    """Simulates two AuditLedger instances racing on the same chain tip —
    the shape of the race docs/cloud-backend.md describes (currently
    unreachable in practice under DuckDB's own single-writer file lock, but
    reachable in this test by directly desynchronizing a second instance's
    cached tip from the true one, the same way test_audit.py's tamper tests
    reach through store.conn to simulate conditions no normal code path
    produces).

    Without the retry, this second append would either violate the new
    UNIQUE constraint and crash, or (pre-constraint) silently fork the chain
    the same way an earlier backlog-cohort bug once did. With the retry, it
    must transparently re-resolve the true tip and produce a validly-chained
    second event.
    """
    first_ledger = AuditLedger(store=ledger_store, actor="tester", as_of=AS_OF_DATETIME)
    first = first_ledger.append(
        event_type=M.AuditEventType.MATCH,
        entity_type="payment",
        entity_id="P0001",
        action="reconcile",
        batch_id="AUD1",
    )

    stale_ledger = AuditLedger(store=ledger_store, actor="tester", as_of=AS_OF_DATETIME)
    # Force this instance to believe the chain is still empty — the state a
    # second writer would be in had it read the tip before `first` was
    # appended, then attempted its own append after.
    stale_ledger._previous_hash = ""
    stale_ledger._chain_initialized = True
    stale_ledger._batch_id = "AUD2"

    second = stale_ledger.append(
        event_type=M.AuditEventType.MATCH,
        entity_type="payment",
        entity_id="P0002",
        action="reconcile",
        batch_id="AUD2",
    )

    # The retry must have kicked in: "" is already claimed by `first`'s own
    # previous_hash, so a naive write would have collided. The corrected
    # write chains off the true tip instead.
    assert second["previous_hash"] == first["event_hash"]

    report = verify_chain(ledger_store)
    assert report["verified"] is True
    assert report["event_count"] == 2


def test_append_many_retries_on_stale_previous_hash(ledger_store):
    """Same race, through append_many()'s batch path — the whole batch must
    be rebuilt off the corrected tip, not just its first row."""
    first_ledger = AuditLedger(store=ledger_store, actor="tester", as_of=AS_OF_DATETIME)
    first = first_ledger.append(
        event_type=M.AuditEventType.MATCH,
        entity_type="payment",
        entity_id="P0001",
        action="reconcile",
        batch_id="AUD1",
    )

    stale_ledger = AuditLedger(store=ledger_store, actor="tester", as_of=AS_OF_DATETIME)
    stale_ledger._previous_hash = ""
    stale_ledger._chain_initialized = True
    stale_ledger._batch_id = "AUD2"

    rows = stale_ledger.append_many(
        [
            {
                "event_type": M.AuditEventType.MATCH,
                "entity_type": "payment",
                "entity_id": f"P{i:04d}",
                "action": "reconcile",
                "batch_id": "AUD2",
            }
            for i in range(2, 4)
        ]
    )

    assert len(rows) == 2
    assert rows.iloc[0]["previous_hash"] == first["event_hash"]
    assert rows.iloc[1]["previous_hash"] == rows.iloc[0]["event_hash"]

    report = verify_chain(ledger_store)
    assert report["verified"] is True
    assert report["event_count"] == 3


def test_append_gives_up_after_max_attempts(ledger_store, monkeypatch):
    """A persistent (not just one-shot) conflict must fail loudly with a
    clear error, not loop forever or silently give up."""
    ledger = AuditLedger(store=ledger_store, actor="tester", as_of=AS_OF_DATETIME)
    ledger.append(
        event_type=M.AuditEventType.MATCH,
        entity_type="payment",
        entity_id="P0001",
        action="reconcile",
        batch_id="AUD1",
    )

    # Force every re-resolution to land back on the same already-taken
    # previous_hash ("" — already claimed by the first event above), instead
    # of the real _initialize_chain's behavior of converging on the true,
    # unclaimed tip. This simulates a *persistent* conflict (every attempt
    # collides, not just the first).
    def _stuck_at_genesis() -> None:
        ledger._previous_hash = ""
        ledger._chain_initialized = True

    monkeypatch.setattr(ledger, "_initialize_chain", _stuck_at_genesis)
    ledger._previous_hash = ""

    with pytest.raises(RuntimeError, match="Failed to append"):
        ledger.append(
            event_type=M.AuditEventType.MATCH,
            entity_type="payment",
            entity_id="P0002",
            action="reconcile",
            batch_id="AUD1",
        )
