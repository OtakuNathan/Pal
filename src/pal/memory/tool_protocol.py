from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from pal.memory.contracts import L1MessageKind, L1TranscriptMessage


def l1_tool_protocol_validation_error(
    transcript: Sequence[L1TranscriptMessage | Mapping[str, Any]],
) -> str:
    """Return an error when a transcript contains an unpaired tool message."""

    pending: set[str] = set()
    assistant_index: int | None = None
    for index, message in enumerate(transcript):
        if isinstance(message, Mapping):
            role = str(message.get("role") or "").strip()
            tool_calls = message.get("tool_calls")
            tool_call_id = str(message.get("tool_call_id") or "").strip()
        elif isinstance(message, L1TranscriptMessage):
            role = str(message.role or "").strip()
            tool_calls = message.tool_calls
            tool_call_id = str(message.tool_call_id or "").strip()
        else:
            return f"tool protocol message at {index} is not an object"
        if pending:
            if role != "tool":
                return (
                    f"assistant tool call at {assistant_index} is missing "
                    "tool results"
                )
            if not tool_call_id or tool_call_id not in pending:
                return f"orphan tool result at {index}"
            pending.remove(tool_call_id)
            continue
        if role == "tool":
            return f"orphan tool result at {index}"
        if role == "assistant" and tool_calls:
            call_ids = {
                str(item.get("id") or "").strip()
                for item in list(tool_calls or ())
                if isinstance(item, dict)
                and str(item.get("id") or "").strip()
            }
            if not call_ids:
                return f"assistant tool call at {index} has no call ids"
            pending = call_ids
            assistant_index = index
    if pending:
        return (
            f"assistant tool call at {assistant_index} is missing tool results"
        )
    return ""


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
        if role == "user":
            transcript.append(
                L1TranscriptMessage(
                    role="user",
                    content=content,
                    kind=L1MessageKind.USER_REQUEST,
                )
            )
            continue
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
        if role == "assistant" and content.strip():
            protocol_assistant_contents.append(content.strip())
            transcript.append(
                L1TranscriptMessage(
                    role="assistant",
                    content=content,
                    kind=L1MessageKind.ASSISTANT_REPLY,
                )
            )
            continue
        if role == "tool":
            result_state = message.get("_pal_result_state")
            transcript.append(
                L1TranscriptMessage(
                    role="tool",
                    content=content,
                    kind=L1MessageKind.TOOL_RESULT,
                    tool_call_id=str(message.get("tool_call_id") or ""),
                    payload=(
                        {"_pal_result_state": dict(result_state)}
                        if isinstance(result_state, dict)
                        else {}
                    ),
                )
            )
    return transcript, protocol_assistant_contents
