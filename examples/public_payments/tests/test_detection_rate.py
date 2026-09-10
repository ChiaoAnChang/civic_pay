"""Measure and assert the detection catch rate (spec §4.5 / §9).

This is the number that gets quoted in the README and any exhibit
description — it must be genuinely measured here, not typed into prose by
hand. Reports precision (no false positives among clean/legitimate rows) and
recall (every injected error of each category is caught) separately, and a
combined catch rate across all three injected error categories.
"""

from __future__ import annotations

from detection import run_detection


def _injected_total(ground_truth) -> int:
    return (
        len(ground_truth.duplicate_payment_ids)
        + len(ground_truth.out_of_window_payment_ids)
        + len(ground_truth.amount_mismatch_payment_ids)
    )


def test_catch_rate_across_all_injected_categories(payments, roster, ground_truth, capsys):
    flags, counts = run_detection(payments, roster)
    flagged_ids = {f.payment_id for f in flags}

    injected_ids = (
        set(ground_truth.duplicate_payment_ids)
        | set(ground_truth.out_of_window_payment_ids)
        | set(ground_truth.amount_mismatch_payment_ids)
    )
    caught = injected_ids & flagged_ids
    injected_total = _injected_total(ground_truth)
    catch_rate = len(caught) / injected_total if injected_total else 0.0

    false_positives = flagged_ids - injected_ids
    decoys_flagged = flagged_ids & set(ground_truth.legitimate_adjustment_payment_ids)

    with capsys.disabled():
        print(
            f"\nDetection catch rate: {len(caught)} of {injected_total} injected errors "
            f"caught ({catch_rate:.1%}). False positives: {len(false_positives)} "
            f"(of which {len(decoys_flagged)} were legitimate-adjustment decoys)."
        )

    assert injected_total > 0
    assert catch_rate == 1.0, f"expected to catch every injected error, caught {catch_rate:.1%}"
    assert false_positives == set(), f"unexpected false-positive flags: {false_positives}"


def test_catch_rate_per_category_is_measured_independently(payments, roster, ground_truth):
    flags, counts = run_detection(payments, roster)

    per_category = {
        "duplicate_payment": ground_truth.duplicate_payment_ids,
        "out_of_window": ground_truth.out_of_window_payment_ids,
        "amount_mismatch": ground_truth.amount_mismatch_payment_ids,
    }
    flagged_by_reason: dict[str, set[str]] = {}
    for f in flags:
        flagged_by_reason.setdefault(f.reason, set()).add(f.payment_id)

    for reason, injected_ids in per_category.items():
        caught = injected_ids & flagged_by_reason.get(reason, set())
        assert len(caught) == len(injected_ids), (
            f"{reason}: caught {len(caught)} of {len(injected_ids)} injected cases"
        )
