from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, ToolDefinitionIR, ToolResultIR

import json
from typing import Any, Iterable, Mapping

from pal.llm.ir import (
    ImagePartIR,
    LLMMessageIR,
    MessageRole,
    TextPartIR,
)
from pal.llm.shapes.base import ShapeDecodeError
from pal.shared.json_values import thaw_json


def text_content(parts: Iterable[Any]) -> str:
    return "".join(part.text for part in parts if isinstance(part, TextPartIR))


def openai_content(parts: Iterable[Any]) -> str | list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPartIR):
            if part.text:
                rendered.append({"type": "text", "text": part.text})
            continue
        if isinstance(part, ImagePartIR):
            rendered.append(
                {
                    "type": "image_url",
                    "image_url": {"url": part.source},
                }
            )
    if not rendered:
        return ""
    if len(rendered) == 1 and rendered[0]["type"] == "text":
        return str(rendered[0]["text"])
    return rendered


def openai_tool_definition(tool: ToolDefinitionIR) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": thaw_json(tool.input_schema),
        },
    }


def responses_tool_definition(tool: ToolDefinitionIR) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": thaw_json(tool.input_schema),
    }


def anthropic_tool_definition(tool: ToolDefinitionIR) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "input_schema": thaw_json(tool.input_schema),
    }


def tool_calls(message: LLMMessageIR) -> tuple[ToolCallIR, ...]:
    return tuple(part for part in message.parts if isinstance(part, ToolCallIR))


def tool_results(message: LLMMessageIR) -> tuple[ToolResultIR, ...]:
    return tuple(part for part in message.parts if isinstance(part, ToolResultIR))


def json_object(value: Any, *, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ShapeDecodeError(f"{label} contained invalid JSON") from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise ShapeDecodeError(f"{label} must be a JSON object")


def role_value(role: MessageRole) -> str:
    return str(role.value)
