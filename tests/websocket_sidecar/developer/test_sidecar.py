"""Focused developer tests for the WebSocket bridge sidecar (websocket_sidecar).

These tests exercise boundary classification, reconnect backoff, connection
state, bounded peer exchanges, dedicated-socket delivery, and manager lifecycle.
The ``websockets`` production
dependency is unavailable in this environment, so the transport path is driven
through a minimal test adapter injected via ``_load_websockets`` (the production
path still imports the real ``websockets`` library); the socket-channel path uses
a real in-process Unix socket server.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from websocket_bridge import sidecar
from websocket_bridge.protocol import (
    BridgeAdaptation,
    MAX_PEER_MESSAGE_COUNT,
    PEER_END_SENTINEL,
    PeerExchangeContext,
)
from websocket_bridge.sidecar import (
    BridgeBoundary,
    InboundClassification,
    SidecarConfig,
    SidecarConnectionState,
    ConnectionStateEvent,
    advance_state,
    classify_inbound,
    compute_backoff_delay,
    is_deliverable_inbound,
    rejection_reason,
)
from pal.foundation.sidecar import (
    SidecarEndpoint,
    SidecarRpcClient,
    pack_sidecar_message,
    read_sidecar_message,
)
from pal.tty.session import SocketDisconnected


# --------------------------------------------------------------------------- #
# Fake socket channel server (real asyncio Unix socket, socket-protocol wire) #
# --------------------------------------------------------------------------- #


class _FakeSocketChannel:
    """A minimal socket-channel server that echoes replies to user messages."""

    def __init__(self, path: Path, replies: list[dict[str, Any]]) -> None:
        self.path = path
        self.replies = list(replies)
        self.received: list[dict[str, Any]] = []
        self._server: asyncio.base_events.Server | None = None
        self._stop = asyncio.Event()

    async def start(self) -> None:
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                try:
                    message = await read_sidecar_message(reader)
                except asyncio.IncompleteReadError:
                    return
                self.received.append(message)
                request_id = str(message.get("request_id") or "")
                if str(message.get("type") or "") == "user_message":
                    for template in self.replies:
                        reply = dict(template)
                        reply["request_id"] = request_id
                        writer.write(pack_sidecar_message(reply))
                    await writer.drain()
        except (ConnectionError, OSError, ValueError):
            return
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None


class _FakeWebSocket:
    """Async-iterable WebSocket stand-in for a single connected peer."""

    def __init__(self, frames: list[str] | None = None) -> None:
        self._frames = list(frames or [])
        self.sent: list[str] = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self) -> str:
        if not self._frames:
            raise StopAsyncIteration
        return self._frames.pop(0)

    async def send(self, text: str) -> None:
        self.sent.append(text)

    async def close(self) -> None:
        self.closed = True


class _FakeServer:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        self.closed = True


class _FakeWebsocketsModule:
    """Minimal stand-in for the ``websockets`` package used as a test adapter."""

    class exceptions:
        class WebSocketException(Exception):
            pass

        class ConnectionClosed(Exception):
            pass

        class ConnectionClosedOK(ConnectionClosed):
            pass

        class ConnectionClosedError(ConnectionClosed):
            pass

        class InvalidHandshake(Exception):
            pass

    @staticmethod
    async def serve(handler: Any, host: str, port: int) -> _FakeServer:
        return _FakeServer()

    @staticmethod
    async def connect(uri: str) -> _FakeWebSocket:
        raise OSError("peer unreachable (test adapter)")


# --------------------------------------------------------------------------- #
# Pure functional-core tests                                                  #
# --------------------------------------------------------------------------- #


class BoundaryClassificationTests(unittest.TestCase):
    def test_ordinary_text_is_deliverable(self) -> None:
        self.assertEqual(classify_inbound("hello"), InboundClassification(True, None))
        self.assertTrue(is_deliverable_inbound("hello"))
        self.assertIsNone(rejection_reason("hello"))

    def test_slash_prefix_is_rejected(self) -> None:
        result = classify_inbound("/interrupt")
        self.assertFalse(result.deliverable)
        self.assertEqual(result.reason, "slash_command_rejected_at_bridge_boundary")

    def test_whitespace_obfuscated_slash_is_rejected(self) -> None:
        result = classify_inbound("   /reset")
        self.assertFalse(result.deliverable)
        self.assertEqual(result.reason, "slash_command_rejected_at_bridge_boundary")

    def test_blank_frame_is_not_deliverable_and_has_no_reason(self) -> None:
        result = classify_inbound("   \n\t")
        self.assertFalse(result.deliverable)
        self.assertIsNone(result.reason)

    def test_control_plane_forged_slash_command_is_rejected(self) -> None:
        forged = json.dumps({"type": "slash_command", "text": "/interrupt"})
        result = classify_inbound(forged)
        self.assertFalse(result.deliverable)
        self.assertEqual(result.reason, "control_plane_payload_rejected_at_bridge_boundary")

    def test_plain_json_user_content_is_deliverable(self) -> None:
        # JSON that is not a control-plane forgery is delivered as ordinary text.
        payload = json.dumps({"type": "user_message", "text": "hi"})
        self.assertTrue(classify_inbound(payload).deliverable)

    def test_non_string_input_is_control_plane_rejection(self) -> None:
        result = classify_inbound(b"not a string")  # type: ignore[arg-type]
        self.assertFalse(result.deliverable)
        self.assertEqual(result.reason, "control_plane_payload_rejected_at_bridge_boundary")

    def test_boundary_satisfies_protocol(self) -> None:
        boundary = BridgeBoundary()
        self.assertIsInstance(boundary, BridgeAdaptation)
        self.assertTrue(boundary.is_deliverable_inbound("hello"))
        self.assertEqual(boundary.rejection_reason("/x"), "slash_command_rejected_at_bridge_boundary")


class BackoffTests(unittest.TestCase):
    def test_exponential_growth_until_cap(self) -> None:
        self.assertEqual(compute_backoff_delay(0, 1.0, 30.0), 1.0)
        self.assertEqual(compute_backoff_delay(1, 1.0, 30.0), 2.0)
        self.assertEqual(compute_backoff_delay(2, 1.0, 30.0), 4.0)
        self.assertEqual(compute_backoff_delay(3, 1.0, 30.0), 8.0)
        self.assertEqual(compute_backoff_delay(5, 1.0, 30.0), 30.0)  # capped
        self.assertEqual(compute_backoff_delay(20, 1.0, 30.0), 30.0)

    def test_non_positive_initial_disables_waiting(self) -> None:
        self.assertEqual(compute_backoff_delay(3, 0.0, 30.0), 0.0)
        self.assertEqual(compute_backoff_delay(3, -1.0, 30.0), 0.0)


class TransientExceptionTests(unittest.TestCase):
    def test_lazy_websockets_exceptions_are_loaded_for_reconnect(self) -> None:
        class _LazyWebsocketsPackage:
            __name__ = "websockets"

        collected = sidecar._transient_exception_types(_LazyWebsocketsPackage())
        exceptions = importlib.import_module("websockets.exceptions")

        self.assertTrue(issubclass(exceptions.ConnectionClosed, collected))
        self.assertTrue(issubclass(exceptions.ConnectionClosedOK, collected))
        self.assertTrue(issubclass(exceptions.ConnectionClosedError, collected))


class StateMachineTests(unittest.TestCase):
    def test_documented_transitions_are_permitted(self) -> None:
        state = advance_state(SidecarConnectionState.DISCONNECTED, ConnectionStateEvent.START)
        self.assertEqual(state, SidecarConnectionState.CONNECTING)
        state = advance_state(state, ConnectionStateEvent.CONNECTION_READY)
        self.assertEqual(state, SidecarConnectionState.CONNECTED)
        state = advance_state(state, ConnectionStateEvent.PEER_DISCONNECTED)
        self.assertEqual(state, SidecarConnectionState.RECONNECT_BACKOFF)
        state = advance_state(state, ConnectionStateEvent.BACKOFF_ELAPSED)
        self.assertEqual(state, SidecarConnectionState.CONNECTING)
        state = advance_state(state, ConnectionStateEvent.CONNECTION_FAILED)
        self.assertEqual(state, SidecarConnectionState.RECONNECT_BACKOFF)
        state = advance_state(state, ConnectionStateEvent.SHUTDOWN_REQUESTED)
        self.assertEqual(state, SidecarConnectionState.SHUTTING_DOWN)
        state = advance_state(state, ConnectionStateEvent.TERMINATED)
        self.assertEqual(state, SidecarConnectionState.DISCONNECTED)

    def test_undocumented_transition_is_rejected(self) -> None:
        with self.assertRaises(sidecar.InvalidStateTransition):
            advance_state(SidecarConnectionState.DISCONNECTED, ConnectionStateEvent.BACKOFF_ELAPSED)
        with self.assertRaises(sidecar.InvalidStateTransition):
            advance_state(SidecarConnectionState.CONNECTED, ConnectionStateEvent.START)


# --------------------------------------------------------------------------- #
# Async shell tests                                                           #
# --------------------------------------------------------------------------- #


class DeliveryAndForwardTests(unittest.IsolatedAsyncioTestCase):
    def _runtime(self, path: Path = Path("/tmp/bridge.sock")) -> sidecar._SidecarRuntime:
        return sidecar._SidecarRuntime(
            config=SidecarConfig(Path("/tmp"), path)
        )

    async def test_active_send_is_root_and_returns_after_transport_accepts(self) -> None:
        runtime = self._runtime()
        peer = _FakeWebSocket()
        runtime.peers["peer-1"] = sidecar._PeerConnection(peer, "peer-1")

        result = await asyncio.wait_for(runtime._send_message("hello peer"), timeout=0.1)
        request = json.loads(peer.sent[0])
        self.assertEqual(request["type"], "user_message")
        self.assertEqual(request["text"], "hello peer")
        self.assertEqual(request["peer_context"]["message_count"], 1)
        self.assertEqual(result, {"message_id": request["request_id"]})
        self.assertEqual(runtime.active_message_count, 0)
        self.assertEqual(runtime.active_exchange_id, "")

    async def test_active_send_requires_exactly_one_peer(self) -> None:
        runtime = self._runtime()
        with self.assertRaisesRegex(RuntimeError, "not connected"):
            await runtime._send_message("hello")
        runtime.peers["a"] = sidecar._PeerConnection(_FakeWebSocket(), "a")
        runtime.peers["b"] = sidecar._PeerConnection(_FakeWebSocket(), "b")
        with self.assertRaisesRegex(RuntimeError, "multiple"):
            await runtime._send_message("hello")

    async def test_peer_message_delivers_only_final_and_increments_count(self) -> None:
        runtime = self._runtime()
        context = PeerExchangeContext.root()
        peer = _FakeWebSocket(
            frames=[sidecar._encode_peer_message("ping", context=context)]
        )
        runtime.peers["peer-1"] = sidecar._PeerConnection(peer, "peer-1")
        delivered: list[str] = []
        original = sidecar.deliver_inbound

        async def capture(text: str, *, config: SidecarConfig) -> str:
            _ = config
            delivered.append(text)
            return "pong"

        sidecar.deliver_inbound = capture  # type: ignore[assignment]
        try:
            await runtime._process_peer_frames(peer, "peer-1")
        finally:
            sidecar.deliver_inbound = original  # type: ignore[assignment]

        self.assertEqual(delivered, ["ping"])
        response = json.loads(peer.sent[0])
        self.assertEqual(response["text"], "pong")
        self.assertEqual(response["peer_context"]["exchange_id"], context.exchange_id)
        self.assertEqual(response["peer_context"]["message_count"], 2)
        self.assertEqual(runtime.active_exchange_id, "")
        self.assertEqual(runtime.active_message_count, 0)

    async def test_only_exact_peer_end_is_a_sentinel(self) -> None:
        runtime = self._runtime()
        context = PeerExchangeContext.root()
        peer = _FakeWebSocket(
            frames=[
                sidecar._encode_peer_message(
                    f" {PEER_END_SENTINEL} ",
                    context=context,
                )
            ]
        )
        runtime.peers["peer-1"] = sidecar._PeerConnection(peer, "peer-1")
        delivered: list[str] = []
        original = sidecar.deliver_inbound

        async def capture(text: str, *, config: SidecarConfig) -> str:
            _ = config
            delivered.append(text)
            return PEER_END_SENTINEL

        sidecar.deliver_inbound = capture  # type: ignore[assignment]
        try:
            await runtime._process_peer_frames(peer, "peer-1")
        finally:
            sidecar.deliver_inbound = original  # type: ignore[assignment]

        self.assertEqual(delivered, [f" {PEER_END_SENTINEL} "])
        self.assertEqual(runtime.sentinel_drop_count, 1)

    async def test_peer_end_silently_terminates_and_resets_count(self) -> None:
        runtime = self._runtime()
        context = PeerExchangeContext.root()
        peer = _FakeWebSocket(
            frames=[sidecar._encode_peer_message("ping", context=context)]
        )
        runtime.peers["peer-1"] = sidecar._PeerConnection(peer, "peer-1")
        original = sidecar.deliver_inbound

        async def end(_text: str, *, config: SidecarConfig) -> str:
            _ = config
            return PEER_END_SENTINEL

        sidecar.deliver_inbound = end  # type: ignore[assignment]
        try:
            await runtime._process_peer_frames(peer, "peer-1")
        finally:
            sidecar.deliver_inbound = original  # type: ignore[assignment]

        self.assertEqual(peer.sent, [])
        self.assertEqual(runtime.active_message_count, 0)
        self.assertEqual(runtime.active_exchange_id, "")
        self.assertEqual(runtime.sentinel_drop_count, 1)

    async def test_eighth_message_is_delivered_but_ninth_is_dropped(self) -> None:
        runtime = self._runtime()
        context = PeerExchangeContext.root()
        context = PeerExchangeContext(context.exchange_id, MAX_PEER_MESSAGE_COUNT)
        peer = _FakeWebSocket(
            frames=[sidecar._encode_peer_message("eighth", context=context)]
        )
        runtime.peers["peer-1"] = sidecar._PeerConnection(peer, "peer-1")
        delivered: list[str] = []
        original = sidecar.deliver_inbound

        async def reply(text: str, *, config: SidecarConfig) -> str:
            _ = config
            delivered.append(text)
            return "would be ninth"

        sidecar.deliver_inbound = reply  # type: ignore[assignment]
        try:
            await runtime._process_peer_frames(peer, "peer-1")
        finally:
            sidecar.deliver_inbound = original  # type: ignore[assignment]

        self.assertEqual(delivered, ["eighth"])
        self.assertEqual(peer.sent, [])
        self.assertEqual(runtime.active_message_count, 0)
        self.assertEqual(runtime.limit_drop_count, 1)

    async def test_incoming_ninth_and_incoming_sentinel_are_dropped_before_pal(self) -> None:
        runtime = self._runtime()
        root = PeerExchangeContext.root()
        ninth = PeerExchangeContext(root.exchange_id, MAX_PEER_MESSAGE_COUNT + 1)
        peer = _FakeWebSocket(
            frames=[
                sidecar._encode_peer_message("too far", context=ninth),
                PEER_END_SENTINEL,
            ]
        )
        runtime.peers["peer-1"] = sidecar._PeerConnection(peer, "peer-1")
        calls = 0
        original = sidecar.deliver_inbound

        async def capture(_text: str, *, config: SidecarConfig) -> str:
            nonlocal calls
            _ = config
            calls += 1
            return "unexpected"

        sidecar.deliver_inbound = capture  # type: ignore[assignment]
        try:
            await runtime._process_peer_frames(peer, "peer-1")
        finally:
            sidecar.deliver_inbound = original  # type: ignore[assignment]

        self.assertEqual(calls, 0)
        self.assertEqual(runtime.active_message_count, 0)
        self.assertEqual(runtime.limit_drop_count, 1)
        self.assertEqual(runtime.sentinel_drop_count, 1)

    async def test_legacy_response_frames_are_never_reinjected(self) -> None:
        runtime = self._runtime()
        peer = _FakeWebSocket(
            frames=[json.dumps({"type": "text_delta", "request_id": "old", "text": "echo"})]
        )
        runtime.peers["peer-1"] = sidecar._PeerConnection(peer, "peer-1")
        await runtime._process_peer_frames(peer, "peer-1")
        self.assertEqual(runtime.legacy_response_drop_count, 1)
        self.assertEqual(peer.sent, [])

    async def test_deliver_inbound_uses_dedicated_socket_and_returns_final_round(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runtime_root = Path(raw_root)
            socket_path = runtime_root / "data" / "channel" / "bridge_test" / "channel.sock"
            socket_path.parent.mkdir(parents=True)
            channel = _FakeSocketChannel(
                socket_path,
                replies=[
                    {"type": "text_delta", "text": "intermediate"},
                    {"type": "llm_done", "finish_reason": "tool_calls"},
                    {"type": "text_delta", "text": "pong"},
                    {"type": "done", "finish_reason": "stop"},
                ],
            )
            await channel.start()
            try:
                config = SidecarConfig(runtime_root, socket_path)
                result = await sidecar.deliver_inbound("hello", config=config)
            finally:
                await channel.stop()

        self.assertEqual(result, "pong")
        self.assertEqual(channel.received[0]["text"], "hello")

    async def test_deliver_inbound_rejects_slash_without_touching_socket(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runtime_root = Path(raw_root)
            socket_path = runtime_root / "channel.sock"
            channel = _FakeSocketChannel(socket_path, replies=[])
            await channel.start()
            runtime = sidecar._SidecarRuntime(
                config=SidecarConfig(runtime_root, socket_path)
            )
            sidecar._RUNTIME = runtime
            try:
                result = await sidecar.deliver_inbound("/secret", config=runtime.config)
            finally:
                sidecar._RUNTIME = None
                await channel.stop()

        self.assertIsNone(result)
        self.assertEqual(channel.received, [])
        self.assertEqual(runtime.rejection_count, 1)

    async def test_deliver_inbound_closes_connection_on_send_failure(self) -> None:
        class _FailingWriter:
            def __init__(self) -> None:
                self.closed = False

            def write(self, data: bytes) -> None:
                raise ConnectionError("channel write failed")

            async def drain(self) -> None:
                pass

            def close(self) -> None:
                self.closed = True

            async def wait_closed(self) -> None:
                self.closed = True

        class _DummyReader:
            pass

        failing_writer = _FailingWriter()

        async def fake_open_unix_connection(_path: str):
            return _DummyReader(), failing_writer

        config = SidecarConfig(Path("/tmp"), Path("/tmp/bridge.sock"))
        original_open = sidecar.asyncio.open_unix_connection
        sidecar.asyncio.open_unix_connection = fake_open_unix_connection  # type: ignore[assignment]
        try:
            with self.assertRaises(SocketDisconnected):
                await sidecar.deliver_inbound("hello", config=config)
        finally:
            sidecar.asyncio.open_unix_connection = original_open  # type: ignore[assignment]
        self.assertTrue(failing_writer.closed)


class ServeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_serve_health_and_shutdown_over_manager_socket(self) -> None:
        # Inject the test adapter for the websockets wire implementation; the
        # production path imports the real library.
        original_loader = sidecar._load_websockets
        sidecar._load_websockets = lambda: _FakeWebsocketsModule()  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as raw_root:
                runtime_root = Path(raw_root)
                config = SidecarConfig(
                    runtime_root=runtime_root,
                    bridge_socket_path=runtime_root / "data" / "channel" / "bridge_test" / "channel.sock",
                    bind_host="127.0.0.1",
                    bind_port=0,
                )
                serve_task = asyncio.create_task(sidecar.serve(config))
                endpoint = SidecarEndpoint(
                    runtime_root=runtime_root,
                    name=sidecar.SIDECAR_NAME,
                    runtime_dir_override=config.bridge_socket_path.parent,
                )
                client = SidecarRpcClient(endpoint=endpoint, request_timeout_seconds=5.0)
                try:
                    health = await self._await_health(client)
                    self.assertTrue(health["process_running"])
                    self.assertTrue(health["listener_bound"])
                    self.assertEqual(health["mode"], "inbound")
                    # Snapshot mirrors runtime state.
                    snapshot = sidecar.health_snapshot()
                    self.assertTrue(snapshot.process_running)
                    self.assertTrue(snapshot.listener_bound)
                    # Shutdown over the manager socket terminates serve cleanly.
                    shutdown = await client.request("shutdown")
                    self.assertTrue(shutdown.get("ok"))
                    await asyncio.wait_for(serve_task, timeout=5.0)
                finally:
                    with contextlib.suppress(Exception):
                        serve_task.cancel()
                        await serve_task
        finally:
            sidecar._load_websockets = original_loader  # type: ignore[assignment]
        # Runtime is released after serve returns.
        self.assertIsNone(sidecar._RUNTIME)

    async def test_serve_reports_missing_websockets_as_fatal(self) -> None:
        original_loader = sidecar._load_websockets
        sidecar._load_websockets = _raise_missing_websockets  # type: ignore[assignment]
        try:
            with tempfile.TemporaryDirectory() as raw_root:
                config = SidecarConfig(
                    runtime_root=Path(raw_root),
                    bridge_socket_path=Path(raw_root) / "data" / "channel" / "bridge_test" / "channel.sock",
                )
                with self.assertLogs(sidecar.logger, level="ERROR") as captured:
                    with self.assertRaises(ModuleNotFoundError):
                        await sidecar.serve(config)
        finally:
            sidecar._load_websockets = original_loader  # type: ignore[assignment]
        self.assertTrue(
            any(
                "websocket bridge sidecar terminated unexpectedly" in message
                for message in captured.output
            )
        )
        self.assertIsNone(sidecar._RUNTIME)

    async def _await_health(self, client: SidecarRpcClient) -> dict[str, Any]:
        deadline = asyncio.get_event_loop().time() + 5.0
        last_exc: Exception | None = None
        while asyncio.get_event_loop().time() < deadline:
            try:
                return await client.request("health")
            except Exception as exc:  # manager socket not ready yet
                last_exc = exc
                await asyncio.sleep(0.05)
        raise AssertionError(f"manager socket never became ready: {last_exc}")


def _raise_missing_websockets():
    raise ModuleNotFoundError("websockets")


if __name__ == "__main__":
    unittest.main()
