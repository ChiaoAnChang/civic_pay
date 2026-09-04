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
from civicpay.enrollment.validators import DEFAULT_RULES_PATH
from civicpay.exceptions.queue import ExceptionManager
from civicpay.quality.pipeline import DEFAULT_CONFIG_PATH as DEFAULT_DQ_CONFIG_PATH
from civicpay.quality.pipeline import run_dq
from civicpay.recon.pipeline import DEFAULT_CONFIG_PATH as DEFAULT_RECON_CONFIG_PATH
from civicpay.recon.pipeline import run_recon
from civicpay.storage.duckdb import DB_PATH_ENV_VAR, DEFAULT_DB_PATH, DuckDBStore

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
        M.PendingEnrollment.TABLE,
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
    config: str = typer.Option(str(DEFAULT_RECON_CONFIG_PATH), help="Reconciliation config YAML."),
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
    config: str = typer.Option(str(DEFAULT_DQ_CONFIG_PATH), help="DQ config YAML."),
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
        "basis",
        "priority_score",
    ):
        table.add_column(col, justify="right" if col in right_aligned else "left")
    for it in items:
        amount_display = "n/a" if it["amount_at_risk"] is None else f"{it['amount_at_risk']:.2f}"
        table.add_row(
            it["exception_id"],
            it["source"],
            it["priority"],
            it["status"],
            str(it["age_days"]),
            str(it["sla_days"]),
            amount_display,
            it["amount_basis"],
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


enroll_app = typer.Typer(
    name="enroll",
    help="Enrollment & validation (Ticket 13).",
    invoke_without_command=True,
)
app.add_typer(enroll_app)


@enroll_app.callback()
def enroll_main(
    ctx: typer.Context,
    db_path: str = typer.Option(None, help="DuckDB database path (form mode only)."),
) -> None:
    """Bare ``civicpay enroll`` launches the Streamlit form; ``civicpay
    enroll validate`` runs the batch CLI path instead."""
    if ctx.invoked_subcommand is None:
        from civicpay.enrollment.forms import run_streamlit_app

        try:
            code = run_streamlit_app(db_path=db_path)
        except FileNotFoundError as e:
            console.print(f"[red]{e}[/]")
            raise typer.Exit(code=1) from e
        raise typer.Exit(code=code)


@enroll_app.command("validate")
def enroll_validate(
    file: str = typer.Option(
        None, help="CSV of candidate enrollments to validate (default: seeded pending_enrollments)."
    ),
    rules: str = typer.Option(str(DEFAULT_RULES_PATH), help="Enrollment rules YAML."),
    db_path: str = typer.Option(str(DEFAULT_DB_PATH), help="DuckDB database path."),
    date: str = typer.Option(str(AS_OF_DATE), help="As-of date (YYYY-MM-DD)."),
) -> None:
    """Validate enrollment candidates and run the dual-source agreement gate.

    Reads either ``--file`` (an external CSV) or the seeded
    ``pending_enrollments`` table (default). Each candidate is validated,
    then — if valid — evaluated by two independent calculation paths;
    agreement writes to ``accepted_enrollments``, disagreement routes to the
    exception queue for human review (``civicpay exception list/resolve``).
    Already-processed records (by enrollment id) are skipped, not
    reprocessed, so re-running is safe.
    """
    from datetime import datetime

    from civicpay.data.synthetic import AS_OF_DATETIME
    from civicpay.enrollment.pipeline import run_enrollment_validate

    as_of = (
        datetime.fromisoformat(date).replace(tzinfo=AS_OF_DATETIME.tzinfo)
        if date
        else AS_OF_DATETIME
    )
    run_enrollment_validate(db_path=db_path, file_path=file, rules_path=rules, as_of=as_of)


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
                M.PendingEnrollment.TABLE,
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

        # 4. Enrollment validation + dual-source gate (Ticket 13). Each
        # enrollment is its own logical batch (ENR-{enrollment_id}), so unlike
        # recon/DQ there is no derived run_id batch to pre-flight-check here.
        from civicpay.enrollment.pipeline import EnrollmentPipeline

        enroll = EnrollmentPipeline(store=store, as_of=as_of).run()
        stages.append(
            (
                "enrollment",
                "ok",
                f"{enroll['accepted']} accepted, {enroll['mismatch']} mismatch, "
                f"{enroll['rejected']} rejected, {enroll['skipped']} skipped "
                f"(backlog seeded: {enroll['backlog_seeded']})",
            )
        )

        # 5. Exception queue.
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

        # 6. Audit verify.
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


dbt_app = typer.Typer(name="dbt", help="dbt analytical marts (v0.2).", no_args_is_help=True)
app.add_typer(dbt_app)

# dbt-core has no `python -m dbt` entry point (unlike streamlit — see
# civicpay.dashboard.app.run_streamlit_app), so the console script is
# located next to the current interpreter instead of hardcoding "dbt" and
# hoping the venv's Scripts/bin dir is on PATH.
_DBT_PROJECT_DIR = "dbt"


def _dbt_executable() -> str:
    import os
    import sys

    scripts_dir = Path(sys.executable).parent
    candidate = scripts_dir / ("dbt.exe" if os.name == "nt" else "dbt")
    return str(candidate) if candidate.exists() else "dbt"


def _run_dbt(command: str, db_path: str | None, date: str | None, select: str | None) -> int:
    import json
    import os
    import subprocess

    env = os.environ.copy()
    # dbt-core opens project files (dbt_project.yml, models/*.sql/.yml) with
    # the platform default text encoding, not UTF-8. On a non-English
    # Windows locale (e.g. cp950 Traditional Chinese) that crashes on the
    # first non-ASCII byte in this project's own source — force UTF-8 mode
    # rather than assuming every dbt file stays pure ASCII.
    env["PYTHONUTF8"] = "1"
    if db_path:
        env[DB_PATH_ENV_VAR] = db_path
    args = [
        _dbt_executable(),
        command,
        "--project-dir",
        _DBT_PROJECT_DIR,
        "--profiles-dir",
        _DBT_PROJECT_DIR,
    ]
    if date:
        args += ["--vars", json.dumps({"as_of_date": date})]
    if select:
        args += ["--select", select]
    result = subprocess.run(args, check=False, env=env)
    return result.returncode


@dbt_app.command("run")
def dbt_run(
    db_path: str = typer.Option(
        None, help="DuckDB database path (default: the standard seeded location)."
    ),
    date: str = typer.Option(
        str(AS_OF_DATE),
        help="As-of date (YYYY-MM-DD) for mart_exception_aging's age calculation.",
    ),
    select: str = typer.Option(None, help="dbt --select expression to build a subset of models."),
) -> None:
    """Build the dbt marts (mart_recon_summary, mart_dq_summary, mart_exception_aging).

    Requires the pipelines to have already run (``civicpay run-all`` or the
    individual ``recon``/``dq``/``exception`` commands) — dbt reads their
    output tables, it does not generate data.
    """
    code = _run_dbt("run", db_path=db_path, date=date, select=select)
    raise typer.Exit(code=code)


@dbt_app.command("test")
def dbt_test(
    db_path: str = typer.Option(
        None, help="DuckDB database path (default: the standard seeded location)."
    ),
    date: str = typer.Option(str(AS_OF_DATE), help="As-of date (YYYY-MM-DD), passed as a dbt var."),
    select: str = typer.Option(None, help="dbt --select expression to test a subset of models."),
) -> None:
    """Run dbt schema tests against the built marts."""
    code = _run_dbt("test", db_path=db_path, date=date, select=select)
    raise typer.Exit(code=code)


@app.command("dashboard")
def dashboard(
    db_path: str = typer.Option(
        None, help="DuckDB database path (default: the standard seeded location)."
    ),
) -> None:
    """Launch the Streamlit dashboard (recon, DQ, exceptions, audit, enrollment views)."""
    from civicpay.dashboard.app import run_streamlit_app

    try:
        code = run_streamlit_app(db_path=db_path)
    except FileNotFoundError as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(code=1) from e
    raise typer.Exit(code=code)


if __name__ == "__main__":
    app()
