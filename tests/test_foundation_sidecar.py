from __future__ import annotations

import asyncio
import io
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from pal.foundation.sidecar import (
    MAX_SIDECAR_FRAME_BYTES,
    SidecarEndpoint,
    SidecarSyncStream,
    cleanup_sidecar_endpoint,
    open_sidecar_connection,
    pack_sidecar_message,
    read_sidecar_message,
    read_sidecar_message_sync,
    start_sidecar_server,
)


class _AsyncReader:
    def __init__(self, payload: bytes) -> None:
        self._stream = io.BytesIO(payload)
        self.read_sizes: list[int] = []

    async def readexactly(self, size: int) -> bytes:
        self.read_sizes.append(size)
        value = self._stream.read(size)
        if len(value) != size:
            raise asyncio.IncompleteReadError(value, size)
        return value


class FoundationSidecarTests(unittest.TestCase):
    def test_async_decoder_reads_large_frames_incrementally(self) -> None:
        packed = pack_sidecar_message({"payload": "x" * 200_000})
        reader = _AsyncReader(packed)
        decoded = asyncio.run(read_sidecar_message(reader))
        self.assertEqual(len(decoded["payload"]), 200_000)
        self.assertEqual(reader.read_sizes[0], 4)
        self.assertGreater(len(reader.read_sizes), 4)
        self.assertLessEqual(max(reader.read_sizes[1:]), 64 * 1024)

    def test_decoders_reject_oversized_frame_before_reading_payload(self) -> None:
        header = (MAX_SIDECAR_FRAME_BYTES + 1).to_bytes(4, "big")
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            read_sidecar_message_sync(io.BytesIO(header))
        reader = _AsyncReader(header)
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            asyncio.run(read_sidecar_message(reader))
        self.assertEqual(reader.read_sizes, [4])

    def test_idle_sync_stream_close_from_other_thread_reclaims_socket(self) -> None:
        client_socket, server_socket = socket.socketpair()
        endpoint = SidecarEndpoint(runtime_root=Path("/tmp"), name="test")
        try:
            with patch(
                "pal.foundation.sidecar._open_sidecar_socket_sync",
                return_value=client_socket,
            ):
                stream = SidecarSyncStream(
                    endpoint=endpoint,
                    method="idle",
                    params={},
                    timeout_seconds=1.0,
                )

            closer = threading.Thread(target=stream.close)
            closer.start()
            closer.join(timeout=2.0)

            self.assertFalse(closer.is_alive())
            self.assertTrue(stream._owner.closed)
            self.assertTrue(stream._closed)
            server_socket.settimeout(1.0)
            while server_socket.recv(4096):
                pass
        finally:
            client_socket.close()
            server_socket.close()

    def test_cross_thread_close_wakes_blocked_sync_stream_before_close(self) -> None:
        client_socket, server_socket = socket.socketpair()
        endpoint = SidecarEndpoint(runtime_root=Path("/tmp"), name="blocked-test")
        ready = threading.Event()
        finished = threading.Event()
        errors: list[BaseException] = []
        streams: list[SidecarSyncStream] = []

        def consume() -> None:
            try:
                with patch(
                    "pal.foundation.sidecar._open_sidecar_socket_sync",
                    return_value=client_socket,
                ):
                    stream = SidecarSyncStream(
                        endpoint=endpoint,
                        method="blocked",
                        params={},
                        timeout_seconds=10.0,
                    )
                streams.append(stream)
                ready.set()
                next(stream)
            except Exception as exc:
                errors.append(exc)
            finally:
                finished.set()

        owner = threading.Thread(target=consume)
        owner.start()
        try:
            self.assertTrue(ready.wait(timeout=1.0))
            time.sleep(0.05)
            started = time.monotonic()
            streams[0].close()
            elapsed = time.monotonic() - started

            owner.join(timeout=2.0)
            self.assertFalse(owner.is_alive())
            self.assertTrue(finished.is_set())
            self.assertLess(elapsed, 2.0)
            self.assertTrue(streams[0]._owner.closed)
            self.assertTrue(streams[0]._closed)
            self.assertTrue(errors)
        finally:
            server_socket.close()
            if owner.is_alive():
                client_socket.close()
                owner.join(timeout=1.0)

    @unittest.skipUnless(hasattr(socket, "AF_UNIX"), "Unix sockets unavailable")
    def test_unix_server_retires_stale_tcp_locator_and_clients_prefer_unix(self) -> None:
        async def exercise() -> None:
            with tempfile.TemporaryDirectory() as raw_root:
                endpoint = SidecarEndpoint(runtime_root=Path(raw_root), name="test")
                endpoint.runtime_dir.mkdir(parents=True)
                endpoint.port_path.write_text("not-a-port", encoding="utf-8")
                accepted = asyncio.Event()

                async def handler(
                    reader: asyncio.StreamReader,
                    writer: asyncio.StreamWriter,
                ) -> None:
                    _ = reader
                    accepted.set()
                    writer.close()
                    await writer.wait_closed()

                server, info = await start_sidecar_server(endpoint, handler)
                try:
                    self.assertEqual(info["transport"], "unix")
                    self.assertFalse(endpoint.port_path.exists())

                    # Even if a crash residue reappears, the live Unix endpoint
                    # is authoritative and must win over the TCP locator.
                    endpoint.port_path.write_text("not-a-port", encoding="utf-8")
                    _, writer = await open_sidecar_connection(endpoint)
                    writer.close()
                    await writer.wait_closed()
                    await asyncio.wait_for(accepted.wait(), timeout=1.0)
                finally:
                    server.close()
                    await server.wait_closed()
                    await cleanup_sidecar_endpoint(endpoint)

        asyncio.run(exercise())


if __name__ == "__main__":
    unittest.main()
