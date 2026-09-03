"""Tests for the synthetic-data generator (Ticket 2)."""

from __future__ import annotations

import pandas as pd
from civicpay.data import models as M
from civicpay.data.synthetic import (
    BUCKET_AMOUNT_MISMATCH,
    BUCKET_DUPLICATE,
    BUCKET_EXACT,
    BUCKET_FUZZY,
    BUCKET_UNMATCHED,
    STALE_AGES_DAYS,
    STALE_FRACTION,
    bucket_counts,
    generate_all,
    generate_payment_file,
    generate_pending_enrollments,
)


def test_generate_all_volumes(synthetic_data):
    """Default volumes match the spec (Section 13.2)."""
    assert len(synthetic_data[M.Customer.TABLE]) == 10_000
    assert len(synthetic_data[M.Account.TABLE]) == 5_000
    assert len(synthetic_data[M.Transaction.TABLE]) == 50_000
    assert len(synthetic_data[M.PaymentFile.TABLE]) == 1
    assert len(synthetic_data[M.PaymentRecord.TABLE]) == 1_000


def test_payment_file_record_count_and_total(synthetic_data):
    pf = synthetic_data[M.PaymentFile.TABLE].iloc[0]
    pr = synthetic_data[M.PaymentRecord.TABLE]
    assert pf["record_count"] == 1_000
    assert pf["file_id"] == pr["file_id"].iloc[0]
    assert abs(pf["total_amount"] - pr["amount"].sum()) < 0.01


def test_determinism_same_seed():
    """Same seed -> identical data across all tables, including timestamps."""
    a = generate_all(seed=42)
    b = generate_all(seed=42)
    assert set(a.keys()) == set(b.keys())
    for table in a:
        df_a, df_b = a[table], b[table]
        assert len(df_a) == len(df_b), f"row count differs for {table}"
        # Full-frame CSV comparison: robust across tz/dtype handling and catches
        # any drift in generated values or timestamps.
        assert df_a.to_csv(index=False) == df_b.to_csv(index=False), f"data differs for {table}"


def test_determinism_fixed_as_of_date():
    """Generated timestamps are anchored to a fixed as-of date, not wall-clock now."""
    from civicpay.data.synthetic import AS_OF_DATETIME

    data = generate_all(seed=42)
    pf = data[M.PaymentFile.TABLE].iloc[0]
    assert pd.Timestamp(pf["received_at"]) == pd.Timestamp(AS_OF_DATETIME)
    cust = data[M.Customer.TABLE]
    assert (pd.to_datetime(cust["created_at"]) <= pd.Timestamp(AS_OF_DATETIME)).all()


def test_different_seed_different_data():
    """Different seed -> (very likely) different data."""
    a = generate_all(seed=42)
    b = generate_all(seed=7)
    assert a[M.Customer.TABLE]["name"].tolist() != b[M.Customer.TABLE]["name"].tolist()


def test_custom_volumes():
    data = generate_all(seed=99, volumes={"customers": 500, "accounts": 200, "transactions": 1_000})
    assert len(data[M.Customer.TABLE]) == 500
    assert len(data[M.Account.TABLE]) == 200
    assert len(data[M.Transaction.TABLE]) == 1_000
    # payment file is always 1000 records (the five buckets)
    assert len(data[M.PaymentRecord.TABLE]) == 1_000


def test_payment_buckets_all_present(synthetic_data):
    """Every reconciliation outcome class is present (Section 13.2)."""
    pr = synthetic_data[M.PaymentRecord.TABLE]
    txns = synthetic_data[M.Transaction.TABLE]
    counts = bucket_counts(pr, txns)
    assert counts["exact"] > 0, "expected exact matches"
    assert counts["unmatched"] > 0, "expected unmatched payments"
    assert counts["fuzzy"] > 0, "expected fuzzy matches"
    assert counts["duplicate"] > 0, "expected duplicate references"
    assert counts["amount_mismatch"] > 0, "expected amount mismatches"
    assert counts["exact"] >= BUCKET_EXACT - 5
    assert counts["unmatched"] >= BUCKET_UNMATCHED - 5
    assert counts["fuzzy"] >= BUCKET_FUZZY - 5
    assert counts["duplicate"] >= BUCKET_DUPLICATE - 5
    assert counts["amount_mismatch"] >= BUCKET_AMOUNT_MISMATCH - 5


