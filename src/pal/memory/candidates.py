from __future__ import annotations

from typing import Any


def l3_commit_args_from_memory_candidate(
    candidate: dict[str, Any],
    *,
    default_scope: str = "system",
    fallback_task_id: str = "",
    source_kind: str = "",
    source_ref: str = "",
) -> dict[str, Any]:
    kind = str(candidate.get("kind") or candidate.get("document_kind") or "case").strip() or "case"
    scope = str(candidate.get("scope") or default_scope or "system").strip() or "system"
    title = " ".join(str(candidate.get("title") or "").split())
    summary = " ".join(
        str(
            candidate.get("summary")
            or candidate.get("search_text")
            or candidate.get("source_excerpt")
            or title
        ).split()
    )
    search_text = str(candidate.get("search_text") or candidate.get("source_excerpt") or summary or title).strip()
    if not title:
        title = _preview_text(summary or search_text, limit=72)
    if not summary:
        summary = search_text or title
    if not search_text:
        search_text = summary or title
    if not kind or not title or not summary or not search_text:
        return {}
    topics_payload = candidate.get("topics")
    if isinstance(topics_payload, str):
        topics = [topics_payload]
    elif isinstance(topics_payload, (list, tuple)):
        topics = list(topics_payload)
    else:
        topics = []
    payload_source = candidate.get("payload")
    payload = dict(payload_source) if isinstance(payload_source, dict) else {}
    if source_kind:
        payload.setdefault("source_kind", str(source_kind))
    if source_ref:
        payload.setdefault("source_ref", str(source_ref))
    if source_kind or source_ref:
        payload.setdefault("memory_candidate_source", "approval")
    args: dict[str, Any] = {
        "kind": kind,
        "scope": scope,
        "title": title,
        "summary": summary,
        "search_text": search_text,
        "topics": _dedupe_nonempty([str(value) for value in topics]),
        "payload": payload,
    }
    task_id = str(candidate.get("task_id") or "").strip()
    if task_id:
        args["task_id"] = task_id
    elif scope == "task" and fallback_task_id:
        args["task_id"] = fallback_task_id
    for key in ("canonical_key", "situation_text", "task_text", "action_text", "result_text"):
        value = str(candidate.get(key) or "").strip()
        if value:
            args[key] = value
    return args


def _dedupe_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = " ".join(str(value or "").split())
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _preview_text(text: str, *, limit: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return f"{value[: max(0, limit - 3)].rstrip()}..."
