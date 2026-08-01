from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Iterator
from typing import Any, Iterable, Mapping, Protocol

from pal.llm.ir import (
    LLMRequestIR,
    LLMResponseDeltaKind,
    LLMResponseIR,
    LLMResponseUpdate,
    WireShape,
)
from pal.shared.json_values import freeze_json_mapping


class ShapeDecodeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ShapeContext:
    wire_shape: WireShape
    endpoint_id: str
    model_id: str


@dataclass(frozen=True)
class EncodedRequest:
    payload: Mapping[str, Any]


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
