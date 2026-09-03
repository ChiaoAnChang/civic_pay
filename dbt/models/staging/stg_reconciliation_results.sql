select
    recon_id,
    batch_id,
    payment_id,
    ledger_transaction_id,
    match_status,
    match_confidence,
    match_method,
    exception_reason,
    reconciled_at,
    reconciled_by
from {{ source('civicpay_raw', 'reconciliation_results') }}
