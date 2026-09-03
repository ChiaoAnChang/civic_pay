{#
    Faithful SQL port of civicpay.exceptions.workflow (priority_score =
    severity_weight x amount_at_risk_factor x age_factor) and
    civicpay.exceptions.queue._amount_at_risk's dataset lookup. See
    docs/dbt.md and docs/exceptions.md for the Python original this mirrors,
    including the None-vs-0.0 and NaN edge cases.

    age_days matches civicpay.exceptions.workflow.age_days exactly: whole
    days from floor(total_seconds / 86400), not a calendar-date subtraction
    (those disagree whenever created_at has a nonzero time-of-day component).
#}

with exc as (
    select
        exception_id,
        source,
        reference_id,
        ref_dataset,
        ref_record_id,
        priority,
        status,
        created_at,
        resolved_at
    from {{ ref('stg_exception_queue') }}
),

amounts as (
    select
        e.exception_id,
        case e.ref_dataset
            when 'transactions' then t.amount
            when 'payment_records' then p.amount
            when 'pending_enrollments' then try_cast(pe.incentive_amount as double)
            else null
        end as amount_at_risk
    from exc e
    left join {{ ref('stg_transactions') }} t
        on e.ref_dataset = 'transactions' and e.ref_record_id = t.transaction_id
    left join {{ ref('stg_payment_records') }} p
        on e.ref_dataset = 'payment_records' and e.ref_record_id = p.payment_id
    left join {{ ref('stg_pending_enrollments') }} pe
        on e.ref_dataset = 'pending_enrollments' and e.ref_record_id = pe.enrollment_id
),

scored as (
    select
        e.exception_id,
        e.source,
        e.reference_id,
        e.priority,
        e.status,
        e.created_at,
        e.resolved_at,
        a.amount_at_risk,
        -- NaN is its own falsy self-inequality, same guard as
        -- civicpay.exceptions.workflow.amount_at_risk_factor.
        case
            when a.amount_at_risk is null or a.amount_at_risk != a.amount_at_risk then 'n/a'
            else 'amount'
        end as amount_basis,
        case lower(e.priority)
            when 'high' then 3.0
            when 'medium' then 2.0
            when 'low' then 1.0
            else 1.0
        end as severity_weight,
        case lower(e.priority)
            when 'high' then 3
            when 'medium' then 7
            when 'low' then 14
            else 7
        end as sla_days,
        greatest(
            0,
            cast(floor(
                date_diff('second', e.created_at, timestamp '{{ var("as_of_date") }} 00:00:00')
                / 86400.0
            ) as integer)
        ) as age_days
    from exc e
    left join amounts a using (exception_id)
),

factored as (
    select
        *,
        case
            when amount_at_risk is null or amount_at_risk != amount_at_risk then 2.5
            when amount_at_risk < 100 then 1.0
            when amount_at_risk < 1000 then 2.0
            when amount_at_risk < 10000 then 3.0
            else 4.0
        end as amount_at_risk_factor,
        case
            when age_days <= sla_days then 1.0
            else round(1.0 + 0.5 * (age_days - sla_days), 4)
        end as age_factor
    from scored
)

select
    exception_id,
    source,
    reference_id,
    priority,
    status,
    created_at,
    resolved_at,
    round(amount_at_risk, 2) as amount_at_risk,
    amount_basis,
    severity_weight,
    sla_days,
    age_days,
    age_factor,
    amount_at_risk_factor,
    round(severity_weight * amount_at_risk_factor * age_factor, 4) as priority_score
from factored
order by priority_score desc
