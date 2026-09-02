# Payment Reconciliation — Technical Documentation

**Module:** `civicpay.recon` + `civicpay.audit` (spec §14.1 / Implementation Backlog Ticket 4)
**Status:** Implemented (v0.1). Audit-ledger **verification and export** are deferred to Ticket 7.

The reconciliation module matches an inbound payment file against ledger
transactions, classifies every payment record, and records a tamper-evident
audit trail. It is Layer 1 of the four-layer CivicPay methodology
(reconciliation → data-quality → exception workflow → audit evidence).

---

## 1. Overview

Reconciliation answers one question for every payment record: *which ledger
transaction, if any, does this payment correspond to?* The answer is a
classification into one of four buckets:

| `match_status` | Meaning |
| --- | --- |
| `matched` | The payment was linked to a ledger transaction (exact or fuzzy). |
| `unmatched_payment` | No ledger transaction corresponds to this payment. |
| `unmatched_ledger` | A ledger transaction no payment corresponds to (the inverse). |
| `exception` | A near-match that needs a human (duplicate, amount mismatch, stale). |

Every payment produces one `reconciliation_results` row. Every ledger
transaction that no payment consumed also produces one row
(`match_status = unmatched_ledger`, `payment_id = NULL`), so the table is a
complete bilateral view of the payment file vs. the ledger.

Every successful match and every opened exception emits an audit event into the
hash-chained `audit_event_log`. Unmatched rows do **not** emit events (they are
not state-changing actions).

---

## 2. Data model

Four DuckDB tables participate. The reconciliation module reads the first two
and writes the last two.

### `payment_records` (input)

Inbound payment file, one row per payment. Columns used by the matcher:
`payment_id`, `reference_id`, `amount`, `currency`, `payment_date`. The
pipeline also updates `status` (matched / unmatched / exception) after a run.

### `transactions` (input)

The ledger, one row per posted transaction. Columns used: `transaction_id`,
`reference_id`, `amount`, `currency`, `posting_date`.

### `reconciliation_results` (output)

| Column | Type | Description |
| --- | --- | --- |
| `recon_id` | VARCHAR PK | `RECON-{batch_id}-{seq}` for payment rows; `RECON-{batch_id}-L{seq}` for unmatched-ledger rows |
| `batch_id` | VARCHAR | Reconciliation batch identifier |
| `payment_id` | VARCHAR | The payment (NULL for unmatched-ledger rows) |
| `ledger_transaction_id` | VARCHAR | The matched ledger transaction (NULL for unmatched-payment) |
| `match_status` | VARCHAR | `matched` / `unmatched_payment` / `unmatched_ledger` / `exception` |
| `match_confidence` | DOUBLE | 1.0 for exact; the Levenshtein ratio for fuzzy; 0.0 otherwise |
| `match_method` | VARCHAR | `exact` / `fuzzy` / `manual` (NULL for non-matches) |
| `exception_reason` | VARCHAR | `duplicate` / `amount_mismatch` / `stale` / `missing_ref` (NULL otherwise) |
| `reconciled_at` | TIMESTAMP | Fixed as-of timestamp (deterministic) |
| `reconciled_by` | VARCHAR | Actor that ran the batch |

### `audit_event_log` (output, append-only)

| Column | Type | Description |
| --- | --- | --- |
| `event_id` | VARCHAR PK | `EVT-{batch_id}-{seq}` |
| `timestamp` | TIMESTAMP | Event time (UTC, fixed for determinism) |
| `event_type` | VARCHAR | `match` / `exception_open` (others reserved for later tickets) |
| `actor` | VARCHAR | Who/what performed the action |
| `entity_type` | VARCHAR | `reconciliation` / `exception` |
| `entity_id` | VARCHAR | The `recon_id` of the related result row |
| `action` | VARCHAR | `match` / `exception_open` |
| `previous_hash` | VARCHAR | SHA-256 of the previous event (empty for the first) |
| `event_hash` | VARCHAR | SHA-256 over this event's stored fields (see §4) |

