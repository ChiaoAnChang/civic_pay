"""Tests for the audit-evidence layer (spec §17.3 Ticket 7)."""

from __future__ import annotations

import json

import pytest
from civicpay.audit.evidence import export_evidence, verify_chain
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
        "verification",
        "audit_events",
        "reconciliation_summary",
        "reconciliation_results",
    }
    assert pkg["batch_id"] == "AUD1"
    assert pkg["verification"]["verified"] is True
    assert pkg["verification"]["event_count"] == 4
    assert len(pkg["audit_events"]) == 4
    assert pkg["reconciliation_summary"] == {"total": 0, "matched": 0, "exceptions": 0}
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
    pkg = export_evidence(store, batch_id="AUDX")
    assert pkg["verification"]["verified"] is True
    assert pkg["reconciliation_summary"]["total"] > 0
    assert pkg["reconciliation_summary"]["total"] == len(pkg["reconciliation_results"])
    store.close()
