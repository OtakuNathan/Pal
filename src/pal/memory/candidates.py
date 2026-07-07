from __future__ import annotations

from typing import Any

STAR_MEMORY_FIELDS = ("situation", "task", "action", "result")
STAR_TEXT_FIELD_KEYS = {
    "situation": "situation_text",
    "task": "task_text",
    "action": "action_text",
    "result": "result_text",
}


def memory_star_from_args(args: dict[str, Any], *, allow_legacy: bool = True) -> tuple[dict[str, str], str]:
    raw_star = args.get("star")
    if raw_star is not None:
        if not isinstance(raw_star, dict):
            return {}, "star must be an object with situation, task, action, and result"
        return _normalize_star(raw_star, label="star")

    if not allow_legacy:
        return {}, ""

    payload = args.get("payload")
    if isinstance(payload, dict):
        payload_star = {
            field: str(payload.get(field) or payload.get(text_key) or "").strip()
            for field, text_key in STAR_TEXT_FIELD_KEYS.items()
        }
        if any(payload_star.values()):
            return _normalize_star(payload_star, label="payload STAR fields")

    legacy = {
        field: str(args.get(text_key) or "").strip()
        for field, text_key in STAR_TEXT_FIELD_KEYS.items()
    }
    if not any(legacy.values()):
        return {}, ""
    return _normalize_star(legacy, label="legacy STAR fields")


def star_text_fields(star: dict[str, str]) -> dict[str, str]:
    return {
        text_key: str(star.get(field) or "").strip()
        for field, text_key in STAR_TEXT_FIELD_KEYS.items()
    }


def l3_commit_args_from_memory_candidate(
    candidate: dict[str, Any],
    *,
    default_scope: str = "system",
    fallback_task_id: str = "",
    source_kind: str = "",
    source_ref: str = "",
) -> dict[str, Any]:
    kind = str(candidate.get("kind") or candidate.get("document_kind") or "case").strip() or "case"
    star, star_error = memory_star_from_args(candidate)
    if star_error:
        return {}
    if kind == "case" and not star:
        return {}
    if kind != "case" and star:
        return {}
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
    if star:
        payload.update(star)
    args: dict[str, Any] = {
        "kind": kind,
        "scope": scope,
        "title": title,
        "summary": summary,
        "search_text": search_text,
        "topics": _dedupe_nonempty([str(value) for value in topics]),
        "payload": payload,
    }
    if star:
        args["star"] = dict(star)
        args.update(star_text_fields(star))
    task_id = str(candidate.get("task_id") or "").strip()
    if task_id:
        args["task_id"] = task_id
    elif scope == "task" and fallback_task_id:
        args["task_id"] = fallback_task_id
    for key in ("canonical_key",):
        value = str(candidate.get(key) or "").strip()
        if value:
            args[key] = value
    return args


def _normalize_star(value: dict[str, Any], *, label: str) -> tuple[dict[str, str], str]:
    star: dict[str, str] = {}
    missing: list[str] = []
    for field in STAR_MEMORY_FIELDS:
        text = " ".join(str(value.get(field) or "").split())
        if not text:
            missing.append(field)
        star[field] = text
    if missing:
        return {}, f"{label} missing required field(s): {', '.join(missing)}"
    return star, ""


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
