from __future__ import annotations

import json
import logging
import re
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EffectOutcome, EffectReceipt
from pal.mcp.ipc import McpManagerRpcError
from pal.mcp.model import McpPromptArgumentSpec, McpPromptSpec, McpProtocolError, McpRemoteError, McpToolSpec
# Keep validators and the exceptions caught below in the same module generation.
from pal.mcp.protocol import validate_message, validate_tool_result
from pal.shared import RuntimeStatus
from pal.shared.diagnostics import diagnostic_text, exception_reason, diagnostic_reason
from pal.shared.result_rendering import render_titled_structured_for_llm


_NAME_RE = re.compile(r"[^a-zA-Z0-9]+")


def sanitize_name(value: str, *, fallback: str = "item") -> str:
    value = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", str(value or "").strip())
    value = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", value)
    cleaned = _NAME_RE.sub("_", value).strip("_").lower()
    return cleaned or fallback


def normalize_tool_payload(payload: dict[str, Any]) -> McpToolSpec:
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


def _tool_error_reason(value: Any, *, depth: int = 0) -> str:
    """Adapt server error envelopes without serializing their private diagnostics."""
    if depth > 8:
        return ""
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except ValueError:
            # Some servers prefix their serialized error with an operation
            # label. Decode the complete trailing envelope, not its escapes.
            match = re.search(r"[\[{]", value)
            if match and match.start() > 0:
                try:
                    decoded = json.loads(value[match.start():])
                except ValueError:
                    pass
                else:
                    reason = _tool_error_reason(decoded, depth=depth + 1)
                    if reason:
                        return reason
            return diagnostic_reason(value)
        return _tool_error_reason(decoded, depth=depth + 1)
    if isinstance(value, dict):
        # Explicit causes take precedence over wrapper messages. These are
        # server conventions, so their interpretation belongs in the adapter.
        fields = {re.sub(r"[^a-z]", "", str(key).lower()): item for key, item in value.items()}
        for key in ("cause", "rootcause", "innererror", "reason", "rawresponse", "details", "error", "message", "detail", "errors",
                    "traceback", "stack", "stacktrace"):
            reason = _tool_error_reason(fields.get(key), depth=depth + 1)
            if reason:
                return reason
    if isinstance(value, list):
        reasons = [_tool_error_reason(item, depth=depth + 1) for item in value]
        return diagnostic_reason("; ".join(dict.fromkeys(reason for reason in reasons if reason)))
    return ""


def normalize_tool_result(result: dict[str, Any], *, server_id: str, tool_name: str) -> CapabilityResult:
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
    # Strip protocol metadata only at protocol positions. structuredContent and
    # text/resource bodies belong to the server's business data and stay intact.
    visible = {key: value for key, value in structured.items() if key not in {"tool_text", "raw_result"}}
    visible["result"] = _protocol_payload_for_llm(raw)
    llm_text = render_titled_structured_for_llm(title, visible)
    if is_error:
        # Server output remains protocol evidence. The adapter owns its failure
        # presentation; a remote error never proves absence of side effects.
        logging.getLogger(__name__).warning("MCP %s/%s tool error: %s", server_id, tool_name,
            diagnostic_text(json.dumps(raw, ensure_ascii=False), limit=None))
        text = (_tool_error_reason(raw.get("structuredContent"))
                or _tool_error_reason([item["text"] for item in raw.get("content", [])
                                       if item.get("type") == "text" and item.get("text")])
                or "Server reported a tool error")
        llm_text = f"MCP {tool_name}: {text}"
    return CapabilityResult(
        status=status,
        text=text,
        structured=structured,
        llm_text=llm_text,
        llm_failure_details=visible if is_error else None,
        recovery_hint=structured.get("next_step", "") if is_error else "",
        effect_receipt=EffectReceipt(outcome=EffectOutcome.UNKNOWN) if is_error else None,
    )


def normalize_protocol_error(exc: Exception, *, server_id: str, name: str, kind: str) -> CapabilityResult:
    error_text = exception_reason(exc)
    remote_error = isinstance(exc, McpRemoteError) or (
        isinstance(exc, McpManagerRpcError) and exc.kind == "remote")
    if remote_error and isinstance(exc, (McpRemoteError, McpManagerRpcError)):
        # JSON-RPC errors travel directly or inside the manager sidecar's
        # error.details.raw_response envelope. Decode before presenting them.
        error_text = (_tool_error_reason(exc.payload)
                      or _tool_error_reason(str(exc)) or "Server rejected the request")
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
        llm_text=f"MCP {name}: {error_text}",
        recovery_hint=structured["next_step"],
        effect_receipt=EffectReceipt(outcome=EffectOutcome.UNKNOWN),
    )


def normalize_prompt_result(result: dict[str, Any], *, server_id: str, prompt_name: str) -> CapabilityResult:
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
            **{key: value for key, value in structured.items() if key not in {"messages", "description", "raw_result"}},
            "result": _protocol_payload_for_llm(raw),
        }),
        effect_receipt=EffectReceipt(outcome=EffectOutcome.NONE, receipt={"mcp_prompt_response": True}),
    )


def _protocol_payload_for_llm(raw: dict[str, Any]) -> dict[str, Any]:
    def content_block(block: Any) -> Any:
        if not isinstance(block, dict):
            return block
        visible = {key: value for key, value in block.items() if key != "_meta"}
        if isinstance(visible.get("resource"), dict):
            visible["resource"] = {key: value for key, value in visible["resource"].items() if key != "_meta"}
        return visible

    def content(value: Any) -> Any:
        return [content_block(block) for block in value] if isinstance(value, list) else content_block(value)

    visible = {key: value for key, value in raw.items() if key != "_meta"}
    if "content" in visible:
        visible["content"] = content(visible["content"])
    if "messages" in visible:
        visible["messages"] = [
            {**{key: value for key, value in message.items() if key not in {"_meta", "content"}},
             "content": content(message.get("content"))}
            for message in visible["messages"]
        ]
    return visible


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
