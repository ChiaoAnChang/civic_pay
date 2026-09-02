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
    bucket_counts,
    generate_all,
    generate_payment_file,
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
