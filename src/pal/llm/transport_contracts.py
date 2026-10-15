"""Shared request and lifecycle contracts below the LLM JSON boundary."""
from __future__ import annotations

import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Protocol, runtime_checkable

from pal.foundation.fd_lease import FdCancellationControl, FdCapability, FdLeaseCancelledError
from pal.llm.ir import WireShape
from pal.llm.models import LLMEndpointModel
from pal.llm.shapes.base import _JSONFrame


class LLMTransportError(RuntimeError):
    pass


class LLMProviderStartedError(LLMTransportError):
    """A request failed after submission may have reached the provider."""


class LLMEndpointSpecStaleError(LLMTransportError):
    """The endpoint snapshot was retired before provider submission."""


class LLMHTTPTransportError(LLMTransportError):
    def __init__(self, payload: Mapping[str, Any], *, status: int, request_id: str = ""):
        self.payload, self.status, self.request_id = payload, status, request_id
        super().__init__(f"WebSocket upgrade rejected: HTTP {status}")


@dataclass
class LLMStreamControl:
    """LLM telemetry plus an fd capability cancellation/revocation gate."""

    started_at: float = field(default_factory=time.monotonic)
    _fd: FdCancellationControl = field(default_factory=FdCancellationControl, repr=False)
    _provider_started: bool = field(default=False, init=False, repr=False)
    _last_network_activity_at: float = field(default=0.0, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    @property
    def cancelled(self) -> bool:
        return self._fd.cancelled

    @property
    def cancel_reason(self) -> str:
        return self._fd.cancel_reason

    @property
    def provider_started(self) -> bool:
        with self._lock:
            return self._provider_started

    @property
    def last_network_activity_at(self) -> float:
        with self._lock:
            return max(self.started_at, self._last_network_activity_at)

    def bind(self, capability: FdCapability[Any]) -> None:
        self._fd.bind(capability)

    def unbind(self, capability: FdCapability[Any]) -> None:
        self._fd.unbind(capability)

    def cancel(self, reason: str = "cancelled") -> None:
        self._fd.cancel(reason)

    def raise_if_cancelled(self) -> None:
        self._fd.raise_if_cancelled()

    def mark_provider_started(self) -> None:
        with self._lock:
            self._provider_started = True
            self._last_network_activity_at = time.monotonic()

    def touch_network(self) -> None:
        self.mark_provider_started()


@dataclass(frozen=True)
class AuthenticatedTransportRequest:
    """Credential-resolved JSON request shared by HTTP and WebSocket surfaces."""

    request_id: str
    endpoint_id: str
    wire_shape: WireShape
    api_key: str
    base_url: str
    timeout_seconds: float
    payload: Mapping[str, Any]
    stream: bool
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    stream_control: LLMStreamControl | None = None
    on_submitted: Callable[[], None] | None = None


SDKTransportRequest = AuthenticatedTransportRequest
LLMStreamCancelledError = FdLeaseCancelledError


@dataclass(frozen=True)
class EncodedTransportRequest:
    """Provider-shaped request passed beneath the shared LLM pipeline."""

    request_id: str
    wire_shape: WireShape
    timeout_seconds: float
    payload: Mapping[str, Any]
    stream: bool
    extra_body: Mapping[str, Any] = field(default_factory=dict)
    stream_control: LLMStreamControl | None = None
    on_submitted: Callable[[], None] | None = None


class LLMJSONTransportPort(Protocol):
    def frames(self, endpoint: LLMEndpointModel, request: EncodedTransportRequest) -> Iterator[_JSONFrame]:
        ...


@runtime_checkable
class AuthenticatedFrameTransport(Protocol):
    def frames(self, request: AuthenticatedTransportRequest) -> Iterator[_JSONFrame]:
        ...

    def activate_endpoint(self, endpoint_id: str) -> None:
        ...

    def close(self) -> None:
        ...


@runtime_checkable
class CloseableFrameIterator(Protocol):
    def close(self) -> None:
        ...
