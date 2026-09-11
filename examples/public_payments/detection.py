"""Benefit-payment error detection engine (Phase 3 concept demo).

This is the "genuinely new adapter code" flagged by the spec's investigation
task (spec §5): ``civicpay.quality.checks`` is generic enough to reuse
as-is for single-table checks (completeness, ranges, enums — see
``config/dq_checks.yml``), but ``civicpay.recon.matcher`` is hard-wired to
payment-vs-ledger matching *by reference id*, which has no equivalent in this
scenario. Duplicate-payment, eligibility-window, and approved-amount checks
here are all **cross-row or cross-table** comparisons (a payment against
other payments for the same claimant/period, or a payment against its own
claimant's roster row) that none of the existing dispatch table's check
functions perform — so they're implemented fresh, following the same
architectural rule as ``civicpay.recon.matcher``: pure functions of plain
DataFrames in, no I/O, no wall-clock time, fully deterministic and
independently unit-testable.

Three detection functions, one per error category (spec §4.1):

* :func:`detect_duplicate_payments` — same claimant + pay period paid twice.
* :func:`detect_out_of_window_payments` — payment period falls outside the
  claimant's approved eligibility window.
* :func:`detect_amount_mismatches` — payment amount doesn't match the
  claimant's approved weekly benefit, and carries no valid adjustment code.

:func:`run_detection` runs all three and returns a flat, priority-scored list
of :class:`Flag` rows plus a summary dict.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from civicpay.exceptions.workflow import compute_priority_score

DUPLICATE_PAYMENT = "duplicate_payment"
OUT_OF_WINDOW = "out_of_window"
AMOUNT_MISMATCH = "amount_mismatch"

# Provisional severity per reason (mirrors the severity field civic_pay's own
# config/dq_checks.yml assigns per check — see civicpay.quality.checks).
_SEVERITY_BY_REASON: dict[str, str] = {
    DUPLICATE_PAYMENT: "high",
    AMOUNT_MISMATCH: "high",
    OUT_OF_WINDOW: "medium",
}

DEFAULT_AMOUNT_TOLERANCE = 0.01
DEFAULT_VALID_ADJUSTMENT_CODES: frozenset[str] = frozenset(
    {"BACKPAY_ADJ", "OVERPAYMENT_OFFSET", "COLA_ADJ"}
)


@dataclass(frozen=True)
class DetectionConfig:
    """Tunable detection parameters."""

    amount_tolerance: float = DEFAULT_AMOUNT_TOLERANCE
    valid_adjustment_codes: frozenset[str] = DEFAULT_VALID_ADJUSTMENT_CODES


@dataclass(frozen=True)
class Flag:
    """One flagged payment record, with a human-readable reason and priority."""

    flag_id: str
    payment_id: str
    claimant_id: str
    reason: str
    detail: str
    amount: float
    severity: str
    priority_score: float


def detect_duplicate_payments(payments: pd.DataFrame) -> pd.DataFrame:
    """Flag every payment that shares (claimant_id, pay_period_start) with
    another *issued* payment.

    Returns the flagged rows of ``payments`` (both/all rows in each duplicate
    group), unchanged, so callers can format details from the original data.
    """
    issued = payments[payments["status"] == "issued"]
    group_sizes = issued.groupby(["claimant_id", "pay_period_start"])["payment_id"].transform(
        "size"
    )
    return issued[group_sizes > 1]


def detect_out_of_window_payments(payments: pd.DataFrame, roster: pd.DataFrame) -> pd.DataFrame:
    """Flag payments whose pay period starts outside the claimant's approved
    eligibility window.

    A left join on ``claimant_id`` brings each payment's own eligibility
    window alongside it; this is exactly the kind of per-row, cross-table
    comparison ``civicpay.quality.checks`` has no dispatch entry for (its
    ``accuracy_referential`` check only tests set membership, not a per-row
    range against another table's matching row).
    """
    issued = payments[payments["status"] == "issued"]
    joined = issued.merge(
        roster[["claimant_id", "eligibility_start", "eligibility_end"]],
        on="claimant_id",
        how="left",
    )
    mask = (joined["pay_period_start"] < joined["eligibility_start"]) | (
        joined["pay_period_start"] > joined["eligibility_end"]
    )
    return joined[mask]


def detect_amount_mismatches(
    payments: pd.DataFrame,
    roster: pd.DataFrame,
    config: DetectionConfig | None = None,
) -> pd.DataFrame:
    """Flag payments whose amount doesn't match the claimant's approved
    weekly benefit and carry no valid adjustment code.

    A payment that differs from the approved amount but carries one of
    ``config.valid_adjustment_codes`` is a legitimate, explained adjustment
    (e.g. a back-pay correction) and is deliberately left unflagged.
    """
    config = config or DetectionConfig()
    issued = payments[payments["status"] == "issued"]
    joined = issued.merge(
        roster[["claimant_id", "approved_weekly_benefit"]], on="claimant_id", how="left"
    )
    amount_differs = (
        joined["amount"] - joined["approved_weekly_benefit"]
    ).abs() > config.amount_tolerance
    has_valid_code = joined["adjustment_code"].isin(config.valid_adjustment_codes)
    mask = amount_differs & ~has_valid_code
    return joined[mask]


def run_detection(
    payments: pd.DataFrame,
    roster: pd.DataFrame,
    config: DetectionConfig | None = None,
    sla_days: int = 7,
) -> tuple[list[Flag], dict[str, int]]:
    """Run all three detection rules and return ``(flags, category_counts)``.

    Each flagged row is priority-scored using
    ``civicpay.exceptions.workflow.compute_priority_score`` (age is always 0
    here — every flag is freshly detected in this run, so ``age_factor`` is
    at its floor of 1.0; a real deployment would compute age from a
    previously-opened exception's ``created_at``, same as
    ``civicpay.exceptions.workflow`` documents).
    """
    config = config or DetectionConfig()
    flags: list[Flag] = []
    seq = 0

    def _append(rows: pd.DataFrame, reason: str, detail_fn) -> None:
        nonlocal seq
        for r in rows.itertuples(index=False):
            seq += 1
            severity = _SEVERITY_BY_REASON[reason]
            score = compute_priority_score(
                priority=severity,
                amount_at_risk=float(r.amount),
                age_days=0,
                sla_days=sla_days,
            )
            flags.append(
                Flag(
                    flag_id=f"PPFLAG-{seq:06d}",
                    payment_id=r.payment_id,
                    claimant_id=r.claimant_id,
                    reason=reason,
                    detail=detail_fn(r),
                    amount=float(r.amount),
                    severity=severity,
                    priority_score=score,
                )
            )

    dup = detect_duplicate_payments(payments)
    _append(
        dup,
        DUPLICATE_PAYMENT,
        lambda r: f"claimant {r.claimant_id} paid twice for period starting {r.pay_period_start}",
    )

    ooo = detect_out_of_window_payments(payments, roster)
    _append(
        ooo,
        OUT_OF_WINDOW,
        lambda r: (
            f"pay period {r.pay_period_start} is outside claimant {r.claimant_id}'s "
            f"eligibility window [{r.eligibility_start}, {r.eligibility_end}]"
        ),
    )

    mismatch = detect_amount_mismatches(payments, roster, config)
    _append(
        mismatch,
        AMOUNT_MISMATCH,
        lambda r: (
            f"paid {r.amount:.2f} vs approved {r.approved_weekly_benefit:.2f} "
            f"for claimant {r.claimant_id}, no valid adjustment code"
        ),
    )

    counts = {
        DUPLICATE_PAYMENT: len(dup),
        OUT_OF_WINDOW: len(ooo),
        AMOUNT_MISMATCH: len(mismatch),
    }
    return flags, counts


__all__ = [
    "AMOUNT_MISMATCH",
    "DUPLICATE_PAYMENT",
    "OUT_OF_WINDOW",
    "DetectionConfig",
    "Flag",
    "detect_amount_mismatches",
    "detect_duplicate_payments",
    "detect_out_of_window_payments",
    "run_detection",
]
