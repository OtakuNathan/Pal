from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

import json
from dataclasses import dataclass
from typing import Any, Mapping

from pal.llm.ir import (
    ImagePartIR,
    LLMFinishReason,
    LLMRequestIR,
    LLMResponseDeltaKind,
    LLMResponseItemKind,
    LLMResponseIR,
    LLMResponseUpdate,
    MessageRole,
    ReasoningPartIR,
    TextPartIR,
    ThinkingLevel,
    WireShape,
)
from pal.llm.shapes.base import (
    EncodedRequest,
    EncodedMessageSpan,
    ShapeCodecBase,
    ShapeContext,
    ShapeDecodeError,
    _JSONFrame,
)
from pal.llm.shapes.builder import (
    ResponseIRBuilder,
    canonical_finish_reason,
    merge_usage,
    usage_from_mapping,
)
from pal.llm.shapes.common import json_object, responses_tool_definition, tool_results
from pal.shared.json_values import thaw_json


@dataclass(frozen=True)
class OpenAIResponseCodec(ShapeCodecBase):
    wire_shape: WireShape = WireShape.OPENAI_RESPONSE

    def encode(self, request: LLMRequestIR, context: ShapeContext) -> EncodedRequest:
        input_items: list[dict[str, Any]] = []
        spans: list[EncodedMessageSpan] = []
        for message in request.messages:
            targets: list[tuple[str | int, ...]] = []
            if (
                message.role == MessageRole.ASSISTANT
                and message.replay is not None
                and message.replay.matches(
                    wire_shape=self.wire_shape,
                    endpoint_id=context.endpoint_id,
                    model_id=context.model_id,
                )
            ):
                output = message.replay.payload.get("output")
                if isinstance(output, (list, tuple)):
                    input_items.extend(thaw_json(item) for item in output if isinstance(item, Mapping))
                    spans.append(EncodedMessageSpan(message.message_id))
                    continue
            if message.role in {MessageRole.SYSTEM, MessageRole.DEVELOPER}:
                text = "".join(part.text for part in message.parts if isinstance(part, TextPartIR))
                if text:
                    input_items.append(
                        {
                            "role": "developer",
                            "content": [{"type": "input_text", "text": text}],
                        }
                    )
                    targets.append(("input", len(input_items) - 1, "content", 0))
                spans.append(EncodedMessageSpan(message.message_id, tuple(targets)))
                continue
            if message.role == MessageRole.TOOL:
                for result in tool_results(message):
                    input_items.append(
                        {
                            "type": "function_call_output",
                            "call_id": result.call_id,
                            "output": result.content,
                        }
                    )
                spans.append(EncodedMessageSpan(message.message_id))
                continue
            if message.role == MessageRole.ASSISTANT:
                text = "".join(part.text for part in message.parts if isinstance(part, TextPartIR))
                if text:
                    input_items.append(
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": text}],
                        }
                    )
                    targets.append(("input", len(input_items) - 1, "content", 0))
                for part in message.parts:
                    if isinstance(part, ToolCallIR):
                        input_items.append(
                            {
                                "type": "function_call",
                                "call_id": part.call_id,
                                "name": part.name,
                                "arguments": json.dumps(thaw_json(part.arguments), ensure_ascii=False),
                            }
                        )
                spans.append(EncodedMessageSpan(message.message_id, tuple(targets)))
                continue
            content = _responses_user_content(message.parts)
            if content:
                input_items.append({"role": "user", "content": content})
                if isinstance(content[-1], Mapping):
                    targets.append(("input", len(input_items) - 1, "content", len(content) - 1))
            spans.append(EncodedMessageSpan(message.message_id, tuple(targets)))
        if not input_items:
            input_items.append({"role": "user", "content": "Continue."})

        policy = request.policy
        payload: dict[str, Any] = {
            "model": context.model_id,
            "input": input_items,
            "max_output_tokens": policy.max_output_tokens,
        }
        if policy.temperature is not None:
            payload["temperature"] = policy.temperature
        if request.tools:
            payload["tools"] = [responses_tool_definition(tool) for tool in request.tools]
            if policy.tool_choice != "omit":
                payload["tool_choice"] = policy.tool_choice
        if policy.thinking_level is not None and policy.thinking_level != ThinkingLevel.OFF:
            payload["reasoning"] = {"effort": _responses_effort(policy.thinking_level)}
        return EncodedRequest(payload, tuple(spans))

    def _new_decoder(self, context: ShapeContext) -> "OpenAIResponseDecoder":
        return OpenAIResponseDecoder(context)


