from __future__ import annotations

from pal.service.contracts import ServiceDefinition


def build_service_trigger_input(definition: ServiceDefinition) -> str:
    lines = ["[Service Trigger]", f"Goal: {definition.goal.strip()}"]
    method = definition.method.strip()
    if method:
        lines.append(f"Method: {method}")
    if definition.skill_refs:
        lines.append(f"Skills: {', '.join(str(item).strip() for item in definition.skill_refs if str(item).strip())}")
    if definition.out_channel_id:
        lines.append(f"Output Channel: {definition.out_channel_id}")
    return "\n".join(lines)
