"""Immutable endpoint composition; credentials and connections remain renewable."""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from threading import RLock

from pal.llm.endpoint_auth import EndpointAuth
from pal.llm.chatgpt import is_chatgpt
from pal.llm.endpoint_spec import LLMEndpointSpec
from pal.llm.ir import WireShape
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeCodec, ShapeContext, _JSONFrame
from pal.llm.shapes.openai_response import OpenAIResponseCodec
from pal.llm.transport_contracts import (
    AuthenticatedFrameTransport, AuthenticatedTransportRequest, CloseableFrameIterator,
    EncodedTransportRequest,
    LLMEndpointSpecStaleError,
)
from pal.shared.json_values import freeze_json_mapping


@dataclass(frozen=True)
class EndpointCodec:
    spec: LLMEndpointSpec
    codec: ShapeCodec
    context: ShapeContext


def compile_endpoint_codec(spec: LLMEndpointSpec) -> EndpointCodec:
    spec = replace(spec, capabilities_blob=freeze_json_mapping(spec.capabilities_blob))
    shape = WireShape(spec.wire_shape)
    codec = codec_for_shape(shape)
    if isinstance(codec, OpenAIResponseCodec):
        codec = replace(codec, subscription=is_chatgpt(spec))
    return EndpointCodec(spec, codec, ShapeContext(
        wire_shape=shape, endpoint_id=spec.endpoint_id, model_id=spec.model_id,
        provider_id=spec.provider, base_url=spec.base_url, capabilities=spec.capabilities_blob,
    ))


@dataclass(frozen=True)
class LLMEndpoint(EndpointCodec):
    auth: EndpointAuth
    transport: AuthenticatedFrameTransport
    _lifetime: "_EndpointLifetime" = field(default_factory=lambda: _EndpointLifetime(), compare=False, repr=False)

    def frames(self, request: EncodedTransportRequest) -> Iterator[_JSONFrame]:
        self._lifetime.acquire()
        try:
            yield from self._frames(request)
        finally:
            if self._lifetime.release():
                self._close_transport()

    def retire(self, *, close_transport: bool = True) -> None:
        if self._lifetime.retire(close_transport=close_transport):
            self._close_transport()

    def _close_transport(self) -> None:
        if isinstance(self.transport, AuthenticatedFrameTransport):
            self.transport.close()

    def _frames(self, request: EncodedTransportRequest) -> Iterator[_JSONFrame]:
        if request.wire_shape != self.context.wire_shape:
            raise ValueError("encoded request does not match the bound endpoint shape")
        credential = self.auth.resolve()
        iterator = self.transport.frames(AuthenticatedTransportRequest(
            request_id=request.request_id, endpoint_id=self.spec.endpoint_id,
            wire_shape=self.context.wire_shape, api_key=credential, base_url=self.spec.base_url,
            timeout_seconds=request.timeout_seconds, payload=request.payload, extra_body=request.extra_body,
            stream=self.auth.force_stream or request.stream, stream_control=request.stream_control,
            on_submitted=request.on_submitted,
        ))
        try:
            for frame in iterator:
                self.auth.observe(frame)
                yield frame
        except Exception as exc:
            failure = self.auth.failure(exc)
            if failure is exc:
                raise
            raise failure from exc
        finally:
            if isinstance(iterator, CloseableFrameIterator):
                iterator.close()


@dataclass
class _EndpointLifetime:
    _active: int = 0
    _retired: bool = False
    _closed: bool = False
    _close_transport: bool = True
    _lock: RLock = field(default_factory=RLock)

    def acquire(self) -> None:
        with self._lock:
            if self._retired:
                raise LLMEndpointSpecStaleError("endpoint composition retired before submission")
            self._active += 1

    def release(self) -> bool:
        with self._lock:
            self._active -= 1
            return self._claim_close()

    def retire(self, *, close_transport: bool = True) -> bool:
        with self._lock:
            if not self._retired:
                self._close_transport = close_transport
            self._retired = True
            return self._claim_close()

    def _claim_close(self) -> bool:
        if self._retired and self._active == 0 and not self._closed:
            self._closed = True
            return self._close_transport
        return False
