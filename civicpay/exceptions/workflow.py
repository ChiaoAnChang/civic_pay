"""Exception workflow — priority scoring and SLA aging (spec §14.3 / §17.3 Ticket 6).

Priority is computed on read (not persisted) so it never goes stale — it is a
non-authoritative triage aid, not an audit record; the auditable fact for a
resolved exception is its ``exception_resolve`` ledger event, not this score:

    priority_score = severity_weight × amount_at_risk_factor × age_factor

* ``severity_weight``: high=3, medium=2, low=1 (from the queue's ``priority``).
* ``amount_at_risk_factor``: buckets the at-risk dollar amount.
* ``age_factor``: 1.0 while within the SLA window, then grows by 0.5 per day
  past the SLA threshold (escalating overdue items).

``amount_at_risk`` is resolved by looking up the referenced record's amount
(transactions / payment_records / pending_enrollments); ``None`` — not
``0.0`` — when no amount concept applies (e.g. a DQ exception on accounts or
customers) or the record/amount can't be resolved. The two are genuinely
different: a resolved $0.00 is a real (if trivial) amount and gets the
lowest bucket; "not applicable" gets a neutral midpoint bucket instead, so a
high-severity amount-less exception isn't structurally capped below every
dollar-bearing one in the same sorted queue (see ``NEUTRAL_AMOUNT_AT_RISK_
FACTOR``).

SLA window: by default it is resolved *per severity* (``SLA_DAYS_BY_SEVERITY``)
rather than one global value — a high-severity item and a low-severity one
plausibly deserve very different escalation windows even though
``severity_weight`` already scales urgency once an item does escalate. Pass an
explicit ``sla_days`` to override with a single value for every item (e.g. the
CLI's ``--sla-days`` flag).
"""

from __future__ import annotations

from datetime import datetime

SEVERITY_WEIGHTS: dict[str, float] = {"high": 3.0, "medium": 2.0, "low": 1.0}
DEFAULT_SLA_DAYS = 7

# Per-severity SLA windows (days), used when no explicit ``sla_days`` override
# is given. A high-severity item starts escalating sooner than a low-severity
# one — the canonical shape of a real SLA policy (P1/P2/P3 response windows).
SLA_DAYS_BY_SEVERITY: dict[str, int] = {"high": 3, "medium": 7, "low": 14}


def severity_weight(priority: str) -> float:
    return SEVERITY_WEIGHTS.get(str(priority).lower(), 1.0)


def resolve_sla_days(priority: str, sla_days: int | None) -> int:
    """Resolve the SLA window: an explicit override, or the per-severity default."""
    if sla_days is not None:
        return sla_days
    return SLA_DAYS_BY_SEVERITY.get(str(priority).lower(), DEFAULT_SLA_DAYS)


# Neutral multiplier used when no dollar amount applies at all
# (``amount is None``) — the midpoint of the 1-4 range, not the floor.
# Distinguishing "not applicable" from "resolved to a genuine $0.00" matters:
# without this, an amount-less exception could never outrank even a
# medium-severity, moderately-priced dollar-bearing one, regardless of how
# severe or systemic the amount-less issue actually was.
NEUTRAL_AMOUNT_AT_RISK_FACTOR = 2.5


def amount_at_risk_factor(amount: float | None) -> float:
    """Bucket the at-risk amount into a 1–4 multiplier, or
    ``NEUTRAL_AMOUNT_AT_RISK_FACTOR`` when ``amount is None`` (no dollar
    amount concept applies to this exception) or NaN (an unresolvable value
    that reached here anyway — e.g. a nullable DOUBLE amount column read
    back as NaN; every bucket comparison against NaN is False, so without
    this guard NaN would fall through to the *highest* bucket, the opposite
    of the intended neutral treatment)."""
    if amount is None or amount != amount:  # NaN != NaN is the only falsy self-inequality
        return NEUTRAL_AMOUNT_AT_RISK_FACTOR
    a = float(amount)
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
    amount_at_risk: float | None,
    age_days: int,
    sla_days: int | None = None,
) -> float:
    """Full priority score (higher = more urgent).

    ``sla_days=None`` (the default) resolves the SLA window per severity via
    ``resolve_sla_days``; pass an explicit value to use the same window for
    every priority (e.g. a CLI override).
    """
    return round(
        severity_weight(priority)
        * amount_at_risk_factor(amount_at_risk)
        * age_factor(age_days, resolve_sla_days(priority, sla_days)),
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
