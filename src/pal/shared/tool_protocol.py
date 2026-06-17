from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any
from uuid import uuid4

from pal.shared.result_rendering import render_structured_for_llm


ToolResultRenderer = Callable[[Any, Any], str]


def ensure_tool_call_identity(tool_call: Any) -> Any:
    call_id = str(getattr(tool_call, "call_id", "") or "").strip() or f"call_{uuid4().hex[:12]}"
    return type(tool_call)(name=tool_call.name, args=dict(tool_call.args), call_id=call_id)


def assistant_tool_message(text: str, tool_calls: Sequence[Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": str(text or ""),
        "tool_calls": [
            {
                "id": str(tool_call.call_id or ""),
                "type": "function",
                "function": {
                    "name": tool_call.name,
                    "arguments": json.dumps(tool_call.args, ensure_ascii=False, sort_keys=True),
                },
            }
            for tool_call in tool_calls
        ],
    }


def default_tool_result_text(
    result: Any,
    *,
    fallback_ok: str = "ok",
    fallback_error: str = "error",
) -> str:
    llm_text = str(getattr(result, "llm_text", "") or "").strip()
    if llm_text:
        return llm_text
    text = str(getattr(result, "text", "") or "").strip()
    if text:
        return text
    structured = getattr(result, "structured", None)
    if structured:
        return render_structured_for_llm(structured)
    return fallback_ok if bool(getattr(result, "ok", False)) else fallback_error


def append_tool_protocol_messages(
    protocol_messages: list[dict[str, Any]],
    *,
    assistant_text: str,
    tool_calls: Sequence[Any],
    tool_results: Sequence[Any],
    render_tool_result_content: ToolResultRenderer | None = None,
) -> None:
    calls = list(tool_calls)
    if not calls:
        return
    renderer = render_tool_result_content or (lambda _tool_call, result: default_tool_result_text(result))
    protocol_messages.append(assistant_tool_message(assistant_text, calls))
    result_by_call_id = {
        str(result.call_id or ""): result
        for result in tool_results
        if str(result.call_id or "").strip()
    }
    results = list(tool_results)
    for index, tool_call in enumerate(calls):
        call_id = str(tool_call.call_id or "").strip()
        result = result_by_call_id.get(call_id) if call_id else None
        if result is None and index < len(results):
            result = results[index]
        if result is None:
            continue
        protocol_messages.append(
            {
                "role": "tool",
                "tool_call_id": call_id,
                "content": renderer(tool_call, result),
            }
        )
