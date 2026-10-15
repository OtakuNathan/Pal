"""Persistent Responses WebSocket surface beneath the shared JSON pipeline.

Each request sends full context. No server-side conversation state, incremental
continuation, multiplexing, or transport-level generation retries are required.
Only a fully received successful terminal response makes a socket reusable.
"""
from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from threading import RLock
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from websockets.exceptions import InvalidStatus
from websockets.protocol import State
from websockets.sync.client import connect

from pal.foundation.fd_lease import FdCapability, FdCloseOutcome, FdLease, FdLeaseCancelledError
from pal.llm.ir import WireShape
from pal.llm.shapes.base import _JSONFrame
from pal.llm.transport_contracts import (
    AuthenticatedTransportRequest as SDKTransportRequest, LLMHTTPTransportError,
    LLMProviderStartedError, LLMTransportError,
)
from pal.shared.json_values import thaw_json


_CONNECTION_MAX_AGE = 55 * 60  # Reconnect before the provider's 60-minute limit.
_POLL_SECONDS = 0.1
_TERMINAL_EVENTS = frozenset({"response.completed", "response.failed", "response.incomplete", "error"})


def _websocket_url(base_url: str) -> str:
    parsed = urlsplit(base_url)
    scheme = {"https": "wss", "http": "ws", "wss": "wss", "ws": "ws"}.get(parsed.scheme)
    if scheme is None or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("WebSocket transport requires a base URL without credentials, query or fragment")
    path = parsed.path.rstrip("/")
    if not path.endswith("/responses"):
        path += "/responses"
    return urlunsplit((scheme, parsed.netloc, path, "", ""))


def _connect(request: SDKTransportRequest) -> Any:
    try:
        return connect(
            _websocket_url(request.base_url),
            additional_headers={"Authorization": "Bearer " + request.api_key} if request.api_key else {},
            open_timeout=min(10.0, request.timeout_seconds),
            close_timeout=1.0,
            max_size=None,
        )
    except InvalidStatus as exc:
        response = exc.response
        try:
            payload = json.loads(response.body)
        except (ValueError, TypeError, UnicodeError):
            payload = {"error": {"code": "request_failed", "message": "WebSocket upgrade rejected"}}
        if not isinstance(payload, dict):
            payload = {"error": {"code": "request_failed"}}
        raise LLMHTTPTransportError(
            payload, status=response.status_code,
            request_id=response.headers.get("x-request-id") or response.headers.get("openai-request-id", ""),
        ) from exc


@dataclass
class _SocketGraph:
    connection: Any = None


@dataclass
class _SocketEntry:
    owner: FdLease[_SocketGraph]
    created_at: float


def _close(graph: _SocketGraph) -> FdCloseOutcome:
    if graph.connection is not None:
        try:
            graph.connection.close()
        except Exception as exc:
            # Keep the uncertain graph bound to its quarantined fd owner.
            return FdCloseOutcome.uncertain(f"WebSocket close failed: {type(exc).__name__}")
        graph.connection = None
    return FdCloseOutcome.detached()


