"""Dataclass schemas for all CivicPay Open Framework entities.

These schemas are the canonical, framework-internal definitions of the
synthetic data. They are NOT derived from any employer system. Column
shapes and value distributions are original design, generated with Faker.

Each dataclass maps 1:1 to a DuckDB table (see ``civicpay.storage.duckdb``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import ClassVar

# --------------------------------------------------------------------------- #
# Enums (as plain str-typed constants for simplicity; DuckDB stores VARCHAR)
# --------------------------------------------------------------------------- #


class CustomerType:
    INDIVIDUAL = "individual"
    BUSINESS = "business"


class AccountType:
    CHECKING = "checking"
    SAVINGS = "savings"
    LOAN = "loan"
    CREDIT_CARD = "credit_card"


class TransactionType:
    DEBIT = "debit"
    CREDIT = "credit"


class TransactionStatus:
    POSTED = "posted"
    PENDING = "pending"
    REVERSED = "reversed"


class PaymentDirection:
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class PaymentStatus:
    MATCHED = "matched"
    UNMATCHED = "unmatched"
    EXCEPTION = "exception"


class MatchStatus:
    MATCHED = "matched"
    UNMATCHED_PAYMENT = "unmatched_payment"
    UNMATCHED_LEDGER = "unmatched_ledger"
    EXCEPTION = "exception"


class MatchMethod:
    EXACT = "exact"
    FUZZY = "fuzzy"
    MANUAL = "manual"


class ExceptionReason:
    DUPLICATE = "duplicate"
    AMOUNT_MISMATCH = "amount_mismatch"
    STALE = "stale"
    MISSING_REF = "missing_ref"


class DQCheckType:
    COMPLETENESS = "completeness"
    ACCURACY = "accuracy"
    CONSISTENCY = "consistency"
    TIMELINESS = "timeliness"
    ANOMALY = "anomaly"


class ExceptionSource:
    RECON = "recon"
    DQ = "dq"


class ExceptionPriority:
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class ExceptionStatus:
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"


class AuditEventType:
    INGEST = "ingest"
    MATCH = "match"
    EXCEPTION_OPEN = "exception_open"
    EXCEPTION_RESOLVE = "exception_resolve"
    CONFIG_CHANGE = "config_change"
    DQ_CHECK = "dq_check"


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #


@dataclass
class Customer:
    customer_id: str
    name: str
    email: str
    phone: str
    address: str
    customer_type: str
    created_at: datetime
    status: str

    TABLE: ClassVar[str] = "customers"


@dataclass
class Account:
    account_id: str
    customer_id: str
    account_type: str
    account_number_masked: str
    currency: str
    current_balance: Decimal
    available_balance: Decimal
    opened_at: datetime
    status: str

    TABLE: ClassVar[str] = "accounts"


@dataclass
class Transaction:
    transaction_id: str
    account_id: str
    transaction_type: str
    amount: Decimal
    currency: str
    posting_date: date
    value_date: date
    description: str
    reference_id: str
    created_at: datetime
    status: str

    TABLE: ClassVar[str] = "transactions"


@dataclass
class PaymentFile:
    file_id: str
    file_name: str
    received_at: datetime
    record_count: int
    total_amount: Decimal
    source_system: str
    status: str

    TABLE: ClassVar[str] = "payment_files"


@dataclass
class PaymentRecord:
    payment_id: str
    file_id: str
    reference_id: str
    amount: Decimal
    currency: str
    direction: str
    counterparty: str
    payment_date: date
    expected_posting_date: date
    status: str

    TABLE: ClassVar[str] = "payment_records"


@dataclass
class ReconciliationResult:
    recon_id: str
    batch_id: str
    payment_id: str
    ledger_transaction_id: str | None
    match_status: str
    match_confidence: Decimal
    match_method: str
    exception_reason: str | None
    reconciled_at: datetime
    reconciled_by: str

    TABLE: ClassVar[str] = "reconciliation_results"


@dataclass
class DQResult:
    dq_check_id: str
    dataset_name: str
    check_type: str
    check_name: str
    passed: bool
    failing_records: int
    quality_score: Decimal
    checked_at: datetime

    TABLE: ClassVar[str] = "dq_results"


@dataclass
class ExceptionItem:
    exception_id: str
    source: str
    reference_id: str
    priority: str
    assigned_to: str | None
    status: str
    created_at: datetime
    resolved_at: datetime | None
    resolution_notes: str | None
    root_cause: str | None

    TABLE: ClassVar[str] = "exception_queue"


@dataclass
class AuditEvent:
    event_id: str
    timestamp: datetime
    event_type: str
    actor: str
    entity_type: str
    entity_id: str
    action: str
    previous_hash: str
    event_hash: str

    TABLE: ClassVar[str] = "audit_event_log"


# Registry: table name -> dataclass (used by storage layer)
ENTITY_MODELS: dict[str, type] = {
    Customer.TABLE: Customer,
    Account.TABLE: Account,
    Transaction.TABLE: Transaction,
    PaymentFile.TABLE: PaymentFile,
    PaymentRecord.TABLE: PaymentRecord,
    ReconciliationResult.TABLE: ReconciliationResult,
    DQResult.TABLE: DQResult,
    ExceptionItem.TABLE: ExceptionItem,
    AuditEvent.TABLE: AuditEvent,
}

# Default generation volumes (Section 13.2 of the spec)
DEFAULT_VOLUMES: dict[str, int] = {
    "customers": 10_000,
    "accounts": 5_000,
    "transactions": 50_000,
    "payment_records": 1_000,  # one payment file with 1000 records
}
