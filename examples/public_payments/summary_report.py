"""Quantified before/after summary for the public_payments demo (spec §4.3).

Produces a single structured summary a reviewer can read without touching
code: how many records were generated, how many errors were deliberately
injected, how many the engine caught, and a dollar figure — "$X of the $Y
synthetic dollars disbursed in this run were flagged as improper" — echoing
how GAO's own improper-payment reporting is framed (see README.md §2).

Every number here is computed from the actual generated data and detection
results, never typed by hand, so it stays true if the generator or detection
rules change.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
from detection import Flag
from rich.console import Console
from rich.table import Table

console = Console()


@dataclass(frozen=True)
class DemoSummary:
    claimants_total: int
    claimants_active: int
    claimants_exhausted: int
    claimants_denied: int
    payments_total: int
    total_disbursed: float
    flags_total: int
    flags_by_reason: dict[str, int]
    flagged_payments_total: int
    flagged_amount: float
    pct_dollars_flagged: float
    dq_checks_run: int
    dq_checks_passed: int
    dq_checks_failed: int
    dq_total_failing_records: int


def build_summary(
    roster: pd.DataFrame,
    payments: pd.DataFrame,
    flags: list[Flag],
    dq_results: list,
) -> DemoSummary:
    """Assemble the quantified summary from generated data + detection output."""
    issued = payments[payments["status"] == "issued"]
    total_disbursed = float(issued["amount"].sum())

    flags_by_reason: dict[str, int] = {}
    for f in flags:
        flags_by_reason[f.reason] = flags_by_reason.get(f.reason, 0) + 1

    flagged_payment_ids = {f.payment_id for f in flags}
    flagged_amount = float(issued[issued["payment_id"].isin(flagged_payment_ids)]["amount"].sum())
    pct_flagged = round(100.0 * flagged_amount / total_disbursed, 4) if total_disbursed else 0.0

    status_counts = roster["status"].value_counts()

    return DemoSummary(
        claimants_total=len(roster),
        claimants_active=int(status_counts.get("active", 0)),
        claimants_exhausted=int(status_counts.get("exhausted", 0)),
        claimants_denied=int(status_counts.get("denied", 0)),
        payments_total=len(payments),
        total_disbursed=round(total_disbursed, 2),
        flags_total=len(flags),
        flags_by_reason=flags_by_reason,
        flagged_payments_total=len(flagged_payment_ids),
        flagged_amount=round(flagged_amount, 2),
        pct_dollars_flagged=pct_flagged,
        dq_checks_run=len(dq_results),
        dq_checks_passed=sum(1 for r in dq_results if r.passed),
        dq_checks_failed=sum(1 for r in dq_results if not r.passed),
        dq_total_failing_records=sum(r.failing_records for r in dq_results),
    )


def print_summary(summary: DemoSummary) -> None:
    table = Table(title="Public Payments Demo — Run Summary (synthetic data)")
    table.add_column("Metric", style="bold")
    table.add_column("Value", justify="right")

    rows = [
        ("Claimants (total)", f"{summary.claimants_total:,}"),
        ("  active", f"{summary.claimants_active:,}"),
        ("  exhausted", f"{summary.claimants_exhausted:,}"),
        ("  denied (no payments)", f"{summary.claimants_denied:,}"),
        ("Payments issued (total)", f"{summary.payments_total:,}"),
        ("Total synthetic dollars disbursed", f"${summary.total_disbursed:,.2f}"),
        ("Flags raised (total)", f"{summary.flags_total:,}"),
    ]
    for reason, count in sorted(summary.flags_by_reason.items()):
        rows.append((f"  {reason}", f"{count:,}"))
    rows += [
        ("Distinct payments flagged", f"{summary.flagged_payments_total:,}"),
        (
            "Synthetic dollars flagged as improper",
            f"${summary.flagged_amount:,.2f}  ({summary.pct_dollars_flagged:.2f}% of disbursed)",
        ),
        (
            "DQ checks run / passed / failed",
            f"{summary.dq_checks_run} / {summary.dq_checks_passed} / {summary.dq_checks_failed}",
        ),
        ("DQ failing records (all checks)", f"{summary.dq_total_failing_records:,}"),
    ]
    for metric, value in rows:
        table.add_row(metric, value)
    console.print(table)
    console.print(
        "\n[bold]Read this like GAO's own improper-payment reporting[/bold]: "
        f"${summary.flagged_amount:,.2f} of the ${summary.total_disbursed:,.2f} synthetic dollars "
        "disbursed in this run were flagged as improper by the engine — a small, honest, "
        "fully-synthetic illustration of the same class of check GAO reports on at national scale. "
        "See README.md for the required scope disclaimer."
    )
