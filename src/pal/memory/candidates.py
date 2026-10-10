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


def _normalize_star(value: dict[str, Any], *, label: str) -> tuple[dict[str, str], str]:
    star: dict[str, str] = {}
    missing: list[str] = []
    for field in STAR_MEMORY_FIELDS:
        text = str(value.get(field) or "")
        if not text.strip():
            missing.append(field)
        star[field] = text
    if missing:
        return {}, f"{label} missing required field(s): {', '.join(missing)}"
    return star, ""
