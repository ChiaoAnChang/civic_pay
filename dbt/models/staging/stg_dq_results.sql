select
    dq_check_id,
    dataset_name,
    check_type,
    check_name,
    passed,
    failing_records,
    quality_score,
    checked_at
from {{ source('civicpay_raw', 'dq_results') }}
