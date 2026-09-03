"""In-flight enrollment record shape (Ticket 13).

``EnrollmentRecord`` is the *typed, validated* form of a candidate — built
from a raw ``pending_enrollments`` row (or a CSV row) only after
``validators.validate`` passes. It is not itself persisted; the persisted
shapes (``PendingEnrollment``, ``AcceptedEnrollment``, ``DualSourceResult``)
live in ``civicpay.data.models`` alongside every other framework table, so
the storage layer needs no enrollment-specific import.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal


@dataclass
class EnrollmentRecord:
    enrollment_id: str
    entity_id: str
    program_code: str
    enrollment_date: datetime
    incentive_amount: Decimal
    term_months: int
    region: str
    submitted_by: str
