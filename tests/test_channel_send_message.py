from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, new_tool_call

import asyncio
from types import SimpleNamespace
import unittest

from pal.channel import (
    ChannelRuntime,
    EndpointConfig,
    ResponseHandle,
    register_with_core as register_channel_with_core,
)
from pal.channel.channel_endpoint_queue_base import ChannelEndpointQueueBase
from pal.channel.contracts import ChannelDeliveryError
from pal.core import PalCore, register_with_core as register_core_with_core
from pal.execution import register_with_core as register_execution_with_core
from tests.runtime_channel_providers import telegram_endpoint_module


TelegramChannelEndpoint = telegram_endpoint_module().TelegramChannelEndpoint


class _ActiveMessageEndpoint(ChannelEndpointQueueBase):
    def __init__(self, endpoint: EndpointConfig) -> None:
        super().__init__(endpoint=endpoint)
        self.sent: list[tuple[ResponseHandle, str]] = []

    def normalize_raw(self, payload):
        return dict(payload or {})

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        self.sent.append((response_handle, text))

    def derive_default_reply_target(self) -> dict[str, str]:
        return {"recipient": "bound-recipient"}

    def inspect_health(self) -> dict[str, object]:
        return {"healthy": True}

    def inspect_auth_state(self) -> dict[str, object]:
        return {"authorized": True}


class _ReplyOnlyEndpoint(_ActiveMessageEndpoint):
    def derive_default_reply_target(self) -> dict[str, str]:
        return {}


class _TelegramBot:
    def __init__(self) -> None:
        self.messages: list[dict[str, object]] = []

    async def send_message(self, **kwargs) -> None:
        self.messages.append(dict(kwargs))


