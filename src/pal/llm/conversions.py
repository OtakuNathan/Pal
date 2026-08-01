from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, ToolDefinitionIR, ToolResultIR

import json
from typing import Any, Mapping

from pal.llm.ir import (
    GenerationPolicyIR,
    ImagePartIR,
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
    MessageState,
    ReasoningPartIR,
    ReplayEnvelope,
    TextPartIR,
    WireShape,
)
from pal.shared.json_values import thaw_json


class LLMConversionError(ValueError):
    pass


def request_ir_from_prompt(
    *,
    messages: list[dict[str, Any]],
    max_output_tokens: int,
    model_hint: str | None = None,
    temperature: float | None = None,
    tools: list[dict[str, Any]] | None = None,
    metadata: Mapping[str, Any] | None = None,
    thinking_budget_tokens: int | None = None,
) -> LLMRequestIR:
    return LLMRequestIR(
        messages=tuple(message_ir_from_dict(message) for message in messages),
        tools=tuple(tool_definition_ir_from_dict(tool) for tool in (tools or [])),
        policy=GenerationPolicyIR(
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            thinking_budget_tokens=thinking_budget_tokens,
        ),
        model_hint=model_hint,
        metadata=dict(metadata or {}),
    )


def message_ir_from_dict(message: Mapping[str, Any]) -> LLMMessageIR:
    role = MessageRole(str(message.get("role") or "user"))
    parts: list[Any] = []
    content = message.get("content")
    if role != MessageRole.TOOL and isinstance(content, str):
        if content:
            parts.append(TextPartIR(content))
    elif role != MessageRole.TOOL and isinstance(content, list):
        for item in content:
            if not isinstance(item, Mapping):
                continue
            item_type = str(item.get("type") or "")
            if item_type in {"text", "input_text", "output_text"}:
                text = str(item.get("text") or "")
                if text:
                    parts.append(TextPartIR(text))
            elif item_type in {"image", "image_url", "input_image"}:
                source = item.get("image_url") or item.get("url") or item.get("source")
                if isinstance(source, Mapping):
                    source = source.get("url")
                if source:
                    parts.append(
                        ImagePartIR(
                            str(source),
                            media_type=str(item.get("media_type") or "").strip() or None,
                        )
                    )
            elif item_type == "artifact_image":
                raise ValueError("artifact_image must be resolved before LLM IR construction")
    if role == MessageRole.ASSISTANT:
        reasoning_content = message.get("reasoning_content")
        if isinstance(reasoning_content, str) and reasoning_content:
            parts.insert(0, ReasoningPartIR(reasoning_content))
        for index, call in enumerate(list(message.get("tool_calls") or [])):
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            if not isinstance(function, Mapping):
                function = call
            call_id = str(call.get("id") or call.get("call_id") or "").strip()
            name = str(function.get("name") or call.get("name") or "").strip()
            if not call_id or not name:
                continue
            raw_arguments = function.get("arguments") or function.get("args") or {}
            if isinstance(raw_arguments, str):
                try:
                    raw_arguments = json.loads(raw_arguments)
                except json.JSONDecodeError as exc:
                    raise LLMConversionError(
                        f"tool call {name!r} ({call_id}) arguments are invalid JSON "
                        f"at line {exc.lineno} column {exc.colno}: {exc.msg}"
                    ) from exc
            if not isinstance(raw_arguments, Mapping):
                raise LLMConversionError(
                    f"tool call {name!r} ({call_id}) arguments must decode to an object"
                )
            parts.append(
                ToolCallIR(
                    call_id=call_id,
                    name=name,
                    arguments=dict(raw_arguments),
                )
            )
    if role == MessageRole.TOOL:
        call_id = str(message.get("tool_call_id") or message.get("call_id") or "").strip()
        name = str(message.get("name") or "tool").strip() or "tool"
        if call_id:
            exact_result = message.get("_pal_tool_result")
            result = dict(exact_result) if isinstance(exact_result, Mapping) else {}
            parts.append(
                ToolResultIR(
                    call_id=call_id,
                    name=name,
                    content=_content_text(content),
                    ok=bool(result.get("ok", True)),
                    status=str(result.get("status") or "ok"),
                    structured=(
                        dict(result["structured"])
                        if isinstance(result.get("structured"), Mapping)
                        else None
                    ),
                )
            )
    metadata = (
        dict(message["_pal_metadata"])
        if isinstance(message.get("_pal_metadata"), Mapping)
        else {}
    )
    metadata.update(
        {
            key: value
            for key, value in message.items()
            if str(key).startswith("_pal_")
            and key
            not in {
                "_pal_message_id",
                "_pal_state",
                "_pal_semantic_kind",
                "_pal_replay",
                "_pal_metadata",
                "_pal_tool_result",
            }
        }
    )
    replay = _replay_from_legacy(message.get("_pal_replay"))
    kwargs: dict[str, Any] = {}
    message_id = str(message.get("_pal_message_id") or "").strip()
    if message_id:
        kwargs["message_id"] = message_id
    return LLMMessageIR(
        role=role,
        parts=tuple(parts),
        state=MessageState(str(message.get("_pal_state") or "complete")),
        semantic_kind=str(message.get("_pal_semantic_kind") or message.get("kind") or ""),
        replay=replay,
        metadata=metadata,
        **kwargs,
    )


