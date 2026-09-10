# Public Payments — Phase 3 Concept Demo

> **Scope, up front:** this is a proof-of-concept that runs entirely on **fabricated, synthetic data**. It is not an engagement with, access to, or endorsement by any government agency, and it is not a claim of production-readiness. No real agency, state program, or recipient data is used anywhere in this directory, including in any sample output below. See [Scope & disclaimer](#scope--disclaimer) for the full statement — read it before reading the numbers below.

## The scale of the real problem

Per GAO's most recent report ([GAO-26-108694](https://www.gao.gov/), published April 27, 2026), federal agencies estimated **$186 billion** in improper payments across 64 programs at 15 agencies in FY2025 — up $24 billion from FY2024. 82% of that ($153 billion) was overpayments; 19 programs had improper-payment rates of 10% or higher (six above 25%); and the cumulative total since FY2003 is roughly **$3 trillion**. Improper payments are tracked and reported annually under the [Payment Integrity Information Act (PIIA)](https://www.paymentaccuracy.gov/) and OMB Circular A-123.

This demo does not solve that problem, touch any real system, or claim to. It illustrates, on a small and fully synthetic scenario, that the same reconciliation / data-quality / exception-workflow / audit-evidence methodology CivicPay Open Framework already applies to bank payments (see the [root README](../../README.md)) generalizes cleanly to the *class* of problem GAO reports on — duplicate payments, payments outside an approved eligibility window, and payments that don't match an approved benefit amount.

## Scenario

A fictional **"Sample State Unemployment Benefit Program — synthetic data"**: a claimant roster (who is approved, for how much, for how long) and a benefit payment ledger (what was actually paid, and when). Unemployment insurance is one of the more thoroughly documented program types in the public improper-payments literature, especially post-2020, and its structure — eligibility roster + periodic payment records + claim status — maps cleanly onto the fields this framework already understands.

**All data is fabricated** by [`generate_synthetic_data.py`](generate_synthetic_data.py) from a fixed seed. No real agency or state program is named or modeled; program codes (`SUI-STD`, `SUI-EXT`) and all claimant names, ids, and amounts are synthetic — same clean-room convention as the root project's [PROVENANCE.md](../../PROVENANCE.md) / Exhibit 41d declaration.

## What gets checked

1. **Duplicate payments** — the same claimant paid twice for the same pay period.
2. **Out-of-window payments** — a payment whose pay period falls outside the claimant's approved eligibility window.
3. **Amount mismatches** — a payment that doesn't match the claimant's approved weekly benefit, with no valid adjustment code (`BACKPAY_ADJ`, `OVERPAYMENT_OFFSET`, `COLA_ADJ`). A payment that differs from the approved amount but *does* carry one of those codes is a legitimate, explained adjustment and is deliberately left unflagged — the test suite specifically checks the engine doesn't just flag every deviation.

A healthy majority of the generated data is clean and valid on purpose — the point of the demo is to show the engine leaving good data alone, not just flagging everything.

## Engine reuse: what's config, what's new code

The spec for this demo asked a concrete question before any detection logic was written: is adapting the existing engine to this new schema purely a matter of configuration, or does it need schema-specific code? The answer, verified against the actual code (not assumed):

- **Config-only:** `civicpay.quality.checks` (completeness / accuracy / range / enum / referential-integrity checks) is genuinely generic — dataset name, table, and field are all just config. [`config/dq_checks.yml`](config/dq_checks.yml) points it at `claimant_roster` and `benefit_payments` with zero code changes; see it exercised in [`run_demo.py`](run_demo.py)'s `run_dq_checks`.
- **New adapter code, genuinely needed:** `civicpay.recon.matcher` is hard-wired to payment-vs-ledger matching *by reference id* — there's no equivalent concept here. And all three checks above are cross-row or cross-table comparisons (a payment against other payments for the same claimant/period, or a payment against its own claimant's roster row) that the DQ dispatch table has no entry for at all (its `referential` check only tests set membership, not a per-row range against a matching row in another table). [`detection.py`](detection.py) implements these fresh, following the same architectural rule as the existing matcher: pure functions of plain DataFrames, no I/O, no wall-clock time, independently unit-tested.
- **Reused as-is:** `civicpay.storage.duckdb.DuckDBStore` (new tables write via `write_dataframe(..., mode="replace")` with no DDL changes needed), `civicpay.audit.ledger.AuditLedger` / `civicpay.audit.evidence.verify_chain` (tamper-evident audit trail over the flagged cases), and `civicpay.exceptions.workflow.compute_priority_score` (severity/amount/age-weighted priority per flag).

## Running it

From a fresh clone of the root `civic_pay` repo:

```bash
pip install -e ".[dev]"
python examples/public_payments/run_demo.py
```

