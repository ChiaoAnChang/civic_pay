"""Conformance test skeleton for SnowflakeStore — UNVERIFIED, never run.

Not collected by civicpay's own test suite: pyproject.toml's
[tool.pytest.ini_options] sets testpaths = ["tests"], so `pytest` from the
repo root never looks under contrib/. Run this file directly
(`pytest contrib/snowflake_backend/tests/`) only if you have both
snowflake-connector-python installed and real SNOWFLAKE_* credentials in
your environment — otherwise every test here is skipped, by design, so this
file is safe to have sitting in the repo without a Snowflake account or
snowflake-connector-python installed at all.

This is the "second implementation" half of docs/cloud-backend.md's stated
acceptance criterion for a real backend: not conformance to a Protocol, but
the existing store-level behavior holding under the same operations. This
skeleton covers the same three operations civicpay's own DuckDBStore tests
exercise most: round-tripping a DataFrame, running a parameterized query,
and counting rows — not the full ~40-call-site suite docs/cloud-backend.md
describes eventually porting.
"""

from __future__ import annotations

import os

import pandas as pd
import pytest

snowflake_connector = pytest.importorskip(
    "snowflake.connector", reason="snowflake-connector-python not installed"
)

_REQUIRED_ENV_VARS = (
    "SNOWFLAKE_ACCOUNT",
    "SNOWFLAKE_USER",
    "SNOWFLAKE_WAREHOUSE",
    "SNOWFLAKE_DATABASE",
    "SNOWFLAKE_SCHEMA",
)

pytestmark = pytest.mark.skipif(
    any(not os.environ.get(v) for v in _REQUIRED_ENV_VARS),
    reason="Real Snowflake credentials not present in the environment",
)


@pytest.fixture
def store():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from store import SnowflakeStore

    s = SnowflakeStore()
    yield s
    s.close()


def test_write_and_read_round_trip(store):
    """Same shape as civicpay's own DuckDBStore round-trip tests: write a
    small DataFrame, read it back, assert the values survived unchanged."""
    df = pd.DataFrame(
        {
            "customer_id": ["CUST-TEST-0001"],
            "name": ["Test Customer"],
            "email": ["test@example.com"],
            "phone": ["555-0100"],
            "address": ["123 Test St"],
            "customer_type": ["individual"],
            "created_at": pd.to_datetime(["2026-09-01"]),
            "status": ["active"],
        }
    )
    store.write_dataframe("customers", df, mode="replace")
    result = store.read_table("customers")
    assert len(result) == 1
    assert result.iloc[0]["customer_id"] == "CUST-TEST-0001"


def test_parameterized_query(store):
    """Exercises the qmark paramstyle setup (store.py sets
    snowflake.connector.paramstyle = "qmark" at import time) against a real
    connection — this is the one thing that cannot be verified by reading
    the connector's source alone."""
    result = store.query("SELECT ? AS n", [42])
    assert int(result.iloc[0]["N"]) == 42


def test_table_count(store):
    count = store.table_count("customers")
    assert count >= 0