class OpenAIResponseDecoder:
    def __init__(self, context: ShapeContext) -> None:
        self.context = context
        self.builder = ResponseIRBuilder(context)
        self.tool_drafts: dict[int, dict[str, Any]] = {}
        self.replay_items: dict[int, dict[str, Any]] = {}
        self.complete = False

    def feed(self, frame: _JSONFrame) -> tuple[LLMResponseUpdate, ...]:
        payload = dict(frame.payload)
        if isinstance(payload.get("output"), (list, tuple)):
            return self._feed_complete_response(payload)
        event_type = str(payload.get("type") or "").strip()
        response = payload.get("response")
        if isinstance(response, Mapping):
            usage = response.get("usage")
            if isinstance(usage, Mapping):
                self.builder.set_usage(merge_usage(self.builder.usage, usage_from_mapping(usage)))
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            self.builder.set_usage(merge_usage(self.builder.usage, usage_from_mapping(usage)))

        updates: list[LLMResponseUpdate] = []
        if event_type in {"response.output_text.delta", "response.refusal.delta"}:
            text = str(payload.get("delta") or "")
            if text:
                self.builder.append_text(text)
                self._refresh_replay()
                updates.append(self.builder.update(LLMResponseDeltaKind.TEXT, text_delta=text))
        elif event_type in {
            "response.reasoning_summary_text.delta",
            "response.reasoning_text.delta",
        }:
            text = str(payload.get("delta") or "")
            if text:
                self.builder.append_reasoning(text)
                self._refresh_replay()
                updates.append(self.builder.update(LLMResponseDeltaKind.REASONING, text_delta=text))
        elif event_type == "response.output_item.added":
            self._start_item(payload)
        elif event_type == "response.function_call_arguments.delta":
            self._append_tool_arguments(payload)
        elif event_type == "response.output_item.done":
            updates.extend(self._finish_item(payload))
        elif event_type in {"response.completed", "response.incomplete", "response.failed"}:
            if isinstance(response, Mapping) and not self.builder.parts:
                return self._feed_complete_response(dict(response))
            status_reason = (
                "length"
                if event_type == "response.incomplete"
                else "error"
                if event_type == "response.failed"
                else "tool_calls"
                if self.builder.has_tools
                else "stop"
            )
            if status_reason in {"length", "error"}:
                self._discard_open_tools()
                if status_reason == "error":
                    self.builder.discard_tool_calls()
            else:
                updates.extend(self._finalize_tools())
            self._refresh_replay()
            updates.append(self.builder.mark_complete(status_reason))
            self.complete = True
        return tuple(updates)

    def finish(self) -> LLMResponseIR:
        if not self.builder.complete:
            self._discard_open_tools()
            self.builder.mark_complete(
                LLMFinishReason.TOOL_CALLS
                if self.builder.has_tools
                else LLMFinishReason.ERROR
            )
        self._refresh_replay()
        return self.builder.finish()

    def _feed_complete_response(self, payload: dict[str, Any]) -> tuple[LLMResponseUpdate, ...]:
        updates: list[LLMResponseUpdate] = []
        output = list(payload.get("output") or [])
        status = str(payload.get("status") or "").strip().lower()
        failed = status in {"failed", "error", "cancelled", "canceled"}
        incomplete = payload.get("incomplete_details") or status == "incomplete"
        self.replay_items = {
            index: dict(item)
            for index, item in enumerate(output)
            if isinstance(item, Mapping)
        }
        for index, item in tuple(self.replay_items.items()):
            kind = str(item.get("type") or "")
            if kind == "message":
                for block in list(item.get("content") or []):
                    if not isinstance(block, Mapping):
                        continue
                    if block.get("type") in {"output_text", "refusal"}:
                        update = self.builder.append_text(str(block.get("text") or block.get("refusal") or ""))
                        if update is not None:
                            updates.append(update)
                committed = self.builder.commit_item(
                    item_id=_response_item_id(self.builder.message_id, index, item),
                    item_kind=LLMResponseItemKind.MESSAGE,
                )
                if committed is not None:
                    updates.append(committed)
                continue
            if kind == "reasoning":
                for block in list(item.get("summary") or []):
                    if isinstance(block, Mapping):
                        update = self.builder.append_reasoning(str(block.get("text") or ""))
                        if update is not None:
                            updates.append(update)
                committed = self.builder.commit_item(
                    item_id=_response_item_id(self.builder.message_id, index, item),
                    item_kind=LLMResponseItemKind.REASONING,
                )
                if committed is not None:
                    updates.append(committed)
                continue
            item_complete = not incomplete or str(item.get("status") or "").lower() == "completed"
            if kind == "function_call" and item_complete and not failed:
                call_id = str(item.get("call_id") or "").strip()
                if not call_id:
                    self.replay_items.pop(index, None)
                    continue
                tool_update = self.builder.append_tool_call(
                    call_id=call_id,
                    name=str(item.get("name") or ""),
                    arguments=json_object(
                        item.get("arguments") or {},
                        label="Responses tool arguments",
                    ),
                )
                updates.append(tool_update)
                committed = self.builder.commit_item(
                    item_id=_response_item_id(self.builder.message_id, index, item),
                    item_kind=LLMResponseItemKind.TOOL_CALL,
                    tool_call=tool_update.tool_call,
                )
                if committed is not None:
                    updates.append(committed)
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            self.builder.set_usage(usage_from_mapping(usage))
        self._refresh_replay()
        finish = (
            "error"
            if failed
            else "length"
            if incomplete
            else "tool_calls"
            if self.builder.has_tools
            else "stop"
        )
        updates.append(self.builder.mark_complete(finish))
        self.complete = True
        return tuple(updates)

    def _start_item(self, payload: Mapping[str, Any]) -> None:
        index = _output_index(payload)
        item = payload.get("item")
        if not isinstance(item, Mapping):
            return
        self.replay_items[index] = dict(item)
        if item.get("type") == "function_call":
            self.tool_drafts[index] = {
                "call_id": str(item.get("call_id") or ""),
                "name": str(item.get("name") or ""),
                "arguments": [str(item.get("arguments") or "")],
            }

    def _append_tool_arguments(self, payload: Mapping[str, Any]) -> None:
        index = _output_index(payload)
        draft = self.tool_drafts.setdefault(
            index,
            {
                "call_id": str(payload.get("call_id") or ""),
                "name": str(payload.get("name") or ""),
                "arguments": [],
            },
        )
        delta = payload.get("delta")
        if isinstance(delta, str):
            draft["arguments"].append(delta)

    def _finish_item(self, payload: Mapping[str, Any]) -> list[LLMResponseUpdate]:
        index = _output_index(payload)
        item = payload.get("item")
        if isinstance(item, Mapping):
            self.replay_items[index] = dict(item)
            if item.get("type") == "function_call":
                draft = self.tool_drafts.setdefault(index, {"arguments": []})
                draft["call_id"] = str(item.get("call_id") or draft.get("call_id") or "")
                draft["name"] = str(item.get("name") or draft.get("name") or "")
                if item.get("arguments") is not None:
                    draft["arguments"] = [str(item.get("arguments") or "")]
        kind = str(dict(item or {}).get("type") or self.replay_items.get(index, {}).get("type") or "")
        updates: list[LLMResponseUpdate] = []
        tool_call = None
        if kind == "function_call":
            tool_updates = self._finalize_tool(index)
            updates.extend(tool_updates)
            if tool_updates:
                tool_call = tool_updates[-1].tool_call
            item_kind = LLMResponseItemKind.TOOL_CALL
        elif kind == "reasoning":
            item_kind = LLMResponseItemKind.REASONING
        elif kind == "message":
            item_kind = LLMResponseItemKind.MESSAGE
        else:
            item_kind = LLMResponseItemKind.UNKNOWN
        committed = self.builder.commit_item(
            item_id=_response_item_id(
                self.builder.message_id,
                index,
                dict(item or self.replay_items.get(index) or {}),
            ),
            item_kind=item_kind,
            tool_call=tool_call,
        )
        if committed is not None:
            updates.append(committed)
        return updates

    def _finalize_tool(self, index: int) -> list[LLMResponseUpdate]:
        draft = self.tool_drafts.pop(index, None)
        if draft is None:
            return []
        call_id = str(draft.get("call_id") or "").strip()
        if not call_id:
            self.replay_items.pop(index, None)
            return []
        name = str(draft.get("name") or "").strip()
        if not name:
            raise ShapeDecodeError("Responses stream tool call has no name")
        arguments = "".join(str(value) for value in draft.get("arguments") or []) or "{}"
        return [
            self.builder.append_tool_call(
                call_id=call_id,
                name=name,
                arguments=json_object(arguments, label=f"tool {name} arguments"),
            )
        ]

    def _finalize_tools(self) -> list[LLMResponseUpdate]:
        updates: list[LLMResponseUpdate] = []
        for index in sorted(tuple(self.tool_drafts)):
            updates.extend(self._finalize_tool(index))
        return updates

    def _discard_open_tools(self) -> None:
        for index in tuple(self.tool_drafts):
            self.replay_items.pop(index, None)
        self.tool_drafts.clear()

    def _refresh_replay(self) -> None:
        if self.replay_items:
            output = [self.replay_items[index] for index in sorted(self.replay_items)]
        else:
            output = _semantic_response_items(self.builder.parts)
        self.builder.replay_payload = {"output": output}


