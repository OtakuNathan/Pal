"""Endpoint composition and compatibility exports for the JSON transport boundary."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from threading import RLock
from typing import Any

from pal.llm.chatgpt import is_chatgpt
from pal.llm.endpoint_auth import ChatGPTEndpointAuth, EndpointAuth
from pal.llm.endpoint_components import LLMEndpoint, compile_endpoint_codec
from pal.llm.endpoint_spec import LLMEndpointSpec, endpoint_spec_fingerprint
from pal.llm.http_transport import DefaultSDKClientFactory, SDKClientFactory, SDKJSONTransport
from pal.llm.models import LLMEndpointModel
from pal.llm.request_contracts import RequestSubmission
from pal.llm.shapes.base import _JSONFrame
from pal.llm.transport_contracts import (
    AuthenticatedFrameTransport, AuthenticatedTransportRequest, CloseableFrameIterator,
    EncodedTransportRequest, LLMEndpointSpecStaleError, LLMJSONTransportPort, LLMProviderStartedError,
    LLMStreamCancelledError, LLMStreamControl, LLMTransportError, SDKTransportRequest,
)
from pal.llm.websocket_transport import ResponsesWebSocketTransport


@dataclass
class DirectEndpointTransport:
    """Compile configured endpoints once; inference executes their fixed components."""

    credential_resolver: Callable[[LLMEndpointSpec], str | None]
    # Optional complete transports remain injectable for SDK/loopback tests.
    sdk_transport: Any = None
    websocket_transport: AuthenticatedFrameTransport | None = None
    _endpoints: dict[str, LLMEndpoint] = field(default_factory=dict, init=False, repr=False)
    _fingerprints: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def bind(self, endpoint: LLMEndpointModel | LLMEndpointSpec) -> LLMEndpoint:
        spec = LLMEndpointSpec.from_value(endpoint)
        fingerprint = endpoint_spec_fingerprint(spec)
        previous = None
        with self._lock:
            if self._fingerprints.get(spec.endpoint_id) == fingerprint:
                return self._endpoints[spec.endpoint_id]
            compiled = compile_endpoint_codec(spec)
            # Access profile selects credential/failure policy, never transport.
            auth_type = ChatGPTEndpointAuth if is_chatgpt(spec) else EndpointAuth
            auth = auth_type(compiled.spec, self.credential_resolver)
            if spec.transport == "websocket":
                port = self.websocket_transport or ResponsesWebSocketTransport()
            else:
                port = self.sdk_transport or SDKJSONTransport()
            bound = LLMEndpoint(compiled.spec, compiled.codec, compiled.context, auth, port)
            previous = self._endpoints.get(spec.endpoint_id)
            self._endpoints[spec.endpoint_id] = bound
            self._fingerprints[spec.endpoint_id] = fingerprint
        if previous is not None:
            previous.retire(close_transport=previous.transport is not bound.transport)
        return bound

    def prepare_endpoints(self, endpoints: Iterable[LLMEndpointModel | LLMEndpointSpec]) -> None:
        specs = tuple(LLMEndpointSpec.from_value(endpoint) for endpoint in endpoints)
        for spec in specs:
            self.bind(spec)
        live = {spec.endpoint_id for spec in specs}
        retired = []
        with self._lock:
            for key in tuple(self._endpoints):
                if key not in live:
                    retired.append(self._endpoints.pop(key))
                    self._fingerprints.pop(key, None)
            retained_ports = {id(endpoint.transport) for endpoint in self._endpoints.values()}
        for endpoint in retired:
            endpoint.retire(close_transport=id(endpoint.transport) not in retained_ports)

    def frames(self, endpoint: LLMEndpointModel, request: EncodedTransportRequest) -> Iterator[_JSONFrame]:
        yield from self.bind(endpoint).frames(request)

    def resume_subscription(self, endpoint: LLMEndpointModel) -> None:
        self.bind(endpoint).auth.resume()

    def refresh_credentials(self) -> bool:
        owner = getattr(self.credential_resolver, "__self__", None)
        refresh = getattr(owner, "refresh", None)
        if not callable(refresh):
            return False
        refresh()
        self.close()
        return True

    def _ports(self) -> tuple[AuthenticatedFrameTransport, ...]:
        with self._lock:
            ports = {id(endpoint.transport): endpoint.transport for endpoint in self._endpoints.values()}
            for port in (self.sdk_transport, self.websocket_transport):
                if isinstance(port, AuthenticatedFrameTransport):
                    ports[id(port)] = port
        return tuple(port for port in ports.values() if isinstance(port, AuthenticatedFrameTransport))

    def activate_endpoint(self, endpoint_id: str) -> None:
        for port in self._ports():
            port.activate_endpoint(endpoint_id)
        # Preserve the audited lifecycle shim for legacy injected SDK clients.
        if not isinstance(self.sdk_transport, AuthenticatedFrameTransport):
            activate = getattr(self.sdk_transport, "activate_endpoint", None)
            if callable(activate):
                activate(endpoint_id)

    def close(self) -> None:
        with self._lock:
            endpoints = tuple(self._endpoints.values())
            self._endpoints.clear()
            self._fingerprints.clear()
        for endpoint in endpoints:
            endpoint.retire()
        bound_ports = {id(endpoint.transport) for endpoint in endpoints}
        for port in (self.sdk_transport, self.websocket_transport):
            if id(port) not in bound_ports and isinstance(port, AuthenticatedFrameTransport):
                port.close()
        if not isinstance(self.sdk_transport, AuthenticatedFrameTransport):
            close = getattr(self.sdk_transport, "close", None)
            if callable(close):
                close()


# Compatibility name for existing callers; composition is transport-neutral.
DirectSDKTransport = DirectEndpointTransport
