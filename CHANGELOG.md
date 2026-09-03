# Changelog

All notable changes to the CivicPay Open Framework are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-09-03

### Added
- **dbt analytical marts** (spec §16, previously deferred): a full `dbt/`
  project (`dbt-duckdb` adapter, no extra infrastructure) with a thin staging
  layer and three marts — `mart_recon_summary`, `mart_dq_summary`,
  `mart_exception_aging` — that are faithful, verified SQL ports of
  `civicpay.dashboard.extractors`, `civicpay.quality.scoring`, and
  `civicpay.exceptions.workflow` respectively, including the `amount_at_risk`
  None/NaN handling and the `reconciliation_rate` / `ledger_coverage_rate`
  split described below. New `civicpay dbt run` / `civicpay dbt test` CLI
  commands; new optional `dbt` dependency group (`pip install -e ".[dbt]"`).
  See [docs/dbt.md](docs/dbt.md).

- **`docs/cloud-backend.md`**: a decision record for a previously-unscoped
  "cloud storage backend" item — documentation only, no code changed.
  Reframes the motivation from "scale" (weak — the dataset is small and
  synthetic) to a latent audit-ledger concurrency race currently masked by
  DuckDB's single-writer file lock, names and compares
  MotherDuck/PostgreSQL/Snowflake/BigQuery as candidates (BigQuery is named
  and rejected, not left neutral), and recommends Snowflake as an eventual
  *optional second* backend — DuckDB stays the default.
- **`previous_hash` `UNIQUE` constraint on `audit_event_log`**, following up
  on the concurrency section of `docs/cloud-backend.md`: two writers reading
  the same chain tip and both appending against it now fails cleanly with a
  `duckdb.ConstraintException` instead of silently forking the chain.
  `AuditLedger.append()`/`append_many()` catch that specific exception,
  re-resolve the true tip, and retry — compare-and-swap expressed as a
  schema constraint. Unreachable and unexercised under DuckDB's own
  single-writer file lock today; ships now regardless, since it is the
  precondition for any future backend that does permit concurrent writers.
  4 new tests, including a persistent-conflict case that asserts a clear
  `RuntimeError` rather than an infinite retry loop.