def _responses_user_content(parts: tuple[Any, ...]) -> list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPartIR) and part.text:
            rendered.append({"type": "input_text", "text": part.text})
        elif isinstance(part, ImagePartIR):
            rendered.append({"type": "input_image", "image_url": part.source})
    return rendered


def _response_item_id(
    message_id: str,
    index: int,
    item: Mapping[str, Any],
) -> str:
    return str(item.get("id") or item.get("call_id") or f"{message_id}:{index}")


def _semantic_response_items(parts: list[Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    text = "".join(part.text for part in parts if isinstance(part, TextPartIR))
    if text:
        items.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        )
    for part in parts:
        if isinstance(part, ToolCallIR):
            items.append(
                {
                    "type": "function_call",
                    "call_id": part.call_id,
                    "name": part.name,
                    "arguments": json.dumps(thaw_json(part.arguments), ensure_ascii=False),
                }
            )
    return items


def _output_index(payload: Mapping[str, Any]) -> int:
    value = payload.get("output_index")
    return int(value) if isinstance(value, int) else 0


def _responses_effort(level: ThinkingLevel) -> str:
    return {
        ThinkingLevel.MINIMAL: "minimal",
        ThinkingLevel.LOW: "low",
        ThinkingLevel.MEDIUM: "medium",
        ThinkingLevel.HIGH: "high",
        ThinkingLevel.XHIGH: "xhigh",
        ThinkingLevel.MAX: "max",
    }.get(level, "medium")
