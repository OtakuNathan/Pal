from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

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
    ShapeCodecBase,
    ShapeContext,
    ShapeDecodeError,
    _JSONFrame,
)
from pal.llm.shapes.builder import ResponseIRBuilder, canonical_finish_reason, merge_usage, usage_from_mapping
from pal.llm.shapes.common import anthropic_tool_definition, json_object, tool_results
from pal.shared.json_values import thaw_json


@dataclass(frozen=True)
class AnthropicMessagesCodec(ShapeCodecBase):
    wire_shape: WireShape = WireShape.ANTHROPIC_MESSAGES

    def encode(self, request: LLMRequestIR, context: ShapeContext) -> EncodedRequest:
        system_parts: list[str] = []
        messages: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role in {MessageRole.SYSTEM, MessageRole.DEVELOPER}:
                text = "".join(part.text for part in message.parts if isinstance(part, TextPartIR))
                if text:
                    system_parts.append(text)
                continue
            if message.role == MessageRole.TOOL:
                blocks = [
                    {
                        "type": "tool_result",
                        "tool_use_id": result.call_id,
                        "content": result.content,
                        "is_error": not result.ok,
                    }
                    for result in tool_results(message)
                ]
                if blocks:
                    _append_message(messages, "user", blocks)
                continue
            if (
                message.role == MessageRole.ASSISTANT
                and message.replay is not None
                and message.replay.matches(
                    wire_shape=self.wire_shape,
                    endpoint_id=context.endpoint_id,
                    model_id=context.model_id,
                )
            ):
                content = message.replay.payload.get("content")
                if isinstance(content, (list, tuple)):
                    _append_message(messages, "assistant", [thaw_json(item) for item in content if isinstance(item, Mapping)])
                    continue
            if message.role == MessageRole.ASSISTANT:
                blocks: list[dict[str, Any]] = []
                # Replay is the authoritative provider wire form.  Keep a
                # lossless semantic fallback for an active turn restored from
                # an older checkpoint (or after an endpoint/model switch)
                # where the envelope is unavailable but reasoning is still in
                # the IR.  Omitting this block while thinking mode is enabled
                # makes Anthropic reject the next request before it reaches
                # the model.
                for part in message.parts:
                    if isinstance(part, ReasoningPartIR):
                        blocks.append(
                            {
                                "type": "redacted_thinking" if part.redacted else "thinking",
                                **({} if part.redacted else {"thinking": part.text}),
                            }
                        )
                text = "".join(part.text for part in message.parts if isinstance(part, TextPartIR))
                if text:
                    blocks.append({"type": "text", "text": text})
                for part in message.parts:
                    if isinstance(part, ToolCallIR):
                        blocks.append(
                            {
                                "type": "tool_use",
                                "id": part.call_id,
                                "name": part.name,
                                "input": thaw_json(part.arguments),
                            }
                        )
                if blocks:
                    _append_message(messages, "assistant", blocks)
                continue
            blocks = _anthropic_user_content(message.parts)
            if blocks:
                _append_message(messages, "user", blocks)
        if not messages:
            messages.append({"role": "user", "content": "Continue."})

        policy = request.policy
        payload: dict[str, Any] = {
            "model": context.model_id,
            "messages": messages,
            "max_tokens": policy.max_output_tokens,
        }
        if system_parts:
            payload["system"] = "\n\n".join(system_parts)
        if policy.temperature is not None:
            payload["temperature"] = policy.temperature
        if request.tools:
            payload["tools"] = [anthropic_tool_definition(tool) for tool in request.tools]
            if policy.tool_choice not in {"", "auto", "omit"}:
                payload["tool_choice"] = {"type": policy.tool_choice}
        if policy.thinking_level is not None and policy.thinking_level != ThinkingLevel.OFF:
            if policy.thinking_budget_tokens is not None:
                upper = int(policy.max_output_tokens) - 1
                if upper >= 1024:
                    payload["thinking"] = {
                        "type": "enabled",
                        "budget_tokens": min(max(1024, int(policy.thinking_budget_tokens)), upper),
                    }
            else:
                payload["thinking"] = {"type": "adaptive"}
        return EncodedRequest(payload)

    def _new_decoder(self, context: ShapeContext) -> "AnthropicMessagesDecoder":
        return AnthropicMessagesDecoder(context)


