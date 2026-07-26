"""Public declarations for the WebSocket sidecar process (review_guarded).

Responsibility: run the WebSocket transport as a dedicated process that bridges
WebSocket text frames to the existing socket channel protocol and back, using
the ``websockets`` library as the SOLE WebSocket wire implementation.

This file declares the public contract surface only. Transport control flow,
reconnect/backoff scheduling, framing, and the socket-channel client sessions
are private implementation owned by the Coder. The formal connection state
machine is recorded in architecture.yaml ``websocket_sidecar``.

Invariants (frozen by ``bridge_protocol``):
* The socket channel is the sole local Pal Channel; WebSocket peers reuse its
  request/response message shapes without adding a new wire message kind,
  envelope, control protocol, or semantics.
* Inbound ordinary messages reach the runtime only through the existing socket
  channel user-message path (equivalent to ``pal client --message``).
* Active requests are correlated by the existing request id. Reasoning,
  tool-call progress, and intermediate model-round text are never surfaced as
  extra messages.
* Slash-prefixed and control-plane-like inbound frames are rejected at the
  boundary and never reach the slash-command dispatcher.
* ``websockets`` is the only WebSocket wire implementation.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import enum
import importlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.channel.endpoints.socket_protocol import read_socket_message
from pal.channel.providers.websocket_bridge.protocol import (
    REJECTION_REASON_CONTROL_PLANE,
    REJECTION_REASON_SLASH,
    SLASH_PREFIX,
)
from pal.foundation.sidecar import (
    SidecarEndpoint,
    cleanup_sidecar_endpoint,
    dispatch_sidecar_request,
    handle_sidecar_client,
    start_sidecar_server,
)
from pal.tty.session import SocketSession

logger = logging.getLogger(__name__)

# The runtime directory under ``<runtime_root>/data/`` that hosts the manager
# socket. The provider connects to ``<runtime_root>/data/<SIDECAR_NAME>/manager.sock``.
SIDECAR_NAME = "websocket_bridge"

# The websockets library is the sole WebSocket wire implementation and is
# imported lazily so this module stays importable without the dependency
# installed (it is formally declared in pyproject.toml). aiohttp and stdlib
# reimplementation are NOT used.


# --------------------------------------------------------------------------- #
# Functional core: boundary classification (no I/O, fully testable)           #
# --------------------------------------------------------------------------- #

# A non-slash inbound frame whose decoded JSON body forges a socket-protocol
# ``slash_command`` control frame is control-plane-like and is rejected. This is
# the only control-plane forgery vector: the reused client->server delivery path
# classifies a frame's socket-protocol type solely from its text (slash prefix ->
# ``slash_command``), so only ``slash_command`` is a control-plane type an
# inbound frame could try to impersonate. Plain JSON user content is delivered.
_CONTROL_PLANE_FORGED_TYPES = frozenset({"slash_command"})
_SOCKET_RESPONSE_TYPES = frozenset(
    {
        "reasoning_delta",
        "text_delta",
        "op_tool_call",
        "llm_done",
        "llm_error",
        "done",
        "error",
        "attachment",
    }
)


@dataclass(frozen=True)
class InboundClassification:
    """Outcome of classifying one inbound WebSocket text frame."""

    deliverable: bool
    reason: str | None


def _looks_control_plane(text: str) -> bool:
    candidate = text.strip()
    if not candidate.startswith("{"):
        return False
    try:
        decoded = json.loads(candidate)
    except (ValueError, TypeError):
        return False
    if not isinstance(decoded, dict):
        return False
    return str(decoded.get("type") or "") in _CONTROL_PLANE_FORGED_TYPES


def _decode_protocol_payload(text: str) -> dict[str, Any] | None:
    candidate = text.strip()
    if not candidate.startswith("{"):
        return None
    try:
        decoded = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    if not isinstance(decoded, dict):
        return None
    payload_type = str(decoded.get("type") or "")
    if payload_type not in {"user_message", "slash_command", *_SOCKET_RESPONSE_TYPES}:
        return None
    return decoded


def classify_inbound(text: str) -> InboundClassification:
    """Classify one ordinary inbound WebSocket message text.

    The hard invariant is the slash prefix: any frame whose first non-whitespace
    character is ``SLASH_PREFIX`` is rejected (this also defeats whitespace
    obfuscation of a slash command). A frame forging a ``slash_command`` control
    envelope is rejected as control-plane-like. A blank frame is neither
    deliverable nor a named rejection; it is simply dropped.
    """
    if not isinstance(text, str):
        return InboundClassification(False, REJECTION_REASON_CONTROL_PLANE)
    if text.lstrip().startswith(SLASH_PREFIX):
        return InboundClassification(False, REJECTION_REASON_SLASH)
    if _looks_control_plane(text):
        return InboundClassification(False, REJECTION_REASON_CONTROL_PLANE)
    if not text.strip():
        return InboundClassification(False, None)
    return InboundClassification(True, None)


def is_deliverable_inbound(text: str) -> bool:
    """Return True only for an ordinary, non-slash inbound frame text."""
    return classify_inbound(text).deliverable


def rejection_reason(text: str) -> str | None:
    """Return the boundary rejection reason, or None when the text is deliverable."""
    return classify_inbound(text).reason


class BridgeBoundary:
    """Concrete bridge boundary classifier satisfying :class:`BridgeAdaptation`."""

    def is_deliverable_inbound(self, text: str) -> bool:
        return is_deliverable_inbound(text)

    def rejection_reason(self, text: str) -> str | None:
        return rejection_reason(text)


# --------------------------------------------------------------------------- #
# Functional core: reconnect backoff (no I/O)                                 #
# --------------------------------------------------------------------------- #


def compute_backoff_delay(attempt: int, initial: float, maximum: float) -> float:
    """Exponential reconnect delay (seconds), capped at ``maximum``.

    ``attempt`` is the zero-based count of consecutive failures: the first retry
    waits ``initial``, then ``2*initial``, ``4*initial``, ... until the cap. A
    non-positive ``initial`` disables waiting.
    """
    if initial <= 0:
        return 0.0
    exponent = max(0, int(attempt))
    delay = float(initial) * (2 ** exponent)
    cap = max(float(maximum), 0.0)
    return float(min(delay, cap))


# --------------------------------------------------------------------------- #
# Functional core: connection state machine (no I/O)                          #
# --------------------------------------------------------------------------- #


class SidecarConnectionState(str, enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    RECONNECT_BACKOFF = "reconnect_backoff"
    SHUTTING_DOWN = "shutting_down"


class ConnectionStateEvent(str, enum.Enum):
    START = "start"
    CONNECTION_READY = "connection_ready"
    CONNECTION_FAILED = "connection_failed"
    PEER_DISCONNECTED = "peer_disconnected"
    BACKOFF_ELAPSED = "backoff_elapsed"
    SHUTDOWN_REQUESTED = "shutdown_requested"
    TERMINATED = "terminated"


_ALLOWED_TRANSITIONS: dict[
    tuple[SidecarConnectionState, ConnectionStateEvent], SidecarConnectionState
] = {
    (SidecarConnectionState.DISCONNECTED, ConnectionStateEvent.START): SidecarConnectionState.CONNECTING,
    (SidecarConnectionState.CONNECTING, ConnectionStateEvent.CONNECTION_READY): SidecarConnectionState.CONNECTED,
    (SidecarConnectionState.CONNECTING, ConnectionStateEvent.CONNECTION_FAILED): SidecarConnectionState.RECONNECT_BACKOFF,
    (SidecarConnectionState.CONNECTED, ConnectionStateEvent.PEER_DISCONNECTED): SidecarConnectionState.RECONNECT_BACKOFF,
    (SidecarConnectionState.CONNECTED, ConnectionStateEvent.SHUTDOWN_REQUESTED): SidecarConnectionState.SHUTTING_DOWN,
    (SidecarConnectionState.RECONNECT_BACKOFF, ConnectionStateEvent.BACKOFF_ELAPSED): SidecarConnectionState.CONNECTING,
    (SidecarConnectionState.RECONNECT_BACKOFF, ConnectionStateEvent.SHUTDOWN_REQUESTED): SidecarConnectionState.SHUTTING_DOWN,
    (SidecarConnectionState.SHUTTING_DOWN, ConnectionStateEvent.TERMINATED): SidecarConnectionState.DISCONNECTED,
}


class InvalidStateTransition(RuntimeError):
    """Raised when a connection state transition is not permitted by the contract."""


def advance_state(
    current: SidecarConnectionState | str,
    event: ConnectionStateEvent | str,
) -> SidecarConnectionState:
    """Apply one permitted state-machine transition or raise.

    The transition table is the frozen ``websocket_sidecar`` connection state
    machine (architecture.yaml). This is a pure decision: it validates and
    returns the next state without any I/O or side effect.
    """
    key = (SidecarConnectionState(current), ConnectionStateEvent(event))
    target = _ALLOWED_TRANSITIONS.get(key)
    if target is None:
        raise InvalidStateTransition(
            f"invalid websocket sidecar transition: {key[0].value} --{key[1].value}-->"
        )
    return target


# --------------------------------------------------------------------------- #
# Imperative shell: runtime, transport, manager socket                        #
# --------------------------------------------------------------------------- #

_RUNTIME: _SidecarRuntime | None = None

# The peer id of the inbound frame currently being delivered, scoped per async
# task so concurrent peer deliveries never cross. Used by ``deliver_inbound`` to
# bind the socket-channel reply stream back to the originating peer.
_current_peer: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "websocket_sidecar_current_peer", default=None
)


def _manager_endpoint(runtime_root: Path) -> SidecarEndpoint:
    return SidecarEndpoint(runtime_root=Path(runtime_root), name=SIDECAR_NAME)


def _load_websockets():
    """Import and return the sole WebSocket wire implementation (``websockets``)."""
    return importlib.import_module("websockets")


def _transient_exception_types(websockets_module: Any) -> tuple[type[BaseException], ...]:
    """Collect the transient WebSocket/connection exception types for reconnect."""
    collected: list[type[BaseException]] = [OSError]
    exceptions = getattr(websockets_module, "exceptions", None)
    if exceptions is not None:
        for name in (
            "ConnectionClosed",
            "ConnectionClosedOK",
            "ConnectionClosedError",
            "WebSocketException",
            "InvalidHandshake",
            "InvalidStatus",
            "InvalidURI",
        ):
            cls = getattr(exceptions, name, None)
            if isinstance(cls, type) and issubclass(cls, BaseException):
                collected.append(cls)
    return tuple(collected)


@dataclass
class _PeerConnection:
    websocket: Any
    peer_id: str
    closed: bool = False

    async def send(self, text: str) -> None:
        if self.closed:
            return
        await self.websocket.send(text)


@dataclass
class _PendingRequest:
    peer_id: str
    future: asyncio.Future[dict[str, Any]]
    current_text_parts: list[str] = field(default_factory=list)
    error_text: str = ""


@dataclass
class _SidecarRuntime:
    """Mutable transport state for one sidecar process (single active instance)."""

    config: SidecarConfig
    state: SidecarConnectionState = SidecarConnectionState.DISCONNECTED
    listener_bound: bool = False
    last_error: str = ""
    rejection_count: int = 0
    peers: dict[str, _PeerConnection] = field(default_factory=dict)
    deliveries: dict[str, str] = field(default_factory=dict)  # request_id -> peer_id
    pending_requests: dict[str, _PendingRequest] = field(default_factory=dict)
    manager_server: Any = None
    manager_endpoint_info: dict[str, Any] = field(default_factory=dict)
    shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _ws: Any = field(default=None, repr=False)
    _ws_transient: tuple[type[BaseException], ...] = field(default=(), repr=False)

    @property
    def process_running(self) -> bool:
        return not self.shutdown_event.is_set()

    # ---- state machine ----
    def _advance(self, event: ConnectionStateEvent | str) -> SidecarConnectionState:
        self.state = advance_state(self.state, event)
        return self.state

    def _safe_advance(self, event: ConnectionStateEvent | str) -> SidecarConnectionState:
        with contextlib.suppress(InvalidStateTransition):
            return self._advance(event)
        return self.state

    # ---- manager socket RPC ----
    async def _handle_manager_client(self, reader: Any, writer: Any) -> None:
        await handle_sidecar_client(reader, writer, self._dispatch)

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        return await dispatch_sidecar_request(
            request,
            self._call_method,
            error_kind=lambda exc: "timeout" if isinstance(exc, TimeoutError) else "sidecar",
            logger=logger,
        )

    async def _call_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method in {"health", "status"}:
            return self._health_payload()
        if method == "send_message":
            return await self._send_message(str(params.get("message") or ""))
        if method == "shutdown":
            await self._begin_shutdown()
            return {"ok": True}
        raise ValueError(f"unknown websocket sidecar method: {method}")

    async def _send_message(self, message: str) -> dict[str, Any]:
        classification = classify_inbound(message)
        if not classification.deliverable:
            reason = classification.reason or "blank_message_rejected"
            raise ValueError(reason)
        active_peers = [peer for peer in self.peers.values() if not peer.closed]
        if not active_peers:
            raise RuntimeError("websocket peer is not connected")
        if len(active_peers) != 1:
            raise RuntimeError("websocket endpoint has multiple connected peers")
        peer = active_peers[0]
        request_id = str(uuid4())
        pending = _PendingRequest(
            peer_id=peer.peer_id,
            future=asyncio.get_running_loop().create_future(),
        )
        self.pending_requests[request_id] = pending
        frame = {
            "type": "user_message",
            "request_id": request_id,
            "text": message,
        }
        try:
            await peer.send(json.dumps(frame, ensure_ascii=False, separators=(",", ":")))
            result = await asyncio.wait_for(
                asyncio.shield(pending.future),
                timeout=max(float(self.config.message_timeout_seconds), 0.001),
            )
        except asyncio.TimeoutError as exc:
            raise TimeoutError(
                f"peer response timed out after {self.config.message_timeout_seconds:.3g}s"
            ) from exc
        finally:
            self.pending_requests.pop(request_id, None)
            if not pending.future.done():
                pending.future.cancel()
        return {
            "message_id": request_id,
            "response": str(result.get("response") or ""),
            "finish_reason": str(result.get("finish_reason") or ""),
        }

    async def _begin_shutdown(self) -> None:
        self._safe_advance(ConnectionStateEvent.SHUTDOWN_REQUESTED)
        self.shutdown_event.set()
        self._fail_pending_requests("websocket bridge is shutting down")
        # Unblock any active peer frame loops so a connected transport shuts down
        # promptly rather than waiting for the next inbound frame.
        for peer in list(self.peers.values()):
            peer.closed = True
        for peer in list(self.peers.values()):
            with contextlib.suppress(Exception):
                await peer.websocket.close()

    def _health_payload(self) -> dict[str, Any]:
        return {
            "ok": self.process_running,
            "process_running": self.process_running,
            "listener_bound": self.listener_bound,
            "connected_peers": len(self.peers),
            "state": self.state.value,
            "last_error": self.last_error,
            "rejection_count": self.rejection_count,
            "mode": "outbound" if self.config.peer_url else "inbound",
            "peer_url": self.config.peer_url or "",
            "bind_host": self.config.bind_host,
            "bind_port": self.config.bind_port,
            **dict(self.manager_endpoint_info),
        }

    # ---- delivery / reply routing ----
    def _register_delivery(self, request_id: str, peer_id: str) -> None:
        self.deliveries[request_id] = peer_id

    def _unregister_delivery(self, request_id: str) -> None:
        self.deliveries.pop(request_id, None)

    def _peer_for_reply(self, request_id: str) -> _PeerConnection | None:
        peer_id = self.deliveries.get(request_id)
        if peer_id is None:
            return None
        return self.peers.get(peer_id)

    def _report_rejection(self, reason: str, text: str) -> None:
        self.rejection_count += 1
        preview = text.strip()[:80]
        self.last_error = f"{reason}: {preview}" if preview else reason
        logger.warning("websocket bridge rejected inbound frame (%s): %r", reason, preview)

    def _route_response(self, payload: dict[str, Any]) -> bool:
        request_id = str(payload.get("request_id") or "")
        pending = self.pending_requests.get(request_id)
        if pending is None:
            return False
        payload_type = str(payload.get("type") or "")
        if payload_type == "text_delta":
            text = str(payload.get("text") or "")
            if text:
                pending.current_text_parts.append(text)
        elif payload_type in {"llm_error", "error"}:
            pending.error_text = str(
                payload.get("error_text") or payload.get("text") or "remote Pal failed"
            )
        terminal = payload_type in {"done", "error"}
        if payload_type == "llm_done":
            finish_reason = str(payload.get("finish_reason") or "")
            if finish_reason == "tool_calls":
                # Text emitted before a tool call belongs to an intermediate
                # model round. It is transport progress, not the peer's final
                # reply, so do not surface it as another message.
                pending.current_text_parts.clear()
                return True
            terminal = bool(finish_reason and finish_reason != "tool_calls")
        if not terminal or pending.future.done():
            return True
        if pending.error_text:
            pending.future.set_exception(RuntimeError(pending.error_text))
        else:
            pending.future.set_result(
                {
                    "response": "".join(pending.current_text_parts),
                    "finish_reason": str(payload.get("finish_reason") or "stop"),
                }
            )
        return True

    def _fail_pending_for_peer(self, peer_id: str, reason: str) -> None:
        for pending in list(self.pending_requests.values()):
            if pending.peer_id == peer_id and not pending.future.done():
                pending.future.set_exception(ConnectionError(reason))

    def _fail_pending_requests(self, reason: str) -> None:
        for pending in list(self.pending_requests.values()):
            if not pending.future.done():
                pending.future.set_exception(ConnectionError(reason))

    # ---- lifecycle ----
    async def run(self) -> None:
        self.manager_server, self.manager_endpoint_info = await start_sidecar_server(
            _manager_endpoint(self.config.runtime_root), self._handle_manager_client
        )
        try:
            if self.config.peer_url:
                await self._run_outbound()
            else:
                await self._run_inbound()
        finally:
            await self._cleanup()

    async def _run_inbound(self) -> None:
        websockets = self._ws
        self._advance(ConnectionStateEvent.START)
        server = await websockets.serve(
            self._handle_peer,
            self.config.bind_host,
            self.config.bind_port,
        )
        self.listener_bound = True
        self._advance(ConnectionStateEvent.CONNECTION_READY)
        logger.info(
            "websocket bridge sidecar inbound listener bound on %s:%s",
            self.config.bind_host,
            self.config.bind_port,
        )
        try:
            await self.shutdown_event.wait()
        finally:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
            self.listener_bound = False

    async def _run_outbound(self) -> None:
        websockets = self._ws
        self._advance(ConnectionStateEvent.START)
        attempt = 0
        while True:
            if self.shutdown_event.is_set():
                return
            connection = None
            try:
                connection = await websockets.connect(self.config.peer_url)
            except self._ws_transient as exc:
                self.last_error = f"connection failed: {exc.__class__.__name__}: {exc}"
                self._safe_advance(ConnectionStateEvent.CONNECTION_FAILED)
                if not await self._backoff_wait(attempt):
                    return
                attempt += 1
                self._safe_advance(ConnectionStateEvent.BACKOFF_ELAPSED)
                continue

            attempt = 0
            self.last_error = ""
            self._safe_advance(ConnectionStateEvent.CONNECTION_READY)
            peer_url = self.config.peer_url
            assert peer_url is not None  # _run_outbound is only reached when peer_url is set
            peer = _PeerConnection(connection, peer_url)
            self.listener_bound = True
            async with self._lock:
                self.peers[peer.peer_id] = peer
            logger.info("websocket bridge sidecar connected to peer %s", self.config.peer_url)
            try:
                await self._process_peer_frames(connection, peer.peer_id)
            except self._ws_transient as exc:
                self.last_error = f"peer disconnected: {exc.__class__.__name__}: {exc}"
            finally:
                peer.closed = True
                self._fail_pending_for_peer(peer.peer_id, "websocket peer disconnected")
                async with self._lock:
                    self.peers.pop(peer.peer_id, None)
                self.listener_bound = False
                with contextlib.suppress(Exception):
                    await connection.close()
            if self.shutdown_event.is_set():
                return
            self._safe_advance(ConnectionStateEvent.PEER_DISCONNECTED)
            if not await self._backoff_wait(0):
                return
            self._safe_advance(ConnectionStateEvent.BACKOFF_ELAPSED)

    async def _backoff_wait(self, attempt: int) -> bool:
        """Wait one reconnect backoff, interruptible by shutdown.

        Returns True when the backoff elapsed (caller should retry), False when a
        shutdown was requested during the wait.
        """
        if self.shutdown_event.is_set():
            return False
        delay = compute_backoff_delay(
            attempt,
            self.config.reconnect_initial_delay_seconds,
            self.config.reconnect_max_delay_seconds,
        )
        if delay <= 0:
            return not self.shutdown_event.is_set()
        try:
            await asyncio.wait_for(self.shutdown_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return True
        return False

    async def _handle_peer(self, websocket: Any) -> None:
        peer_id = str(uuid4())
        peer = _PeerConnection(websocket, peer_id)
        async with self._lock:
            self.peers[peer_id] = peer
        logger.info("websocket bridge peer connected (%d active)", len(self.peers))
        try:
            await self._process_peer_frames(websocket, peer_id)
        except self._ws_transient as exc:
            logger.debug("websocket bridge peer %s disconnected: %s", peer_id, exc)
        finally:
            peer.closed = True
            self._fail_pending_for_peer(peer.peer_id, "websocket peer disconnected")
            async with self._lock:
                self.peers.pop(peer_id, None)

    async def _process_peer_frames(self, websocket: Any, peer_id: str) -> None:
        async for raw in websocket:
            text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8", "replace")
            protocol_payload = _decode_protocol_payload(text)
            inbound_text = text
            request_id: str | None = None
            if protocol_payload is not None:
                payload_type = str(protocol_payload.get("type") or "")
                if payload_type == "user_message":
                    inbound_text = str(protocol_payload.get("text") or "")
                    request_id = str(protocol_payload.get("request_id") or "") or None
                elif payload_type == "slash_command":
                    self._report_rejection(REJECTION_REASON_SLASH, text)
                    continue
                elif payload_type in _SOCKET_RESPONSE_TYPES:
                    if not self._route_response(protocol_payload):
                        self.last_error = (
                            "orphan websocket response: "
                            f"{payload_type}:{str(protocol_payload.get('request_id') or '')}"
                        )
                    continue
            classification = classify_inbound(inbound_text)
            if not classification.deliverable:
                if classification.reason is not None:
                    self._report_rejection(classification.reason, inbound_text)
                continue
            token = _current_peer.set(peer_id)
            try:
                await deliver_inbound(
                    inbound_text,
                    config=self.config,
                    request_id=request_id,
                )
            except Exception as exc:  # transient delivery failure: report, keep bridging
                self.last_error = f"delivery failed: {exc.__class__.__name__}: {exc}"
                logger.exception("websocket bridge delivery failed")
            finally:
                _current_peer.reset(token)

    async def _cleanup(self) -> None:
        self._safe_advance(ConnectionStateEvent.TERMINATED)
        for peer in list(self.peers.values()):
            peer.closed = True
        for peer in list(self.peers.values()):
            with contextlib.suppress(Exception):
                await peer.websocket.close()
        self.peers.clear()
        self.deliveries.clear()
        self._fail_pending_requests("websocket bridge stopped")
        self.pending_requests.clear()
        self.listener_bound = False
        if self.manager_server is not None:
            self.manager_server.close()
            with contextlib.suppress(Exception):
                await self.manager_server.wait_closed()
            self.manager_server = None
        with contextlib.suppress(Exception):
            await cleanup_sidecar_endpoint(_manager_endpoint(self.config.runtime_root))


@dataclass(frozen=True)
class SidecarConfig:
    """Declarative configuration for one bridge sidecar process.

    ``socket_channel_path`` is the existing local socket channel (pal.sock) and
    remains the sole message ingress. ``bind_host``/``bind_port`` expose the
    inbound WebSocket listener on the trusted LAN; ``peer_url`` is the optional
    outbound peer bridge URL. Reconnect tuning reuses socket-channel delivery
    semantics where possible.
    """

    runtime_root: Path
    socket_channel_path: Path
    bind_host: str = "0.0.0.0"
    bind_port: int = 0
    peer_url: str | None = None
    reconnect_initial_delay_seconds: float = 1.0
    reconnect_max_delay_seconds: float = 30.0
    message_timeout_seconds: float = 3000.0
    binding_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SidecarHealth:
    """Observable sidecar health surfaced to the owning provider."""

    process_running: bool = False
    listener_bound: bool = False
    connected_peers: int = 0
    last_error: str = ""


async def serve(config: SidecarConfig) -> None:
    """Run the sidecar until shut down: serve/connect, bridge, reconnect.

    Implements the connection lifecycle (connect, reconnect with backoff, clean
    disconnect, error reporting). Private implementation is deferred.
    """
    global _RUNTIME
    websockets = _load_websockets()
    runtime = _SidecarRuntime(config=config)
    runtime._ws = websockets
    runtime._ws_transient = _transient_exception_types(websockets)
    _RUNTIME = runtime
    try:
        await runtime.run()
    finally:
        if _RUNTIME is runtime:
            _RUNTIME = None


async def deliver_inbound(
    text: str,
    *,
    config: SidecarConfig,
    request_id: str | None = None,
) -> None:
    """Deliver one ordinary inbound message into the existing socket channel
    user-message path (SocketSession send to pal.sock). Slash-prefixed or
    control-plane-like text MUST be rejected before this path is reached.
    """
    classification = classify_inbound(text)
    if not classification.deliverable:
        # Defense in depth: the boundary rejects upstream. If a non-deliverable
        # frame reaches here it is never forwarded; a named rejection is reported.
        runtime = _RUNTIME
        if runtime is not None and classification.reason is not None:
            runtime._report_rejection(classification.reason, text)
        return

    runtime = _RUNTIME
    peer_id = _current_peer.get()
    session: SocketSession | None = None
    active_request_id = ""
    try:
        reader, writer = await asyncio.open_unix_connection(str(config.socket_channel_path))
        session = SocketSession(
            reader,
            writer,
            read_message=read_socket_message,
            request_id_factory=lambda: request_id or str(uuid4()),
        )
        active_request_id = await session.send(text)
        if runtime is not None and peer_id is not None:
            runtime._register_delivery(active_request_id, peer_id)
        async for payload in session.stream_response(active_request_id):
            await forward_reply(payload, config=config)
    finally:
        if runtime is not None and active_request_id:
            runtime._unregister_delivery(active_request_id)
        if session is not None:
            await session.aclose()


async def forward_reply(payload: dict[str, Any], *, config: SidecarConfig) -> None:
    """Forward one existing socket channel reply frame back to the peer over
    WebSocket. Reuses the socket protocol message shape; adds no new envelope.
    """
    runtime = _RUNTIME
    if runtime is None:
        return
    if not isinstance(payload, dict):
        return
    request_id = str(payload.get("request_id") or "")
    peer = runtime._peer_for_reply(request_id)
    if peer is None:
        return
    # Reuse the socket protocol message shape verbatim, serialized as text with
    # no added wrapper/envelope so the peer sees the existing frame as-is.
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    await peer.send(text)


def health_snapshot() -> SidecarHealth:
    """Return the current observable sidecar health."""
    runtime = _RUNTIME
    if runtime is None:
        return SidecarHealth(
            process_running=False,
            listener_bound=False,
            connected_peers=0,
            last_error="sidecar not running",
        )
    return SidecarHealth(
        process_running=runtime.process_running,
        listener_bound=runtime.listener_bound,
        connected_peers=len(runtime.peers),
        last_error=runtime.last_error,
    )


__all__ = [
    "SidecarConfig",
    "SidecarHealth",
    "serve",
    "deliver_inbound",
    "forward_reply",
    "health_snapshot",
]