That single command generates the synthetic roster + payment ledger, loads it into a fresh local DuckDB file (`data/public_payments.duckdb`, gitignored and fully regenerable), runs the reused data-quality checks and the new detection rules, writes a tamper-evident audit trail, verifies the hash chain, and prints the summary below. Optional flags: `--seed`, `--claimants`, `--db-path`.

Run the test suite (including the catch-rate measurement) on its own:

```bash
pytest examples/public_payments/tests -v
```

_Sample output from `python examples/public_payments/run_demo.py` (seed=42, default claimant count = 3,000) — regenerate it yourself; nothing here is hand-typed. See [`tests/test_detection_rate.py`](tests/test_detection_rate.py) for the same catch-rate figures asserted as a test._

```text
1. Generating synthetic data
Generated 3,000 roster rows and 61,812 payment rows.

2. Loading into DuckDB

3. Data-quality checks (reused civicpay.quality.checks)
  pass  claimant_roster: No null claimant ids (0 failing)
  pass  claimant_roster: No null approved benefit amounts (0 failing)
  pass  claimant_roster: Approved benefit non-negative (0 failing)
  pass  claimant_roster: Valid program code (0 failing)
  pass  claimant_roster: Valid status (0 failing)
  pass  benefit_payments: No null claimant ids (0 failing)
  pass  benefit_payments: Amount non-negative (0 failing)
  pass  benefit_payments: Valid status (0 failing)
  pass  benefit_payments: Claimant exists on roster (0 failing)
  FAIL  benefit_payments: Amount z-score outliers (2 failing)   <- informational only, not routed

4. Error detection (new adapter code — see detection.py)
Flags raised: {'duplicate_payment': 240, 'out_of_window': 120, 'amount_mismatch': 120}

5. Audit trail (reused civicpay.audit)
Audit events written: 490. Chain verified: True.

6. Summary
             Public Payments Demo — Run Summary (synthetic data)
┌───────────────────────────────────────┬───────────────────────────────────┐
│ Metric                                 │                              Value │
├───────────────────────────────────────┼───────────────────────────────────┤
│ Claimants (total)                      │                              3,000 │
│   active                               │                              1,595 │
│   exhausted                            │                              1,245 │
│   denied (no payments)                 │                                160 │
│ Payments issued (total)                │                             61,812 │
│ Total synthetic dollars disbursed      │                     $29,157,322.00 │
│ Flags raised (total)                   │                                480 │
│   amount_mismatch                      │                                120 │
│   duplicate_payment                    │                                240 │
│   out_of_window                        │                                120 │
│ Distinct payments flagged              │                                480 │
│ Synthetic dollars flagged as improper  │  $233,609.00  (0.80% of disbursed) │
│ DQ checks run / passed / failed        │                         10 / 9 / 1 │
│ DQ failing records (all checks)        │                                  2 │
└───────────────────────────────────────┴───────────────────────────────────┘

Read this like GAO's own improper-payment reporting: $233,609.00 of the $29,157,322.00
synthetic dollars disbursed in this run were flagged as improper by the engine — a
small, honest, fully-synthetic illustration of the same class of check GAO reports
on at national scale.
```

**Catch rate** (from `tests/test_detection_rate.py`, run against this same seeded dataset): **480 of 480 injected errors caught (100%)**, with **zero false positives** — including zero flags among the 40 legitimate-adjustment decoy rows built specifically to test that the engine doesn't just flag every amount deviation. The one data-quality check that fails above (`Amount z-score outliers`) is the statistical anomaly check, which is informational by design (`route_failures: false` — see `config/dq_checks.yml`) and is not part of the three injected-error categories the catch rate above measures.

## Test suite

[`tests/test_demo.py`](tests/test_demo.py) asserts every injected error is caught by its category and that clean/legitimately-adjusted records are never flagged. [`tests/test_detection_rate.py`](tests/test_detection_rate.py) measures the actual catch rate against the generator's own ground truth (which rows it deliberately corrupted) — the number quoted above comes directly from that test, not from prose.

## Scope & disclaimer

This is a proof-of-concept built on synthetic data, developed independently as an extension of CivicPay Open Framework's existing open-source methodology. It:

- Does **not** engage with, access, or receive data from any real government agency, state, or benefit program.
- Does **not** claim to solve, or make a dent in, the $186B/$3T figures cited above — those numbers are cited only to frame the *class* of problem this technique targets, at the scale GAO itself reports it.
- Is **not** production-ready software, and carries the same "pre-alpha / research-and-teaching-reference" status as the rest of [CivicPay Open Framework](../../README.md).
- Uses **only fabricated data** — see [`generate_synthetic_data.py`](generate_synthetic_data.py) for exactly how it's generated, and the root project's [PROVENANCE.md](../../PROVENANCE.md) for the clean-room declaration this demo follows.

## License

Apache-2.0, same as the rest of this repository. See [LICENSE](../../LICENSE).