class AnthropicMessagesDecoder:
    def __init__(self, context: ShapeContext) -> None:
        self.context = context
        self.builder = ResponseIRBuilder(context)
        self.blocks: dict[int, dict[str, Any]] = {}
        self.tool_drafts: dict[int, dict[str, Any]] = {}
        self.complete = False

    def feed(self, frame: _JSONFrame) -> tuple[LLMResponseUpdate, ...]:
        payload = dict(frame.payload)
        if isinstance(payload.get("content"), (list, tuple)):
            return self._feed_complete_message(payload)
        event_type = str(payload.get("type") or "")
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            self.builder.set_usage(merge_usage(self.builder.usage, usage_from_mapping(usage)))
        message = payload.get("message")
        if isinstance(message, Mapping) and isinstance(message.get("usage"), Mapping):
            self.builder.set_usage(merge_usage(self.builder.usage, usage_from_mapping(message["usage"])))

        updates: list[LLMResponseUpdate] = []
        if event_type == "content_block_start":
            self._start_block(payload)
        elif event_type == "content_block_delta":
            updates.extend(self._append_delta(payload))
        elif event_type == "content_block_stop":
            updates.extend(self._finish_block(_block_index(payload)))
        elif event_type == "message_delta":
            delta = payload.get("delta")
            stop_reason = delta.get("stop_reason") if isinstance(delta, Mapping) else None
            if stop_reason:
                reason = canonical_finish_reason(stop_reason, has_tools=self.builder.has_tools)
                if reason in {
                    LLMFinishReason.LENGTH,
                    LLMFinishReason.ERROR,
                    LLMFinishReason.CONTENT_FILTER,
                }:
                    self._discard_open_tools()
                    if reason in {
                        LLMFinishReason.ERROR,
                        LLMFinishReason.CONTENT_FILTER,
                    }:
                        self.builder.discard_tool_calls()
                else:
                    updates.extend(self._finalize_tools())
                self._refresh_replay()
                updates.append(self.builder.mark_complete(reason))
                self.complete = True
        elif event_type == "message_stop":
            updates.extend(self._finalize_tools())
            self._refresh_replay()
            if not self.complete:
                updates.append(self.builder.mark_complete("tool_calls" if self.builder.has_tools else "stop"))
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

    def _feed_complete_message(self, payload: Mapping[str, Any]) -> tuple[LLMResponseUpdate, ...]:
        updates: list[LLMResponseUpdate] = []
        content = list(payload.get("content") or [])
        reason = canonical_finish_reason(payload.get("stop_reason") or "stop", has_tools=False)
        self.blocks = {index: dict(block) for index, block in enumerate(content) if isinstance(block, Mapping)}
        for index in sorted(self.blocks):
            block = self.blocks[index]
            block_type = str(block.get("type") or "")
            if block_type == "text":
                update = self.builder.append_text(str(block.get("text") or ""))
            elif block_type == "thinking":
                update = self.builder.append_reasoning(str(block.get("thinking") or ""))
            elif block_type == "redacted_thinking":
                update = self.builder.append_reasoning("", redacted=True)
            elif block_type == "tool_use" and reason not in {
                LLMFinishReason.ERROR,
                LLMFinishReason.CONTENT_FILTER,
            }:
                call_id = str(block.get("id") or "").strip()
                if not call_id:
                    self.blocks.pop(index, None)
                    continue
                update = self.builder.append_tool_call(
                    call_id=call_id,
                    name=str(block.get("name") or ""),
                    arguments=json_object(block.get("input") or {}, label="Anthropic tool input"),
                )
            else:
                update = None
            if update is not None:
                updates.append(update)
                item_kind = (
                    LLMResponseItemKind.TOOL_CALL
                    if block_type == "tool_use"
                    else LLMResponseItemKind.REASONING
                    if block_type in {"thinking", "redacted_thinking"}
                    else LLMResponseItemKind.MESSAGE
                )
                committed = self.builder.commit_item(
                    item_id=str(block.get("id") or f"{self.builder.message_id}:{index}"),
                    item_kind=item_kind,
                    tool_call=update.tool_call,
                )
                if committed is not None:
                    updates.append(committed)
        usage = payload.get("usage")
        if isinstance(usage, Mapping):
            self.builder.set_usage(usage_from_mapping(usage))
        self._refresh_replay()
        updates.append(self.builder.mark_complete(canonical_finish_reason(reason, has_tools=self.builder.has_tools)))
        self.complete = True
        return tuple(updates)

    def _start_block(self, payload: Mapping[str, Any]) -> None:
        index = _block_index(payload)
        block = payload.get("content_block")
        if not isinstance(block, Mapping):
            return
        self.blocks[index] = dict(block)
        if block.get("type") == "tool_use":
            self.tool_drafts[index] = {
                "id": str(block.get("id") or ""),
                "name": str(block.get("name") or ""),
                "input_json": [],
            }

    def _append_delta(self, payload: Mapping[str, Any]) -> list[LLMResponseUpdate]:
        index = _block_index(payload)
        delta = payload.get("delta")
        if not isinstance(delta, Mapping):
            return []
        delta_type = str(delta.get("type") or "")
        block = self.blocks.setdefault(index, {})
        if delta_type == "text_delta":
            text = str(delta.get("text") or "")
            block["type"] = "text"
            block["text"] = str(block.get("text") or "") + text
            update = self.builder.append_text(text)
            self._refresh_replay()
            return [update] if update is not None else []
        if delta_type == "thinking_delta":
            text = str(delta.get("thinking") or "")
            block["type"] = "thinking"
            block["thinking"] = str(block.get("thinking") or "") + text
            update = self.builder.append_reasoning(text)
            self._refresh_replay()
            return [update] if update is not None else []
        if delta_type == "signature_delta":
            block["signature"] = str(block.get("signature") or "") + str(delta.get("signature") or "")
            self._refresh_replay()
            return []
        if delta_type == "input_json_delta":
            draft = self.tool_drafts.setdefault(index, {"id": "", "name": "", "input_json": []})
            draft["input_json"].append(str(delta.get("partial_json") or ""))
        return []

    def _finish_block(self, index: int) -> list[LLMResponseUpdate]:
        draft = self.tool_drafts.get(index)
        if draft is None:
            block = dict(self.blocks.get(index) or {})
            kind = str(block.get("type") or "")
            committed = self.builder.commit_item(
                item_id=str(block.get("id") or f"{self.builder.message_id}:{index}"),
                item_kind=(
                    LLMResponseItemKind.REASONING
                    if kind in {"thinking", "redacted_thinking"}
                    else LLMResponseItemKind.MESSAGE
                    if kind == "text"
                    else LLMResponseItemKind.UNKNOWN
                ),
            )
            return [committed] if committed is not None else []
        if not str(draft.get("id") or "").strip():
            self.tool_drafts.pop(index, None)
            self.blocks.pop(index, None)
            return []
        name = str(draft.get("name") or "").strip()
        if not name:
            raise ShapeDecodeError("Anthropic stream tool call has no name")
        raw = "".join(str(item) for item in draft.get("input_json") or []) or "{}"
        arguments = json_object(raw, label=f"tool {name} input")
        self.tool_drafts.pop(index, None)
        self.blocks[index] = {
            "type": "tool_use",
            "id": str(draft["id"]),
            "name": name,
            "input": thaw_json(arguments),
        }
        tool_update = self.builder.append_tool_call(
            call_id=str(draft["id"]),
            name=name,
            arguments=dict(arguments),
        )
        committed = self.builder.commit_item(
            item_id=str(draft["id"]),
            item_kind=LLMResponseItemKind.TOOL_CALL,
            tool_call=tool_update.tool_call,
        )
        return [
            tool_update,
            *([committed] if committed is not None else []),
        ]

    def _finalize_tools(self) -> list[LLMResponseUpdate]:
        updates: list[LLMResponseUpdate] = []
        for index in sorted(tuple(self.tool_drafts)):
            draft = self.tool_drafts.pop(index)
            call_id = str(draft.get("id") or "").strip()
            if not call_id:
                self.blocks.pop(index, None)
                continue
            name = str(draft.get("name") or "").strip()
            if not name:
                raise ShapeDecodeError("Anthropic stream tool call has no name")
            arguments = draft.get("parsed_input")
            if not isinstance(arguments, Mapping):
                raw = "".join(str(item) for item in draft.get("input_json") or []) or "{}"
                arguments = json_object(raw, label=f"tool {name} input")
            self.blocks[index] = {
                "type": "tool_use",
                "id": call_id,
                "name": name,
                "input": thaw_json(arguments),
            }
            updates.append(
                self.builder.append_tool_call(
                    call_id=call_id,
                    name=name,
                    arguments=dict(arguments),
                )
            )
            committed = self.builder.commit_item(
                item_id=call_id,
                item_kind=LLMResponseItemKind.TOOL_CALL,
                tool_call=updates[-1].tool_call,
            )
            if committed is not None:
                updates.append(committed)
        return updates

    def _discard_open_tools(self) -> None:
        for index in tuple(self.tool_drafts):
            self.blocks.pop(index, None)
        self.tool_drafts.clear()

    def _refresh_replay(self) -> None:
        self.builder.replay_payload = {"content": [self.blocks[index] for index in sorted(self.blocks)]}


