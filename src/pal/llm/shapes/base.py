from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from collections.abc import Iterator
from typing import Any, Iterable, Mapping, Protocol, TypeAlias

from pal.llm.ir import (
    LLMRequestIR,
    LLMResponseDeltaKind,
    LLMResponseIR,
    LLMResponseUpdate,
    WireShape,
)
from pal.shared.json_values import freeze_json_mapping, thaw_json


class ShapeDecodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShapeContext:
    wire_shape: WireShape
    endpoint_id: str
    model_id: str
    provider_id: str = ""
    base_url: str = ""
    capabilities: Mapping[str, Any] = field(default_factory=dict)


JSONPathPart: TypeAlias = str | int


@dataclass(frozen=True)
class EncodedMessageSpan:
    message_id: str
    cache_targets: tuple[tuple[JSONPathPart, ...], ...] = ()
    cache_prefix_fingerprint: str = ""
    estimated_cache_prefix_tokens: int = 0


@dataclass(frozen=True)
class EncodedRequest:
    payload: Mapping[str, Any]
    message_spans: tuple[EncodedMessageSpan, ...] = ()
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    applied_cache_breakpoint_message_ids: tuple[str, ...] = ()


def finalize_cache_spans(encoded: EncodedRequest) -> EncodedRequest:
    """Describe the exact provider prefix ending at each span's last target."""

    payload = thaw_json(encoded.payload)
    spans: list[EncodedMessageSpan] = []
    for span in encoded.message_spans:
        if not span.cache_targets:
            spans.append(span)
            continue
        prefix = _provider_prefix(payload, span.cache_targets[-1])
        if prefix is None:
            spans.append(span)
            continue
        serialized = json.dumps(
            prefix,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        spans.append(
            EncodedMessageSpan(
                message_id=span.message_id,
                cache_targets=span.cache_targets,
                cache_prefix_fingerprint=hashlib.sha256(
                    serialized.encode("utf-8")
                ).hexdigest(),
                estimated_cache_prefix_tokens=max(1, (len(serialized) + 3) // 4),
            )
        )
    return EncodedRequest(
        payload=encoded.payload,
        message_spans=tuple(spans),
        extra_body=encoded.extra_body,
        applied_cache_breakpoint_message_ids=(
            encoded.applied_cache_breakpoint_message_ids
        ),
    )


def _provider_prefix(
    payload: dict[str, Any],
    path: tuple[JSONPathPart, ...],
) -> dict[str, Any] | None:
    if len(path) < 2 or path[0] not in {"input", "messages", "system"}:
        return None
    root = str(path[0])
    try:
        root_index = int(path[1])
    except (TypeError, ValueError):
        return None
    sequence = payload.get(root)
    if not isinstance(sequence, list) or not (0 <= root_index < len(sequence)):
        return None

    # Output budget and sampling settings do not change the rendered input
    # prefix. Cache policy/routing fields are injected after this description.
    prefix = {
        key: thaw_json(value)
        for key, value in payload.items()
        if key not in {"max_tokens", "max_output_tokens", "temperature"}
    }
    prefix_sequence = thaw_json(sequence[: root_index + 1])
    if len(path) >= 4 and path[2] in {"content", "output"}:
        try:
            content_index = int(path[3])
            item = prefix_sequence[-1]
            content_key = str(path[2])
            content = item.get(content_key) if isinstance(item, dict) else None
            if not isinstance(content, list) or not (
                0 <= content_index < len(content)
            ):
                return None
            item[content_key] = content[: content_index + 1]
        except (TypeError, ValueError):
            return None
    prefix[root] = prefix_sequence
    if root == "system":
        prefix.pop("messages", None)
    return prefix


@dataclass(frozen=True)
class _JSONFrame:
    """Private transport-to-codec frame; never crosses the codec boundary."""

    sequence: int
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("JSON frame sequence must be non-negative")
        if not isinstance(self.payload, Mapping):
            raise TypeError("JSON frame payload must be an object")
        object.__setattr__(self, "payload", freeze_json_mapping(self.payload))


class _ShapeDecoder(Protocol):
    def feed(self, frame: _JSONFrame) -> tuple[LLMResponseUpdate, ...]:
        ...

    def finish(self) -> LLMResponseIR:
        ...


class ShapeCodec(Protocol):
    wire_shape: WireShape

    def encode(self, request: LLMRequestIR, context: ShapeContext) -> EncodedRequest:
        ...

    def decode(
        self,
        frames: Iterable[_JSONFrame],
        context: ShapeContext,
    ) -> Iterator[LLMResponseUpdate]:
        ...


def request_parameter_supported(
    context: ShapeContext,
    parameter: str,
) -> bool:
    """Return whether an endpoint accepts one optional generation parameter."""

    normalized = str(parameter or "").strip().lower()
    if not normalized:
        return False
    explicit = context.capabilities.get(f"supports_{normalized}")
    if isinstance(explicit, bool):
        return explicit
    unsupported = context.capabilities.get("unsupported_request_parameters", ())
    if isinstance(unsupported, str):
        unsupported = (unsupported,)
    if not isinstance(unsupported, (list, tuple, set, frozenset)):
        return True
    return normalized not in {
        str(item or "").strip().lower()
        for item in unsupported
        if str(item or "").strip()
    }


class ShapeCodecBase:
    """Internal iterator codec shared by single-shot and streaming transports."""

    def _new_decoder(self, context: ShapeContext) -> _ShapeDecoder:
        raise NotImplementedError

    def decode(
        self,
        frames: Iterable[_JSONFrame],
        context: ShapeContext,
    ) -> Iterator[LLMResponseUpdate]:
        decoder = self._new_decoder(context)
        terminal_seen = False
        last_response: LLMResponseIR | None = None
        for frame in frames:
            for update in decoder.feed(frame):
                if update.delta_kind == LLMResponseDeltaKind.STATE:
                    terminal_seen = True
                last_response = update.response
                yield update
        final = decoder.finish()
        if not terminal_seen or last_response != final:
            yield LLMResponseUpdate(final, delta_kind=LLMResponseDeltaKind.STATE)
