# Methodology

The CivicPay Open Framework organizes financial-data-governance into four layers:

1. **Payment Reconciliation** — match inbound payment files against ledger
   entries; classify every record as matched, unmatched-payment,
   unmatched-ledger, or exception. Exact matching (reference + amount + date
   window) combined with fuzzy matching for near-matches; flags duplicates and
   amount mismatches.

2. **Data-Quality Monitoring** — five dimensions: completeness, accuracy,
   consistency, timeliness, anomaly. Produces a per-dataset quality score.

3. **Exception Workflow** — priority-ranked queue, SLA aging, resolution with
   root-cause capture. Ensures no flagged item is silently dropped.

4. **Audit-Evidence Layer** — append-only, hash-chained (tamper-evident) event
   log; exportable evidence packages for auditors and examiners.

Together they form a continuous loop: ingest -> match -> detect defects ->
resolve exceptions -> evidence everything.

The methodology is informed by professional experience with enterprise-scale
financial data systems and implemented from public regulatory needs, generic
data-engineering patterns, and original clean-room design. It is not derived
from any employer's proprietary methodology. See PROVENANCE.md.
