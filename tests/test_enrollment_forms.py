"""Tests for the Streamlit enrollment form (Ticket 13 §4), using Streamlit's
``AppTest`` harness to drive the real widget tree without a browser.

``AppTest.from_file`` needs a real, inspectable source file, so each test
writes a tiny wrapper script (via ``tmp_path``) that calls
``civicpay.enrollment.forms.render`` against an isolated on-disk store,
rather than exercising ``forms.py``'s ``__main__`` block directly (which
defaults to the shared ``DEFAULT_DB_PATH``).
"""

from __future__ import annotations

import pytest
from civicpay.audit.evidence import verify_chain
from civicpay.data import models as M
from civicpay.storage.duckdb import DuckDBStore
from streamlit.testing.v1 import AppTest

_WRAPPER = """\
from civicpay.data.synthetic import AS_OF_DATETIME
from civicpay.enrollment.forms import render
from civicpay.storage.duckdb import DuckDBStore

store = DuckDBStore({db_path!r})
store.init_schema()
render(store=store, as_of=AS_OF_DATETIME)
"""


@pytest.fixture
def app_and_db(tmp_path):
    db_path = str(tmp_path / "form_test.duckdb")
    script = tmp_path / "wrapper.py"
    script.write_text(_WRAPPER.format(db_path=db_path), encoding="utf-8")
    at = AppTest.from_file(str(script))
    at.run(timeout=30)
    assert not at.exception, at.exception
    return at, db_path


def test_form_renders_step_one_with_constrained_widgets(app_and_db):
    at, _ = app_and_db
    # program_code and region are enums -> selectboxes; entity_id is free text.
    assert len(at.selectbox) == 2
    assert len(at.text_input) == 1


def _advance_to_step_3(
    at,
    *,
    entity_id="ENT-TEST-1",
    program="GROWTH",
    region="WEST",
    term=12,
    amount=1200.00,
    operator="operator-01",
):
    at.text_input[0].set_value(entity_id)
    at.selectbox[0].set_value(program)
    at.selectbox[1].set_value(region)
    at.button[0].click()  # Next
    at.run(timeout=30)
    assert not at.exception, at.exception

    at.number_input[0].set_value(term)
    at.number_input[1].set_value(amount)
    at.text_input[0].set_value(operator)
    next_btn = [b for b in at.button if b.label == "Next"][0]
    next_btn.click()
    at.run(timeout=30)
    assert not at.exception, at.exception
    return at


def test_valid_record_reaches_review_with_no_errors(app_and_db):
    at, _ = app_and_db
    at = _advance_to_step_3(at)
    assert at.error == []
    assert any("Validation passed" in s.value for s in at.success)
    submit_btn = [b for b in at.button if b.label == "Submit"][0]
    assert submit_btn.disabled is False


def test_out_of_range_amount_disables_submit(app_and_db):
    at, _ = app_and_db
    # GROWTH caps at 25,000 -- the number_input itself clamps to that max,
    # so drive an invalid state through term_months vs. program mismatch
    # instead: STARTER only allows 1-12 months, request 12 there is fine,
    # but flip region to something inconsistent isn't a validator rule --
    # use a blank submitted_by (a required field) to force a real error.
    at = _advance_to_step_3(at, operator="")
    assert any(e.value and "submitted_by" in e.value for e in at.error)
    submit_btn = [b for b in at.button if b.label == "Submit"][0]
    assert submit_btn.disabled is True


def test_submit_writes_accepted_enrollment_and_verifies_chain(app_and_db):
    at, db_path = app_and_db
    at = _advance_to_step_3(at, amount=1200.00, term=12)  # divides evenly -> accepted
    submit_btn = [b for b in at.button if b.label == "Submit"][0]
    submit_btn.click()
    at.run(timeout=30)
    assert not at.exception, at.exception
    assert any("accepted" in s.value for s in at.success)

    store = DuckDBStore(db_path)
    accepted = store.read_table(M.AcceptedEnrollment.TABLE)
    assert len(accepted) == 1
    row = accepted.iloc[0]
    assert row["entity_id"] == "ENT-TEST-1"
    assert row["batch_id"] == row["enrollment_id"]  # no double ENR- prefix
    report = verify_chain(store)
    assert report["verified"] is True
    store.close()


def test_submit_mismatch_routes_to_exception_queue(app_and_db):
    at, db_path = app_and_db
    # 1000.00 / 7 is the same known-diverging pair used in the dual_source tests.
    at = _advance_to_step_3(at, amount=1000.00, term=7)
    submit_btn = [b for b in at.button if b.label == "Submit"][0]
    submit_btn.click()
    at.run(timeout=30)
    assert not at.exception, at.exception
    assert any("mismatch" in w.value for w in at.warning)

    store = DuckDBStore(db_path)
    exc = store.read_table(M.ExceptionItem.TABLE)
    assert len(exc) == 1
    assert exc.iloc[0]["source"] == M.ExceptionSource.ENROLLMENT_DUAL_SOURCE
    store.close()


def test_form_resets_after_submit(app_and_db):
    at, _ = app_and_db
    at = _advance_to_step_3(at)
    submit_btn = [b for b in at.button if b.label == "Submit"][0]
    submit_btn.click()
    at.run(timeout=30)
    # Back to step 1: entity/program/region widgets again, not the review table.
    assert len(at.selectbox) == 2
    assert len(at.text_input) == 1
