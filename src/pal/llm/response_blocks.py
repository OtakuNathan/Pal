"""Non-executable response blocks between wire splitting and IR decoding.

Text/reasoning already have unambiguous block types. Tool payloads remain raw
JSON values or strings until both protocol-hook layers have finished. Only
decode_response_blocks creates the shared, executable ToolCallIR contract.
"""
from __future__ import annotations

import json
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from pal.llm.decode_contracts import ShapeDecodeError
from pal.llm.ir import (
    LLMFinishReason, LLMMessageIR, LLMResponseDeltaKind, LLMResponseIR,
    LLMResponseItemKind, LLMResponseUpdate, LLMUsageIR, MessageRole,
    MessageState, PromptRegionIR, ReasoningPartIR, ReplayEnvelope, TextPartIR, WireShape,
)
from pal.shared.json_values import freeze_json_mapping, thaw_json
from pal.shared.tool_protocol import ToolCallIR


@dataclass(frozen=True)
class ResponseBlock:
    kind: LLMResponseItemKind
    payload: Mapping[str, Any]
    label: str = "tool arguments"

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", LLMResponseItemKind(self.kind))
        object.__setattr__(self, "payload", freeze_json_mapping(self.payload))

    @property
    def call_id(self) -> str:
        return str(self.payload.get("call_id") or "")

    @property
    def name(self) -> str:
        return str(self.payload.get("name") or "")

    @property
    def arguments(self) -> Any:
        return self.payload.get("arguments")


def tool_block(*, call_id: str, name: str, arguments: Any, label: str = "tool arguments") -> ResponseBlock:
    return ResponseBlock(LLMResponseItemKind.TOOL_CALL,
                         {"call_id": call_id, "name": name, "arguments": arguments}, label)


@dataclass(frozen=True)
class ResponseBlockMessage:
    role: MessageRole = MessageRole.ASSISTANT
    parts: tuple[TextPartIR | ReasoningPartIR | ResponseBlock, ...] = ()
    message_id: str = ""
    state: MessageState = MessageState.IN_PROGRESS
    replay: ReplayEnvelope | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    semantic_kind: str = ""
    prompt_region: PromptRegionIR = PromptRegionIR.UNSPECIFIED

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", tuple(self.parts))
        object.__setattr__(self, "metadata", freeze_json_mapping(self.metadata))

    @property
    def text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, TextPartIR))

    @property
    def reasoning_text(self) -> str:
        return "".join(p.text for p in self.parts if isinstance(p, ReasoningPartIR))

    @property
    def tool_blocks(self) -> tuple[ResponseBlock, ...]:
        return tuple(p for p in self.parts if isinstance(p, ResponseBlock)
                     and p.kind == LLMResponseItemKind.TOOL_CALL)


@dataclass(frozen=True)
class ResponseBlockSnapshot:
    message: ResponseBlockMessage
    finish_reason: LLMFinishReason = LLMFinishReason.STOP
    usage: LLMUsageIR = field(default_factory=LLMUsageIR)
    provider_response_count: int = 1
    provider_generation_id: str = ""
    returned_model: str = ""
    actual_provider: str = ""
    service_tier: str = ""
    reasoning_context: str = ""
    attempt_ids: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return self.message.text

    @property
    def reasoning_text(self) -> str:
        return self.message.reasoning_text

    @property
    def tool_blocks(self) -> tuple[ResponseBlock, ...]:
        return self.message.tool_blocks


@dataclass(frozen=True)
class ResponseBlockUpdate:
    response: ResponseBlockSnapshot
    delta_kind: LLMResponseDeltaKind
    text_delta: str = ""
    tool_block: ResponseBlock | None = None
    item_id: str = ""
    item_kind: LLMResponseItemKind | None = None


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


def decode_response_blocks(updates: Iterable[ResponseBlockUpdate]) -> Iterator[LLMResponseUpdate]:
    calls: dict[str, tuple[ResponseBlock, ToolCallIR]] = {}

    def decode_block(block: ResponseBlock) -> ToolCallIR:
        if block.kind != LLMResponseItemKind.TOOL_CALL:
            raise ShapeDecodeError(f"unsupported response block {block.kind}")
        previous = calls.get(block.call_id)
        if previous is not None and previous[0] is block:
            return previous[1]
        if not block.call_id.strip() or not block.name.strip():
            raise ShapeDecodeError("tool block requires a call id and name")
        call = ToolCallIR(block.call_id, block.name,
                          json_object(block.arguments, label=block.label))
        if previous is not None and previous[1] != call:
            raise ShapeDecodeError("decoded tool block changed after publication")
        calls[block.call_id] = (block, call)
        return call

    for update in updates:
        source = update.response
        if any(not isinstance(p, (TextPartIR, ReasoningPartIR, ResponseBlock))
               for p in source.message.parts):
            raise ShapeDecodeError("response hooks must return non-executable blocks")
        parts = tuple(decode_block(p) if isinstance(p, ResponseBlock) else p
                      for p in source.message.parts)
        message = LLMMessageIR(
            role=source.message.role, parts=parts, message_id=source.message.message_id,
            state=source.message.state,
            replay=materialize_replay(source.message.replay,
                                      tuple(p for p in parts if isinstance(p, ToolCallIR))),
            metadata=source.message.metadata,
            semantic_kind=source.message.semantic_kind, prompt_region=source.message.prompt_region,
        )
        response = LLMResponseIR(
            message=message, finish_reason=source.finish_reason, usage=source.usage,
            provider_response_count=source.provider_response_count,
            provider_generation_id=source.provider_generation_id, returned_model=source.returned_model,
            actual_provider=source.actual_provider, service_tier=source.service_tier,
            reasoning_context=source.reasoning_context, attempt_ids=source.attempt_ids,
        )
        yield LLMResponseUpdate(
            response, update.delta_kind, text_delta=update.text_delta,
            tool_call=decode_block(update.tool_block) if update.tool_block is not None else None,
            item_id=update.item_id, item_kind=update.item_kind,
        )


