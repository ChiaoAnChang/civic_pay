# Architecture

This document summarizes the CivicPay Open Framework architecture. See the
project charter (Part 2, Section 11) for the full specification, and
[ai-implementation-backlog.md](ai-implementation-backlog.md) for forward-looking
tickets not yet built.

## Tech stack

Python 3.11+ · DuckDB (embedded) · dbt (v0.2) · Streamlit · Faker · Typer · RapidFuzz · pytest · ruff.

## Data flow

```
[Enrollment form / CLI] -> validate -> [Dual-source gate] (v0.2)
                                          |            \
                                     agree|             \disagree
                                          v               v
                            [accepted_enrollments]   [Exception queue]
                                                              |
[Synthetic data generator] -> raw/ (CSV/Parquet)              |
        |                                                     |
        v                                                     |
[Ingest] -> DuckDB raw tables                                 |
        |                                                     |
        v                                                     |
[Reconciliation module] -> recon_results -> [Exception queue]  |
        |        \                                        |    |
        |         \-> audit events ---------------> [Audit-evidence log] (hash-chained)
        |                                                 ^
        +->[Data-quality module] -> dq_results -> [Exception queue]
                                                              |
                                                              v
                                              [Streamlit dashboard] <- reads raw/pipeline tables
        |
        v
[dbt marts (v0.2)] -> mart_recon_summary / mart_dq_summary / mart_exception_aging
                       (parallel SQL port of the dashboard's own Python extractors —
                        see docs/dbt.md — not a dependency of the dashboard or audit export)
```

## Storage

DuckDB is embedded (in-process, file-based). No cloud account or DBA required —
critical for adoption by under-resourced institutions. The reference
implementation runs entirely on synthetic data.

## Module status (v0.1)

| Module | Status | Docs |
| --- | --- | --- |
| Synthetic data + schemas | Implemented | — |
| DuckDB storage layer | Implemented | — |
| Payment reconciliation + audit-ledger core | Implemented | [reconciliation.md](reconciliation.md) |
| Data-quality monitoring | Implemented | [data-quality.md](data-quality.md) |
| Exception workflow (priority + SLA aging) | Implemented | [exceptions.md](exceptions.md) |
| Audit-evidence layer (verify + export) | Implemented | [audit.md](audit.md) |
| Streamlit dashboard | Implemented | — |
| CLI wiring + end-to-end (`run-all`) | Implemented | — |
| PyPI packaging + install CI | Implemented | — |
| dbt analytical marts | Implemented (v0.2) | [dbt.md](dbt.md) |
| Enrollment & validation module (Ticket 13) | Implemented (v0.2) | [ai-implementation-backlog.md](ai-implementation-backlog.md) |
| Cloud storage backend | Documented + unverified reference draft, not adopted | [cloud-backend.md](cloud-backend.md), [contrib/snowflake_backend/](../contrib/snowflake_backend/) |