- **`contrib/snowflake_backend/`**: an unverified `SnowflakeStore` reference
  implementation, extending the design doc above into real, reviewable
  code. Mirrors `DuckDBStore`'s full eight-method surface; lives outside
  `civicpay/` so it never ships, is never imported by shipped code, and
  adds no dependency to the package. Writing it caught two real corrections
  to `docs/cloud-backend.md` (the connector's default paramstyle is
  `pyformat`, not qmark; `write_pandas()` needs `quote_identifiers=False`
  to match this project's unquoted-lowercase `SCHEMA_DDL`), both fixed in
  the doc and called out in the code's own comments. Never executed against
  a real Snowflake account — see `contrib/snowflake_backend/README.md`'s
  status section and known gaps (most notably, no compensation for
  Snowflake's non-enforcement of `PRIMARY KEY`).

### Changed
- **`amount_at_risk` distinguishes "not applicable" from a resolved `$0.00`**:
  a DQ exception on `accounts`/`customers` (no dollar
  amount concept) now resolves `amount_at_risk` to `None`, not `0.0` — a
  real, if trivial, resolved zero amount and "no amount concept applies at
  all" were previously indistinguishable, and `amount_at_risk_factor(0.0)`
  is the formula's *lowest* bucket (1.0 of a 1–4 range), so a high-severity
  amount-less exception could never outrank even a medium-severity,
  moderately-priced dollar-bearing one in the same sorted queue — severity
  drove ranking *within* the amount-less cohort but was subordinate *across*
  cohorts. `amount_at_risk_factor(None)` now returns a new
  `NEUTRAL_AMOUNT_AT_RISK_FACTOR` (2.5, the range's midpoint) instead. A new
  per-item `amount_basis: "amount" | "n/a"` field exposes which case
  applies — surfaced as a `basis`/`amount_basis` column in the CLI table and
  the dashboard. No fake dollar amount is invented; the basis is always
  disclosed.

### Fixed
- **`civicpay dbt run`/`test` crashed with `UnicodeDecodeError` on a
  non-English Windows locale** (e.g. `cp950` Traditional Chinese): dbt-core
  opens its own project files (`dbt_project.yml`, `models/*.sql`/`.yml`)
  using the platform default text encoding rather than UTF-8, and this
  project's own SQL comments/YAML descriptions contain non-ASCII characters
  (`—`, `×`, `§`). Fixed by forcing `PYTHONUTF8=1` on the `dbt` subprocess
  the CLI wrapper launches, rather than stripping non-ASCII characters from
  the project's own source.
- **NaN amount would have landed in the highest priority bucket, not the
  neutral one — caught by code review before the `amount_at_risk` change
  above was finalized.** `transactions.amount` / `payment_records.amount`
  are nullable `DOUBLE` columns; a SQL `NULL` reads back as pandas `NaN`, and
  `float(nan)` doesn't raise, so it slipped past the
  `except (TypeError, ValueError)` guard as a "resolved" amount. Every bucket
  comparison in `amount_at_risk_factor` is `False` against NaN, so it fell
  through to the highest bucket (4.0) — the opposite of the neutral (2.5)
  treatment the `amount_at_risk` change above was specifically introducing.
  Latent (no current data path nulls an amount) but schema-permitted. Fixed
  in both `queue.py` (`_amount_at_risk` checks `math.isnan`) and defensively
  in `workflow.py`'s public `amount_at_risk_factor` (treats NaN the same as
  `None`).
- **`civicpay dashboard --db-path` / `civicpay enroll --db-path`**: both
  Streamlit entry points can now target a non-default database. Since
  `streamlit run` launches a subprocess that can't receive a Python
  argument directly, the path is forwarded via a new `CIVICPAY_DB_PATH` env
  var, resolved by `civicpay.storage.duckdb.resolve_db_path()`. Fails fast
  with a clean error if the file doesn't exist, instead of silently
  rendering a blank dashboard against a freshly-created empty database.
- `docs/exceptions.md` and `docs/audit.md`: standalone
  technical docs for the exception workflow and audit-evidence layer,
  matching the existing `docs/reconciliation.md`/`docs/data-quality.md`
  style — covering the per-severity SLA design, the backlog-cohort pattern,
  and the two real audit bugs described below (root cause and why each
  matters for future changes to that module).
- **Enrollment & validation module** (Ticket 13, v0.2 — `civicpay/enrollment/`):
  point-of-capture error prevention complementing v0.1's post-hoc
  reconciliation. `validators.validate()` runs per-field and cross-field
  checks (declarative rules in `config/enrollment_rules.yml`) against a raw
  candidate row; a passing record goes through a **dual-source agreement
  gate** (`dual_source.py`) — one pure-Python proration path, one
  independent SQL-via-DuckDB path — and is accepted (`accepted_enrollments`)
  only when both agree within tolerance, otherwise routed to the existing
  `exception_queue` (`source="enrollment_dual_source"`) for human review via
  `civicpay exception list/resolve`. Every dual-source evaluation is
  recorded in a new `enrollment_dual_source_results` table (agreeing or not,
  mirroring `dq_results`'s always-record-everything convention). New CLI:
  `civicpay enroll validate [--file records.csv]`, reading either an
  external CSV or the seeded `pending_enrollments` pool by default; wired
  into `civicpay run-all` as a new stage. `civicpay seed` now also seeds 200
  enrollment candidates, a small fixed cohort deliberately violating
  validation rules (bad date, out-of-range amount, duplicate entity id,
  stray-space numeric, missing required) so the validators have known
  defects to catch, plus a deterministic aged-mismatch backlog (mirroring
  the DQ module's `BACKLOG_BATCH_ID` pattern) so SLA escalation is visible
  in the exception queue without wall-clock timestamps. No REST API and no
  new dependencies — an architecture-fit review before implementation found
  several of the ticket's original design assumptions didn't match how the
  core pipeline actually turned out (SLA handling, evidence export, and
  exception routing had all changed since the original design).
  **Dashboard:** a new "Enrollment & Validation" section (candidate/outcome
  counts, a dual-source-mismatch table joining both computed values, and an
  in-page resolve action — Accept Path A / Accept Path B / Reject &
  re-enter, each emitting an audit event via the existing `ExceptionManager.
  resolve`) — the dashboard's first write action; every other view is
  read-only. **Form:** `civicpay enroll` launches a 3-step Streamlit
  constrained-input form (`civicpay/enrollment/forms.py`) — entity/program/
  region, terms/amount, then a live-validated review step whose Submit
  button is disabled until every blocking error clears; `civicpay enroll
  validate` (with `--file`) remains the batch path. Verified end-to-end with
  `streamlit.testing.v1.AppTest` (both the dashboard section and the form),
  not just an HTTP+log smoke test.

### Changed
- **BREAKING (evidence package schema): `export_evidence` always exports
  both a reconciliation section and an exception section**, replacing the
  recon-only design. `dq_results` turned out to carry no
  batch identity and is replaced (not appended) per run, so a `--mode
  recon|dq` flag — the original plan — was never implementable without a
  schema change; `exception_queue` is the real batch-scoped, append-only DQ
  story instead. Package keys added: `scope` (row counts per table queried +
  a deterministic `event_timestamp_range` anchor alongside the wall-clock
  `exported_at`), `exception_summary`, `exceptions`. `export_evidence(...,
  full=False)` is the new default — `reconciliation_results` is empty unless
  `--full` is passed (was always the complete rows, up to ~50k); exception
  rows are always included in full (already bounded by
  `max_exceptions_per_check`). New `UnknownBatchIdError`: exporting a
  mistyped/nonexistent `batch_id` now fails loudly (was: a silent, empty,
  `verified: true` package with nothing distinguishing "no activity" from
  "wrong batch id"). CLI: `civicpay audit export` gained `--full`.

### Fixed
- **Dashboard reconciliation rate disagreed with the CLI's own number**:
  `civicpay.recon.matcher` computes
  `reconciliation_rate` over payment-side rows only (0.89 on the default
  seed, matching `docs/reconciliation.md` and the CLI's own `run-all`
  output), but the dashboard's `reconciliation_summary()` recomputed the
  same-named metric independently over *every* `reconciliation_results` row
  — including the `unmatched_ledger` rows appended for ledger transactions
  that were never going to match anything (~49,110 of them on the default
  seed) — giving ~0.018 for the identical batch. Not a data-realism issue;
  a "demo-friendly higher-match seed" (the original proposal) would have
  hidden the discrepancy rather than fixed it. `reconciliation_summary()`
  now computes the headline rate payment-side-only, matching the engine, and
  reports ledger coverage as its own separate field
  (`ledger_coverage_rate`/`unmatched_ledger`) rather than blending it in —
  surfaced as a 5th "Ledger Coverage" metric card on the dashboard.
- **`.gitignore` gained a blanket `*.duckdb` rule.** An ad hoc scratch
  database created at the repo root during manual Ticket 13 verification
  (not under the already-ignored `data/processed/`) was swept into a commit
  before this rule existed. Removed from the working tree; the rule now
  covers any stray DuckDB file regardless of location.
- **Audit hash chain could fork when two batches shared a timestamp.**
  `AuditLedger._initialize_chain` resumed the chain via `ORDER BY timestamp
  DESC, event_id DESC LIMIT 1` — a heuristic that assumed batch-id strings
  sort in the order batches are actually appended. They don't (e.g.
  `"EVT-R1-..."` sorts after `"EVT-D1-..."` regardless of which was written
  more recently), and every pipeline here shares the same deterministic
  `as_of` timestamp, so ties are common. A new `AuditLedger` instance
  initializing after two prior batches already coexist could resume from a
  stale, non-tip event and fork the chain. Found while implementing the
  backlog-cohort feature (which introduces a second `AuditLedger`
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
  setting up a fresh local dev environment (Windows, UTC-5) to verify the
  anomaly-weight change below — 10 of 11 test failures in that environment
  traced back to this one root cause.

### Added
- **Deterministic exception backlog cohort**: on a
  full-config `dq check`/`run-all` against a fresh database, `QualityPipeline`
  now also seeds a small, fixed cohort of already-aged exceptions (real
  failing record ids, `created_at` backdated 2/5/12/21/45 days,
  batch id `DQ-000-PRIOR`) so SLA escalation is visible in the CLI table
  and dashboard out of the box, instead of every exception showing
  `age_days = 0` on a static demo DB. Genuine DQ detections still use
  `created_at = as_of` unchanged (this must agree with the paired
  `exception_open` audit event). Seeded once — idempotent on re-run via the
  same `batch_id_in_use` check pipelines already use for their own batch ids.
  `summary["backlog_seeded"]` reports the count (0 after the first run).
  `exception list` / the dashboard also gained a per-item `sla_days` column.

### Changed
- **Per-severity SLA windows for exception aging**:
  `SLA_DAYS_BY_SEVERITY = {"high": 3, "medium": 7, "low": 14}` replaces the
  old flat 7-day default for every exception regardless of severity — a
  high-severity item now starts escalating sooner than a low-severity one,
  matching real SLA/triage practice. `--sla-days` (CLI) /
  `sla_days=` (`ExceptionManager.list`, `dashboard.exception_queue`) still
  works as an explicit override (applies the same window to every item);
  the resolved window is now also exposed per-item as a new `sla_days`
  field/column (CLI table, dashboard). Per-check-type SLA config remains
  deferred to v0.2.
- **Anomaly checks excluded from the dataset quality score**:
  `type_weights.anomaly` is now `0.0` (was `0.5`). A z-score/IQR anomaly
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
- **Re-run idempotency pre-flight guard**: both pipelines
  (`QualityPipeline`, `ReconciliationPipeline`) now check whether a `batch_id`
  is already present in the append-only audit log before any writes, and fail
  fast with `BatchIdAlreadyUsedError` (a clear, actionable message) instead of
  a raw DuckDB primary-key constraint error mid-run. `civicpay run-all` checks
  both derived batch ids (`{run_id}-RECON`, `{run_id}-DQ`) up front.
- **Genuinely-stale transaction cohort**: synthetic
  transactions now include a ~3.5% cohort with `created_at` dates 45/60/90 days
  before the as-of date, so the timeliness DQ check catches meaningful staleness
  instead of an Aug-1 boundary artifact (records that were only 31 days old).
  Payment records stay fresh; reconciliation DoD counts are unchanged.
- Trusted Publishing (OIDC) for PyPI: releases are published via GitHub Actions
  identity, no shared API token secret required.
- Version is now derived from git tags via `hatch-vcs` (`dynamic = ["version"]`).
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
- 248 tests passing, 1 skipped; ruff clean (check + format).

[Unreleased]: https://github.com/ChiaoAnChang/civic_pay/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ChiaoAnChang/civic_pay/releases/tag/v0.1.0
