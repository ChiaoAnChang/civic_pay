# Changelog

All notable changes to the CivicPay Open Framework are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- **Audit hash chain could fork when two batches shared a timestamp.**
  `AuditLedger._initialize_chain` resumed the chain via `ORDER BY timestamp
  DESC, event_id DESC LIMIT 1` — a heuristic that assumed batch-id strings
  sort in the order batches are actually appended. They don't (e.g.
  `"EVT-R1-..."` sorts after `"EVT-D1-..."` regardless of which was written
  more recently), and every pipeline here shares the same deterministic
  `as_of` timestamp, so ties are common. A new `AuditLedger` instance
  initializing after two prior batches already coexist could resume from a
  stale, non-tip event and fork the chain. Found while implementing the
  OPEN_QUESTIONS §G backlog cohort (which introduces a second `AuditLedger`
  instance mid-run): reproduced as `civicpay run-all` failing with
  `audit-verify BROKEN` / `chain_fork` on a fresh database. Fixed at the
  root (`civicpay/audit/ledger.py`): `_initialize_chain` now finds the
  chain's true tip structurally — the one `event_hash` never referenced as
  anyone's `previous_hash` — instead of guessing from timestamp/event_id
  ordering.
- **Audit hash chain falsely reported as broken on any non-UTC machine.**
  `AS_OF_DATETIME` (and other timestamps) are timezone-aware (`tzinfo=UTC`),
  but every DuckDB `TIMESTAMP` column is timezone-naive. Writing a tz-aware
  pandas/Python datetime into one silently converts it through the *local
  system timezone* before dropping the tz (verified empirically: a UTC value
  written on a UTC-5 machine came back 5 hours earlier) — so the value
  persisted to `audit_event_log` differed from the value hashed at append
  time, and `audit verify` / `civicpay run-all` reported the chain as
  tampered with zero actual tampering, on any machine not set to UTC. Fixed
  at the storage layer: `civicpay/storage/duckdb.py` now normalizes
  timezone-aware datetimes to naive UTC before every write
  (`DuckDBStore.write_dataframe` via a new `_naive_utc` helper) and before
  every raw-SQL parameter bind (new `DuckDBStore.execute()`, now used by
  `ExceptionManager.resolve/assign` and `AuditLedger._initialize_chain`
  instead of reaching into `store.conn.execute()` directly). Found while
  setting up a fresh local dev environment (Windows, UTC-5) to verify
  OPEN_QUESTIONS §E — 10 of 11 test failures in that environment traced back
  to this one root cause.

### Added
- **Deterministic exception backlog cohort** (OPEN_QUESTIONS §G): on a
  full-config `dq check`/`run-all` against a fresh database, `QualityPipeline`
  now also seeds a small, fixed cohort of already-aged exceptions (real
  failing record ids, `created_at` backdated 2/5/12/21/45 days,
  batch id `DQ-000-PRIOR`) so SLA escalation (§F) is visible in the CLI table
  and dashboard out of the box, instead of every exception showing
  `age_days = 0` on a static demo DB. Genuine DQ detections still use
  `created_at = as_of` unchanged (this must agree with the paired
  `exception_open` audit event). Seeded once — idempotent on re-run via the
  same `batch_id_in_use` check pipelines already use for their own batch ids.
  `summary["backlog_seeded"]` reports the count (0 after the first run).
  `exception list` / the dashboard also gained a per-item `sla_days` column.

### Changed
- **Per-severity SLA windows for exception aging** (OPEN_QUESTIONS §F):
  `SLA_DAYS_BY_SEVERITY = {"high": 3, "medium": 7, "low": 14}` replaces the
  old flat 7-day default for every exception regardless of severity — a
  high-severity item now starts escalating sooner than a low-severity one,
  matching real SLA/triage practice. `--sla-days` (CLI) /
  `sla_days=` (`ExceptionManager.list`, `dashboard.exception_queue`) still
  works as an explicit override (applies the same window to every item);
  the resolved window is now also exposed per-item as a new `sla_days`
  field/column (CLI table, dashboard). Per-check-type SLA config remains
  deferred to v0.2.
