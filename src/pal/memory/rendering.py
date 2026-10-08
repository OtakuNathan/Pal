from __future__ import annotations

import json

from pal.foundation.diagnostics import diagnostic_text
from pal.memory.contracts import L3MutationResult, L3RecallResult, L3RecallView, MemoryQuery

def normalize_recall_view(raw: object) -> L3RecallView:
    if isinstance(raw, L3RecallView):
        return raw
    value = str(raw or "").strip().lower()
    return L3RecallView.ORIGIN if value == L3RecallView.ORIGIN.value else L3RecallView.SUMMARY


def build_recall_structured_payload(
    *,
    provider_id: str,
    query: MemoryQuery,
    result: L3RecallResult,
    view: L3RecallView | str,
) -> dict[str, object]:
    normalized_view = normalize_recall_view(view)
    metadata = dict(result.metadata or {})
    payload: dict[str, object] = {
        "provider_id": str(provider_id or "").strip() or "unknown",
        "view": normalized_view.value,
        "queries": list(query.queries),
        "topic_scope": list(query.topic_scope),
        "hit_count": len(list(result.hits or [])),
        "hits_preview": _render_hit_previews(list(result.hits or []), view=normalized_view),
    }
    if metadata:
        payload["metadata"] = metadata
    return payload


def render_recall_result_for_llm(
    *,
    provider_id: str,
    query: MemoryQuery,
    result: L3RecallResult,
    view: L3RecallView | str,
) -> str:
    _ = (provider_id, query)
    normalized_view = normalize_recall_view(view)
    lines: list[str] = [
        f'<recalled_memories view="{normalized_view.value}">',
        "When updating or deleting memory, copy the complete mem_ref exactly, including prefixes such as fact: or case:.",
    ]

    metadata = result.metadata or {}
    if metadata.get("degraded") or metadata.get("degraded_reason"):
        lines.append("Recall degraded: " + str(metadata.get("degraded_reason") or "retrieval is incomplete"))
    if query.mem_ref:
        lines.append(f"mem_ref: {query.mem_ref}; lookup_status: {metadata.get('lookup_status', 'unavailable')}")
        if metadata.get("successors"):
            lines.append("successors: " + ", ".join(metadata["successors"]))
    item_lines = _render_hit_lines(list(result.hits or []), view=normalized_view)
    if item_lines:
        lines.extend(item_lines)
    else:
        if not query.mem_ref:
            lines.append("No matching memories returned.")
    if metadata:
        lines.append("Provider metadata: " + _metadata_text(metadata))
    lines.append("</recalled_memories>")
    return "\n".join(lines)


def build_mutation_structured_payload(result: L3MutationResult) -> dict[str, object]:
    payload = dict(result.hit or {})
    mem_ref = str(result.document_id or payload.get("mem_ref") or payload.get("document_id") or "").strip()
    payload.pop("document_id", None)
    if mem_ref:
        payload["mem_ref"] = mem_ref
    if result.metadata:
        payload["metadata"] = dict(result.metadata)
    if not payload:
        payload = {"mem_ref": mem_ref} if mem_ref else {}
    return payload


def render_mutation_result_for_llm(action: str, result: L3MutationResult) -> str:
    mem_ref = str(result.document_id or "").strip()
    lines = [f"Memory {action} result:", f"status: {result.status}"]
    if mem_ref:
        lines.append(f"mem_ref: {mem_ref}")
    metadata = result.metadata or {}
    if metadata.get("reason"):
        lines.append(f"reason: {metadata['reason']}")
    if metadata.get("successors"):
        lines.append("successors: " + ", ".join(metadata["successors"]))
    if metadata:
        lines.append("Provider metadata: " + _metadata_text(metadata))
    return "\n".join(lines)


def _metadata_text(metadata: dict) -> str:
    return diagnostic_text(json.dumps(metadata, ensure_ascii=False, default=str), limit=None)


def _render_hit_lines(hits: list[dict[str, object]], *, view: L3RecallView) -> list[str]:
    lines: list[str] = []
    for hit in hits:
        mem_ref = str(hit.get("mem_ref") or hit.get("document_id") or "").strip()
        body = _content_for_view(hit, view=view)
        if not mem_ref or not body:
            continue
        lines.append(f"[{mem_ref}]: {body}")
    return lines


def _render_hit_previews(hits: list[dict[str, object]], *, view: L3RecallView) -> list[dict[str, str]]:
    previews: list[dict[str, str]] = []
    content_key = "search_text" if view == L3RecallView.ORIGIN else "summary"
    for hit in hits:
        mem_ref = str(hit.get("mem_ref") or hit.get("document_id") or "").strip()
        content = _content_for_view(hit, view=view)
        preview = {
            "mem_ref": mem_ref,
            content_key: content,
        }
        previews.append({key: value for key, value in preview.items() if value})
    return previews


def _content_for_view(hit: dict[str, object], *, view: L3RecallView) -> str:
    if view == L3RecallView.ORIGIN:
        raw = hit.get("search_text") or hit.get("rendered") or hit.get("summary") or ""
    else:
        raw = hit.get("summary") or hit.get("rendered") or hit.get("search_text") or ""
    # Keep the selected record intact; the shared output-snapshot delivery owns
    # delivery limits and preserves large output in an immutable file.
    return str(raw or "")
