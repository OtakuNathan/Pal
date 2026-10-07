"""Current finding CRUD; work_items owns the shared ledger and creation contract."""
from __future__ import annotations
from typing import Any, Mapping


def prepare_finding_edit(workspace: Mapping[str, Any], args: Mapping[str, Any]):
    from pal.bunshin.v2.draft_values import (
        BunshinUpdateFindingInput, BunshinRemoveFindingInput, normalize_finding, _finding_hash,
    )
    if dict(workspace.get("bunshin_v2") or {}).get("role") != "verifier":
        raise ValueError("only a verifier can edit its current draft findings")
    updating = "finding_kind" in args
    model = BunshinUpdateFindingInput if updating else BunshinRemoveFindingInput
    values = model.model_validate(args, strict=True).model_dump(exclude_none=True)
    finding_id = values.pop("finding_id")
    revision = values.pop("expected_revision")
    finding = normalize_finding(values) if updating else None
    reason = str(values.get("reason") or "").strip() if not updating else ""
    if not updating and not reason:
        raise ValueError("removing a finding requires an audit reason")

    def reducer(payload: dict[str, Any]):
        items = [dict(item) for item in payload.get("items", [])]
        target = next((item for item in items if item.get("kind") == "finding"
                       and item.get("item_id") == finding_id), None)
        if updating and revision == 0:
            if finding_id in set(payload.get("_reserved_finding_ids") or []) or target is not None:
                raise ValueError("finding identity was already used or belongs to protected history; choose a never-used ID")
            if not finding_id.strip() or finding_id != finding_id.strip():
                raise ValueError("finding_id must be a non-empty exact draft-local key without surrounding whitespace")
            assert finding is not None
            semantic_hash = _finding_hash(finding)
            if any(item.get("semantic_hash") == semantic_hash for item in items):
                raise ValueError("another current finding already records this content; update that identity")
            items.append({"item_id": finding_id, "kind": "finding", "status": "completed",
                          "summary": finding["summary"], "ordinal": len(items), "origin": "verifier:current",
                          "semantic_hash": semantic_hash, "finding": finding, "revision": 1})
            payload["items"] = items
            return payload, {"created": True, "updated": True, "finding_id": finding_id, "revision": 1}
        if target is None:
            raise ValueError("finding identity is not in the current editable draft; external/submitted history cannot be edited")
        if int(target.get("revision") or 1) != revision:
            raise ValueError("finding revision is stale; call read_verification_draft_status before editing")
        if updating:
            assert finding is not None
            semantic_hash = _finding_hash(finding)
            if any(item is not target and item.get("semantic_hash") == semantic_hash for item in items):
                raise ValueError("update would duplicate another current finding")
            target.update(finding=finding, summary=finding["summary"], semantic_hash=semantic_hash,
                          revision=revision + 1)
        else:
            items.remove(target)
        payload["items"] = items
        return payload, {"updated" if updating else "removed": True,
                         "finding_id": finding_id, "revision": revision + int(updating),
                         **({"reason": reason} if reason else {})}

    return reducer
