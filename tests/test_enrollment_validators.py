"""Tests for enrollment validators (Ticket 13 §5)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from civicpay.enrollment.validators import load_rules, to_enrollment_record, validate

AS_OF = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def rules():
    return load_rules()


def _good_raw(**overrides):
    raw = {
        "enrollment_id": "ENR-000001",
        "entity_id": "ENT-00001",
        "program_code": "GROWTH",
        "enrollment_date": "2025-01-15T00:00:00",
        "incentive_amount": "1200.00",
        "term_months": "12",
        "region": "WEST",
        "submitted_by": "operator-01",
    }
    raw.update(overrides)
    return raw


def test_valid_record_has_no_errors(rules):
    result = validate(_good_raw(), rules, AS_OF)
    assert result.is_valid
    assert result.errors == []


@pytest.mark.parametrize(
    "field",
    [
        "entity_id",
        "program_code",
        "enrollment_date",
        "incentive_amount",
        "term_months",
        "region",
        "submitted_by",
    ],
)
def test_missing_required_field_blocks(rules, field):
    raw = _good_raw(**{field: ""})
    result = validate(raw, rules, AS_OF)
    assert not result.is_valid
    assert any(i.field == field for i in result.errors)


def test_unknown_program_code_blocks(rules):
    result = validate(_good_raw(program_code="NOPE"), rules, AS_OF)
    assert not result.is_valid
    assert any(i.field == "program_code" for i in result.errors)


def test_unknown_region_blocks(rules):
    result = validate(_good_raw(region="ATLANTIS"), rules, AS_OF)
    assert not result.is_valid
    assert any(i.field == "region" for i in result.errors)


def test_out_of_range_amount_blocks(rules):
    result = validate(_good_raw(incentive_amount="999999.00"), rules, AS_OF)
    assert not result.is_valid
    assert any(i.field == "incentive_amount" for i in result.errors)


def test_negative_amount_blocks(rules):
    result = validate(_good_raw(incentive_amount="-50.00"), rules, AS_OF)
    assert not result.is_valid
    assert any(i.field == "incentive_amount" for i in result.errors)


def test_stray_space_numeric_blocks(rules):
    # Survives outer .strip() but still isn't a valid number.
    result = validate(_good_raw(incentive_amount="120 0.00"), rules, AS_OF)
    assert not result.is_valid
    assert any(i.field == "incentive_amount" for i in result.errors)


def test_outer_whitespace_is_tolerated(rules):
    # Leading/trailing whitespace alone (no embedded space) is stripped, not an error.
    result = validate(_good_raw(incentive_amount="  1200.00  "), rules, AS_OF)
    assert result.is_valid


def test_term_months_out_of_program_range_blocks(rules):
    # GROWTH allows 6-36 months.
    result = validate(_good_raw(term_months="1"), rules, AS_OF)
    assert not result.is_valid
    assert any(i.field == "term_months" for i in result.errors)


def test_non_numeric_term_months_blocks(rules):
    result = validate(_good_raw(term_months="twelve"), rules, AS_OF)
    assert not result.is_valid
    assert any(i.field == "term_months" for i in result.errors)


def test_date_before_minimum_blocks(rules):
    result = validate(_good_raw(enrollment_date="2019-01-01T00:00:00"), rules, AS_OF)
    assert not result.is_valid
    assert any(i.field == "enrollment_date" for i in result.errors)


def test_future_date_blocks(rules):
    result = validate(_good_raw(enrollment_date="2027-01-01T00:00:00"), rules, AS_OF)
    assert not result.is_valid
    assert any(i.field == "enrollment_date" for i in result.errors)


def test_duplicate_entity_id_in_batch_blocks(rules):
    seen = {"ENT-00001"}
    result = validate(_good_raw(entity_id="ENT-00001"), rules, AS_OF, seen_entity_ids=seen)
    assert not result.is_valid
    assert any(i.field == "entity_id" for i in result.errors)


def test_no_duplicate_check_without_seen_set(rules):
    result = validate(_good_raw(), rules, AS_OF, seen_entity_ids=None)
    assert result.is_valid


def test_to_enrollment_record_builds_typed_record(rules):
    raw = _good_raw()
    result = validate(raw, rules, AS_OF)
    assert result.is_valid
    record = to_enrollment_record(raw)
    assert record.enrollment_id == "ENR-000001"
    assert record.incentive_amount == Decimal("1200.00")
    assert record.term_months == 12