def _append_message(messages: list[dict[str, Any]], role: str, content: Any) -> None:
    if messages and messages[-1].get("role") == role:
        previous = messages[-1].get("content")
        if isinstance(previous, (list, tuple)) and isinstance(content, (list, tuple)):
            previous.extend(content)
            return
        if isinstance(previous, str) and isinstance(content, str):
            messages[-1]["content"] = previous + "\n\n" + content
            return
    messages.append({"role": role, "content": content})


def _anthropic_user_content(parts: tuple[Any, ...]) -> str | list[dict[str, Any]]:
    rendered: list[dict[str, Any]] = []
    for part in parts:
        if isinstance(part, TextPartIR) and part.text:
            rendered.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePartIR):
            if part.source.startswith("data:") and ";base64," in part.source:
                header, data = part.source.split(",", 1)
                media_type = part.media_type or header.removeprefix("data:").split(";", 1)[0]
                rendered.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}})
            else:
                rendered.append({"type": "image", "source": {"type": "url", "url": part.source}})
    if len(rendered) == 1 and rendered[0].get("type") == "text":
        return str(rendered[0]["text"])
    return rendered


def _block_index(payload: Mapping[str, Any]) -> int:
    value = payload.get("index")
    return int(value) if isinstance(value, int) else 0
