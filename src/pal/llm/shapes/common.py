from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, ToolDefinitionIR, ToolResultIR

from typing import Any, Iterable, Mapping

from pal.llm.ir import (
    ImagePartIR,
    LLMMessageIR,
    MessageRole,
    TextPartIR,
)
from pal.llm.response_blocks import json_object
from pal.shared.json_values import thaw_json


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


def role_value(role: MessageRole) -> str:
    return str(role.value)
