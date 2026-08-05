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
from pal.llm.shapes.base import ShapeContext, ShapeDecodeError


class ResponseIRBuilder:
    def __init__(self, context: ShapeContext) -> None:
        self.context = context
        self.message_id = str(uuid4())
        self.parts: list[Any] = []
        self.finish_reason = LLMFinishReason.STOP
        self.usage = LLMUsageIR()
        self.replay_payload: dict[str, Any] = {}
        self.committed_items: dict[str, LLMResponseItemKind] = {}
        self.complete = False

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


def usage_from_mapping(payload: Mapping[str, Any] | None) -> LLMUsageIR:
    source = dict(payload or {})
    raw_input_tokens = _first_int(source, "input_tokens", "prompt_tokens")
    output_tokens = _first_int(source, "output_tokens", "completion_tokens")
    cached = _first_int(source, "cached_input_tokens", "cache_read_input_tokens")
    cache_write = _first_int(source, "cache_creation_input_tokens", "cache_write_input_tokens")
    details = source.get("prompt_tokens_details")
    if isinstance(details, Mapping):
        cached = max(cached, _first_int(details, "cached_tokens"))
        cache_write = max(cache_write, _first_int(details, "cache_write_tokens"))
    anthropic_categories = any(
        key in source
        for key in ("cache_read_input_tokens", "cache_creation_input_tokens")
    )
    if anthropic_categories:
        uncached = raw_input_tokens
        input_tokens = raw_input_tokens + cached + cache_write
    else:
        input_tokens = raw_input_tokens
        uncached = max(0, input_tokens - cached - cache_write)
    output_details = source.get("completion_tokens_details")
    reasoning = _first_int(source, "reasoning_tokens")
    if isinstance(output_details, Mapping):
        reasoning = max(reasoning, _first_int(output_details, "reasoning_tokens"))
    return LLMUsageIR(
        input_tokens=input_tokens,
        uncached_input_tokens=uncached,
        cached_input_tokens=cached,
        cache_write_input_tokens=cache_write,
        output_tokens=output_tokens,
        reasoning_tokens=reasoning,
        cost=_first_float(source, "cost", "total_cost"),
        reported=payload is not None,
    )


def merge_usage(left: LLMUsageIR, right: LLMUsageIR) -> LLMUsageIR:
    if not right.reported:
        return left
    return replace(
        right,
        input_tokens=max(left.input_tokens, right.input_tokens),
        uncached_input_tokens=max(left.uncached_input_tokens, right.uncached_input_tokens),
        cached_input_tokens=max(left.cached_input_tokens, right.cached_input_tokens),
        cache_write_input_tokens=max(left.cache_write_input_tokens, right.cache_write_input_tokens),
        output_tokens=max(left.output_tokens, right.output_tokens),
        reasoning_tokens=max(left.reasoning_tokens, right.reasoning_tokens),
        cost=max(left.cost, right.cost),
        reported=left.reported or right.reported,
    )


def _first_int(source: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = source.get(name)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            continue
    return 0


def _first_float(source: Mapping[str, Any], *names: str) -> float:
    for name in names:
        value = source.get(name)
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            continue
    return 0.0
