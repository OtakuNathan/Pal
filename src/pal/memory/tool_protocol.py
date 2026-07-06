from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pal.memory.contracts import L1MessageKind, L1TranscriptMessage


def l1_tool_protocol_transcript(
    messages: list[dict[str, Any]],
    *,
    truncate_tool_result: Callable[[str], str] | None = None,
) -> tuple[list[L1TranscriptMessage], list[str]]:
    transcript: list[L1TranscriptMessage] = []
    protocol_assistant_contents: list[str] = []
    for message in list(messages or []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "").strip()
        content = str(message.get("content") or "")
        if role == "tool" and truncate_tool_result is not None:
            content = truncate_tool_result(content)
        tool_calls = message.get("tool_calls")
        if role == "assistant" and tool_calls:
            protocol_assistant_contents.append(content.strip())
            transcript.append(
                L1TranscriptMessage(
                    role="assistant",
                    content=content,
                    kind=L1MessageKind.ASSISTANT_TOOL_CALL,
                    tool_calls=[dict(item) for item in list(tool_calls or []) if isinstance(item, dict)],
                )
            )
            continue
        if role == "tool":
            transcript.append(
                L1TranscriptMessage(
                    role="tool",
                    content=content,
                    kind=L1MessageKind.TOOL_RESULT,
                    tool_call_id=str(message.get("tool_call_id") or ""),
                )
            )
    return transcript, protocol_assistant_contents
