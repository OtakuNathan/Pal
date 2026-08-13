from __future__ import annotations

from typing import Any, Mapping

from pal.llm.ir import (
    ArtifactRefPartIR,
    ImagePartIR,
    LLMFinishReason,
    LLMMessageIR,
    LLMResponseDeltaKind,
    LLMResponseItemKind,
    LLMResponseIR,
    LLMResponseUpdate,
    LLMUsageIR,
    MessageRole,
    MessageState,
    PromptRegionIR,
    ReasoningPartIR,
    ReplayEnvelope,
    TextPartIR,
    WireShape,
)
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import (
    ToolCallIR,
    ToolResultIR,
)


def update_to_payload(update: LLMResponseUpdate) -> dict[str, Any]:
    return {
        "response": response_to_payload(update.response),
        "delta_kind": update.delta_kind.value,
        "text_delta": update.text_delta,
        "tool_call": part_to_payload(update.tool_call) if update.tool_call else None,
        "item_id": update.item_id,
        "item_kind": update.item_kind.value if update.item_kind is not None else None,
    }


def update_from_payload(payload: Mapping[str, Any]) -> LLMResponseUpdate:
    call_payload = payload.get("tool_call")
    call = part_from_payload(call_payload) if isinstance(call_payload, Mapping) else None
    if call is not None and not isinstance(call, ToolCallIR):
        raise ValueError("stream update tool_call is not a tool call")
    return LLMResponseUpdate(
        response=response_from_payload(dict(payload.get("response") or {})),
        delta_kind=LLMResponseDeltaKind(str(payload.get("delta_kind") or "state")),
        text_delta=str(payload.get("text_delta") or ""),
        tool_call=call,
        item_id=str(payload.get("item_id") or ""),
        item_kind=(
            LLMResponseItemKind(str(payload.get("item_kind")))
            if payload.get("item_kind")
            else None
        ),
    )


def response_to_payload(response: LLMResponseIR) -> dict[str, Any]:
    return {
        "message": message_to_payload(response.message),
        "finish_reason": response.finish_reason.value,
        "usage": dict(response.usage.__dict__),
        "provider_response_count": response.provider_response_count,
    }


def response_from_payload(payload: Mapping[str, Any]) -> LLMResponseIR:
    usage = dict(payload.get("usage") or {})
    raw_provider_response_count = payload.get("provider_response_count")
    return LLMResponseIR(
        message=message_from_payload(dict(payload.get("message") or {})),
        finish_reason=LLMFinishReason(str(payload.get("finish_reason") or "error")),
        usage=LLMUsageIR(**{key: usage[key] for key in LLMUsageIR.__dataclass_fields__ if key in usage}),
        provider_response_count=(
            1
            if raw_provider_response_count is None
            else int(raw_provider_response_count)
        ),
    )


def message_to_payload(message: LLMMessageIR) -> dict[str, Any]:
    return {
        "role": message.role.value,
        "parts": [part_to_payload(item) for item in message.parts],
        "message_id": message.message_id,
        "state": message.state.value,
        "semantic_kind": message.semantic_kind,
        "prompt_region": message.prompt_region.value,
        "replay": (
            {
                "wire_shape": message.replay.wire_shape.value,
                "endpoint_id": message.replay.endpoint_id,
                "model_id": message.replay.model_id,
                "payload": thaw_json(message.replay.payload),
            }
            if message.replay
            else None
        ),
        "metadata": thaw_json(message.metadata),
    }