- **Anomaly checks excluded from the dataset quality score** (OPEN_QUESTIONS
  §E): `type_weights.anomaly` is now `0.0` (was `0.5`). A z-score/IQR anomaly
  check flags a small tail of records by construction, so its own score sits
  near 100 regardless of data quality elsewhere — including it in the average
  inflated the dataset score rather than protecting against outliers
  "dominating" it, and it was already excluded from exception routing
  (`route_failures: false`), so the scoring weight was the last inconsistency.
  The anomaly failure rate is now reported separately: `QualityPipeline.run()`
  returns it as `per_dataset_anomaly_rate` in the summary (also printed in the
  CLI table), and the dashboard's `dq_dataset_scores()` — previously an
  unweighted flat mean that ignored `type_weights` entirely — now uses the
  same type-weighted formula as the CLI plus an `anomaly_rate` column.

### Added
- **Re-run idempotency pre-flight guard** (OPEN_QUESTIONS §C): both pipelines
  (`QualityPipeline`, `ReconciliationPipeline`) now check whether a `batch_id`
  is already present in the append-only audit log before any writes, and fail
  fast with `BatchIdAlreadyUsedError` (a clear, actionable message) instead of
  a raw DuckDB primary-key constraint error mid-run. `civicpay run-all` checks
  both derived batch ids (`{run_id}-RECON`, `{run_id}-DQ`) up front.
- **Genuinely-stale transaction cohort** (OPEN_QUESTIONS §D): synthetic
  transactions now include a ~3.5% cohort with `created_at` dates 45/60/90 days
  before the as-of date, so the timeliness DQ check catches meaningful staleness
  instead of an Aug-1 boundary artifact (records that were only 31 days old).
  Payment records stay fresh; reconciliation DoD counts are unchanged.
- Trusted Publishing (OIDC) for PyPI: releases are published via GitHub Actions
  identity, no shared API token secret required.
- Version is now derived from git tags via `hatch-vcs` (`dynamic = ["version"]`).

## [0.1.0] — 2026-09-01

### Added
- **Payment Reconciliation** (Ticket 1–3): deterministic synthetic-data generation
  (Faker, seed=42), DuckDB storage layer, audit-ledger hash-chained append-only
  log, fuzzy + exact matching with RapidFuzz, configurable pipeline.
- **Data-Quality Monitoring** (Ticket 4, spec T5): nine typed checks
  (completeness, accuracy, consistency, timeliness, anomaly), per-dataset
  quality score (§12), exception routing, `config/dq_checks.yml`.
- **Exception Workflow** (Ticket 5, spec T6): priority formula
  (`severity × amount_at_risk × age_factor`), SLA aging, list/resolve CLI,
  root-cause capture with audit events.
- **Audit-Evidence Layer** (Ticket 6, spec T7): `verify_chain` (hash-chain
  traversal, detects tampering/deletion/forks/orphans), `export_evidence`
  (JSON evidence package), CLI.
- **Streamlit Dashboard** (Ticket 7, spec T8): four views — reconciliation
  summary, DQ scores, exception queue with aging, recent audit events.
- **CLI Wiring** (Ticket 8, spec T10): `civicpay run-all` end-to-end
  (seed → reconcile → DQ → exception list → audit verify), `--run-id` for fresh
  batch ids, PyPI publish script, install CI (Py 3.11 + 3.12).
- **Documentation** (Ticket 9, spec T11): README with four-layer architecture
  diagram and full CLI reference, `docs/data-quality.md`,
  `docs/reconciliation.md`, updated `docs/architecture.md` status table,
  `CONTRIBUTING.md` ticket roadmap.

### Fixed
- `verify_chain` rewrite: the old sort-by-timestamp approach broke when batches
  shared a timestamp (D < R reordering put DQ events before RECON events,
  breaking linkage). Now walks the hash chain via `previous_hash → event_hash`.
  Genesis validation tightened for full-chain verification.

### Tests
- 153 tests passing; ruff clean (check + format).

[Unreleased]: https://github.com/joannechang39/civicpay-open-framework/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/joannechang39/civicpay-open-framework/releases/tag/v0.1.0
