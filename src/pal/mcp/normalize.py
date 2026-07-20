from __future__ import annotations

import copy
import re
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.mcp.model import McpPromptArgumentSpec, McpPromptSpec, McpRejectedItem, McpToolSpec
from pal.shared import RuntimeStatus
from pal.shared.result_rendering import render_titled_structured_for_llm


_NAME_RE = re.compile(r"[^a-zA-Z0-9]+")


def sanitize_name(value: str, *, fallback: str = "item") -> str:
    cleaned = _NAME_RE.sub("_", str(value or "").strip()).strip("_").lower()
    return cleaned or fallback


def normalize_tool_payload(payload: dict[str, Any]) -> McpToolSpec:
    return McpToolSpec(
        name=str(payload.get("name") or "").strip(),
        description=str(payload.get("description") or "").strip(),
        input_schema=payload.get("inputSchema"),
        output_schema=payload.get("outputSchema"),
        annotations=dict(payload.get("annotations") or {}),
        raw=dict(payload),
    )


def normalize_prompt_payload(payload: dict[str, Any]) -> McpPromptSpec:
    arguments = []
    for item in list(payload.get("arguments") or []):
        if not isinstance(item, dict):
            continue
        arguments.append(
            McpPromptArgumentSpec(
                name=str(item.get("name") or "").strip(),
                description=str(item.get("description") or "").strip(),
                required=bool(item.get("required", False)),
                raw=dict(item),
            )
        )
    return McpPromptSpec(
        name=str(payload.get("name") or "").strip(),
        description=str(payload.get("description") or "").strip(),
        arguments=tuple(arguments),
        raw=dict(payload),
    )


def schema_normalize_or_reject(
    schema: dict[str, Any] | None,
    *,
    external_name: str,
    allow_missing: bool = True,
) -> tuple[dict[str, Any] | None, McpRejectedItem | None, tuple[str, ...]]:
    if schema is None:
        if not allow_missing:
            return None, McpRejectedItem(kind="tool", external_name=external_name, reason="missing_input_schema"), ()
        return {"type": "object", "properties": {}, "additionalProperties": False}, None, ("missing_input_schema",)
    if not isinstance(schema, dict):
        return None, McpRejectedItem(kind="tool", external_name=external_name, reason="invalid_input_schema", raw={}), ()

    normalized = copy.deepcopy(schema)
    schema_type = normalized.get("type")
    if schema_type is None:
        normalized["type"] = "object"
    elif schema_type != "object":
        return None, McpRejectedItem(kind="tool", external_name=external_name, reason="non_object_input_schema", raw_schema=schema), ()

    properties = normalized.setdefault("properties", {})
    if not isinstance(properties, dict):
        return None, McpRejectedItem(kind="tool", external_name=external_name, reason="invalid_properties_schema", raw_schema=schema), ()
    required = normalized.get("required")
    if required is not None and not isinstance(required, list):
        return None, McpRejectedItem(kind="tool", external_name=external_name, reason="invalid_required_schema", raw_schema=schema), ()
    if required is None:
        normalized["required"] = []
    return normalized, None, ()


def prompt_arguments_schema(prompt: McpPromptSpec) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for argument in prompt.arguments:
        name = str(argument.name or "").strip()
        if not name:
            continue
        properties[name] = {
            "type": "string",
            "description": argument.description or f"Argument `{name}` for MCP prompt `{prompt.name}`.",
        }
        if argument.required:
            required.append(name)
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def normalize_tool_result(result: dict[str, Any], *, server_id: str, tool_name: str) -> CapabilityResult:
    raw = dict(result or {})
    text = _content_text(raw.get("content"))
    if not text:
        text = str(raw.get("structuredContent") or raw.get("content") or "").strip()
    if not text:
        text = f"MCP tool `{tool_name}` returned no text content."
    is_error = bool(raw.get("isError"))
    structured = {
        "mcp": {"server_id": server_id, "tool_name": tool_name},
        "tool_text": text,
        "raw_result": raw,
        "error_kind": "tool_execution" if is_error else None,
    }
    status = RuntimeStatus.ERROR if is_error else RuntimeStatus.OK
    title = "MCP tool execution failed" if is_error else "MCP tool result"
    structured_text = render_titled_structured_for_llm(title, structured)
    llm_text = f"{title}:\n{text}\n\n{structured_text}" if is_error else structured_text
    return CapabilityResult(
        status=status,
        text=text,
        structured=structured,
        llm_text=llm_text,
    )


def normalize_protocol_error(exc: Exception, *, server_id: str, name: str, kind: str) -> CapabilityResult:
    error_text = str(exc).strip() or exc.__class__.__name__
    structured = {
        "mcp": {"server_id": server_id, "name": name, "kind": kind},
        "error_kind": "protocol",
        "error": error_text,
        "error_type": exc.__class__.__name__,
    }
    return CapabilityResult(
        status=RuntimeStatus.ERROR,
        text=f"MCP {kind} protocol error: {error_text}",
        structured=structured,
        llm_text=f"MCP protocol error:\n{error_text}\n\n{render_titled_structured_for_llm('MCP protocol error detail', structured)}",
    )


def normalize_prompt_result(result: dict[str, Any], *, server_id: str, prompt_name: str) -> CapabilityResult:
    raw = dict(result or {})
    messages = list(raw.get("messages") or [])
    unsupported = _unsupported_prompt_content_types(messages)
    structured = {
        "messages": messages,
        "description": str(raw.get("description") or ""),
        "unsupported_content_types": unsupported,
        "mcp": {"server_id": server_id, "prompt_name": prompt_name},
        "raw_result": raw,
    }
    return CapabilityResult(
        status=RuntimeStatus.OK,
        text=f"Rendered MCP prompt: {prompt_name}",
        structured=structured,
        llm_text=render_titled_structured_for_llm("Rendered MCP prompt", structured),
    )


def _content_text(content: Any) -> str:
    chunks: list[str] = []
    for item in list(content or []):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and item.get("text"):
            chunks.append(str(item.get("text")))
    return "\n".join(chunks).strip()


def _unsupported_prompt_content_types(messages: list[Any]) -> list[str]:
    unsupported: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        items = content if isinstance(content, list) else [content]
        for item in items:
            if not isinstance(item, dict):
                continue
            content_type = str(item.get("type") or "").strip()
            if content_type and content_type != "text":
                unsupported.add(content_type)
    return sorted(unsupported)
