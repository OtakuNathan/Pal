from __future__ import annotations

import json
import re
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import replace
from typing import Any

from pal.llm.ir import (
    LLMFinishReason,
    LLMMessageIR,
    LLMResponseDeltaKind,
    LLMResponseIR,
    LLMResponseItemKind,
    LLMResponseUpdate,
    MessageRole,
    MessageState,
    ReasoningPartIR,
    TextPartIR,
    ThinkingLevel,
)
from pal.llm.response_hooks import (
    ProviderResponseHookContext,
    ProviderResponseHookError,
)
from pal.shared.tool_protocol import ToolCallIR, new_tool_call


# Grammar compatibility follows DeepSeek's MIT-licensed V4 encoding parser:
# https://huggingface.co/deepseek-ai/DeepSeek-V4-Flash/blob/main/encoding/encoding_dsv4.py
_DSML_TOKEN = "｜DSML｜"
_CLOSED_TOOL_INTERACTION_PATTERN = re.compile(
    r"<closed_tool_interaction>.*?</closed_tool_interaction>",
    flags=re.DOTALL,
)
_CLEARED_TOOL_MARKER_PATTERN = re.compile(
    r"(?m)^\s*\[old tool (?:interaction|result) cleared\]\s*$"
)
_EOS_TOKEN = "<｜end▁of▁sentence｜>"
_DSML_TAG_PATTERN = re.compile(
    r"<\s*/?\s*[|｜]{1,2}\s*DSML\s*[|｜]{1,2}\s*"
    r"(?:tool_calls|invoke|parameter)\b",
    flags=re.IGNORECASE,
)
_DSML_CANONICAL_PREFIXES = tuple(
    f"<{slash}{bars}DSML{bars}{tag}"
    for slash in ("", "/")
    for bars in ("|", "||")
    for tag in ("tool_calls", "invoke", "parameter")
)
_PROJECTION_OPEN = "<closed_tool_interaction>"
_PROJECTION_CLOSE = "</closed_tool_interaction>"
_CLEARED_MARKERS = (
    "[old tool interaction cleared]",
    "[old tool result cleared]",
)


