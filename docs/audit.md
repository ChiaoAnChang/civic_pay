# Audit-Evidence Layer — Technical Documentation

**Ticket:** Spec Ticket 7 (user "Ticket 6") · **Status:** Implemented · **Tests:** see [reconciliation.md](reconciliation.md) §10 for full-suite results

`civicpay.audit.ledger` maintains an append-only, hash-chained event log (`audit_event_log`); `civicpay.audit.evidence` verifies the chain and exports tamper-evident evidence packages. Every state-changing action across every module (recon, DQ, exceptions, and — v0.2 — enrollment) writes through this layer.

## Hash chaining

Each event's `event_hash = sha256(canonical_json(payload))`, where `payload` is the event's hashed fields (`event_id`, `timestamp`, `event_type`, `actor`, `entity_type`, `entity_id`, `action`, `previous_hash`) — including the *previous* event's hash, so altering any prior event invalidates every hash after it. Canonicalization: sorted JSON keys, datetimes forced to UTC ISO-8601 (naive datetimes are assumed UTC — see the timezone pitfall below for why that assumption has to actually hold), Decimals as strings.

`verify_chain(store, batch_id=None)` recomputes every hash from the persisted rows and walks the chain via `previous_hash → event_hash` links (not by sorting on timestamp — see the fork bug below for why that distinction matters). Detects: `content_hash_mismatch` (a hashed field altered without recomputing the hash), `chain_linkage_broken` / `no_genesis` (an event inserted, deleted, or reordered), `chain_fork` (two events claim the same predecessor), `cycle_detected`, `orphaned_events`. A batch-filtered verification allows the batch's first event to legitimately point at a prior batch's hash (a boundary); a full-chain verification requires the genesis to have `previous_hash == ""`.

## `AuditLedger`: one instance per logical batch

An `AuditLedger` instance's `_batch_id` (used to build `event_id = f"EVT-{batch_id}-{seq:06d}"`) locks to whichever `batch_id` its **first** appended event carries — the `batch_id` field on later `.append()`/`.append_many()` calls through the *same* instance is not re-read. This is why every pipeline creates a fresh `AuditLedger` per logical batch rather than sharing one long-lived instance: `ExceptionManager.resolve()` creates one per resolution (`batch_id=f"EXC-RESOLVE-{exception_id}"`), `QualityPipeline` creates a separate one for its DQ-backlog cohort, and `EnrollmentPipeline` creates one per enrollment (`batch_id = enrollment_id`) plus a separate one for its own backlog cohort.

## Two real bugs found and fixed here (both worth knowing before touching this module)

### 1. Timestamps silently shifted through the local system timezone

`AS_OF_DATETIME` and other pipeline timestamps are timezone-aware (`tzinfo=UTC`), but every DuckDB `TIMESTAMP` column is timezone-**naive**. Writing a tz-aware value into one silently converts it through the *local system timezone* before dropping the tz (standard DB-driver behavior) — verified empirically: a UTC value written on a UTC-5 machine came back 5 hours earlier on read. Since the ledger's canonicalization treats a naive datetime as "already UTC," a value that round-tripped through a non-UTC machine no longer matched the hash computed from the original tz-aware value — `audit verify` reported the chain as tampered with zero actual tampering, on **any** machine not set to UTC.

**Fixed at the storage layer**, not per-caller: `civicpay/storage/duckdb.py`'s `DuckDBStore.write_dataframe()` (via `_naive_utc()`) and `.execute()` now normalize timezone-aware datetimes to naive UTC before every write / every raw-SQL parameter bind. Any code writing a datetime through `store.write_dataframe()` or `store.execute()` gets this for free; reaching into `store.conn.execute()` directly bypasses it (there are no remaining call sites that do, as of this writing — keep it that way).

### 2. Chain-resume could fork when two batches shared a timestamp

