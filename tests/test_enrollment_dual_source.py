"""Tests for the dual-source agreement gate (Ticket 13 §6)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from civicpay.enrollment.dual_source import (
    agree,
    compute_expected_payout_method_a,
    compute_expected_payout_method_b,
    evaluate,
)
from civicpay.enrollment.models import EnrollmentRecord


def _record(amount: str, term: int) -> EnrollmentRecord:
    return EnrollmentRecord(
        enrollment_id="ENR-000001",
        entity_id="ENT-00001",
        program_code="GROWTH",
        enrollment_date=datetime(2025, 1, 1, tzinfo=UTC),
        incentive_amount=Decimal(amount),
        term_months=term,
        region="WEST",
        submitted_by="operator-01",
    )


def test_method_a_prorates_and_rounds_per_period():
    record = _record("1200.00", 12)
    assert compute_expected_payout_method_a(record) == Decimal("1200.00")


def test_methods_agree_when_amount_divides_evenly(in_memory_store):
    record = _record("1200.00", 12)
    a = compute_expected_payout_method_a(record)
    b = compute_expected_payout_method_b(in_memory_store, record)
    assert a == b == Decimal("1200.00")
    assert agree(a, b)


def test_methods_diverge_on_uneven_division(in_memory_store):
    # 1000.00 / 7 -> 142.857... -> rounds to 142.86 per period -> *7 = 1000.02
    record = _record("1000.00", 7)
    a = compute_expected_payout_method_a(record)
    b = compute_expected_payout_method_b(in_memory_store, record)
    assert a == Decimal("1000.02")
    assert b == Decimal("1000.00")
    assert not agree(a, b)


def test_agree_respects_tolerance():
    assert agree(Decimal("100.00"), Decimal("100.01"), tolerance=Decimal("0.01"))
    assert not agree(Decimal("100.00"), Decimal("100.02"), tolerance=Decimal("0.01"))


def test_evaluate_bundles_both_amounts_and_delta(in_memory_store):
    record = _record("1000.00", 7)
    result = evaluate(in_memory_store, record)
    assert result.method_a_amount == Decimal("1000.02")
    assert result.method_b_amount == Decimal("1000.00")
    assert result.delta == Decimal("0.02")
    assert result.agreed is False


def test_evaluate_agrees_on_clean_division(in_memory_store):
    record = _record("2400.00", 24)
    result = evaluate(in_memory_store, record)
    assert result.agreed is True
    assert result.delta == Decimal("0.00")