### Enumerations

```
MatchStatus     matched · unmatched_payment · unmatched_ledger · exception
MatchMethod     exact · fuzzy · manual
ExceptionReason duplicate · amount_mismatch · stale · missing_ref
PaymentStatus   matched · unmatched · exception
AuditEventType  match · exception_open · (exception_resolve · ingest · dq_check · config_change)
```

---

## 3. Matching algorithm

The matcher (`civicpay.recon.matcher.reconcile`) is a **pure function** of its
inputs — no I/O, no wall-clock, no global state — which makes it fully
deterministic and trivially testable. Payments are processed in file order.
Classification is evaluated per payment in five branches; the first branch that
fires wins.

### Reference normalization

Before any comparison, reference ids are normalized:

1. Strip whitespace.
2. Uppercase.
3. Remove separators (`-`, `_`, whitespace).

`REF-M-0001` → `REFM0001`; `ref_t_0002` → `REFT0002`. Normalization is applied
to both payment and ledger references, so `REF-M-0001` and `ref m 0001` are
treated as the same reference.

### Classification branches

Let *candidates* be the ledger entries sharing the payment's normalized
reference. A ledger entry is **consumed** once it has been matched to a payment.

1. **`matched` / `exact`** — An *unconsumed* candidate whose currency matches,
   amount is within `amount_tolerance`, and posting date is within
   `date_window_days`. The ledger entry is consumed. Confidence 1.0.

2. **`exception` / `duplicate`** — A *consumed* candidate whose currency,
   amount, and date all agree. The payment is a plausible duplicate of an
   already-matched payment. The ledger entry stays consumed (not re-consumed).

3. **`exception` / `amount_mismatch`** — The reference matches candidate(s),
   but **no** candidate's amount is within tolerance (the amount is genuinely
   wrong). The ledger entry is **not** consumed. If some candidate's amount is
   within tolerance but the date or currency is off, this branch does *not* fire
   and the payment falls through to fuzzy matching.

4. **`matched` / `fuzzy`** — No exact reference exists. Over the *unconsumed*
   ledger, candidates are pre-filtered by currency, amount tolerance, and date
   window; among the survivors, the one with the highest normalized Levenshtein
   ratio ≥ `fuzzy_threshold` is matched. The ledger entry is consumed. Confidence
   is the ratio.

5. **`unmatched_payment`** — No match by reference or fuzzy similarity. If the
   payment is older than `stale_days` relative to the as-of date, it is
   upgraded to `exception` / `stale` so it is not silently dropped.

Ledger entries never matched by any payment become `unmatched_ledger` rows
(assembled by the pipeline, not the matcher).

### Why fuzzy uses the same tolerance as exact

A fuzzy match is a **reference-similarity** match, not a looser amount/date
match. The amount and date must still agree within the normal tolerance; only
the reference id is allowed to be garbled. The matcher therefore pre-filters
fuzzy candidates with the *same* `amount_tolerance` and `date_window_days` used
for exact matching.

> **Tuning note.** Widening the fuzzy amount/date filters beyond the exact
> tolerance is possible but increases the candidate pool and the number of
> near-equal string-similarity ties (rapidfuzz scores two character
> substitutions on equal-length strings as 0.90), which can cause ambiguous
> cross-matches. If you loosen these filters, add stronger deterministic
> tie-breakers and regression tests covering the fuzzy bucket.

---

## 4. Audit ledger core

`civicpay.audit.ledger` provides the append-only, hash-chained primitives
shared by reconciliation (now) and the full audit-evidence layer (Ticket 7).

### Canonical serialization

`canonical_json(payload)` produces a stable JSON string:

- Object keys are sorted lexicographically.
- Timestamps are UTC, ISO-8601 (naive datetimes are treated as UTC).
- `Decimal` values are serialized as plain strings (no float re-encoding drift).

