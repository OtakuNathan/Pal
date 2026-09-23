from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR

import json
from dataclasses import dataclass, replace
from typing import Any, Mapping

from pal.llm.ir import (
    LLMFinishReason,
    LLMRequestIR,
    LLMResponseDeltaKind,
    LLMResponseIR,
    LLMResponseUpdate,
    MessageRole,
    PromptRegionIR,
    ReasoningPartIR,
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
    finalize_cache_spans,
    request_parameter_supported,
)
from pal.llm.shapes.builder import (
    ResponseIRBuilder,
    canonical_finish_reason,
)
from pal.llm.shapes.common import (
    json_object,
    openai_content,
    openai_tool_definition,
    role_value,
    text_content,
    tool_calls,
    tool_results,
)
from pal.shared.json_values import thaw_json


@dataclass(frozen=True)
class OpenAICompletionCodec(ShapeCodecBase):
    wire_shape: WireShape = WireShape.OPENAI_COMPLETION

    def encode(self, request: LLMRequestIR, context: ShapeContext) -> EncodedRequest:
        messages: list[dict[str, Any]] = []
        spans: list[EncodedMessageSpan] = []
        for message in request.messages:
            wire_start = len(messages)
            try:
                if (
                    message.role == MessageRole.ASSISTANT
                    and message.replay is not None
                    and message.replay.matches(
                        wire_shape=self.wire_shape,
                        endpoint_id=context.endpoint_id,
                        model_id=context.model_id,
                    )
                ):
                    replay_message = message.replay.payload.get("message")
                    replay_messages = message.replay.payload.get("messages")
                    if isinstance(replay_messages, (list, tuple)):
                        targets = []
                        for item in replay_messages:
                            messages.append(thaw_json(item))
                            targets.extend(_chat_message_cache_targets(messages, len(messages) - 1))
                        spans.append(EncodedMessageSpan(message.message_id, tuple(targets)))
                        continue
                    if isinstance(replay_message, Mapping):
                        messages.append(thaw_json(replay_message))
                        spans.append(
                            EncodedMessageSpan(
                                message.message_id,
                                _chat_message_cache_targets(
                                    messages,
                                    len(messages) - 1,
                                ),
                            )
                        )
                        continue
                if message.role == MessageRole.TOOL:
                    targets: list[tuple[str | int, ...]] = []
                    for result in tool_results(message):
                        targets.extend(
                            _append_chat_message(
                                messages,
                                {
                                    "role": "tool",
                                    "tool_call_id": result.call_id,
                                    "content": [
                                        {"type": "text", "text": result.content}
                                    ],
                                },
                            )
                        )
                    spans.append(EncodedMessageSpan(message.message_id, tuple(targets)))
                    continue
                role = _completion_message_role(message, messages, context.has_conversation_prefix)
                content = openai_content(message.parts)
                if role == "user":
                    content = _openai_user_blocks(content)
                payload: dict[str, Any] = {
                    "role": role,
                    "content": content,
                }
                calls = tool_calls(message)
                if calls:
                    payload["tool_calls"] = [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.name,
                                "arguments": json.dumps(thaw_json(call.arguments), ensure_ascii=False),
                            },
                        }
                        for call in calls
                    ]
                targets = _append_chat_message(messages, payload)
                spans.append(
                    EncodedMessageSpan(
                        message.message_id,
                        targets,
                    )
                )
            finally:
                if spans and spans[-1].message_id == message.message_id:
                    paths = tuple(("messages", i) for i in range(wire_start, len(messages)))
                    spans[-1] = replace(spans[-1], wire_item_paths=paths or spans[-1].cache_targets)
                    if message.metadata.get("continuity_part_index") == 0 and spans[-1].cache_targets:
                        spans[-1] = replace(spans[-1], continuity_target=spans[-1].cache_targets[0])
        if not messages:
            messages.append({"role": "user", "content": "Continue."})

        policy = request.policy
        payload: dict[str, Any] = {
            "model": context.model_id,
            "messages": messages,
            "max_tokens": policy.max_output_tokens,
        }
        if (
            policy.temperature is not None
            and request_parameter_supported(context, "temperature")
        ):
            payload["temperature"] = policy.temperature
        if request.tools:
            payload["tools"] = [openai_tool_definition(tool) for tool in request.tools]
            if policy.tool_choice != "omit":
                payload["tool_choice"] = policy.tool_choice
        if policy.thinking_level is not None and policy.thinking_level != ThinkingLevel.OFF:
            payload["reasoning_effort"] = policy.thinking_level.value
        return finalize_cache_spans(EncodedRequest(payload, tuple(spans)))

    def _new_decoder(self, context: ShapeContext) -> "OpenAICompletionDecoder":
        return OpenAICompletionDecoder(context)


