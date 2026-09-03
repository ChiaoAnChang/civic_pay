"""Enrollment validators (Ticket 13 §5).

Pure functions (no Streamlit import) so they are unit-testable and reusable
by both the form and the CLI batch-validate path. Rules are loaded from
``config/enrollment_rules.yml`` (declarative, not hard-coded).

Operates on a *raw* row (a dict of strings, matching a CSV row or a
``pending_enrollments`` row — see that table's docstring for why
``incentive_amount``/``term_months`` are text, not numeric, columns) rather
than an already-typed ``EnrollmentRecord``, since coercion and rejection of
malformed numeric text is itself one of the checks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from civicpay.enrollment.models import EnrollmentRecord

DEFAULT_RULES_PATH = Path("config/enrollment_rules.yml")

REQUIRED_FIELDS = (
    "entity_id",
    "program_code",
    "enrollment_date",
    "incentive_amount",
    "term_months",
    "region",
    "submitted_by",
)


@dataclass
class Issue:
    field: str
    message: str
    severity: str = "error"  # "error" | "warning"


@dataclass
class ValidationResult:
    errors: list[Issue] = field(default_factory=list)
    warnings: list[Issue] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def load_rules(path: Path | str | None = None) -> dict[str, Any]:
    """Load enrollment validation rules from YAML."""
    path = Path(path) if path else DEFAULT_RULES_PATH
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _stripped(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def validate(
    raw: dict[str, Any],
    rules: dict[str, Any],
    as_of: datetime,
    seen_entity_ids: set[str] | None = None,
) -> ValidationResult:
    """Validate one raw enrollment row against ``rules``.

    ``seen_entity_ids`` supports the cross-field duplicate-entity_id check
    across an in-progress batch — the caller accumulates entity ids across
    calls (a record is not compared against itself).
    """
    result = ValidationResult()

    for f in REQUIRED_FIELDS:
        if not _stripped(raw.get(f)):
            result.errors.append(Issue(field=f, message="required field is missing or blank"))
    if result.errors:
        # Nothing further can be safely checked without the required fields.
        return result

    entity_id = _stripped(raw["entity_id"])
    program_code = _stripped(raw["program_code"])
    region = _stripped(raw["region"])

    programs: dict[str, Any] = rules.get("programs", {})
    program_cfg = programs.get(program_code)
    if program_cfg is None:
        result.errors.append(
            Issue(field="program_code", message=f"'{program_code}' is not a configured program")
        )

    if region not in rules.get("regions", []):
        result.errors.append(Issue(field="region", message=f"'{region}' is not a valid region"))

    if seen_entity_ids is not None and entity_id in seen_entity_ids:
        result.errors.append(
            Issue(field="entity_id", message=f"duplicate entity_id '{entity_id}' in this batch")
        )

    # incentive_amount: strip outer whitespace, then must parse cleanly — an
    # embedded space (or other stray character) that survives strip() is
    # rejected, not silently repaired.
    amount: Decimal | None = None
    amount_text = _stripped(raw["incentive_amount"])
    try:
        amount = Decimal(amount_text)
    except InvalidOperation:
        result.errors.append(
            Issue(field="incentive_amount", message=f"'{amount_text}' is not a valid number")
        )
    else:
        if amount <= 0:
            result.errors.append(
                Issue(field="incentive_amount", message="incentive_amount must be > 0")
            )
        elif program_cfg is not None and amount > Decimal(str(program_cfg["max_incentive_amount"])):
            result.errors.append(
                Issue(
                    field="incentive_amount",
                    message=(
                        f"{amount} exceeds program {program_code}'s cap "
                        f"({program_cfg['max_incentive_amount']})"
                    ),
                )
            )

    term_months: int | None = None
    term_text = _stripped(raw["term_months"])
    try:
        term_months = int(term_text)
    except ValueError:
        result.errors.append(
            Issue(field="term_months", message=f"'{term_text}' is not a valid integer")
        )
    else:
        if program_cfg is not None:
            bounds = program_cfg["term_months"]
            if not (bounds["min"] <= term_months <= bounds["max"]):
                result.errors.append(
                    Issue(
                        field="term_months",
                        message=(
                            f"{term_months} is outside program {program_code}'s "
                            f"range ({bounds['min']}-{bounds['max']})"
                        ),
                    )
                )

    enrollment_date = raw["enrollment_date"]
    if isinstance(enrollment_date, str):
        try:
            enrollment_date = datetime.fromisoformat(enrollment_date)
        except ValueError:
            result.errors.append(
                Issue(field="enrollment_date", message=f"'{enrollment_date}' is not a valid date")
            )
            enrollment_date = None
    if isinstance(enrollment_date, datetime):
        min_date = rules.get("min_enrollment_date")
        cmp_date = enrollment_date.replace(tzinfo=None)
        as_of_naive = as_of.replace(tzinfo=None) if as_of.tzinfo else as_of
        if min_date and cmp_date < datetime.fromisoformat(str(min_date)):
            result.errors.append(
                Issue(
                    field="enrollment_date",
                    message=f"enrollment_date is before the earliest allowed date ({min_date})",
                )
            )
        elif cmp_date > as_of_naive:
            result.errors.append(
                Issue(field="enrollment_date", message="enrollment_date is in the future")
            )

    return result


def to_enrollment_record(raw: dict[str, Any]) -> EnrollmentRecord:
    """Build a typed ``EnrollmentRecord`` from a raw row that already passed
    :func:`validate` — callers must not call this on an invalid row."""
    enrollment_date = raw["enrollment_date"]
    if isinstance(enrollment_date, str):
        enrollment_date = datetime.fromisoformat(enrollment_date)
    return EnrollmentRecord(
        enrollment_id=_stripped(raw["enrollment_id"]),
        entity_id=_stripped(raw["entity_id"]),
        program_code=_stripped(raw["program_code"]),
        enrollment_date=enrollment_date,
        incentive_amount=Decimal(_stripped(raw["incentive_amount"])),
        term_months=int(_stripped(raw["term_months"])),
        region=_stripped(raw["region"]),
        submitted_by=_stripped(raw["submitted_by"]),
    )
