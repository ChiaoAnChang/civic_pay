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

1. **Payment Reconciliation** — match inbound payment files against ledger entries; classify matched / unmatched / exception.
2. **Data-Quality Monitoring** — completeness, accuracy, consistency, timeliness, anomaly checks; per-dataset quality score.
3. **Exception Workflow** — priority-ranked queue, SLA aging, resolution with root-cause capture.
4. **Audit-Evidence Layer** — append-only, hash-chained (tamper-evident) event log; exportable evidence packages.

## Tech stack

Python 3.11+ · DuckDB (embedded, no infrastructure) · dbt (v0.2) · Streamlit · Faker · Typer · RapidFuzz · pytest · ruff.

## Install (development)

```bash
pip install -e ".[dev]"
```

## Quickstart

```bash
# Generate deterministic synthetic data and load into DuckDB
civicpay seed --seed 42

# (Or write raw CSV/Parquet only, without loading)
civicpay seed --seed 42 --files-only
```

See [docs/architecture.md](docs/architecture.md) and [docs/methodology.md](docs/methodology.md) for design details.

## Clean-room provenance

This project is developed clean-room: from public regulatory requirements, published technical standards, and original design — never from any employer's proprietary systems, code, or data. See [PROVENANCE.md](PROVENANCE.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
