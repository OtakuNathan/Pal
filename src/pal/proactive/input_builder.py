from __future__ import annotations

from pal.proactive.contracts import ProactiveDefinition


def build_proactive_trigger_input(definition: ProactiveDefinition) -> str:
    lines = [
        "<proactive_trigger>",
        (
            "Turn: This is a Pal-authored self-initiated proactive turn. "
            "Treat this trigger as the current request and execute it now."
        ),
        f"Goal: {definition.goal.strip()}",
    ]
    method = definition.method.strip()
    if method:
        lines.append(f"Method: {method}")
    if definition.skill_refs:
        lines.append(f"Skills: {', '.join(str(item).strip() for item in definition.skill_refs if str(item).strip())}")
    if definition.out_channel_id:
        lines.append(f"Output Channel: {definition.out_channel_id}")
    lines.append(
        "Instruction: Perform the described action. Do not create, configure, or describe "
        "proactive tasks."
    )
    lines.append(
        "Delivery: Your response text will be automatically delivered to the configured output "
        "channel. Do not attempt to discover or call channel/capability to send messages — just "
        "produce the response content directly."
    )
    lines.append("</proactive_trigger>")
    return "\n".join(lines)
