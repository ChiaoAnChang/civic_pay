"""CivicPay Open Framework command-line interface (Typer).

Only the ``seed`` command is implemented in v0.1. Subsequent tickets add
``ingest``, ``recon``, ``dq``, ``exception``, ``audit``, and ``dashboard``.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from civicpay.data import models as M
from civicpay.data.synthetic import generate_all
from civicpay.storage.duckdb import DEFAULT_DB_PATH, DuckDBStore

app = typer.Typer(
    name="civicpay",
    help="CivicPay Open Framework — financial-data-governance reference toolkit.",
    no_args_is_help=True,
)
console = Console()

# Default output directory for --files-only (raw synthetic CSVs)
DEFAULT_FILES_DIR = Path("data/synthetic")


@app.callback()
def _main() -> None:
    """CivicPay Open Framework CLI.

    A clean-room, open-source financial-data-governance reference toolkit.
    Run ``civicpay <command> --help`` for command details.
    """


@app.command()
def seed(
    customers: int = typer.Option(10_000, help="Number of synthetic customers to generate."),
    accounts: int = typer.Option(5_000, help="Number of synthetic accounts to generate."),
    transactions: int = typer.Option(
        50_000, help="Number of synthetic ledger transactions to generate."
    ),
    seed: int = typer.Option(42, help="Random seed for deterministic generation."),
    files_only: bool = typer.Option(
        False,
        help="Write raw CSV files without loading into DuckDB (for the public sample dataset).",
    ),
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), help="DuckDB database path."),
    out_dir: Path = typer.Option(DEFAULT_FILES_DIR, help="Output directory for --files-only."),
) -> None:
    """Generate deterministic synthetic data and load it into DuckDB (default).

    With ``--files-only``, raw CSV files are written without loading, producing
    the public sample dataset. The same ``--seed`` always yields identical data.
    """
    volumes = {
        "customers": customers,
        "accounts": accounts,
        "transactions": transactions,
    }
    console.print(
        f"[bold cyan]civicpay seed[/] — seed={seed}, volumes={volumes}, files_only={files_only}"
    )

    data = generate_all(seed=seed, volumes=volumes)

    if files_only:
        out_dir.mkdir(parents=True, exist_ok=True)
        for table, df in data.items():
            path = out_dir / f"{table}.csv"
            df.to_csv(path, index=False)
            console.print(f"  wrote {path} ({len(df):,} rows)")
        console.print("[green]Done.[/] Raw CSVs written to ", highlight=False)
        console.print(str(out_dir))
        return

    store = DuckDBStore(db_path)
    store.write_many(data, mode="replace")
    # Print a summary table
    table = Table(title="Seeded data")
    table.add_column("Table", style="bold")
    table.add_column("Rows", justify="right")
    for t in [
        M.Customer.TABLE,
        M.Account.TABLE,
        M.Transaction.TABLE,
        M.PaymentFile.TABLE,
        M.PaymentRecord.TABLE,
    ]:
        table.add_row(t, f"{store.table_count(t):,}")
    console.print(table)
    console.print(f"[green]Done.[/] DuckDB at {db_path}")
    store.close()


if __name__ == "__main__":
    app()
