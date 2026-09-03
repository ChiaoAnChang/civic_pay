"""Deterministic synthetic-data generators for CivicPay Open Framework.

All data is synthetic. No real PII, account numbers, or transaction data are
used. Distributions are original design generated with Faker; they are NOT
derived from any employer system (see PROVENANCE.md).

The payment file is deliberately constructed in five buckets so that every
reconciliation outcome (matched / fuzzy / unmatched / duplicate exception /
amount-mismatch exception) is exercised by the test suite and visible in the
demo dashboard:

    - ~850 exact matches   (reference_id + amount + date match a ledger txn)
    - ~80  unmatched payments (reference_id not present in ledger)
    - ~40  fuzzy matches    (reference close, amount/date within tolerance)
    - ~20  duplicate references (reference_id already present in the file)
    - ~10  amount mismatches (reference_id matches a txn but amount differs)

Determinism: the same ``seed`` always yields identical data.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from faker import Faker

from civicpay.data import models as M

# Fixed reference point for all generated timestamps. Using a constant (instead
# of "now") guarantees determinism: the same --seed always yields identical data
# across runs and calendar days. This is the synthetic data's "as-of" instant.
AS_OF_DATETIME = datetime(2026, 9, 1, 0, 0, 0, tzinfo=UTC)
AS_OF_DATE = AS_OF_DATETIME.date()
# Earliest date for historical records (5 years before the as-of date).
EARLIEST_DATE = AS_OF_DATE - timedelta(days=5 * 365)

# Bucket sizes (sum = 1000 payment records)
BUCKET_EXACT = 850
BUCKET_UNMATCHED = 80
BUCKET_FUZZY = 40
BUCKET_DUPLICATE = 20
BUCKET_AMOUNT_MISMATCH = 10

# Size of the transaction tail consumed by the payment-matching buckets
# (exact + fuzzy + amount-mismatch). The genuinely-stale cohort is injected
# BEYOND this index so it never participates in payment matching and the
# reconciliation outcome counts are unaffected.
_PAYMENT_POOL_SIZE = BUCKET_EXACT + BUCKET_FUZZY + BUCKET_AMOUNT_MISMATCH

# Enrollment candidate generation (Ticket 13). Program definitions are
# original synthetic values for the demo — not derived from, and not matched
# to, any real incentive/rebate program.
ENROLLMENT_PROGRAMS: dict[str, dict[str, Any]] = {
    "STARTER": {"max_incentive_amount": 5_000, "term_months": (1, 12)},
    "GROWTH": {"max_incentive_amount": 25_000, "term_months": (6, 36)},
    "ENTERPRISE": {"max_incentive_amount": 100_000, "term_months": (12, 60)},
}
ENROLLMENT_REGIONS = ["NORTHEAST", "SOUTHEAST", "MIDWEST", "SOUTHWEST", "WEST"]
MIN_ENROLLMENT_DATE = date(2020, 1, 1)

# Deliberately-defective cohort sizes (Ticket 13 §3 / DoD #2) — small, fixed
# counts so validators have a known set of each defect class to catch:
# bad_date (outside the allowed range), out_of_range_amount, duplicate
# entity_id, stray-space numeric (a value that survives .strip() but still
# isn't parseable, e.g. an embedded space), and missing required.
_ENROLLMENT_DEFECT_COUNTS: dict[str, int] = {
    "bad_date": 5,
    "out_of_range_amount": 5,
    "duplicate_entity_id": 5,
    "stray_space": 5,
    "missing_required": 5,
}


def _stray_space_number(value: float) -> str:
    """A numeric string with an embedded space that survives ``.strip()``.

    E.g. ``1500.00`` -> ``"150 0.00"`` — leading/trailing strip does not fix
    it, so it correctly fails ``float()`` parsing (a realistic fat-finger
    defect, not merely cosmetic whitespace).
    """
    s = f"{value:.2f}"
    mid = len(s) // 2
    return s[:mid] + " " + s[mid:]


def generate_pending_enrollments(n: int, fake: Faker, rng: random.Random) -> pd.DataFrame:
    """Generate ``n`` enrollment candidates for Ticket 13's validation layer.

    Most are valid; a small fixed cohort deliberately violates
    ``config/enrollment_rules.yml`` (see ``_ENROLLMENT_DEFECT_COUNTS``) so
    ``civicpay.enrollment.validators`` has a known set of defects to catch.
    Called after ``generate_payment_file`` in ``generate_all`` so it consumes
    ``fake``/``rng`` further along the shared deterministic stream without
    shifting any earlier generator's output.
    """
    defect_total = sum(_ENROLLMENT_DEFECT_COUNTS.values())
    n_clean = max(0, n - defect_total)
    total = n_clean + defect_total
    entity_ids = [f"ENT-{i:05d}" for i in range(1, total + 1)]

    def clean_row(i: int, entity_id: str) -> dict[str, Any]:
        program = rng.choice(list(ENROLLMENT_PROGRAMS))
        cfg = ENROLLMENT_PROGRAMS[program]
        term = rng.randint(*cfg["term_months"])
        # ~80% of clean candidates are built from a round monthly installment
        # (installment * term), so the dual-source gate's two independent
        # rounding policies agree exactly -- matching the real-world pattern
        # this ticket generalizes: a per-period ledger and a lump-sum total
        # usually agree, and only occasionally drift by rounding remainder.
        # The other ~20% use a fully arbitrary total (unlikely to divide
        # evenly across the term), which is where they genuinely diverge.
        if rng.random() < 0.8:
            monthly = round(rng.uniform(50.0, float(cfg["max_incentive_amount"]) / term), 2)
            amount = round(monthly * term, 2)
        else:
            amount = round(rng.uniform(500.0, float(cfg["max_incentive_amount"])), 2)
        enroll_date = fake.date_time_between(
            start_date=MIN_ENROLLMENT_DATE, end_date=AS_OF_DATETIME
        ).replace(tzinfo=UTC)
        return {
            "enrollment_id": f"ENR-{i:06d}",
            "entity_id": entity_id,
            "program_code": program,
            "enrollment_date": enroll_date,
            "incentive_amount": f"{amount:.2f}",
            "term_months": str(term),
            "region": rng.choice(ENROLLMENT_REGIONS),
            "submitted_by": f"operator-{rng.randint(1, 20):02d}",
            "status": M.EnrollmentStatus.PENDING,
            "created_at": AS_OF_DATETIME,
        }

    rows: list[dict[str, Any]] = [clean_row(i, entity_ids[i - 1]) for i in range(1, n_clean + 1)]
    idx = n_clean + 1

    for _ in range(_ENROLLMENT_DEFECT_COUNTS["bad_date"]):
        row = clean_row(idx, entity_ids[idx - 1])
        if rng.random() < 0.5:
            bad = MIN_ENROLLMENT_DATE - timedelta(days=rng.randint(1, 365))
        else:
            bad = AS_OF_DATE + timedelta(days=rng.randint(1, 365))
        row["enrollment_date"] = datetime.combine(bad, datetime.min.time()).replace(tzinfo=UTC)
        rows.append(row)
        idx += 1

    for _ in range(_ENROLLMENT_DEFECT_COUNTS["out_of_range_amount"]):
        row = clean_row(idx, entity_ids[idx - 1])
        cap = ENROLLMENT_PROGRAMS[row["program_code"]]["max_incentive_amount"]
        bad_amount = rng.choice([-round(rng.uniform(10, 500), 2), round(cap * 3, 2)])
        row["incentive_amount"] = f"{bad_amount:.2f}"
        rows.append(row)
        idx += 1

    dup_entity = entity_ids[0]  # reuse a real clean row's entity_id on purpose
    for _ in range(_ENROLLMENT_DEFECT_COUNTS["duplicate_entity_id"]):
        row = clean_row(idx, dup_entity)
        rows.append(row)
        idx += 1

    for _ in range(_ENROLLMENT_DEFECT_COUNTS["stray_space"]):
        row = clean_row(idx, entity_ids[idx - 1])
        amount = float(row["incentive_amount"])
        row["incentive_amount"] = _stray_space_number(amount)
        rows.append(row)
        idx += 1

    for _ in range(_ENROLLMENT_DEFECT_COUNTS["missing_required"]):
        row = clean_row(idx, entity_ids[idx - 1])
        row["submitted_by"] = ""
        rows.append(row)
        idx += 1

    return pd.DataFrame(rows)


# Intentionally stale cohort (OPEN_QUESTIONS §D): a small fraction of ledger
# transactions are seeded with genuinely old created_at dates (45/60/90 days
# before the as-of date) so the timeliness DQ check catches meaningful
# staleness rather than a date-range boundary artifact. Without this, the only
# "stale" records were August-1 postings that happened to be 31 days old vs the
# September-1 as-of date — i.e. 1 day over the threshold.
STALE_FRACTION = 0.035
STALE_AGES_DAYS = (45, 60, 90)


def _utcnow() -> datetime:
    """Return the fixed as-of datetime (deterministic, never wall-clock now)."""
    return AS_OF_DATETIME


def _mask_account_number(rng: random.Random) -> str:
    """Return a masked account number like ****1234 (synthetic, never real)."""
    last4 = f"{rng.randint(0, 9999):04d}"
    return f"****{last4}"


# Visually-similar but non-digit substitutions. Because real reference_ids use
# only digits in their numeric portion, substituting a digit with a letter
# guarantees the mangled ref is NOT a real ledger reference, while remaining
# Levenshtein-close to the original (1 substitution) for fuzzy matching.
_VISUAL_SUBS = {"0": "O", "1": "l", "2": "Z", "5": "S", "6": "G", "8": "B"}


def _fuzz_reference(ref: str, rng: random.Random) -> str:
    """Mangle a reference id so it is close but not exact (for fuzzy matching).

    Guarantees the result is NOT a real ledger reference (it contains a letter
    where the original had a digit) while staying one edit from the original.
    """
    if not ref or len(ref) < 4:
        return ref + "X"
    chars = list(ref)
    positions = [i for i in range(2, len(chars)) if chars[i] in _VISUAL_SUBS]
    if positions:
        i = rng.choice(positions)
        chars[i] = _VISUAL_SUBS[chars[i]]
        return "".join(chars)
    # Fallback: shift one character's code point so the result still differs.
    i = rng.randint(2, len(chars) - 1)
    chars[i] = chr(ord(chars[i]) + 1)
    return "".join(chars)


def generate_customers(n: int, fake: Faker, rng: random.Random) -> pd.DataFrame:
    """Generate ``n`` synthetic customers."""
    rows: list[dict[str, Any]] = []
    for i in range(1, n + 1):
        created = fake.date_time_between(start_date=EARLIEST_DATE, end_date=AS_OF_DATETIME).replace(
            tzinfo=UTC
        )
        rows.append(
            {
                "customer_id": f"CUST-{i:06d}",
                "name": fake.name(),
                "email": fake.email(),
                "phone": fake.phone_number(),
                "address": fake.address().replace("\n", ", "),
                "customer_type": rng.choice([M.CustomerType.INDIVIDUAL, M.CustomerType.BUSINESS]),
                "created_at": created,
                "status": rng.choices(["active", "closed"], weights=[0.95, 0.05])[0],
            }
        )
    return pd.DataFrame(rows)


def generate_accounts(
    n: int, customers: pd.DataFrame, fake: Faker, rng: random.Random
) -> pd.DataFrame:
    """Generate ``n`` synthetic accounts linked to customers."""
    cust_ids = customers["customer_id"].tolist()
    rows: list[dict[str, Any]] = []
    acct_types = [
        M.AccountType.CHECKING,
        M.AccountType.SAVINGS,
        M.AccountType.LOAN,
        M.AccountType.CREDIT_CARD,
    ]
    for i in range(1, n + 1):
        opened = fake.date_time_between(start_date=EARLIEST_DATE, end_date=AS_OF_DATETIME).replace(
            tzinfo=UTC
        )
        atype = rng.choice(acct_types)
        current = round(rng.uniform(50.0, 25_000.0), 2)
        available = (
            round(current - rng.uniform(0.0, 500.0), 2)
            if atype != M.AccountType.LOAN
            else round(rng.uniform(0.0, 5_000.0), 2)
        )
        rows.append(
            {
                "account_id": f"ACCT-{i:06d}",
                "customer_id": rng.choice(cust_ids),
                "account_type": atype,
                "account_number_masked": _mask_account_number(rng),
                "currency": "USD",
                "current_balance": current,
                "available_balance": available,
                "opened_at": opened,
                "status": rng.choices(["active", "closed"], weights=[0.95, 0.05])[0],
            }
        )
    return pd.DataFrame(rows)


def generate_transactions(
    n: int, accounts: pd.DataFrame, fake: Faker, rng: random.Random
) -> pd.DataFrame:
    """Generate ``n`` synthetic ledger transactions, each with a unique reference_id.

    The first ``BUCKET_EXACT`` reference_ids (REF-M-0001..) are the designated
    "matchable" set used to construct exact-match payments later.
    """
    acct_ids = accounts["account_id"].tolist()
    rows: list[dict[str, Any]] = []
    # Recent bulk: Aug 3..31 (max 29 days old vs the Sep 1 as-of date, so none
    # are stale by the 30-day threshold). Starts on Aug 3 (not Aug 1) so that
    # fuzzy-bucket payments shifted back by one day still land on Aug 2 (30
    # days, not stale) rather than Aug 1 (31 days, a boundary artifact).
    base_date = date(2026, 8, 3)
    for i in range(1, n + 1):
        posting = fake.date_between(start_date=base_date, end_date=date(2026, 8, 31))
        value = posting + timedelta(days=rng.randint(0, 2))
        ttype = rng.choice([M.TransactionType.DEBIT, M.TransactionType.CREDIT])
        amount = round(rng.uniform(10.0, 2_500.0), 2)
        # Reference id: first BUCKET_EXACT are the matchable set
        if i <= BUCKET_EXACT:
            ref = f"REF-M-{i:04d}"
        else:
            ref = f"REF-T-{i:06d}"
        rows.append(
            {
                "transaction_id": f"TXN-{i:06d}",
                "account_id": rng.choice(acct_ids),
                "transaction_type": ttype,
                "amount": amount,
                "currency": "USD",
                "posting_date": posting,
                "value_date": value,
                "description": fake.sentence(nb_words=4),
                "reference_id": ref,
                "created_at": datetime.combine(posting, datetime.min.time()).replace(tzinfo=UTC),
                "status": rng.choices(
                    [
                        M.TransactionStatus.POSTED,
                        M.TransactionStatus.PENDING,
                        M.TransactionStatus.REVERSED,
                    ],
                    weights=[0.9, 0.08, 0.02],
                )[0],
            }
        )

    # Inject a genuinely stale cohort at the tail (beyond the payment-matching
    # pools) so the timeliness check catches meaningful staleness. These records
    # are unmatched_ledger in reconciliation (their REF-T ids match no payment),
    # so the recon outcome counts are unaffected. No rng is consumed here — the
    # value_date offset is index-based, keeping the payment generator's rng
    # input identical to before this cohort existed.
    stale_count = min(round(n * STALE_FRACTION), max(0, n - _PAYMENT_POOL_SIZE))
    if stale_count > 0:
        for j, row in enumerate(rows[-stale_count:]):
            age = STALE_AGES_DAYS[j % len(STALE_AGES_DAYS)]
            posting = AS_OF_DATE - timedelta(days=age)
            row["posting_date"] = posting
            row["value_date"] = posting + timedelta(days=j % 3)
            row["created_at"] = datetime.combine(posting, datetime.min.time()).replace(tzinfo=UTC)

    return pd.DataFrame(rows)


def generate_payment_file(
    transactions: pd.DataFrame,
    fake: Faker,
    rng: random.Random,
    file_id: str = "FILE-0001",
    file_name: str = "sample_payment_file.csv",
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Generate one payment file + 1000 payment records across five buckets.

    Returns ``(payment_files_df, payment_records_df)``. Buckets that draw from
    ledger transactions are capped at the available count; the remainder is
    filled with unmatched payments so the file always contains 1000 records.
    """
    received_at = _utcnow()
    base_date = date(2026, 8, 31)

    n_txns = len(transactions)
    exact = min(BUCKET_EXACT, n_txns)
    fuzzy = min(BUCKET_FUZZY, max(0, n_txns - exact))
    amount_mm = min(BUCKET_AMOUNT_MISMATCH, max(0, n_txns - exact - fuzzy))
    unmatched = 1000 - exact - fuzzy - amount_mm - BUCKET_DUPLICATE
    if unmatched < 0:
        # If transactions are extremely few, trim duplicate bucket too
        unmatched = 0
        exact = min(exact, 1000 - BUCKET_DUPLICATE)

    records: list[dict[str, Any]] = []
    pid = 1

    def make_payment(
        ref: str,
        amount: float,
        pdate: date,
        counterparty: str,
        direction: str = M.PaymentDirection.INBOUND,
    ) -> dict[str, Any]:
        nonlocal pid
        rec = {
            "payment_id": f"PMT-{pid:06d}",
            "file_id": file_id,
            "reference_id": ref,
            "amount": round(amount, 2),
            "currency": "USD",
            "direction": direction,
            "counterparty": counterparty,
            "payment_date": pdate,
            "expected_posting_date": pdate + timedelta(days=rng.randint(0, 1)),
            "status": M.PaymentStatus.UNMATCHED,  # reconciler updates this
        }
        pid += 1
        return rec

    # Bucket 1: exact matches (exact) — ref + amount + date match a ledger txn
    exact_pool = transactions.iloc[:exact].copy()
    for _, txn in exact_pool.iterrows():
        records.append(
            make_payment(
                ref=txn["reference_id"],
                amount=float(txn["amount"]),
                pdate=txn["posting_date"],
                counterparty=fake.company(),
            )
        )

    # Bucket 2: unmatched payments (unmatched) — reference_ids not in ledger
    for _ in range(unmatched):
        records.append(
            make_payment(
                ref=f"REF-UNMATCHED-{rng.randint(100000, 999999):06d}",
                amount=round(rng.uniform(10.0, 2_500.0), 2),
                pdate=fake.date_between(start_date=date(2026, 8, 15), end_date=base_date),
                counterparty=fake.company(),
            )
        )

    # Bucket 3: fuzzy matches (fuzzy) — close reference, amount/date within tolerance
    fuzzy_pool = transactions.iloc[exact : exact + fuzzy]
    for _, txn in fuzzy_pool.iterrows():
        amt = float(txn["amount"]) + rng.choice([-0.5, 0.5, -0.3, 0.3])
        pd_offset = rng.choice([-1, 1])
        pdate = txn["posting_date"] + timedelta(days=pd_offset)
        records.append(
            make_payment(
                ref=_fuzz_reference(txn["reference_id"], rng),
                amount=round(max(0.01, amt), 2),
                pdate=pdate,
                counterparty=fake.company(),
            )
        )

    # Bucket 4: duplicate references (BUCKET_DUPLICATE) — a true duplicate of an
    # exact-match payment (same ref, same amount, same date) so it is clearly a
    # duplicate rather than an amount mismatch.
    exact_refs = exact_pool["reference_id"].tolist()
    rng.shuffle(exact_refs)
    dup_refs = exact_refs[:BUCKET_DUPLICATE] if len(exact_refs) >= BUCKET_DUPLICATE else exact_refs
    for ref in dup_refs:
        src = exact_pool[exact_pool["reference_id"] == ref].iloc[0]
        records.append(
            make_payment(
                ref=ref,
                amount=float(src["amount"]),
                pdate=src["posting_date"],
                counterparty=fake.company(),
            )
        )

    # Bucket 5: amount mismatches (amount_mm) — ref matches a txn exactly, amount
    # differs beyond tolerance, and the reference is not otherwise duplicated.
    mm_pool = transactions.iloc[exact + fuzzy : exact + fuzzy + amount_mm]
    for _, txn in mm_pool.iterrows():
        amt = float(txn["amount"]) + rng.choice([15.0, -12.0, 20.0, -8.0])  # beyond tolerance
        records.append(
            make_payment(
                ref=txn["reference_id"],
                amount=round(max(0.01, amt), 2),
                pdate=txn["posting_date"],
                counterparty=fake.company(),
            )
        )

    payment_records_df = pd.DataFrame(records)
    total_amount = float(payment_records_df["amount"].sum())
    payment_files_df = pd.DataFrame(
        [
            {
                "file_id": file_id,
                "file_name": file_name,
                "received_at": received_at,
                "record_count": len(payment_records_df),
                "total_amount": round(total_amount, 2),
                "source_system": "synthetic-generator",
                "status": "ingested",
            }
        ]
    )
    return payment_files_df, payment_records_df


