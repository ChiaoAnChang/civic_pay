{#
    Faithful SQL port of civicpay.quality.scoring.dataset_quality_score +
    anomaly_rate, as assembled per-dataset by
    civicpay.dashboard.extractors.dq_dataset_scores. dq_results is replaced
    wholesale on every `civicpay dq check` run (not appended), so grouping by
    dataset_name alone — with no batch/run filter — reflects the latest run
    only, matching that Python behavior exactly.

    Per-check-type weights mirror config/dq_checks.yml's `type_weights`,
    duplicated as dbt vars (see dbt_project.yml) since dbt can't read that
    YAML at compile time. anomaly defaults to weight 0.0: anomaly checks flag
    a small tail of records by construction, so folding them into the
    average would inflate the score rather than reflect real defects — see
    anomaly_rate below for that number reported on its own instead.
#}

with dq as (
    select * from {{ ref('stg_dq_results') }}
),

per_type as (
    select
        dataset_name,
        check_type,
        avg(quality_score) as type_score
    from dq
    group by dataset_name, check_type
),

weighted as (
    select
        dataset_name,
        type_score,
        case check_type
            when 'completeness' then {{ var('dq_type_weight_completeness') }}
            when 'accuracy' then {{ var('dq_type_weight_accuracy') }}
            when 'consistency' then {{ var('dq_type_weight_consistency') }}
            when 'timeliness' then {{ var('dq_type_weight_timeliness') }}
            when 'anomaly' then {{ var('dq_type_weight_anomaly') }}
            else 1.0
        end as type_weight
    from per_type
),

dataset_score as (
    select
        dataset_name,
        round(sum(type_score * type_weight) / nullif(sum(type_weight), 0), 4) as quality_score
    from weighted
    group by dataset_name
),

dataset_anomaly_rate as (
    select
        dataset_name,
        round(100.0 - avg(quality_score), 4) as anomaly_rate
    from dq
    where check_type = 'anomaly'
    group by dataset_name
),

totals as (
    select
        dataset_name,
        count(*) as checks_run,
        sum(case when passed then 1 else 0 end) as checks_passed,
        sum(case when not passed then 1 else 0 end) as checks_failed,
        sum(failing_records) as total_failing_records,
        max(checked_at) as checked_at
    from dq
    group by dataset_name
)

select
    t.dataset_name,
    coalesce(d.quality_score, 100.0) as quality_score,
    a.anomaly_rate,
    t.checks_run,
    t.checks_passed,
    t.checks_failed,
    t.total_failing_records,
    t.checked_at
from totals t
left join dataset_score d using (dataset_name)
left join dataset_anomaly_rate a using (dataset_name)
order by t.dataset_name
