# Exception Workflow — Technical Documentation

**Ticket:** Spec Ticket 6 (user "Ticket 5") · **Status:** Implemented · **Tests:** see [reconciliation.md](reconciliation.md) §10 for full-suite results

The exception workflow manages every flagged item — from reconciliation, data quality, and (v0.2) enrollment dual-source mismatches — end to end: prioritize, age against SLA, resolve, capture root cause. `civicpay.exceptions.workflow` holds the pure scoring functions; `civicpay.exceptions.queue.ExceptionManager` reads and updates the shared `exception_queue` table against a `DuckDBStore`.

## Priority formula

```
priority_score = severity_weight × amount_at_risk_factor × age_factor
```

- **`severity_weight`** — high=3, medium=2, low=1, from the queue item's `priority` field.
- **`amount_at_risk_factor`** — buckets the at-risk dollar amount: <100→1, 100–999→2, 1k–9.9k→3, ≥10k→4. Resolved by looking up the referenced record's amount (see `_AMOUNT_DATASETS` below); 0 when no amount applies (e.g. an accounts/customers DQ exception).
- **`age_factor`** — 1.0 while within the SLA window, then +0.5 per day overdue.

Priority is computed **on read**, not persisted, so it never goes stale — but that also means it is a non-authoritative triage aid, not an audit record. The auditable fact for a resolved exception is its `exception_resolve` ledger event, not the priority score at the moment it happened to be viewed.

## SLA windows are per-severity, not a single global value

```python
SLA_DAYS_BY_SEVERITY = {"high": 3, "medium": 7, "low": 14}
```

`age_factor`'s SLA threshold resolves per-severity by default (`resolve_sla_days(priority, sla_days=None)`) — a high-severity item starts escalating after 3 days, a low-severity one after 14. This replaced a single flat 7-day SLA for every exception regardless of severity (OPEN_QUESTIONS §F): severity already scales *how urgent* an escalated item looks (3×/2×/1×), but a single SLA meant every exception escalated at the same day count regardless of severity — the one place this module visibly departed from the real triage practice (P1/P2/P3-style response windows) it's meant to demonstrate.

`--sla-days` (CLI) / `sla_days=` (`ExceptionManager.list`, `dashboard.exception_queue`) still exists as an explicit override — pass a value to apply the same window to every item instead. The resolved window is exposed per-item as a `sla_days` field (CLI table, dashboard) so which window applied to a given item is visible, not implicit.

## amount_at_risk resolution

```python
_AMOUNT_DATASETS: dict[str, tuple[str, str, str]] = {
    "transactions": (M.Transaction.TABLE, "transaction_id", "amount"),
    "payment_records": (M.PaymentRecord.TABLE, "payment_id", "amount"),
    "pending_enrollments": (M.PendingEnrollment.TABLE, "enrollment_id", "incentive_amount"),
}
```

`reference_id` encodes `"{dataset}:{record_id}"`; `_amount_at_risk` looks up `(table, id_field, amount_field)` and coerces to `float()` (some amount columns — `pending_enrollments.incentive_amount` — are intentionally text, not numeric, since that table holds unvalidated intake data; see `docs/ai-implementation-backlog.md` for why). Adding a new exception-producing dataset means adding one entry here.

## The demo backlog cohort (why exceptions don't all show age_days=0)

Both `QualityPipeline` (DQ) and `EnrollmentPipeline` (v0.2 enrollment) seed a small, deterministic cohort of **already-aged** exceptions once per fresh database (`BACKLOG_BATCH_ID` in `civicpay/quality/pipeline.py`, `_BACKLOG_ENTITY_PREFIX` in `civicpay/enrollment/pipeline.py`) — real failing records (or, for enrollment, records deliberately constructed to genuinely diverge), with `created_at` backdated by a fixed number of days, so SLA escalation is visible in a plain `run-all` or the dashboard without waiting on wall-clock time (OPEN_QUESTIONS §G).

**Why not just use wall-clock `now()` for `created_at` instead:** that would make every run's exception ages non-reproducible, which is worse for a portfolio/reference artifact than a small seeded backlog. The genuine detection path (a *fresh* DQ or enrollment run) always stamps `created_at = as_of` — real detections happen "now," in the deterministic simulated present. Only the seeded backlog is deliberately backdated, and only its `created_at` field — the paired audit event's own `timestamp` still uses `as_of` (see [audit.md](audit.md) for why: keeping ledger timestamps consistent avoids a chain-resume hazard).

Seeded once, gated by `batch_id_in_use` on the backlog's own fixed batch id — a re-run against the same database sees it's already there and skips reseeding, so it neither duplicates nor resurrects an already-resolved demo item.

## Resolution

`ExceptionManager.resolve(exception_id, root_cause, actor="system", resolution_notes=None)`:

- Marks the item `resolved`, captures `root_cause` (required) and optional `resolution_notes`.
- Emits a unique `exception_resolve` audit event with `batch_id = f"EXC-RESOLVE-{exception_id}"` (a fresh batch id per resolution, so resolving many items never collides on the append-only audit log's primary keys).
- Raises `ValueError` on an already-resolved or missing exception id (tested).

**Reused, not extended, for the v0.2 enrollment 3-way decision.** The enrollment dashboard's "Accept Path A / Accept Path B / Reject & re-enter" resolution (`civicpay.enrollment.pipeline.resolve_enrollment_mismatch`) does **not** add a parallel resolution method or widen `resolve`'s signature — there's no dedicated column for a 3-way outcome on the shared exception schema, so the decision is encoded into `resolution_notes` (`"decision=accept_a"` etc.), the same trade-off `root_cause` already makes for every exception type. `resolve_enrollment_mismatch` completes the enrollment itself (writing `accepted_enrollments` with the chosen path's amount, when there's a source `pending_enrollments` row to complete) *before* delegating to `ExceptionManager.resolve` for the actual state change and audit event.

## CLI

```bash
civicpay exception list --status open
civicpay exception list --sla-days 5          # override: same window for every item
civicpay exception resolve --id EXC-DQ-001-000001 --root-cause "stale upstream feed"
```

## Design notes & limitations

- **No schema extension.** Priority is computed on read so it never goes stale; if a dashboard/mart later needs a persisted score, a view/mart can be added without touching this module.
- **Per-check-type SLA config** (rather than per-severity) remains deferred — a real enhancement, not needed for the demo (OPEN_QUESTIONS §F).
- **Enrollment mismatches are the second real producer** of `exception_queue` rows — `ReconciliationPipeline` never writes to it directly (it only emits audit events for its own exception classifications); `QualityPipeline` and `EnrollmentPipeline` do.
