from __future__ import annotations

from pal.memory.contracts import L1MessageKind, L1TranscriptMessage


def normalize_l1_tool_protocol(messages: list[L1TranscriptMessage]) -> list[L1TranscriptMessage]:
    complete_assistant_indices, paired_tool_indices = complete_tool_protocol_indices(messages)
    repaired: list[L1TranscriptMessage] = []
    for index, message in enumerate(messages):
        role = str(message.role or "").strip()
        if role == "assistant" and message.tool_calls:
            if index in complete_assistant_indices:
                repaired.append(message)
                continue
            repaired.append(
                L1TranscriptMessage(
                    role="user",
                    content=render_incomplete_tool_call_context(message.content, tool_calls=message.tool_calls),
                    kind=L1MessageKind.RUNTIME_CONTEXT_MEMORY,
                )
            )
            continue
        if role == "tool":
            if index in paired_tool_indices:
                repaired.append(message)
                continue
            repaired.append(
                L1TranscriptMessage(
                    role="user",
                    content=render_orphan_tool_result_context(message.content, tool_call_id=message.tool_call_id),
                    kind=L1MessageKind.RUNTIME_CONTEXT_MEMORY,
                )
            )
            continue
        repaired.append(message)
    return repaired


def complete_tool_protocol_indices(messages: list) -> tuple[set[int], set[int]]:
    complete_assistants: set[int] = set()
    paired_tools: set[int] = set()
    index = 0
    while index < len(messages):
        message = messages[index]
        role = str(message.role or "").strip()
        if role != "assistant":
            index += 1
            continue
        expected_ids = assistant_tool_call_ids(getattr(message, "tool_calls", None))
        if not expected_ids:
            index += 1
            continue
        seen: set[str] = set()
        group_tool_indices: list[int] = []
        cursor = index + 1
        while cursor < len(messages) and str(messages[cursor].role or "").strip() == "tool":
            tool_call_id = str(getattr(messages[cursor], "tool_call_id", "") or "").strip()
            if tool_call_id in expected_ids and tool_call_id not in seen:
                group_tool_indices.append(cursor)
                seen.add(tool_call_id)
            cursor += 1
        if seen == expected_ids:
            complete_assistants.add(index)
            paired_tools.update(group_tool_indices)
        index = cursor
    return complete_assistants, paired_tools


def assistant_tool_call_ids(tool_calls: object) -> set[str]:
    ids: set[str] = set()
    for item in list(tool_calls or []):
        if not isinstance(item, dict):
            continue
        call_id = str(item.get("id") or "").strip()
        if call_id:
            ids.add(call_id)
    return ids


def render_orphan_tool_result_context(content: str, *, tool_call_id: object = None) -> str:
    rendered = str(content or "").strip()
    call_id = str(tool_call_id or "").strip()
    lines = [
        '<runtime_context_update source="historical_tool_result">',
        "A historical tool result was retained without its assistant tool-call header, so it is shown as context instead of an active tool protocol message.",
    ]
    if call_id:
        lines.append(f"tool_call_id: {call_id}")
    if rendered:
        lines.append(rendered)
    lines.append("</runtime_context_update>")
    return "\n".join(lines)


def render_incomplete_tool_call_context(content: str, *, tool_calls: object = None) -> str:
    rendered = str(content or "").strip()
    names: list[str] = []
    for item in list(tool_calls or []):
        if not isinstance(item, dict):
            continue
        function = item.get("function") or {}
        if not isinstance(function, dict):
            continue
        name = str(function.get("name") or "").strip()
        if name:
            names.append(name)
    lines = [
        '<runtime_context_update source="historical_tool_call">',
        "A historical assistant tool-call header was retained without all matching tool results, so it is shown as context instead of an active tool protocol message.",
    ]
    if names:
        lines.append("tool_calls: " + ", ".join(names))
    if rendered:
        lines.append(rendered)
    lines.append("</runtime_context_update>")
    return "\n".join(lines)
