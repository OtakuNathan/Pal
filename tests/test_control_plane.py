from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from pal.channel import ChannelRuntime, register_with_core as register_channel_with_core
from pal.channel.channel_endpoint_queue_base import ChannelEndpointQueueBase
from pal.channel.contracts import ChannelEnvelope, EndpointConfig, ResponseHandle
from pal.channel.endpoints.telegram_endpoint import TelegramChannelEndpoint
from pal.control import ControlAction, ControlCommandSpec, ControlEvent, ControlPlane, ControlRoute, register_with_core as register_control_with_core
from pal.core import PalCore, TurnContinuation, register_with_core as register_core_with_core
from pal.core.turns import channel_turn_program
from pal.foundation import EventEnvelope
from pal.llm.repository import DEFAULT_THINK_LEVEL
from pal.memory import MemoryService, register_with_core as register_memory_with_core
from pal.shared import EventKind, PromptAssemblyContext, RuntimeStatus, SourceKind
from pal.stream_events import NormalizedLLMStreamEvent


class _StubEndpoint(ChannelEndpointQueueBase):
    def normalize_raw(self, payload):
        return dict(payload or {})

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        _ = (response_handle, text)

    def inspect_health(self) -> dict[str, object]:
        return {"healthy": True}

    def inspect_auth_state(self) -> dict[str, object]:
        return {"authorized": True}


@dataclass
class _FakeSettingsRepository:
    think_level: str = DEFAULT_THINK_LEVEL

    def set_think_level(self, think_level: str) -> None:
        self.think_level = str(think_level)


@dataclass
class _FakeLLMRuntime:
    settings_repository: _FakeSettingsRepository
    think_level: str = DEFAULT_THINK_LEVEL

    def refresh_runtime_settings(self) -> None:
        self.think_level = self.settings_repository.think_level


class _BlockingInterruptHandle:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def cancel(self) -> None:
        self.calls += 1
        self.started.set()
        await self.release.wait()


class ControlPlaneTests(unittest.TestCase):
    def test_channel_ingress_promotes_slash_text_to_slash_command(self) -> None:
        endpoint = _StubEndpoint(
            endpoint=EndpointConfig(endpoint_id="socket_main", channel_kind="socket", binding_key="runtime.sock")
        )

        envelope = endpoint.accept_raw(
            {"text": "/think", "session_id": "sess-1", "request_id": "req-1"},
            event_kind=EventKind.USER_MESSAGE,
            correlation_id="req-1",
            reply_target={"session_id": "sess-1", "request_id": "req-1"},
        )

        self.assertIsNotNone(envelope)
        assert envelope is not None
        self.assertEqual(envelope.event.event_kind, EventKind.SLASH_COMMAND)

    def test_dynamic_command_registration_and_panel_rendering(self) -> None:
        plane = ControlPlane()
        plane.register_command(
            ControlCommandSpec(
                name="ping",
                handler=lambda invocation: ControlAction(
                    action_kind="invoke_capability",
                    target_scope="execution",
                    target_id="op_demo_ping",
                    args={"value": "pong"},
                    route=invocation.route,
                ),
                description="Ping a capability.",
                usage="/ping",
                show_in_panel=True,
                panel_button=True,
            )
        )

        rendered = plane.render_panel_text()

        self.assertIn("/control", rendered)
        self.assertIn("/think [off|low|balanced|deep]", rendered)
        self.assertIn("/ping", rendered)

    def test_think_without_argument_shows_current_level(self) -> None:
        plane = ControlPlane()
        action = plane.parse_event(
            ControlEvent(
                event_kind=EventKind.SLASH_COMMAND,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "/think"},
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_kind, "show_think")

    def test_think_alias_normalizes_to_canonical_level(self) -> None:
        plane = ControlPlane()
        action = plane.parse_event(
            ControlEvent(
                event_kind=EventKind.SLASH_COMMAND,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "/think high"},
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_kind, "set_think")
        self.assertEqual(action.args["think_level"], "deep")


class PalControlFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.core = PalCore()
        register_core_with_core(self.core)

        self.channel_runtime = ChannelRuntime()
        self.endpoint = _StubEndpoint(
            endpoint=EndpointConfig(endpoint_id="socket_main", channel_kind="socket", binding_key="runtime.sock")
        )
        self.channel_runtime.register_endpoint(self.endpoint)
        register_channel_with_core(self.core.context, self.channel_runtime)

        self.settings_repository = _FakeSettingsRepository()
        self.llm_runtime = _FakeLLMRuntime(settings_repository=self.settings_repository)
        self.core.context.port_registry["llm:llm"] = self.llm_runtime

        self.memory_service = MemoryService()
        register_memory_with_core(self.core.context, self.memory_service)

        self.control_plane = ControlPlane()
        register_control_with_core(self.core.context, self.control_plane)

        self.route = ControlRoute(
            endpoint_id="socket_main",
            channel_kind="socket",
            reply_target={"session_id": "sess-1", "request_id": "req-1"},
            control_scope_key="socket:socket_main:sess-1",
            correlation_id="req-1",
        )

    async def test_set_think_updates_future_turn_snapshot_only(self) -> None:
        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="set_think",
                target_scope="runtime",
                args={"think_level": "deep"},
                route=self.route,
            )
        )
        envelope = ChannelEnvelope(
            event=EventEnvelope(
                event_kind=EventKind.USER_MESSAGE,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "hello", "session_id": "sess-1", "request_id": "req-1"},
            ),
            endpoint=self.endpoint.endpoint,
            response_handle=ResponseHandle(endpoint_id="socket_main", reply_target={"session_id": "sess-1", "request_id": "req-1"}),
        )
        continuation = self.core.turn_manager.start(envelope)

        self.settings_repository.set_think_level("low")
        self.llm_runtime.refresh_runtime_settings()
        prompt = self.core.turn_executor.build_turn_prompt(
            continuation,
            PromptAssemblyContext(event=envelope.event, core_mode="default"),
            max_output_tokens=64,
        )

        self.assertEqual(continuation.turn_settings_snapshot["think_level"], "deep")
        self.assertEqual(prompt.metadata["think_level"], "deep")

    async def test_core_publishes_control_catalog_to_channel_endpoint(self) -> None:
        await self.core.publish_control_catalog_async(endpoint_id="socket_main")

        self.assertTrue(self.endpoint.has_queued_status())
        queued = list(self.endpoint.status_outbox)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].kind, "control_catalog")
        commands = list(queued[0].payload.get("commands") or [])
        self.assertTrue(any(item.get("command") == "control" for item in commands))

    async def test_reset_request_is_deduplicated_and_expires(self) -> None:
        action = ControlAction(
            action_kind="open_reset_confirm",
            target_scope="memory",
            route=self.route,
        )

        await self.core.handle_control_action_async(action)
        scope_state = self.core.state.control_scopes[self.route.control_scope_key]
        first_request = scope_state.pending_requests["reset_confirm"]

        await self.core.handle_control_action_async(action)
        second_request = scope_state.pending_requests["reset_confirm"]

        self.assertEqual(first_request.request_id, second_request.request_id)

        second_request.expires_at = "2000-01-01T00:00:00+00:00"
        await self.core.expire_pending_control_requests_async()
        self.assertNotIn("reset_confirm", scope_state.pending_requests)

    async def test_interrupt_by_scope_marks_turn_and_aborts_stream(self) -> None:
        response_handle = ResponseHandle(
            endpoint_id="socket_main",
            reply_target={"session_id": "sess-1", "request_id": "req-1"},
        )
        envelope = ChannelEnvelope(
            event=EventEnvelope(
                event_kind=EventKind.USER_MESSAGE,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "hello", "session_id": "sess-1", "request_id": "req-1"},
            ),
            endpoint=self.endpoint.endpoint,
            response_handle=response_handle,
        )
        scope_key = self.route.control_scope_key
        continuation = TurnContinuation(
            turn_id="turn-1",
            channel_envelope=envelope,
            program=channel_turn_program(envelope, core_mode="default", max_output_tokens=64),
            correlation_id="req-1",
            control_scope_key=scope_key,
            turn_settings_snapshot={"think_level": "balanced"},
        )
        self.core.state.active_turns["turn-1"] = continuation
        self.core.state.turn_scopes["turn-1"] = scope_key
        scope_state = self.core._ensure_scope_state(scope_key)
        scope_state.active_turn_id = "turn-1"
        scope_state.drained_event.clear()
        self.endpoint.queue_stream_event(
            NormalizedLLMStreamEvent(event_kind="text_delta", text="hello"),
            response_handle=response_handle,
        )
        task = asyncio.create_task(asyncio.sleep(10))
        self.core.state.turn_tasks["turn-1"] = task

        interrupted = await self.core.turn_manager.interrupt_by_scope(scope_key)

        self.assertTrue(interrupted)
        self.assertTrue(continuation.interrupted)
        self.assertTrue(self.endpoint._stream_sessions[id(response_handle)]["closed"])
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_interrupt_by_scope_deduplicates_concurrent_interrupts(self) -> None:
        response_handle = ResponseHandle(
            endpoint_id="socket_main",
            reply_target={"session_id": "sess-1", "request_id": "req-1"},
        )
        envelope = ChannelEnvelope(
            event=EventEnvelope(
                event_kind=EventKind.USER_MESSAGE,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "hello", "session_id": "sess-1", "request_id": "req-1"},
            ),
            endpoint=self.endpoint.endpoint,
            response_handle=response_handle,
        )
        scope_key = self.route.control_scope_key
        continuation = TurnContinuation(
            turn_id="turn-2",
            channel_envelope=envelope,
            program=channel_turn_program(envelope, core_mode="default", max_output_tokens=64),
            correlation_id="req-2",
            control_scope_key=scope_key,
            turn_settings_snapshot={"think_level": "balanced"},
        )
        self.core.state.active_turns["turn-2"] = continuation
        self.core.state.turn_scopes["turn-2"] = scope_key
        scope_state = self.core._ensure_scope_state(scope_key)
        scope_state.active_turn_id = "turn-2"
        scope_state.drained_event.clear()
        task = asyncio.create_task(asyncio.sleep(10))
        self.core.state.turn_tasks["turn-2"] = task
        handle = _BlockingInterruptHandle()
        self.core.context.execution_runtime.register_interrupt_handle("turn-2", handle)

        first = asyncio.create_task(self.core.turn_manager.interrupt_by_scope(scope_key))
        await handle.started.wait()
        second = asyncio.create_task(self.core.turn_manager.interrupt_by_scope(scope_key))
        await asyncio.sleep(0)

        self.assertEqual(handle.calls, 1)
        handle.release.set()

        self.assertTrue(await first)
        self.assertTrue(await second)
        self.assertIsNone(scope_state.interrupt_task)
        self.assertIsNone(scope_state.interrupting_turn_id)
        with self.assertRaises(asyncio.CancelledError):
            await task


class TelegramControlBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_tg_control_test_"))
        self.endpoint = TelegramChannelEndpoint(
            endpoint=EndpointConfig(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                binding_key="user:42",
                send_policy={},
            ),
            runtime_root=self.runtime_root,
            bot_token="token",
        )

    async def test_callback_query_is_normalized_to_slash_command(self) -> None:
        class _User:
            id = 42

        class _Chat:
            id = 100

        class _Message:
            chat = _Chat()
            message_id = 12
            message_thread_id = None

        class _CallbackQuery:
            data = "ctl:/interrupt"
            message = _Message()
            from_user = _User()
            id = "cb-1"

            async def answer(self) -> None:
                return None

        class _Update:
            callback_query = _CallbackQuery()

        payload = await self.endpoint._control_payload_from_update(_Update())

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["text"], "/interrupt")
        self.assertEqual(payload["source_metadata"]["callback_data"], "control")
