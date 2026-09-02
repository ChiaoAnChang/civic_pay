# CivicPay Open Framework

A clean-room, open-source reference implementation of financial-data-governance methodology — payment reconciliation, data-quality monitoring, exception workflow, and a tamper-evident audit-evidence layer — for U.S. community banks, credit unions, and small fintechs.

> **Status:** Pre-alpha (v0.1). Research/teaching reference, not production software.

## Why this exists

Financial-sector data-integrity failures — unreconciled payments, data-quality defects, missing audit trails — can cause real consumer harm and have been the subject of significant regulatory enforcement actions across the U.S. financial sector. The institutions most exposed to these failure modes (community banks, credit unions, small fintechs) are often the least able to afford enterprise tooling. CivicPay Open Framework makes a practitioner-informed reconciliation / data-quality / audit-evidence methodology freely adoptable on synthetic data, using only open-source components.

## What it is (and is not)

CivicPay Open Framework is a **governance toolset**, not a payment processor or core banking system. It runs entirely on **synthetic data**. See [DISCLAIMER.md](DISCLAIMER.md) and [SECURITY.md](SECURITY.md).

- Not a payment processor, money transmitter, or core banking system.
- Not Reg E / BSA / AML compliance advice.
- Not production-ready; not for real PII without an institution-specific security and compliance review.

## Architecture (four layers)

```text
┌─────────────────────────────────────────────────────────────────────┐
│                         Streamlit Dashboard                        │
│        (recon summary · DQ scores · exceptions · audit log)          │
├──────────────┬────────────────┬──────────────────┬───────────────────┤
│  1. Recon    │   2. Data-      │  3. Exception    │  4. Audit-        │
│  matching    │   quality       │  workflow        │  evidence         │
│  + outcome   │   checks +      │  priority + SLA  │  hash-chained     │
│  counts      │   quality score │  aging + resolve │  log + export      │
├──────────────┴────────────────┴──────────────────┴───────────────────┤
│                    DuckDB (embedded, synthetic data)                │
└─────────────────────────────────────────────────────────────────────┘
        determinism (seed=)              tamper-evidence (SHA-256 chain)
```

1. **Payment Reconciliation** — match inbound payment files against ledger entries; classify matched / unmatched / exception.
2. **Data-Quality Monitoring** — completeness, accuracy, consistency, timeliness, anomaly checks; per-dataset quality score.
3. **Exception Workflow** — priority-ranked queue, SLA aging, resolution with root-cause capture.
4. **Audit-Evidence Layer** — append-only, hash-chained (tamper-evident) event log; exportable evidence packages.

## Tech stack

Python 3.11+ · DuckDB (embedded, no infrastructure) · dbt (v0.2) · Streamlit · Faker · Typer · RapidFuzz · pytest · ruff.

## Install

```bash
pip install -e ".[dev]"
```

## Quickstart

One command runs the full pipeline end-to-end (seed → reconcile → DQ → exceptions → audit verify):

```bash
civicpay run-all
```

Or step by step:

```bash
# 1. Generate deterministic synthetic data and load into DuckDB
civicpay seed --seed 42

# (Or write raw CSV/Parquet only, without loading)
civicpay seed --seed 42 --files-only

# 2. Run payment reconciliation against the ledger
civicpay recon run --batch-id BATCH-001

# 3. Run data-quality checks across all datasets
civicpay dq check --batch-id DQ-001
# (single dataset)  civicpay dq check --dataset transactions

# 4. View the exception queue (sorted by computed priority, with SLA aging)
civicpay exception list --status open
# Resolve one, capturing root cause (emits an audit event)
civicpay exception resolve --id EXC-DQ-001-000001 --root-cause "stale upstream feed"

# 5. Verify the audit hash chain is intact (detects tampering)
civicpay audit verify --batch BATCH-001
# Export a tamper-evident evidence package as JSON
civicpay audit export --batch BATCH-001 --out evidence.json

# Launch the dashboard
civicpay dashboard
```

### Data quality configuration

DQ checks are configured in [`config/dq_checks.yml`](config/dq_checks.yml) — per-dataset check definitions (type, rule, params), type weights, and the exception-routing cap. See [docs/data-quality.md](docs/data-quality.md) for the full check catalogue.

### Determinism & re-runs

Data generation and all pipelines are deterministic given a fixed `--seed`. Each run should use a **fresh `--batch-id`** (the audit log and exception queue are append-only; re-running the same batch id on the same DB collides on primary keys).

See [docs/reconciliation.md](docs/reconciliation.md) for the reconciliation module's full technical documentation (algorithm, data model, configuration, API, test results, and operational behavior). See [docs/data-quality.md](docs/data-quality.md) for the DQ module, [docs/architecture.md](docs/architecture.md) for the module status table, and [docs/methodology.md](docs/methodology.md) for the overall design.

## Clean-room provenance

This project is developed clean-room: from public regulatory requirements, published technical standards, and original design — never from any employer's proprietary systems, code, or data. See [PROVENANCE.md](PROVENANCE.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
