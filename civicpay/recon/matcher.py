"""Payment reconciliation matcher (spec §14.1).

Matches an inbound payment file against ledger entries and classifies every
payment record. The matcher is a pure function of its inputs (payments,
transactions, config, batch_id, as_of): no I/O, no wall-clock time, no global
state. This makes it fully deterministic and trivially testable.

Classification (per payment record, in file order):

1. **matched / exact** — an unconsumed ledger entry with the same normalized
   reference, amount within tolerance, and posting date within the window.
2. **exception / duplicate** — the reference matches an already-consumed ledger
   entry, and the amount and date also agree (a plausible duplicate payment).
3. **exception / amount_mismatch** — the reference matches a ledger entry but
   the amount is outside tolerance (the ledger entry is left unconsumed).
4. **matched / fuzzy** — no exact reference, but a candidate passes amount +
   date filters and the normalized reference strings are Levenshtein-close.
5. **unmatched_payment** — no ledger entry matches by reference or fuzzy.

Ledger entries never matched by any payment become ``unmatched_ledger`` rows
(assembled by the pipeline, not here).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd
from civicpay.data import models as M
from civicpay.data.synthetic import AS_OF_DATETIME

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ReconConfig:
    """Tunable reconciliation parameters (loaded from ``config/recon.yml``)."""

    amount_tolerance: float = 1.0
    date_window_days: int = 1
    fuzzy_threshold: float = 0.85
    stale_days: int = 30

    def within_amount(self, a: float, b: float) -> bool:
        return abs(a - b) <= self.amount_tolerance

    def within_date_window(self, payment_date: date, ledger_date: date) -> bool:
        return abs((payment_date - ledger_date).days) <= self.date_window_days

    def is_stale(self, payment_date: date, as_of: date) -> bool:
        return (as_of - payment_date).days > self.stale_days


# --------------------------------------------------------------------------- #
# Reference normalization
# --------------------------------------------------------------------------- #

_SEPARATOR_RE = re.compile(r"[\s\-_]+")


def normalize_reference(ref: str) -> str:
    """Normalize a reference id for matching: strip, uppercase, drop separators.

    ``"REF-M-0001"`` -> ``"REFM0001"``; ``" ref_t_0002 "`` -> ``"REFT0002"``.
    """
    if ref is None:
        return ""
    return _SEPARATOR_RE.sub("", str(ref)).upper()


# --------------------------------------------------------------------------- #
# Fuzzy string similarity (rapidfuzz-backed, graceful fallback)
# --------------------------------------------------------------------------- #


def _ratio(a: str, b: str) -> float:
    """Return a 0..1 normalized similarity between two strings."""
    try:
        from rapidfuzz import fuzz  # type: ignore[import]
    except Exception:  # pragma: no cover - rapidfuzz is a declared dependency
        return 0.0
    return fuzz.ratio(a, b) / 100.0


# --------------------------------------------------------------------------- #
# Ledger index
# --------------------------------------------------------------------------- #


@dataclass
class _LedgerEntry:
    transaction_id: str
    reference_id: str
    normalized_ref: str
    amount: float
    posting_date: date
    currency: str


@dataclass
class LedgerIndex:
    """Index of ledger entries keyed by normalized reference.

    A single normalized reference may map to several ledger entries (duplicate
    reference ids in the ledger); each is matched and consumed independently.
    """

    by_ref: dict[str, list[_LedgerEntry]] = field(default_factory=dict)
    all_entries: list[_LedgerEntry] = field(default_factory=list)
    consumed: set[str] = field(default_factory=set)

    @classmethod
    def from_transactions(cls, transactions: pd.DataFrame) -> LedgerIndex:
        idx = cls()
        for _, t in transactions.iterrows():
            ref = normalize_reference(t["reference_id"])
            entry = _LedgerEntry(
                transaction_id=str(t["transaction_id"]),
                reference_id=str(t["reference_id"]),
                normalized_ref=ref,
                amount=float(t["amount"]),
                posting_date=_to_date(t["posting_date"]),
                currency=str(t["currency"]),
            )
            idx.by_ref.setdefault(ref, []).append(entry)
            idx.all_entries.append(entry)
        return idx

    def unconsumed_candidates(self, ref: str) -> list[_LedgerEntry]:
        return [e for e in self.by_ref.get(ref, []) if e.transaction_id not in self.consumed]

    def consumed_candidates(self, ref: str) -> list[_LedgerEntry]:
        return [e for e in self.by_ref.get(ref, []) if e.transaction_id in self.consumed]


def _to_date(value: Any) -> date:
    """Coerce a pandas/Python value to a ``date``."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    return pd.Timestamp(value).date()