def generate_all(
    seed: int = 42,
    volumes: dict[str, int] | None = None,
) -> dict[str, pd.DataFrame]:
    """Generate the full synthetic dataset deterministically.

    Returns a dict mapping table name -> DataFrame, for: customers, accounts,
    transactions, payment_files, payment_records.
    """
    v = {**M.DEFAULT_VOLUMES, **(volumes or {})}
    # NOTE: `Faker(seed=...)` constructor does NOT seed all of Faker's internal
    # RNGs (provider selection stays unseeded), so it is non-deterministic across
    # instances. `seed_instance` seeds every internal RNG, giving full determinism.
    fake = Faker()
    fake.seed_instance(seed)
    rng = random.Random(seed)
    # `rng` is a separate deterministic stream for bucket choices and amounts.

    customers = generate_customers(v["customers"], fake, rng)
    accounts = generate_accounts(v["accounts"], customers, fake, rng)
    transactions = generate_transactions(v["transactions"], accounts, fake, rng)
    payment_files, payment_records = generate_payment_file(transactions, fake, rng)
    # Appended last so it consumes fake/rng further along the shared
    # deterministic stream without shifting any earlier generator's output.
    pending_enrollments = generate_pending_enrollments(v["pending_enrollments"], fake, rng)

    return {
        M.Customer.TABLE: customers,
        M.Account.TABLE: accounts,
        M.Transaction.TABLE: transactions,
        M.PaymentFile.TABLE: payment_files,
        M.PaymentRecord.TABLE: payment_records,
        M.PendingEnrollment.TABLE: pending_enrollments,
    }


