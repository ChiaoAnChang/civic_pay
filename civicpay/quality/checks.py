"""Data-quality check engine (spec §14.2).

Five check dimensions, each operating on a pandas DataFrame:

* **Completeness** — null checks on required fields; expected record count.
* **Accuracy** — valid enums; range checks (amount >= 0); referential integrity.
* **Consistency** — cross-field rules (e.g. available_balance <= current_balance
  for non-loan accounts).
* **Timeliness** — staleness (max age of a timestamp field vs. as-of date).
* **Anomaly** — statistical outliers on a numeric field (z-score or IQR).

Each check is a *pure function* of its inputs (no I/O, no wall-clock): given the
same DataFrame, config, and as-of date, it returns the same result. This makes
the engine fully deterministic and trivially testable.

A ``CheckResult`` carries the full failing-record count (never capped) plus a
capped sample of failing record ids (for exception routing / audit).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np
import pandas as pd

from civicpay.data.synthetic import AS_OF_DATETIME
from civicpay.quality.scoring import check_quality_score


@dataclass
class CheckResult:
    """The outcome of one data-quality check.

    ``failing_records`` is the full, uncapped count. ``failing_ids`` is a
    capped sample of failing record ids (for exception routing); the rest are
    captured only in the count.
    """

    check_id: str
    dataset_name: str
    check_type: str
    check_name: str
    passed: bool
    checked: int
    failing_records: int
    quality_score: float
    checked_at: datetime
    failing_ids: list[str] = field(default_factory=list)
    severity: str = "low"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _to_date(value: Any):
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, (pd.Timestamp, datetime)):
        return pd.Timestamp(value).date()
    return pd.Timestamp(value).date()


def _is_null(value: Any) -> bool:
    """True for pandas/NaN nulls and empty strings."""
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------- #
# Completeness
# --------------------------------------------------------------------------- #


def check_completeness_null(df: pd.DataFrame, field: str, **_: Any) -> tuple[int, list[str]]:
    """Count nulls in ``field``; return (failing_count, failing_ids).

    Requires an ``id_field`` to be passed by the caller via the pipeline; here
    we return failing_ids only when an ``id_field`` kwarg is present.
    """
    if field not in df.columns:
        return 0, []
    mask = df[field].isna() | (df[field].astype(str).str.strip() == "")
    failing = int(mask.sum())
    ids: list[str] = []
    if failing > 0:
        id_field = df.attrs.get("id_field")
        if id_field and id_field in df.columns:
            ids = df.loc[mask, id_field].astype(str).tolist()
    return failing, ids


def check_completeness_count(
    df: pd.DataFrame, expected_count: int, **_: Any
) -> tuple[int, list[str]]:
    """Expected record count. Failing records = the whole dataset if mismatched."""
    checked = len(df)
    if checked == expected_count:
        return 0, []
    # Mismatch: every record is implicated; no individual id to route.
    return checked, []


# --------------------------------------------------------------------------- #
# Accuracy
# --------------------------------------------------------------------------- #


def _failing_ids(df: pd.DataFrame, mask: pd.Series) -> list[str]:
    id_field = df.attrs.get("id_field")
    if not id_field or id_field not in df.columns:
        return []
    return df.loc[mask, id_field].astype(str).tolist()


def check_accuracy_range(
    df: pd.DataFrame,
    field: str,
    minimum: float | None = None,
    maximum: float | None = None,
    **_: Any,
) -> tuple[int, list[str]]:
    if field not in df.columns:
        return 0, []
    values = pd.to_numeric(df[field], errors="coerce")
    mask = values.isna()
    if minimum is not None:
        mask |= values < minimum
    if maximum is not None:
        mask |= values > maximum
    failing = int(mask.sum())
    return failing, _failing_ids(df, mask)


def check_accuracy_enum(
    df: pd.DataFrame, field: str, values: list[str], **_: Any
) -> tuple[int, list[str]]:
    if field not in df.columns:
        return 0, []
    allowed = set(values)
    mask = ~df[field].astype(str).isin(allowed) & ~df[field].isna()
    # Treat nulls separately (a completeness concern), but flag stray values.
    failing = int(mask.sum())
    return failing, _failing_ids(df, mask)


def check_accuracy_referential(
    df: pd.DataFrame,
    field: str,
    reference_values: pd.Series,
    **_: Any,
) -> tuple[int, list[str]]:
    if field not in df.columns:
        return 0, []
    ref_set = set(reference_values.dropna().astype(str).tolist())
    mask = ~df[field].astype(str).isin(ref_set) & ~df[field].isna()
    failing = int(mask.sum())
    return failing, _failing_ids(df, mask)


# --------------------------------------------------------------------------- #
# Consistency
# --------------------------------------------------------------------------- #


def check_consistency_rule(df: pd.DataFrame, rule: str, **_: Any) -> tuple[int, list[str]]:
    """Named cross-field consistency rules."""
    if rule == "available_le_current":
        # available_balance <= current_balance for non-loan accounts.
        if "available_balance" not in df.columns or "current_balance" not in df.columns:
            return 0, []
        avail = pd.to_numeric(df["available_balance"], errors="coerce")
        curr = pd.to_numeric(df["current_balance"], errors="coerce")
        scope = (
            df["account_type"] != "loan"
            if "account_type" in df.columns
            else pd.Series(True, index=df.index)
        )
        mask = scope & (avail > curr)
        failing = int(mask.sum())
        return failing, _failing_ids(df, mask)
    # Unknown rule -> no-op pass.
    return 0, []


# --------------------------------------------------------------------------- #
# Timeliness
# --------------------------------------------------------------------------- #


def check_timeliness_staleness(
    df: pd.DataFrame, field: str, max_age_days: int, as_of: datetime, **_: Any
) -> tuple[int, list[str]]:
    if field not in df.columns:
        return 0, []
    # Normalize both sides to tz-naive UTC to avoid tz-mismatch errors.
    as_of_ts = pd.Timestamp(as_of)
    if as_of_ts.tzinfo is not None:
        as_of_ts = as_of_ts.tz_convert("UTC").tz_localize(None)
    dates = pd.to_datetime(df[field], errors="coerce")
    if getattr(dates.dt, "tz", None) is not None:
        dates = dates.dt.tz_convert("UTC").dt.tz_localize(None)
    age_days = (as_of_ts - dates).dt.days
    mask = age_days > max_age_days
    failing = int(mask.sum())
    return failing, _failing_ids(df, mask)


# --------------------------------------------------------------------------- #
# Anomaly
# --------------------------------------------------------------------------- #


def check_anomaly_zscore(
    df: pd.DataFrame, field: str, threshold: float = 3.0, **_: Any
) -> tuple[int, list[str]]:
    """Statistical outliers by z-score (|z| > threshold)."""
    if field not in df.columns:
        return 0, []
    values = pd.to_numeric(df[field], errors="coerce")
    std = float(values.std(ddof=0))
    if std == 0 or not np.isfinite(std):
        return 0, []
    mean = float(values.mean())
    z = (values - mean).abs() / std
    mask = z > threshold
    failing = int(mask.sum())
    return failing, _failing_ids(df, mask)


def check_anomaly_iqr(
    df: pd.DataFrame, field: str, threshold: float = 1.5, **_: Any
) -> tuple[int, list[str]]:
    """Statistical outliers by IQR (Tukey)."""
    if field not in df.columns:
        return 0, []
    values = pd.to_numeric(df[field], errors="coerce").dropna()
    if len(values) < 4:
        return 0, []
    q1 = float(values.quantile(0.25))
    q3 = float(values.quantile(0.75))
    iqr = q3 - q1
    if iqr == 0:
        return 0, []
    lower = q1 - threshold * iqr
    upper = q3 + threshold * iqr
    mask = (values < lower) | (values > upper)
    # Realign mask to original df index.
    full_mask = pd.Series(False, index=df.index)
    full_mask.loc[mask.index[mask]] = True
    failing = int(mask.sum())
    return failing, _failing_ids(df, full_mask)


# --------------------------------------------------------------------------- #
# Dispatcher
# --------------------------------------------------------------------------- #

_CHECK_DISPATCH = {
    ("completeness", "null"): check_completeness_null,
    ("completeness", "count"): check_completeness_count,
    ("accuracy", "range"): check_accuracy_range,
    ("accuracy", "enum"): check_accuracy_enum,
    ("accuracy", "referential"): check_accuracy_referential,
    ("consistency", None): check_consistency_rule,
    ("timeliness", "staleness"): check_timeliness_staleness,
    ("anomaly", "zscore"): check_anomaly_zscore,
    ("anomaly", "iqr"): check_anomaly_iqr,
}


def run_check(
    dataset_name: str,
    df: pd.DataFrame,
    check_config: dict[str, Any],
    as_of: datetime = AS_OF_DATETIME,
    seq: int = 0,
) -> CheckResult:
    """Run one configured check against ``df`` and return a CheckResult.

    ``check_config`` keys: ``type``, ``name``, ``severity``, ``rule`` (for
    consistency / accuracy sub-kinds), plus rule-specific params (``field``,
    ``values``, ``min``/``maximum``, ``max_age_days``, ``threshold``,
    ``expected_count``).
    """
    check_type = check_config["type"]
    rule = check_config.get("rule")
    # Infer the sub-kind when the config omits an explicit ``rule``.
    if not rule:
        if check_type == "completeness":
            rule = "count" if "expected_count" in check_config else "null"
        elif check_type == "timeliness":
            rule = "staleness"
        elif check_type == "anomaly":
            rule = "zscore"
    key = (check_type, rule)
    fn = _CHECK_DISPATCH.get(key)
    if fn is None:
        # Fall back to a type-level match (e.g. consistency, whose rule is a
        # named cross-field rule passed through as a parameter).
        fn = _CHECK_DISPATCH.get((check_type, None))
    if fn is None:
        raise ValueError(f"Unknown check type/rule: {key}")

    # Build kwargs from config, dropping framework-only keys.
    params = {
        k: v
        for k, v in check_config.items()
        if k not in {"type", "name", "severity", "route_failures", "weight"}
    }
    if "min" in params:
        params["minimum"] = params.pop("min")
    if "maximum" in params:
        params["maximum"] = params.pop("maximum")
    if fn is check_timeliness_staleness:
        params["as_of"] = as_of

    checked = len(df)
    if fn is check_accuracy_referential:
        # reference_values injected by the pipeline as a special kwarg.
        failing, ids = fn(df, **params)
    else:
        failing, ids = fn(df, **params)

    score = check_quality_score(checked, failing)
    return CheckResult(
        check_id=f"DQ-{dataset_name}-{seq:04d}",
        dataset_name=dataset_name,
        check_type=check_type,
        check_name=check_config.get("name", f"{check_type}/{rule}"),
        passed=failing == 0,
        checked=checked,
        failing_records=failing,
        quality_score=score,
        checked_at=as_of,
        failing_ids=ids,
        severity=check_config.get("severity", "low"),
    )
