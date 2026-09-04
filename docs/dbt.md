# dbt Analytical Marts — Technical Documentation

**Ticket:** spec §16 / "dbt analytical marts" · **Status:** Implemented (v0.2) · **Adapter:** `dbt-duckdb`, running directly against the same DuckDB file the Python pipelines write to — no warehouse, no additional infrastructure.

`dbt/` holds a small, complete dbt project: a thin staging layer over the raw tables, and three marts that are faithful SQL ports of logic that otherwise lives only in Python (`civicpay.dashboard.extractors`, `civicpay.quality.scoring`, `civicpay.exceptions.workflow`). The point isn't new analysis — the Python modules already compute all of this for the CLI and dashboard — it's giving the same numbers a declarative, tested, documented SQL form that a downstream BI tool or analyst could query directly, and that dbt's own test framework can hold to the same invariants the Python unit tests hold the originals to.

## Why marts duplicate Python logic instead of calling it

dbt models are pure SQL; they cannot import `civicpay.exceptions.workflow`. Each mart is therefore a **deliberate, documented, line-by-line SQL translation** of one Python module, verified against real pipeline output (see "Verification" below) rather than trusted by construction. Where the two diverge, the Python version is authoritative — a mart is analytical surface, not a second source of truth (the exact posture `mart_exception_aging`'s own `priority_score` already holds relative to the exception queue: [exceptions.md](exceptions.md) — "priority is computed on read, not persisted... a non-authoritative triage aid, not an audit record").

## Layout

```
dbt/
  dbt_project.yml       # vars: as_of_date, dq_type_weight_* (see below)
  profiles.yml           # committed — no secrets, just a DuckDB file path
  macros/
    generate_schema_name.sql   # standard override: +schema means the literal schema, not a suffix
  models/
    staging/              # thin passthrough views, one per source table a mart needs
      _sources.yml
      _staging.yml         # not_null/unique/accepted_values on the raw shape
      stg_*.sql
    marts/
      mart_recon_summary.sql
      mart_dq_summary.sql
      mart_exception_aging.sql
      _marts.yml
  tests/
    assert_recon_rates_in_range.sql   # singular test, no external package
```

Staging models are **passthrough, not cleaning** — column shapes are already enforced by `civicpay.storage.duckdb`'s DDL, and validation already has a dedicated home in [data-quality.md](data-quality.md). Re-implementing that in dbt would be a second, divergent copy of the same responsibility; staging here exists only to give marts a stable interface boundary onto the raw tables.

## The three marts

### `mart_recon_summary`

One row per reconciliation `batch_id`. Ports `civicpay.dashboard.extractors.reconciliation_summary` exactly, including a fix for a real dashboard bug: `reconciliation_rate` is computed over **payment-side rows only** (`match_status != 'unmatched_ledger'`), and `ledger_coverage_rate` is a separate column over the full ledger — the same metric name meaning two different numbers, blended into one field, was a real bug in the dashboard before that fix; the mart inherits the corrected, split shape rather than the original bug.

### `mart_dq_summary`

One row per `dataset_name`. Ports `civicpay.quality.scoring.dataset_quality_score` (check-type-weighted average, computed over whatever `dq_results` currently holds — that table is replaced wholesale each `civicpay dq check` run, never appended, so an unfiltered `group by dataset_name` is already "latest run only," matching the Python behavior with no batch filter needed) and `anomaly_rate` (excluded from the weighted score, reported on its own).

Per-check-type weights (`civicpay/config/dq_checks.yml`'s `type_weights`) are duplicated as dbt vars in `dbt_project.yml` (`dq_type_weight_completeness`, etc.) since dbt has no built-in way to read an arbitrary YAML config file at compile time. **If `civicpay/config/dq_checks.yml`'s `type_weights` ever change, these vars must be updated by hand** — the one place this mart can silently drift from its Python source without a test catching it (accuracy of the *value*, not the *shape*, isn't something `not_null`/`accepted_values` can check).

### `mart_exception_aging`

One row per `exception_queue` item, with the full priority formula computed in SQL:

```
priority_score = severity_weight × amount_at_risk_factor × age_factor
```

This is the most involved port, because `civicpay.exceptions.queue._amount_at_risk` dynamically resolves each exception's dollar amount from a *different* table depending on `reference_id`'s `"{dataset}:{record_id}"` prefix. The mart reproduces this with three `LEFT JOIN`s (`transactions`, `payment_records`, `pending_enrollments`) gated by `ref_dataset`, matching `_AMOUNT_DATASETS` in `civicpay/exceptions/queue.py`. It also reproduces both edge cases in that logic: "not applicable" (`amount_at_risk IS NULL`) resolves to `amount_basis = 'n/a'` and the neutral `2.5` factor, and a NaN amount (`pending_enrollments.incentive_amount` is stored as raw `TEXT` and `try_cast`s to `NULL` on unparseable input, which is the SQL analogue of the Python NaN guard) is treated identically — never falling through to the highest bucket.

`age_days` matches `civicpay.exceptions.workflow.age_days` exactly: `floor(total_seconds / 86400)`, via `date_diff('second', created_at, as_of_timestamp) / 86400.0`, not a calendar-date subtraction — those two disagree whenever `created_at` has a nonzero time-of-day component relative to midnight `as_of`.

`as_of_date` is a dbt var (`dbt_project.yml`, default `2026-09-01` — matching `civicpay.data.synthetic.AS_OF_DATE`) so re-running the marts against a freshly-seeded database with a different as-of date doesn't require editing SQL: `civicpay dbt run --date 2026-10-01`.

## Verification

There is no unit-test framework spanning Python and dbt in one run, so faithfulness was checked the direct way: `civicpay run-all` was run into a scratch database, the marts were built against it, and the mart output was compared row-for-row against the same run's own CLI summary. `mart_exception_aging`'s top `priority_score` (62.5) matched `run-all`'s own reported "top priority=62.50" for the same run exactly. `mart_recon_summary`/`mart_dq_summary` were checked against `civicpay.dashboard.extractors`' own functions called directly against the same database. This is a point-in-time check, not a regression test — if the Python formulas change, the SQL ports must be updated by hand and re-verified the same way; there is currently no CI gate that would catch drift automatically (a natural next step, not yet built).

## Running

```bash
civicpay dbt run                          # build all 3 marts (+ staging views)
civicpay dbt test                         # run the 25 schema/data tests
civicpay dbt run --db-path some/other.duckdb --date 2026-10-01
civicpay dbt run --select mart_dq_summary # build/test a subset
```

Requires the `dbt` optional dependency group: `pip install -e ".[dbt]"` (`dbt-core`, `dbt-duckdb` — not installed by default, since core CLI usage never needs them). Internally, `civicpay dbt *` shells out to the `dbt` console script located next to the current Python interpreter (dbt-core has no `python -m dbt` entry point, unlike `streamlit` — see `civicpay.dashboard.app.run_streamlit_app`), with `--project-dir dbt --profiles-dir dbt` and `CIVICPAY_DB_PATH` forwarded the same way the Streamlit apps receive a non-default database path.

**`PYTHONUTF8=1` is forced on the subprocess.** dbt-core opens its own project files (`dbt_project.yml`, `models/*.sql`/`.yml`) with the platform default text encoding rather than UTF-8. On a non-English Windows locale (e.g. `cp950` Traditional Chinese) this crashes on the first non-ASCII byte in this project's own source (`—`, `×`, `§` appear throughout the SQL comments and YAML descriptions) with `UnicodeDecodeError`. Forcing UTF-8 mode on the subprocess is the actual fix — stripping every non-ASCII character from the project's own comments was the alternative and the worse one, since it would have to be re-litigated every time prose is added anywhere under `dbt/`.

## Design notes & limitations

- **Materialization**: staging = `view` (cheap, no extra storage — DuckDB is fast enough that materializing intermediate views is unnecessary), marts = `table` (queried repeatedly; avoids recomputing the exception-aging joins on every read).
- **Schema names**: `+schema: staging` / `+schema: marts` produce literal `staging.*` / `marts.*` tables via a `generate_schema_name` override — dbt's un-overridden default would instead produce `main_staging`/`main_marts` (target-schema-prefixed), which is correct but less readable for a project this size.
- **No `packages.yml`/dbt Hub dependency.** The one range-style check (`reconciliation_rate`/`ledger_coverage_rate` within `[0, 100]`) is written as a plain singular SQL test (`tests/assert_recon_rates_in_range.sql`) instead of pulling in `dbt_utils` for one macro — not worth a package dependency (and the install/network step that comes with it) for a single check.
- **No source freshness checks.** These tables aren't append-only event streams with a `loaded_at` column in the streaming-freshness sense — `dq_results` is replaced wholesale per run, `exception_queue`/`reconciliation_results` are batch-appended. Freshness in the dbt sense doesn't map cleanly onto that shape.
- **Dashboard relationship**: the Streamlit dashboard's Python extractors (`civicpay/dashboard/extractors.py`) and these marts are **parallel, not layered** — the dashboard does not query the marts, and the marts do not replace the extractors. Both compute the same numbers from the same raw tables, independently, for different audiences (an interactive Python UI vs. SQL-queryable analytical tables). Making the dashboard read from the marts instead was considered and deferred: it would add a hard `civicpay dbt run` dependency before the dashboard could show current data, which conflicts with `civicpay dashboard` and `civicpay enroll`'s current zero-extra-step, run-immediately-after-seed design.