The same payload always yields the same string regardless of dict insertion
order or Python run.

### Content hash and chain

`compute_event_hash(payload)` is the SHA-256 hex digest of
`canonical_json(payload)`. The hashed payload comprises **exactly the fields
persisted to `audit_event_log`** — `event_id, timestamp, event_type, actor,
entity_type, entity_id, action, previous_hash` — so a verifier can recompute
every `event_hash` from the stored rows alone. The helper
`event_body_from_row(row)` reconstructs the hashable body from a persisted row.

Each event stores the previous event's `event_hash` in `previous_hash` (empty
for the first event), so any alteration anywhere in the chain invalidates every
subsequent hash.

### Scope

This ticket implements **append** only: `AuditLedger.append()` writes one
hash-chained event immediately; `AuditLedger.append_many()` builds a batch via
an internal row-builder and persists it in one write. **Verification** (detecting
a broken/tampered chain) and **evidence export** are the audit-evidence ticket's
job (Ticket 7) and are intentionally not implemented here. Tests recompute hashes
directly from stored rows to assert the chain property that Ticket 7 will rely on.

---

## 5. Configuration

`config/recon.yml`:

```yaml
# Max |payment.amount - ledger.amount| for an amount match (exact and fuzzy).
amount_tolerance: 1.0

# Max |payment.date - ledger.posting_date| in days (exact and fuzzy).
date_window_days: 1

# Min normalized Levenshtein similarity (0..1) for a fuzzy reference match.
fuzzy_threshold: 0.85

# An unmatched payment older than this (days, vs as-of) becomes a stale exception.
stale_days: 30
```

Defaults are calibrated against the synthetic sample file (seed=42) so every
outcome class is exercised. Real institutions should tune these to their own
tolerance policy.

### Key aliases

The loader also accepts the spec's original key names, so either form works:

| Clear name (recommended) | Spec alias |
| --- | --- |
| `amount_tolerance` | `match_tolerance_amount` |
| `date_window_days` | `match_date_window_days` |

`fuzzy_threshold` and `stale_days` use a single name.

---

## 6. CLI usage

```bash
# 1. Generate deterministic synthetic data and load into DuckDB
civicpay seed --seed 42

# 2. Run payment reconciliation against the ledger
civicpay recon run
```

### `civicpay recon run` options

| Flag | Default | Description |
| --- | --- | --- |
| `--file` | *(none)* | Optional payment CSV to ingest into `payment_records` before reconciling |
| `--file-id` | *(none)* | Reconcile only this payment file id |
| `--date` | `2026-09-01` | As-of date (YYYY-MM-DD) for stale detection |
| `--batch-id` | `BATCH-001` | Reconciliation batch id (controls `recon_id` / `event_id`) |
| `--db-path` | `data/processed/civicpay.duckdb` | DuckDB database path |
| `--config` | `config/recon.yml` | Reconciliation config YAML |

### Example output

```
          Reconciliation — BATCH-001
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━┓
┃ Metric                      ┃        Value ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━┩
│ Payments Processed          │        1,000 │
│ Ledger Records Processed    │       50,000 │
│ Matched Exact               │          850 │
│ Matched Fuzzy               │           40 │
│ Matched Total               │          890 │
│ Unmatched Payment           │           80 │
│ Exception Duplicate         │           20 │
│ Exception Amount Mismatch   │           10 │
│ Exception Stale             │            0 │
│ Exception Total             │           30 │
│ Unmatched Ledger            │       49,110 │
│ Reconciliation Results Rows │       50,110 │
│ Audit Events                │          920 │
│ Total Amount Reconciled     │ 1,091,103.54 │
│ Reconciliation Rate         │       0.8900 │
└─────────────────────────────┴──────────────┘
```

---

## 7. Programmatic API

### Pure matcher