`AuditLedger._initialize_chain()` used to resume the chain via `ORDER BY timestamp DESC, event_id DESC LIMIT 1` — a heuristic that assumed batch-id strings sort in the order batches are actually appended. They don't: `"EVT-R1-..."` sorts after `"EVT-D1-..."` regardless of which was written more recently, and every pipeline here shares the same deterministic `as_of` timestamp, so ties between batches are the norm, not the exception. A **new** `AuditLedger` instance initializing after two or more same-timestamp batches already coexist could resume from a stale, non-tip event — and since that event already had a different child, the new append forked the chain. This surfaced concretely once a second scenario existed where a fresh `AuditLedger` instance is created *after* other batches already exist in the same run (the v0.2 enrollment backlog cohort, which appends via its own ledger instance right after the main enrollment batch's).

**Fixed at the root:** `_initialize_chain` now finds the chain's tip **structurally** — the one `event_hash` never referenced as anyone's `previous_hash` — instead of guessing from timestamp/event-id ordering. This is ordering-independent and correct for any number of same-timestamp batches appended in any order, not just the specific case that first exposed it.

## Evidence export

`export_evidence(store, batch_id, out_path=None, full=False)` bundles a verification report, the batch's audit events, and the batch's backing rows into one JSON package. It **always** includes both a reconciliation section (`reconciliation_summary`/`reconciliation_results`) and an exception section (`exception_summary`/`exceptions`) for every batch — there is no `--mode recon|dq` flag, deliberately:

**Why not a mode flag (the original design):** `dq_results` carries no `batch_id` column at all and is replaced (not appended) every DQ run, so there is no per-batch DQ history to export with or without a flag — a `--mode dq` option could only ever export "whatever the last DQ run happened to leave behind," mislabeled with a batch id it might not belong to. `exception_queue`, by contrast, **is** batch-scoped (`exception_id = EXC-{batch_id}-...`) and append-only — the real batch-level DQ (and, since v0.2, enrollment) story. Querying it the same way regardless of batch kind means a recon batch's export naturally has an empty exception section and a DQ/enrollment batch's export naturally has an empty reconciliation section — no flag to get wrong.

**Size default:** `full=False` (the default) omits the full `reconciliation_results` rows, which can run into the tens of thousands for a large batch — only summary counts are included. `--full` opts in. Exception rows are always included in full regardless — they're already bounded by `max_exceptions_per_check` at routing time.

**Unknown batch ids fail loudly:** `export_evidence` raises `UnknownBatchIdError` when `verification["event_count"] == 0` — a mistyped or nonexistent `batch_id` used to silently produce a structurally valid, empty, `verified: true` package with nothing distinguishing "this batch genuinely has zero activity" from "you got the batch id wrong."

**The `scope` block:** every export carries per-table row counts (`audit_events_rows`, `reconciliation_results_rows`, `reconciliation_results_included`, `exception_rows`) and a deterministic `event_timestamp_range` anchor, alongside the wall-clock `exported_at` (kept wall-clock deliberately — it's a point-in-time export action, not part of the deterministic dataset). An empty section in the package reads as "this table genuinely had 0 rows for this batch," not "the export queried the wrong thing."

## CLI

```bash
civicpay audit verify --batch BATCH-001
civicpay audit verify                          # full chain
civicpay audit export --batch BATCH-001 --out evidence.json
civicpay audit export --batch BATCH-001 --out evidence.json --full
```

## Design notes & limitations

- **`payload=` on `AuditLedger.append()` is accepted but not hashed or persisted** (no column on `audit_event_log`) — reserved for a future ticket. Detail beyond `action`'s free-text string currently has nowhere authoritative to live; the v0.2 enrollment module's dual-source amounts live in their own table (`enrollment_dual_source_results`) instead, joined in by the dashboard, rather than waiting on this.
- **Concurrent writers are not addressed.** Single-operator local use (per the enrollment module's own REST-API-scope decision) means this hasn't needed hardening; the structural chain-tip fix above removes the specific ordering hazard that made it worse, but two truly concurrent appends racing to read "the current tip" before either writes is still a real, unaddressed risk if this were ever exposed to concurrent callers.
