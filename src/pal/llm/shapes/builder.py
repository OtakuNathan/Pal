from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

from dataclasses import replace
from typing import Any, Mapping
from uuid import uuid4

from pal.llm.ir import (
    LLMFinishReason,
    LLMMessageIR,
    LLMResponseDeltaKind,
    LLMResponseItemKind,
    LLMResponseIR,
    LLMResponseUpdate,
    LLMUsageIR,
    MessageRole,
    MessageState,
    ReasoningPartIR,
    ReplayEnvelope,
    TextPartIR,
)
from pal.llm.response_evidence import WireResponseEvidence
from pal.llm.usage_normalization import merge_usage, usage_from_mapping
from pal.llm.shapes.base import ShapeContext, ShapeDecodeError


class ResponseIRBuilder:
    def __init__(self, context: ShapeContext) -> None:
        self.context = context
        self.message_id = str(uuid4())
        self.parts: list[Any] = []
        self.finish_reason = LLMFinishReason.STOP
        self.usage = LLMUsageIR()
        self.replay_payload: dict[str, Any] = {}
        self.replay_source_payload: dict[str, Any] | None = None
        self.committed_items: dict[str, LLMResponseItemKind] = {}
        self.complete = False
        self.evidence = WireResponseEvidence(context.wire_shape)
        self.provider_generation_id = ""

    def observe_frame(self, frame: Any) -> None:
        self.evidence.observe(frame)
        self.usage = self.evidence.usage
        self.provider_generation_id = self.evidence.provider_generation_id

    def append_text(self, text: str) -> LLMResponseUpdate | None:
        value = str(text or "")
        if not value:
            return None
        if self.parts and isinstance(self.parts[-1], TextPartIR):
            self.parts[-1] = TextPartIR(self.parts[-1].text + value)
        else:
            self.parts.append(TextPartIR(value))
        return self.update(LLMResponseDeltaKind.TEXT, text_delta=value)

    def append_reasoning(self, text: str, *, redacted: bool = False) -> LLMResponseUpdate | None:
        value = str(text or "")
        if not value and not redacted:
            return None
        if (
            self.parts
            and isinstance(self.parts[-1], ReasoningPartIR)
            and self.parts[-1].redacted == redacted
        ):
            previous = self.parts[-1]
            self.parts[-1] = ReasoningPartIR(previous.text + value, redacted=redacted)
        else:
            self.parts.append(ReasoningPartIR(value, redacted=redacted))
        return self.update(LLMResponseDeltaKind.REASONING, text_delta=value)

    def append_tool_call(self, *, call_id: str, name: str, arguments: Mapping[str, Any]) -> LLMResponseUpdate:
        tool_call = ToolCallIR(
            call_id=str(call_id),
            name=str(name),
            arguments=dict(arguments),
        )
        self.parts.append(tool_call)
        self.finish_reason = LLMFinishReason.TOOL_CALLS
        return self.update(LLMResponseDeltaKind.TOOL_CALL, tool_call=tool_call)

    def set_usage(self, usage: LLMUsageIR) -> None:
        self.usage = usage

    def set_generation_id(self, generation_id: str) -> None:
        value = str(generation_id or "").strip()
        if value:
            self.provider_generation_id = value

    def mark_complete(self, finish_reason: LLMFinishReason | str | None = None) -> LLMResponseUpdate:
        if finish_reason is not None:
            self.finish_reason = canonical_finish_reason(finish_reason, has_tools=self.has_tools)
        elif self.has_tools:
            self.finish_reason = LLMFinishReason.TOOL_CALLS
        self.complete = True
        return self.update(LLMResponseDeltaKind.STATE)

    @property
    def has_tools(self) -> bool:
        return any(isinstance(part, ToolCallIR) for part in self.parts)

    def discard_tool_calls(self) -> None:
        self.parts = [part for part in self.parts if not isinstance(part, ToolCallIR)]
        self.committed_items = {
            item_id: kind
            for item_id, kind in self.committed_items.items()
            if kind != LLMResponseItemKind.TOOL_CALL
        }
        self.replay_payload = {}

    def commit_item(
        self,
        *,
        item_id: str,
        item_kind: LLMResponseItemKind | str,
        tool_call: ToolCallIR | None = None,
    ) -> LLMResponseUpdate | None:
        normalized_id = str(item_id or "").strip()
        if not normalized_id:
            raise ShapeDecodeError("completed LLM item has no stable id")
        normalized_kind = LLMResponseItemKind(item_kind)
        previous = self.committed_items.get(normalized_id)
        if previous is not None:
            if previous != normalized_kind:
                raise ShapeDecodeError("completed LLM item changed kind")
            return None
        if normalized_kind == LLMResponseItemKind.TOOL_CALL and tool_call is None:
            raise ShapeDecodeError("completed LLM tool item has no tool call")
        self.committed_items[normalized_id] = normalized_kind
        try:
            return LLMResponseUpdate(
                response=self.snapshot(),
                delta_kind=LLMResponseDeltaKind.ITEM_COMMITTED,
                tool_call=tool_call,
                item_id=normalized_id,
                item_kind=normalized_kind,
            )
        except Exception:
            self.committed_items.pop(normalized_id, None)
            raise

    def snapshot(self) -> LLMResponseIR:
        replay = None
        if self.replay_payload:
            replay = ReplayEnvelope(
                wire_shape=self.context.wire_shape,
                endpoint_id=self.context.endpoint_id,
                model_id=self.context.model_id,
                payload=dict(self.replay_payload),
                source_payload=self.replay_source_payload,
            )
        state = MessageState.COMPLETE if self.complete else MessageState.IN_PROGRESS
        message = LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=tuple(self.parts),
            message_id=self.message_id,
            state=state,
            replay=replay,
            metadata={
                "committed_items": [
                    {"item_id": item_id, "item_kind": kind.value}
                    for item_id, kind in self.committed_items.items()
                ]
            },
        )
        return LLMResponseIR(
            message=message,
            finish_reason=self.finish_reason,
            usage=self.usage,
            provider_generation_id=self.provider_generation_id,
            returned_model=self.evidence.returned_model,
            actual_provider=self.evidence.actual_provider,
            service_tier=self.evidence.service_tier,
        )

    def update(
        self,
        kind: LLMResponseDeltaKind,
        *,
        text_delta: str = "",
        tool_call: ToolCallIR | None = None,
    ) -> LLMResponseUpdate:
        return LLMResponseUpdate(
            response=self.snapshot(),
            delta_kind=kind,
            text_delta=text_delta,
            tool_call=tool_call,
        )

    def finish(self) -> LLMResponseIR:
        if not self.complete:
            self.complete = True
        response = self.snapshot()
        if not response.message.parts:
            raise ShapeDecodeError("LLM response contained no assistant content or tool calls")
        return response


def canonical_finish_reason(value: LLMFinishReason | str, *, has_tools: bool = False) -> LLMFinishReason:
    raw = str(getattr(value, "value", value) or "").strip().lower()
    if raw in {"length", "max_tokens", "max_output_tokens", "incomplete"}:
        return LLMFinishReason.LENGTH
    if raw in {"content_filter", "refusal", "safety"}:
        return LLMFinishReason.CONTENT_FILTER
    if raw in {"error", "failed", "cancelled", "canceled"}:
        return LLMFinishReason.ERROR
    if has_tools or raw in {"tool_calls", "tool_use", "function_call"}:
        return LLMFinishReason.TOOL_CALLS
    return LLMFinishReason.STOP
