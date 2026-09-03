{#
    Passthrough, not cleaning. Column shape/types are already enforced by
    civicpay.storage.duckdb's DDL and validated upstream by the
    quality module (see docs/data-quality.md) — this staging layer exists to
    give marts a stable interface boundary onto raw tables, not to
    re-implement cleaning dbt doesn't need to own.
#}
select
    transaction_id,
    account_id,
    transaction_type,
    amount,
    currency,
    posting_date,
    value_date,
    reference_id,
    created_at,
    status
from {{ source('civicpay_raw', 'transactions') }}