def materialize_replay(
    replay: ReplayEnvelope | None, calls: tuple[ToolCallIR, ...] = (),
) -> ReplayEnvelope | None:
    """Materialize native tool input only AFTER hook processing.

    Anthropic SSE carries input as JSON fragments; native replay needs an object.
    Repairs of malformed native parameters retain the original source payload.
    """
    if replay is None:
        return None
    if not calls:
        if replay.wire_shape != WireShape.ANTHROPIC_MESSAGES:
            return replay
        if not any(item.get("type") == "tool_use" and isinstance(item.get("input"), str)
                   for item in replay.payload.get("content", ())):
            return replay
    payload = thaw_json(replay.payload)
    accepted = {c.call_id: c for c in calls}
    changed_by_hook = False
    if replay.wire_shape == WireShape.ANTHROPIC_MESSAGES:
        for item in payload.get("content", ()):
            if item.get("type") != "tool_use":
                continue
            raw = item.get("input")
            if "input" not in item or raw is None:
                # Missing native fields remain missing evidence, even if the
                # semantic fallback accepted empty arguments.
                continue
            call = accepted.get(str(item.get("id") or ""))
            if call is not None and item.get("name") != call.name:
                item["name"] = call.name
                changed_by_hook = True
            try:
                arguments = json_object(raw, label="native tool input")
            except ShapeDecodeError:
                if call is None:
                    continue
                arguments = thaw_json(call.arguments)
                changed_by_hook = True
            if call is not None and arguments != thaw_json(call.arguments):
                arguments = thaw_json(call.arguments)
                changed_by_hook = True
            item["input"] = arguments
    else:
        if replay.wire_shape == WireShape.OPENAI_COMPLETION:
            items = payload.get("message", {}).get("tool_calls", ())
        else:
            items = [item for item in payload.get("output", ()) if item.get("type") == "function_call"]
        for item in items:
            call = accepted.get(str(item.get("id") if replay.wire_shape == WireShape.OPENAI_COMPLETION else item.get("call_id")))
            if call is None:
                continue
            value = item.get("function", {}) if replay.wire_shape == WireShape.OPENAI_COMPLETION else item
            if value.get("name") != call.name:
                value["name"] = call.name
                changed_by_hook = True
            if "arguments" not in value or value["arguments"] is None:
                continue
            try:
                arguments = json_object(value.get("arguments"), label="native tool arguments")
            except ShapeDecodeError:
                arguments = None
            if arguments != thaw_json(call.arguments):
                value["arguments"] = json.dumps(thaw_json(call.arguments), ensure_ascii=False)
                changed_by_hook = True
    if payload == thaw_json(replay.payload):
        return replay
    return replace(replay, payload=payload, source_payload=(
        replay.source_payload if replay.source_payload is not None else replay.payload
    ) if changed_by_hook else replay.source_payload)


def response_to_blocks(response: LLMResponseIR) -> ResponseBlockSnapshot:
    """Reuse the same adapter pipeline for merged output-recovery responses."""
    parts: list[TextPartIR | ReasoningPartIR | ResponseBlock] = []
    for part in response.message.parts:
        if isinstance(part, ToolCallIR):
            parts.append(tool_block(call_id=part.call_id, name=part.name, arguments=part.arguments))
        elif isinstance(part, (TextPartIR, ReasoningPartIR)):
            parts.append(part)
        else:
            raise ShapeDecodeError("response recovery contains an unsupported content part")
    return ResponseBlockSnapshot(
        message=ResponseBlockMessage(
            role=response.message.role,
            parts=tuple(parts),
            message_id=response.message.message_id, state=response.message.state,
            replay=response.message.replay, metadata=response.message.metadata,
            semantic_kind=response.message.semantic_kind, prompt_region=response.message.prompt_region,
        ),
        finish_reason=response.finish_reason, usage=response.usage,
        provider_response_count=response.provider_response_count,
        provider_generation_id=response.provider_generation_id, returned_model=response.returned_model,
        actual_provider=response.actual_provider, service_tier=response.service_tier,
        reasoning_context=response.reasoning_context, attempt_ids=response.attempt_ids,
    )


def updates_to_blocks(updates: Iterable[LLMResponseUpdate]) -> Iterator[ResponseBlockUpdate]:
    for update in updates:
        call = update.tool_call
        yield ResponseBlockUpdate(
            response_to_blocks(update.response), update.delta_kind, update.text_delta,
            tool_block=tool_block(call_id=call.call_id, name=call.name, arguments=call.arguments)
            if call is not None else None,
            item_id=update.item_id, item_kind=update.item_kind,
        )
