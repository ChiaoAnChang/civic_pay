"""Data-quality scoring (spec §12).

Quality scores are a 0–100 quantitative measure of a dataset's quality.

* Per-check score: ``100 * (checked - failing) / checked`` for the records a
  single check examined.
* Per-dataset score: the check-type-weighted average of per-check scores. The
  spec (§12) weights by check *type* (completeness / accuracy / consistency /
  timeliness / anomaly); default weights are equal. The per-type score is the
  mean of that type's per-check scores, then the dataset score is the
  weighted mean across types.
"""

from __future__ import annotations

from typing import Any

# Default per-type weights (equal). Override via config ``type_weights``.
DEFAULT_TYPE_WEIGHTS: dict[str, float] = {
    "completeness": 1.0,
    "accuracy": 1.0,
    "consistency": 1.0,
    "timeliness": 1.0,
    "anomaly": 1.0,
}


def check_quality_score(checked: int, failing: int) -> float:
    """Per-check pass rate as a 0–100 score.

    An empty dataset (``checked == 0``) scores 100 by convention: there is no
    record that fails, so no quality defect. A dataset-level expected-count
    check is the right place to flag a missing dataset, not the row checks.
    """
    if checked <= 0:
        return 100.0
    failing = max(0, min(failing, checked))
    return round(100.0 * (checked - failing) / checked, 4)


def dataset_quality_score(
    check_scores: list[tuple[str, float]],
    type_weights: dict[str, float] | None = None,
) -> float:
    """Check-type-weighted average of per-check scores (spec §12).

    ``check_scores`` is a list of ``(check_type, per_check_score)`` pairs. The
    per-type score is the mean of that type's scores; the dataset score is the
    weighted mean across types using ``type_weights`` (defaults equal).
    """
    if not check_scores:
        return 100.0
    weights = {**DEFAULT_TYPE_WEIGHTS, **(type_weights or {})}

    by_type: dict[str, list[float]] = {}
    for check_type, score in check_scores:
        by_type.setdefault(check_type, []).append(score)

    weighted_sum = 0.0
    total_weight = 0.0
    for check_type, scores in by_type.items():
        type_score = sum(scores) / len(scores)
        w = weights.get(check_type, 1.0)
        weighted_sum += type_score * w
        total_weight += w
    if total_weight <= 0:
        return 100.0
    return round(weighted_sum / total_weight, 4)


def anomaly_rate(check_scores: list[tuple[str, float]]) -> float | None:
    """Mean failure rate (0-100) of the ``anomaly``-type checks, or ``None``.

    Anomaly checks are excluded from ``dataset_quality_score`` by default
    (``type_weights.anomaly: 0.0``) because they flag a small tail of records
    by construction, so their own score sits near 100 regardless of data
    quality elsewhere — including them in the average would inflate the
    dataset score rather than reflect real defects. The rate is still worth
    surfacing on its own, as the complement of the per-type score computed
    inside ``dataset_quality_score``. Returns ``None`` when no anomaly checks
    ran (nothing to report, as opposed to a rate of 0).
    """
    scores = [score for check_type, score in check_scores if check_type == "anomaly"]
    if not scores:
        return None
    return round(100.0 - sum(scores) / len(scores), 4)


def summarize(check_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Build a summary dict from a list of check-result rows."""
    total_checks = len(check_results)
    passed = sum(1 for c in check_results if c["passed"])
    failed = total_checks - passed
    total_failing_records = sum(int(c["failing_records"]) for c in check_results)
    return {
        "checks_run": total_checks,
        "checks_passed": passed,
        "checks_failed": failed,
        "total_failing_records": total_failing_records,
    }
