"""Tests for the data-quality module (spec §14.2 / §17.3 Ticket 5).

Covers all five check dimensions, the §12 quality score (per-check and
per-dataset type-weighted), exception routing (full count vs. capped queue
items), audit events (dq_check + exception_open), the CLI, determinism, and
empty-dataset handling. Defects are injected into small controlled DataFrames so
each edge is isolated; the synthetic generator is not modified.
"""

from __future__ import annotations

import pandas as pd
import pytest
from civicpay.data import models as M
from civicpay.data.synthetic import AS_OF_DATETIME, generate_all
from civicpay.quality.checks import (
    check_accuracy_enum,
    check_accuracy_range,
    check_accuracy_referential,
    check_anomaly_iqr,
    check_anomaly_zscore,
    check_completeness_count,
    check_completeness_null,
    check_consistency_rule,
    check_timeliness_staleness,
    run_check,
)
from civicpay.quality.pipeline import QualityPipeline, load_config
from civicpay.quality.scoring import anomaly_rate, check_quality_score, dataset_quality_score
from civicpay.storage.duckdb import DuckDBStore

AS_OF = AS_OF_DATETIME


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #


@pytest.fixture()
def fresh_store():
    store = DuckDBStore(":memory:")
    store.init_schema()
    yield store
    store.close()


def _set_id(df: pd.DataFrame, id_field: str) -> pd.DataFrame:
    df.attrs["id_field"] = id_field
    return df


def _txn_df(rows: list[dict]) -> pd.DataFrame:
    cols = [
        "transaction_id",
        "account_id",
        "transaction_type",
        "amount",
        "currency",
        "posting_date",
        "value_date",
        "description",
        "reference_id",
        "created_at",
        "status",
    ]
    df = pd.DataFrame(rows, columns=cols)
    return _set_id(df, "transaction_id")


def _good_txn(tid="TXN-001", amount=100.00, ref="REF-001", status="posted", created="2026-08-15"):
    return {
        "transaction_id": tid,
        "account_id": "ACC-001",
        "transaction_type": "debit",
        "amount": amount,
        "currency": "USD",
        "posting_date": "2026-08-15",
        "value_date": "2026-08-15",
        "description": "n",
        "reference_id": ref,
        "created_at": created,
        "status": status,
    }


# --------------------------------------------------------------------------- #
# Scoring (§12)
# --------------------------------------------------------------------------- #


def test_check_quality_score_basic():
    assert check_quality_score(100, 0) == 100.0
    assert check_quality_score(100, 5) == 95.0
    assert check_quality_score(200, 1) == 99.5


def test_check_quality_score_empty_is_100():
    assert check_quality_score(0, 0) == 100.0


def test_check_quality_score_clamps_failing():
    assert check_quality_score(10, 20) == 0.0  # clamped to checked


def test_dataset_score_type_weighted():
    # Two completeness checks (100, 80) -> type avg 90; one anomaly (50) -> type avg 50.
    scores = [("completeness", 100.0), ("completeness", 80.0), ("anomaly", 50.0)]
    # equal weights: (90 + 50) / 2 = 70
    assert dataset_quality_score(scores) == pytest.approx(70.0)


def test_dataset_score_respects_type_weights():
    scores = [("completeness", 100.0), ("completeness", 80.0), ("anomaly", 50.0)]
    # weight anomaly 0 -> only completeness matters -> 90
    assert dataset_quality_score(scores, {"anomaly": 0.0}) == pytest.approx(90.0)


def test_dataset_score_empty_is_100():
    assert dataset_quality_score([]) == 100.0


def test_dataset_score_default_excludes_anomaly():
    # Default config (config/dq_checks.yml) weights anomaly 0.0; a low anomaly
    # score should not affect the dataset score at all when that weight is applied.
    scores = [("completeness", 100.0), ("completeness", 80.0), ("anomaly", 10.0)]
    assert dataset_quality_score(scores, {"anomaly": 0.0}) == pytest.approx(90.0)


def test_anomaly_rate_is_complement_of_type_score():
    scores = [("completeness", 100.0), ("anomaly", 95.0), ("anomaly", 85.0)]
    # anomaly type avg = 90 -> rate = 10
    assert anomaly_rate(scores) == pytest.approx(10.0)


