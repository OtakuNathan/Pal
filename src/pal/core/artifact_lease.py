"""Attachment/artifact leases across compaction (Q12/R08).

A lease is not a second ownership registry: an artifact referenced by a
durably staged pending event (Q12) or by a committed L1/seed state (R08)
keeps its hot TTL refreshed, so the ordinary reap_expired tick cannot
retire content a pending consumer still needs. The queue stores
references only — artifact bytes are never copied into the staging file.
"""
from __future__ import annotations

from typing import Any, Iterable


def _collect_ids(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        artifact_id = str(node.get("artifact_id") or "").strip()
        if artifact_id and not artifact_id.startswith("unavailable:"):
            out.add(artifact_id)
        for child in node.values():
            _collect_ids(child, out)
    elif isinstance(node, (list, tuple)):
        for child in node:
            _collect_ids(child, out)


def _collect_message_ids(message: Any, out: set[str]) -> None:
    _collect_ids(getattr(message, "payload", None), out)
    for part in getattr(message, "parts", ()) or ():
        part_id = str(getattr(part, "artifact_id", "") or "").strip()
        if part_id and not part_id.startswith("unavailable:"):
            out.add(part_id)


def artifact_ids_from_staged_records(records: Iterable[Any]) -> set[str]:
    """Artifact references carried by durably staged pending events."""
    ids: set[str] = set()
    for record in records or ():
        payload = getattr(record, "payload", None)
        if isinstance(payload, (dict, list, tuple)):
            _collect_ids(payload, ids)
            if isinstance(payload, dict):
                # Typed message-ir staging keeps parts under "parts".
                _collect_ids(payload.get("parts"), ids)
    return ids


def artifact_ids_from_l1_turns(turns: Iterable[Any]) -> set[str]:
    """Artifact references living in L1 turns (active input or seed)."""
    ids: set[str] = set()
    for turn in turns or ():
        for message in getattr(turn, "messages", ()) or ():
            _collect_message_ids(message, ids)
    return ids


def touch_artifacts(context: Any, artifact_ids: Iterable[str], scope_key: str) -> int:
    """Refresh the hot TTL of each referenced artifact (the lease).

    Failures are non-fatal: a lease refresh must never break the turn or
    the compaction that carries it; a missing/unreadable artifact simply
    yields no lease.
    """
    service = getattr(context, "port_registry", {}).get("artifact:artifact")
    scope = str(scope_key or "").strip()
    if service is None or not scope:
        return 0
    touched = 0
    for artifact_id in sorted(set(str(item or "").strip() for item in artifact_ids or ())):
        if not artifact_id:
            continue
        select = getattr(service, "select", None)
        if not callable(select):
            break
        try:
            select(artifact_id, scope)
            touched += 1
        except Exception:
            continue
    return touched


__all__ = [
    "artifact_ids_from_staged_records",
    "artifact_ids_from_l1_turns",
    "touch_artifacts",
]
