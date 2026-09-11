"""Synthetic unemployment-insurance benefit data generator (Phase 3 concept demo).

Generates two datasets for a fictional "Sample State Unemployment Benefit
Program — synthetic data":

* **claimant_roster** — one row per approved (or denied) benefit claim:
  claimant id, program, approved weekly benefit amount, eligibility window,
  and status.
* **benefit_payments** — one row per benefit payment issued against a roster
  claim: claimant id, pay period, amount, payment date, and an optional
  adjustment code.

All data is fabricated with Faker/``random`` from a fixed seed. No real
agency, state program, or recipient data is used or referenced — see
``examples/public_payments/README.md`` for the clean-room declaration.

The baseline dataset is generated first (a believable, mostly-clean program),
then a small, precisely-counted set of error patterns is injected on top of
it (see :func:`inject_errors`). Because the injected rows are tracked
explicitly (returned as ``ground_truth``), the test suite can measure the
engine's catch rate against a known answer instead of asserting a number by
hand — see ``tests/test_detection_rate.py``.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import date, timedelta

import pandas as pd
from faker import Faker

# Fixed reference point for all generated dates — deterministic, not wall-clock
# "today" — so the same seed always yields identical data (mirrors
# civicpay.data.synthetic.AS_OF_DATETIME).
AS_OF_DATE = date(2026, 9, 1)

# Program definitions: fictional, generic names — not modeled on any specific
# real state's program name or parameters.
PROGRAMS: dict[str, dict[str, int]] = {
    "SUI-STD": {"window_weeks": 26, "weight": 70},
    "SUI-EXT": {"window_weeks": 39, "weight": 30},
}

MIN_WEEKLY_BENEFIT = 120
MAX_WEEKLY_BENEFIT = 823

# Fraction of claimants whose claim was denied outright (zero payments).
DENIED_FRACTION = 0.05

# Valid reasons a payment may legitimately differ from the approved weekly
# benefit amount. A payment that differs from the approved amount WITHOUT one
# of these codes is a genuine amount-mismatch error; a payment that differs
# WITH one of these codes is a legitimate, explained adjustment.
VALID_ADJUSTMENT_CODES: tuple[str, ...] = ("BACKPAY_ADJ", "OVERPAYMENT_OFFSET", "COLA_ADJ")

DEFAULT_CLAIMANT_COUNT = 3_000

# Injected-error cohort sizes. Kept as fixed, known counts (not fractions) so
# the detection-rate test can assert an exact, reproducible catch rate rather
# than a number typed by hand — see tests/test_detection_rate.py.
INJECTED_DUPLICATE_COUNT = 120
INJECTED_OUT_OF_WINDOW_COUNT = 120
INJECTED_AMOUNT_MISMATCH_COUNT = 120
# Decoy rows: amount differs from the approved benefit, but WITH a valid
# adjustment code — these must NOT be flagged as amount-mismatch errors. This
# is what proves the engine "correctly leaves good data alone" (spec §4.1)
# specifically for the amount-mismatch rule, not just for untouched rows.
LEGITIMATE_ADJUSTMENT_COUNT = 40


@dataclass(frozen=True)
class GroundTruth:
    """The exact set of injected-error payment ids, by category (for tests)."""

    duplicate_payment_ids: frozenset[str] = field(default_factory=frozenset)
    out_of_window_payment_ids: frozenset[str] = field(default_factory=frozenset)
    amount_mismatch_payment_ids: frozenset[str] = field(default_factory=frozenset)
    legitimate_adjustment_payment_ids: frozenset[str] = field(default_factory=frozenset)


def generate_roster(n: int, fake: Faker, rng: random.Random) -> pd.DataFrame:
    """Generate ``n`` claimant roster rows.

    Each claimant is assigned a program, an approved weekly benefit amount,
    and an eligibility window. ``DENIED_FRACTION`` of claimants are denied
    (no payments will ever be generated for them); the rest are ``active``
    (still within their window as of ``AS_OF_DATE``) or ``exhausted`` (window
    has elapsed).
    """
    program_codes = list(PROGRAMS)
    program_weights = [PROGRAMS[p]["weight"] for p in program_codes]

    rows = []
    for i in range(1, n + 1):
        claimant_id = f"CLM-{i:06d}"
        program_code = rng.choices(program_codes, weights=program_weights, k=1)[0]
        window_weeks = PROGRAMS[program_code]["window_weeks"]
        approved_weekly_benefit = float(rng.randint(MIN_WEEKLY_BENEFIT, MAX_WEEKLY_BENEFIT))
        # Claim started somewhere between 2 and (window + 20) weeks ago, so the
        # cohort spans still-active claims and fully-exhausted ones.
        start_weeks_ago = rng.randint(2, window_weeks + 20)
        eligibility_start = AS_OF_DATE - timedelta(weeks=start_weeks_ago)
        eligibility_end = eligibility_start + timedelta(weeks=window_weeks)
        denied = rng.random() < DENIED_FRACTION
        status = (
            "denied" if denied else ("exhausted" if AS_OF_DATE >= eligibility_end else "active")
        )
        rows.append(
            {
                "claimant_id": claimant_id,
                "program_code": program_code,
                "approved_weekly_benefit": approved_weekly_benefit,
                "eligibility_start": eligibility_start,
                "eligibility_end": eligibility_end,
                "status": status,
                "claimant_name": fake.name(),
            }
        )
    return pd.DataFrame(rows)


def generate_payments(roster: pd.DataFrame, rng: random.Random) -> pd.DataFrame:
    """Generate the clean baseline payment ledger for a roster.

    One weekly payment per elapsed week within each non-denied claimant's
    eligibility window (capped at the window length), amount equal to the
    claimant's approved weekly benefit, paid a few days after the period ends.
    """
    rows = []
    seq = 0
    for r in roster.itertuples(index=False):
        if r.status == "denied":
            continue
        weeks_elapsed = min(
            (r.eligibility_end - r.eligibility_start).days // 7,
            max(0, (AS_OF_DATE - r.eligibility_start).days // 7),
        )
        for week in range(weeks_elapsed):
            seq += 1
            pay_period_start = r.eligibility_start + timedelta(weeks=week)
            pay_period_end = pay_period_start + timedelta(days=6)
            payment_date = pay_period_end + timedelta(days=rng.randint(1, 5))
            rows.append(
                {
                    "payment_id": f"PMT-{seq:07d}",
                    "claimant_id": r.claimant_id,
                    "program_code": r.program_code,
                    "pay_period_start": pay_period_start,
                    "pay_period_end": pay_period_end,
                    "payment_date": payment_date,
                    "amount": r.approved_weekly_benefit,
                    "adjustment_code": None,
                    "status": "issued",
                }
            )
    return pd.DataFrame(rows)


def _next_payment_id(payments: pd.DataFrame) -> int:
    if payments.empty:
        return 1
    return int(payments["payment_id"].str.removeprefix("PMT-").astype(int).max()) + 1


def inject_errors(
    payments: pd.DataFrame,
    roster: pd.DataFrame,
    rng: random.Random,
) -> tuple[pd.DataFrame, GroundTruth]:
    """Inject a precisely-counted set of error patterns into ``payments``.

    Target rows for each category are sampled without replacement and kept
    disjoint across categories (and disjoint from the roster's denied
    claimants, who never appear in the ledger at all), so each injected error
    belongs to exactly one category and the ground truth is unambiguous.
    """
    eligible_idx = payments.index[payments["status"] == "issued"].tolist()
    total_needed = (
        INJECTED_DUPLICATE_COUNT
        + INJECTED_OUT_OF_WINDOW_COUNT
        + INJECTED_AMOUNT_MISMATCH_COUNT
        + LEGITIMATE_ADJUSTMENT_COUNT
    )
    if len(eligible_idx) < total_needed:
        raise ValueError(
            f"Not enough baseline payments ({len(eligible_idx)}) to inject "
            f"{total_needed} distinct error rows. Increase claimant count."
        )
    sample = rng.sample(eligible_idx, total_needed)
    dup_idx = sample[:INJECTED_DUPLICATE_COUNT]
    ooo_idx = sample[
        INJECTED_DUPLICATE_COUNT : INJECTED_DUPLICATE_COUNT + INJECTED_OUT_OF_WINDOW_COUNT
    ]
    mismatch_idx = sample[
        INJECTED_DUPLICATE_COUNT + INJECTED_OUT_OF_WINDOW_COUNT : INJECTED_DUPLICATE_COUNT
        + INJECTED_OUT_OF_WINDOW_COUNT
        + INJECTED_AMOUNT_MISMATCH_COUNT
    ]
    legit_idx = sample[-LEGITIMATE_ADJUSTMENT_COUNT:]

    payments = payments.copy()
    roster_by_claimant = roster.set_index("claimant_id")
    next_id = _next_payment_id(payments)

    # 1. Duplicate payments: append an exact-copy row (new payment_id) for
    #    each sampled row, so both the original and the copy are flagged.
    dup_rows = payments.loc[dup_idx].copy()
    dup_new_ids = []
    for _ in range(len(dup_rows)):
        dup_new_ids.append(f"PMT-{next_id:07d}")
        next_id += 1
    dup_rows["payment_id"] = dup_new_ids
    duplicate_payment_ids = frozenset(payments.loc[dup_idx, "payment_id"]) | frozenset(dup_new_ids)
    payments = pd.concat([payments, dup_rows], ignore_index=True)

    # 2. Out-of-window payments: push the pay period outside the claimant's
    #    eligibility window (before start or after end, chosen per row).
    for idx in ooo_idx:
        claimant_id = payments.at[idx, "claimant_id"]
        window = roster_by_claimant.loc[claimant_id]
        if rng.random() < 0.5:
            new_start = window["eligibility_start"] - timedelta(days=rng.randint(7, 40))
        else:
            new_start = window["eligibility_end"] + timedelta(days=rng.randint(7, 40))
        payments.at[idx, "pay_period_start"] = new_start
        payments.at[idx, "pay_period_end"] = new_start + timedelta(days=6)
        payments.at[idx, "payment_date"] = new_start + timedelta(days=6 + rng.randint(1, 5))
    out_of_window_payment_ids = frozenset(payments.loc[ooo_idx, "payment_id"])

    # 3. Amount mismatches: change the amount away from the approved weekly
    #    benefit, with no adjustment code (a genuine, unexplained error).
    for idx in mismatch_idx:
        claimant_id = payments.at[idx, "claimant_id"]
        approved = roster_by_claimant.loc[claimant_id, "approved_weekly_benefit"]
        delta = rng.choice([-1, 1]) * rng.randint(50, 300)
        payments.at[idx, "amount"] = max(0.0, approved + delta)
    amount_mismatch_payment_ids = frozenset(payments.loc[mismatch_idx, "payment_id"])

    # 4. Legitimate adjustments (decoys): amount also differs from the
    #    approved benefit, but WITH a valid adjustment code — must NOT be
    #    flagged. Proves the engine isn't just flagging every deviation.
    for idx in legit_idx:
        claimant_id = payments.at[idx, "claimant_id"]
        approved = roster_by_claimant.loc[claimant_id, "approved_weekly_benefit"]
        delta = rng.choice([-1, 1]) * rng.randint(20, 150)
        payments.at[idx, "amount"] = max(0.0, approved + delta)
        payments.at[idx, "adjustment_code"] = rng.choice(VALID_ADJUSTMENT_CODES)
    legitimate_adjustment_payment_ids = frozenset(payments.loc[legit_idx, "payment_id"])

    ground_truth = GroundTruth(
        duplicate_payment_ids=duplicate_payment_ids,
        out_of_window_payment_ids=out_of_window_payment_ids,
        amount_mismatch_payment_ids=amount_mismatch_payment_ids,
        legitimate_adjustment_payment_ids=legitimate_adjustment_payment_ids,
    )
    return payments, ground_truth


def generate_dataset(
    seed: int = 42, claimant_count: int = DEFAULT_CLAIMANT_COUNT
) -> tuple[pd.DataFrame, pd.DataFrame, GroundTruth]:
    """Generate the full synthetic scenario: ``(roster, payments, ground_truth)``.

    Deterministic: the same ``seed`` and ``claimant_count`` always produce
    identical output.
    """
    fake = Faker()
    fake.seed_instance(seed)
    rng = random.Random(seed)

    roster = generate_roster(claimant_count, fake, rng)
    payments = generate_payments(roster, rng)
    payments, ground_truth = inject_errors(payments, roster, rng)
    return roster, payments, ground_truth


def write_csvs(roster: pd.DataFrame, payments: pd.DataFrame, out_dir: str) -> None:
    """Write the two datasets as CSVs (clearly labeled synthetic by content/README)."""
    from pathlib import Path

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    roster.to_csv(out / "claimant_roster.csv", index=False)
    payments.to_csv(out / "benefit_payments.csv", index=False)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--claimants", type=int, default=DEFAULT_CLAIMANT_COUNT)
    parser.add_argument(
        "--out-dir", default=str(__import__("pathlib").Path(__file__).parent / "data")
    )
    args = parser.parse_args()

    roster_df, payments_df, gt = generate_dataset(seed=args.seed, claimant_count=args.claimants)
    write_csvs(roster_df, payments_df, args.out_dir)
    print(
        f"Generated {len(roster_df):,} roster rows and {len(payments_df):,} payment rows "
        f"-> {args.out_dir}"
    )
