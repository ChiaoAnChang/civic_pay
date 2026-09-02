"""Tests for the DuckDB storage layer (Ticket 2)."""

from __future__ import annotations

import pandas as pd
from civicpay.data import models as M
from civicpay.storage.duckdb import SCHEMA_DDL


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
