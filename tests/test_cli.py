"""Tests for the CLI (Ticket 2 — seed command only)."""

from __future__ import annotations

import pandas as pd
from civicpay.cli import app
from civicpay.storage.duckdb import DuckDBStore
from typer.testing import CliRunner

runner = CliRunner()


def test_seed_help():
    result = runner.invoke(app, ["seed", "--help"])
    assert result.exit_code == 0
    assert "seed" in result.output.lower() or "Generate" in result.output


def test_seed_files_only(tmp_path):
    out = tmp_path / "synthetic"
    result = runner.invoke(
        app,
        [
            "seed",
            "--seed",
            "42",
            "--files-only",
            "--out-dir",
            str(out),
            "--customers",
            "500",
            "--accounts",
            "200",
            "--transactions",
            "1000",
        ],
    )
    assert result.exit_code == 0, result.output
    assert (out / "customers.csv").exists()
    df = pd.read_csv(out / "payment_records.csv")
    assert len(df) == 1_000
    assert (out / "transactions.csv").exists()


def test_seed_into_duckdb(tmp_path):
    db_path = tmp_path / "civicpay.duckdb"
    result = runner.invoke(
        app,
        [
            "seed",
            "--seed",
            "42",
            "--db-path",
            str(db_path),
            "--customers",
            "500",
            "--accounts",
            "200",
            "--transactions",
            "1000",
        ],
    )
    assert result.exit_code == 0, result.output
    store = DuckDBStore(str(db_path))
    assert store.table_count("customers") == 500
    assert store.table_count("transactions") == 1000
    assert store.table_count("payment_records") == 1000
    store.close()


def test_seed_is_deterministic(tmp_path):
    db1 = tmp_path / "a.duckdb"
    db2 = tmp_path / "b.duckdb"
    for db in (db1, db2):
        runner.invoke(
            app,
            [
                "seed",
                "--seed",
                "42",
                "--db-path",
                str(db),
                "--customers",
                "300",
                "--accounts",
                "100",
                "--transactions",
                "500",
            ],
        )
    s1 = DuckDBStore(str(db1))
    s2 = DuckDBStore(str(db2))
    t1 = s1.read_table("transactions")
    t2 = s2.read_table("transactions")
    assert t1["reference_id"].tolist() == t2["reference_id"].tolist()
    assert t1["amount"].tolist() == t2["amount"].tolist()
    s1.close()
    s2.close()