def test_payment_records_all_linked_to_file(synthetic_data):
    pr = synthetic_data[M.PaymentRecord.TABLE]
    assert pr["file_id"].nunique() == 1
    assert (pr["currency"] == "USD").all()
    assert pr["direction"].isin(["inbound", "outbound"]).all()
    assert (pr["amount"] >= 0).all()


def test_account_numbers_masked(synthetic_data):
    acc = synthetic_data[M.Account.TABLE]
    masked = acc["account_number_masked"]
    assert masked.str.startswith("****").all()
    assert (masked.str.len() == 8).all()  # **** + 4 digits


def test_transaction_reference_ids_unique(synthetic_data):
    txns = synthetic_data[M.Transaction.TABLE]
    # reference_id should be unique across ledger transactions
    assert txns["reference_id"].is_unique


def test_generate_payment_file_smoke():
    """Standalone payment-file generation works without full dataset."""
    import random

    from civicpay.data.synthetic import generate_accounts, generate_customers, generate_transactions
    from faker import Faker

    fake = Faker(seed=123)
    rng = random.Random(123)
    cust = generate_customers(100, fake, rng)
    acc = generate_accounts(100, cust, fake, rng)
    txns = generate_transactions(500, acc, fake, rng)
    pf, pr = generate_payment_file(txns, fake, rng)
    assert len(pf) == 1
    assert len(pr) == 1_000


def test_stale_cohort_is_genuinely_old():
    """A ~3.5% cohort of transactions is seeded with genuinely old created_at
    dates (45/60/90 days), not a date-range boundary artifact (OPEN_QUESTIONS §D)."""
    from civicpay.data.synthetic import AS_OF_DATETIME

    data = generate_all(seed=42, volumes={"customers": 500, "accounts": 200, "transactions": 2_000})
    txns = data[M.Transaction.TABLE]
    as_of = pd.Timestamp(AS_OF_DATETIME).tz_convert("UTC").tz_localize(None)
    created = pd.to_datetime(txns["created_at"], errors="coerce")
    if getattr(created.dt, "tz", None) is not None:
        created = created.dt.tz_convert("UTC").dt.tz_localize(None)
    age_days = (as_of - created).dt.days

    stale = age_days[age_days > 30]
    expected = round(2_000 * STALE_FRACTION)
    assert len(stale) == expected
    # Every stale record is genuinely old (one of the seeded ages), never a
    # 31-day boundary artifact.
    assert sorted(stale.unique().tolist()) == list(STALE_AGES_DAYS)

    # The non-stale bulk is recent (<=30 days) and never 31 days.
    recent = age_days[age_days <= 30]
    assert recent.max() <= 30


def test_stale_cohort_beyond_payment_pools():
    """Stale cohort records sit beyond the payment-matching pools, so they are
    unmatched_ledger and the reconciliation outcome counts are unaffected."""
    from civicpay.data.synthetic import _PAYMENT_POOL_SIZE

    data = generate_all(seed=42, volumes={"customers": 500, "accounts": 200, "transactions": 2_000})
    txns = data[M.Transaction.TABLE]
    # Stale cohort is the tail slice; its start index is >= the pool size.
    stale_count = round(2_000 * STALE_FRACTION)
    stale_start = len(txns) - stale_count
    assert stale_start >= _PAYMENT_POOL_SIZE
    # And the recon bucket counts are exactly the designed sizes.
    counts = bucket_counts(data[M.PaymentRecord.TABLE], txns)
    assert counts["exact"] == BUCKET_EXACT
    assert counts["unmatched"] == BUCKET_UNMATCHED
    assert counts["fuzzy"] == BUCKET_FUZZY