def _openai_user_blocks(content: Any) -> list[dict[str, Any]]:
    if isinstance(content, list):
        return [dict(block) for block in content if isinstance(block, Mapping)]
    text = str(content or "")
    return [{"type": "text", "text": text}] if text else []


def _completion_message_role(
    message: Any,
    messages: list[dict[str, Any]],
    has_conversation_prefix: bool = False,
) -> str:
    if message.role != MessageRole.DEVELOPER:
        return role_value(message.role)
    # Position-sensitive projection.  In an incremental (tail-only) encode
    # the conversation prefix is absent from ``messages``; the explicit
    # boundary flag keeps the same developer message from being promoted to
    # system just because it happens to start the tail batch.
    stable_prefix = message.prompt_region == PromptRegionIR.STABLE_SYSTEM or (
        message.prompt_region != PromptRegionIR.ACTIVE_DYNAMIC
        and not has_conversation_prefix
        and not any(str(item.get("role") or "") != "system" for item in messages)
    )
    return "system" if stable_prefix else "user"


def _append_chat_message(
    messages: list[dict[str, Any]],
    payload: dict[str, Any],
) -> tuple[tuple[str | int, ...], ...]:
    role = str(payload.get("role") or "")
    content = payload.get("content")
    if (
        role == "user"
        and messages
        and messages[-1].get("role") == "user"
        and isinstance(messages[-1].get("content"), list)
        and isinstance(content, list)
    ):
        message_index = len(messages) - 1
        previous = messages[-1]["content"]
        start = len(previous)
        previous.extend(content)
        return tuple(
            ("messages", message_index, "content", index)
            for index in range(start, len(previous))
        )
    if (
        role == "system"
        and messages
        and messages[-1].get("role") == "system"
        and isinstance(messages[-1].get("content"), str)
        and isinstance(content, str)
    ):
        messages[-1]["content"] = _merge_instruction_text(
            str(messages[-1].get("content") or ""),
            content,
        )
        return (("messages", len(messages) - 1),)
    messages.append(payload)
    message_index = len(messages) - 1
    if isinstance(content, list):
        return tuple(
            ("messages", message_index, "content", index)
            for index in range(len(content))
        )
    return (("messages", message_index),) if content else ()


def _chat_message_cache_targets(
    messages: list[dict[str, Any]],
    message_index: int,
) -> tuple[tuple[str | int, ...], ...]:
    if not (0 <= message_index < len(messages)):
        return ()
    content = messages[message_index].get("content")
    if isinstance(content, str):
        return (("messages", message_index),) if content else ()
    if not isinstance(content, list):
        return ()
    supported = {"text", "image_url", "input_audio", "file", "refusal"}
    return tuple(
        ("messages", message_index, "content", index)
        for index, block in enumerate(content)
        if isinstance(block, Mapping) and str(block.get("type") or "") in supported
    )


def _merge_instruction_text(left: str, right: str) -> str:
    if left.strip() and right.strip():
        return f"{left.rstrip()}\n\n{right.lstrip()}"
    return left or right


