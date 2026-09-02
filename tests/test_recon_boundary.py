"""Boundary-condition tests for the reconciliation matcher and audit ledger.

These tests exercise edge cases not covered by the happy-path / outcome-count
suite: tolerance and window boundaries (inclusive edges), the fuzzy-threshold
boundary, the stale boundary, currency mismatches, multi-candidate references,
empty datasets, zero tolerance, audit-ledger chain resumption, and tamper
detection (recompute mismatch).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pandas as pd
import pytest
from civicpay.audit.ledger import (
    AuditLedger,
    canonical_json,
    compute_event_hash,
    event_body_from_row,
)
from civicpay.data import models as M
from civicpay.recon.matcher import (
    ReconConfig,
    normalize_reference,
    reconcile,
)
from civicpay.recon.pipeline import ReconciliationPipeline
from civicpay.storage.duckdb import DuckDBStore

AS_OF = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Minimal-frame builders (only the columns the matcher reads)
# --------------------------------------------------------------------------- #


def _pay(pid, ref, amount, cur, d, status="ingested"):
    return pd.DataFrame(
        [
            {
                "payment_id": pid,
                "file_id": "F1",
                "reference_id": ref,
                "amount": amount,
                "currency": cur,
                "direction": "in",
                "counterparty": "Acme",
                "payment_date": d,
                "expected_posting_date": d,
                "status": status,
            }
        ]
    )


def _txn(tid, ref, amount, cur, d):
    return pd.DataFrame(
        [
            {
                "transaction_id": tid,
                "account_id": "A1",
                "transaction_type": "debit",
                "amount": amount,
                "currency": cur,
                "posting_date": d,
                "value_date": d,
                "description": "n",
                "reference_id": ref,
                "created_at": d,
                "status": "posted",
            }
        ]
    )


def _recon(payments, txns, config=ReconConfig()):
    return reconcile(payments, txns, config, "B", AS_OF)


# --------------------------------------------------------------------------- #
# Reference normalization boundaries
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, ""),
        ("REFM0001", "REFM0001"),  # already normalized
        ("---", ""),  # separators only
        (" _ - ", ""),  # separators + whitespace only
        ("ref\tm\n0001", "REFM0001"),  # tabs/newlines as separators
        ("  REF-M-0001  ", "REFM0001"),  # surrounding whitespace
    ],
)
def test_normalize_reference_boundaries(raw, expected):
    assert normalize_reference(raw) == expected


# --------------------------------------------------------------------------- #
# Config helper boundaries (inclusive edges)
# --------------------------------------------------------------------------- #


def test_within_amount_inclusive_at_tolerance():
    cfg = ReconConfig(amount_tolerance=1.0)
    assert cfg.within_amount(100.0, 101.0)  # diff == tol -> within
    assert cfg.within_amount(100.0, 99.0)
    assert not cfg.within_amount(100.0, 101.01)  # just over
    assert not cfg.within_amount(100.0, 98.99)


def test_within_date_window_inclusive_at_window():
    cfg = ReconConfig(date_window_days=1)
    from datetime import date

    base = date(2026, 9, 1)
    assert cfg.within_date_window(base, base)  # same day
    assert cfg.within_date_window(base, date(2026, 8, 31))  # 1 day before == window
    assert cfg.within_date_window(base, date(2026, 9, 2))  # 1 day after == window
    assert not cfg.within_date_window(base, date(2026, 9, 3))  # 2 days > window


def test_is_stale_strictly_greater_than_stale_days():
    from datetime import date

    cfg = ReconConfig(stale_days=30)
    as_of = date(2026, 9, 1)
    # age == stale_days (Aug 2 -> Sep 1 = 30 days) -> NOT stale (boundary is >)
    assert not cfg.is_stale(date(2026, 8, 2), as_of)
    # age == stale_days + 1 -> stale
    assert cfg.is_stale(date(2026, 8, 1), as_of)


# --------------------------------------------------------------------------- #
# Matcher: amount tolerance boundary
# --------------------------------------------------------------------------- #


def test_amount_diff_exactly_at_tolerance_matches():
    d = pd.Timestamp("2026-08-15").date()
    pays = _pay("P1", "REF-001", 100.00, "USD", d)
    txns = _txn("T1", "REF-001", 101.00, "USD", d)  # diff == tol (1.0)
    results, _, _ = _recon(pays, txns)
    assert results[0].match_status == M.MatchStatus.MATCHED
    assert results[0].match_method == M.MatchMethod.EXACT


def test_amount_diff_just_over_tolerance_is_mismatch():
    d = pd.Timestamp("2026-08-15").date()
    pays = _pay("P1", "REF-001", 100.00, "USD", d)
    txns = _txn("T1", "REF-001", 101.01, "USD", d)  # diff 1.01 > tol
    results, _, _ = _recon(pays, txns)
    assert results[0].match_status == M.MatchStatus.EXCEPTION
    assert results[0].exception_reason == M.ExceptionReason.AMOUNT_MISMATCH


def test_zero_tolerance_requires_exact_amount():
    d = pd.Timestamp("2026-08-15").date()
    cfg = ReconConfig(amount_tolerance=0.0)
    pays = _pay("P1", "REF-001", 100.00, "USD", d)
    txns = _txn("T1", "REF-001", 100.01, "USD", d)  # 0.01 off
    results, _, _ = _recon(pays, txns, cfg)
    assert results[0].match_status == M.MatchStatus.EXCEPTION
    assert results[0].exception_reason == M.ExceptionReason.AMOUNT_MISMATCH


# --------------------------------------------------------------------------- #
# Matcher: date window boundary
# --------------------------------------------------------------------------- #


def test_date_diff_at_window_matches():
    base = pd.Timestamp("2026-08-15").date()
    next_day = pd.Timestamp("2026-08-16").date()
    pays = _pay("P1", "REF-001", 100.00, "USD", next_day)
    txns = _txn("T1", "REF-001", 100.00, "USD", base)  # 1 day == window
    results, _, _ = _recon(pays, txns)
    assert results[0].match_status == M.MatchStatus.MATCHED
    assert results[0].match_method == M.MatchMethod.EXACT


def test_date_diff_beyond_window_falls_through_to_unmatched():
    base = pd.Timestamp("2026-08-15").date()
    two_days = pd.Timestamp("2026-08-17").date()
    pays = _pay("P1", "REF-001", 100.00, "USD", two_days)
    txns = _txn("T1", "REF-001", 100.00, "USD", base)  # 2 days > window
    results, _, _ = _recon(pays, txns)
    # Amount is within tolerance but date is off -> NOT amount_mismatch; fuzzy
    # pre-filter excludes it (date outside window) -> unmatched_payment.
    assert results[0].match_status == M.MatchStatus.UNMATCHED_PAYMENT
    assert results[0].exception_reason is None


# --------------------------------------------------------------------------- #
# Matcher: fuzzy threshold boundary
# --------------------------------------------------------------------------- #


def test_fuzzy_at_threshold_matches_and_above_does_not():
    d = pd.Timestamp("2026-08-15").date()
    # "REF1234" (len 7) vs "REF123X" (1 substitution) -> ratio 6/7 = 0.8571.
    pays = _pay("P1", "REF123X", 100.00, "USD", d)
    txns = _txn("T1", "REF1234", 100.00, "USD", d)

    at_default, _, _ = _recon(pays, txns, ReconConfig(fuzzy_threshold=0.85))
    assert at_default[0].match_status == M.MatchStatus.MATCHED
    assert at_default[0].match_method == M.MatchMethod.FUZZY
    assert at_default[0].match_confidence == pytest.approx(0.8571, abs=0.001)

    above, _, _ = _recon(pays, txns, ReconConfig(fuzzy_threshold=0.86))
    assert above[0].match_status == M.MatchStatus.UNMATCHED_PAYMENT


def test_fuzzy_requires_amount_and_date_within_tolerance():
    d = pd.Timestamp("2026-08-15").date()
    next_day = pd.Timestamp("2026-08-16").date()
    pays = _pay("P1", "REF123X", 100.00, "USD", next_day)
    txns = _txn("T1", "REF1234", 100.00, "USD", d)
    # Date 1 day == window -> still within fuzzy pre-filter -> matches.
    r1, _, _ = _recon(pays, txns)
    assert r1[0].match_status == M.MatchStatus.MATCHED

    two_days = pd.Timestamp("2026-08-17").date()
    pays2 = _pay("P1", "REF123X", 100.00, "USD", two_days)
    r2, _, _ = _recon(pays2, txns)  # date 2 > window -> fuzzy pre-filter rejects
    assert r2[0].match_status == M.MatchStatus.UNMATCHED_PAYMENT


# --------------------------------------------------------------------------- #
# Matcher: stale boundary
# --------------------------------------------------------------------------- #


def test_stale_boundary_at_exactly_stale_days_is_not_stale():
    ledger = _txn("T1", "REF-LEDG", 100.00, "USD", pd.Timestamp("2026-08-15").date())
    pays = _pay("P1", "REF-UNQ", 100.00, "USD", pd.Timestamp("2026-08-02").date())  # 30 days
    results, _, _ = _recon(pays, ledger)
    assert results[0].match_status == M.MatchStatus.UNMATCHED_PAYMENT
    assert results[0].exception_reason is None


def test_stale_one_day_over_is_exception():
    ledger = _txn("T1", "REF-LEDG", 100.00, "USD", pd.Timestamp("2026-08-15").date())
    pays = _pay("P1", "REF-UNQ", 100.00, "USD", pd.Timestamp("2026-08-01").date())  # 31 days
    results, _, _ = _recon(pays, ledger)
    assert results[0].match_status == M.MatchStatus.EXCEPTION
    assert results[0].exception_reason == M.ExceptionReason.STALE


# --------------------------------------------------------------------------- #
# Matcher: currency mismatch
# --------------------------------------------------------------------------- #


def test_currency_mismatch_with_matching_ref_is_flagged():
    d = pd.Timestamp("2026-08-15").date()
    pays = _pay("P1", "REF-001", 100.00, "USD", d)
    txns = _txn("T1", "REF-001", 100.00, "EUR", d)  # same ref/amount/date, diff currency
    results, _, _ = _recon(pays, txns)
    # The reference and amount agree, but the currency differs so no exact match
    # is possible; the candidate's amount is within tolerance, so this is not a
    # clean amount_mismatch either — it falls through and is reported as an
    # amount_mismatch exception (the closest available reason).
    assert results[0].match_status == M.MatchStatus.EXCEPTION
    assert results[0].exception_reason == M.ExceptionReason.AMOUNT_MISMATCH


# --------------------------------------------------------------------------- #
# Matcher: multi-candidate same reference
# --------------------------------------------------------------------------- #


def test_multiple_ledger_entries_same_reference_all_match():
    d = pd.Timestamp("2026-08-15").date()
    # Two ledger entries share reference REF-001 (different amounts).
    txns = pd.concat(
        [_txn("T1", "REF-001", 100.00, "USD", d), _txn("T2", "REF-001", 200.00, "USD", d)],
        ignore_index=True,
    )
    pays = pd.concat(
        [_pay("P1", "REF-001", 100.00, "USD", d), _pay("P2", "REF-001", 200.00, "USD", d)],
        ignore_index=True,
    )
    results, ledger, _ = _recon(pays, txns)
    statuses = [r.match_status for r in results]
    assert statuses == [M.MatchStatus.MATCHED, M.MatchStatus.MATCHED]
    assert results[0].ledger_transaction_id == "T1"
    assert results[1].ledger_transaction_id == "T2"
    assert len(ledger.consumed) == 2


def test_duplicate_after_exact_consumption_is_flagged_duplicate():
    d = pd.Timestamp("2026-08-15").date()
    txns = _txn("T1", "REF-001", 100.00, "USD", d)
    # Two identical payments referencing the same single ledger entry.
    pays = pd.concat(
        [_pay("P1", "REF-001", 100.00, "USD", d), _pay("P2", "REF-001", 100.00, "USD", d)],
        ignore_index=True,
    )
    results, ledger, _ = _recon(pays, txns)
    assert results[0].match_status == M.MatchStatus.MATCHED
    assert results[1].match_status == M.MatchStatus.EXCEPTION
    assert results[1].exception_reason == M.ExceptionReason.DUPLICATE
    assert len(ledger.consumed) == 1  # only one ledger entry, consumed once


# --------------------------------------------------------------------------- #
# Matcher: empty / minimal datasets
# --------------------------------------------------------------------------- #


def test_empty_payments_raises_in_pipeline():
    store = DuckDBStore(":memory:")
    store.init_schema()
    # No payment records loaded.
    pipeline = ReconciliationPipeline(store=store)
    with pytest.raises(ValueError, match="No payment records"):
        pipeline.run(batch_id="EMPTY")
    store.close()


def test_no_transactions_all_payments_unmatched():
    d = pd.Timestamp("2026-08-15").date()
    pays = pd.concat(
        [_pay("P1", "REF-A", 100.0, "USD", d), _pay("P2", "REF-B", 200.0, "USD", d)],
        ignore_index=True,
    )
    txns = pd.DataFrame(columns=["transaction_id", "reference_id", "amount", "currency", "posting_date"])
    results, ledger, summary = _recon(pays, txns)
    assert len(results) == 2
    assert all(r.match_status == M.MatchStatus.UNMATCHED_PAYMENT for r in results)
    assert summary["unmatched_ledger"] == 0  # no ledger to be unmatched
    assert len(ledger.consumed) == 0


def test_single_exact_match():
    d = pd.Timestamp("2026-08-15").date()
    pays = _pay("P1", "REF-001", 100.00, "USD", d)
    txns = _txn("T1", "REF-001", 100.00, "USD", d)
    results, ledger, summary = _recon(pays, txns)
    assert len(results) == 1
    assert results[0].match_status == M.MatchStatus.MATCHED
    assert results[0].match_method == M.MatchMethod.EXACT
    assert summary["matched_exact"] == 1
    assert summary["unmatched_ledger"] == 0


# --------------------------------------------------------------------------- #
# Audit ledger: chain resumption & boundaries
# --------------------------------------------------------------------------- #


@pytest.fixture()
def fresh_store():
    store = DuckDBStore(":memory:")
    store.init_schema()
    yield store
    store.close()


def test_append_many_empty_is_noop(fresh_store):
    ledger = AuditLedger(store=fresh_store, actor="t")
    df = ledger.append_many([])
    assert df.empty
    assert fresh_store.table_count(M.AuditEvent.TABLE) == 0


def test_chain_resumes_across_append_calls(fresh_store):
    ledger = AuditLedger(store=fresh_store, actor="t")
    first = ledger.append(
        event_type=M.AuditEventType.INGEST,
        entity_type="payment_file",
        entity_id="F1",
        action="ingest",
        batch_id="B",
    )
    second = ledger.append(
        event_type=M.AuditEventType.MATCH,
        entity_type="reconciliation",
        entity_id="R1",
        action="match",
        batch_id="B",
    )
    assert second["previous_hash"] == first["event_hash"]
    assert first["previous_hash"] == ""
    # Recompute both hashes from persisted rows.
    events = fresh_store.read_table(M.AuditEvent.TABLE).sort_values(["timestamp", "event_id"])
    assert compute_event_hash(event_body_from_row(events.iloc[0].to_dict())) == first["event_hash"]
    assert compute_event_hash(event_body_from_row(events.iloc[1].to_dict())) == second["event_hash"]


def test_chain_resumes_across_append_then_append_many(fresh_store):
    ledger = AuditLedger(store=fresh_store, actor="t")
    first = ledger.append(
        event_type=M.AuditEventType.INGEST,
        entity_type="payment_file",
        entity_id="F1",
        action="ingest",
        batch_id="B",
    )
    df = ledger.append_many(
        [
            {
                "event_type": M.AuditEventType.MATCH,
                "entity_type": "reconciliation",
                "entity_id": "R1",
                "action": "match",
                "batch_id": "B",
            }
        ]
    )
    assert df.iloc[0]["previous_hash"] == first["event_hash"]
    assert fresh_store.table_count(M.AuditEvent.TABLE) == 2


# --------------------------------------------------------------------------- #
# Audit ledger: canonicalization of non-trivial types
# --------------------------------------------------------------------------- #


def test_canonical_json_handles_decimal_set_nested_and_tz():
    from datetime import datetime

    payload = {
        "amount": Decimal("100.00"),
        "tags": {"b", "a"},  # set -> sorted list
        "nested": {"x": 1, "y": [3, 2, 1]},
        "ts": datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC),
    }
    s = canonical_json(payload)
    # Set serialized as a sorted list.
    assert '"a"' in s and '"b"' in s and s.index('"a"') < s.index('"b"')
    # Decimal as string, no float re-encoding.
    assert '"100.00"' in s
    # Nested dict keys sorted.
    assert s.index('"x"') < s.index('"y"')
    # UTC ISO-8601.
    assert "2026-09-01T12:00:00+00:00" in s
    # Deterministic across calls.
    assert canonical_json(payload) == s


# --------------------------------------------------------------------------- #
# Audit ledger: tamper detection (recompute mismatch)
# --------------------------------------------------------------------------- #


def test_tampered_event_hash_detected_on_recompute(fresh_store):
    """If a stored event is altered, recomputing its hash no longer matches the
    stored value — the tamper-evidence property Ticket 7's verifier relies on."""
    from civicpay.data.synthetic import generate_all

    data = generate_all(seed=42)
    fresh_store.write_many(
        {M.PaymentRecord.TABLE: data["payment_records"], M.Transaction.TABLE: data["transactions"]},
        mode="append",
    )
    ReconciliationPipeline(store=fresh_store).run(batch_id="TAMPER")

    events = fresh_store.read_table(M.AuditEvent.TABLE).sort_values(["timestamp", "event_id"])
    target_idx = events.index[5]
    original_hash = events.at[target_idx, "event_hash"]

    # Tamper: alter the action of one event (but keep its stored hash).
    fresh_store.query(
        f"UPDATE {M.AuditEvent.TABLE} SET action = 'tampered' "
        f"WHERE event_id = ?",
        [events.at[target_idx, "event_id"]],
    )

    tampered = fresh_store.read_table(M.AuditEvent.TABLE).sort_values(["timestamp", "event_id"])
    tampered_row = tampered.iloc[5].to_dict()
    recomputed = compute_event_hash(event_body_from_row(tampered_row))
    # The recomputed hash no longer matches the (unchanged) stored hash.
    assert recomputed != original_hash
    # And the stored hash is still the original (only the field changed).
    assert tampered_row["event_hash"] == original_hash
