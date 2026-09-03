{#
    Faithful SQL port of civicpay.dashboard.extractors.reconciliation_summary
    (see docs/dbt.md). reconciliation_rate is computed over payment-side rows
    only (match_status != 'unmatched_ledger'); ledger_coverage_rate is the
    separate, full-ledger metric — kept apart because blending them produced
    a real bug: the same metric name meaning two very
    different numbers depending on which rows it was computed over.
#}

with recon as (
    select * from {{ ref('stg_reconciliation_results') }}
),

payment_side as (
    select *
    from recon
    where match_status != 'unmatched_ledger'
),

per_batch_payment as (
    select
        batch_id,
        count(*) as total,
        sum(case when match_status = 'matched' then 1 else 0 end) as matched
    from payment_side
    group by batch_id
),

per_batch_ledger as (
    select
        batch_id,
        count(*) as ledger_total,
        sum(case when match_status = 'unmatched_ledger' then 1 else 0 end) as unmatched_ledger,
        max(reconciled_at) as reconciled_at
    from recon
    group by batch_id
)

select
    l.batch_id,
    coalesce(p.total, 0) as total,
    coalesce(p.matched, 0) as matched,
    coalesce(p.total, 0) - coalesce(p.matched, 0) as exceptions,
    round(100.0 * coalesce(p.matched, 0) / nullif(p.total, 0), 2) as reconciliation_rate,
    l.ledger_total,
    l.unmatched_ledger,
    round(100.0 * (l.ledger_total - l.unmatched_ledger) / nullif(l.ledger_total, 0), 2)
        as ledger_coverage_rate,
    l.reconciled_at
from per_batch_ledger l
left join per_batch_payment p using (batch_id)
order by l.reconciled_at desc
