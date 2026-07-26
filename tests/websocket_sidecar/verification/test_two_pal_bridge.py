from __future__ import annotations

import asyncio
import contextlib
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pal.channel import EndpointConfig
from pal.channel.providers.websocket_bridge.runtime import WebSocketBridgeEndpoint
from pal.foundation.sidecar import pack_sidecar_message, read_sidecar_message


class _FinalReplySocket:
    """Small real Unix-socket peer that speaks Pal's existing socket protocol."""

    def __init__(self, path: Path, final_reply: str) -> None:
        self.path = path
        self.final_reply = final_reply
        self.received: list[dict[str, Any]] = []
        self.server: asyncio.Server | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._handle, path=str(self.path))

    async def stop(self) -> None:
        if self.server is None:
            return
        self.server.close()
        await self.server.wait_closed()
        self.server = None

    async def _handle(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            request = await read_sidecar_message(reader)
            self.received.append(request)
            request_id = str(request.get("request_id") or "")
            events = [
                {
                    "type": "reasoning_delta",
                    "request_id": request_id,
                    "reasoning_text": "private reasoning",
                },
                {
                    "type": "text_delta",
                    "request_id": request_id,
                    "text": "intermediate content",
                },
                {
                    "type": "op_tool_call",
                    "request_id": request_id,
                    "op_tool_call": {"name": "test_tool", "args": {}},
                },
                {
                    "type": "llm_done",
                    "request_id": request_id,
                    "finish_reason": "tool_calls",
                },
                {
                    "type": "text_delta",
                    "request_id": request_id,
                    "text": self.final_reply,
                },
                {
                    "type": "llm_done",
                    "request_id": request_id,
                    "finish_reason": "stop",
                },
            ]
            for event in events:
                writer.write(pack_sidecar_message(event))
            await writer.drain()
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(("127.0.0.1", 0))
        return int(candidate.getsockname()[1])


def _endpoint(
    runtime_root: Path,
    endpoint_id: str,
    *,
    bind_port: int,
    peer_url: str | None = None,
) -> WebSocketBridgeEndpoint:
    endpoint = WebSocketBridgeEndpoint(
        endpoint=EndpointConfig(
            endpoint_id=endpoint_id,
            channel_kind="websocket_bridge",
            binding_key=f"lan:{endpoint_id}",
        )
    )
    endpoint.runtime_root = runtime_root
    endpoint.socket_channel_path = runtime_root / "pal.sock"
    endpoint.binding_metadata = {
        "bind_host": "127.0.0.1",
        "bind_port": bind_port,
        "peer_url": peer_url,
        "reconnect_initial_delay_seconds": 0.05,
        "reconnect_max_delay_seconds": 0.1,
        "message_timeout_seconds": 10.0,
    }
    endpoint.message_timeout_seconds = 10.0
    return endpoint


class TwoPalBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_sidecars_exchange_final_messages_in_both_directions(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_ws_a_") as raw_a, tempfile.TemporaryDirectory(
            prefix="pal_ws_b_"
        ) as raw_b:
            root_a = Path(raw_a)
            root_b = Path(raw_b)
            socket_a = _FinalReplySocket(root_a / "pal.sock", "reply from A")
            socket_b = _FinalReplySocket(root_b / "pal.sock", "reply from B")
            await socket_a.start()
            await socket_b.start()

            port_b = _free_loopback_port()
            endpoint_b = _endpoint(root_b, "pal-b", bind_port=port_b)
            endpoint_a = _endpoint(
                root_a,
                "pal-a",
                bind_port=0,
                peer_url=f"ws://127.0.0.1:{port_b}",
            )
            try:
                await endpoint_b.start_async()
                await self._wait_for_health(endpoint_b, connected=False)
                await endpoint_a.start_async()
                await self._wait_for_health(endpoint_a, connected=True)
                await self._wait_for_health(endpoint_b, connected=True)

                from_a = await endpoint_a.send_message("hello from A")
                self.assertEqual(from_a.status, "completed")
                self.assertEqual(from_a.response_text, "reply from B")
                self.assertNotIn("private reasoning", from_a.response_text)
                self.assertNotIn("intermediate content", from_a.response_text)
                self.assertEqual(socket_b.received[0]["text"], "hello from A")
                self.assertEqual(socket_a.received, [])

                from_b = await endpoint_b.send_message("hello from B")
                self.assertEqual(from_b.status, "completed")
                self.assertEqual(from_b.response_text, "reply from A")
                self.assertEqual(socket_a.received[0]["text"], "hello from B")
            finally:
                await endpoint_a.stop_async()
                await endpoint_b.stop_async()
                await socket_a.stop()
                await socket_b.stop()

    async def _wait_for_health(
        self,
        endpoint: WebSocketBridgeEndpoint,
        *,
        connected: bool,
    ) -> None:
        deadline = asyncio.get_running_loop().time() + 10.0
        last: dict[str, Any] = {}
        while asyncio.get_running_loop().time() < deadline:
            try:
                last = await endpoint._rpc_client(
                    request_timeout_seconds=1.0
                ).request("health")
            except Exception:
                await asyncio.sleep(0.05)
                continue
            if bool(last.get("listener_bound")) and (
                not connected or int(last.get("connected_peers") or 0) == 1
            ):
                return
            await asyncio.sleep(0.05)
        self.fail(f"websocket sidecar did not become ready: {last!r}")


if __name__ == "__main__":
    unittest.main()
