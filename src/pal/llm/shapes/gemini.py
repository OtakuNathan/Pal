"""Gemini Interactions wire codec (2026-05-20 API revision)."""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
from dataclasses import replace
from typing import Any
from urllib.parse import unquote_to_bytes, urlsplit

from pal.foundation.diagnostics import diagnostic_text
from pal.llm.ir import ImagePartIR, LLMFinishReason, LLMRequestIR, LLMResponseItemKind, MessageRole, PromptRegionIR, TextPartIR, WireShape
from pal.llm.replay_compatibility import accepts_replay
from pal.llm.shapes.base import (
    EncodedMessageSpan, EncodedRequest, ShapeCodecBase, ShapeContext, ShapeDecodeError,
    _JSONFrame, finalize_cache_spans, request_parameter_supported,
)
from pal.llm.shapes.builder import ResponseBlockBuilder
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolCallIR, ToolResultIR


def _content(part):
    if isinstance(part, TextPartIR):
        return {"type": "text", "text": part.text}
    if isinstance(part, ImagePartIR):
        if part.source.startswith("data:"):
            header, separator, data = part.source.partition(",")
            if not separator:
                raise ShapeDecodeError("invalid Gemini image data URI")
            media_type, *parameters = header[5:].split(";")
            if "base64" not in parameters:
                data = base64.b64encode(unquote_to_bytes(data)).decode("ascii")
            return {"type": "image", "mime_type": part.media_type or media_type, "data": data}
        mime_type = part.media_type or mimetypes.guess_type(urlsplit(part.source).path)[0]
        image = {"type": "image", "uri": part.source}
        if mime_type:
            image["mime_type"] = mime_type
        return image
    raise ShapeDecodeError("unsupported Gemini input part")


class GeminiInteractionsCodec(ShapeCodecBase):
    wire_shape = WireShape.GEMINI_INTERACTIONS

    def encode(self, request: LLMRequestIR, context: ShapeContext) -> EncodedRequest:
        steps, system, spans = [], [], []
        for message in request.messages:
            start = len(steps)
            head_instruction = message.role == MessageRole.SYSTEM or (
                message.role == MessageRole.DEVELOPER and message.prompt_region != PromptRegionIR.ACTIVE_DYNAMIC
            )
            if head_instruction and not steps and not context.has_conversation_prefix:
                system.append(message.text)
            elif message.role == MessageRole.ASSISTANT and message.replay and accepts_replay(
                message.replay, wire_shape=self.wire_shape, endpoint_id=context.endpoint_id,
                model_id=context.model_id, capabilities=context.capabilities,
            ):
                steps.extend(thaw_json(message.replay.payload["steps"]))
            elif message.role == MessageRole.ASSISTANT:
                for part in message.parts:
                    if isinstance(part, TextPartIR):
                        steps.append({"type": "model_output", "content": [_content(part)]})
                    elif isinstance(part, ToolCallIR):
                        steps.append({"type": "function_call", "id": part.call_id,
                                      "name": part.name, "arguments": thaw_json(part.arguments)})
            elif message.role == MessageRole.TOOL:
                for part in message.parts:
                    if not isinstance(part, ToolResultIR):
                        raise ShapeDecodeError("invalid Gemini tool result")
                    steps.append({"type": "function_result", "call_id": part.call_id, "name": part.name,
                                  "result": [{"type": "text", "text": part.content}], "is_error": not part.ok})
            else:
                steps.append({"type": "user_input", "content": [_content(part) for part in message.parts]})
            paths = tuple(("input", i) for i in range(start, len(steps)))
            spans.append(EncodedMessageSpan(message.message_id, cache_targets=paths, wire_item_paths=paths))
        policy = request.policy
        config = {"max_output_tokens": policy.max_output_tokens}
        thinking_active = bool(policy.thinking_level and policy.thinking_level.value != "off")
        if thinking_active:
            config["thinking_level"] = policy.thinking_level.value
        if (
            policy.temperature is not None
            and (not thinking_active or context.capabilities.get("supports_temperature") is True)
            and request_parameter_supported(context, "temperature", thinking_level=policy.thinking_level)
        ):
            config["temperature"] = policy.temperature
        payload = {"model": context.model_id, "input": steps, "generation_config": config,
                   "store": context.capabilities.get("continuation_mode") != "full"}
        if system:
            payload["system_instruction"] = "\n\n".join(system)
        if request.tools:
            payload["tools"] = [{"type": "function", "name": tool.name, "description": tool.description,
                                 "parameters": thaw_json(tool.input_schema)} for tool in request.tools]
            if policy.tool_choice != "omit":
                config["tool_choice"] = "any" if policy.tool_choice == "required" else policy.tool_choice
        result = finalize_cache_spans(EncodedRequest(payload, tuple(spans)))
        instruction_digest = hashlib.sha256(payload.get("system_instruction", "").encode()).hexdigest()
        return replace(result, message_spans=tuple(
            span if span.wire_item_paths else replace(span, cache_prefix_fingerprint=instruction_digest)
            for span in result.message_spans
        ))

    def _new_decoder(self, context: ShapeContext) -> GeminiDecoder:
        return GeminiDecoder(context)


