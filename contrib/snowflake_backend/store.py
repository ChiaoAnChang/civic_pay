"""Snowflake backend — UNVERIFIED reference implementation. Read
contrib/snowflake_backend/README.md before using or trusting this file.

Never executed against a real Snowflake account. Written as a faithful port
of civicpay.storage.duckdb.DuckDBStore's public surface, following
docs/cloud-backend.md's port-surface inventory — not as tested, production
code. Not imported by anything under civicpay/, not part of the packaged
wheel (pyproject.toml only packages "civicpay"), and does not add
snowflake-connector-python as a dependency of the shipped framework.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import snowflake.connector

# civicpay.storage.duckdb.SCHEMA_DDL is reused verbatim, not re-declared:
# every type it uses (DOUBLE, VARCHAR, TIMESTAMP, DATE, BOOLEAN, INTEGER,
# PRIMARY KEY, CREATE TABLE IF NOT EXISTS) is also valid Snowflake DDL —
# DOUBLE is a documented Snowflake alias for FLOAT, and unquoted TIMESTAMP
# defaults to TIMESTAMP_NTZ (no timezone), which happens to match this
# project's own naive-UTC convention. See docs/cloud-backend.md's
# comparison table for the type-by-type reasoning. PRIMARY KEY is accepted
# but NOT enforced by Snowflake (informational only) — see README.md's
# "known gap" section; this file does not compensate for that.
from civicpay.storage.duckdb import SCHEMA_DDL
from snowflake.connector.pandas_tools import write_pandas

# The connector's default paramstyle is "pyformat" (%s / %(name)s), not
# qmark — verified against the connector's source and issue tracker before
# writing this file (an earlier draft of docs/cloud-backend.md incorrectly
# assumed qmark was the default; corrected there and here). Every SQL string
# elsewhere in civicpay (exceptions/queue.py, enrollment/, audit/,
# dashboard/extractors.py) uses "?" placeholders, so qmark must be set
# explicitly, at module level, BEFORE the first connect() — it is
# process-global, not a per-connection/cursor setting. Doing this at import
# time (rather than inside __init__) mirrors the connector's own documented
# usage pattern; it means importing this module has the side effect of
# changing process-wide connector behavior, which is worth knowing if
# anything else in the same process also opens Snowflake connections.
snowflake.connector.paramstyle = "qmark"

# Mirrors civicpay.storage.duckdb.DB_PATH_ENV_VAR's role: the one place a
# caller configures where data lives. Unlike a filesystem path, a Snowflake
# connection has no single scalar value, so this is a set of env vars
# instead of one env var + a resolve_db_path()-style override argument.
_REQUIRED_ENV_VARS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA",
)


def resolve_connection_params() -> dict[str, str]:
    """Read connection parameters from SNOWFLAKE_* env vars.

    Authentication is deliberately left to whatever SNOWFLAKE_PASSWORD or
    SNOWFLAKE_PRIVATE_KEY_PATH the environment provides — this function only
    validates presence, not which auth method is in play, since that choice
    belongs to whoever actually runs this against a real account.
    """
    missing = [v for v in _REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing required Snowflake connection env var(s): {', '.join(missing)}. "
            "See contrib/snowflake_backend/README.md."
        )
    params = {
        "account": os.environ["SNOWFLAKE_ACCOUNT"],
        "user": os.environ["SNOWFLAKE_USER"],
        "warehouse": os.environ["SNOWFLAKE_WAREHOUSE"],
        "database": os.environ["SNOWFLAKE_DATABASE"],
        "schema": os.environ["SNOWFLAKE_SCHEMA"],
    }
    if os.environ.get("SNOWFLAKE_PASSWORD"):
        params["password"] = os.environ["SNOWFLAKE_PASSWORD"]
    if os.environ.get("SNOWFLAKE_ROLE"):
        params["role"] = os.environ["SNOWFLAKE_ROLE"]
    return params


def _naive_utc(df: pd.DataFrame) -> pd.DataFrame:
    """Same normalization as civicpay.storage.duckdb._naive_utc, reimplemented
    rather than assumed: this file has never been run against a live
    Snowflake account, so whether the connector's own tz handling matches
    DuckDB's was never verified either way. Normalizing explicitly here
    means the naive-UTC cross-backend invariant docs/cloud-backend.md
    describes holds by construction, not by hoping two different drivers
    happen to agree.
    """
    tz_cols = [c for c in df.columns if isinstance(df[c].dtype, pd.DatetimeTZDtype)]
    if not tz_cols:
        return df
    df = df.copy()
    for c in tz_cols:
        df[c] = df[c].dt.tz_convert("UTC").dt.tz_localize(None)
    return df


class SnowflakeStore:
    """UNVERIFIED. See contrib/snowflake_backend/README.md before use.

    Mirrors civicpay.storage.duckdb.DuckDBStore's public method surface
    exactly, so it is a drop-in replacement *shape*-wise. It is not a
    drop-in replacement in practice until the gaps listed in README.md are
    closed and this has been exercised against a real account.
    """

    def __init__(self, connection_params: dict[str, str] | None = None):
        self._connection_params = connection_params or resolve_connection_params()
        self._conn: snowflake.connector.SnowflakeConnection | None = None

    @property
    def conn(self) -> snowflake.connector.SnowflakeConnection:
        if self._conn is None:
            self._conn = snowflake.connector.connect(**self._connection_params)
        return self._conn

    def init_schema(self) -> None:
        """Create all framework tables if they do not exist.

        Identical in structure to DuckDBStore.init_schema() — same
        SCHEMA_DDL, same loop. The only reason this isn't literally
        `DuckDBStore.init_schema` reused via inheritance is that the two
        classes share no base class (deliberately: see docs/cloud-backend.md
        on not extracting a Protocol from a single implementation).
        """
        cursor = self.conn.cursor()
        try:
            for ddl in SCHEMA_DDL.values():
                cursor.execute(ddl)
        finally:
            cursor.close()

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- writes -------------------------------------------------------- #

    def write_dataframe(self, table: str, df: pd.DataFrame, mode: str = "append") -> None:
        """Write a DataFrame to a table.

        Bulk-load path is write_pandas(), the Snowflake-connector-native
        equivalent of DuckDBStore's zero-copy conn.register() +
        INSERT...SELECT — there is no direct analogue of DuckDB's
        register-a-DataFrame-as-a-virtual-table trick, so write_pandas is
        the closest faithful port, not an arbitrary substitute.

        quote_identifiers=False throughout: this project's SCHEMA_DDL uses
        unquoted, lowercase column names, which Snowflake folds to uppercase
        (transaction_id -> TRANSACTION_ID) the same way DuckDB
        case-insensitively matches unquoted identifiers. write_pandas's
        default (quote_identifiers=True) would instead emit quoted,
        DataFrame-column-cased identifiers ("transaction_id") — a different,
        non-matching column from Snowflake's perspective, not a
        case-insensitive alias of the DDL-created one.
        """
        df = _naive_utc(df)
        known_table = table in SCHEMA_DDL
        if known_table:
            cursor = self.conn.cursor()
            try:
                cursor.execute(SCHEMA_DDL[table])
            finally:
                cursor.close()
        overwrite = mode in ("replace", "overwrite")
        write_pandas(
            conn=self.conn,
            df=df,
            table_name=table,
            auto_create_table=not known_table,
            overwrite=overwrite,
            quote_identifiers=False,
        )

    def write_many(self, tables: dict[str, pd.DataFrame], mode: str = "append") -> None:
        """Write multiple tables at once (after ensuring schema exists)."""
        self.init_schema()
        for table, df in tables.items():
            self.write_dataframe(table, df, mode=mode)

    def execute(
        self, sql: str, params: list[Any] | None = None
    ) -> snowflake.connector.cursor.SnowflakeCursor:
        """Run a parameterized statement against Snowflake.

        Same tz-normalization DuckDBStore.execute() does for the same
        reason (see _naive_utc's docstring above) — not verified as
        necessary against a real Snowflake connection, applied defensively
        because the cost of skipping it and being wrong is silent data
        corruption, not a loud error.
        """
        params = [
            p.astimezone(UTC).replace(tzinfo=None) if isinstance(p, datetime) and p.tzinfo else p
            for p in (params or [])
        ]
        cursor = self.conn.cursor()
        cursor.execute(sql, params)
        return cursor

    # -- reads --------------------------------------------------------- #

    def query(self, sql: str, params: list[Any] | None = None) -> pd.DataFrame:
        """Run a SQL query and return the result as a DataFrame.

        fetch_pandas_all() requires the connector's [pandas] extra
        (pulls in pyarrow) — see requirements.txt.
        """
        cursor = self.execute(sql, params)
        try:
            return cursor.fetch_pandas_all()
        finally:
            cursor.close()

    def read_table(self, table: str, limit: int | None = None) -> pd.DataFrame:
        """Read a full table (optionally limited) into a DataFrame."""
        sql = f"SELECT * FROM {table}"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return self.query(sql)

    def table_count(self, table: str) -> int:
        """Return the row count of a table."""
        cursor = self.execute(f"SELECT COUNT(*) FROM {table}")
        try:
            row = cursor.fetchone()
        finally:
            cursor.close()
        return int(row[0]) if row else 0


def default_store() -> SnowflakeStore:
    """Return a store backed by SNOWFLAKE_* env vars. See README.md."""
    return SnowflakeStore()
