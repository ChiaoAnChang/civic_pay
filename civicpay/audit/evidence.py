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

    Returns a report with ``verified`` (bool), ``event_count``, and a
    ``broken_event`` dict (``event_id`` + ``reason``) if the chain is broken.
    """
    df = _rows_for_batch(store, batch_id)
    broken: dict[str, Any] | None = None
    prev_hash = ""
    # When verifying a single batch, the first event's previous_hash legitimately
    # points at a prior batch's last event (a chain boundary), so we allow it.
    allow_boundary = batch_id is not None

    for i, (_, row) in enumerate(df.iterrows()):
        r = row.to_dict()
        body = {field: r[field] for field in HASHED_FIELDS}
        recomputed = compute_event_hash(body)
        content_ok = recomputed == r["event_hash"]

        if i == 0 and allow_boundary:
            link_ok = True  # boundary: previous_hash may point outside the batch
        else:
            link_ok = r["previous_hash"] == prev_hash

        if broken is None and (not content_ok or not link_ok):
            reason = "content_hash_mismatch" if not content_ok else "chain_linkage_broken"
            broken = {"event_id": r["event_id"], "reason": reason, "position": i}

        prev_hash = r["event_hash"]

    return {
        "verified": broken is None,
        "event_count": int(len(df)),
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