# --------------------------------------------------------------------------- #
# Bucket introspection (used by tests and the dashboard)
# --------------------------------------------------------------------------- #


def bucket_counts(payment_records: pd.DataFrame, transactions: pd.DataFrame) -> dict[str, int]:
    """Classify payment records into their design buckets for verification.

    This is a diagnostic helper, not part of the reconciler. It reconstructs
    bucket membership from the generated data so tests can assert each outcome
    class is present.
    """
    ledger_refs = set(transactions["reference_id"].tolist())

    # reference_id -> amount for exact ledger matches
    ledger_by_ref: dict[str, float] = {}
    for _, t in transactions.iterrows():
        ledger_by_ref.setdefault(t["reference_id"], float(t["amount"]))

    exact = 0
    unmatched = 0
    fuzzy = 0
    duplicate = 0
    amount_mismatch = 0

    seen_exact_refs: set[str] = set()
    for _, p in payment_records.iterrows():
        ref = p["reference_id"]
        amt = float(p["amount"])
        if ref not in ledger_refs:
            # not in ledger: fuzzy if close to some ledger ref, else unmatched
            if any(_lev_close(ref, lr) for lr in ledger_refs):
                fuzzy += 1
            else:
                unmatched += 1
        elif ref in seen_exact_refs:
            # a reference we already counted as exact -> this is a duplicate
            duplicate += 1
        elif abs(amt - ledger_by_ref[ref]) < 1.0:
            exact += 1
            seen_exact_refs.add(ref)
        else:
            amount_mismatch += 1

    return {
        "exact": exact,
        "unmatched": unmatched,
        "fuzzy": fuzzy,
        "duplicate": duplicate,
        "amount_mismatch": amount_mismatch,
    }


def _lev_close(a: str, b: str, threshold: float = 0.85) -> bool:
    """Quick Levenshtein-ratio closeness test (no external dep for diagnostics)."""
    try:
        from rapidfuzz import fuzz  # type: ignore[import]
    except Exception:  # pragma: no cover - rapidfuzz is a declared dep
        return False
    return fuzz.ratio(a, b) / 100.0 >= threshold