@dataclass
class ResponsesWebSocketTransport:
    connector: Callable[[SDKTransportRequest], Any] = _connect
    clock: Callable[[], float] = time.monotonic
    _connections: dict[tuple[str, ...], list[_SocketEntry]] = field(default_factory=dict, init=False, repr=False)
    _lock: RLock = field(default_factory=RLock, init=False, repr=False)

    def frames(self, request: SDKTransportRequest) -> Iterator[_JSONFrame]:
        if request.wire_shape != WireShape.OPENAI_RESPONSE:
            raise ValueError("WebSocket transport requires openai_response")
        _websocket_url(request.base_url)
        if request.timeout_seconds <= 0:
            raise ValueError("WebSocket timeout must be positive")
        payload = {**thaw_json(request.payload), **thaw_json(request.extra_body)}
        for key in ("stream", "background"):
            payload.pop(key, None)
        payload["type"] = "response.create"
        message = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        control = request.stream_control
        if control is not None:
            control.raise_if_cancelled()
        key, entry, capability = self._acquire(request)
        completed = False
        send_attempted = False
        try:
            if control is not None:
                control.bind(capability)
                control.raise_if_cancelled()
            capability.call_sync(lambda graph: self._ensure_connection(graph, request))
            if control is not None:
                control.raise_if_cancelled()
            # A failed write can have reached the provider. Do not turn an
            # ambiguous send/receive failure into a retryable connection error.
            send_attempted = True
            if control is not None:
                control.mark_provider_started()
            capability.call_sync(lambda graph: graph.connection.send(message))
            if request.on_submitted is not None:
                request.on_submitted()
            deadline = self.clock() + request.timeout_seconds
            sequence = 0
            while True:
                if control is not None:
                    control.raise_if_cancelled()
                remaining = deadline - self.clock()
                if remaining <= 0:
                    raise TimeoutError("Responses WebSocket response timed out")
                try:
                    raw = capability.call_sync(
                        lambda graph: graph.connection.recv(timeout=min(_POLL_SECONDS, remaining))
                    )
                except TimeoutError:
                    continue
                if control is not None:
                    control.touch_network()
                deadline = self.clock() + request.timeout_seconds
                event = json.loads(raw)
                if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                    raise ValueError("WebSocket frame is missing an event type")
                terminal = event["type"] in _TERMINAL_EVENTS
                completed = event["type"] == "response.completed"
                yield _JSONFrame(sequence, event)
                sequence += 1
                if terminal:
                    return
        except (LLMHTTPTransportError, FdLeaseCancelledError):
            raise
        except Exception as exc:
            if control is not None:
                control.raise_if_cancelled()
            if send_attempted:
                raise LLMProviderStartedError(
                    f"WebSocket interrupted after send began ({type(exc).__name__})."
                ) from exc
            raise LLMTransportError(f"Responses WebSocket connection failed ({type(exc).__name__})") from exc
        finally:
            try:
                capability.release_sync(reuse=completed)
            finally:
                if control is not None:
                    control.unbind(capability)
                self._settle(key, entry)

    def _ensure_connection(self, graph: _SocketGraph, request: SDKTransportRequest) -> None:
        if graph.connection is None:
            graph.connection = self.connector(request)

    def _acquire(self, request: SDKTransportRequest) -> tuple[tuple[str, ...], _SocketEntry, FdCapability[_SocketGraph]]:
        fingerprint = hashlib.sha256(request.api_key.encode()).hexdigest()
        key = (request.endpoint_id, str(request.payload.get("model") or ""), request.base_url,
               str(request.timeout_seconds), fingerprint)
        closing = []
        with self._lock:
            for old_key, entries in self._connections.items():
                for entry in entries:
                    if ((old_key[0] == key[0] and old_key != key)
                            or self.clock() - entry.created_at >= _CONNECTION_MAX_AGE):
                        if entry.owner.request_retire("connection_expired_or_reconfigured"):
                            closing.append(entry)
            self._discard_closed()
            entries = self._connections.setdefault(key, [])
            entry = None
            capability = None
            # Sequential turns prefer the most recently returned connection.
            for candidate in reversed(entries):
                if not candidate.owner.reusable:
                    continue
                lease = candidate.owner.acquire(operation_id=request.request_id)
                if lease.call_sync(lambda graph: graph.connection is None or graph.connection.state == State.OPEN):
                    entry, capability = candidate, lease
                    break
                lease.release_sync(reuse=False)
            if entry is None:
                entry = _SocketEntry(FdLease(
                    resource_kind=f"llm.responses_websocket:{request.endpoint_id}",
                    _resource=_SocketGraph(), capacity=1,
                    closer_sync=_close, hard_closer_sync=_close,
                ), self.clock())
                entries.append(entry)
                capability = entry.owner.acquire(operation_id=request.request_id)
        for old_entry in closing:
            old_entry.owner.close_sync()
        assert capability is not None
        return key, entry, capability

    def _settle(self, key: tuple[str, ...], returned: _SocketEntry) -> None:
        closing = []
        with self._lock:
            entries = self._connections.get(key, [])
            if returned in entries and returned.owner.reusable:
                entries.remove(returned)
                entries.append(returned)
            idle = [entry for entry in self._connections.get(key, []) if entry.owner.reusable]
            # Concurrent requests have exclusive sockets. Keep one idle socket
            # once they finish, rather than retaining the peak fan-out forever.
            for entry in idle[:-1]:
                if entry.owner.request_retire("excess_idle_connection"):
                    closing.append(entry)
        for entry in closing:
            entry.owner.close_sync()
        with self._lock:
            self._discard_closed()

    def _discard_closed(self) -> None:
        for key, entries in tuple(self._connections.items()):
            remaining = [entry for entry in entries if not (entry.owner.closed or entry.owner.quarantined)]
            if remaining:
                self._connections[key] = remaining
            else:
                self._connections.pop(key, None)

    def _retire(self, reason: str, endpoint_id: str | None = None) -> None:
        closing = []
        with self._lock:
            for key, entries in self._connections.items():
                if key[0] == endpoint_id:
                    continue
                for entry in entries:
                    if entry.owner.request_retire(reason):
                        closing.append(entry)
        for entry in closing:
            entry.owner.close_sync()
        with self._lock:
            self._discard_closed()

    def activate_endpoint(self, endpoint_id: str) -> None:
        self._retire("endpoint_deactivated", endpoint_id=str(endpoint_id or ""))

    def close(self) -> None:
        self._retire("transport_close")
