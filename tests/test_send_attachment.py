from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.channel import ChannelRuntime, EndpointConfig, ResponseHandle, register_with_core as register_channel_with_core
from pal.channel.channel_endpoint_queue_base import ChannelEndpointQueueBase
from pal.channel.endpoints.socket_endpoint import SocketChannelEndpoint
from pal.channel.endpoints.telegram_endpoint import TelegramChannelEndpoint, _telegram_markdown
from pal.core import PalCore, register_with_core as register_core_with_core
from pal.execution import register_with_core as register_execution_with_core
from pal.foundation import AttachmentSpec, EventEnvelope
from pal.llm import CanonicalToolCall
from pal.shared import EventKind, SourceKind


class _AttachmentEndpoint(ChannelEndpointQueueBase):
    def __init__(self, endpoint: EndpointConfig) -> None:
        super().__init__(endpoint=endpoint)
        self.attachments: list[tuple[ResponseHandle, AttachmentSpec]] = []

    def normalize_raw(self, payload):
        return dict(payload or {})

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        _ = (response_handle, text)

    def send_attachment(self, response_handle: ResponseHandle, attachment: AttachmentSpec) -> None:
        self.attachments.append((response_handle, attachment))

    def inspect_health(self) -> dict[str, object]:
        return {"healthy": True}

    def inspect_auth_state(self) -> dict[str, object]:
        return {"authorized": True}


class _FakeTelegramBot:
    def __init__(self) -> None:
        self.documents: list[dict[str, object]] = []
        self.messages: list[dict[str, object]] = []

    async def send_message(self, **kwargs) -> None:
        self.messages.append(dict(kwargs))

    async def send_document(self, **kwargs) -> None:
        document = kwargs.get("document")
        self.documents.append(dict(kwargs))
        assert document is not None
        assert not document.closed