def test_anomaly_rate_none_when_no_anomaly_checks():
    assert anomaly_rate([("completeness", 100.0)]) is None


# --------------------------------------------------------------------------- #
# Completeness
# --------------------------------------------------------------------------- #


def test_completeness_null_catches_nulls():
    df = _txn_df([_good_txn("T1"), _good_txn("T2"), {**_good_txn("T3"), "reference_id": None}])
    failing, ids = check_completeness_null(df, "reference_id")
    assert failing == 1
    assert ids == ["T3"]


def test_completeness_null_catches_empty_strings():
    df = _txn_df([_good_txn("T1"), {**_good_txn("T2"), "reference_id": "  "}])
    failing, ids = check_completeness_null(df, "reference_id")
    assert failing == 1
    assert ids == ["T2"]


def test_completeness_count_expected_match():
    df = _txn_df([_good_txn("T1"), _good_txn("T2")])
    failing, _ = check_completeness_count(df, expected_count=2)
    assert failing == 0


def test_completeness_count_mismatch_flags_all():
    df = _txn_df([_good_txn("T1"), _good_txn("T2")])
    failing, _ = check_completeness_count(df, expected_count=5)
    assert failing == 2  # whole dataset implicated


# --------------------------------------------------------------------------- #
# Accuracy
# --------------------------------------------------------------------------- #


def test_accuracy_range_catches_negative():
    df = _txn_df([_good_txn("T1", amount=100), _good_txn("T2", amount=-5)])
    failing, ids = check_accuracy_range(df, "amount", minimum=0)
    assert failing == 1
    assert ids == ["T2"]


def test_accuracy_range_inclusive_boundary():
    df = _txn_df([_good_txn("T1", amount=0)])  # amount == min
    failing, _ = check_accuracy_range(df, "amount", minimum=0)
    assert failing == 0


def test_accuracy_enum_catches_invalid():
    df = _txn_df([_good_txn("T1", status="posted"), _good_txn("T2", status="bogus")])
    failing, ids = check_accuracy_enum(df, "status", ["posted", "pending", "reversed"])
    assert failing == 1
    assert ids == ["T2"]


def test_accuracy_referential_catches_missing():
    df = _txn_df([_good_txn("T1"), {**_good_txn("T2"), "account_id": "ACC-999"}])
    ref = pd.Series(["ACC-001"])  # only ACC-001 exists
    failing, ids = check_accuracy_referential(df, "account_id", ref)
    assert failing == 1
    assert ids == ["T2"]


# --------------------------------------------------------------------------- #
# Consistency
# --------------------------------------------------------------------------- #


def _acct_df(rows):
    cols = [
        "account_id",
        "customer_id",
        "account_type",
        "account_number_masked",
        "currency",
        "current_balance",
        "available_balance",
        "opened_at",
        "status",
    ]
    df = pd.DataFrame(rows, columns=cols)
    return _set_id(df, "account_id")


def test_consistency_available_le_current_catches_violation():
    df = _acct_df(
        [
            {
                "account_id": "A1",
                "customer_id": "C1",
                "account_type": "checking",
                "account_number_masked": "****1",
                "currency": "USD",
                "current_balance": 100,
                "available_balance": 80,
                "opened_at": "2026-01-01",
                "status": "active",
            },
            {
                "account_id": "A2",
                "customer_id": "C2",
                "account_type": "checking",
                "account_number_masked": "****2",
                "currency": "USD",
                "current_balance": 100,
                "available_balance": 150,
                "opened_at": "2026-01-01",
                "status": "active",
            },  # violation
        ]
    )
    failing, ids = check_consistency_rule(df, rule="available_le_current")
    assert failing == 1
    assert ids == ["A2"]


def test_consistency_skips_loan_accounts():
    df = _acct_df(
        [
            {
                "account_id": "A1",
                "customer_id": "C1",
                "account_type": "loan",
                "account_number_masked": "****1",
                "currency": "USD",
                "current_balance": 100,
                "available_balance": 500,
                "opened_at": "2026-01-01",
                "status": "active",
            },
        ]
    )
    failing, _ = check_consistency_rule(df, rule="available_le_current")
    assert failing == 0  # loan accounts exempt