```python
from civicpay.recon.matcher import ReconConfig, reconcile

results, ledger, summary = reconcile(
    payments=payments_df,  # pandas.DataFrame
    transactions=transactions_df,  # pandas.DataFrame
    config=ReconConfig(),
    batch_id="BATCH-001",
)
# results: list[MatchResult] — one per payment
# ledger:  LedgerIndex — .consumed holds matched transaction_ids
# summary: dict with matched_exact, matched_fuzzy, unmatched_payment, ...
```

The matcher performs no I/O. Use it directly for testing or custom pipelines.

### Full pipeline

```python
from civicpay.recon.pipeline import ReconciliationPipeline
from civicpay.storage.duckdb import DuckDBStore

store = DuckDBStore("data/processed/civicpay.duckdb")
summary = ReconciliationPipeline(store=store).run(batch_id="BATCH-001")
# - reads payment_records + transactions from DuckDB
# - runs the matcher
# - appends unmatched_ledger rows (one per unconsumed ledger transaction)
# - writes reconciliation_results (REPLACE)
# - emits 890 match + 30 exception_open audit events
# - updates payment_records.status
# - prints a rich summary table
store.close()
```

Or use the convenience entry point:

```python
from civicpay.recon.pipeline import run_recon

summary = run_recon(db_path="…", config_path="config/recon.yml", batch_id="BATCH-001")
```

---

## 8. Determinism guarantees

A reconciliation run is reproducible given the same inputs:

- **Fixed as-of timestamp.** All `reconciled_at` and audit `timestamp` values use
  the synthetic data's `AS_OF_DATETIME` (2026-09-01 00:00:00 UTC), never wall-clock.
- **Seed-driven data.** `civicpay seed --seed 42` always produces identical
  payment and ledger data.
- **Caller-supplied batch id.** `batch_id` controls `recon_id` and `event_id`
  sequences, so the same batch over the same data yields identical identifiers.

Two independent runs over freshly seeded stores produce byte-identical
`reconciliation_results` CSV. The test suite asserts this.

---

## 9. Synthetic dataset expected outcomes (seed=42)

The sample payment file is constructed in five buckets so every outcome class is
exercised and visible in the demo:

| Outcome | Count |
| --- | --- |
| matched / exact | 850 |
| matched / fuzzy | 40 |
| unmatched_payment | 80 |
| exception / duplicate | 20 |
| exception / amount_mismatch | 10 |
| exception / stale | 0 |
| unmatched_ledger | 49,110 |
| **reconciliation_results rows** | **50,110** |
| **audit events** | **920** (890 match + 30 exception_open) |
| reconciliation_rate | 0.8900 |

The synthetic data's as-of date (2026-09-01) and `stale_days=30` keep all 80
unmatched payments within the stale window, so none are escalated.

---

## 10. Testing

```bash
pytest tests/test_recon.py -q          # reconciliation + audit ledger tests
pytest tests/test_recon_boundary.py -q  # boundary-condition suite
pytest -q                              # full suite (72 tests)
ruff check . && ruff format --check .
```

Coverage:

**Happy-path & outcomes** (`test_recon.py`): reference normalization, config
tolerances + spec aliases, `canonical_json` determinism + UTC forcing, pure
matcher outcome counts (all buckets), full pipeline row/audit/status checks,
hash-chain recomputability from stored rows, determinism, CLI smoke.

**Boundary conditions** (`test_recon_boundary.py`):

- Tolerance edges: amount diff exactly at tolerance (matches), one cent over
  (amount_mismatch), zero tolerance (exact amount required).
- Date-window edges: diff at the window (matches), beyond the window falls
  through to unmatched (not amount_mismatch).
- Fuzzy-threshold edge: a 1-substitution 7-char reference (ratio 0.857) matches
  at threshold 0.85 but not at 0.86; fuzzy still requires amount + date within
  tolerance.