class _SafeTextGate:
    """Release ordinary text while retaining reserved-protocol prefixes.

    Shape codecs already provide the input iterator.  This gate is only the
    small cross-chunk decorator DeepSeek needs: it never owns transport or wire
    decoding, and it retains at most one possible reserved tag prefix.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._discarding_projection = False
        self.dsml_seen = False

    def feed(self, value: str, *, final: bool = False) -> str:
        if self.dsml_seen:
            return ""
        self._pending += str(value or "")
        released: list[str] = []
        while self._pending:
            if self._discarding_projection:
                close_index = self._pending.find(_PROJECTION_CLOSE)
                if close_index >= 0:
                    self._pending = self._pending[close_index + len(_PROJECTION_CLOSE) :]
                    self._discarding_projection = False
                    continue
                if final:
                    self._pending = ""
                else:
                    keep = max(0, len(_PROJECTION_CLOSE) - 1)
                    self._pending = self._pending[-keep:] if keep else ""
                break

            special_index = _first_special_index(self._pending)
            if special_index < 0:
                released.append(self._pending)
                self._pending = ""
                break
            if special_index:
                released.append(self._pending[:special_index])
                self._pending = self._pending[special_index:]

            if self._pending.startswith("<"):
                if self._pending.startswith(_PROJECTION_OPEN):
                    self._pending = self._pending[len(_PROJECTION_OPEN) :]
                    self._discarding_projection = True
                    continue
                if _PROJECTION_OPEN.startswith(self._pending):
                    if final:
                        self._pending = ""
                    break
                if _DSML_TAG_PATTERN.match(self._pending):
                    self.dsml_seen = True
                    self._pending = ""
                    break
                if _could_be_dsml_prefix(self._pending):
                    if final:
                        self.dsml_seen = True
                        self._pending = ""
                    break
                released.append(self._pending[0])
                self._pending = self._pending[1:]
                continue

            marker = next(
                (item for item in _CLEARED_MARKERS if self._pending.startswith(item)),
                None,
            )
            if marker is not None:
                self._pending = self._pending[len(marker) :]
                if self._pending.startswith("\r\n"):
                    self._pending = self._pending[2:]
                elif self._pending.startswith("\n"):
                    self._pending = self._pending[1:]
                continue
            if any(item.startswith(self._pending) for item in _CLEARED_MARKERS):
                if final:
                    self._pending = ""
                break
            released.append(self._pending[0])
            self._pending = self._pending[1:]
        return "".join(released)

    @property
    def pending_dsml(self) -> bool:
        return bool(self._pending) and _could_be_dsml_prefix(self._pending)


def normalize_deepseek_updates(
    context: ProviderResponseHookContext,
    updates: Iterable[LLMResponseUpdate],
) -> Iterator[LLMResponseUpdate]:
    """Decorate a shape-codec update iterator with DeepSeek normalization."""

    text_gate = _SafeTextGate()
    reasoning_gate = _SafeTextGate()
    recovery_prefix = _output_recovery_text_prefix(context)
    projected_parts: list[Any] = []
    native_calls: dict[str, ToolCallIR] = {}
    source_text = ""
    source_reasoning = ""
    deferred_text = ""
    structured_reasoning_seen = False
    thinking_enabled = context.request.policy.thinking_level not in {
        None,
        ThinkingLevel.OFF,
    }
    last_response: LLMResponseIR | None = None
    terminal_seen = False

    for update in updates:
        response = update.response
        last_response = response

        if update.delta_kind == LLMResponseDeltaKind.REASONING:
            structured_reasoning_seen = True
            source_reasoning += update.text_delta
            safe = reasoning_gate.feed(update.text_delta)
            if reasoning_gate.dsml_seen:
                raise ProviderResponseHookError(
                    "DeepSeek emitted textual DSML inside reasoning content"
                )
            if safe:
                _append_part(projected_parts, ReasoningPartIR(safe))
                yield _projected_update(
                    response,
                    projected_parts,
                    LLMResponseDeltaKind.REASONING,
                    text_delta=safe,
                )
            if deferred_text:
                source_text += deferred_text
                safe_text = text_gate.feed(deferred_text)
                deferred_text = ""
                if safe_text:
                    _append_part(projected_parts, TextPartIR(safe_text))
                    yield _projected_update(
                        response,
                        projected_parts,
                        LLMResponseDeltaKind.TEXT,
                        text_delta=safe_text,
                    )
            continue

        if update.delta_kind == LLMResponseDeltaKind.TEXT:
            value = update.text_delta
            if recovery_prefix:
                # The preceding response stopped inside a reserved DSML
                # envelope.  This response is only its provider continuation,
                # so no fragment is independently safe to expose.  Parse the
                # concatenated wire text at the terminal boundary instead.
                source_text += value
                continue
            if thinking_enabled and not structured_reasoning_seen:
                deferred_text += value
                if "</think>" not in deferred_text:
                    continue
                textual_reasoning, value = deferred_text.split("</think>", 1)
                deferred_text = ""
                source_text += textual_reasoning + "</think>" + value
                textual_reasoning = textual_reasoning.removeprefix("<think>")
                safe_reasoning = reasoning_gate.feed(textual_reasoning)
                if reasoning_gate.dsml_seen:
                    raise ProviderResponseHookError(
                        "DeepSeek emitted textual DSML inside reasoning content"
                    )
                if safe_reasoning:
                    _append_part(projected_parts, ReasoningPartIR(safe_reasoning))
                    yield _projected_update(
                        response,
                        projected_parts,
                        LLMResponseDeltaKind.REASONING,
                        text_delta=safe_reasoning,
                    )
            else:
                source_text += value
            safe = text_gate.feed(value)
            if safe:
                _append_part(projected_parts, TextPartIR(safe))
                yield _projected_update(
                    response,
                    projected_parts,
                    LLMResponseDeltaKind.TEXT,
                    text_delta=safe,
                )
            continue

        if update.delta_kind == LLMResponseDeltaKind.TOOL_CALL:
            call = update.tool_call
            if call is not None and call.call_id not in native_calls:
                native_calls[call.call_id] = call
                projected_parts.append(call)
                yield _projected_update(
                    response,
                    projected_parts,
                    LLMResponseDeltaKind.TOOL_CALL,
                    tool_call=call,
                )
            continue

        if update.delta_kind == LLMResponseDeltaKind.ITEM_COMMITTED:
            # Item closure is not a response-terminal signal.  In particular,
            # Anthropic-shaped responses close their text block before the
            # message_stop event that carries max_tokens.  Treating that block
            # closure as terminal would try to parse a truncated DSML envelope
            # before the hook knows it is recoverable.  Native tool items are
            # already semantic, so preserve their closure for Core; textual
            # DSML remains hidden until the actual response terminal arrives.
            if update.item_kind == LLMResponseItemKind.TOOL_CALL:
                yield LLMResponseUpdate(
                    response=_project_response(response, projected_parts),
                    delta_kind=LLMResponseDeltaKind.ITEM_COMMITTED,
                    tool_call=update.tool_call,
                    item_id=update.item_id,
                    item_kind=update.item_kind,
                )
            continue

        terminal_seen = True
        if deferred_text and response.text.startswith(source_text + deferred_text):
            # With no textual </think> delimiter, classification must wait for
            # the complete response.  The DSML parser will reject a thinking
            # response that omitted the required boundary; ordinary text is
            # released only in the final projection.
            deferred_text = ""
        source_text, text_dsml = _feed_terminal_remainder(
            response.text,
            source_text,
            text_gate,
            projected_parts,
            TextPartIR,
        )
        source_reasoning, reasoning_dsml = _feed_terminal_remainder(
            response.reasoning_text,
            source_reasoning,
            reasoning_gate,
            projected_parts,
            ReasoningPartIR,
        )
        if reasoning_dsml:
            raise ProviderResponseHookError(
                "DeepSeek emitted textual DSML inside reasoning content"
            )
        for call in response.tool_calls:
            if call.call_id not in native_calls:
                native_calls[call.call_id] = call
                projected_parts.append(call)
                yield _projected_update(
                    response,
                    projected_parts,
                    LLMResponseDeltaKind.TOOL_CALL,
                    tool_call=call,
                )

        has_dsml = bool(recovery_prefix) or text_gate.dsml_seen or text_gate.pending_dsml or text_dsml
        if has_dsml:
            if response.finish_reason == LLMFinishReason.LENGTH:
                # The incomplete DSML envelope is provider recovery state, not
                # assistant text.  Preserve its lossless wire replay for a
                # same-endpoint continuation while exposing only the already
                # safe semantic prefix to L1, channels, and final rendering.
                metadata = dict(response.message.metadata)
                metadata["preserve_replay_for_output_recovery"] = True
                hidden = _project_response(
                    response,
                    projected_parts,
                    finish_reason=LLMFinishReason.LENGTH,
                    state=MessageState.COMPLETE,
                )
                hidden = replace(
                    hidden,
                    message=replace(
                        hidden.message,
                        metadata=metadata,
                        replay=response.message.replay,
                    ),
                )
                yield LLMResponseUpdate(hidden, LLMResponseDeltaKind.STATE)
                continue
            if response.finish_reason not in {
                LLMFinishReason.STOP,
                LLMFinishReason.TOOL_CALLS,
            }:
                raise ProviderResponseHookError(
                    "DeepSeek DSML appeared in an unsuccessful provider response"
                )
            try:
                if native_calls:
                    normalized = _preserve_native_calls(
                        context,
                        response,
                        tuple(native_calls.values()),
                    )
                else:
                    parse_response = response
                    parse_text = response.text
                    if recovery_prefix:
                        parse_text = recovery_prefix + response.text
                        parse_response = replace(
                            response,
                            message=replace(
                                response.message,
                                parts=(
                                    *tuple(
                                        part
                                        for part in response.message.parts
                                        if isinstance(part, ReasoningPartIR)
                                    ),
                                    TextPartIR(parse_text),
                                ),
                            ),
                        )
                    normalized = _parse_dsml_response(
                        context,
                        parse_response,
                        _canonicalize_dsml(parse_text),
                        dsml_token=_DSML_TOKEN,
                    )
                    if recovery_prefix:
                        # Earlier safe text/reasoning is already present in
                        # the preceding response.  Keep only semantic parts
                        # produced by this continuation: its newly emitted
                        # structured reasoning and the newly closed tool
                        # items.  The replay text itself is provider-private.
                        continuation_reasoning = tuple(
                            part
                            for part in response.message.parts
                            if isinstance(part, ReasoningPartIR)
                        )
                        normalized = replace(
                            normalized,
                            message=replace(
                                normalized.message,
                                parts=(
                                    *continuation_reasoning,
                                    *normalized.tool_calls,
                                ),
                            ),
                        )
            except ProviderResponseHookError:
                raise
            except Exception as exc:
                raise ProviderResponseHookError(
                    f"DeepSeek DSML response normalization failed: {type(exc).__name__}"
                ) from exc
            if not native_calls:
                for call in normalized.tool_calls:
                    yield LLMResponseUpdate(
                        response=normalized,
                        delta_kind=LLMResponseDeltaKind.TOOL_CALL,
                        tool_call=call,
                    )
            yield LLMResponseUpdate(normalized, LLMResponseDeltaKind.STATE)
            continue

        finalized = _project_response(
            response,
            projected_parts,
            finish_reason=response.finish_reason,
            state=MessageState.COMPLETE,
        )
        if not finalized.message.parts:
            raise ProviderResponseHookError(
                "DeepSeek response contained only an echoed internal projection"
            )
        yield LLMResponseUpdate(finalized, LLMResponseDeltaKind.STATE)

    if last_response is None:
        raise ProviderResponseHookError("DeepSeek response produced no semantic updates")
    if not terminal_seen and (text_gate.dsml_seen or text_gate.pending_dsml):
        raise ProviderResponseHookError("DeepSeek DSML response ended before a terminal update")


def _output_recovery_text_prefix(
    context: ProviderResponseHookContext,
) -> str:
    """Return the contiguous hidden DSML prefix for one continuation chain."""

    messages = list(context.request.messages)
    if not messages or str(messages[-1].semantic_kind or "") != "output_continuation":
        return ""
    fragments: list[str] = []
    index = len(messages) - 1
    while index >= 0:
        message = messages[index]
        if (
            message.role == MessageRole.USER
            and str(message.semantic_kind or "") == "output_continuation"
        ):
            index -= 1
            continue
        if (
            message.role == MessageRole.ASSISTANT
            and bool(message.metadata.get("preserve_replay_for_output_recovery"))
        ):
            text = _replay_text(message)
            if not text:
                raise ProviderResponseHookError(
                    "DeepSeek output recovery replay has no textual DSML state"
                )
            fragments.append(text)
            index -= 1
            continue
        break
    return "".join(reversed(fragments))


def _replay_text(message: LLMMessageIR) -> str:
    replay = message.replay
    if replay is None:
        return ""
    payload = replay.payload
    if replay.wire_shape == "anthropic_messages":
        return "".join(
            str(block.get("text") or "")
            for block in list(payload.get("content") or [])
            if isinstance(block, Mapping) and str(block.get("type") or "") == "text"
        )
    if replay.wire_shape == "openai_completion":
        raw_message = payload.get("message")
        if isinstance(raw_message, Mapping):
            return str(raw_message.get("content") or "")
        return ""
    if replay.wire_shape == "openai_response":
        text: list[str] = []
        for item in list(payload.get("output") or []):
            if not isinstance(item, Mapping) or str(item.get("type") or "") != "message":
                continue
            for block in list(item.get("content") or []):
                if isinstance(block, Mapping) and str(block.get("type") or "") in {
                    "output_text",
                    "refusal",
                }:
                    text.append(str(block.get("text") or block.get("refusal") or ""))
        return "".join(text)
    return ""


def _parse_dsml_response(
    context: ProviderResponseHookContext,
    response: LLMResponseIR,
    text: str,
    *,
    dsml_token: str,
) -> LLMResponseIR:
    tool_calls_open = f"<{dsml_token}tool_calls>"
    tool_calls_close = f"</{dsml_token}tool_calls>"
    open_index = text.find(tool_calls_open)
    close_index = text.find(tool_calls_close, open_index + len(tool_calls_open))
    if close_index < 0:
        raise ProviderResponseHookError("DeepSeek DSML tool_calls block is not closed")
    if text.find(tool_calls_open, open_index + len(tool_calls_open)) >= 0:
        raise ProviderResponseHookError("DeepSeek DSML response has nested tool_calls blocks")

    prefix = text[:open_index]
    body = text[open_index + len(tool_calls_open) : close_index]
    suffix = text[close_index + len(tool_calls_close) :].strip()
    if suffix not in {"", _EOS_TOKEN}:
        raise ProviderResponseHookError("DeepSeek DSML response has content after tool_calls")

    existing_reasoning = tuple(
        ReasoningPartIR(
            _strip_internal_projection(part.text),
            redacted=part.redacted,
        )
        for part in response.message.parts
        if isinstance(part, ReasoningPartIR)
        and (_strip_internal_projection(part.text) or part.redacted)
    )
    textual_reasoning, content = _split_reasoning_and_content(
        context,
        prefix.rstrip(),
        has_structured_reasoning=bool(existing_reasoning),
    )
    content = _strip_internal_projection(content).strip()
    textual_reasoning = _strip_internal_projection(textual_reasoning).strip()
    calls = _parse_tool_calls(
        body,
        message_id=response.message.message_id,
        dsml_token=dsml_token,
    )
    if not calls:
        raise ProviderResponseHookError("DeepSeek DSML tool_calls block is empty")

    parts: list[Any] = list(existing_reasoning)
    if textual_reasoning:
        parts.append(ReasoningPartIR(textual_reasoning))
    if content:
        parts.append(TextPartIR(content))
    parts.extend(calls)
    message = replace(
        response.message,
        parts=tuple(parts),
        state=MessageState.COMPLETE,
        replay=None,
    )
    return replace(
        response,
        message=message,
        finish_reason=LLMFinishReason.TOOL_CALLS,
    )


def _split_reasoning_and_content(
    context: ProviderResponseHookContext,
    prefix: str,
    *,
    has_structured_reasoning: bool,
) -> tuple[str, str]:
    if "</think>" in prefix:
        reasoning, content = prefix.split("</think>", 1)
        reasoning = reasoning.removeprefix("<think>")
        if "</think>" in content or "<think>" in content:
            raise ProviderResponseHookError("DeepSeek DSML response has malformed thinking delimiters")
        return reasoning, content
    if "<think>" in prefix:
        raise ProviderResponseHookError("DeepSeek DSML response has an unclosed thinking block")
    thinking_level = context.request.policy.thinking_level
    thinking_enabled = thinking_level not in {None, ThinkingLevel.OFF}
    if thinking_enabled and prefix.strip() and not has_structured_reasoning:
        raise ProviderResponseHookError("DeepSeek thinking response is missing </think>")
    return "", prefix


def _parse_tool_calls(
    body: str,
    *,
    message_id: str,
    dsml_token: str,
) -> tuple[ToolCallIR, ...]:
    invoke_pattern = re.compile(
        rf'<{re.escape(dsml_token)}invoke\s+name="([^"]+)">\s*(.*?)\s*</{re.escape(dsml_token)}invoke>',
        flags=re.DOTALL,
    )
    calls: list[ToolCallIR] = []
    cursor = 0
    for invoke_index, match in enumerate(invoke_pattern.finditer(body)):
        if body[cursor : match.start()].strip():
            raise ProviderResponseHookError("DeepSeek DSML tool_calls contains unexpected content")
        name = str(match.group(1) or "").strip()
        if not name:
            raise ProviderResponseHookError("DeepSeek DSML tool call has no name")
        arguments = _parse_parameters(
            match.group(2),
            tool_name=name,
            dsml_token=dsml_token,
        )
        call_id = f"call_ds_{message_id.replace('-', '')}_{invoke_index}"
        calls.append(new_tool_call(call_id=call_id, name=name, arguments=arguments))
        cursor = match.end()
    if body[cursor:].strip():
        raise ProviderResponseHookError("DeepSeek DSML tool_calls contains malformed invoke content")
    return tuple(calls)


def _parse_parameters(
    body: str,
    *,
    tool_name: str,
    dsml_token: str,
) -> Mapping[str, Any]:
    parameter_pattern = re.compile(
        rf'<{re.escape(dsml_token)}parameter\s+name="([^"]+)"\s+string="(true|false)">(.*?)</{re.escape(dsml_token)}parameter>',
        flags=re.DOTALL,
    )
    arguments: dict[str, Any] = {}
    cursor = 0
    for match in parameter_pattern.finditer(body):
        if body[cursor : match.start()].strip():
            raise ProviderResponseHookError(
                f"DeepSeek DSML tool {tool_name} contains malformed parameter content"
            )
        name = str(match.group(1) or "").strip()
        if not name:
            raise ProviderResponseHookError(f"DeepSeek DSML tool {tool_name} has an unnamed parameter")
        if name in arguments:
            raise ProviderResponseHookError(
                f"DeepSeek DSML tool {tool_name} repeats parameter {name}"
            )
        raw_value = match.group(3)
        if match.group(2) == "true":
            value: Any = raw_value
        else:
            try:
                value = json.loads(raw_value)
            except json.JSONDecodeError as exc:
                raise ProviderResponseHookError(
                    f"DeepSeek DSML tool {tool_name} parameter {name} is not valid JSON"
                ) from exc
        arguments[name] = value
        cursor = match.end()
    if body[cursor:].strip():
        raise ProviderResponseHookError(
            f"DeepSeek DSML tool {tool_name} contains malformed parameter content"
        )
    return arguments


def _first_special_index(value: str) -> int:
    indexes = [index for index in (value.find("<"), value.find("[")) if index >= 0]
    return min(indexes) if indexes else -1


def _normalized_prefix(value: str) -> str:
    return "".join(str(value).replace("｜", "|").split())


def _could_be_dsml_prefix(value: str) -> bool:
    normalized = _normalized_prefix(value)
    return bool(normalized) and any(
        candidate.lower().startswith(normalized.lower())
        for candidate in _DSML_CANONICAL_PREFIXES
    )


def _canonicalize_dsml(value: str) -> str:
    pattern = re.compile(
        r"<\s*(?P<close>/?)\s*[|｜]{1,2}\s*DSML\s*[|｜]{1,2}\s*"
        r"(?P<tag>tool_calls|invoke|parameter)\b",
        flags=re.IGNORECASE,
    )
    return pattern.sub(
        lambda match: f"<{match.group('close')}{_DSML_TOKEN}{match.group('tag').lower()}",
        str(value or ""),
    )


def _append_part(parts: list[Any], part: TextPartIR | ReasoningPartIR) -> None:
    if parts and type(parts[-1]) is type(part):
        previous = parts[-1]
        if isinstance(part, TextPartIR) and isinstance(previous, TextPartIR):
            parts[-1] = TextPartIR(previous.text + part.text)
            return
        if (
            isinstance(part, ReasoningPartIR)
            and isinstance(previous, ReasoningPartIR)
            and previous.redacted == part.redacted
        ):
            parts[-1] = ReasoningPartIR(
                previous.text + part.text,
                redacted=part.redacted,
            )
            return
    parts.append(part)


def _project_response(
    response: LLMResponseIR,
    parts: list[Any] | tuple[Any, ...],
    *,
    finish_reason: LLMFinishReason | None = None,
    state: MessageState | None = None,
) -> LLMResponseIR:
    return replace(
        response,
        message=replace(
            response.message,
            parts=tuple(parts),
            state=state or response.message.state,
            replay=None,
        ),
        finish_reason=finish_reason or response.finish_reason,
    )


def _projected_update(
    response: LLMResponseIR,
    parts: list[Any],
    kind: LLMResponseDeltaKind,
    *,
    text_delta: str = "",
    tool_call: ToolCallIR | None = None,
) -> LLMResponseUpdate:
    return LLMResponseUpdate(
        response=_project_response(response, parts),
        delta_kind=kind,
        text_delta=text_delta,
        tool_call=tool_call,
    )


def _feed_terminal_remainder(
    cumulative: str,
    consumed: str,
    gate: _SafeTextGate,
    projected_parts: list[Any],
    part_type: type[TextPartIR] | type[ReasoningPartIR],
) -> tuple[str, bool]:
    if not cumulative.startswith(consumed):
        raise ProviderResponseHookError(
            "DeepSeek codec updates diverged from the terminal response"
        )
    remainder = cumulative[len(consumed) :]
    safe = gate.feed(remainder, final=True)
    if safe:
        _append_part(projected_parts, part_type(safe))
    return cumulative, gate.dsml_seen or gate.pending_dsml


def _preserve_native_calls(
    context: ProviderResponseHookContext,
    response: LLMResponseIR,
    native_calls: tuple[ToolCallIR, ...],
) -> LLMResponseIR:
    canonical = _canonicalize_dsml(response.text)
    open_tag = f"<{_DSML_TOKEN}tool_calls>"
    close_tag = f"</{_DSML_TOKEN}tool_calls>"
    open_index = canonical.find(open_tag)
    close_index = canonical.find(close_tag, open_index + len(open_tag))
    if open_index < 0 or close_index < 0:
        raise ProviderResponseHookError("DeepSeek DSML tool_calls block is not closed")
    suffix = canonical[close_index + len(close_tag) :].strip()
    if suffix not in {"", _EOS_TOKEN}:
        raise ProviderResponseHookError("DeepSeek DSML response has content after tool_calls")
    existing_reasoning = tuple(
        ReasoningPartIR(
            _strip_internal_projection(part.text),
            redacted=part.redacted,
        )
        for part in response.message.parts
        if isinstance(part, ReasoningPartIR)
        and (_strip_internal_projection(part.text) or part.redacted)
    )
    textual_reasoning, content = _split_reasoning_and_content(
        context,
        canonical[:open_index].rstrip(),
        has_structured_reasoning=bool(existing_reasoning),
    )
    parts: list[Any] = list(existing_reasoning)
    textual_reasoning = _strip_internal_projection(textual_reasoning).strip()
    content = _strip_internal_projection(content).strip()
    if textual_reasoning:
        parts.append(ReasoningPartIR(textual_reasoning))
    if content:
        parts.append(TextPartIR(content))
    parts.extend(native_calls)
    return _project_response(
        response,
        parts,
        finish_reason=LLMFinishReason.TOOL_CALLS,
        state=MessageState.COMPLETE,
    )


def _strip_internal_projection(text: str) -> str:
    stripped = _CLOSED_TOOL_INTERACTION_PATTERN.sub("", str(text or ""))
    return _CLEARED_TOOL_MARKER_PATTERN.sub("", stripped)