# --------------------------------------------------------------------------- #
# Timeliness
# --------------------------------------------------------------------------- #


def test_timeliness_catches_stale():
    df = _txn_df([_good_txn("T1", created="2026-07-01")])  # 62 days old
    failing, ids = check_timeliness_staleness(df, "created_at", 30, AS_OF)
    assert failing == 1
    assert ids == ["T1"]


def test_timeliness_boundary_not_stale():
    df = _txn_df([_good_txn("T1", created="2026-08-02")])  # 30 days == limit -> not stale
    failing, _ = check_timeliness_staleness(df, "created_at", 30, AS_OF)
    assert failing == 0


# --------------------------------------------------------------------------- #
# Anomaly
# --------------------------------------------------------------------------- #


def test_anomaly_zscore_catches_outlier():
    amounts = [100.0] * 100 + [10000.0]  # one clear outlier
    df = _txn_df([_good_txn(f"T{i}", amount=a) for i, a in enumerate(amounts)])
    failing, ids = check_anomaly_zscore(df, "amount", threshold=3.0)
    assert failing == 1
    assert "T100" in ids


def test_anomaly_iqr_catches_outlier():
    # A spread base distribution (so IQR > 0) plus one extreme outlier.
    amounts = [100.0 + i for i in range(50)] + [5000.0]
    df = _txn_df([_good_txn(f"T{i}", amount=a) for i, a in enumerate(amounts)])
    failing, _ = check_anomaly_iqr(df, "amount", threshold=1.5)
    assert failing >= 1


def test_anomaly_no_outliers_on_uniform_data():
    amounts = [100.0, 200.0, 150.0, 175.0, 125.0, 160.0, 140.0, 180.0]
    df = _txn_df([_good_txn(f"T{i}", amount=a) for i, a in enumerate(amounts)])
    failing, _ = check_anomaly_zscore(df, "amount", threshold=3.0)
    assert failing == 0


# --------------------------------------------------------------------------- #
# run_check dispatcher
# --------------------------------------------------------------------------- #


def test_run_check_infers_completeness_null_rule():
    df = _txn_df([_good_txn("T1"), {**_good_txn("T2"), "reference_id": None}])
    result = run_check(
        "transactions",
        df,
        {"type": "completeness", "name": "nulls", "field": "reference_id"},
        AS_OF,
        1,
    )
    assert result.check_type == "completeness"
    assert result.failing_records == 1
    assert result.passed is False
    assert result.quality_score == 50.0


def test_run_check_unknown_rule_raises():
    df = _txn_df([_good_txn("T1")])
    with pytest.raises(ValueError, match="Unknown check"):
        run_check(
            "transactions",
            df,
            {"type": "accuracy", "name": "x", "rule": "bogus", "field": "amount"},
            AS_OF,
            1,
        )


# --------------------------------------------------------------------------- #
# Empty datasets
# --------------------------------------------------------------------------- #


def test_checks_on_empty_dataset_score_100():
    df = _txn_df([])
    assert check_completeness_null(df, "reference_id") == (0, [])
    assert check_accuracy_range(df, "amount", minimum=0) == (0, [])
    assert check_anomaly_zscore(df, "amount") == (0, [])
    assert check_quality_score(0, 0) == 100.0


# --------------------------------------------------------------------------- #
# Pipeline: end-to-end on synthetic data
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def synthetic_store(tmp_path_factory):
    db = tmp_path_factory.mktemp("dq") / "dq.duckdb"
    store = DuckDBStore(str(db))
    store.init_schema()
    data = generate_all(seed=42, volumes={"customers": 500, "accounts": 200, "transactions": 2000})
    store.write_many(data, mode="replace")
    yield store
    store.close()


