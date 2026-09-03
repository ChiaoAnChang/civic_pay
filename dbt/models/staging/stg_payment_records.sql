select
    payment_id,
    file_id,
    reference_id,
    amount,
    currency,
    direction,
    counterparty,
    payment_date,
    expected_posting_date,
    status
from {{ source('civicpay_raw', 'payment_records') }}
