{#
    reference_id encodes "{dataset}:{record_id}" (see
    civicpay.exceptions.queue._AMOUNT_DATASETS) — split here once so every
    downstream mart reads the two parts as plain columns instead of
    re-parsing the string.
#}
select
    exception_id,
    source,
    reference_id,
    split_part(reference_id, ':', 1) as ref_dataset,
    split_part(reference_id, ':', 2) as ref_record_id,
    priority,
    assigned_to,
    status,
    created_at,
    resolved_at,
    resolution_notes,
    root_cause
from {{ source('civicpay_raw', 'exception_queue') }}
