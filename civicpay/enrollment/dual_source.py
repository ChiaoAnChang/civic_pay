"""Dual-source agreement gate (Ticket 13 §6).

Two independent calculation paths for the expected payout, structurally
separate (no shared helper that would let one bug affect both):

* **Path A** — pure Python: prorate per period, round each period, then sum.
* **Path B** — SQL via ``DuckDBStore``: compute the total, round once.

They agree on most records and diverge exactly where per-period rounding
accumulates (e.g. an amount that doesn't divide evenly across the term) — a
realistic rounding-policy divergence, not an artificial forced bug. This is
also the honest real-world failure mode the pattern generalizes: a
per-installment ledger vs. a lump-sum accounting view can legitimately
disagree by a cent or more.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

from civicpay.enrollment.models import EnrollmentRecord
from civicpay.storage.duckdb import DuckDBStore

DEFAULT_TOLERANCE = Decimal("0.01")

_CENTS = Decimal("0.01")


def compute_expected_payout_method_a(record: EnrollmentRecord) -> Decimal:
    """Per-period proration: round the monthly installment, then sum it
    across the term. Pure Python, no I/O."""
    monthly = (record.incentive_amount / record.term_months).quantize(
        _CENTS, rounding=ROUND_HALF_UP
    )
    return (monthly * record.term_months).quantize(_CENTS, rounding=ROUND_HALF_UP)


def compute_expected_payout_method_b(store: DuckDBStore, record: EnrollmentRecord) -> Decimal:
    """Total-then-round: SQL computes the lump-sum total, rounded once.

    Deliberately independent of Path A: no shared arithmetic helper. Runs
    through ``DuckDBStore`` (mirrors the "logic lives in SQL, data lives in
    the warehouse" dbt-on-warehouse pattern the module generalizes).
    """
    df = store.query(
        "SELECT ROUND(CAST(? AS DOUBLE), 2) AS total",
        [float(record.incentive_amount)],
    )
    return Decimal(str(df["total"].iloc[0])).quantize(_CENTS, rounding=ROUND_HALF_UP)


def agree(a: Decimal, b: Decimal, tolerance: Decimal = DEFAULT_TOLERANCE) -> bool:
    return abs(a - b) <= tolerance


@dataclass
class DualSourceEvaluation:
    method_a_amount: Decimal
    method_b_amount: Decimal
    delta: Decimal
    tolerance: Decimal
    agreed: bool


def evaluate(
    store: DuckDBStore,
    record: EnrollmentRecord,
    tolerance: Decimal = DEFAULT_TOLERANCE,
) -> DualSourceEvaluation:
    """Run both paths and compare — no I/O beyond Path B's own SQL query."""
    a = compute_expected_payout_method_a(record)
    b = compute_expected_payout_method_b(store, record)
    delta = abs(a - b)
    return DualSourceEvaluation(
        method_a_amount=a,
        method_b_amount=b,
        delta=delta,
        tolerance=tolerance,
        agreed=agree(a, b, tolerance),
    )