def message_from_payload(payload: Mapping[str, Any]) -> LLMMessageIR:
    replay_payload = payload.get("replay")
    replay = None
    if isinstance(replay_payload, Mapping):
        replay = ReplayEnvelope(
            wire_shape=WireShape(str(replay_payload.get("wire_shape") or "")),
            endpoint_id=str(replay_payload.get("endpoint_id") or ""),
            model_id=str(replay_payload.get("model_id") or ""),
            payload=dict(replay_payload.get("payload") or {}),
        )
    return LLMMessageIR(
        role=MessageRole(str(payload.get("role") or "user")),
        parts=tuple(part_from_payload(item) for item in payload.get("parts") or ()),
        message_id=str(payload.get("message_id") or ""),
        state=MessageState(str(payload.get("state") or "complete")),
        semantic_kind=str(payload.get("semantic_kind") or ""),
        prompt_region=PromptRegionIR(
            str(payload.get("prompt_region") or "unspecified")
        ),
        replay=replay,
        metadata=dict(payload.get("metadata") or {}),
    )


def part_to_payload(part: Any) -> dict[str, Any]:
    if isinstance(part, TextPartIR):
        return {"kind": "text", "text": part.text}
    if isinstance(part, ImagePartIR):
        return {"kind": "image", "source": part.source, "media_type": part.media_type}
    if isinstance(part, ArtifactRefPartIR):
        return {
            "kind": "artifact_ref",
            "artifact_id": part.artifact_id,
            "artifact_kind": part.kind,
            "file_name": part.file_name,
            "summary": part.summary,
            "status": part.status,
            "available_actions": list(part.available_actions),
        }
    if isinstance(part, ReasoningPartIR):
        return {"kind": "reasoning", "text": part.text, "redacted": part.redacted}
    if isinstance(part, ToolCallIR):
        return {"kind": "tool_call", "call_id": part.call_id, "name": part.name, "arguments": thaw_json(part.arguments)}
    if isinstance(part, ToolResultIR):
        return {
            "kind": "tool_result", "call_id": part.call_id, "name": part.name,
            "content": part.content, "ok": part.ok, "status": part.status,
            "structured": thaw_json(part.structured) if part.structured is not None else None,
            # Runtime-only truth. Provider shape codecs deliberately ignore it.
            "context_delivery": (
                thaw_json(part.context_delivery)
                if part.context_delivery is not None
                else None
            ),
            "replay_result_ref": part.replay_result_ref,
        }
    raise TypeError(f"unsupported LLM IR part: {type(part).__name__}")


def part_from_payload(payload: Mapping[str, Any]) -> Any:
    kind = str(payload.get("kind") or "")
    if kind == "text":
        return TextPartIR(str(payload.get("text") or ""))
    if kind == "image":
        return ImagePartIR(str(payload.get("source") or ""), str(payload.get("media_type") or "") or None)
    if kind == "artifact_ref":
        return ArtifactRefPartIR(
            artifact_id=str(payload.get("artifact_id") or ""),
            kind=str(payload.get("artifact_kind") or ""),
            file_name=str(payload.get("file_name") or ""),
            summary=str(payload.get("summary") or ""),
            status=str(payload.get("status") or ""),
            available_actions=tuple(str(item) for item in payload.get("available_actions") or ()),
        )
    if kind == "reasoning":
        return ReasoningPartIR(str(payload.get("text") or ""), bool(payload.get("redacted")))
    if kind == "tool_call":
        call_id = str(payload.get("call_id") or "").strip()
        if not call_id:
            raise ValueError("serialized tool call is missing call_id")
        return ToolCallIR(
            name=str(payload.get("name") or ""), arguments=dict(payload.get("arguments") or {}),
            call_id=call_id,
        )
    if kind == "tool_result":
        return ToolResultIR(
            call_id=str(payload.get("call_id") or ""), name=str(payload.get("name") or ""),
            content=str(payload.get("content") or ""), ok=bool(payload.get("ok", True)),
            status=str(payload.get("status") or "ok"),
            structured=dict(payload["structured"]) if isinstance(payload.get("structured"), Mapping) else None,
            context_delivery=(
                dict(payload["context_delivery"])
                if isinstance(payload.get("context_delivery"), Mapping)
                else None
            ),
            replay_result_ref=str(payload.get("replay_result_ref") or ""),
        )
    raise ValueError(f"unknown LLM IR part kind: {kind}")
