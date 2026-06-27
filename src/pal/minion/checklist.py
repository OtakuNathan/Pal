from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChecklistItem:
    item_id: str
    kind: str
    source_text: str
    status: str = "pending"
    source_kind: str = ""
    source_ref: str = ""
    parent_item_id: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            key: value
            for key, value in {
                "id": self.item_id,
                "kind": self.kind,
                "status": self.status,
                "source_text": self.source_text,
                "source_kind": self.source_kind,
                "source_ref": self.source_ref,
                "parent_item_id": self.parent_item_id,
                "metadata": dict(self.metadata),
            }.items()
            if value not in ("", {}, [], None)
        }


def build_acceptance_checklist(*sources: Any, status: str = "pending") -> list[dict[str, Any]]:
    items: list[ChecklistItem] = []
    seen: set[str] = set()
    for source in sources:
        for text in string_list(source):
            token = loose_token(text)
            if not token or token in seen:
                continue
            seen.add(token)
            items.append(
                ChecklistItem(
                    item_id=f"AC-{len(items) + 1}",
                    kind="acceptance",
                    source_text=text,
                    status=status,
                    source_kind="acceptance_criteria",
                )
            )
    return [item.to_dict() for item in items]


def build_evidence_projection(refs: Any) -> list[dict[str, Any]]:
    projected: list[dict[str, Any]] = []
    for raw in list(refs or []):
        if not isinstance(raw, dict):
            continue
        ref = dict(raw)
        item: dict[str, Any] = {
            "id": f"EV-{len(projected) + 1}",
            "kind": str(ref.get("kind") or "").strip(),
            "status": ref.get("status"),
            "ok": ref.get("ok"),
            "summary": _first_text(ref, "summary", "command", "path", "query"),
        }
        for key in (
            "evidence_ref_id",
            "call_id",
            "ledger_id",
            "tool_name",
            "operation",
            "command",
            "cwd",
            "exit_code",
            "path",
            "query",
            "evidence_id",
            "method",
            "server_id",
            "workspace_root",
            "file",
            "file_sha256",
            "freshness",
            "unavailable_reason",
            "stdout_preview",
            "stderr_preview",
        ):
            if ref.get(key) not in (None, "", []):
                item[key] = ref.get(key)
        projected.append({key: value for key, value in item.items() if value not in (None, "", [])})
    return projected


def resolve_acceptance_ref(value: Any, checklist: list[dict[str, Any]]) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    normalized = text.lower().replace("_", "-")
    for item in checklist:
        item_id = str(item.get("id") or "").strip()
        if item_id and normalized == item_id.lower():
            return item_id
    compact = normalized.replace("-", "").lstrip("#")
    if compact.isdigit():
        return _acceptance_id_at(checklist, int(compact))
    if compact.startswith("ac") and compact[2:].isdigit():
        return _acceptance_id_at(checklist, int(compact[2:]))
    loose = loose_token(text)
    for item in checklist:
        if loose and loose == loose_token(item.get("source_text")):
            return str(item.get("id") or "").strip()
    return text


def normalize_coverage_refs(value: Any, checklist: list[dict[str, Any]], *, legacy_index: bool = False) -> Any:
    if isinstance(value, (list, tuple, set)):
        return [normalize_coverage_refs(item, checklist, legacy_index=legacy_index) for item in value]
    if isinstance(value, dict):
        return {str(key): normalize_coverage_refs(nested, checklist, legacy_index=legacy_index) for key, nested in value.items()}
    resolved = resolve_acceptance_ref(value, checklist)
    if legacy_index and resolved.upper().startswith("AC-"):
        return resolved.split("-", 1)[1]
    return resolved


def resolve_evidence_ref(value: Any, projection: list[dict[str, Any]]) -> dict[str, Any]:
    ref = dict(value or {}) if isinstance(value, dict) else {"evidence_id": str(value or "").strip()}
    candidates = [
        str(ref.get("evidence_id") or ref.get("id") or "").strip(),
        str(ref.get("evidence_ref_id") or "").strip(),
        str(ref.get("call_id") or "").strip(),
        str(ref.get("ledger_id") or "").strip(),
    ]
    for item in projection:
        item_candidates = {
            str(item.get("id") or "").strip(),
            str(item.get("evidence_ref_id") or "").strip(),
            str(item.get("call_id") or "").strip(),
            str(item.get("ledger_id") or "").strip(),
        }
        if any(candidate and candidate in item_candidates for candidate in candidates):
            return dict(item)
    return {}


def repair_acceptance_refs(finding: dict[str, Any], checklist: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for key in ("failed_acceptance_criteria", "acceptance_criteria", "covers", "coverage", "acceptance_refs"):
        refs.extend(_flatten_refs(finding.get(key)))
    normalized: list[str] = []
    seen: set[str] = set()
    for ref in refs:
        resolved = resolve_acceptance_ref(ref, checklist)
        if not resolved or resolved in seen:
            continue
        seen.add(resolved)
        normalized.append(resolved)
    return normalized


def compact_checklist(items: list[dict[str, Any]], *, limit: int = 20) -> list[dict[str, Any]]:
    compacted: list[dict[str, Any]] = []
    for item in items[:limit]:
        if not isinstance(item, dict):
            continue
        compacted.append(
            {
                key: value
                for key, value in {
                    "id": item.get("id"),
                    "kind": item.get("kind"),
                    "status": item.get("status"),
                    "text": item.get("source_text") or item.get("action") or item.get("summary"),
                    "parent_item_id": item.get("parent_item_id"),
                }.items()
                if value not in (None, "", [])
            }
        )
    return compacted


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, dict):
        return [str(item).strip() for item in value.values() if str(item or "").strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item or "").strip()]
    text = str(value or "").strip()
    return [text] if text else []


def loose_token(value: Any) -> str:
    words: list[str] = []
    for word in str(value or "").strip().lower().split():
        stripped = word.strip(" \t\r\n.,;:!?()[]{}'\"`")
        if stripped:
            words.append(stripped)
    return " ".join(words)


def _acceptance_id_at(checklist: list[dict[str, Any]], index: int) -> str:
    if index <= 0 or index > len(checklist):
        return ""
    return str(checklist[index - 1].get("id") or "").strip()


def _flatten_refs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, int, float)):
        text = str(value).strip()
        return [text] if text else []
    if isinstance(value, dict):
        refs: list[str] = []
        for nested in value.values():
            refs.extend(_flatten_refs(nested))
        return refs
    if isinstance(value, (list, tuple, set)):
        refs: list[str] = []
        for nested in value:
            refs.extend(_flatten_refs(nested))
        return refs
    text = str(value or "").strip()
    return [text] if text else []


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, (dict, list, tuple, set)):
            continue
        text = str(value or "").strip()
        if text:
            return text
    return ""
