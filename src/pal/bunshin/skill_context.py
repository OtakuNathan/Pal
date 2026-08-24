from __future__ import annotations

from typing import Any, Mapping

from pal.shared.prompt_rendering import render_xml_block


def skill_user_context(item: Mapping[str, Any]) -> str:
    """Return canonical startup skill user context, accepting old checkpoints."""

    current = str(item.get("user_context") or "").strip()
    if current.startswith("<skill>") and current.endswith("</skill>"):
        return current
    legacy = str(item.get("system_reminder") or "").strip()
    prefix = "<system-reminder>"
    suffix = "</system-reminder>"
    if legacy.startswith(prefix) and legacy.endswith(suffix):
        body = legacy[len(prefix) : -len(suffix)].strip()
        return render_xml_block("skill", body) if body else ""
    return ""


def normalized_skill_injection(item: Mapping[str, Any]) -> dict[str, str] | None:
    skill_id = str(item.get("skill_id") or "").strip()
    user_context = skill_user_context(item)
    if not skill_id or not user_context:
        return None
    return {"skill_id": skill_id, "user_context": user_context}