class SendAttachmentTests(unittest.IsolatedAsyncioTestCase):
    def _build_core_with_channel(self):
        core = PalCore()
        register_core_with_core(core)
        register_execution_with_core(core.context)
        channel_runtime = ChannelRuntime()
        endpoint = _AttachmentEndpoint(
            endpoint=EndpointConfig(endpoint_id="socket_main", channel_kind="socket", binding_key="runtime.sock")
        )
        channel_runtime.register_endpoint(endpoint)
        register_channel_with_core(core.context, channel_runtime)
        return core, channel_runtime, endpoint

    def _start_turn(self, core: PalCore, endpoint: _AttachmentEndpoint):
        envelope = endpoint.emit_normalized(
            EventEnvelope(
                event_kind=EventKind.USER_MESSAGE,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "send file"},
                correlation_id="turn-attachment",
            ),
            response_handle=ResponseHandle(
                endpoint_id=endpoint.endpoint.endpoint_id,
                reply_target={"session_id": "sess-1", "request_id": "req-1"},
            ),
        )
        assert envelope is not None
        return core.turn_manager.start(envelope)

    async def test_core_send_attachment_for_turn_queues_to_channel(self) -> None:
        core, channel_runtime, endpoint = self._build_core_with_channel()
        continuation = self._start_turn(core, endpoint)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.txt"
            path.write_text("hello", encoding="utf-8")

            result = await core.send_attachment_for_turn(
                continuation.turn_id,
                AttachmentSpec(path=str(path), caption="Report"),
            )

            self.assertEqual(result.status, "ok")
            self.assertTrue(endpoint.has_queued_attachments())
            channel_runtime.sync_endpoints()
            self.assertEqual(len(endpoint.attachments), 1)
            _, attachment = endpoint.attachments[0]
            self.assertEqual(attachment.file_name, "report.txt")
            self.assertEqual(attachment.caption, "Report")

    async def test_execution_tool_uses_core_turn_io_provider(self) -> None:
        core, channel_runtime, endpoint = self._build_core_with_channel()
        continuation = self._start_turn(core, endpoint)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "artifact.txt"
            path.write_text("artifact", encoding="utf-8")

            result = await core.context.execution_runtime.execute_tool_async(
                CanonicalToolCall(name="op_channel_send_attachment", args={"path": str(path), "caption": "Artifact"}),
                turn_id=continuation.turn_id,
            )

            self.assertTrue(result.ok)
            self.assertEqual(result.status, "ok")
            channel_runtime.sync_endpoints()
            self.assertEqual(len(endpoint.attachments), 1)
            self.assertEqual(endpoint.attachments[0][1].caption, "Artifact")

    async def test_execution_tool_reports_structured_failures(self) -> None:
        core, _, endpoint = self._build_core_with_channel()
        self._start_turn(core, endpoint)

        missing_turn = await core.context.execution_runtime.execute_tool_async(
            CanonicalToolCall(name="op_channel_send_attachment", args={"path": "missing.txt"}),
            turn_id=None,
        )

        self.assertFalse(missing_turn.ok)
        self.assertEqual(missing_turn.structured["reason"], "turn_id_required")

    async def test_socket_endpoint_sends_attachment_payload(self) -> None:
        endpoint = SocketChannelEndpoint(
            endpoint=EndpointConfig(endpoint_id="socket_main", channel_kind="socket", binding_key="runtime.sock")
        )
        outbound: asyncio.Queue[dict[str, object]] = asyncio.Queue()
        endpoint.sessions["sess-1"] = SimpleNamespace(outbound=outbound, closed=False)

        endpoint.send_attachment(
            ResponseHandle(endpoint_id="socket_main", reply_target={"session_id": "sess-1", "request_id": "req-1"}),
            AttachmentSpec(path="/tmp/report.txt", caption="Report", file_name="report.txt", mime_type="text/plain"),
        )

        payload = outbound.get_nowait()
        self.assertEqual(payload["type"], "attachment")
        self.assertEqual(payload["request_id"], "req-1")
        self.assertEqual(payload["path"], "/tmp/report.txt")
        self.assertEqual(payload["file_name"], "report.txt")

    async def test_telegram_endpoint_sends_document(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(endpoint_id="telegram_main", channel_kind="telegram", binding_key="chat:42")
        )
        bot = _FakeTelegramBot()
        endpoint.application = SimpleNamespace(bot=bot)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "report.txt"
            path.write_text("hello", encoding="utf-8")

            await endpoint._send_attachment_async(
                ResponseHandle(endpoint_id="telegram_main", reply_target={"chat_id": "42", "thread_id": "7"}),
                AttachmentSpec(path=str(path), caption="Report"),
            )

        self.assertEqual(len(bot.documents), 1)
        self.assertEqual(bot.documents[0]["chat_id"], 42)
        self.assertEqual(bot.documents[0]["message_thread_id"], 7)
        self.assertEqual(bot.documents[0]["filename"], "report.txt")
        self.assertEqual(bot.documents[0]["caption"], "Report")

    async def test_telegram_reply_requeues_when_application_is_not_running(self) -> None:
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(endpoint_id="telegram_main", channel_kind="telegram", binding_key="chat:42")
        )
        handle = ResponseHandle(endpoint_id="telegram_main", reply_target={"chat_id": "42"})
        endpoint.queue_reply("hello", response_handle=handle)

        emitted = endpoint.flush_outbox()

        self.assertEqual(len(endpoint.outbox), 1)
        self.assertEqual(emitted[0].event_kind, EventKind.REPLY_FAILED)
        self.assertIn("not running", emitted[0].payload["reason"])

        bot = _FakeTelegramBot()
        endpoint.application = SimpleNamespace(bot=bot)
        endpoint.flush_outbox()
        await asyncio.sleep(0.05)

        self.assertFalse(endpoint.outbox)
        self.assertEqual(str(bot.messages[0]["text"]).strip(), "hello")

    def test_telegram_markdown_flattens_gfm_tables_to_readable_lists(self) -> None:
        rendered, mode = _telegram_markdown(
            "# Pal Report\n\n"
            "| Item | Status | Detail |\n"
            "|------|--------|--------|\n"
            "| **Core** | OK | default |\n"
            "| MCP | OK | 8 tools |\n"
        )

        self.assertEqual(mode, "MarkdownV2")
        self.assertNotIn("```", rendered)
        self.assertNotIn("\\| Item", rendered)
        self.assertIn("Status:", rendered)
        self.assertIn("Detail:", rendered)
        self.assertIn("Core", rendered)

    def test_send_attachment_is_discoverable_but_not_resident_llm_tool(self) -> None:
        core, _, _ = self._build_core_with_channel()
        core.publish_module_capabilities("execution")
        core.publish_module_capabilities("channel")

        names = {
            contract["function"]["name"]
            for contract in core.tool_surface.build_llm_tool_contracts()
        }

        self.assertNotIn("op_channel_send_attachment", names)
        search = core.context.execution_runtime.execute_tool(
            CanonicalToolCall(name="op_tool_search", args={"query": "send attachment", "top_k": 5})
        )
        self.assertTrue(search.ok)
        self.assertEqual(search.structured["hits"][0]["name"], "op_channel_send_attachment")
        self.assertNotIn("aliases", search.structured["hits"][0])


if __name__ == "__main__":
    unittest.main()
