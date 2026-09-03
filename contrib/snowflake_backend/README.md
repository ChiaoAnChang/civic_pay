# Snowflake Backend — Reference Implementation (UNVERIFIED)

**Status: written, never executed against a real Snowflake account.** This is a faithful, reviewable draft of the `SnowflakeStore` described in [`docs/cloud-backend.md`](../../docs/cloud-backend.md), not a tested or supported second backend. It exists to make that document's recommendation concrete — actual code, not just prose — while being honest that "concrete" and "validated" are different claims.

## Why this lives here and not in `civicpay/`

- `pyproject.toml`'s `[tool.hatch.build.targets.wheel]` only packages `civicpay/` — nothing under `contrib/` ships to anyone who `pip install`s the framework. This code is browsable and reviewable in the repo, but it is not distributed, not imported by anything in `civicpay/`, and does not add `snowflake-connector-python` as a dependency of the shipped package.
- `civicpay/storage/duckdb.py` is untouched. DuckDB remains the only active, tested backend — see `docs/cloud-backend.md`'s reasoning for why a full replacement isn't recommended (it would break the framework's zero-infrastructure adoption story and its free, hermetic `:memory:` test suite).

## What's here

- `store.py` — `SnowflakeStore`, implementing the same eight-method public surface as `civicpay.storage.duckdb.DuckDBStore` (`read_table`, `write_dataframe`, `write_many`, `execute`, `query`, `table_count`, `init_schema`, `close`), so it is a drop-in replacement *shape*-wise.
- `requirements.txt` — what you'd need to actually try this (`snowflake-connector-python[pandas]`; the `[pandas]` extra pulls in `pyarrow`, required by `fetch_pandas_all()`).
- `tests/test_snowflake_store.py` — a skeleton conformance test, skipped unless real credentials are present in the environment. It reuses one fixture pattern from `civicpay`'s own test suite (seed a tiny DataFrame, write it, read it back, assert equality) rather than inventing a new one.

## Known, deliberately unaddressed gap

`SCHEMA_DDL`'s `PRIMARY KEY` declarations are **not enforced by Snowflake** (Snowflake documents primary/foreign key constraints on standard tables as informational only). `DuckDBStore` gets real duplicate-key protection for free from DuckDB's engine; `SnowflakeStore` does not, and this file does not add an application-level compensating check (an existence-check-before-insert or a post-write verification read, as `docs/cloud-backend.md` describes). Implementing that honestly requires its own design and test pass against a real account — faking it here would be worse than leaving it as a named, visible gap. **Do not treat this file as safe for concurrent or duplicate-key-sensitive writes.**

## Three specific porting details this draft had to get right (and initially didn't, before verification)

1. **Paramstyle is not qmark by default.** The connector's default is `pyformat` (`%s`/`%(name)s`). `?`-style params — which is what every SQL string in `civicpay/exceptions/queue.py`, `civicpay/enrollment/`, `civicpay/audit/`, `civicpay/dashboard/extractors.py` uses — require `snowflake.connector.paramstyle = "qmark"` set **before the first `connect()` call**, and it is process-global, not per-connection. `store.py` sets this at import time with a comment explaining why.
2. **Identifier casing.** This project's `SCHEMA_DDL` uses unquoted, lowercase column names (e.g. `transaction_id`), which Snowflake folds to uppercase (`TRANSACTION_ID`) the same way DuckDB folds unquoted identifiers case-insensitively — but `write_pandas()`'s default `quote_identifiers=True` would instead emit *quoted*, DataFrame-column-cased identifiers (`"transaction_id"`), which is a **different, non-matching column** from Snowflake's perspective, not a resolvable case-insensitive alias. `store.py` passes `quote_identifiers=False` throughout to keep loaded data landing in the same folded-uppercase columns the DDL created.
3. **Naive-UTC convention.** `civicpay.storage.duckdb`'s `_naive_utc()` (`DataFrame` columns) and its `execute()`'s per-param tz-stripping exist because a tz-aware value written to a naive column silently shifts through the local system timezone — this is exactly the bug `docs/cloud-backend.md` flags as a cross-backend invariant that has to be carried forward deliberately. `store.py` reimplements both, rather than assuming Snowflake's driver behaves the same way DuckDB's does (it was not tested to confirm either way — the safe assumption was to normalize explicitly rather than trust it).

## What would have to happen before this could be taken seriously as a real backend

1. Someone with a real Snowflake account runs `tests/test_snowflake_store.py` and the rest of the port-surface inventory in `docs/cloud-backend.md` against it, and fixes whatever it gets wrong (there will be something — this has never touched a live warehouse).
2. The `previous_hash` `UNIQUE`-constraint fix (`docs/cloud-backend.md`'s concurrency section) ships against DuckDB first, independent of this work.
3. The primary-key-enforcement gap above gets a real, tested design.
4. `civicpay`'s ~40 `DuckDBStore(":memory:")` test call sites get parameterized over a backend fixture, and this file is what gets plugged into the "second backend" side of that fixture, per `docs/cloud-backend.md`'s stated acceptance criterion ("the existing suite passes against it," not "conforms to a Protocol").

None of that has happened. This is a draft, not a milestone.
