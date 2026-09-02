"""Audit-evidence layer (spec §17.3 Ticket 7).

Two capabilities on top of the hash-chained ledger:

* :func:`verify_chain` — recompute every ``event_hash`` from the persisted
  audit-event rows and confirm the chain links are intact. Detects both content
  tampering (a stored field was altered without recomputing the hash) and chain
  breakage (an event was inserted, deleted, or reordered).
* :func:`export_evidence` — bundle a batch's audit events, the verification
  report, and the reconciliation rows backing the batch's claims into a single
  structured JSON evidence package.
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


def export_evidence(
    store: DuckDBStore,
    batch_id: str,
    out_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build (and optionally persist) the tamper-evident evidence package.

    The package contains: the verification report, the batch's audit events (with
    their hashes), and the reconciliation rows backing the batch's claims.
    """
    verification = verify_chain(store, batch_id)
    audit_events = _rows_for_batch(store, batch_id)

    recon = store.query(
        f"SELECT * FROM {M.ReconciliationResult.TABLE} WHERE batch_id = ?",
        [batch_id],
    )
    summary = _recon_summary(recon)

    package = {
        "batch_id": batch_id,
        "exported_at": datetime.now(UTC).isoformat(),
        "verification": verification,
        "audit_events": audit_events.drop(columns=[]).to_dict(orient="records"),
        "reconciliation_summary": summary,
        "reconciliation_results": recon.to_dict(orient="records"),
    }

    if out_path is not None:
        out = Path(out_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(package, indent=2, default=str, ensure_ascii=False),
            encoding="utf-8",
        )
    return package
