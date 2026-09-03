"""Tests for the exception workflow (spec §17.3 Ticket 6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest
from civicpay.data import models as M
from civicpay.data.synthetic import AS_OF_DATETIME, generate_all
from civicpay.exceptions.queue import ExceptionManager
from civicpay.exceptions.workflow import (
    DEFAULT_SLA_DAYS,
    SLA_DAYS_BY_SEVERITY,
    age_factor,
    amount_at_risk_factor,
    compute_priority_score,
    resolve_sla_days,
    severity_weight,
)
from civicpay.storage.duckdb import DuckDBStore

UTC = UTC


# --------------------------------------------------------------------------- #
# Priority formula
# --------------------------------------------------------------------------- #


def test_severity_weights():
    assert severity_weight("high") == 3.0
    assert severity_weight("medium") == 2.0
    assert severity_weight("low") == 1.0
    assert severity_weight("unknown") == 1.0


@pytest.mark.parametrize(
    "amount,factor",
    [
        (0, 1.0),
        (50, 1.0),
        (99.99, 1.0),
        (100, 2.0),
        (999, 2.0),
        (1000, 3.0),
        (9999.99, 3.0),
        (10000, 4.0),
        (50000, 4.0),
    ],
)
def test_amount_at_risk_factor_buckets(amount, factor):
    assert amount_at_risk_factor(amount) == factor


def test_amount_at_risk_factor_none_is_neutral_midpoint():
    """None ("not applicable") gets a distinct neutral factor -- not the
    lowest bucket (0 would be a real, if trivial, resolved amount)."""
    from civicpay.exceptions.workflow import NEUTRAL_AMOUNT_AT_RISK_FACTOR

    assert amount_at_risk_factor(None) == NEUTRAL_AMOUNT_AT_RISK_FACTOR
    assert 1.0 < NEUTRAL_AMOUNT_AT_RISK_FACTOR < 4.0


def test_age_factor_within_sla():
    assert age_factor(0) == 1.0
    assert age_factor(DEFAULT_SLA_DAYS) == 1.0  # boundary: exactly SLA


def test_age_factor_escalates_past_sla():
    # one day overdue -> +0.5
    assert age_factor(DEFAULT_SLA_DAYS + 1) == 1.5
    # three days overdue -> +1.5
    assert age_factor(DEFAULT_SLA_DAYS + 3) == 2.5


def test_age_factor_respects_custom_sla():
    assert age_factor(5, sla_days=5) == 1.0
    assert age_factor(6, sla_days=5) == 1.5


def test_compute_priority_score_combines_factors():
    # high severity, $5000 amount, 10 days old, sla 7 -> 3 * 3 * 2.5
    assert compute_priority_score("high", 5000, 10, sla_days=7) == 22.5
    # low severity, $50 amount, fresh -> 1 * 1 * 1
    assert compute_priority_score("low", 50, 0) == 1.0


def test_resolve_sla_days_per_severity_by_default():
    assert resolve_sla_days("high", None) == SLA_DAYS_BY_SEVERITY["high"] == 3
    assert resolve_sla_days("medium", None) == SLA_DAYS_BY_SEVERITY["medium"] == 7
    assert resolve_sla_days("low", None) == SLA_DAYS_BY_SEVERITY["low"] == 14
    assert resolve_sla_days("unknown", None) == DEFAULT_SLA_DAYS


def test_resolve_sla_days_explicit_override_wins():
    assert resolve_sla_days("high", 30) == 30
    assert resolve_sla_days("low", 1) == 1


def test_compute_priority_score_defaults_to_per_severity_sla():
    # high severity escalates from day 3, not the old global default of 7.
    assert compute_priority_score("high", 0, 10) == severity_weight("high") * 1.0 * age_factor(
        10, 3
    )
    # low severity has more headroom (14 days) before escalating.
    assert compute_priority_score("low", 0, 10) == severity_weight("low") * 1.0 * age_factor(10, 14)


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture
def seeded_store():
    store = DuckDBStore(":memory:")
    store.init_schema()
    data = generate_all(seed=42, volumes={"customers": 100, "accounts": 50, "transactions": 500})
    store.write_many(data, mode="replace")
    return store


def _exc_row(
    eid: str,
    ref: str = "transactions:TXN-000001",
    priority: str = "low",
    status: str = M.ExceptionStatus.OPEN,
    created_at: datetime | None = None,
) -> dict:
    return {
        "exception_id": eid,
        "source": "dq",
        "reference_id": ref,
        "priority": priority,
        "assigned_to": None,
        "status": status,
        "created_at": created_at or AS_OF_DATETIME,
        "resolved_at": None,
        "resolution_notes": None,
        "root_cause": None,
    }


def _write_exc(store: DuckDBStore, rows: list[dict]) -> None:
    df = pd.DataFrame(rows)
    store.write_dataframe(M.ExceptionItem.TABLE, df, mode="append")


# --------------------------------------------------------------------------- #
# Manager.list
# --------------------------------------------------------------------------- #


def test_list_sorts_by_priority_score_desc(seeded_store):
    as_of = AS_OF_DATETIME
    _write_exc(
        seeded_store,
        [
            # accounts has no amount concept -> amount_basis="n/a", neutral
            # factor 2.5 (not the lowest bucket 1.0 a resolved $0 would get).
            _exc_row("E1", ref="accounts:ACC-1", priority="low", created_at=as_of),  # 1*2.5*1 = 2.5
            _exc_row(
                "E2", ref="accounts:ACC-2", priority="high", created_at=as_of - timedelta(days=10)
            ),  # high SLA=3 -> age_factor(10,3)=4.5 -> 3*2.5*4.5 = 33.75
            _exc_row(
                "E3", ref="accounts:ACC-3", priority="medium", created_at=as_of - timedelta(days=5)
            ),  # medium SLA=7 -> age_factor(5,7)=1.0 -> 2*2.5*1 = 5.0
        ],
    )
    items = ExceptionManager(store=seeded_store, as_of=as_of).list()
    ids = [i["exception_id"] for i in items]
    assert ids == ["E2", "E3", "E1"]
    assert items[0]["priority_score"] == 33.75
    assert items[0]["sla_days"] == 3
    assert items[0]["amount_basis"] == "n/a"


def test_list_sla_days_override_applies_to_every_item(seeded_store):
    as_of = AS_OF_DATETIME
    _write_exc(
        seeded_store,
        [
            _exc_row("E1", ref="accounts:ACC-1", priority="high", created_at=as_of),
            _exc_row("E2", ref="accounts:ACC-2", priority="low", created_at=as_of),
        ],
    )
    items = ExceptionManager(store=seeded_store, as_of=as_of).list(sla_days=30)
    assert {i["exception_id"]: i["sla_days"] for i in items} == {"E1": 30, "E2": 30}


def test_list_filters_by_status(seeded_store):
    as_of = AS_OF_DATETIME
    _write_exc(
        seeded_store,
        [
            _exc_row("E1", status=M.ExceptionStatus.OPEN),
            _exc_row("E2", status=M.ExceptionStatus.RESOLVED),
        ],
    )
    open_items = ExceptionManager(store=seeded_store, as_of=as_of).list(
        status=M.ExceptionStatus.OPEN
    )
    assert [i["exception_id"] for i in open_items] == ["E1"]


def test_list_resolves_amount_at_risk_from_transaction(seeded_store):
    # Pick a real transaction id and read its amount.
    txns = seeded_store.read_table(M.Transaction.TABLE)
    sample = txns.iloc[0]
    ref = f"transactions:{sample['transaction_id']}"
    expected_amount = float(sample["amount"])
    _write_exc(seeded_store, [_exc_row("E1", ref=ref, priority="medium")])
    items = ExceptionManager(store=seeded_store, as_of=AS_OF_DATETIME).list()
    assert items[0]["amount_at_risk"] == pytest.approx(expected_amount, rel=1e-6)
    assert items[0]["amount_basis"] == "amount"
    # factor bucket for the actual amount
    assert items[0]["amount_at_risk_factor"] == amount_at_risk_factor(expected_amount)


def test_list_amount_not_applicable_for_non_amount_dataset(seeded_store):
    """accounts/customers exceptions have no dollar-amount concept -- this is
    ``None`` ("not applicable"), not a resolved ``0.0``, and gets the neutral
    midpoint factor rather than the lowest bucket (OPEN_QUESTIONS, exception
    workflow: distinguishing "N/A" from "genuinely $0" so a high-severity
    amount-less exception isn't structurally capped below every
    dollar-bearing one)."""
    _write_exc(seeded_store, [_exc_row("E1", ref="accounts:ACC-000001", priority="high")])
    items = ExceptionManager(store=seeded_store, as_of=AS_OF_DATETIME).list()
    assert items[0]["amount_at_risk"] is None
    assert items[0]["amount_basis"] == "n/a"
    assert items[0]["amount_at_risk_factor"] == amount_at_risk_factor(None)


def test_list_amount_not_applicable_when_reference_missing(seeded_store):
    _write_exc(seeded_store, [_exc_row("E1", ref="transactions:DOES-NOT-EXIST", priority="high")])
    items = ExceptionManager(store=seeded_store, as_of=AS_OF_DATETIME).list()
    assert items[0]["amount_at_risk"] is None
    assert items[0]["amount_basis"] == "n/a"


def test_list_amount_nan_treated_as_not_applicable_not_highest_bucket(seeded_store):
    """A nullable amount column (transactions.amount / payment_records.amount
    have no NOT NULL constraint) reads a SQL NULL back as NaN, not None --
    and float(nan) does not raise. Without a guard, amount_at_risk_factor's
    bucket comparisons are all False against NaN and fall through to the
    *highest* bucket (4.0), the opposite of the intended neutral (2.5)
    treatment for an unresolvable amount. Regression for a real bug found in
    code review."""
    import math

    txns = seeded_store.read_table(M.Transaction.TABLE)
    row = txns.iloc[0].to_dict()
    row["transaction_id"] = "TXN-NAN-TEST"
    row["amount"] = float("nan")
    seeded_store.write_dataframe(M.Transaction.TABLE, pd.DataFrame([row]), mode="append")

    _write_exc(seeded_store, [_exc_row("E1", ref="transactions:TXN-NAN-TEST", priority="high")])
    items = ExceptionManager(store=seeded_store, as_of=AS_OF_DATETIME).list()
    assert items[0]["amount_at_risk"] is None
    assert items[0]["amount_basis"] == "n/a"
    assert not math.isnan(items[0]["amount_at_risk_factor"])
    assert items[0]["amount_at_risk_factor"] == amount_at_risk_factor(None)


def test_list_age_days_uses_created_at(seeded_store):
    as_of = AS_OF_DATETIME
    _write_exc(seeded_store, [_exc_row("E1", created_at=as_of - timedelta(days=12))])
    items = ExceptionManager(store=seeded_store, as_of=as_of).list()
    assert items[0]["age_days"] == 12


# --------------------------------------------------------------------------- #
# Manager.resolve
# --------------------------------------------------------------------------- #


def test_resolve_marks_resolved_and_captures_root_cause(seeded_store):
    _write_exc(seeded_store, [_exc_row("E1")])
    result = ExceptionManager(store=seeded_store, as_of=AS_OF_DATETIME).resolve(
        "E1", root_cause="stale upstream feed"
    )
    assert result["status"] == M.ExceptionStatus.RESOLVED
    row = seeded_store.read_table(M.ExceptionItem.TABLE)
    r = row[row["exception_id"] == "E1"].iloc[0]
    assert r["status"] == M.ExceptionStatus.RESOLVED
    assert r["root_cause"] == "stale upstream feed"
    # DuckDB stores timestamps tz-naive; compare against the naive value.
    assert pd.Timestamp(r["resolved_at"]) == pd.Timestamp(AS_OF_DATETIME.replace(tzinfo=None))


def test_resolve_emits_unique_audit_event(seeded_store):
    _write_exc(seeded_store, [_exc_row("E1"), _exc_row("E2")])
    mgr = ExceptionManager(store=seeded_store, as_of=AS_OF_DATETIME)
    mgr.resolve("E1", root_cause="a")
    mgr.resolve("E2", root_cause="b")
    events = seeded_store.query(
        f"SELECT * FROM {M.AuditEvent.TABLE} WHERE event_type = ? ORDER BY event_id",
        [M.AuditEventType.EXCEPTION_RESOLVE],
    )
    assert len(events) == 2
    # unique event ids
    assert events["event_id"].iloc[0] != events["event_id"].iloc[1]
    assert "E1" in events["event_id"].iloc[0]
    assert "E2" in events["event_id"].iloc[1]


def test_resolve_already_resolved_raises(seeded_store):
    _write_exc(seeded_store, [_exc_row("E1", status=M.ExceptionStatus.RESOLVED)])
    with pytest.raises(ValueError, match="already resolved"):
        ExceptionManager(store=seeded_store, as_of=AS_OF_DATETIME).resolve("E1", root_cause="x")


def test_resolve_missing_raises(seeded_store):
    with pytest.raises(ValueError, match="not found"):
        ExceptionManager(store=seeded_store, as_of=AS_OF_DATETIME).resolve("NOPE", root_cause="x")


def test_assign_moves_to_in_progress(seeded_store):
    _write_exc(seeded_store, [_exc_row("E1")])
    ExceptionManager(store=seeded_store, as_of=AS_OF_DATETIME).assign("E1", owner="analyst-1")
    row = seeded_store.read_table(M.ExceptionItem.TABLE)
    r = row[row["exception_id"] == "E1"].iloc[0]
    assert r["status"] == M.ExceptionStatus.IN_PROGRESS
    assert r["assigned_to"] == "analyst-1"