- Stale edge: age exactly `stale_days` is not stale; one day over is.
- Currency mismatch with a matching reference.
- Multi-candidate same reference (two ledger entries, two payments both exact).
- Duplicate detected only after the original is consumed.
- Empty datasets: no payments raises; no ledger yields all unmatched.
- Single exact match.
- Audit ledger: empty `append_many` is a no-op; the chain resumes across
  `append` + `append_many` calls (previous_hash links).
- `canonical_json` over Decimal, set, nested dict, and tz-aware datetime.
- Tamper detection: altering a stored field makes the recomputed hash no longer
  match the stored value (the property Ticket 7's verifier relies on).

### Test execution results

A full run of the suite (recorded 2026-09-01, Python 3.11, seed 42, defaults):

```
$ pytest -q
........................................................................ [100%]
72 passed in 107.16s

$ ruff check .
All checks passed!
```

Per-file breakdown of the reconciliation tests:

| File | Tests | Focus |
| --- | --- | --- |
| `tests/test_recon.py` | 20 | happy-path, outcome counts, pipeline, determinism, CLI smoke |
| `tests/test_recon_boundary.py` | 29 | tolerance / window / fuzzy / stale edges, currency, multi-candidate, empty datasets, audit chain, tamper detection |
| **Reconciliation subtotal** | **49** | |
| `tests/test_synthetic.py` | 11 | synthetic data determinism + buckets |
| `tests/test_storage.py` | 8 | DuckDB schema + read/write |
| `tests/test_cli.py` | 4 | `civicpay seed` + `civicpay recon run` |
| **Full suite** | **72** | all green, ruff clean |

The 49 reconciliation tests are enumerated below. Boundary tests use small,
controlled DataFrames (not the 50,000-row synthetic set) so each edge is
isolated and deterministic.

<details>
<summary>Enumerated reconciliation test cases</summary>

`tests/test_recon.py`:

- `test_normalize_reference` — separators, case, whitespace
- `test_recon_config_defaults`
- `test_recon_config_tolerances`
- `test_canonical_json_determinism`
- `test_canonical_json_forces_utc`
- `test_config_key_aliases` — `match_tolerance_amount` / `match_date_window_days`
- `test_compute_event_hash_stable`
- `test_hash_chain_links_and_recomputable_from_stored_rows`
- `test_append_persists_one_event`
- `test_reconcile_outcome_counts` — all DoD buckets
- `test_reconcile_match_status_distribution`
- `test_pipeline_writes_reconciliation_results` — 50,110 rows
- `test_pipeline_unmatched_ledger_count` — 49,110
- `test_pipeline_audit_events` — 920 (890 match + 30 exception_open)
- `test_pipeline_updates_payment_status`
- `test_pipeline_reconciliation_rate` — 0.8900
- `test_pipeline_determinism` — byte-identical results across runs
- `test_pipeline_file_id_filter`
- `test_recon_cli_run_end_to_end`

`tests/test_recon_boundary.py`:

- `test_normalize_reference_boundaries` — None, already-normalized, separators-only, tabs/newlines (parametrized, 6 cases)
- `test_within_amount_inclusive_at_tolerance`
- `test_within_date_window_inclusive_at_window`
- `test_is_stale_strictly_greater_than_stale_days`
- `test_amount_diff_exactly_at_tolerance_matches`
- `test_amount_diff_just_over_tolerance_is_mismatch`
- `test_zero_tolerance_requires_exact_amount`
- `test_date_diff_at_window_matches`
- `test_date_diff_beyond_window_falls_through_to_unmatched`
- `test_fuzzy_at_threshold_matches_and_above_does_not` — ratio 0.857 at threshold 0.85 / 0.86
- `test_fuzzy_requires_amount_and_date_within_tolerance`
- `test_stale_boundary_at_exactly_stale_days_is_not_stale`
- `test_stale_one_day_over_is_exception`
- `test_currency_mismatch_with_matching_ref_is_flagged`
- `test_multiple_ledger_entries_same_reference_all_match`
- `test_duplicate_after_exact_consumption_is_flagged_duplicate`
- `test_empty_payments_raises_in_pipeline`
- `test_no_transactions_all_payments_unmatched`
- `test_single_exact_match`
- `test_append_many_empty_is_noop`
- `test_chain_resumes_across_append_calls`
- `test_chain_resumes_across_append_then_append_many`
- `test_canonical_json_handles_decimal_set_nested_and_tz`
- `test_tampered_event_hash_detected_on_recompute`

</details>

### How to reproduce

```bash
# from the repo root, after installing the package (pip install -e .)
pytest tests/test_recon.py tests/test_recon_boundary.py -v   # the 49 reconciliation tests
pytest -q                                                   # full suite (72)
ruff check . && ruff format --check .                      # lint clean
```

The outcome-class counts asserted in `test_reconcile_outcome_counts` match the
synthetic dataset table in §9 (seed 42, defaults): matched_exact 850,
matched_fuzzy 40, unmatched_payment 80, exception_duplicate 20,
exception_amount_mismatch 10, exception_stale 0, unmatched_ledger 49,110,
reconciliation_results rows 50,110, audit_events 920.

---

## 11. Result inspection (DuckDB SQL)

After a run, inspect results directly:

```sql
-- Outcome counts by match status
SELECT match_status, COUNT(*) AS n
FROM reconciliation_results
GROUP BY match_status
ORDER BY n DESC;

-- Open exceptions needing review
SELECT recon_id, payment_id, ledger_transaction_id, exception_reason
FROM reconciliation_results
WHERE match_status = 'exception'
ORDER BY exception_reason;

-- Unmatched ledger (payments never received for these transactions)
SELECT ledger_transaction_id, COUNT(*) AS n
FROM reconciliation_results
WHERE match_status = 'unmatched_ledger'
LIMIT 20;

-- Audit events for a batch
SELECT event_id, event_type, entity_id, action
FROM audit_event_log
ORDER BY timestamp, event_id
LIMIT 20;
```

---

## 12. Operational behavior & reruns

- **`reconciliation_results` is replaced each run** (`mode = replace`). Each
  `civicpay recon run` overwrites the previous batch's results for a clean,
  reproducible view.
- **`audit_event_log` is append-only.** Each run appends new events; the hash
  chain resumes from the most recent persisted `event_hash` (empty if the log
  is empty).
- **`payment_records.status` is updated** to `matched` / `unmatched` /
  `exception` based on the run's outcome.
- **`batch_id` controls identifiers.** Reusing a `batch_id` produces the same
  `recon_id` / `event_id` sequence over the same data; use distinct batch ids to
  keep runs separable in the audit log.

---

## 13. Design notes & clean-room provenance

The matching algorithm, thresholds, schema shapes, and synthetic data
distributions are **original clean-room design** — derived from public
regulatory requirements, published data-engineering patterns (dimensional
modeling, ETL/ELT, reconciliation matching, DAMA-DMBOK-style data-quality
framing), and the beneficiary's professional experience with enterprise-scale
financial data systems. They are **not** derived from any employer's proprietary
systems, code, table names, dashboards, thresholds, or workflow definitions.
See `PROVENANCE.md` for the full clean-room statement.

---

## 14. Limitations & future work

- **Audit verification/export** (detecting a tampered chain; JSON evidence
  packages for a date window) is the audit-evidence ticket (Ticket 7). This
  ticket writes a valid, verifiable chain but does not yet verify or export it.
- **Exception workflow** (priority ranking, SLA aging, resolution) is Ticket 6.
  Reconciliation opens exceptions; the workflow module will manage them.
- **dbt marts** (recon summary, DQ summary, exception aging as formal models)
  are the v0.2 milestone (Ticket 9). v0.1 results are queryable directly in
  DuckDB.
- **Fuzzy tie-breaking.** Fuzzy matching selects the highest similarity with a
  stable first-encountered-wins rule. Loosening the amount/date filters requires
  stronger deterministic tie-breakers (see the tuning note in §3).
