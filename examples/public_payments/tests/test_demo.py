"""Assert the demo catches every injected error and leaves clean data alone."""

from __future__ import annotations

from pathlib import Path

from detection import (
    AMOUNT_MISMATCH,
    DUPLICATE_PAYMENT,
    OUT_OF_WINDOW,
    run_detection,
)


def _flag_ids_by_reason(flags, reason) -> set[str]:
    return {f.payment_id for f in flags if f.reason == reason}


class TestInjectedErrorsAreCaught:
    def test_duplicate_payments_caught_exactly(self, payments, roster, ground_truth):
        flags, _ = run_detection(payments, roster)
        assert _flag_ids_by_reason(flags, DUPLICATE_PAYMENT) == set(
            ground_truth.duplicate_payment_ids
        )

    def test_out_of_window_payments_caught_exactly(self, payments, roster, ground_truth):
        flags, _ = run_detection(payments, roster)
        assert _flag_ids_by_reason(flags, OUT_OF_WINDOW) == set(
            ground_truth.out_of_window_payment_ids
        )

    def test_amount_mismatches_caught_exactly(self, payments, roster, ground_truth):
        flags, _ = run_detection(payments, roster)
        assert _flag_ids_by_reason(flags, AMOUNT_MISMATCH) == set(
            ground_truth.amount_mismatch_payment_ids
        )


class TestCleanDataIsLeftAlone:
    def test_legitimate_adjustments_are_never_flagged(self, payments, roster, ground_truth):
        """Amount differs from the approved benefit, but with a valid adjustment
        code — proves the engine isn't just flagging every deviation."""
        flags, _ = run_detection(payments, roster)
        flagged_ids = {f.payment_id for f in flags}
        assert flagged_ids.isdisjoint(ground_truth.legitimate_adjustment_payment_ids)

    def test_no_flags_beyond_the_three_injected_categories(self, payments, roster, ground_truth):
        flags, _ = run_detection(payments, roster)
        flagged_ids = {f.payment_id for f in flags}
        expected = (
            set(ground_truth.duplicate_payment_ids)
            | set(ground_truth.out_of_window_payment_ids)
            | set(ground_truth.amount_mismatch_payment_ids)
        )
        assert flagged_ids == expected

    def test_flagged_fraction_of_total_payments_is_small(self, payments, roster):
        flags, _ = run_detection(payments, roster)
        flagged_ids = {f.payment_id for f in flags}
        assert len(flagged_ids) / len(payments) < 0.05


class TestReusedDataQualityChecks:
    """The generic civicpay.quality.checks engine, reused unmodified (config-only)."""

    def test_no_dq_defects_were_injected_so_all_checks_pass(self, roster, payments):
        from run_demo import run_dq_checks

        results = run_dq_checks({"claimant_roster": roster, "benefit_payments": payments})
        assert results, "expected at least one configured check to run"
        failing = [r for r in results if not r.passed and r.check_type != "anomaly"]
        assert failing == []


class TestRunDemoEndToEnd:
    def test_full_pipeline_writes_a_verified_audit_chain(self, roster, payments, tmp_path: Path):
        from civicpay.audit.evidence import verify_chain
        from civicpay.audit.ledger import AuditLedger
        from civicpay.storage.duckdb import DuckDBStore
        from run_demo import AS_OF_DATETIME, BATCH_ID, build_audit_events, run_dq_checks

        store = DuckDBStore(tmp_path / "test_public_payments.duckdb")
        try:
            store.init_schema()
            store.write_dataframe("claimant_roster", roster, mode="replace")
            store.write_dataframe("benefit_payments", payments, mode="replace")

            dq_results = run_dq_checks({"claimant_roster": roster, "benefit_payments": payments})
            flags, _ = run_detection(payments, roster)
            events = build_audit_events(dq_results, flags)
            AuditLedger(store=store, actor="test", as_of=AS_OF_DATETIME).append_many(events)

            verification = verify_chain(store, batch_id=BATCH_ID)
            assert verification["verified"] is True
            assert verification["event_count"] == len(events)
        finally:
            store.close()
