"""CivicPay Open Framework command-line interface (Typer).

Implements ``seed`` (synthetic data) and ``recon run`` (payment reconciliation).
Subsequent tickets add ``ingest``, ``dq``, ``exception``, ``audit``, and
``dashboard``.
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from civicpay.audit.evidence import BatchIdAlreadyUsedError, verify_chain
from civicpay.data import models as M
from civicpay.data.synthetic import AS_OF_DATE, generate_all
from civicpay.exceptions.queue import ExceptionManager
from civicpay.quality.pipeline import run_dq
from civicpay.recon.pipeline import run_recon
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


recon_app = typer.Typer(name="recon", help="Payment reconciliation.", no_args_is_help=True)
app.add_typer(recon_app)


@recon_app.command("run")
def recon_run(
    file: str = typer.Option(None, help="Optional payment CSV to ingest before reconciling."),
    file_id: str = typer.Option(None, help="Reconcile only this payment file id."),
    date: str = typer.Option(str(AS_OF_DATE), help="As-of date (YYYY-MM-DD) for stale detection."),
    batch_id: str = typer.Option("BATCH-001", help="Reconciliation batch id."),
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), help="DuckDB database path."),
    config: str = typer.Option("config/recon.yml", help="Reconciliation config YAML."),
) -> None:
    """Run payment reconciliation against the ledger in DuckDB.

    Reads ``payment_records`` and ``transactions`` from DuckDB, matches every
    payment, writes ``reconciliation_results`` (including unmatched ledger
    rows), emits tamper-evident audit events, and prints a summary.
    """
    from datetime import datetime

    from civicpay.data.synthetic import AS_OF_DATETIME

    as_of = (
        datetime.fromisoformat(date).replace(tzinfo=AS_OF_DATETIME.tzinfo)
        if date
        else AS_OF_DATETIME
    )
    # Optionally ingest a payment CSV into payment_records first.
    if file:
        import pandas as pd

        store = DuckDBStore(db_path)
        store.init_schema()
        df = pd.read_csv(file)
        store.write_dataframe(M.PaymentRecord.TABLE, df, mode="replace")
        console.print(f"  ingested {len(df):,} payment records from {file}")
        store.close()
    try:
        summary = run_recon(
            db_path=db_path,
            config_path=config,
            batch_id=batch_id,
            file_id=file_id,
            as_of=as_of,
        )
    except BatchIdAlreadyUsedError as e:
        console.print(f"[red]Pre-flight check failed:[/] {e}")
        raise typer.Exit(code=1) from e
    console.print(f"[green]Done.[/] Reconciliation batch {summary['batch_id']}")


dq_app = typer.Typer(name="dq", help="Data-quality monitoring.", no_args_is_help=True)
app.add_typer(dq_app)


@dq_app.command("check")
def dq_check(
    dataset: str = typer.Option(
        None, help="Check only this dataset (default: all configured datasets)."
    ),
    batch_id: str = typer.Option("DQ-001", help="Data-quality batch id."),
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), help="DuckDB database path."),
    config: str = typer.Option("config/dq_checks.yml", help="DQ config YAML."),
    date: str = typer.Option(str(AS_OF_DATE), help="As-of date (YYYY-MM-DD) for staleness."),
) -> None:
    """Run data-quality checks across datasets in DuckDB.

    Reads each configured dataset from DuckDB, runs its checks, writes
    ``dq_results`` (replaced per run), routes per-record failures to the
    ``exception_queue``, emits tamper-evident audit events, and prints a
    per-dataset quality-score summary.
    """
    from datetime import datetime

    from civicpay.data.synthetic import AS_OF_DATETIME

    as_of = (
        datetime.fromisoformat(date).replace(tzinfo=AS_OF_DATETIME.tzinfo)
        if date
        else AS_OF_DATETIME
    )
    try:
        summary = run_dq(
            db_path=db_path,
            config_path=config,
            batch_id=batch_id,
            dataset=dataset,
            as_of=as_of,
        )
    except BatchIdAlreadyUsedError as e:
        console.print(f"[red]Pre-flight check failed:[/] {e}")
        raise typer.Exit(code=1) from e
    scores = summary["per_dataset_scores"]
    score_str = ", ".join(f"{k}={v:.2f}" for k, v in scores.items())
    console.print(f"[green]Done.[/] DQ batch {summary['batch_id']} — {score_str}")


exc_app = typer.Typer(name="exception", help="Exception workflow.", no_args_is_help=True)
app.add_typer(exc_app)


@exc_app.command("list")
def exception_list(
    status: str = typer.Option(None, help="Filter by status: open|in_progress|resolved."),
    sla_days: int = typer.Option(
        None,
        help="SLA threshold in days. Default: resolved per severity "
        "(high=3, medium=7, low=14); pass a value to override for every item.",
    ),
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), help="DuckDB database path."),
    date: str = typer.Option(str(AS_OF_DATE), help="As-of date (YYYY-MM-DD) for aging."),
) -> None:
    """List exceptions, sorted by computed priority (most urgent first)."""
    from datetime import datetime

    from civicpay.data.synthetic import AS_OF_DATETIME

    as_of = (
        datetime.fromisoformat(date).replace(tzinfo=AS_OF_DATETIME.tzinfo)
        if date
        else AS_OF_DATETIME
    )
    store = DuckDBStore(db_path)
    items = ExceptionManager(store=store, as_of=as_of).list(status=status, sla_days=sla_days)
    store.close()
    table = Table(title=f"Exception Queue — {len(items)} item(s)")
    right_aligned = ("age_days", "sla_days", "amount_at_risk", "priority_score")
    for col in (
        "exception_id",
        "source",
        "priority",
        "status",
        "age_days",
        "sla_days",
        "amount_at_risk",
        "priority_score",
    ):
        table.add_column(col, justify="right" if col in right_aligned else "left")
    for it in items:
        table.add_row(
            it["exception_id"],
            it["source"],
            it["priority"],
            it["status"],
            str(it["age_days"]),
            str(it["sla_days"]),
            f"{it['amount_at_risk']:.2f}",
            f"{it['priority_score']:.2f}",
        )
    console.print(table)


@exc_app.command("resolve")
def exception_resolve(
    id: str = typer.Option(..., help="Exception id to resolve."),
    root_cause: str = typer.Option(..., help="Root cause of the exception."),
    notes: str = typer.Option(None, help="Optional resolution notes."),
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), help="DuckDB database path."),
    date: str = typer.Option(str(AS_OF_DATE), help="As-of date (YYYY-MM-DD)."),
) -> None:
    """Resolve an exception, capture root cause, and emit an audit event."""
    from datetime import datetime

    from civicpay.data.synthetic import AS_OF_DATETIME

    as_of = (
        datetime.fromisoformat(date).replace(tzinfo=AS_OF_DATETIME.tzinfo)
        if date
        else AS_OF_DATETIME
    )
    store = DuckDBStore(db_path)
    try:
        result = ExceptionManager(store=store, as_of=as_of).resolve(
            exception_id=id, root_cause=root_cause, resolution_notes=notes
        )
    finally:
        store.close()
    console.print(
        f"[green]Resolved.[/] {result['exception_id']} — root cause: {result['root_cause']}"
    )


audit_app = typer.Typer(name="audit", help="Audit-evidence layer.", no_args_is_help=True)
app.add_typer(audit_app)


@audit_app.command("verify")
def audit_verify(
    batch: str = typer.Option(None, help="Recon batch id to verify (omitted = full chain)."),
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), help="DuckDB database path."),
) -> None:
    """Verify the audit-event hash chain is intact."""
    from civicpay.audit.evidence import verify_chain

    store = DuckDBStore(db_path)
    report = verify_chain(store, batch_id=batch)
    store.close()
    status = "[green]VERIFIED[/]" if report["verified"] else "[red]BROKEN[/]"
    console.print(f"Audit chain: {status} — {report['event_count']} event(s) checked.")
    if not report["verified"]:
        b = report["broken_event"]
        console.print(
            f"  [red]Broken at[/] {b['event_id']} — {b['reason']} (position {b['position']})"
        )


@audit_app.command("export")
def audit_export(
    batch: str = typer.Option(..., help="Batch id to export (recon or DQ)."),
    out: str = typer.Option("evidence.json", help="Output JSON path."),
    full: bool = typer.Option(
        False, help="Include full reconciliation_results rows (can be very large)."
    ),
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), help="DuckDB database path."),
) -> None:
    """Export the tamper-evident evidence package as structured JSON."""
    from civicpay.audit.evidence import UnknownBatchIdError, export_evidence

    store = DuckDBStore(db_path)
    try:
        pkg = export_evidence(store, batch_id=batch, out_path=out, full=full)
    except UnknownBatchIdError as e:
        console.print(f"[red]Export failed:[/] {e}")
        raise typer.Exit(code=1) from e
    finally:
        store.close()
    v = pkg["verification"]
    console.print(
        f"[green]Exported.[/] {out} — batch {batch}: "
        f"{v['event_count']} audit events, verification={'verified' if v['verified'] else 'broken'}, "
        f"recon summary={pkg['reconciliation_summary']}, "
        f"exception summary={pkg['exception_summary']}"
    )


@app.command("run-all")
def run_all(
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), help="DuckDB database path."),
    seed: bool = typer.Option(True, help="Seed synthetic data first (use --no-seed to skip)."),
    date: str = typer.Option(str(AS_OF_DATE), help="As-of date (YYYY-MM-DD)."),
    run_id: str = typer.Option(
        "RUNALL", help="Prefix for recon/DQ batch ids (use a fresh one per run)."
    ),
) -> None:
    """End-to-end: seed -> reconcile -> DQ -> exception list -> audit verify.

    Each stage uses a batch id derived from ``--run-id`` (e.g. ``RUNALL-RECON``,
    ``RUNALL-DQ``). Because the audit log and exception queue are append-only,
    re-running on the same database with the same ``--run-id`` collides on primary
    keys — pass a fresh ``--run-id`` (or a fresh ``--db-path``) for each run.
    """
    from datetime import datetime

    from civicpay.data.synthetic import AS_OF_DATETIME

    as_of = (
        datetime.fromisoformat(date).replace(tzinfo=AS_OF_DATETIME.tzinfo)
        if date
        else AS_OF_DATETIME
    )
    stages: list[tuple[str, str, str]] = []  # (stage, status, detail)

    store = DuckDBStore(db_path)
    try:
        # 0. Pre-flight: both derived batch ids must be unused, otherwise the
        # append-only audit log would collide on primary keys mid-run (after
        # seed/recon have already written). Check before any writes happen.
        from civicpay.audit.evidence import batch_id_in_use

        store.init_schema()
        for derived in (f"{run_id}-RECON", f"{run_id}-DQ"):
            if batch_id_in_use(store, derived):
                raise BatchIdAlreadyUsedError(derived)
        stages.append(("pre-flight", "ok", f"batch ids {run_id}-RECON/{run_id}-DQ unused"))

        # 1. Seed.
        if seed:
            data = generate_all(seed=42)
            for table in [
                M.Customer.TABLE,
                M.Account.TABLE,
                M.Transaction.TABLE,
                M.PaymentFile.TABLE,
                M.PaymentRecord.TABLE,
            ]:
                store.write_dataframe(table, data[table], mode="replace")
            stages.append(("seed", "ok", f"{sum(len(data[t]) for t in data)} rows"))
        else:
            stages.append(("seed", "skipped", "--no-seed"))

        # 2. Reconcile.
        recon = run_recon(db_path=db_path, batch_id=f"{run_id}-RECON", as_of=as_of)
        stages.append(
            (
                "reconcile",
                "ok",
                f"rate={recon['reconciliation_rate']}% matched={recon['matched_total']} exceptions={recon['exception_total']}",
            )
        )

        # 3. Data quality.
        dq = run_dq(db_path=db_path, batch_id=f"{run_id}-DQ", as_of=as_of)
        scores = dq["per_dataset_scores"]
        stages.append(
            (
                "data-quality",
                "ok",
                f"{dq['checks_passed']}/{dq['checks_run']} checks pass, "
                + ", ".join(f"{k}={v:.2f}" for k, v in scores.items()),
            )
        )

        # 4. Exception queue.
        items = ExceptionManager(store=store, as_of=as_of).list()
        if items:
            stages.append(
                (
                    "exception-list",
                    "ok",
                    f"{len(items)} item(s), top priority={items[0]['priority_score']:.2f}",
                )
            )
        else:
            stages.append(("exception-list", "ok", "0 items"))

        # 5. Audit verify.
        report = verify_chain(store)
        stages.append(
            (
                "audit-verify",
                "verified" if report["verified"] else "BROKEN",
                f"{report['event_count']} events",
            )
        )
    except BatchIdAlreadyUsedError as e:
        console.print(f"[red]Pre-flight check failed:[/] {e}")
        stages.append(("pre-flight", "BLOCKED", f"batch_id '{e.batch_id}' already in audit log"))
    finally:
        store.close()

    table = Table(title="End-to-End Run")
    table.add_column("stage", style="bold")
    table.add_column("status")
    table.add_column("detail")
    for stage, status, detail in stages:
        style = "green" if status in ("ok", "verified", "skipped") else "red"
        table.add_row(stage, f"[{style}]{status}[/]", detail)
    console.print(table)
    if stages[-1][1] != "verified":
        raise typer.Exit(code=1)


@app.command("dashboard")
def dashboard() -> None:
    """Launch the Streamlit dashboard (recon, DQ, exceptions, audit views)."""
    from civicpay.dashboard.app import run_streamlit_app

    raise SystemExit(run_streamlit_app())


if __name__ == "__main__":
    app()