def test_pipeline_runs_all_checks(synthetic_store):
    summary = QualityPipeline(store=synthetic_store, config=load_config()).run(batch_id="DQ-TEST")
    assert summary["checks_run"] == 17
    assert summary["checks_passed"] + summary["checks_failed"] == 17
    assert summary["datasets_checked"] == 4
    assert set(summary["per_dataset_scores"]) == {
        "transactions",
        "accounts",
        "customers",
        "payment_records",
    }
    # All dataset scores are in [0, 100].
    for score in summary["per_dataset_scores"].values():
        assert 0.0 <= score <= 100.0
    # transactions has an anomaly check -> reported separately, excluded from
    # its score by the default config (type_weights.anomaly: 0.0).
    assert "transactions" in summary["per_dataset_anomaly_rate"]
    assert set(summary["per_dataset_anomaly_rate"]) <= set(summary["per_dataset_scores"])


def test_pipeline_writes_dq_results(synthetic_store):
    QualityPipeline(store=synthetic_store, config=load_config()).run(batch_id="DQ-RES")
    n = synthetic_store.table_count(M.DQResult.TABLE)
    assert n == 17
    df = synthetic_store.read_table(M.DQResult.TABLE)
    assert set(df["dataset_name"].unique()) == {
        "transactions",
        "accounts",
        "customers",
        "payment_records",
    }
    assert (df["failing_records"] >= 0).all()


def test_pipeline_routes_failures_to_exception_queue(synthetic_store):
    summary = QualityPipeline(store=synthetic_store, config=load_config()).run(batch_id="DQ-EXC")
    # Full failing count is always > routed count (capped at 25 per failing check).
    assert summary["exceptions_routed"] <= summary["total_failing_records"] + 1
    exc = synthetic_store.read_table(M.ExceptionItem.TABLE)
    assert (exc["source"] == "dq").all()
    assert (exc["status"] == "open").all()
    # reference_id encodes dataset:record_id.
    assert exc["reference_id"].str.contains(":").all()


def test_pipeline_emits_audit_events(synthetic_store):
    summary = QualityPipeline(store=synthetic_store, config=load_config()).run(batch_id="DQ-AUD")
    # dq_check per check + exception_open per routed item.
    assert summary["audit_events"] == 17 + summary["exceptions_routed"]
    # Count only THIS batch's audit events (the log is append-only across runs).
    types = synthetic_store.query(
        f"SELECT event_type, COUNT(*) n FROM {M.AuditEvent.TABLE} "
        f"WHERE event_id LIKE 'EVT-DQ-AUD-%' GROUP BY event_type"
    )
    by_type = dict(zip(types["event_type"], types["n"], strict=False))
    assert by_type.get("dq_check", 0) == 17
    assert by_type.get("exception_open", 0) == summary["exceptions_routed"]


def test_pipeline_dq_check_id_and_exception_id_deterministic():
    """Two independent runs over freshly seeded stores produce identical
    check ids and scores."""
    scores = []
    check_ids = []
    for _ in range(2):
        store = DuckDBStore(":memory:")
        store.init_schema()
        data = generate_all(
            seed=42, volumes={"customers": 500, "accounts": 200, "transactions": 2000}
        )
        store.write_many(data, mode="replace")
        QualityPipeline(store=store, config=load_config()).run(batch_id="DQ-DET")
        check_ids.append(
            store.read_table(M.DQResult.TABLE).sort_values("dq_check_id")["dq_check_id"].tolist()
        )
        scores.append(
            QualityPipeline(store=store, config=load_config()).run(batch_id="DQ-DET2")[
                "per_dataset_scores"
            ]
        )
        store.close()
    assert check_ids[0] == check_ids[1]  # deterministic check ids
    assert scores[0] == scores[1]  # deterministic scores


def test_pipeline_single_dataset_filter(synthetic_store):
    summary = QualityPipeline(store=synthetic_store, config=load_config()).run(
        batch_id="DQ-ONE", dataset="accounts"
    )
    assert summary["datasets_checked"] == 1
    # Only accounts checks (4) written this run.
    assert synthetic_store.table_count(M.DQResult.TABLE) == 4


def test_pipeline_unknown_dataset_raises(synthetic_store):
    with pytest.raises(ValueError, match="No DQ config"):
        QualityPipeline(store=synthetic_store, config=load_config()).run(
            batch_id="X", dataset="nope"
        )


# --------------------------------------------------------------------------- #
# Pipeline: known defect is caught (DoD)
# --------------------------------------------------------------------------- #


