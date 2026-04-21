from __future__ import annotations

from pal.memory.contracts import L3RecallResult, L3RecallView, MemoryQuery

_MAX_RECALL_ITEMS = 3
_MAX_RECALL_LINE_CHARS = 240


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
    minimal_metadata = {
        "retrieval_mode": metadata.get("retrieval_mode"),
        "degraded": metadata.get("degraded"),
        "degraded_reason": metadata.get("degraded_reason"),
    }
    minimal_metadata = {key: value for key, value in minimal_metadata.items() if value not in (None, "", False)}
    if minimal_metadata:
        payload["metadata"] = minimal_metadata
    return payload


def render_recall_result_for_llm(
    *,
    provider_id: str,
    query: MemoryQuery,
    result: L3RecallResult,
    view: L3RecallView | str,
) -> str:
    normalized_view = normalize_recall_view(view)
    lines: list[str] = [f"provider: {str(provider_id or '').strip() or 'unknown'}"]
    if query.queries:
        lines.append(f"queries: {_join_preview(query.queries)}")
    if query.topic_scope:
        lines.append(f"topics: {_join_preview(query.topic_scope)}")

    hits = list(result.hits or [])
    lines.append(f"retrieved: {len(hits)} memories")

    item_lines = _render_hit_lines(hits, view=normalized_view)
    if item_lines:
        lines.append("memories:")
        lines.extend(f"- {item}" for item in item_lines)
    else:
        lines.append("No matching memories found.")

    return "L3 recall result:\n" + "\n".join(lines)


def _render_hit_lines(hits: list[dict[str, object]], *, view: L3RecallView) -> list[str]:
    lines: list[str] = []
    for hit in hits[:_MAX_RECALL_ITEMS]:
        title = _clip(str(hit.get("title") or "").strip(), limit=96)
        if view == L3RecallView.ORIGIN:
            body = _clip(
                str(hit.get("search_text") or hit.get("rendered") or hit.get("summary") or "").strip(),
                limit=_MAX_RECALL_LINE_CHARS,
            )
        else:
            body = _clip(
                str(hit.get("rendered") or hit.get("summary") or hit.get("title") or "").strip(),
                limit=_MAX_RECALL_LINE_CHARS,
            )
        if not body and title:
            body = title
        if not body:
            continue
        if title and body != title:
            lines.append(f"{title}: {body}")
        else:
            lines.append(body)
    return lines


def _render_hit_previews(hits: list[dict[str, object]], *, view: L3RecallView) -> list[dict[str, str]]:
    previews: list[dict[str, str]] = []
    for hit in hits[:_MAX_RECALL_ITEMS]:
        if view == L3RecallView.ORIGIN:
            content = _clip(
                str(hit.get("search_text") or hit.get("rendered") or hit.get("summary") or "").strip(),
                limit=_MAX_RECALL_LINE_CHARS,
            )
        else:
            content = _clip(
                str(hit.get("rendered") or hit.get("summary") or hit.get("title") or "").strip(),
                limit=_MAX_RECALL_LINE_CHARS,
            )
        preview = {
            "document_id": str(hit.get("document_id") or "").strip(),
            "kind": str(hit.get("document_kind") or "").strip(),
            "title": _clip(str(hit.get("title") or "").strip(), limit=96),
            "content": content,
        }
        previews.append({key: value for key, value in preview.items() if value})
    return previews


def _join_preview(items: list[str]) -> str:
    return ", ".join(_clip(str(item).strip(), limit=96) for item in items if str(item).strip())


def _clip(text: str, *, limit: int) -> str:
    normalized = str(text or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(limit - 3, 0)].rstrip() + "..."