class ChannelSendMessageTests(unittest.IsolatedAsyncioTestCase):
    def _build_core(self):
        core = PalCore()
        register_core_with_core(core)
        register_execution_with_core(core.context)
        runtime = ChannelRuntime()
        endpoint = _ActiveMessageEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="channel-main",
                channel_kind="test",
                binding_key="bound",
            )
        )
        runtime.register_endpoint(endpoint)
        register_channel_with_core(core.context, runtime)
        core.publish_module_capabilities("execution")
        core.publish_module_capabilities("channel")
        return core, runtime, endpoint

    async def test_runtime_resolves_bound_target_and_queues_message(self) -> None:
        runtime = ChannelRuntime()
        endpoint = _ActiveMessageEndpoint(
            endpoint=EndpointConfig("channel-main", "test", "bound")
        )
        runtime.register_endpoint(endpoint)

        receipt = await runtime.send_message("channel-main", "hello")
        self.assertEqual(receipt.endpoint_id, "channel-main")
        self.assertEqual(receipt.status, "accepted")
        self.assertTrue(receipt.message_id)

        runtime.sync_endpoints()
        self.assertEqual(len(endpoint.sent), 1)
        handle, text = endpoint.sent[0]
        self.assertEqual(text, "hello")
        self.assertEqual(handle.reply_target, {"recipient": "bound-recipient"})

    async def test_runtime_rejects_unavailable_or_reply_only_endpoint(self) -> None:
        runtime = ChannelRuntime()
        with self.assertRaises(ChannelDeliveryError) as missing:
            await runtime.send_message("missing", "hello")
        self.assertEqual(missing.exception.reason, "channel_not_found")

        endpoint = _ReplyOnlyEndpoint(
            endpoint=EndpointConfig("reply-only", "socket", "runtime.sock")
        )
        runtime.register_endpoint(endpoint)
        with self.assertRaises(ChannelDeliveryError) as unsupported:
            await runtime.send_message("reply-only", "hello")
        self.assertEqual(unsupported.exception.reason, "active_send_unsupported")

        endpoint.attached = False
        with self.assertRaises(ChannelDeliveryError) as detached:
            await runtime.send_message("reply-only", "hello")
        self.assertEqual(detached.exception.reason, "channel_detached")

    async def test_telegram_uses_endpoint_binding_as_active_target(self) -> None:
        runtime = ChannelRuntime()
        endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig("telegram-main", "telegram", "chat:42")
        )
        bot = _TelegramBot()
        endpoint.application = SimpleNamespace(bot=bot)
        runtime.register_endpoint(endpoint)

        receipt = await runtime.send_message("telegram-main", "bound message")
        self.assertEqual(receipt.status, "accepted")
        runtime.sync_endpoints()
        await asyncio.sleep(0.05)

        self.assertEqual(len(bot.messages), 1)
        self.assertEqual(bot.messages[0]["chat_id"], 42)
        self.assertEqual(str(bot.messages[0]["text"]).strip(), "bound message")

    async def test_llm_tool_is_indirect_and_uses_only_channel_id_and_message(self) -> None:
        core, runtime, endpoint = self._build_core()
        direct_names = {
            contract["function"]["name"]
            for contract in core.tool_surface.build_llm_tool_contracts()
        }
        self.assertNotIn("channel_send_message", direct_names)

        search = core.context.execution_runtime.execute_tool(
            new_tool_call(
                name="search_tools",
                args={"query": "send message to channel endpoint", "top_k": 5},
            )
        )
        self.assertTrue(search.ok)
        self.assertIn(
            "channel_send_message",
            [hit["alias"] for hit in search.structured["hits"]],
        )
        read = core.context.execution_runtime.execute_tool(
            new_tool_call(
                name="read_tool",
                args={"name": "channel_send_message"},
            )
        )
        self.assertTrue(read.ok)
        schema = read.structured["input_schema"]
        self.assertEqual(set(schema["properties"]), {"name", "message"})
        self.assertEqual(set(schema["required"]), {"name", "message"})
        self.assertFalse(schema["additionalProperties"])

        result = await core.context.execution_runtime.execute_tool_async(
            new_tool_call(
                name="call_tool",
                args={
                    "name": "channel_send_message",
                    "args": {
                        "name": "channel-main",
                        "message": "hello from Pal",
                    },
                },
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "ok")
        self.assertEqual(
            set(result.structured),
            {"channel_id", "message_id", "status"},
        )
        self.assertEqual(result.structured["status"], "accepted")
        runtime.sync_endpoints()
        self.assertEqual(endpoint.sent[0][1], "hello from Pal")

    async def test_llm_tool_rejects_slash_command(self) -> None:
        core, _, endpoint = self._build_core()
        result = await core.context.execution_runtime.execute_tool_async(
            new_tool_call(
                name="call_tool",
                args={
                    "name": "channel_send_message",
                    "args": {"name": "channel-main", "message": "/status"},
                },
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "invalid")
        self.assertEqual(endpoint.sent, [])

    async def test_current_websocket_peer_reply_must_use_normal_final(self) -> None:
        core, runtime, _ = self._build_core()
        peer_endpoint = _ActiveMessageEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="petra",
                channel_kind="websocket_bridge",
                binding_key="lan:peer",
            )
        )
        runtime.register_endpoint(peer_endpoint)
        core.state.active_turns["peer-turn"] = SimpleNamespace(
            delivery_binding=SimpleNamespace(endpoint=peer_endpoint.endpoint)
        )

        result = await core.context.execution_runtime.execute_tool_async(
            new_tool_call(
                name="call_tool",
                args={
                    "name": "channel_send_message",
                    "args": {
                        "name": "petra",
                        "message": "this would recursively start another peer turn",
                    },
                },
            ),
            turn_id="peer-turn",
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.status, "forbidden")
        self.assertEqual(
            result.structured["details"]["reason"],
            "peer_reply_must_use_final",
        )
        runtime.sync_endpoints()
        self.assertEqual(peer_endpoint.sent, [])


if __name__ == "__main__":
    unittest.main()