class OpenAICompletionDecoder:
    def __init__(self, context: ShapeContext) -> None:
        self.context = context
        self.builder = ResponseIRBuilder(context)
        self.tool_drafts: dict[int, dict[str, Any]] = {}
        self.tool_calls_finalized = False
        self.native_message: dict[str, Any] = {}
        self.native_tools: dict[int, dict[str, Any]] = {}

    def feed(self, frame: _JSONFrame) -> tuple[LLMResponseUpdate, ...]:
        self.builder.observe_frame(frame)
        payload = dict(frame.payload)
        choices = list(payload.get("choices") or [])
        if not choices:
            return ()
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ShapeDecodeError("OpenAI completion choice must be an object")
        if isinstance(first.get("message"), Mapping):
            return self._feed_complete_message(dict(first["message"]), first.get("finish_reason"))
        delta = first.get("delta")
        if not isinstance(delta, Mapping):
            delta = {}
        self._capture_delta(delta)
        updates: list[LLMResponseUpdate] = []
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            self.builder.append_reasoning(reasoning)
            self._refresh_replay()
            updates.append(self.builder.update(LLMResponseDeltaKind.REASONING, text_delta=reasoning))
        content = delta.get("content")
        if isinstance(content, str) and content:
            self.builder.append_text(content)
            self._refresh_replay()
            updates.append(self.builder.update(LLMResponseDeltaKind.TEXT, text_delta=content))
        self._accumulate_tool_deltas(delta.get("tool_calls"))
        finish_reason = first.get("finish_reason")
        if finish_reason:
            reason = canonical_finish_reason(finish_reason, has_tools=bool(self.tool_drafts))
            if reason in {
                LLMFinishReason.LENGTH,
                LLMFinishReason.ERROR,
                LLMFinishReason.CONTENT_FILTER,
            }:
                self.tool_drafts.clear()
                self.tool_calls_finalized = True
                self.builder.discard_tool_calls()
            else:
                updates.extend(self._finalize_tools())
            self._refresh_replay()
            updates.append(self.builder.mark_complete(reason))
        return tuple(updates)

    def finish(self) -> LLMResponseIR:
        if not self.builder.complete:
            self.tool_drafts.clear()
            self.tool_calls_finalized = True
            self.builder.discard_tool_calls()
            self.builder.mark_complete(LLMFinishReason.ERROR)
        self._refresh_replay()
        return self.builder.finish()

    def _feed_complete_message(self, message: dict[str, Any], finish_reason: Any) -> tuple[LLMResponseUpdate, ...]:
        self.native_message = thaw_json(message)
        updates: list[LLMResponseUpdate] = []
        reason = canonical_finish_reason(finish_reason, has_tools=bool(message.get("tool_calls")))
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str) and reasoning:
            update = self.builder.append_reasoning(reasoning)
            if update is not None:
                updates.append(update)
        content = message.get("content")
        if isinstance(content, str) and content:
            update = self.builder.append_text(content)
            if update is not None:
                updates.append(update)
        elif isinstance(content, (list, tuple)):
            for item in content:
                if isinstance(item, Mapping) and item.get("type") in {"text", "output_text"}:
                    update = self.builder.append_text(str(item.get("text") or ""))
                    if update is not None:
                        updates.append(update)
        for position, item in enumerate(
            []
            if reason
            in {
                LLMFinishReason.LENGTH,
                LLMFinishReason.ERROR,
                LLMFinishReason.CONTENT_FILTER,
            }
            else list(message.get("tool_calls") or [])
        ):
            if not isinstance(item, Mapping):
                continue
            function = item.get("function")
            if not isinstance(function, Mapping):
                continue
            call_id = str(item.get("id") or "").strip()
            if not call_id:
                continue
            call = self.builder.append_tool_call(
                call_id=call_id,
                name=str(function.get("name") or ""),
                arguments=json_object(function.get("arguments") or {}, label="tool arguments"),
            )
            updates.append(call)
        self.tool_calls_finalized = True
        self._refresh_replay()
        updates.append(self.builder.mark_complete(reason))
        return tuple(updates)

    def _accumulate_tool_deltas(self, value: Any) -> None:
        for position, item in enumerate(list(value or [])):
            if not isinstance(item, Mapping):
                continue
            raw_index = item.get("index")
            index = int(raw_index) if isinstance(raw_index, int) else position
            draft = self.tool_drafts.setdefault(
                index,
                {"call_id": "", "name": "", "arguments": []},
            )
            if item.get("id"):
                draft["call_id"] = str(item["id"])
            function = item.get("function")
            if not isinstance(function, Mapping):
                continue
            name_fragment = str(function.get("name") or "")
            if name_fragment:
                current = str(draft["name"])
                if not current:
                    draft["name"] = name_fragment
                elif name_fragment.startswith(current):
                    draft["name"] = name_fragment
                elif name_fragment != current and not current.endswith(name_fragment):
                    draft["name"] = current + name_fragment
            arguments = function.get("arguments")
            if isinstance(arguments, str):
                draft["arguments"].append(arguments)
            elif isinstance(arguments, Mapping):
                draft["arguments"].append(json.dumps(thaw_json(arguments), ensure_ascii=False))

    def _finalize_tools(self) -> list[LLMResponseUpdate]:
        if self.tool_calls_finalized:
            return []
        updates: list[LLMResponseUpdate] = []
        for index in sorted(self.tool_drafts):
            draft = self.tool_drafts[index]
            call_id = str(draft.get("call_id") or "").strip()
            if not call_id:
                continue
            name = str(draft.get("name") or "").strip()
            if not name:
                raise ShapeDecodeError("OpenAI stream tool call has no name")
            raw_arguments = "".join(str(item) for item in draft.get("arguments") or []) or "{}"
            updates.append(
                self.builder.append_tool_call(
                    call_id=call_id,
                    name=name,
                    arguments=json_object(raw_arguments, label=f"tool {name} arguments"),
                )
            )
        self.tool_calls_finalized = True
        return updates

    def _capture_delta(self, delta: Mapping[str, Any]) -> None:
        # Preserve protocol values independently of the executable IR. In
        # particular arguments are a STRING, not a dict to re-serialize.
        for key, value in delta.items():
            # Null in a delta is not a replacement for accumulated content.
            # Retain an initial null (e.g. tool-only content), but don't let
            # later placeholder fields erase an accepted prefix.
            if (value is None and key in self.native_message
                    and key in {"role", "content", "reasoning_content", "reasoning",
                                "refusal", "tool_calls", "reasoning_details"}):
                continue
            if key == "tool_calls":
                for position, item in enumerate(value or ()):
                    if not isinstance(item, Mapping):
                        continue
                    index = item.get("index", position)
                    if not isinstance(index, int):
                        raise ShapeDecodeError("tool delta index must be an integer")
                    target = self.native_tools.setdefault(index, {})
                    for field, part in item.items():
                        if field == "index":
                            continue
                        if part is None and field in target:
                            continue
                        if field == "function" and isinstance(part, Mapping):
                            if not isinstance(target.get("function"), dict):
                                target["function"] = {}
                            function = target["function"]
                            for name, fragment in part.items():
                                if fragment is None and name in function:
                                    continue
                                if name == "arguments" and isinstance(fragment, str):
                                    function[name] = str(function.get(name) or "") + fragment
                                elif name == "name" and isinstance(fragment, str):
                                    current = str(function.get(name) or "")
                                    function[name] = (fragment if fragment.startswith(current)
                                                      else current if current.endswith(fragment)
                                                      else current + fragment)
                                else:
                                    function[name] = thaw_json(fragment)
                        else:
                            target[field] = thaw_json(part)
                self.native_message[key] = [self.native_tools[i] for i in sorted(self.native_tools)]
            elif key == "reasoning_details" and isinstance(value, (list, tuple)):
                # OpenRouter emits an ordered sequence of detail chunks,
                # including opaque/signature blocks. Keep every chunk;
                # replacing the array loses all but the last frame.
                previous = self.native_message.get(key) or []
                self.native_message[key] = [*previous, *thaw_json(value)]
            elif key in {"content", "reasoning_content", "reasoning", "refusal"} and isinstance(value, str):
                self.native_message[key] = str(self.native_message.get(key) or "") + value
            else:
                self.native_message[key] = thaw_json(value)

    def _refresh_replay(self) -> None:
        message = thaw_json(self.native_message)
        message.setdefault("role", "assistant")
        source = None
        if self.tool_calls_finalized and message.get("tool_calls"):
            accepted = {part.call_id for part in self.builder.parts if isinstance(part, ToolCallIR)}
            calls = [item for item in message["tool_calls"] if str(item.get("id") or "") in accepted]
            if calls != message["tool_calls"]:
                source = {"message": thaw_json(message)}
                if calls:
                    message["tool_calls"] = calls
                else:
                    message.pop("tool_calls", None)
        self.builder.replay_payload = {"message": message}
        self.builder.replay_source_payload = source
