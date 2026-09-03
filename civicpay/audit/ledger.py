"""Minimal hash-chained (tamper-evident) audit ledger core.

This module provides the append-only ledger primitives shared by the
reconciliation pipeline (and, later, the full audit-evidence layer). Every
event is written with a content hash over the canonical serialization of its
stored fields (including the previous event's hash), so the chain is
tamper-evident and re-computable from the persisted rows alone: a later
verifier reads ``audit_event_log``, recomputes each ``event_hash`` from the
stored columns, and confirms every ``previous_hash`` links to its predecessor.

Verification (detecting a broken/tampered chain) and evidence export are
intentionally out of scope here and belong to the audit-evidence ticket; this
module only writes events with a valid, verifiable chain so that later
verification can recompute it.

Canonicalization rules (original clean-room design):

* Object keys are sorted lexicographically.
* Timestamps are UTC, ISO-8601 (naive datetimes are assumed UTC).
* Decimals are serialized as plain strings (no float re-encoding drift).
* The previous event's hash is part of the current event's hashed payload, so a
  change anywhere in the chain invalidates every subsequent hash.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import duckdb
import pandas as pd
from civicpay.data import models as M
from civicpay.data.synthetic import AS_OF_DATETIME
from civicpay.storage.duckdb import DuckDBStore

# Fields whose canonical serialization is hashed. These are exactly the columns
# persisted to audit_event_log (minus event_hash itself), so a verifier can
# recompute the hash from the stored row.
HASHED_FIELDS: tuple[str, ...] = (
    "event_id",
    "timestamp",
    "event_type",
    "actor",
    "entity_type",
    "entity_id",
    "action",
    "previous_hash",
)


def _default(obj: Any) -> Any:
    """JSON encoder fallback for non-native types (datetime, Decimal)."""
    if isinstance(obj, datetime):
        # Force UTC. Naive datetimes are treated as already UTC.
        aware = obj if obj.tzinfo else obj.replace(tzinfo=UTC)
        return aware.astimezone(UTC).isoformat()
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def canonical_json(payload: dict[str, Any]) -> str:
    """Serialize ``payload`` to a stable, sorted-key JSON string.

    Determinism: the same payload always yields the same string, regardless of
    dict insertion order, across Python runs.
    """
    return json.dumps(payload, sort_keys=True, default=_default, ensure_ascii=False)


def compute_event_hash(payload: dict[str, Any]) -> str:
    """Return the SHA-256 hex digest of ``canonical_json(payload)``."""
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def event_body_from_row(row: dict[str, Any]) -> dict[str, Any]:
    """Reconstruct the hashable body from a persisted audit-event row.

    Used by verifiers to recompute ``event_hash`` from stored columns.
    """
    return {field: row[field] for field in HASHED_FIELDS}


@dataclass
class AuditLedger:
    """Append-only, hash-chained audit ledger backed by the DuckDB store.

    Each appended event records the previous event's hash (empty string for the
    first event) and a content hash over the canonical serialization of the
    event's stored fields including that previous hash. This makes the chain
    tamper-evident: a later verifier can recompute every hash and detect any
    alteration.
    """

    store: DuckDBStore
    actor: str = "system"
    as_of: datetime = field(default_factory=lambda: AS_OF_DATETIME)
    _previous_hash: str = field(default="", init=False)
    _seq: int = field(default=0, init=False)
    _batch_id: str = field(default="BATCH", init=False)
    _chain_initialized: bool = field(default=False, init=False)

    def _initialize_chain(self) -> None:
        """Resume the chain from its true tip, if any.

        The tip is *not* well-approximated by ``ORDER BY timestamp DESC,
        event_id DESC`` — that was tried and is unsound. Multiple batches
        (recon, DQ, ...) share the same deterministic ``as_of`` timestamp, so
        ties break on ``event_id`` (which embeds the batch id) as a plain
        string: e.g. ``"EVT-R1-..."`` sorts after ``"EVT-D1-..."`` regardless
        of which was actually appended more recently. A *new* ``AuditLedger``
        instance created after several same-timestamp batches already coexist
        (as the deterministic backlog cohort does, appending its own
        events right after the main DQ batch's) could then resume from a
        stale, non-tip event — and if that event already has a different
        child, the new append forks the chain (observed in practice: a
        ``chain_fork`` at the true tip's predecessor).

        The chain's own structure gives an unambiguous, ordering-free answer
        instead: the tip is whichever ``event_hash`` is never referenced as
        anyone's ``previous_hash``.
        """
        try:
            row = self.store.execute(
                f"SELECT event_hash FROM {M.AuditEvent.TABLE} t "
                f"WHERE NOT EXISTS ("
                f"SELECT 1 FROM {M.AuditEvent.TABLE} n WHERE n.previous_hash = t.event_hash"
                f") LIMIT 1"
            ).fetchone()
        except Exception:
            row = None
        self._previous_hash = row[0] if row else ""
        self._chain_initialized = True

    def _next_row(
        self,
        event_type: str,
        entity_type: str,
        entity_id: str,
        action: str,
        batch_id: str,
        timestamp: datetime,
    ) -> dict[str, Any]:
        """Build one hash-chained event row (does not persist it)."""
        if not self._chain_initialized:
            self._batch_id = batch_id
            self._initialize_chain()
        self._seq += 1
        event_id = f"EVT-{self._batch_id}-{self._seq:06d}"
        body = {
            "event_id": event_id,
            "timestamp": timestamp,
            "event_type": event_type,
            "actor": self.actor,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "action": action,
            "previous_hash": self._previous_hash,
        }
        event_hash = compute_event_hash(body)
        row = {**body, "event_hash": event_hash}
        self._previous_hash = event_hash
        return row

    # Retry budget for _write_rows_with_retry. Not reachable under DuckDB's
    # own single-writer file lock (see docs/cloud-backend.md) — a small,
    # finite bound exists so a bug that made this genuinely unreachable path
    # loop forever would fail loudly (RuntimeError) instead of hanging.
    _MAX_APPEND_ATTEMPTS = 5

    def _write_rows_with_retry(
        self, row_builder: Callable[[], list[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        """Build rows via ``row_builder`` and persist them, retrying against
        the true chain tip if a concurrent writer advanced it first.

        The ``previous_hash`` ``UNIQUE`` constraint (``civicpay.storage.
        duckdb.SCHEMA_DDL``) turns the read-then-write race in
        ``_initialize_chain``'s docstring into a catchable
        ``duckdb.ConstraintException`` instead of a silent chain fork: if
        another writer's event claimed the same ``previous_hash`` first,
        this instance's insert fails cleanly rather than both being
        accepted. On that specific exception, roll back this instance's
        local sequence counter, re-resolve the true tip, and rebuild the
        same logical rows chained off the corrected tip.

        Under DuckDB's own single-writer file lock, a second writer can
        never even open the database concurrently, so this retry path is
        unreachable and untested against a live race today — it exists so
        this code is not itself the reason a future backend that does
        permit concurrent writers (see docs/cloud-backend.md) would still
        be unsafe.
        """
        last_error: duckdb.ConstraintException | None = None
        for _attempt in range(self._MAX_APPEND_ATTEMPTS):
            seq_before = self._seq
            rows = row_builder()
            if not rows:
                return rows
            try:
                self.store.write_dataframe(M.AuditEvent.TABLE, pd.DataFrame(rows), mode="append")
                return rows
            except duckdb.ConstraintException as e:
                last_error = e
                self._seq = seq_before
                self._chain_initialized = False
                self._initialize_chain()
        raise RuntimeError(
            f"Failed to append audit event(s) after {self._MAX_APPEND_ATTEMPTS} "
            f"attempts due to a persistent chain-tip conflict: {last_error}"
        )

    def append(
        self,
        event_type: str,
        entity_type: str,
        entity_id: str,
        action: str,
        payload: dict[str, Any] | None = None,  # noqa: ARG002 - reserved for Ticket 7
        batch_id: str = "BATCH",
        timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        """Append and persist one audit event; return the persisted row.

        ``payload`` is accepted for forward-compatibility with the audit-evidence
        ticket (which will persist it in a dedicated column); it is not hashed
        here, because only stored fields are hashed so the chain stays
        re-computable from the table alone.
        """
        rows = self._write_rows_with_retry(
            lambda: [
                self._next_row(
                    event_type=event_type,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    action=action,
                    batch_id=batch_id,
                    timestamp=timestamp or self.as_of,
                )
            ]
        )
        return rows[0]

    def append_many(self, events: list[dict[str, Any]]) -> pd.DataFrame:
        """Append and persist a batch of events in one write.

        ``events`` is a list of dicts with keys: event_type, entity_type,
        entity_id, action, payload (optional, reserved), batch_id (optional),
        timestamp (optional). Returns the persisted rows as a DataFrame.
        """

        def build() -> list[dict[str, Any]]:
            return [
                self._next_row(
                    event_type=ev["event_type"],
                    entity_type=ev["entity_type"],
                    entity_id=ev["entity_id"],
                    action=ev["action"],
                    batch_id=ev.get("batch_id", "BATCH"),
                    timestamp=ev.get("timestamp") or self.as_of,
                )
                for ev in events
            ]

        rows = self._write_rows_with_retry(build) if events else []
        return pd.DataFrame(rows) if rows else pd.DataFrame()
