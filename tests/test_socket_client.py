from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

from pal.socket_client import send_message


class _FakeWriter:
    def __init__(self) -> None:
        self.written = b""
        self.closed = False

    def write(self, data: bytes) -> None:
        self.written += data

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True

    async def wait_closed(self) -> None:
        return None


class PalV2SocketClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_llm_done_stop_finishes_streamed_reply(self) -> None:
        writer = _FakeWriter()
        events = iter(
            [
                {"type": "text_delta", "request_id": "req-1", "text": "pong"},
                {"type": "llm_done", "request_id": "req-1", "finish_reason": "stop"},
            ]
        )

        async def fake_open_unix_connection(_path: str):
            return object(), writer

        async def fake_read_socket_message(_reader):
            return next(events)

        with patch("pal.socket_client.uuid4", return_value="req-1"):
            with patch("pal.socket_client.asyncio.open_unix_connection", side_effect=fake_open_unix_connection):
                with patch("pal.socket_client.read_socket_message", side_effect=fake_read_socket_message):
                    transcript = await send_message(Path("/tmp/pal.sock"), "ping")

        self.assertEqual(transcript.text_parts, ["pong"])
        self.assertEqual(transcript.finish_reason, "stop")
        self.assertTrue(writer.closed)

    async def test_llm_done_tool_calls_does_not_finish_turn(self) -> None:
        writer = _FakeWriter()
        events = iter(
            [
                {
                    "type": "tool_call",
                    "request_id": "req-1",
                    "tool_call": {"name": "op_probe", "args": {}},
                },
                {"type": "llm_done", "request_id": "req-1", "finish_reason": "tool_calls"},
                {"type": "text_delta", "request_id": "req-1", "text": "done"},
                {"type": "llm_done", "request_id": "req-1", "finish_reason": "stop"},
            ]
        )

        async def fake_open_unix_connection(_path: str):
            return object(), writer

        async def fake_read_socket_message(_reader):
            return next(events)

        with patch("pal.socket_client.uuid4", return_value="req-1"):
            with patch("pal.socket_client.asyncio.open_unix_connection", side_effect=fake_open_unix_connection):
                with patch("pal.socket_client.read_socket_message", side_effect=fake_read_socket_message):
                    transcript = await send_message(Path("/tmp/pal.sock"), "ping")

        self.assertEqual([tool["name"] for tool in transcript.tool_calls], ["op_probe"])
        self.assertEqual(transcript.text_parts, ["done"])
        self.assertEqual(transcript.finish_reason, "stop")
