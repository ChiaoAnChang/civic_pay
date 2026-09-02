"""Tests for the DuckDB storage layer (Ticket 2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
from civicpay.data import models as M
from civicpay.storage.duckdb import SCHEMA_DDL, _naive_utc


def test_init_schema_creates_all_tables(in_memory_store):
    tables = [
        M.Customer.TABLE,
        M.Account.TABLE,
        M.Transaction.TABLE,
        M.PaymentFile.TABLE,
        M.PaymentRecord.TABLE,
        M.ReconciliationResult.TABLE,
        M.DQResult.TABLE,
        M.ExceptionItem.TABLE,
        M.AuditEvent.TABLE,
    ]
    for t in tables:
        assert in_memory_store.table_count(t) == 0


def test_write_and_read_dataframe(in_memory_store):
    df = pd.DataFrame(
        [
            {
                "customer_id": "CUST-000001",
                "name": "Alice",
                "email": "a@x.com",
                "phone": "555-0100",
                "address": "1 Main St",
                "customer_type": "individual",
                "created_at": pd.Timestamp("2026-01-01"),
                "status": "active",
            },
            {
                "customer_id": "CUST-000002",
                "name": "Bob",
                "email": "b@x.com",
                "phone": "555-0101",
                "address": "2 Main St",
                "customer_type": "business",
                "created_at": pd.Timestamp("2026-01-02"),
                "status": "active",
            },
        ]
    )
    in_memory_store.write_dataframe(M.Customer.TABLE, df, mode="replace")
    assert in_memory_store.table_count(M.Customer.TABLE) == 2

    out = in_memory_store.read_table(M.Customer.TABLE)
    assert len(out) == 2
    assert set(out["customer_id"]) == {"CUST-000001", "CUST-000002"}


def test_overwrite_mode(in_memory_store):
    df1 = pd.DataFrame(
        [
            {
                "customer_id": "CUST-001",
                "name": "A",
                "email": "a@x",
                "phone": "p",
                "address": "a",
                "customer_type": "individual",
                "created_at": pd.Timestamp("2026-01-01"),
                "status": "active",
            }
        ]
    )
    df2 = pd.DataFrame(
        [
            {
                "customer_id": "CUST-002",
                "name": "B",
                "email": "b@x",
                "phone": "p",
                "address": "b",
                "customer_type": "business",
                "created_at": pd.Timestamp("2026-01-02"),
                "status": "active",
            }
        ]
    )
    in_memory_store.write_dataframe(M.Customer.TABLE, df1, mode="replace")
    in_memory_store.write_dataframe(M.Customer.TABLE, df2, mode="overwrite")
    assert in_memory_store.table_count(M.Customer.TABLE) == 1
    out = in_memory_store.read_table(M.Customer.TABLE)
    assert out["customer_id"].iloc[0] == "CUST-002"


def test_append_mode(in_memory_store):
    df1 = pd.DataFrame(
        [
            {
                "customer_id": "CUST-001",
                "name": "A",
                "email": "a@x",
                "phone": "p",
                "address": "a",
                "customer_type": "individual",
                "created_at": pd.Timestamp("2026-01-01"),
                "status": "active",
            }
        ]
    )
    df2 = pd.DataFrame(
        [
            {
                "customer_id": "CUST-002",
                "name": "B",
                "email": "b@x",
                "phone": "p",
                "address": "b",
                "customer_type": "individual",
                "created_at": pd.Timestamp("2026-01-02"),
                "status": "active",
            }
        ]
    )
    in_memory_store.write_dataframe(M.Customer.TABLE, df1, mode="replace")
    in_memory_store.write_dataframe(M.Customer.TABLE, df2, mode="append")
    assert in_memory_store.table_count(M.Customer.TABLE) == 2


def test_query_returns_dataframe(in_memory_store):
    df = pd.DataFrame(
        [
            {
                "customer_id": "CUST-001",
                "name": "Alice",
                "email": "a@x",
                "phone": "p",
                "address": "a",
                "customer_type": "individual",
                "created_at": pd.Timestamp("2026-01-01"),
                "status": "active",
            }
        ]
    )
    in_memory_store.write_dataframe(M.Customer.TABLE, df, mode="replace")
    out = in_memory_store.query("SELECT name FROM customers WHERE status = ?", ["active"])
    assert out["name"].iloc[0] == "Alice"


def test_write_many(in_memory_store, synthetic_data):
    seed_tables = {
        M.Customer.TABLE: synthetic_data[M.Customer.TABLE],
        M.Account.TABLE: synthetic_data[M.Account.TABLE],
    }
    in_memory_store.write_many(seed_tables, mode="replace")
    assert in_memory_store.table_count(M.Customer.TABLE) == 10_000
    assert in_memory_store.table_count(M.Account.TABLE) == 5_000


def test_full_seed_round_trip(in_memory_store, synthetic_data):
    """The full synthetic dataset round-trips through DuckDB."""
    in_memory_store.write_many(synthetic_data, mode="replace")
    assert in_memory_store.table_count(M.Customer.TABLE) == 10_000
    assert in_memory_store.table_count(M.Transaction.TABLE) == 50_000
    assert in_memory_store.table_count(M.PaymentRecord.TABLE) == 1_000
    assert in_memory_store.table_count(M.PaymentFile.TABLE) == 1

    # payment file metadata is consistent with payment records
    pf = in_memory_store.read_table(M.PaymentFile.TABLE).iloc[0]
    pr = in_memory_store.read_table(M.PaymentRecord.TABLE)
    assert pf["record_count"] == len(pr)
    assert abs(pf["total_amount"] - pr["amount"].sum()) < 0.01


def test_naive_utc_converts_to_utc_not_just_strips_tz():
    """A tz-aware column is converted THROUGH UTC, not merely stripped.

    DuckDB TIMESTAMP columns are naive; binding a tz-aware value silently
    converts it via the local system timezone before dropping the tz (see
    ``_naive_utc``'s docstring), which corrupts the value on any machine not
    set to UTC. A +09:00 offset (distinct from UTC and from any single test
    machine's local zone) proves the fix does a real UTC conversion rather
    than coincidentally working because the input already happened to be UTC.
    """
    plus9 = timezone(timedelta(hours=9))
    aware = pd.Timestamp(datetime(2026, 9, 1, 9, 0, 0, tzinfo=plus9))  # 2026-09-01 00:00 UTC
    df = pd.DataFrame({"ts": pd.Series([aware])})
    assert isinstance(df["ts"].dtype, pd.DatetimeTZDtype)

    out = _naive_utc(df)
    assert out["ts"].dtype.kind == "M"  # naive datetime64, tz dropped
    assert out["ts"].iloc[0] == pd.Timestamp(2026, 9, 1, 0, 0, 0)


def test_naive_utc_leaves_naive_columns_untouched():
    df = pd.DataFrame({"ts": [pd.Timestamp(2026, 9, 1)]})
    out = _naive_utc(df)
    assert out["ts"].iloc[0] == pd.Timestamp(2026, 9, 1)


def test_write_dataframe_normalizes_tz_aware_column(in_memory_store):
    """End-to-end: a tz-aware ``created_at`` round-trips to the correct UTC
    wall-clock through the real DuckDB TIMESTAMP column, regardless of the
    host machine's local timezone."""
    plus9 = timezone(timedelta(hours=9))
    df = pd.DataFrame(
        [
            {
                "customer_id": "CUST-001",
                "name": "Alice",
                "email": "a@x",
                "phone": "p",
                "address": "a",
                "customer_type": "individual",
                "created_at": datetime(2026, 9, 1, 9, 0, 0, tzinfo=plus9),  # 00:00 UTC
                "status": "active",
            }
        ]
    )
    in_memory_store.write_dataframe(M.Customer.TABLE, df, mode="replace")
    out = in_memory_store.read_table(M.Customer.TABLE)
    assert pd.Timestamp(out["created_at"].iloc[0]) == pd.Timestamp(2026, 9, 1, 0, 0, 0)


def test_execute_normalizes_tz_aware_datetime_param(in_memory_store):
    """``store.execute`` normalizes a tz-aware datetime query parameter the
    same way ``write_dataframe`` normalizes a DataFrame column — the raw-SQL
    path used by e.g. ``ExceptionManager.resolve()`` must not silently shift
    through the local timezone either."""
    df = pd.DataFrame(
        [
            {
                "customer_id": "CUST-001",
                "name": "Alice",
                "email": "a@x",
                "phone": "p",
                "address": "a",
                "customer_type": "individual",
                "created_at": pd.Timestamp("2026-01-01"),
                "status": "active",
            }
        ]
    )
    in_memory_store.write_dataframe(M.Customer.TABLE, df, mode="replace")

    plus9 = timezone(timedelta(hours=9))
    aware_param = datetime(2026, 9, 1, 9, 0, 0, tzinfo=plus9)  # 00:00 UTC
    in_memory_store.execute(
        "UPDATE customers SET created_at = ? WHERE customer_id = ?",
        [aware_param, "CUST-001"],
    )
    out = in_memory_store.read_table(M.Customer.TABLE)
    assert pd.Timestamp(out["created_at"].iloc[0]) == pd.Timestamp(2026, 9, 1, 0, 0, 0)


def test_schema_ddl_covers_all_entities():
    """Every entity has DDL defined."""
    for model in [
        M.Customer,
        M.Account,
        M.Transaction,
        M.PaymentFile,
        M.PaymentRecord,
        M.ReconciliationResult,
        M.DQResult,
        M.ExceptionItem,
        M.AuditEvent,
    ]:
        assert model.TABLE in SCHEMA_DDL
