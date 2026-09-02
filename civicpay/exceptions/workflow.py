"""Exception workflow — priority scoring and SLA aging (spec §14.3 / §17.3 Ticket 6).

Priority is computed on read (not persisted) so it never goes stale:

    priority_score = severity_weight × amount_at_risk_factor × age_factor

* ``severity_weight``: high=3, medium=2, low=1 (from the queue's ``priority``).
* ``amount_at_risk_factor``: buckets the at-risk dollar amount.
* ``age_factor``: 1.0 while within the SLA window, then grows by 0.5 per day
  past the SLA threshold (escalating overdue items).

``amount_at_risk`` is resolved by looking up the referenced record's amount
(transactions / payment_records); 0 when no amount applies.
"""

from __future__ import annotations

from datetime import datetime

SEVERITY_WEIGHTS: dict[str, float] = {"high": 3.0, "medium": 2.0, "low": 1.0}
DEFAULT_SLA_DAYS = 7


def severity_weight(priority: str) -> float:
    return SEVERITY_WEIGHTS.get(str(priority).lower(), 1.0)


def amount_at_risk_factor(amount: float) -> float:
    """Bucket the at-risk amount into a 1–4 multiplier."""
    a = float(amount or 0.0)
    if a < 100:
        return 1.0
    if a < 1_000:
        return 2.0
    if a < 10_000:
        return 3.0
    return 4.0


def age_factor(age_days: int, sla_days: int = DEFAULT_SLA_DAYS) -> float:
    """1.0 within the SLA window; +0.5 per day overdue."""
    age = max(0, int(age_days))
    if age <= sla_days:
        return 1.0
    return round(1.0 + 0.5 * (age - sla_days), 4)


def compute_priority_score(
    priority: str,
    amount_at_risk: float,
    age_days: int,
    sla_days: int = DEFAULT_SLA_DAYS,
) -> float:
    """Full priority score (higher = more urgent)."""
    return round(
        severity_weight(priority)
        * amount_at_risk_factor(amount_at_risk)
        * age_factor(age_days, sla_days),
        4,
    )


def age_days(created_at: datetime, as_of: datetime) -> int:
    """Whole days between creation and the as-of date (>= 0)."""
    if created_at is None:
        return 0
    a = datetime.fromisoformat(str(as_of)) if isinstance(as_of, str) else as_of
    c = datetime.fromisoformat(str(created_at)) if isinstance(created_at, str) else created_at
    # Compare tz-naive; drop tzinfo (treat aware as UTC).
    a = a.replace(tzinfo=None) if a.tzinfo else a
    c = c.replace(tzinfo=None) if c.tzinfo else c
    delta = a - c
    return max(0, int(delta.total_seconds() // 86400))