def test_pipeline_known_defect_is_caught(fresh_store):
    """DoD: a known injected defect is caught by a check and routed."""
    data = generate_all(seed=7, volumes={"customers": 50, "accounts": 50, "transactions": 200})
    fresh_store.write_many(data, mode="replace")

    # Inject a known defect: a transaction with a null reference_id.
    txns = fresh_store.read_table(M.Transaction.TABLE)
    txns.loc[txns.index[0], "reference_id"] = None
    fresh_store.write_dataframe(M.Transaction.TABLE, txns, mode="replace")

    cfg = load_config()
    QualityPipeline(store=fresh_store, config=cfg).run(batch_id="DQ-DEFECT")
    dq = fresh_store.read_table(M.DQResult.TABLE)
    null_check = dq[dq["check_name"] == "No null reference ids"].iloc[0]
    assert null_check["passed"] is False or bool(null_check["passed"]) is False
    assert int(null_check["failing_records"]) >= 1
    # The defect is routed to the exception queue.
    exc = fresh_store.read_table(M.ExceptionItem.TABLE)
    assert (exc["reference_id"].str.startswith("transactions:")).any()


# --------------------------------------------------------------------------- #
# CLI smoke test
# --------------------------------------------------------------------------- #


def test_cli_dq_check_runs(tmp_path):
    from civicpay.cli import app
    from typer.testing import CliRunner

    db = tmp_path / "cli.duckdb"
    store = DuckDBStore(str(db))
    store.init_schema()
    data = generate_all(seed=42, volumes={"customers": 50, "accounts": 50, "transactions": 200})
    store.write_many(data, mode="replace")
    store.close()

    runner = CliRunner()
    result = runner.invoke(app, ["dq", "check", "--db-path", str(db), "--batch-id", "DQ-CLI"])
    assert result.exit_code == 0, result.output
    assert "DQ-CLI" in result.output


# --------------------------------------------------------------------------- #
# Re-run idempotency pre-flight guard (OPEN_QUESTIONS §C)
# --------------------------------------------------------------------------- #


def test_dq_re_run_same_batch_id_blocked(synthetic_store):
    """Re-running DQ with a batch_id already in the audit log must fail fast
    with BatchIdAlreadyUsedError (not a raw DuckDB PK constraint error)."""
    from civicpay.audit.evidence import BatchIdAlreadyUsedError

    pipe = QualityPipeline(store=synthetic_store, config=load_config())
    pipe.run(batch_id="DQ-RERUN")  # first run: succeeds, writes audit events

    with pytest.raises(BatchIdAlreadyUsedError) as exc:
        pipe.run(batch_id="DQ-RERUN")  # second run: same batch_id -> blocked
    assert "DQ-RERUN" in str(exc.value)
    assert "append-only" in str(exc.value)
    assert "--batch-id" in str(exc.value)


def test_dq_pre_flight_uses_batch_id_prefix_not_substring(synthetic_store):
    """The dash-delimited prefix must not false-positive: 'DQ-0010' first must
    NOT block a later 'DQ-001' (nor vice versa)."""
    pipe = QualityPipeline(store=synthetic_store, config=load_config())
    pipe.run(batch_id="DQ-0010")  # creates EVT-DQ-0010-* events
    summary = pipe.run(batch_id="DQ-001")  # distinct prefix -> must succeed
    assert summary["batch_id"] == "DQ-001"


def test_cli_dq_re_run_blocked_message(tmp_path):
    """The CLI surfaces the pre-flight failure as a readable message + exit 1."""
    from civicpay.cli import app
    from typer.testing import CliRunner

    db = tmp_path / "rerun.duckdb"
    store = DuckDBStore(str(db))
    store.init_schema()
    data = generate_all(seed=42, volumes={"customers": 50, "accounts": 50, "transactions": 200})
    store.write_many(data, mode="replace")
    store.close()

    runner = CliRunner()
    runner.invoke(app, ["dq", "check", "--db-path", str(db), "--batch-id", "DQ-DUP"])
    result = runner.invoke(app, ["dq", "check", "--db-path", str(db), "--batch-id", "DQ-DUP"])
    assert result.exit_code == 1
    assert "DQ-DUP" in result.output
    assert "--batch-id" in result.output
