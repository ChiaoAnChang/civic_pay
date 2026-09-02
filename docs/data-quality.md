# Data-Quality Module — Technical Documentation

**Ticket:** Spec Ticket 5 (user "Ticket 4") · **Status:** Implemented · **Tests:** 33

The data-quality (DQ) module runs configurable checks across the synthetic datasets, computes per-dataset quality scores, routes per-record failures into the exception queue, and emits tamper-evident audit events.

## Quality score

Per spec §12, the per-dataset quality score is:

\[
\text{quality\_score} = 100 \times \frac{\text{checked} - \text{failing}}{\text{checked}}
\]

aggregated across check **types** (completeness, accuracy, consistency, timeliness, anomaly) with configurable per-type weights (default equal; anomaly weighted 0.5). Empty datasets score 100 (no failing records).

## Check catalogue

Nine typed checks, dispatched by `(check_type, rule)` from [`config/dq_checks.yml`](../config/dq_checks.yml):

| Type | Rule | What it detects |
| --- | --- | --- |
| completeness | `null` | null/empty required fields |
| completeness | `count` | dataset below a minimum row count |
| accuracy | `range` | numeric field outside `[min, max]` |
| accuracy | `enum` | field value not in the allowed set |
| accuracy | `referential` | foreign-key value with no parent row |
| consistency | `available_le_current` | non-loan account with available balance > current |
| timeliness | `staleness` | record older than the SLA threshold (e.g. 30 days) |
| anomaly | `zscore` | value beyond a z-score threshold (defaults: abs(z) > 3) |
| anomaly | `iqr` | value beyond the Tukey IQR fence |

A typed configuration model (not a generic expression engine) keeps checks safe and testable; a generic evaluator is deferred to a later release.

## Exception routing

Per-record failures are routed to the `exception_queue` (source = `dq`), capped at `max_exceptions_per_check` (default 25). The **full** failing count is always stored in `dq_results.failing_records`; only the queue is capped. Anomaly checks default to `route_failures: false` (statistical outliers are informational). The `exception_queue.reference_id` encodes `"{dataset}:{record_id}"`; the `exception_id` is `EXC-{batch_id}-{seq}`.

## Configuration

```yaml
type_weights:
  anomaly: 0.5            # informational checks down-weighted
max_exceptions_per_check: 25
datasets:
  transactions:
    id_field: transaction_id
    checks:
      - check_name: "No null reference ids"
        type: completeness
        rule: null
        field: reference_id
        severity: high
      - check_name: "Amount non-negative"
        type: accuracy
        rule: range
        field: amount
        min: 0
        severity: high
      - check_name: "Account exists"
        type: accuracy
        rule: referential
        field: account_id
        ref_dataset: accounts
        ref_field: account_id
        severity: medium
        route_failures: true
      # ... timeliness, anomaly checks ...
```

## CLI

```bash
civicpay dq check                       # all datasets, batch DQ-001
civicpay dq check --dataset transactions
civicpay dq check --batch-id DQ-2026-09-01 --date 2026-09-01
```

## API

```python
from civicpay.quality.pipeline import run_dq

summary = run_dq(batch_id="DQ-001", as_of=as_of)
# summary keys: batch_id, datasets_checked, checks_run, checks_passed,
# checks_failed, total_failing_records, exceptions_routed, audit_events,
# per_dataset_scores
```

## Test results

```
tests/test_quality.py  33 passed
```

Coverage: scoring (5), completeness (4), accuracy (4), consistency (2), timeliness (2), anomaly (3), the `run_check` dispatcher (2), empty datasets (1), the end-to-end pipeline on synthetic data (7), known-defect injection (1), and CLI smoke (1). See [reconciliation.md](reconciliation.md) §10 for the full-suite execution results.

## Design notes & limitations

- **Determinism:** scores are deterministic given a fixed `--seed` and `--date`.
- **Re-runs:** `dq_results` is replaced per run; `audit_event_log` and `exception_queue` are append-only — use a fresh `--batch-id` per run (a pre-flight check blocks re-runs with a used `batch_id` and prints a clear error instead of a raw primary-key collision).
- **Synthetic data:** a genuinely-stale cohort (~3.5% of transactions, `STALE_FRACTION` in `civicpay/data/synthetic.py`) is seeded with `created_at` dates 45/60/90 days before the as-of date, so the timeliness check catches meaningful staleness rather than a date-range boundary artifact. (An earlier design let the timeliness check fire on Aug-1 postings that were only 31 days old vs the Sep-1 as-of date — 1 day over the 30-day threshold — which looked like a bug rather than a deliberate DQ finding.) The stale cohort sits beyond the payment-matching pools (exact/fuzzy/amount-mismatch), so it does not affect the reconciliation DoD counts, and `payment_records` stay fully within the 30-day window.
- **Anomaly checks** are z-score / IQR only; more sophisticated detectors (isolation forest, etc.) are deferred.
