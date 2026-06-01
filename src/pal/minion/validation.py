from __future__ import annotations

from typing import Any


class MinionWorkOrderValidationError(ValueError):
    def __init__(self, message: str, *, field: str = "milestones") -> None:
        super().__init__(message)
        self.payload = {"field": field, "reason": "invalid_work_order"}


def normalize_milestones(raw: Any, *, field: str = "milestones") -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise MinionWorkOrderValidationError(f"{field} is required and must be a non-empty array", field=field)
    milestones: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if isinstance(item, dict):
            title = str(item.get("title") or item.get("name") or item.get("task") or "").strip()
            if not title:
                raise MinionWorkOrderValidationError(f"{field}[{index}].title is required", field=field)
            normalized = {
                "title": _clip_text(title),
                "summary": _clip_text(item.get("summary") or item.get("description") or ""),
                "acceptance": _string_list(item.get("acceptance") or item.get("acceptance_criteria")),
            }
            for key in ("milestone_id", "skill_refs", "capability_refs"):
                if key in item:
                    normalized[key] = item[key]
            milestones.append(normalized)
            continue
        title = str(item or "").strip()
        if title:
            milestones.append({"title": _clip_text(title), "summary": "", "acceptance": []})
    if not milestones:
        raise MinionWorkOrderValidationError(f"{field} is required and must contain at least one milestone", field=field)
    return milestones


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _clip_text(value: Any, limit: int = 4000) -> str:
    text = str(value or "").strip()
    if limit > 0 and len(text) > limit:
        return text[:limit].rstrip()
    return text
