# Cloud Storage Backend — Design & Decision Record

**Status:** Documented, not implemented. This is a decision record for a decision **not yet made to build** — options, the discriminating criterion, what's blocking, what would have to be true to pick each. It is deliberately not a roadmap ("v0.3 will add Snowflake"); nothing here is scheduled.

## The question isn't "which cloud backend" — it's "which of three axes are we actually buying"

"Move to a cloud backend" conflates three independent concerns. A given backend can buy one, two, or all three, and the honest framing of *why* to do this work depends entirely on which axis is the actual goal:

1. **Where the bytes live** — a local file vs. a hosted/shared location. A deployment concern.
2. **Which engine executes SQL** — DuckDB vs. Postgres vs. a warehouse. A dialect/porting-cost concern.
3. **What concurrency guarantees exist** — today, none, because nothing has ever needed any. The actual limitation.

These are separable, and conflating them produces the weakest possible justification for this work: "scale." The dataset here is synthetic and small — `civicpay run-all`'s default 50k-transaction volume reconciles in seconds on an embedded engine, and the dbt marts (`docs/dbt.md`) build in under 5 seconds. Nobody reviewing this framework would find "we needed a cloud warehouse because DuckDB is slow" credible, because it isn't true.

## The honest motivation: a latent concurrency race, currently masked by a single-writer file lock

`civicpay.audit.ledger.AuditLedger._initialize_chain()` (`civicpay/audit/ledger.py:108-138`) resumes the hash chain by finding the one `event_hash` that is never referenced as anyone's `previous_hash`:

```sql
SELECT event_hash FROM audit_event_log t
WHERE NOT EXISTS (
    SELECT 1 FROM audit_event_log n WHERE n.previous_hash = t.event_hash
) LIMIT 1
```

This value is cached once per `AuditLedger` instance (`self._previous_hash`), and every subsequent `append()` chains off that cached value, advancing it locally. This is a textbook read-then-write race: two concurrent writers can both read the same tip, both append with the same `previous_hash`, and produce exactly the same *shape* of chain fork this codebase has already hit once — a *second* `AuditLedger` instance initializing after several same-timestamp batches already coexisted resumed from a stale, non-tip event and forked the chain, fixed by making `_initialize_chain` resolve the tip structurally rather than by ordering. This race is that same failure mode across processes instead of within one.

**This has never caused a problem in production because DuckDB takes an exclusive file lock.** A second process opening the database file read-write fails outright — there is no window for two writers to interleave. The single-writer constraint that a "cloud backend" would remove is the same constraint that currently makes the ledger correct.

The honest thesis, then, is not "DuckDB can't handle concurrent writers, so move to the cloud." It's:

