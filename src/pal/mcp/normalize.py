from __future__ import annotations

import re
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EffectOutcome, EffectReceipt
from pal.mcp.ipc import McpManagerRpcError
from pal.mcp.model import McpPromptArgumentSpec, McpPromptSpec, McpProtocolError, McpRemoteError, McpToolSpec
from pal.shared import RuntimeStatus
from pal.shared.diagnostics import diagnostic_text, exception_report
from pal.shared.result_rendering import render_titled_structured_for_llm


_NAME_RE = re.compile(r"[^a-zA-Z0-9]+")


def sanitize_name(value: str, *, fallback: str = "item") -> str:
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(value or "").strip())
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    cleaned = _NAME_RE.sub("_", value).strip("_").lower()
    return cleaned or fallback


def normalize_tool_payload(payload: dict[str, Any]) -> McpToolSpec:
    from pal.mcp.protocol import validate_message
    validate_message(payload, "Tool")
    return McpToolSpec(
        name=payload["name"],
        description=str(payload.get("description") or "").strip(),
        input_schema=payload.get("inputSchema"),
        output_schema=payload.get("outputSchema"),
        annotations=dict(payload.get("annotations") or {}),
        raw=dict(payload),
    )


def normalize_prompt_payload(payload: dict[str, Any]) -> McpPromptSpec:
    from pal.mcp.protocol import validate_message
    validate_message(payload, "Prompt")
    arguments = []
    for item in list(payload.get("arguments") or []):
        if not isinstance(item, dict):
            continue
        arguments.append(
            McpPromptArgumentSpec(
                name=item["name"],
                description=str(item.get("description") or "").strip(),
                required=bool(item.get("required", False)),
                raw=dict(item),
            )
        )
    return McpPromptSpec(
        name=payload["name"],
        description=str(payload.get("description") or "").strip(),
        arguments=tuple(arguments),
        raw=dict(payload),
    )


def prompt_arguments_schema(prompt: McpPromptSpec) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    for argument in prompt.arguments:
        name = argument.name
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
    from pal.mcp.protocol import validate_tool_result
    try:
        validate_tool_result(result)
    except McpProtocolError as exc:
        return normalize_protocol_error(exc, server_id=server_id, name=tool_name, kind="tool")
    raw = dict(result)
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
    if is_error:
        structured["error_code"] = "mcp_tool_error"
        structured["next_step"] = (
            "For argument errors, use read_tool with the called Pal alias to inspect its exact schema. "
            f"For server failures, inspect read_mcp_server(name={server_id!r}) and inspect_mcp_state. "
            "Reconcile external writes before retrying; an error does not prove no side effect occurred."
        )
    status = RuntimeStatus.ERROR if is_error else RuntimeStatus.OK
    title = "MCP tool execution failed" if is_error else "MCP tool result"
    # Preserve the protocol payload internally for output-schema validation.
    # The model sees it once, without the additional extracted tool_text copy.
    visible = {key: value for key, value in structured.items() if key != "tool_text"}
    llm_text = render_titled_structured_for_llm(title, visible)
    return CapabilityResult(
        status=status,
        text=text,
        structured=structured,
        llm_text=llm_text,
    )


def normalize_protocol_error(exc: Exception, *, server_id: str, name: str, kind: str) -> CapabilityResult:
    error_text = exception_report(exc)
    remote_error = isinstance(exc, McpRemoteError) or (
        isinstance(exc, McpManagerRpcError) and exc.kind == "remote")
    structured = {
        "mcp": {"server_id": server_id, "name": name, "kind": kind},
        "error_kind": "remote" if remote_error else "protocol",
        "error_code": "mcp_remote_error" if remote_error else "mcp_protocol_error",
        "error": error_text,
        "error_type": exc.__class__.__name__,
        "next_step": f"Use inspect_mcp_state and read_mcp_server(name={server_id!r}) for transport/server state. Correct the reported cause; reconcile external writes before retrying. A quarantined server requires explicit attach after correction; rescan does not retry it.",
    }
    if getattr(exc, "payload", None):
        structured["protocol_details"] = dict(exc.payload)
    return CapabilityResult(
        status=RuntimeStatus.ERROR,
        text=f"MCP {kind} protocol error: {error_text}",
        structured=structured,
        llm_text=diagnostic_text(render_titled_structured_for_llm("MCP protocol error", structured), limit=None),
    )


def normalize_prompt_result(result: dict[str, Any], *, server_id: str, prompt_name: str) -> CapabilityResult:
    from pal.mcp.protocol import validate_message
    try:
        validate_message(result, "GetPromptResult")
    except McpProtocolError as exc:
        return normalize_protocol_error(exc, server_id=server_id, name=prompt_name, kind="prompt")
    raw = dict(result)
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
        llm_text=render_titled_structured_for_llm("Rendered MCP prompt", {
            key: value for key, value in structured.items() if key not in {"messages", "description"}
        }),
        effect_receipt=EffectReceipt(outcome=EffectOutcome.NONE, receipt={"mcp_prompt_response": True}),
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
