from __future__ import annotations

import asyncio
import socket
import tempfile
import unittest
from pathlib import Path
from typing import Any

from pal.channel import EndpointConfig
from websocket_bridge.protocol import PEER_END_SENTINEL
from websocket_bridge.runtime import WebSocketBridgeEndpoint


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
        ),
        socket_path=runtime_root / "data" / "channel" / endpoint_id / "channel.sock",
    )
    endpoint.runtime_root = runtime_root
    endpoint.data_root = runtime_root / "data" / "channel" / endpoint_id
    endpoint.binding_metadata = {
        "bind_host": "127.0.0.1",
        "bind_port": bind_port,
        "peer_url": peer_url,
        "reconnect_initial_delay_seconds": 0.05,
        "reconnect_max_delay_seconds": 0.1,
    }
    return endpoint


class TwoPalBridgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_dedicated_channels_tag_identity_and_sentinel_stops_exchange(self) -> None:
        with tempfile.TemporaryDirectory(prefix="pal_ws_a_") as raw_a, tempfile.TemporaryDirectory(
            prefix="pal_ws_b_"
        ) as raw_b:
            root_a = Path(raw_a)
            root_b = Path(raw_b)
            port_b = _free_loopback_port()
            # On Pal A the endpoint is the remote Petra; on Petra B it is Pal.
            endpoint_b = _endpoint(root_b, "pal", bind_port=port_b)
            endpoint_a = _endpoint(
                root_a,
                "petra",
                bind_port=0,
                peer_url=f"ws://127.0.0.1:{port_b}",
            )
            try:
                await endpoint_b.start_async()
                await self._wait_for_health(endpoint_b, connected=False)
                await endpoint_a.start_async()
                await self._wait_for_health(endpoint_a, connected=True)
                await self._wait_for_health(endpoint_b, connected=True)

                receipt = await endpoint_a.send_message("hello from Pal")
                self.assertEqual(receipt.status, "accepted")

                at_petra = await self._wait_for_ingress(endpoint_b)
                self.assertEqual(at_petra.event.payload["text"], "hello from Pal\n\n--pal")
                endpoint_b.send_reply(at_petra.response_handle, "hello from Petra")

                at_pal = await self._wait_for_ingress(endpoint_a)
                self.assertEqual(at_pal.event.payload["text"], "hello from Petra\n\n--petra")
                endpoint_a.send_reply(at_pal.response_handle, PEER_END_SENTINEL)

                await self._wait_for_exchange_idle(endpoint_a)
                await asyncio.sleep(0.1)
                self.assertEqual(endpoint_b.poll(), [])
            finally:
                await endpoint_a.stop_async()
                await endpoint_b.stop_async()

            self.assertFalse((root_a / "pal.sock").exists())
            self.assertFalse((root_b / "pal.sock").exists())
            self.assertFalse((root_a / "data" / "channel" / "petra" / "channel.sock").exists())
            self.assertFalse((root_b / "data" / "channel" / "pal" / "channel.sock").exists())

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

    async def _wait_for_ingress(self, endpoint: WebSocketBridgeEndpoint):
        deadline = asyncio.get_running_loop().time() + 10.0
        while asyncio.get_running_loop().time() < deadline:
            messages = endpoint.poll()
            if messages:
                return messages[0]
            await asyncio.sleep(0.05)
        self.fail(f"endpoint {endpoint.endpoint.endpoint_id} received no peer message")

    async def _wait_for_exchange_idle(self, endpoint: WebSocketBridgeEndpoint) -> None:
        deadline = asyncio.get_running_loop().time() + 10.0
        last: dict[str, Any] = {}
        while asyncio.get_running_loop().time() < deadline:
            last = await endpoint._rpc_client(
                request_timeout_seconds=1.0
            ).request("health")
            if (
                int(last.get("active_message_count") or 0) == 0
                and int(last.get("sentinel_drop_count") or 0) >= 1
            ):
                return
            await asyncio.sleep(0.05)
        self.fail(f"peer exchange did not return to idle: {last!r}")


if __name__ == "__main__":
    unittest.main()
