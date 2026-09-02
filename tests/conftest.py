"""Pytest fixtures shared across the test suite."""

from __future__ import annotations

import pytest
from civicpay.data.synthetic import generate_all


@pytest.fixture(scope="session")
def synthetic_data():
    """Deterministic full synthetic dataset (seed=42)."""
    return generate_all(seed=42)


@pytest.fixture()
def in_memory_store():
    """A fresh in-memory DuckDB store for each test."""
    from civicpay.storage.duckdb import DuckDBStore

    store = DuckDBStore(":memory:")
    store.init_schema()
    yield store
    store.close()
