"""Tests for the enrollment pipeline orchestration (Ticket 13)."""

from __future__ import annotations

import pandas as pd
import pytest
from civicpay.audit.evidence import verify_chain
from civicpay.data import models as M
from civicpay.data.synthetic import generate_all
from civicpay.enrollment.pipeline import EnrollmentPipeline
from civicpay.exceptions.queue import ExceptionManager
from civicpay.storage.duckdb import DuckDBStore


@pytest.fixture()
def seeded_store():
    store = DuckDBStore(":memory:")
    store.init_schema()
    data = generate_all(
        seed=42,
        volumes={
            "customers": 200,
            "accounts": 100,
            "transactions": 500,
            "pending_enrollments": 100,
        },
    )
    store.write_many(data, mode="replace")
    yield store
    store.close()


def test_run_processes_all_pending_and_chain_verifies(seeded_store):
    summary = EnrollmentPipeline(store=seeded_store).run()
    assert summary["processed"] == 100
    assert summary["accepted"] + summary["mismatch"] + summary["rejected"] == 100
    assert summary["rejected"] == 25  # the deliberate defect cohort (§3)
    assert summary["backlog_seeded"] == 3
    report = verify_chain(seeded_store)
    assert report["verified"] is True


def test_accepted_and_mismatch_counts_are_mutually_exclusive_and_positive(seeded_store):
    summary = EnrollmentPipeline(store=seeded_store).run()
    # Both outcomes should occur given the ~80/20 agree/diverge generator design.
    assert summary["accepted"] > 0
    assert summary["mismatch"] > 0


def test_every_dual_source_evaluation_is_recorded(seeded_store):
    summary = EnrollmentPipeline(store=seeded_store).run()
    n_results = seeded_store.table_count(M.DualSourceResult.TABLE)
    # Backlog cohort adds 3 more evaluations beyond the pending pool's valid records.
    assert n_results == (summary["accepted"] + summary["mismatch"] + summary["backlog_seeded"])


def test_accepted_rows_written(seeded_store):
    summary = EnrollmentPipeline(store=seeded_store).run()
    assert seeded_store.table_count(M.AcceptedEnrollment.TABLE) == summary["accepted"]


def test_mismatches_routed_to_exception_queue(seeded_store):
    summary = EnrollmentPipeline(store=seeded_store).run()
    exc = seeded_store.read_table(M.ExceptionItem.TABLE)
    mismatches = exc[exc["source"] == M.ExceptionSource.ENROLLMENT_DUAL_SOURCE]
    # mismatch count from real records + 3 backlog mismatches.
    assert len(mismatches) == summary["mismatch"] + summary["backlog_seeded"]
    assert (mismatches["status"] == M.ExceptionStatus.OPEN).all()
    assert mismatches["reference_id"].str.startswith("pending_enrollments:").all()


def test_pending_status_updated(seeded_store):
    EnrollmentPipeline(store=seeded_store).run()
    df = seeded_store.read_table(M.PendingEnrollment.TABLE)
    assert not (df["status"] == M.EnrollmentStatus.PENDING).any()
    assert set(df["status"].unique()) <= {
        M.EnrollmentStatus.ACCEPTED,
        M.EnrollmentStatus.MISMATCH,
        M.EnrollmentStatus.REJECTED,
    }


def test_rerun_is_idempotent_and_skips_everything(seeded_store):
    first = EnrollmentPipeline(store=seeded_store).run()
    second = EnrollmentPipeline(store=seeded_store).run()
    assert second["processed"] == 0
    assert second["backlog_seeded"] == 0
    # No duplicate rows from the second run.
    assert seeded_store.table_count(M.AcceptedEnrollment.TABLE) == first["accepted"]
    exc = seeded_store.read_table(M.ExceptionItem.TABLE)
    mismatches = exc[exc["source"] == M.ExceptionSource.ENROLLMENT_DUAL_SOURCE]
    assert len(mismatches) == first["mismatch"] + first["backlog_seeded"]


def test_amount_at_risk_resolves_incentive_amount(seeded_store):
    from civicpay.data.synthetic import AS_OF_DATETIME

    EnrollmentPipeline(store=seeded_store).run()
    items = ExceptionManager(store=seeded_store, as_of=AS_OF_DATETIME).list()
    enroll_items = [i for i in items if i["source"] == M.ExceptionSource.ENROLLMENT_DUAL_SOURCE]
    assert enroll_items
    real_items = [i for i in enroll_items if "BACKLOG" not in i["exception_id"]]
    assert all(i["amount_at_risk"] > 0 for i in real_items)


def test_backlog_cohort_ages_and_escalates(seeded_store):
    from civicpay.data.synthetic import AS_OF_DATETIME

    EnrollmentPipeline(store=seeded_store).run()
    items = ExceptionManager(store=seeded_store, as_of=AS_OF_DATETIME).list()
    backlog = {i["exception_id"]: i for i in items if "BACKLOG" in i["exception_id"]}
    assert len(backlog) == 3
    ages = sorted(i["age_days"] for i in backlog.values())
    assert ages == [5, 15, 30]


def test_file_path_input(tmp_path, seeded_store):
    csv_path = tmp_path / "candidates.csv"
    pd.DataFrame(
        [
            {
                "enrollment_id": "ENR-FILE-001",
                "entity_id": "ENT-F001",
                "program_code": "STARTER",
                "enrollment_date": "2025-01-15",
                "incentive_amount": "1200.00",
                "term_months": "12",
                "region": "WEST",
                "submitted_by": "operator-01",
            },
            {
                "enrollment_id": "ENR-FILE-002",
                "entity_id": "ENT-F002",
                "program_code": "GROWTH",
                "enrollment_date": "2025-02-01",
                "incentive_amount": "1000.00",
                "term_months": "7",
                "region": "MIDWEST",
                "submitted_by": "operator-02",
            },
        ]
    ).to_csv(csv_path, index=False)

    store = DuckDBStore(":memory:")
    store.init_schema()
    summary = EnrollmentPipeline(store=store).run(file_path=csv_path)
    assert summary["processed"] == 2
    assert summary["accepted"] == 1
    assert summary["mismatch"] == 1

    rerun = EnrollmentPipeline(store=store).run(file_path=csv_path)
    assert rerun["skipped"] == 2
    store.close()


def test_partial_dataset_generation_still_deterministic():
    """Two independent generations with the same seed produce identical
    pending_enrollments (defect cohort included)."""
    d1 = generate_all(seed=42, volumes={"customers": 50, "accounts": 30, "transactions": 100})
    d2 = generate_all(seed=42, volumes={"customers": 50, "accounts": 30, "transactions": 100})
    pd.testing.assert_frame_equal(d1[M.PendingEnrollment.TABLE], d2[M.PendingEnrollment.TABLE])
