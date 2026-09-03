{#
    incentive_amount is intentionally raw TEXT in the source table
    (unvalidated intake — see civicpay.data.models.PendingEnrollment) so it
    is not cast here; mart_exception_aging try_casts it defensively at the
    point of use, mirroring how civicpay.exceptions.queue resolves it.
#}
select
    enrollment_id,
    entity_id,
    program_code,
    enrollment_date,
    incentive_amount,
    term_months,
    region,
    submitted_by,
    status,
    created_at
from {{ source('civicpay_raw', 'pending_enrollments') }}