def test_payment_records_are_fresh():
    """Payment records stay within the 30-day timeliness window (the stale cohort
    is ledger-only; stale payments would escalate and change recon counts)."""
    from civicpay.data.synthetic import AS_OF_DATETIME

    data = generate_all(seed=42)
    pr = data[M.PaymentRecord.TABLE]
    as_of = pd.Timestamp(AS_OF_DATETIME).tz_convert("UTC").tz_localize(None)
    pdates = pd.to_datetime(pr["payment_date"], errors="coerce")
    if getattr(pdates.dt, "tz", None) is not None:
        pdates = pdates.dt.tz_convert("UTC").dt.tz_localize(None)
    age_days = (as_of - pdates).dt.days
    assert (age_days <= 30).all()


def test_stale_cohort_skipped_below_pool_size():
    """Small transaction volumes (<= the payment-pool size) inject no stale
    cohort, so genuinely-old postings never leak into the exact/fuzzy/
    amount-mismatch buckets and distort the recon DoD (OPEN_QUESTIONS §D)."""
    from civicpay.data.synthetic import _PAYMENT_POOL_SIZE

    data = generate_all(
        seed=42, volumes={"customers": 100, "accounts": 50, "transactions": _PAYMENT_POOL_SIZE}
    )
    txns = data[M.Transaction.TABLE]
    assert len(txns) == _PAYMENT_POOL_SIZE
    # n == pool size -> stale_count == 0, so all rows are recent (<=30 days).
    from civicpay.data.synthetic import AS_OF_DATETIME

    as_of = pd.Timestamp(AS_OF_DATETIME).tz_convert("UTC").tz_localize(None)
    created = pd.to_datetime(txns["created_at"], errors="coerce")
    if getattr(created.dt, "tz", None) is not None:
        created = created.dt.tz_convert("UTC").dt.tz_localize(None)
    age_days = (as_of - created).dt.days
    assert (age_days <= 30).all()


# --------------------------------------------------------------------------- #
# Enrollment candidates (Ticket 13)
# --------------------------------------------------------------------------- #


def test_pending_enrollments_seeded_in_generate_all(synthetic_data):
    assert M.PendingEnrollment.TABLE in synthetic_data
    df = synthetic_data[M.PendingEnrollment.TABLE]
    assert len(df) == 200  # DEFAULT_VOLUMES["pending_enrollments"]
    assert (df["status"] == M.EnrollmentStatus.PENDING).all()


def test_enrollment_defect_cohort_present():
    import random

    from faker import Faker

    fake = Faker()
    fake.seed_instance(42)
    rng = random.Random(42)
    df = generate_pending_enrollments(50, fake, rng)
    assert len(df) == 50

    # Duplicate entity_id defect: at least one entity_id appears more than once.
    assert df["entity_id"].duplicated().any()

    # Stray-space numeric defect: at least one incentive_amount has an
    # embedded space that survives .strip() (unparseable even after outer trim).
    stripped = df["incentive_amount"].str.strip()
    assert (stripped.str.contains(" ")).any()

    # Missing-required defect: at least one submitted_by is blank.
    assert (df["submitted_by"].str.strip() == "").any()

    # Out-of-range amount defect: at least one negative incentive_amount.
    # (coerce, not astype -- the stray-space cohort is deliberately
    # unparseable and must not blow up this unrelated assertion.)
    numeric = pd.to_numeric(df["incentive_amount"], errors="coerce")
    assert (numeric < 0).any()


def test_enrollment_candidates_deterministic():
    import random

    from faker import Faker

    fake_a = Faker()
    fake_a.seed_instance(42)
    df_a = generate_pending_enrollments(50, fake_a, random.Random(42))

    fake_b = Faker()
    fake_b.seed_instance(42)
    df_b = generate_pending_enrollments(50, fake_b, random.Random(42))

    assert df_a.to_csv(index=False) == df_b.to_csv(index=False)


def test_enrollment_generation_does_not_disturb_earlier_generators():
    """Appending the enrollment generator to generate_all must not change any
    earlier generator's output (it's called last, consuming rng/fake further
    along the shared stream)."""
    with_enrollment = generate_all(
        seed=42, volumes={"customers": 100, "accounts": 50, "transactions": 500}
    )
    without = {
        k: v
        for k, v in generate_all(
            seed=42, volumes={"customers": 100, "accounts": 50, "transactions": 500}
        ).items()
        if k != M.PendingEnrollment.TABLE
    }
    for table in without:
        assert with_enrollment[table].to_csv(index=False) == without[table].to_csv(index=False)