class GeminiDecoder:
    def __init__(self, context: ShapeContext) -> None:
        self.builder = ResponseBlockBuilder(context)
        self.steps: dict[int, dict[str, Any]] = {}
        self.closed: set[int] = set()
        self.arguments: dict[int, str] = {}

    def _close(self, index: int):
        if index not in self.steps:
            raise ShapeDecodeError("Gemini step.stop without step.start")
        step = self.steps[index]
        if index in self.closed:
            raise ShapeDecodeError("duplicate Gemini step.stop")
        self.closed.add(index)
        kind = step["type"]
        item_id = step.get("id") or f"{self.builder.message_id}:{index}"
        block = None
        if kind == "function_call":
            if index in self.arguments:
                step["arguments"] = self.arguments.pop(index)
                try:
                    step["arguments"] = json.loads(step["arguments"])
                except ValueError:
                    pass  # Provider/endpoint hooks see raw arguments before shared validation.
            if not step.get("id"):
                raise ShapeDecodeError("incomplete Gemini function call")
            if "arguments" not in step:
                step["arguments"] = {}
            update = self.builder.append_tool_block(call_id=item_id, name=step.get("name", ""), arguments=step["arguments"])
            yield update
            block = update.tool_block
            item_kind = LLMResponseItemKind.TOOL_CALL
        elif kind == "thought":
            item_kind = LLMResponseItemKind.REASONING
        elif kind == "model_output":
            item_kind = LLMResponseItemKind.MESSAGE
        else:
            raise ShapeDecodeError(f"unsupported Gemini step {kind}")
        self.builder.replay_payload = {"steps": [self.steps[i] for i in sorted(self.closed)]}
        commit = self.builder.commit_item(item_id=item_id, item_kind=item_kind, tool_block=block)
        if commit:
            yield commit

    def _start(self, index: int, step: dict):
        if index in self.steps:
            raise ShapeDecodeError("duplicate Gemini step.start")
        self.steps[index] = step
        if step["type"] == "model_output":
            for part in step.get("content", ()):
                if part.get("type") == "text":
                    yield self.builder.append_text(part["text"])
        if step["type"] == "thought":
            for part in step.get("summary", ()):
                yield self.builder.append_reasoning(part.get("text", ""))

    def _delta(self, index: int, delta: dict):
        if index not in self.steps or index in self.closed:
            raise ShapeDecodeError("Gemini delta outside an open step")
        step = self.steps[index]
        kind = delta["type"]
        if kind == "text" and step["type"] == "model_output":
            parts = step.setdefault("content", [])
            if not parts or parts[-1]["type"] != "text":
                parts.append({"type": "text", "text": ""})
            parts[-1]["text"] += delta["text"]
            return self.builder.append_text(delta["text"])
        if kind == "thought_signature":
            step["signature"] = delta["signature"]
        elif kind == "thought_summary":
            part = delta["content"]
            step.setdefault("summary", []).append(part)
            return self.builder.append_reasoning(part.get("text", ""))
        elif kind == "arguments_delta" and step["type"] == "function_call":
            self.arguments[index] = self.arguments.get(index, "") + delta["arguments"]
        else:
            raise ShapeDecodeError(f"unsupported Gemini delta {kind}")
        return None

    def feed(self, frame: _JSONFrame):
        if self.builder.complete:
            raise ShapeDecodeError("unexpected Gemini frame after terminal")
        self.builder.observe_frame(frame)
        data = thaw_json(frame.payload)
        event = data.get("event_type")
        interaction = data.get("interaction", data)
        updates = []
        if event == "step.start":
            updates.extend(self._start(data["index"], data["step"]))
        elif event == "step.delta":
            updates.append(self._delta(data["index"], data["delta"]))
        elif event == "step.stop":
            updates.extend(self._close(data["index"]))
        elif event is None or event == "interaction.completed":
            status = interaction.get("status")
            if not self.steps:
                for index, step in enumerate(interaction.get("steps", ())):
                    if status == "incomplete" and step.get("type") == "function_call":
                        continue
                    updates.extend(self._start(index, step))
                    updates.extend(self._close(index))
            if status == "incomplete" and not interaction.get("errors"):
                self.builder.discard_tool_calls()
                retained = [step for _, step in sorted(self.steps.items())
                            if step["type"] != "function_call"]
                if retained:
                    self.builder.replay_payload = {"steps": retained}
                updates.append(self.builder.mark_complete(LLMFinishReason.LENGTH))
            elif status in {"completed", "requires_action"} and self.steps.keys() == self.closed:
                updates.append(self.builder.mark_complete())
            else:
                raise ShapeDecodeError(f"Gemini interaction ended with status {status!r} or unclosed steps")
        elif event == "error":
            error = data.get("error") or {}
            raise ShapeDecodeError(diagnostic_text(error.get("message") or error.get("code") or "Gemini stream failed", limit=500))
        elif event not in {"interaction.created", "interaction.status_update"}:
            raise ShapeDecodeError(f"unexpected Gemini event {event}")
        return tuple(update for update in updates if update is not None)

    def finish(self):
        if not self.builder.complete:
            raise ShapeDecodeError("Gemini stream ended before terminal interaction")
        if self.builder.finish_reason == LLMFinishReason.LENGTH:
            # The cap can arrive before any text or complete function call.
            return self.builder.snapshot()
        return self.builder.finish()