> **Moving to any backend that permits concurrent writers is blocked on a concurrency-control design for the ledger, not on a storage abstraction.** The limitation is real (a genuine multi-caseworker municipal deployment — this framework's stated real-world target — needs more than one writer), but naively removing DuckDB's exclusion lock without replacing what it was protecting makes the system *less* correct, not more capable.

**The fix that has to come first, independent of any backend decision — IMPLEMENTED.** A `UNIQUE` constraint now exists on `audit_event_log.previous_hash` (`civicpay/storage/duckdb.py`'s `SCHEMA_DDL`), and `AuditLedger.append()`/`append_many()` (`civicpay/audit/ledger.py`) catch the resulting `duckdb.ConstraintException`, roll back their local sequence counter, re-resolve the true tip, and retry — compare-and-swap expressed as a schema constraint, not an unenforced convention. This project has exactly one global hash chain (not one per batch — `_initialize_chain` scans the whole table), so there is exactly one genesis event ever, and a plain `UNIQUE` constraint is sufficient with no partial-index carve-out needed for it. This retry path is unreachable and unexercised under DuckDB's own single-writer file lock (nothing can produce the race today), so it is tested by directly desynchronizing a second `AuditLedger` instance's cached tip — not by an actual concurrent-process race, which DuckDB itself still prevents. It is the precondition that makes any future shared backend viable, not a promise that one is coming.

**Note for a future backend implementer:** `CREATE TABLE IF NOT EXISTS` does not retrofit this constraint onto a database file created before this change — an existing `data/processed/*.duckdb` needs a fresh `civicpay seed` to pick it up. The equivalent constraint on Snowflake would need the application-level compensation described above, since Snowflake does not enforce it at the schema level the way DuckDB does.

One more cross-backend invariant worth stating plainly: the audit chain's hash is computed over an ISO-8601 UTC rendering (the `_naive_utc` fix, `civicpay/storage/duckdb.py:47-69`, OPEN_QUESTIONS §P), not a backend-specific timestamp representation. A chain written on DuckDB verifies correctly on any other backend *only if* that backend preserves the "naive value means UTC" convention exactly — this has to be carried forward deliberately, not assumed.

## Port-surface inventory

`civicpay/storage/duckdb.py` is the only file in the package that touches the `duckdb` Python package directly (verified by grep across `civicpay/`: zero other files reference `.conn.`, `duckdb.`, or `register(`). Every other module — `recon/`, `quality/`, `exceptions/`, `audit/`, `enrollment/`, `dashboard/` — calls only `DuckDBStore`'s eight public methods (`read_table`, `write_dataframe`, `write_many`, `execute`, `query`, `table_count`, `init_schema`, `close`). `civicpay/recon/` and `civicpay/quality/` do all matching/scoring logic in pandas after `read_table()`, never in SQL.

This means `DuckDBStore` is already a Facade — there is no missing abstraction, only a missing second implementation. **A formal `StorageBackend` Protocol/ABC is not being extracted in this document**, for the reason `OPEN_QUESTIONS.md` itself models repeatedly (§M defers Plotly with "no concrete need"; §F separates "cheap, build it" from "real overkill, defer"): a Protocol built from exactly one implementation reliably enshrines that implementation's leaks rather than describing a real contract. Concretely, three leaks a premature Protocol would either freeze or paper over:

- **`execute()`'s declared return type is `duckdb.DuckDBPyConnection`** (`civicpay/storage/duckdb.py:316`), and a caller depends on it (`audit/ledger.py:129` calls `.fetchone()` on the result).
- **`write_dataframe(mode="replace")` has a DuckDB-only fallback branch** for tables outside `SCHEMA_DDL` (lines 298–303): `DROP` → `register()` a DataFrame as a virtual table → `CREATE TABLE AS SELECT * FROM` it. This is DuckDB's schema-inference-from-a-registered-DataFrame trick; no other engine has an equivalent shape.
- **`.conn` is used outside the package**, deliberately: `tests/test_audit.py` (lines 67, 79, 92, 185) reaches through `store.conn.execute()` to simulate tampering that bypasses the sanitizing write path — a legitimate use, but it means the honest claim is "nothing *shipped* uses `.conn`," not "nothing uses it."

Instead, here is the exact surface any second backend has to reimplement, so this document is itself the contract until a second implementation makes a Protocol worth writing:

| Surface | Location | What a new backend must provide |
| --- | --- | --- |
| `SCHEMA_DDL` type names | `storage/duckdb.py:73-233` | `DOUBLE`, `VARCHAR`, `TIMESTAMP`, `DATE`, `BOOLEAN`, `INTEGER`, `PRIMARY KEY` — per-engine equivalents below |
| Bulk write path | `storage/duckdb.py:283-308` | DuckDB: `conn.register()` (zero-copy) + `INSERT ... SELECT`. No other engine has this; each needs its own bulk-load primitive |
| `execute(sql, params)` paramstyle | `storage/duckdb.py:316-330`, called from `exceptions/queue.py`, `enrollment/pipeline.py`, `enrollment/dual_source.py`, `audit/ledger.py`, `audit/evidence.py`, `dashboard/extractors.py` | DuckDB uses `?` (qmark) throughout every call site |
| `execute()` return type | `storage/duckdb.py:316` | `audit/ledger.py:129` calls `.fetchone()` on it — any replacement cursor must support that |
| `resolve_db_path()` | `storage/duckdb.py:30-44` | Assumes the resolved value is a filesystem `Path`. A hosted backend's "path" is a connection string/account identifier — the return type and `CIVICPAY_DB_PATH` semantics both need to widen |

**The acceptance criterion for a second backend, if one is ever built, is not conformance to a Protocol — it's the existing test suite passing against it.** Roughly 40 test call sites construct `DuckDBStore(":memory:")`; parameterizing that fixture over a backend turns the current suite into a conformance suite for free, and is a far stronger, more honest contract than a structural type written with no second implementation to validate it against.

## Per-engine comparison

| | **MotherDuck** | **PostgreSQL** | **Snowflake** | **BigQuery** |
| --- | --- | --- | --- | --- |
| Paramstyle vs. this project's `?` | Identical (same `duckdb` package) | `%s` (psycopg) — mechanical rewrite across 6 files | **Default is `pyformat` (`%s`/`%(name)s`), not qmark** — but `?` is fully supported by setting `snowflake.connector.paramstyle = "qmark"` once, module-level, before the first `connect()` (verified against the connector's own source and issue tracker; it is process-global, not per-connection/cursor — a real constraint if this process ever needed two Snowflake connections with different paramstyles, moot for this project's single-backend-at-a-time usage). With that one line set, no per-call-site rewrite is needed | Named `@p` parameters via a `ScalarQueryParameter` object API — `execute(sql, params)`'s shape doesn't survive; needs a translation layer |
| `SCHEMA_DDL` type rewrite | None — verbatim | Small: `DOUBLE` → `DOUBLE PRECISION`; rest compatible | Small: `DOUBLE` is a valid alias as-is; `TIMESTAMP` defaults to `TIMESTAMP_NTZ` (no timezone) — coincidentally matches this project's naive-UTC convention | Full rewrite: `VARCHAR`→`STRING`, `DOUBLE`→`FLOAT64`, `INTEGER`→`INT64` |
| Bulk write replacement for `register()` | None — identical API | `COPY FROM STDIN` (psycopg3) or batched `executemany` | `write_pandas()` — a documented top-level helper in `snowflake-connector-python` | `load_table_from_dataframe()` (`google-cloud-bigquery`) |
| `PRIMARY KEY` enforcement | Enforced (same DuckDB engine) | Enforced | **Declared but not checked** — Snowflake accepts the syntax and never validates uniqueness; a duplicate `exception_id` that DuckDB rejects today would silently insert | **No enforcement of any kind** — even declaring one is metadata-only in newer BigQuery, not a real constraint |
| dbt adapter | None needed (`dbt-duckdb`, `path:` becomes an `md:` string) | `dbt-postgres` | `dbt-snowflake` | `dbt-bigquery` |
| dbt dialect exposure to fix | None | `mart_exception_aging.sql`'s one `date_diff('second', ...)` → `EXTRACT(EPOCH FROM ...)` | Same one call → `DATEDIFF(...)` | Same one call → rewrite needed |
| Fixes multi-machine access (axis 1) | Yes | Yes | Yes | Yes |
| Fixes concurrent ledger writers (axis 3) | **No** — buys hosting, not concurrency control | Yes, *if* the `UNIQUE`-constraint fix above ships first | Yes for the constraint-violation-and-retry pattern *in principle*, but the constraint itself isn't enforced — the compare-and-swap retry logic becomes entirely application-level, with no database backstop if the app-level check has a bug | **Actively worse** — no enforcement path exists at all |
| Cost/operational model | Usage-based, low friction | Self-hosted or managed (RDS/Cloud SQL) — predictable | Per-second compute billing while a warehouse is active; needs auto-suspend tuned low for a demo workload | Per-query billing; high per-statement latency, ill-suited to this project's fine-grained, high-frequency single-row `UPDATE`s (e.g. every exception resolution) |

**BigQuery is named and rejected, not left neutral.** Every property this project's append-only, read-the-tip-then-write ledger depends on — enforced uniqueness, low-latency single-row writes, transactional `UPDATE` semantics — is structurally absent from BigQuery's design, which optimizes for large-batch analytical scans instead. Naming a candidate and rejecting it for a specific, evidenced reason is worth more here than staying neutral on four options.

## Recommendation: Snowflake as the (eventual, optional) second backend — with the reasoning made explicit

If a second backend is ever built, this document recommends **Snowflake**, kept alongside DuckDB as the default rather than replacing it. The reasoning has two genuinely different parts, and they should not be blurred together:

**The architectural case is real but not decisive on its own.** Snowflake's `?`-paramstyle support (opt-in via one module-level setting, not automatic — see the table above) and DuckDB-compatible `DOUBLE`/`TIMESTAMP_NTZ` types mean the port-surface changes above are smaller than Postgres's in some respects (no per-call-site paramstyle rewrite) and comparable in others (bulk-write replacement, DDL types). It is a fully legitimate warehouse choice. But Postgres is *at least* as capable architecturally, cheaper to run, and has an enforced `UNIQUE` constraint that Snowflake lacks — on pure engineering merits, the two are close, and an argument that Snowflake is *technically superior* for this project would be overstated.

**The deciding factor is the author's own professional Snowflake expertise**, and this document says so plainly rather than dressing it up as a purely technical conclusion. For a framework that doubles as NIW petition evidence, an integration built by someone with genuine Snowflake depth will be higher quality and more representative of demonstrated expertise than a generic port to an engine chosen for architectural neutrality. `dbt` + Snowflake is also the field's de facto standard pairing for exactly this kind of transformation-and-marts work — it directly extends the dbt project already built this session (`docs/dbt.md`) rather than sitting next to it as an unrelated addition.

This recommendation is explicitly **not** a decision to build. It answers "if we build a second backend, which one, and why" — not "we are building a second backend." That remains open.

## If it is ever built: shape of the work (not scheduled)

- **`DuckDBStore` stays the default and the test backend.** No existing behavior changes; `civicpay run-all`, `civicpay dashboard`, and the entire test suite continue to work with zero setup, preserving the framework's stated no-infrastructure adoption story.
- **A `SnowflakeStore` implementing the same eight-method surface**, selected via an explicit opt-in (e.g. a `CIVICPAY_BACKEND=snowflake` env var alongside `CIVICPAY_DB_PATH`/connection parameters) — never a silent default.
- **The `previous_hash` `UNIQUE`-constraint fix ships first**, against DuckDB, independent of and before any Snowflake work — see above.
- **Snowflake's non-enforcement of uniqueness must be compensated at the application layer**: an explicit existence check before insert, or a post-write verification read, since the database will not backstop a bug in that logic the way DuckDB's real `PRIMARY KEY` does today. This should be a named, tested code path, not an assumption.
- **Testing**: keep the existing suite running against `DuckDBStore(":memory:")` as the default (free, fast, hermetic — Snowflake has no free ephemeral equivalent). Add a separately-marked integration suite (e.g. `@pytest.mark.snowflake`) that only runs when real credentials are present, skipped by default in CI — the common pattern for optional cloud-dependent tests.
- **dbt**: add a second `target:` in `dbt/profiles.yml` (Snowflake alongside the existing `dev` DuckDB target); rewrite `mart_exception_aging.sql`'s one `date_diff` call behind a dialect-conditional or a small dbt macro.

## Reference implementation: `contrib/snowflake_backend/`

At Joanne's request, a draft `SnowflakeStore` implementing the eight-method surface above now exists at [`contrib/snowflake_backend/`](../contrib/snowflake_backend/) — real, reviewable code, not just this document's prose description of what it would look like. It is explicitly **unverified**: written from the port-surface inventory above and cross-checked against the Snowflake connector's own source and documentation, but never executed against a live Snowflake account. `contrib/snowflake_backend/README.md` states its status, known gaps (most importantly, no compensation for Snowflake's non-enforcement of `PRIMARY KEY`), and what would have to happen before it could be taken seriously as a real backend.

It lives outside `civicpay/` deliberately: `pyproject.toml` only packages `civicpay/`, so this code never ships to anyone who `pip install`s the framework, is never imported by any shipped module, and adds no dependency to the shipped package. `civicpay/storage/duckdb.py` is untouched.

Two corrections surfaced while writing it, both fixed in this document and worth recording as an example of verify-before-asserting catching a real error: the Snowflake connector's *default* paramstyle is `pyformat` (`%s`), not qmark as an earlier draft of this document assumed — `?`-style parameters require one explicit `snowflake.connector.paramstyle = "qmark"` set before the first `connect()` call, not automatic default behavior. And `write_pandas()`'s default `quote_identifiers=True` would have silently written to the wrong (quoted, DataFrame-cased) columns given this project's unquoted, lowercase `SCHEMA_DDL` — `quote_identifiers=False` is required to land data in the same folded-uppercase columns the DDL creates.

## What this document deliberately does not do

It does not add a `StorageBackend` Protocol, and does not change `civicpay/storage/duckdb.py` or wire any second backend into the live CLI/dashboard. It is a decision record for a decision that has not been made to *adopt* a second backend — matching the "documentation [and, since, a reference draft] only, further discussion before deciding how to proceed" scope this work was explicitly given. The existence of a draft `SnowflakeStore` is not a decision to ship one.
