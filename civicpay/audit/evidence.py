"""Audit-evidence layer (spec §17.3 Ticket 7).

Two capabilities on top of the hash-chained ledger:

* :func:`verify_chain` — recompute every ``event_hash`` from the persisted
  audit-event rows and confirm the chain links are intact. Detects both content
  tampering (a stored field was altered without recomputing the hash) and chain
  breakage (an event was inserted, deleted, or reordered).
* :func:`export_evidence` — bundle a batch's audit events, the verification
  report, and the batch's backing rows into a single structured JSON evidence
  package. Every batch gets both a reconciliation section and an exception
  section (a batch may legitimately populate only one of them) rather than
  requiring the caller to already know and correctly state which kind of
  batch they're exporting — see the function's docstring for why a
  ``--mode recon|dq`` split (the original design) isn't viable: ``dq_results``
  carries no batch identity and is replaced (not appended) per run, so it
  can't be exported per-batch at all without a schema change.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
from civicpay.audit.ledger import HASHED_FIELDS, compute_event_hash
from civicpay.data import models as M
from civicpay.storage.duckdb import DuckDBStore


def batch_id_in_use(store: DuckDBStore, batch_id: str) -> bool:
    """Return True if any audit event already exists for this batch_id.

    The audit log is append-only and tamper-evident, so re-running a pipeline
    with the same ``batch_id`` collides on ``event_id`` primary keys (a raw
    DuckDB constraint error). This pre-flight check lets pipelines fail fast
    with a clear, actionable message instead — and lets ``run-all`` detect
    collisions before any partial writes happen.
    """
    return not _rows_for_batch(store, batch_id).empty


class BatchIdAlreadyUsedError(ValueError):
    """Raised when a pipeline batch_id already exists in the append-only audit log.

    Carries a clear, actionable message (rather than letting DuckDB raise a raw
    primary-key constraint error deep in the write path) telling the caller to
    use a fresh batch_id. There is intentionally no --force escape hatch: the
    audit log is append-only and tamper-evident, so re-running the same
    batch_id cannot produce a clean result — the guard fails fast instead.
    """

    def __init__(self, batch_id: str) -> None:
        self.batch_id = batch_id
        super().__init__(
            f"Batch id '{batch_id}' already has audit events in the log. "
            f"The audit log is append-only (tamper-evident), so re-running with "
            f"the same batch_id collides on primary keys. Use a fresh "
            f"--batch-id (or --run-id for 'civicpay run-all') for each run."
        )


class UnknownBatchIdError(ValueError):
    """Raised when exporting evidence for a batch_id with zero audit events.

    Without this guard, a mistyped or nonexistent batch_id silently produced a
    structurally valid, empty, ``verified: true`` evidence package — nothing
    distinguished "this batch genuinely has no activity" from "you got the
    batch id wrong." Fail loudly instead.
    """

    def __init__(self, batch_id: str) -> None:
        self.batch_id = batch_id
        super().__init__(
            f"No audit events found for batch_id '{batch_id}' — nothing to export. "
            f"Check the batch id, e.g. via 'civicpay audit verify --batch {batch_id}'."
        )


def _rows_for_batch(store: DuckDBStore, batch_id: str | None) -> pd.DataFrame:
    """Read audit events; optionally filtered to one batch by event_id prefix."""
    df = store.read_table(M.AuditEvent.TABLE)
    if batch_id:
        prefix = f"EVT-{batch_id}-"
        df = df[df["event_id"].astype(str).str.startswith(prefix)]
    return df.sort_values(["timestamp", "event_id"]).reset_index(drop=True)


def verify_chain(store: DuckDBStore, batch_id: str | None = None) -> dict[str, Any]:
    """Verify the integrity of the audit-event hash chain.

    The chain is a linked list: each event's ``previous_hash`` is the
    ``event_hash`` of its predecessor. We walk that chain (rather than sorting by
    timestamp, which is unreliable when events share a timestamp across batches)
    and confirm: (1) there is a single genesis event, (2) every event is reached
    exactly once, and (3) every stored ``event_hash`` matches a recomputation of
    its content. Returns ``verified`` (bool), ``event_count``, and a
    ``broken_event`` dict (``event_id`` + ``reason``) if the chain is broken.
    """
    df = _rows_for_batch(store, batch_id)
    events = [r.to_dict() for _, r in df.iterrows()]
    if not events:
        return {"verified": True, "event_count": 0, "batch_id": batch_id, "broken_event": None}

    hashes = {e["event_hash"] for e in events}
    # successor map: previous_hash -> events that claim to follow it
    successor: dict[str, list[dict]] = {}
    for e in events:
        successor.setdefault(e["previous_hash"] or "", []).append(e)

    # genesis: the first event of a chain. For a full-chain verification
    # (batch_id is None) the genesis must have an empty previous_hash. For a
    # batch-filtered verification, the first event legitimately points at a prior
    # batch's hash (a boundary), so we also allow previous_hash to reference a
    # hash outside the filtered event set.
    if batch_id is None:
        genesis = [e for e in events if (e["previous_hash"] or "") == ""]
    else:
        genesis = [
            e
            for e in events
            if (e["previous_hash"] or "") == "" or (e["previous_hash"] or "") not in hashes
        ]

    broken: dict[str, Any] | None = None
    if len(genesis) != 1:
        reason = "no_genesis" if not genesis else "chain_linkage_broken"
        broken = {
            "event_id": genesis[0]["event_id"] if genesis else None,
            "reason": reason,
            "position": 0,
        }
    else:
        seen: set[str] = set()
        current = genesis[0]
        while current is not None:
            if current["event_id"] in seen:
                broken = broken or {"event_id": current["event_id"], "reason": "cycle_detected"}
                break
            seen.add(current["event_id"])
            body = {field: current[field] for field in HASHED_FIELDS}
            if compute_event_hash(body) != current["event_hash"]:
                broken = broken or {
                    "event_id": current["event_id"],
                    "reason": "content_hash_mismatch",
                    "position": len(seen) - 1,
                }
            succs = successor.get(current["event_hash"], [])
            if len(succs) > 1:
                broken = broken or {"event_id": current["event_id"], "reason": "chain_fork"}
                break
            current = succs[0] if succs else None
        if broken is None and len(seen) != len(events):
            broken = {"event_id": None, "reason": "orphaned_events", "position": len(seen)}

    return {
        "verified": broken is None,
        "event_count": int(len(events)),
        "batch_id": batch_id,
        "broken_event": broken,
    }


def _recon_summary(recon: pd.DataFrame) -> dict[str, int]:
    if recon.empty:
        return {"total": 0, "matched": 0, "exceptions": 0}
    matched = int((recon["match_status"] == M.MatchStatus.MATCHED).sum())
    total = int(len(recon))
    return {"total": total, "matched": matched, "exceptions": total - matched}


def _exceptions_for_batch(store: DuckDBStore, batch_id: str) -> pd.DataFrame:
    """Read exception_queue rows opened by this batch (dash-delimited prefix,
    same technique as ``_rows_for_batch`` for audit events)."""
    df = store.read_table(M.ExceptionItem.TABLE)
    prefix = f"EXC-{batch_id}-"
    df = df[df["exception_id"].astype(str).str.startswith(prefix)]
    return df.sort_values("exception_id").reset_index(drop=True)


def _exception_summary(exceptions: pd.DataFrame) -> dict[str, int]:
    if exceptions.empty:
        return {"total": 0, "open": 0, "in_progress": 0, "resolved": 0}
    counts = exceptions["status"].value_counts()
    return {
        "total": int(len(exceptions)),
        "open": int(counts.get(M.ExceptionStatus.OPEN, 0)),
        "in_progress": int(counts.get(M.ExceptionStatus.IN_PROGRESS, 0)),
        "resolved": int(counts.get(M.ExceptionStatus.RESOLVED, 0)),
    }


def export_evidence(
    store: DuckDBStore,
    batch_id: str,
    out_path: str | Path | None = None,
    full: bool = False,
) -> dict[str, Any]:
    """Build (and optionally persist) the tamper-evident evidence package.

    Every batch gets both a reconciliation section and an exception section —
    a recon batch typically populates only the former, a DQ batch only the
    latter, and that's fine; both are queried the same way regardless of
    which kind of batch was passed, so there's no ``--mode`` to get wrong.
    (``dq_results`` itself carries no batch identity and is replaced, not
    appended, per run — see the module docstring — so it isn't exportable
    per-batch; ``exception_queue``, which is batch-scoped and append-only,
    carries the real batch-level DQ story instead.)

    ``full=False`` (the default) omits the full ``reconciliation_results``
    rows, which can run into the tens of thousands for a large batch — only
    ``reconciliation_summary`` (counts) is included. Pass ``full=True`` for
    the complete backing rows. Exception rows are always included in full:
    they're already bounded by ``max_exceptions_per_check`` at routing time,
    so they don't have the same size problem.

    Raises :class:`UnknownBatchIdError` if the batch has zero audit events —
    a mistyped batch_id would otherwise silently export an empty, structurally
    valid, "verified" package with nothing to flag it as likely wrong.
    """
    verification = verify_chain(store, batch_id)
    if verification["event_count"] == 0:
        raise UnknownBatchIdError(batch_id)
    audit_events = _rows_for_batch(store, batch_id)

    recon = store.query(
        f"SELECT * FROM {M.ReconciliationResult.TABLE} WHERE batch_id = ?",
        [batch_id],
    )
    exceptions = _exceptions_for_batch(store, batch_id)

    scope = {
        "audit_events_rows": int(len(audit_events)),
        "reconciliation_results_rows": int(len(recon)),
        "reconciliation_results_included": full,
        "exception_rows": int(len(exceptions)),
        # A deterministic anchor alongside the wall-clock exported_at below:
        # the actual as-of range this batch's events were recorded at.
        "event_timestamp_range": [
            str(audit_events["timestamp"].min()),
            str(audit_events["timestamp"].max()),
        ],
    }

    package = {
        "batch_id": batch_id,
        "exported_at": datetime.now(UTC).isoformat(),
        "scope": scope,
        "verification": verification,
        "audit_events": audit_events.to_dict(orient="records"),
        "reconciliation_summary": _recon_summary(recon),
        "reconciliation_results": recon.to_dict(orient="records") if full else [],
        "exception_summary": _exception_summary(exceptions),
        "exceptions": exceptions.to_dict(orient="records"),
    }

    if out_path is not None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(package, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
    return package
