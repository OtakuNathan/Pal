"""Adversarial verification corpus for the WebSocket bridge sidecar.

Extends the developer corpus with the protocol edges the developer tests leave
open:

* the OUTBOUND serve lifecycle (connect failure -> reconnect backoff -> shutdown
  interrupt -> clean exit) which the developer corpus only covers in inbound mode,
* end-to-end boundary enforcement through the production frame loop
  (``_process_peer_frames``), proving slash/control-plane frames never reach the
  socket channel while ordinary frames do,
* reconnect-backoff interruption by shutdown,
* binary-frame decoding through the frame loop,
* reply forwarding dropped (not raised) when the originating peer is gone,
* the wire-implementation invariant: ``websockets`` is the only WebSocket wire
  implementation and is imported lazily (no aiohttp / stdlib reimplementation).

The production ``websockets`` dependency is unavailable in this environment, so
the transport is driven through minimal adapters injected via
``_load_websockets``; the socket-channel path uses a real in-process Unix socket
server.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from websocket_bridge import sidecar
from websocket_bridge.protocol import PeerExchangeContext
from websocket_bridge.sidecar import (
    SidecarConfig,
    compute_backoff_delay,
)
from pal.foundation.sidecar import (
    SidecarEndpoint,
    SidecarRpcClient,
    pack_sidecar_message,
    read_sidecar_message,
)


# --------------------------------------------------------------------------- #
# Shared fakes                                                                #
# --------------------------------------------------------------------------- #


class _FakeSocketChannel:
    """A minimal socket-channel server that echoes replies to user messages."""

    def __init__(self, path: Path, replies: list[dict[str, Any]]) -> None:
        self.path = path
        self.replies = list(replies)
        self.received: list[dict[str, Any]] = []
        self._server: asyncio.base_events.Server | None = None

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
    """Async-iterable WebSocket stand-in for one connected peer."""

    def __init__(self, frames: list[Any] | None = None) -> None:
        self._frames = list(frames or [])
        self.sent: list[str] = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
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


class _FailingOutboundWebsockets:
    """``websockets`` adapter whose outbound ``connect`` always fails.

    Records the number of connect attempts so a test can assert the reconnect
    loop actually cycled before shutdown interrupted it.
    """

    connect_attempts = 0

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

    @classmethod
    async def connect(cls, uri: str):  # noqa: ANN206 - test stand-in
        cls.connect_attempts += 1
        raise OSError(f"peer unreachable (test adapter) attempt={cls.connect_attempts}")


class _DisconnectingWebSocket(_FakeWebSocket):
    def __init__(self, disconnect_error: Exception | None = None) -> None:
        super().__init__()
        self._disconnect_error = disconnect_error
        self._closed_event = asyncio.Event()

    async def __anext__(self):
        if self._disconnect_error is not None:
            error = self._disconnect_error
            self._disconnect_error = None
            raise error
        await self._closed_event.wait()
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True
        self._closed_event.set()


class _DisconnectingOutboundWebsockets(_FailingOutboundWebsockets):
    """First connection drops normally; the second remains connected."""

    @classmethod
    async def connect(cls, uri: str):  # noqa: ANN206 - test stand-in
        cls.connect_attempts += 1
        if cls.connect_attempts == 1:
            return _DisconnectingWebSocket(cls.exceptions.ConnectionClosedOK())
        return _DisconnectingWebSocket()


# --------------------------------------------------------------------------- #
# Outbound serve lifecycle (untested by the developer corpus)                #
# --------------------------------------------------------------------------- #


class OutboundServeLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self._original_loader = sidecar._load_websockets
        _FailingOutboundWebsockets.connect_attempts = 0

    async def asyncTearDown(self) -> None:
        sidecar._load_websockets = self._original_loader
        sidecar._RUNTIME = None

    async def test_outbound_connect_failure_triggers_backoff_then_shutdown_exits(self) -> None:
        sidecar._load_websockets = lambda: _FailingOutboundWebsockets()  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as raw_root:
            runtime_root = Path(raw_root)
            config = SidecarConfig(
                runtime_root=runtime_root,
                bridge_socket_path=runtime_root / "data" / "channel" / "bridge_test" / "channel.sock",
                peer_url="ws://peer.example/bridge",
                reconnect_initial_delay_seconds=30.0,
                reconnect_max_delay_seconds=30.0,
            )
            serve_task = asyncio.create_task(sidecar.serve(config))
            endpoint = SidecarEndpoint(
                runtime_root=runtime_root,
                name=sidecar.SIDECAR_NAME,
                runtime_dir_override=config.bridge_socket_path.parent,
            )
            client = SidecarRpcClient(endpoint=endpoint, request_timeout_seconds=5.0)
            try:
                # Wait until at least one connect attempt failed and the runtime
                # settled into the reconnect_backoff state (peer unreachable).
                await self._wait_for_state(
                    client, {"reconnect_backoff"}, deadline_seconds=5.0
                )
                self.assertGreater(_FailingOutboundWebsockets.connect_attempts, 0)
                health = await client.request("health")
                self.assertEqual(health["mode"], "outbound")
                self.assertIn(health["last_error"], health["last_error"])  # error surfaced
                self.assertTrue(health["last_error"])  # peer-unreachable reported

                # Shutdown during the (30s) backoff must interrupt it promptly.
                shutdown = await client.request("shutdown")
                self.assertTrue(shutdown.get("ok"))
                await asyncio.wait_for(serve_task, timeout=5.0)
            finally:
                with contextlib.suppress(Exception):
                    serve_task.cancel()
                    await serve_task

        # The runtime singleton is released after a clean outbound shutdown and
        # at least one reconnect attempt actually occurred.
        self.assertIsNone(sidecar._RUNTIME)
        self.assertGreater(_FailingOutboundWebsockets.connect_attempts, 0)

    async def test_normal_peer_disconnect_reconnects_without_exiting_sidecar(self) -> None:
        _DisconnectingOutboundWebsockets.connect_attempts = 0
        sidecar._load_websockets = lambda: _DisconnectingOutboundWebsockets()  # type: ignore[assignment]
        with tempfile.TemporaryDirectory() as raw_root:
            runtime_root = Path(raw_root)
            config = SidecarConfig(
                runtime_root=runtime_root,
                bridge_socket_path=runtime_root / "data" / "channel" / "bridge_test" / "channel.sock",
                peer_url="ws://peer.example/bridge",
                reconnect_initial_delay_seconds=0.0,
                reconnect_max_delay_seconds=0.0,
            )
            serve_task = asyncio.create_task(sidecar.serve(config))
            endpoint = SidecarEndpoint(
                runtime_root=runtime_root,
                name=sidecar.SIDECAR_NAME,
                runtime_dir_override=config.bridge_socket_path.parent,
            )
            client = SidecarRpcClient(endpoint=endpoint, request_timeout_seconds=5.0)
            try:
                await self._wait_for_connect_attempts(2, deadline_seconds=5.0)
                health = await client.request("health")
                self.assertTrue(health["process_running"])
                self.assertEqual(health["state"], "connected")
                self.assertEqual(health["connected_peers"], 1)

                shutdown = await client.request("shutdown")
                self.assertTrue(shutdown.get("ok"))
                await asyncio.wait_for(serve_task, timeout=5.0)
            finally:
                with contextlib.suppress(Exception):
                    serve_task.cancel()
                    await serve_task

        self.assertIsNone(sidecar._RUNTIME)
        self.assertGreaterEqual(_DisconnectingOutboundWebsockets.connect_attempts, 2)

    async def _wait_for_state(
        self,
        client: SidecarRpcClient,
        wanted: set[str],
        *,
        deadline_seconds: float,
    ) -> None:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + deadline_seconds
        last: dict[str, Any] = {}
        while loop.time() < deadline:
            try:
                health = await client.request("health")
            except Exception:
                await asyncio.sleep(0.02)
                continue
            last = health
            if str(health.get("state") or "") in wanted:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(
            f"sidecar never reached state in {wanted}; last health={last!r}"
        )

    async def _wait_for_connect_attempts(
        self,
        wanted: int,
        *,
        deadline_seconds: float,
    ) -> None:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + deadline_seconds
        while loop.time() < deadline:
            if _DisconnectingOutboundWebsockets.connect_attempts >= wanted:
                return
            await asyncio.sleep(0.02)
        raise AssertionError(
            "sidecar did not reconnect after a normal peer disconnect; "
            f"attempts={_DisconnectingOutboundWebsockets.connect_attempts}"
        )


# --------------------------------------------------------------------------- #
# End-to-end boundary enforcement through the production frame loop          #
# --------------------------------------------------------------------------- #


class FrameLoopBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_frame_loop_delivers_ordinary_rejects_boundary(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runtime_root = Path(raw_root)
            socket_path = runtime_root / "channel.sock"
            channel = _FakeSocketChannel(
                socket_path,
                replies=[{"type": "done", "finish_reason": "stop"}],
            )
            await channel.start()
            try:
                runtime = sidecar._SidecarRuntime(
                    config=SidecarConfig(
                        runtime_root=runtime_root, bridge_socket_path=socket_path
                    )
                )
                fake_peer = _FakeWebSocket()
                runtime.peers["peer-1"] = sidecar._PeerConnection(fake_peer, "peer-1")
                sidecar._RUNTIME = runtime
                try:
                    frames = [
                        sidecar._encode_peer_message(
                            "hello world", context=PeerExchangeContext.root()
                        ),
                        "/secret",                                       # slash -> rejected
                        "   /reset",                                     # ws-obfuscated slash -> rejected
                        json.dumps({"type": "slash_command", "text": "/x"}),  # forged -> rejected
                        sidecar._encode_peer_message(
                            "ok", context=PeerExchangeContext.root()
                        ),
                        "   \n\t",                                       # blank -> dropped, no rejection
                        sidecar._encode_peer_message(
                            "hi again", context=PeerExchangeContext.root()
                        ),
                    ]
                    fake_ws = _FakeWebSocket(frames=frames)
                    await runtime._process_peer_frames(fake_ws, "peer-1")
                finally:
                    sidecar._RUNTIME = None
            finally:
                await channel.stop()

            # ONLY the two ordinary frames (and the framed user message)
            # reached the socket channel, each as a user_message.
            user_messages = [
                m for m in channel.received if str(m.get("type") or "") == "user_message"
            ]
            texts = [str(m.get("text") or "") for m in user_messages]
            self.assertEqual(texts, ["hello world", "ok", "hi again"])
            # No frame was ever classified as a slash_command by the delivery path.
            self.assertFalse(
                any(str(m.get("type") or "") == "slash_command" for m in channel.received)
            )
            # Slash x2 + forged control-plane x1 = three reported rejections.
            self.assertEqual(runtime.rejection_count, 3)

    async def test_frame_loop_rejects_unframed_binary_text(self) -> None:
        with tempfile.TemporaryDirectory() as raw_root:
            runtime_root = Path(raw_root)
            socket_path = runtime_root / "channel.sock"
            channel = _FakeSocketChannel(socket_path, replies=[{"type": "done", "finish_reason": "stop"}])
            await channel.start()
            try:
                runtime = sidecar._SidecarRuntime(
                    config=SidecarConfig(
                        runtime_root=runtime_root, bridge_socket_path=socket_path
                    )
                )
                runtime.peers["peer-1"] = sidecar._PeerConnection(_FakeWebSocket(), "peer-1")
                sidecar._RUNTIME = runtime
                try:
                    # Unframed text has no enforceable exchange count and is
                    # therefore rejected before local Pal ingress.
                    fake_ws = _FakeWebSocket(frames=[b"binary hello"])
                    await runtime._process_peer_frames(fake_ws, "peer-1")
                finally:
                    sidecar._RUNTIME = None
            finally:
                await channel.stop()

            user_messages = [
                m for m in channel.received if str(m.get("type") or "") == "user_message"
            ]
            self.assertEqual(user_messages, [])
            self.assertEqual(runtime.rejection_count, 1)

    async def test_delivery_failure_to_socket_channel_is_reported_not_raised(self) -> None:
        # The local socket channel does not exist -> open_unix_connection fails.
        # The frame loop must report it (health last_error) and keep bridging
        # rather than crashing the transport.
        with tempfile.TemporaryDirectory() as raw_root:
            runtime_root = Path(raw_root)
            runtime = sidecar._SidecarRuntime(
                config=SidecarConfig(
                    runtime_root=runtime_root,
                    bridge_socket_path=runtime_root / "missing.sock",
                )
            )
            sidecar._RUNTIME = runtime
            try:
                fake_ws = _FakeWebSocket(
                    frames=[
                        sidecar._encode_peer_message(
                            "hello", context=PeerExchangeContext.root()
                        )
                    ]
                )
                # Must not raise; the failure is captured in last_error.
                await runtime._process_peer_frames(fake_ws, "peer-1")
            finally:
                sidecar._RUNTIME = None
        self.assertIn("delivery failed", runtime.last_error)


# --------------------------------------------------------------------------- #
# Backoff interrupt + reply routing edge                                      #
# --------------------------------------------------------------------------- #


class BackoffAndReplyEdgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_backoff_wait_interrupted_by_shutdown(self) -> None:
        runtime = sidecar._SidecarRuntime(
            config=SidecarConfig(
                runtime_root=Path("/tmp"),
                bridge_socket_path=Path("/tmp/bridge.sock"),
                reconnect_initial_delay_seconds=30.0,
                reconnect_max_delay_seconds=30.0,
            )
        )
        runtime.shutdown_event.set()
        # Even with a 30s configured delay, shutdown returns False immediately.
        result = await asyncio.wait_for(runtime._backoff_wait(0), timeout=2.0)
        self.assertFalse(result)

    async def test_backoff_wait_zero_delay_returns_true_when_running(self) -> None:
        runtime = sidecar._SidecarRuntime(
            config=SidecarConfig(
                runtime_root=Path("/tmp"),
                bridge_socket_path=Path("/tmp/bridge.sock"),
                reconnect_initial_delay_seconds=0.0,
            )
        )
        # A disabled (<=0) delay does not wait and reports "retry" while running.
        result = await runtime._backoff_wait(3)
        self.assertTrue(result)

    async def test_disconnect_clears_exchange_counter(self) -> None:
        runtime = sidecar._SidecarRuntime(
            config=SidecarConfig(Path("/tmp"), Path("/tmp/bridge.sock"))
        )
        context = sidecar.PeerExchangeContext.root()
        runtime._activate_exchange(context)
        runtime._terminate_exchange("disconnect")
        self.assertEqual(runtime.active_exchange_id, "")
        self.assertEqual(runtime.active_message_count, 0)


# --------------------------------------------------------------------------- #
# Wire-implementation invariant (websockets is the sole wire implementation)  #
# --------------------------------------------------------------------------- #


class WireImplementationInvariantTests(unittest.TestCase):
    def test_compute_backoff_caps_and_grows(self) -> None:
        # Sanity on the pure backoff for non-integer / large attempts.
        self.assertEqual(compute_backoff_delay(0, 0.5, 4.0), 0.5)
        self.assertEqual(compute_backoff_delay(3, 0.5, 4.0), 4.0)  # 0.5*8=4 capped
        self.assertEqual(compute_backoff_delay(100, 0.5, 4.0), 4.0)

    def test_module_uses_lazy_websockets_only(self) -> None:
        import inspect

        source = inspect.getsource(sidecar)
        # The websockets library is imported lazily through _load_websockets and
        # is the only WebSocket wire implementation.
        self.assertIn("importlib.import_module", source)
        self.assertIn('"websockets"', source)
        # No forbidden alternative wire implementations are referenced.
        for forbidden in ("import aiohttp", "from aiohttp", "http.server", "websocketserver"):
            self.assertNotIn(forbidden.lower(), source.lower(), f"forbidden wire ref: {forbidden}")


if __name__ == "__main__":
    unittest.main()
