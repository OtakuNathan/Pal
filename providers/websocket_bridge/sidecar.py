"""Public declarations for the WebSocket sidecar process (review_guarded).

Responsibility: run the WebSocket transport as a dedicated process that bridges
bounded peer exchanges to the provider-private local channel socket, using the
``websockets`` library as the sole WebSocket wire implementation.

This file declares the public contract surface only. Transport control flow,
reconnect/backoff scheduling, framing, and the socket-channel client sessions
are private implementation owned by the Coder. The formal connection state
machine is recorded in architecture.yaml ``websocket_sidecar``.

Invariants (frozen by ``bridge_protocol``):
* Peer traffic never enters the ordinary TTY ``pal.sock``.
* Active sends are fire-and-forget: completion means the WebSocket transport
  accepted the root frame.
* Only the final local Pal round can become the next peer message.
* ``[[peer_end]]`` and the ninth-message boundary terminate and clear an
  exchange without emitting another frame.
* Slash-prefixed and control-plane-like inbound frames are rejected at the
  boundary and never reach the slash-command dispatcher.
* ``websockets`` is the only WebSocket wire implementation.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import importlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.channel.endpoints.socket_protocol import read_socket_message
try:
    from .protocol import (
        InvalidPeerContext,
        MAX_PEER_MESSAGE_COUNT,
        PEER_END_SENTINEL,
        PeerExchangeContext,
        REJECTION_REASON_CONTROL_PLANE,
        REJECTION_REASON_PEER_CONTEXT,
        REJECTION_REASON_SLASH,
        SLASH_PREFIX,
        parse_peer_context,
    )
except ImportError:
    # The production sidecar imports this module directly from its runtime-root
    # provider directory, outside a Python package.
    from protocol import (  # type: ignore[no-redef]
        InvalidPeerContext,
        MAX_PEER_MESSAGE_COUNT,
        PEER_END_SENTINEL,
        PeerExchangeContext,
        REJECTION_REASON_CONTROL_PLANE,
        REJECTION_REASON_PEER_CONTEXT,
        REJECTION_REASON_SLASH,
        SLASH_PREFIX,
        parse_peer_context,
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


def _encode_peer_message(
    text: str,
    *,
    context: PeerExchangeContext,
    request_id: str | None = None,
) -> str:
    frame = {
        "type": "user_message",
        "request_id": str(request_id or uuid4()),
        "text": str(text),
        "peer_context": context.to_wire(),
    }
    return json.dumps(frame, ensure_ascii=False, separators=(",", ":"))


def _context_for_inbound(payload: dict[str, Any] | None) -> PeerExchangeContext:
    if payload is None or "peer_context" not in payload:
        raise InvalidPeerContext("peer_context is required")
    return parse_peer_context(payload.get("peer_context"))


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


def _manager_endpoint(config: SidecarConfig) -> SidecarEndpoint:
    data_root = (
        Path(config.data_root)
        if config.data_root is not None
        else Path(config.bridge_socket_path).parent
    )
    return SidecarEndpoint(
        runtime_root=Path(config.runtime_root),
        name=SIDECAR_NAME,
        runtime_dir_override=data_root,
    )


def _load_websockets():
    """Import and return the sole WebSocket wire implementation (``websockets``)."""
    return importlib.import_module("websockets")


def _transient_exception_types(websockets_module: Any) -> tuple[type[BaseException], ...]:
    """Collect the transient WebSocket/connection exception types for reconnect."""
    collected: list[type[BaseException]] = [OSError]
    exceptions = getattr(websockets_module, "exceptions", None)
    if exceptions is None:
        # websockets 14.x doesn't expose ``exceptions`` on the package until the
        # submodule has been imported. Without this explicit import, an ordinary
        # ConnectionClosed escapes the reconnect loop and kills the sidecar.
        module_name = str(getattr(websockets_module, "__name__", "") or "websockets")
        try:
            exceptions = importlib.import_module(f"{module_name}.exceptions")
        except (ImportError, AttributeError):
            exceptions = None
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
class _ResponseStream:
    """Incremental local-Pal response text for one peer message."""

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
    sentinel_drop_count: int = 0
    limit_drop_count: int = 0
    legacy_response_drop_count: int = 0
    active_exchange_id: str = ""
    active_message_count: int = 0
    peers: dict[str, _PeerConnection] = field(default_factory=dict)
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
        if message == PEER_END_SENTINEL:
            self._terminate_exchange("sentinel")
            raise ValueError("peer_end_is_not_a_root_message")
        active_peers = [peer for peer in self.peers.values() if not peer.closed]
        if not active_peers:
            raise RuntimeError("websocket peer is not connected")
        if len(active_peers) != 1:
            raise RuntimeError("websocket endpoint has multiple connected peers")
        peer = active_peers[0]
        request_id = str(uuid4())
        context = PeerExchangeContext.root()
        self._activate_exchange(context)
        try:
            await peer.send(
                _encode_peer_message(message, context=context, request_id=request_id)
            )
        finally:
            # The wire context, not a process-local conversation ledger, carries
            # the exchange identity and count to the next hop.
            self._clear_exchange()
        return {"message_id": request_id}

    async def _begin_shutdown(self) -> None:
        self._safe_advance(ConnectionStateEvent.SHUTDOWN_REQUESTED)
        self.shutdown_event.set()
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
            "sentinel_drop_count": self.sentinel_drop_count,
            "limit_drop_count": self.limit_drop_count,
            "legacy_response_drop_count": self.legacy_response_drop_count,
            "active_exchange_id": self.active_exchange_id,
            "active_message_count": self.active_message_count,
            "mode": "outbound" if self.config.peer_url else "inbound",
            "peer_url": self.config.peer_url or "",
            "bind_host": self.config.bind_host,
            "bind_port": self.config.bind_port,
            **dict(self.manager_endpoint_info),
        }

    def _report_rejection(self, reason: str, text: str) -> None:
        self.rejection_count += 1
        preview = text.strip()[:80]
        self.last_error = f"{reason}: {preview}" if preview else reason
        logger.warning("websocket bridge rejected inbound frame (%s): %r", reason, preview)

    def _activate_exchange(self, context: PeerExchangeContext) -> None:
        self.active_exchange_id = context.exchange_id
        self.active_message_count = context.message_count

    def _clear_exchange(self) -> None:
        self.active_exchange_id = ""
        self.active_message_count = 0

    def _terminate_exchange(self, reason: str) -> None:
        if reason == "sentinel":
            self.sentinel_drop_count += 1
        elif reason == "limit":
            self.limit_drop_count += 1
        self._clear_exchange()

    # ---- lifecycle ----
    async def run(self) -> None:
        self.manager_server, self.manager_endpoint_info = await start_sidecar_server(
            _manager_endpoint(self.config), self._handle_manager_client
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
                logger.warning(
                    "websocket bridge outbound connection failed; retrying: %s: %s",
                    exc.__class__.__name__,
                    exc,
                )
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
                logger.info(
                    "websocket bridge peer disconnected; reconnecting: %s: %s",
                    exc.__class__.__name__,
                    exc,
                )
            finally:
                peer.closed = True
                self._terminate_exchange("disconnect")
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
            self._terminate_exchange("disconnect")
            async with self._lock:
                self.peers.pop(peer_id, None)

    async def _process_peer_frames(self, websocket: Any, peer_id: str) -> None:
        peer = self.peers.get(peer_id)
        if peer is None:
            peer = _PeerConnection(websocket, peer_id)
        async for raw in websocket:
            text = raw if isinstance(raw, str) else bytes(raw).decode("utf-8", "replace")
            protocol_payload = _decode_protocol_payload(text)
            inbound_text = text
            if protocol_payload is not None:
                payload_type = str(protocol_payload.get("type") or "")
                if payload_type == "user_message":
                    inbound_text = str(protocol_payload.get("text") or "")
                elif payload_type == "slash_command":
                    self._report_rejection(REJECTION_REASON_SLASH, text)
                    continue
                elif payload_type in _SOCKET_RESPONSE_TYPES:
                    self.legacy_response_drop_count += 1
                    logger.info(
                        "websocket bridge dropped legacy response frame type=%s",
                        payload_type,
                    )
                    continue
            if inbound_text == PEER_END_SENTINEL:
                self._terminate_exchange("sentinel")
                continue
            classification = classify_inbound(inbound_text)
            if not classification.deliverable:
                if classification.reason is not None:
                    self._report_rejection(classification.reason, inbound_text)
                self._terminate_exchange("rejected")
                continue
            try:
                exchange = _context_for_inbound(protocol_payload)
            except InvalidPeerContext as exc:
                self._report_rejection(REJECTION_REASON_PEER_CONTEXT, str(exc))
                self._terminate_exchange("invalid_context")
                continue
            if exchange.message_count > MAX_PEER_MESSAGE_COUNT:
                self._terminate_exchange("limit")
                continue
            self._activate_exchange(exchange)
            try:
                final_text = await deliver_inbound(
                    inbound_text,
                    config=self.config,
                )
            except Exception as exc:  # transient delivery failure: report, keep bridging
                self.last_error = f"delivery failed: {exc.__class__.__name__}: {exc}"
                logger.exception("websocket bridge delivery failed")
                self._terminate_exchange("delivery_error")
                continue
            if not final_text:
                self._terminate_exchange("empty")
                continue
            if final_text == PEER_END_SENTINEL:
                self._terminate_exchange("sentinel")
                continue
            next_exchange = exchange.next_message()
            if next_exchange.message_count > MAX_PEER_MESSAGE_COUNT:
                self._terminate_exchange("limit")
                continue
            self._activate_exchange(next_exchange)
            try:
                await peer.send(
                    _encode_peer_message(final_text, context=next_exchange)
                )
            except Exception:
                self._terminate_exchange("send_error")
                raise
            else:
                # The exchange continues only in the message metadata now owned
                # by the peer.  Keeping a local ledger would leave stale state
                # when the peer ends the conversation without another frame.
                self._clear_exchange()

    async def _cleanup(self) -> None:
        self._safe_advance(ConnectionStateEvent.TERMINATED)
        for peer in list(self.peers.values()):
            peer.closed = True
        for peer in list(self.peers.values()):
            with contextlib.suppress(Exception):
                await peer.websocket.close()
        self.peers.clear()
        self._terminate_exchange("shutdown")
        self.listener_bound = False
        if self.manager_server is not None:
            self.manager_server.close()
            with contextlib.suppress(Exception):
                await self.manager_server.wait_closed()
            self.manager_server = None
        with contextlib.suppress(Exception):
            await cleanup_sidecar_endpoint(_manager_endpoint(self.config))


@dataclass(frozen=True)
class SidecarConfig:
    """Declarative configuration for one bridge sidecar process.

    ``bridge_socket_path`` is the provider-owned local channel socket.
    ``bind_host``/``bind_port`` expose the inbound WebSocket listener on the
    trusted LAN; ``peer_url`` is the optional outbound peer bridge URL.
    """

    runtime_root: Path
    bridge_socket_path: Path
    data_root: Path | None = None
    bind_host: str = "0.0.0.0"
    bind_port: int = 0
    peer_url: str | None = None
    reconnect_initial_delay_seconds: float = 1.0
    reconnect_max_delay_seconds: float = 30.0
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
    runtime: _SidecarRuntime | None = None
    try:
        websockets = _load_websockets()
        runtime = _SidecarRuntime(config=config)
        runtime._ws = websockets
        runtime._ws_transient = _transient_exception_types(websockets)
        _RUNTIME = runtime
        await runtime.run()
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("websocket bridge sidecar terminated unexpectedly")
        raise
    finally:
        if runtime is not None and _RUNTIME is runtime:
            _RUNTIME = None


async def deliver_inbound(
    text: str,
    *,
    config: SidecarConfig,
) -> str | None:
    """Run one peer message through the provider-private local channel.

    The local socket may stream reasoning and tool rounds, but this adapter
    returns only the final model round.  Empty/error responses terminate the
    exchange and are never converted into peer text.
    """
    classification = classify_inbound(text)
    if not classification.deliverable:
        runtime = _RUNTIME
        if runtime is not None and classification.reason is not None:
            runtime._report_rejection(classification.reason, text)
        return None

    session: SocketSession | None = None
    stream = _ResponseStream()
    try:
        reader, writer = await asyncio.open_unix_connection(str(config.bridge_socket_path))
        session = SocketSession(
            reader,
            writer,
            read_message=read_socket_message,
            request_id_factory=lambda: str(uuid4()),
        )
        request_id = await session.send(text)
        async for payload in session.stream_response(request_id):
            payload_type = str(payload.get("type") or "")
            if payload_type == "text_delta":
                chunk = str(payload.get("text") or "")
                if chunk:
                    stream.current_text_parts.append(chunk)
                continue
            if payload_type in {"llm_error", "error"}:
                stream.error_text = str(
                    payload.get("error_text") or payload.get("text") or "local Pal failed"
                )
                continue
            if payload_type == "llm_done":
                finish_reason = str(payload.get("finish_reason") or "")
                if finish_reason == "tool_calls":
                    stream.current_text_parts.clear()
                    stream.error_text = ""
                    continue
    finally:
        if session is not None:
            await session.aclose()

    if stream.error_text:
        runtime = _RUNTIME
        if runtime is not None:
            runtime.last_error = f"local Pal response failed: {stream.error_text}"
        return None
    final_text = "".join(stream.current_text_parts)
    return final_text if final_text.strip() else None


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
    "health_snapshot",
]
