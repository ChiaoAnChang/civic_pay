"""DuckDB storage layer for CivicPay Open Framework.

Provides an embedded, no-infrastructure analytical database. Handles schema
creation, DataFrame writes, and reads. All DDL is original clean-room design;
table shapes come from :mod:`civicpay.data.models` and are not derived from
any employer system.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from civicpay.data import models as M

# Default location for the processed DuckDB database file.
DEFAULT_DB_PATH = Path("data/processed/civicpay.duckdb")

# DDL for every framework table. Types map to DuckDB SQL types.
SCHEMA_DDL: dict[str, str] = {
    M.Customer.TABLE: """
        CREATE TABLE IF NOT EXISTS customers (
            customer_id        VARCHAR PRIMARY KEY,
            name               VARCHAR,
            email              VARCHAR,
            phone              VARCHAR,
            address            VARCHAR,
            customer_type      VARCHAR,
            created_at         TIMESTAMP,
            status             VARCHAR
        )
    """,
    M.Account.TABLE: """
        CREATE TABLE IF NOT EXISTS accounts (
            account_id          VARCHAR PRIMARY KEY,
            customer_id        VARCHAR,
            account_type        VARCHAR,
            account_number_masked VARCHAR,
            currency            VARCHAR,
            current_balance     DOUBLE,
            available_balance   DOUBLE,
            opened_at           TIMESTAMP,
            status              VARCHAR
        )
    """,
    M.Transaction.TABLE: """
        CREATE TABLE IF NOT EXISTS transactions (
            transaction_id      VARCHAR PRIMARY KEY,
            account_id          VARCHAR,
            transaction_type    VARCHAR,
            amount              DOUBLE,
            currency            VARCHAR,
            posting_date        DATE,
            value_date           DATE,
            description         VARCHAR,
            reference_id        VARCHAR,
            created_at          TIMESTAMP,
            status              VARCHAR
        )
    """,
    M.PaymentFile.TABLE: """
        CREATE TABLE IF NOT EXISTS payment_files (
            file_id             VARCHAR PRIMARY KEY,
            file_name           VARCHAR,
            received_at         TIMESTAMP,
            record_count        INTEGER,
            total_amount        DOUBLE,
            source_system       VARCHAR,
            status              VARCHAR
        )
    """,
    M.PaymentRecord.TABLE: """
        CREATE TABLE IF NOT EXISTS payment_records (
            payment_id          VARCHAR PRIMARY KEY,
            file_id             VARCHAR,
            reference_id        VARCHAR,
            amount              DOUBLE,
            currency            VARCHAR,
            direction           VARCHAR,
            counterparty        VARCHAR,
            payment_date        DATE,
            expected_posting_date DATE,
            status              VARCHAR
        )
    """,
    M.ReconciliationResult.TABLE: """
        CREATE TABLE IF NOT EXISTS reconciliation_results (
            recon_id            VARCHAR PRIMARY KEY,
            batch_id            VARCHAR,
            payment_id          VARCHAR,
            ledger_transaction_id VARCHAR,
            match_status        VARCHAR,
            match_confidence    DOUBLE,
            match_method        VARCHAR,
            exception_reason    VARCHAR,
            reconciled_at       TIMESTAMP,
            reconciled_by       VARCHAR
        )
    """,
    M.DQResult.TABLE: """
        CREATE TABLE IF NOT EXISTS dq_results (
            dq_check_id         VARCHAR PRIMARY KEY,
            dataset_name        VARCHAR,
            check_type           VARCHAR,
            check_name          VARCHAR,
            passed              BOOLEAN,
            failing_records     INTEGER,
            quality_score        DOUBLE,
            checked_at          TIMESTAMP
        )
    """,
    M.ExceptionItem.TABLE: """
        CREATE TABLE IF NOT EXISTS exception_queue (
            exception_id        VARCHAR PRIMARY KEY,
            source              VARCHAR,
            reference_id        VARCHAR,
            priority            VARCHAR,
            assigned_to         VARCHAR,
            status              VARCHAR,
            created_at          TIMESTAMP,
            resolved_at         TIMESTAMP,
            resolution_notes    VARCHAR,
            root_cause          VARCHAR
        )
    """,
    M.AuditEvent.TABLE: """
        CREATE TABLE IF NOT EXISTS audit_event_log (
            event_id            VARCHAR PRIMARY KEY,
            timestamp           TIMESTAMP,
            event_type          VARCHAR,
            actor               VARCHAR,
            entity_type         VARCHAR,
            entity_id           VARCHAR,
            action              VARCHAR,
            previous_hash       VARCHAR,
            event_hash          VARCHAR
        )
    """,
}

# Tables that should be overwritten (not appended) during a fresh seed.
SEED_TABLES = [
    M.Customer.TABLE,
    M.Account.TABLE,
    M.Transaction.TABLE,
    M.PaymentFile.TABLE,
    M.PaymentRecord.TABLE,
]


class DuckDBStore:
    """Thin wrapper around a DuckDB connection.

    Parameters
    ----------
    db_path:
        Path to the DuckDB file. Use ``:memory:`` for an ephemeral in-memory
        database (handy for tests).
    """

    def __init__(self, db_path: str | os.PathLike[str] = DEFAULT_DB_PATH):
        self.db_path = str(db_path)
        self._conn: duckdb.DuckDBPyConnection | None = None

    # -- connection -------------------------------------------------------- #

    @property
    def conn(self) -> duckdb.DuckDBPyConnection:
        if self._conn is None:
            path = Path(self.db_path)
            if path != Path(":memory:") and str(path) != ":memory:":
                path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = duckdb.connect(self.db_path)
        return self._conn

    def init_schema(self) -> None:
        """Create all framework tables if they do not exist."""
        for ddl in SCHEMA_DDL.values():
            self.conn.execute(ddl)

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- writes ------------------------------------------------------------ #

    def write_dataframe(self, table: str, df: pd.DataFrame, mode: str = "append") -> None:
        """Write a DataFrame to a table.

        ``mode='replace'`` clears existing rows then inserts (preserving the
        canonical schema and primary keys for known framework tables; only
        unknown tables are dropped and recreated). ``mode='append'`` inserts
        rows. ``mode='overwrite'`` deletes existing rows then inserts.
        """
        if mode == "replace":
            if table in SCHEMA_DDL:
                # Preserve the canonical schema/PKs: ensure the table exists,
                # then truncate and insert.
                self.conn.execute(SCHEMA_DDL[table])
                self.conn.execute(f"DELETE FROM {table}")
            else:
                self.conn.execute(f"DROP TABLE IF EXISTS {table}")
                self.conn.register("_df_tmp", df)
                self.conn.execute(f"CREATE TABLE {table} AS SELECT * FROM _df_tmp")
                self.conn.unregister("_df_tmp")
                return
        if mode == "overwrite":
            self.conn.execute(f"DELETE FROM {table}")
        self.conn.register("_df_tmp", df)
        self.conn.execute(f"INSERT INTO {table} SELECT * FROM _df_tmp")
        self.conn.unregister("_df_tmp")

    def write_many(self, tables: dict[str, pd.DataFrame], mode: str = "append") -> None:
        """Write multiple tables at once (after ensuring schema exists)."""
        self.init_schema()
        for table, df in tables.items():
            self.write_dataframe(table, df, mode=mode)

    # -- reads ------------------------------------------------------------- #

    def query(self, sql: str, params: list[Any] | None = None) -> pd.DataFrame:
        """Run a SQL query and return the result as a DataFrame."""
        return self.conn.execute(sql, params or []).df()

    def read_table(self, table: str, limit: int | None = None) -> pd.DataFrame:
        """Read a full table (optionally limited) into a DataFrame."""
        sql = f"SELECT * FROM {table}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self.conn.execute(sql).df()

    def table_count(self, table: str) -> int:
        """Return the row count of a table."""
        row = self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
        return int(row[0]) if row else 0


def default_store() -> DuckDBStore:
    """Return a store backed by the default on-disk path."""
    return DuckDBStore(DEFAULT_DB_PATH)
