"""Public payments demo — one command: generate -> check -> detect -> summarize.

Proof-of-concept using entirely synthetic data. See README.md for the
required scope disclaimer before reading any number this script prints.

Usage::

    python examples/public_payments/run_demo.py
    python examples/public_payments/run_demo.py --seed 7 --claimants 1000
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import yaml
from civicpay.audit.evidence import verify_chain
from civicpay.audit.ledger import AuditLedger
from civicpay.data import models as M
from civicpay.quality.checks import CheckResult, check_accuracy_referential, run_check
from civicpay.storage.duckdb import DuckDBStore
from detection import run_detection
from generate_synthetic_data import AS_OF_DATE, DEFAULT_CLAIMANT_COUNT, generate_dataset
from rich.console import Console
from rich.table import Table
from summary_report import build_summary, print_summary

console = Console()

HERE = Path(__file__).parent
DEFAULT_DB_PATH = HERE / "data" / "public_payments.duckdb"
DEFAULT_CONFIG_PATH = HERE / "config" / "dq_checks.yml"
AS_OF_DATETIME = datetime.combine(AS_OF_DATE, datetime.min.time(), tzinfo=UTC)
BATCH_ID = "PP-DEMO-001"


def run_dq_checks(
    datasets: dict[str, pd.DataFrame], config_path: Path = DEFAULT_CONFIG_PATH
) -> list[CheckResult]:
    """Run the configured checks against in-memory DataFrames.

    Mirrors ``civicpay.quality.pipeline.QualityPipeline.run``'s dispatch (the
    referential rule needs pre-loaded reference values, so it's intercepted
    before the generic ``run_check`` dispatcher — see ``civicpay.quality.
    pipeline`` for the original), but reused here as a light wrapper around
    already-in-memory data instead of a DuckDB-backed pipeline run: this demo
    doesn't need the DuckDB-specific exception-queue/backlog machinery that
    pipeline also does, only the check functions themselves.
    """
    with open(config_path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}

    results: list[CheckResult] = []
    for ds_name, ds_cfg in cfg.get("datasets", {}).items():
        df = datasets[ds_name]
        df = df.copy()
        df.attrs["id_field"] = ds_cfg.get("id_field")
        for seq, check_cfg in enumerate(ds_cfg.get("checks", []), start=1):
            if check_cfg.get("rule") == "referential":
                ref_ds = check_cfg["reference_dataset"]
                ref_field = check_cfg["reference_field"]
                ref_values = datasets[ref_ds][ref_field]
                failing, ids = check_accuracy_referential(df, check_cfg["field"], ref_values)
                results.append(
                    CheckResult(
                        check_id=f"DQ-{ds_name}-{seq:04d}",
                        dataset_name=ds_name,
                        check_type=check_cfg["type"],
                        check_name=check_cfg.get("name", "referential"),
                        passed=failing == 0,
                        checked=len(df),
                        failing_records=failing,
                        quality_score=100.0 * (len(df) - failing) / len(df) if len(df) else 100.0,
                        checked_at=AS_OF_DATETIME,
                        failing_ids=ids,
                        severity=check_cfg.get("severity", "low"),
                    )
                )
            else:
                results.append(run_check(ds_name, df, check_cfg, AS_OF_DATETIME, seq))
    return results


def build_audit_events(dq_results: list[CheckResult], flags: list) -> list[dict]:
    events: list[dict] = []
    for r in dq_results:
        events.append(
            {
                "event_type": M.AuditEventType.DQ_CHECK,
                "entity_type": "dq_check",
                "entity_id": r.check_id,
                "action": r.check_name,
                "batch_id": BATCH_ID,
                "timestamp": AS_OF_DATETIME,
            }
        )
    for flag in flags:
        events.append(
            {
                "event_type": M.AuditEventType.EXCEPTION_OPEN,
                "entity_type": "public_payments_flag",
                "entity_id": flag.flag_id,
                "action": f"exception_open:{flag.reason}",
                "batch_id": BATCH_ID,
                "timestamp": AS_OF_DATETIME,
            }
        )
    return events


def print_top_flags(flags: list, n: int = 10) -> None:
    top = sorted(flags, key=lambda f: f.priority_score, reverse=True)[:n]
    table = Table(title=f"Top {len(top)} flags by priority score")
    table.add_column("Payment ID")
    table.add_column("Claimant")
    table.add_column("Reason")
    table.add_column("Amount", justify="right")
    table.add_column("Priority", justify="right")
    table.add_column("Detail")
    for f in top:
        table.add_row(
            f.payment_id,
            f.claimant_id,
            f.reason,
            f"${f.amount:,.2f}",
            f"{f.priority_score:.2f}",
            f.detail,
        )
    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--claimants", type=int, default=DEFAULT_CLAIMANT_COUNT)
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()

    console.rule("[bold]1. Generating synthetic data[/bold]")
    roster, payments, ground_truth = generate_dataset(seed=args.seed, claimant_count=args.claimants)
    console.print(f"Generated {len(roster):,} roster rows and {len(payments):,} payment rows.")

    console.rule("[bold]2. Loading into DuckDB[/bold]")
    db_path = Path(args.db_path)
    db_path.unlink(missing_ok=True)  # fresh run every time: data is fully regenerable
    store = DuckDBStore(db_path)
    try:
        store.init_schema()
        store.write_dataframe("claimant_roster", roster, mode="replace")
        store.write_dataframe("benefit_payments", payments, mode="replace")

        console.rule("[bold]3. Data-quality checks (reused civicpay.quality.checks)[/bold]")
        dq_results = run_dq_checks({"claimant_roster": roster, "benefit_payments": payments})
        for r in dq_results:
            status = "[green]pass[/green]" if r.passed else "[red]FAIL[/red]"
            console.print(
                f"  {status}  {r.dataset_name}: {r.check_name} ({r.failing_records} failing)"
            )

        console.rule("[bold]4. Error detection (new adapter code — see detection.py)[/bold]")
        flags, counts = run_detection(payments, roster)
        console.print(f"Flags raised: {counts}")

        console.rule("[bold]5. Audit trail (reused civicpay.audit)[/bold]")
        events = build_audit_events(dq_results, flags)
        AuditLedger(store=store, actor="public_payments_demo", as_of=AS_OF_DATETIME).append_many(
            events
        )
        verification = verify_chain(store, batch_id=BATCH_ID)
        console.print(
            f"Audit events written: {len(events):,}. Chain verified: {verification['verified']}."
        )

        console.rule("[bold]6. Summary[/bold]")
        summary = build_summary(roster, payments, flags, dq_results)
        print_summary(summary)
        print_top_flags(flags)
    finally:
        store.close()


if __name__ == "__main__":
    main()
