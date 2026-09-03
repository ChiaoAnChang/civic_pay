{#
    Singular data test (no external package needed): a passing test returns
    zero rows. reconciliation_rate/ledger_coverage_rate are percentages —
    either falling outside [0, 100] indicates a bug in mart_recon_summary's
    arithmetic, not real data.
#}
select batch_id, reconciliation_rate, ledger_coverage_rate
from {{ ref('mart_recon_summary') }}
where reconciliation_rate not between 0 and 100
   or ledger_coverage_rate not between 0 and 100