# --------------------------------------------------------------------------- #
# Result row
# --------------------------------------------------------------------------- #


@dataclass
class MatchResult:
    payment_id: str
    ledger_transaction_id: str | None
    match_status: str
    match_method: str | None
    match_confidence: float
    exception_reason: str | None
    reconciled_at: datetime
    recon_id: str
    batch_id: str


# --------------------------------------------------------------------------- #
# Matcher
# --------------------------------------------------------------------------- #


def reconcile(
    payments: pd.DataFrame,
    transactions: pd.DataFrame,
    config: ReconConfig,
    batch_id: str,
    as_of: datetime = AS_OF_DATETIME,
) -> tuple[list[MatchResult], LedgerIndex, dict[str, Any]]:
    """Reconcile ``payments`` against ``transactions``.

    Returns ``(results, ledger_index, summary)`` where ``results`` has one
    entry per payment record and ``ledger_index.consumed`` records which ledger
    transactions were matched (so the pipeline can derive unmatched_ledger).
    """
    ledger = LedgerIndex.from_transactions(transactions)
    results: list[MatchResult] = []
    seq = 0

    counts = {
        M.MatchStatus.MATCHED: 0,
        M.MatchStatus.UNMATCHED_PAYMENT: 0,
        M.MatchStatus.EXCEPTION: 0,
        "matched_exact": 0,
        "matched_fuzzy": 0,
        "exception_duplicate": 0,
        "exception_amount_mismatch": 0,
        "exception_stale": 0,
    }
    reconciled_amount = 0.0

    for _, p in payments.iterrows():
        seq += 1
        payment_id = str(p["payment_id"])
        ref = normalize_reference(p["reference_id"])
        amount = float(p["amount"])
        pdate = _to_date(p["payment_date"])
        currency = str(p["currency"])

        status, method, ledger_id, confidence, reason = _classify(
            ref=ref,
            amount=amount,
            pdate=pdate,
            currency=currency,
            ledger=ledger,
            config=config,
        )

        # Stale upgrade: an unmatched payment older than stale_days becomes an
        # exception rather than a silent unmatched_payment.
        if status == M.MatchStatus.UNMATCHED_PAYMENT and config.is_stale(pdate, as_of.date()):
            status = M.MatchStatus.EXCEPTION
            reason = M.ExceptionReason.STALE
            method = None

        if status == M.MatchStatus.MATCHED:
            reconciled_amount += amount
            if method == M.MatchMethod.EXACT:
                counts["matched_exact"] += 1
            else:
                counts["matched_fuzzy"] += 1
        elif status == M.MatchStatus.EXCEPTION:
            if reason == M.ExceptionReason.DUPLICATE:
                counts["exception_duplicate"] += 1
            elif reason == M.ExceptionReason.AMOUNT_MISMATCH:
                counts["exception_amount_mismatch"] += 1
            elif reason == M.ExceptionReason.STALE:
                counts["exception_stale"] += 1

        counts[status] += 1

        results.append(
            MatchResult(
                payment_id=payment_id,
                ledger_transaction_id=ledger_id,
                match_status=status,
                match_method=method,
                match_confidence=confidence,
                exception_reason=reason,
                reconciled_at=as_of,
                recon_id=f"RECON-{batch_id}-{seq:06d}",
                batch_id=batch_id,
            )
        )

    consumed = ledger.consumed
    ledger_count = len(ledger.all_entries)
    matched_total = counts["matched_exact"] + counts["matched_fuzzy"]
    summary: dict[str, Any] = {
        "batch_id": batch_id,
        "payments_processed": len(results),
        "ledger_records_processed": ledger_count,
        "matched_exact": counts["matched_exact"],
        "matched_fuzzy": counts["matched_fuzzy"],
        "matched_total": matched_total,
        "unmatched_payment": counts[M.MatchStatus.UNMATCHED_PAYMENT],
        "exception_duplicate": counts["exception_duplicate"],
        "exception_amount_mismatch": counts["exception_amount_mismatch"],
        "exception_stale": counts["exception_stale"],
        "exception_total": counts[M.MatchStatus.EXCEPTION],
        "unmatched_ledger": ledger_count - len(consumed),
        "total_amount_reconciled": round(reconciled_amount, 2),
        "reconciliation_rate": round(matched_total / len(results), 4) if results else 0.0,
    }
    return results, ledger, summary