def tool_definition_ir_from_dict(tool: Mapping[str, Any]) -> ToolDefinitionIR:
    function = tool.get("function")
    source = function if isinstance(function, Mapping) else tool
    name = str(source.get("name") or "").strip()
    schema = source.get("parameters") or source.get("input_schema") or {"type": "object", "properties": {}}
    if not name:
        raise LLMConversionError("tool definition has no name")
    if not isinstance(schema, Mapping):
        raise LLMConversionError(f"tool {name!r} input schema must be an object")
    return ToolDefinitionIR(
        name=name,
        description=str(source.get("description") or name),
        input_schema=dict(schema),
    )


def message_ir_to_dict(message: LLMMessageIR) -> dict[str, Any]:
    if any(isinstance(part, ReasoningPartIR) and part.redacted for part in message.parts):
        raise LLMConversionError(
            "legacy message dictionaries cannot represent redacted reasoning without losing information"
        )
    content_parts: list[dict[str, Any]] = []
    for part in message.parts:
        if isinstance(part, TextPartIR):
            content_parts.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePartIR):
            content_parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": part.source},
                    "media_type": part.media_type,
                }
            )
    content: Any
    if not content_parts:
        content = ""
    elif len(content_parts) == 1 and content_parts[0]["type"] == "text":
        content = content_parts[0]["text"]
    else:
        content = content_parts
    payload: dict[str, Any] = {
        "role": message.role.value,
        "content": content,
        "_pal_message_id": message.message_id,
        "_pal_state": message.state.value,
        "_pal_semantic_kind": message.semantic_kind,
        "_pal_metadata": thaw_json(message.metadata),
    }
    if message.replay is not None:
        payload["_pal_replay"] = {
            "wire_shape": message.replay.wire_shape.value,
            "endpoint_id": message.replay.endpoint_id,
            "model_id": message.replay.model_id,
            "payload": thaw_json(message.replay.payload),
        }
    if message.tool_calls:
        payload["tool_calls"] = [
            {
                "id": call.call_id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(thaw_json(call.arguments), ensure_ascii=False)},
            }
            for call in message.tool_calls
        ]
    if message.reasoning_text:
        payload["reasoning_content"] = message.reasoning_text
    results = [part for part in message.parts if isinstance(part, ToolResultIR)]
    if len(results) > 1:
        raise LLMConversionError(
            "one legacy message cannot represent multiple tool results without losing information"
        )
    if results:
        result = results[0]
        payload.update(
            {
                "tool_call_id": result.call_id,
                "name": result.name,
                "content": result.content,
                "_pal_tool_result": {
                    "ok": result.ok,
                    "status": result.status,
                    "structured": (
                        thaw_json(result.structured)
                        if result.structured is not None
                        else None
                    ),
                },
            }
        )
    return payload


def tool_definition_ir_to_dict(tool: ToolDefinitionIR) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": thaw_json(tool.input_schema),
        },
    }


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(str(item.get("text") or "") for item in value if isinstance(item, Mapping))
    return str(value or "")


def _replay_from_legacy(value: Any) -> ReplayEnvelope | None:
    if not isinstance(value, Mapping):
        return None
    return ReplayEnvelope(
        wire_shape=WireShape(str(value.get("wire_shape") or "")),
        endpoint_id=str(value.get("endpoint_id") or ""),
        model_id=str(value.get("model_id") or ""),
        payload=dict(value.get("payload") or {}),
    )