def _classify(
    ref: str,
    amount: float,
    pdate: date,
    currency: str,
    ledger: LedgerIndex,
    config: ReconConfig,
) -> tuple[str, str | None, str | None, float, str | None]:
    """Classify one payment record. Returns (status, method, ledger_id, confidence, reason)."""
    # --- Exact reference candidates ---------------------------------------- #
    unconsumed = ledger.unconsumed_candidates(ref)
    consumed = ledger.consumed_candidates(ref)

    # 1. matched/exact: an unconsumed entry with matching amount + date.
    for entry in unconsumed:
        if (
            entry.currency == currency
            and config.within_amount(amount, entry.amount)
            and config.within_date_window(pdate, entry.posting_date)
        ):
            ledger.consumed.add(entry.transaction_id)
            return M.MatchStatus.MATCHED, M.MatchMethod.EXACT, entry.transaction_id, 1.0, None

    # 2. exception/duplicate: reference already consumed, amount + date agree.
    for entry in consumed:
        if (
            entry.currency == currency
            and config.within_amount(amount, entry.amount)
            and config.within_date_window(pdate, entry.posting_date)
        ):
            return (
                M.MatchStatus.EXCEPTION,
                None,
                entry.transaction_id,
                1.0,
                M.ExceptionReason.DUPLICATE,
            )

    # 3. exception/amount_mismatch: the reference matches a ledger entry, but
    #    the amount is outside tolerance on every candidate. The ledger entry is
    #    left unconsumed for investigation.
    if unconsumed or consumed:
        amount_ok = any(
            e.currency == currency and config.within_amount(amount, e.amount)
            for e in (unconsumed + consumed)
        )
        if not amount_ok:
            link = (unconsumed or consumed)[0].transaction_id
            return (
                M.MatchStatus.EXCEPTION,
                None,
                link,
                0.0,
                M.ExceptionReason.AMOUNT_MISMATCH,
            )

    # --- Fuzzy matching ----------------------------------------------------- #
    best_entry = _fuzzy_match(ref, amount, pdate, currency, ledger, config)
    if best_entry is not None:
        ledger.consumed.add(best_entry.transaction_id)
        confidence = _ratio(ref, best_entry.normalized_ref)
        return (
            M.MatchStatus.MATCHED,
            M.MatchMethod.FUZZY,
            best_entry.transaction_id,
            round(confidence, 4),
            None,
        )

    # 5. unmatched_payment.
    return M.MatchStatus.UNMATCHED_PAYMENT, None, None, 0.0, None


def _fuzzy_match(
    ref: str,
    amount: float,
    pdate: date,
    currency: str,
    ledger: LedgerIndex,
    config: ReconConfig,
) -> _LedgerEntry | None:
    """Find the best unconsumed ledger entry by amount + date + string similarity.

    Candidates are pre-filtered on currency, fuzzy amount tolerance, and fuzzy
    date window before the (more expensive) Levenshtein ratio is applied, so the
    fuzzy scorer only runs on a small candidate set.
    """
    if not ref:
        return None

    best: _LedgerEntry | None = None
    best_score = config.fuzzy_threshold
    for entry in ledger.all_entries:
        if entry.transaction_id in ledger.consumed:
            continue
        if entry.currency != currency:
            continue
        if not config.within_amount(amount, entry.amount):
            continue
        if not config.within_date_window(pdate, entry.posting_date):
            continue
        score = _ratio(ref, entry.normalized_ref)
        if score >= best_score:
            best_score = score
            best = entry
    return best
