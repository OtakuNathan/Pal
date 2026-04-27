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
from pal.control import (
    ControlAction,
    ControlCommandSpec,
    ControlEvent,
    ControlPlane,
    ControlRoute,
    InteractionResult,
    register_with_core as register_control_with_core,
)
from pal.core import PalCore, TurnContinuation, register_with_core as register_core_with_core
from pal.core.contracts import PendingControlRequest
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

    def send_status(self, response_handle: ResponseHandle, kind: str, payload: dict[str, object]) -> None:
        if not hasattr(self, "sent_statuses"):
            self.sent_statuses = []
        self.sent_statuses.append((str(kind), dict(payload), dict(response_handle.reply_target)))

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

    def test_unknown_interaction_action_normalizes_to_invalid_command(self) -> None:
        plane = ControlPlane()
        route = ControlRoute(
            endpoint_id="telegram_main",
            channel_kind="telegram",
            reply_target={"chat_id": "42"},
            control_scope_key="tg:telegram_main:42:root",
        )

        action = plane.handle_interaction(
            InteractionResult(
                interaction_id="ix-1",
                interaction_kind="control_panel",
                action_key="control.unknown.action",
                route=route,
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_kind, "invalid_command")
        self.assertEqual(action.args["interaction_origin"], "button")


class PalControlFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.core = PalCore()
        register_core_with_core(self.core)

        self.channel_runtime = ChannelRuntime()
        self.endpoint = _StubEndpoint(
            endpoint=EndpointConfig(endpoint_id="socket_main", channel_kind="socket", binding_key="runtime.sock")
        )
        self.endpoint.sent_statuses = []
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

    def _make_channel_envelope(
        self,
        *,
        turn_id: str,
        request_id: str,
        session_id: str = "sess-1",
        text: str = "hello",
    ) -> ChannelEnvelope:
        return ChannelEnvelope(
            event=EventEnvelope(
                event_kind=EventKind.USER_MESSAGE,
                source_kind=SourceKind.CHANNEL,
                payload={"text": text, "session_id": session_id, "request_id": request_id},
                correlation_id=request_id,
                event_id=turn_id,
            ),
            endpoint=self.endpoint.endpoint,
            response_handle=ResponseHandle(
                endpoint_id="socket_main",
                reply_target={"session_id": session_id, "request_id": request_id},
            ),
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

    async def test_quiescing_scope_does_not_create_background_turn_task(self) -> None:
        scope_state = self.core._ensure_scope_state(self.route.control_scope_key)
        scope_state.quiescing = True
        envelope = ChannelEnvelope(
            event=EventEnvelope(
                event_kind=EventKind.USER_MESSAGE,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "hello", "session_id": "sess-1", "request_id": "req-1"},
            ),
            endpoint=self.endpoint.endpoint,
            response_handle=ResponseHandle(endpoint_id="socket_main", reply_target={"session_id": "sess-1", "request_id": "req-1"}),
        )

        await self.core.schedule_channel_turn_async(envelope)

        self.assertNotIn(envelope.event.event_id, self.core.state.turn_tasks)
        self.assertTrue(self.endpoint.has_queued_replies())
        self.assertEqual(self.endpoint.outbox[-1].text, "This scope is resetting. Please retry in a moment.")
        self.assertTrue(self.endpoint.has_queued_status())
        self.assertEqual(self.endpoint.status_outbox[-1].kind, "working_stop")

    async def test_control_action_emits_working_stop(self) -> None:
        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="show_think",
                target_scope="runtime",
                route=self.route,
            )
        )

        self.assertTrue(self.endpoint.has_queued_status())
        self.assertEqual(self.endpoint.status_outbox[-1].kind, "working_stop")

    async def test_run_until_idle_flushes_working_stop_after_background_turn_completion(self) -> None:
        envelope = ChannelEnvelope(
            event=EventEnvelope(
                event_kind=EventKind.USER_MESSAGE,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "hello", "session_id": "sess-1", "request_id": "req-1"},
            ),
            endpoint=self.endpoint.endpoint,
            response_handle=ResponseHandle(endpoint_id="socket_main", reply_target={"session_id": "sess-1", "request_id": "req-1"}),
        )

        async def _complete_turn(channel_envelope):
            _ = channel_envelope
            await asyncio.sleep(0)
            return None

        self.core.process_channel_turn_async = _complete_turn  # type: ignore[method-assign]

        await self.core.schedule_channel_turn_async(envelope)
        await self.core.run_until_idle_async(max_iterations=32)

        self.assertTrue(any(kind == "working_stop" for kind, _, _ in self.endpoint.sent_statuses))

    async def test_button_backed_set_think_resolves_interaction(self) -> None:
        route = ControlRoute(
            endpoint_id="telegram_main",
            channel_kind="telegram",
            reply_target={"chat_id": "42", "message_id": "12"},
            control_scope_key="tg:telegram_main:42:root",
            correlation_id="tg-1",
        )

        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="set_think",
                target_scope="runtime",
                args={
                    "think_level": "deep",
                    "interaction_origin": "button",
                    "interaction_id": "ctl_panel_1",
                    "interaction_kind": "control_panel",
                },
                route=route,
            )
        )

        statuses = list(self.channel_runtime.status_outbox)
        self.assertGreaterEqual(len(statuses), 2)
        self.assertEqual(statuses[-2].kind, "interactive_resolve")
        self.assertEqual(statuses[-2].payload["spec"].text, "Think level updated to deep. This applies to new turns only.")
        self.assertEqual(statuses[-1].kind, "working_stop")

    async def test_button_backed_interrupt_resolves_interaction(self) -> None:
        route = ControlRoute(
            endpoint_id="telegram_main",
            channel_kind="telegram",
            reply_target={"chat_id": "42", "message_id": "13"},
            control_scope_key="tg:telegram_main:42:root",
            correlation_id="tg-2",
        )

        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="interrupt_turn",
                target_scope="runtime",
                args={
                    "interaction_origin": "button",
                    "interaction_id": "ctl_panel_2",
                    "interaction_kind": "control_panel",
                },
                route=route,
            )
        )

        statuses = list(self.channel_runtime.status_outbox)
        self.assertGreaterEqual(len(statuses), 2)
        self.assertEqual(statuses[-2].kind, "interactive_resolve")
        self.assertEqual(statuses[-2].payload["spec"].text, "No active turn to interrupt in this scope.")
        self.assertEqual(statuses[-1].kind, "working_stop")

    async def test_control_panel_buttons_are_driven_by_command_specs(self) -> None:
        self.control_plane.register_command(
            ControlCommandSpec(
                name="ping",
                handler=lambda invocation: ControlAction(
                    action_kind="invoke_capability",
                    target_scope="execution",
                    target_id="op_demo_ping",
                    route=invocation.route,
                ),
                description="Ping a capability.",
                usage="/ping",
                show_in_panel=True,
                panel_button=True,
                panel_label="Ping Plugin",
            )
        )

        spec = self.core._build_control_panel_interaction(self.control_plane, self.route)
        flattened = [button for row in spec.buttons for button in row]

        self.assertTrue(any(button.label == "Think" and button.action_key == "control.think.open" for button in flattened))
        self.assertTrue(any(button.label == "Compact" and button.action_key == "control.compact.run" for button in flattened))
        self.assertTrue(any(button.label == "Interrupt" and button.action_key == "control.interrupt.run" for button in flattened))
        self.assertTrue(any(button.label == "Reset Memory" and button.action_key == "control.reset.open" for button in flattened))
        self.assertTrue(
            any(
                button.label == "Ping Plugin"
                and button.action_key == "control.command.run"
                and button.action_args == {"command_name": "ping"}
                for button in flattened
            )
        )

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

    async def test_same_scope_user_turns_are_serialized_through_pending_queue(self) -> None:
        first_started = asyncio.Event()
        first_release = asyncio.Event()
        second_started = asyncio.Event()
        second_release = asyncio.Event()
        started: list[str] = []

        async def _blocking_turn(channel_envelope):
            turn_id = channel_envelope.event.event_id
            started.append(turn_id)
            if turn_id == "turn-queue-1":
                first_started.set()
                await first_release.wait()
            if turn_id == "turn-queue-2":
                second_started.set()
                await second_release.wait()
            return None

        self.core.process_channel_turn_async = _blocking_turn  # type: ignore[method-assign]
        first = self._make_channel_envelope(turn_id="turn-queue-1", request_id="req-queue-1")
        second = self._make_channel_envelope(turn_id="turn-queue-2", request_id="req-queue-2")

        await self.core.schedule_channel_turn_async(first)
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await self.core.schedule_channel_turn_async(second)

        scope_state = self.core._ensure_scope_state(self.route.control_scope_key)
        self.assertEqual(scope_state.active_turn_id, "turn-queue-1")
        self.assertEqual(len(scope_state.pending_channel_turns), 1)
        self.assertNotIn("turn-queue-2", self.core.state.turn_tasks)
        self.assertEqual(self.endpoint.status_outbox[-1].kind, "working_stop")

        first_release.set()
        await asyncio.wait_for(second_started.wait(), timeout=1.0)

        self.assertEqual(started, ["turn-queue-1", "turn-queue-2"])
        self.assertEqual(scope_state.active_turn_id, "turn-queue-2")
        self.assertEqual(len(scope_state.pending_channel_turns), 0)
        self.assertIn("turn-queue-2", self.core.state.turn_tasks)
        self.assertTrue(any(item.kind == "typing_start" for item in self.endpoint.status_outbox))

        second_task = self.core.state.turn_tasks["turn-queue-2"]
        second_release.set()
        await asyncio.wait_for(second_task, timeout=1.0)
        await asyncio.sleep(0)
        self.assertIsNone(scope_state.active_turn_id)
        self.assertTrue(scope_state.drained_event.is_set())

    async def test_different_scopes_can_run_channel_turns_in_parallel(self) -> None:
        first_started = asyncio.Event()
        second_started = asyncio.Event()
        release = asyncio.Event()

        async def _blocking_turn(channel_envelope):
            if channel_envelope.event.event_id == "turn-scope-1":
                first_started.set()
            if channel_envelope.event.event_id == "turn-scope-2":
                second_started.set()
            await release.wait()
            return None

        self.core.process_channel_turn_async = _blocking_turn  # type: ignore[method-assign]
        first = self._make_channel_envelope(turn_id="turn-scope-1", request_id="req-scope-1", session_id="sess-1")
        second = self._make_channel_envelope(turn_id="turn-scope-2", request_id="req-scope-2", session_id="sess-2")

        await self.core.schedule_channel_turn_async(first)
        await self.core.schedule_channel_turn_async(second)
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await asyncio.wait_for(second_started.wait(), timeout=1.0)

        first_scope = self.core._ensure_scope_state(self.core._derive_channel_control_scope_key(first))
        second_scope = self.core._ensure_scope_state(self.core._derive_channel_control_scope_key(second))
        self.assertEqual(first_scope.active_turn_id, "turn-scope-1")
        self.assertEqual(second_scope.active_turn_id, "turn-scope-2")
        self.assertEqual(len(first_scope.pending_channel_turns), 0)
        self.assertEqual(len(second_scope.pending_channel_turns), 0)

        tasks = [self.core.state.turn_tasks["turn-scope-1"], self.core.state.turn_tasks["turn-scope-2"]]
        release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)

    async def test_interrupt_bypasses_same_scope_pending_turn_queue(self) -> None:
        first_started = asyncio.Event()
        release = asyncio.Event()

        async def _blocking_turn(channel_envelope):
            first_started.set()
            await release.wait()
            return None

        self.core.process_channel_turn_async = _blocking_turn  # type: ignore[method-assign]
        first = self._make_channel_envelope(turn_id="turn-interrupt-1", request_id="req-interrupt-1")
        second = self._make_channel_envelope(turn_id="turn-interrupt-2", request_id="req-interrupt-2")

        await self.core.schedule_channel_turn_async(first)
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await self.core.schedule_channel_turn_async(second)
        first_task = self.core.state.turn_tasks["turn-interrupt-1"]

        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="interrupt_turn",
                target_scope="runtime",
                route=self.route,
            )
        )

        self.assertTrue(self.endpoint.has_queued_replies())
        self.assertEqual(self.endpoint.outbox[-1].text, "Interrupted the current turn.")
        await asyncio.sleep(0)
        self.assertTrue(first_task.done())

        release.set()
        if "turn-interrupt-2" in self.core.state.turn_tasks:
            await asyncio.wait_for(self.core.state.turn_tasks["turn-interrupt-2"], timeout=1.0)

    async def test_reset_clears_same_scope_pending_turn_queue(self) -> None:
        first_started = asyncio.Event()
        release = asyncio.Event()

        async def _blocking_turn(channel_envelope):
            first_started.set()
            await release.wait()
            return None

        self.core.process_channel_turn_async = _blocking_turn  # type: ignore[method-assign]
        first = self._make_channel_envelope(turn_id="turn-reset-1", request_id="req-reset-1")
        second = self._make_channel_envelope(turn_id="turn-reset-2", request_id="req-reset-2")

        await self.core.schedule_channel_turn_async(first)
        await asyncio.wait_for(first_started.wait(), timeout=1.0)
        await self.core.schedule_channel_turn_async(second)
        scope_state = self.core._ensure_scope_state(self.route.control_scope_key)
        self.assertEqual(len(scope_state.pending_channel_turns), 1)

        request = PendingControlRequest(
            request_id="reset_test",
            request_kind="reset_confirm",
            control_scope_key=self.route.control_scope_key,
            route=self.route,
            expires_at="2999-01-01T00:00:00+00:00",
        )
        scope_state.pending_requests["reset_confirm"] = request

        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="reset_memory",
                target_scope="memory",
                args={"request_id": request.request_id},
                route=self.route,
            )
        )

        self.assertEqual(len(scope_state.pending_channel_turns), 0)
        self.assertNotIn("turn-reset-2", self.core.state.turn_tasks)
        release.set()


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

    async def test_callback_query_is_normalized_to_interaction_result(self) -> None:
        self.endpoint._interactive_messages["ctl_panel_1"] = {
            "chat_id": 100,
            "message_id": 12,
            "interaction_kind": "control_panel",
            "expires_at_monotonic": None,
            "actions": {
                "b0": {
                    "action_key": "control.interrupt.run",
                    "action_args": {},
                }
            },
        }

        class _User:
            id = 42

        class _Chat:
            id = 100

        class _Message:
            chat = _Chat()
            message_id = 12
            message_thread_id = None

        class _CallbackQuery:
            data = "ix:ctl_panel_1:b0"
            message = _Message()
            from_user = _User()
            id = "cb-1"

            async def answer(self) -> None:
                return None

        class _Update:
            callback_query = _CallbackQuery()

        payload = await self.endpoint._interaction_result_from_update(_Update())

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertIsInstance(payload, InteractionResult)
        self.assertEqual(payload.interaction_id, "ctl_panel_1")
        self.assertEqual(payload.action_key, "control.interrupt.run")

    async def test_callback_query_without_registered_interaction_does_not_fall_through_to_user_message(self) -> None:
        class _User:
            id = 42

        class _Chat:
            id = 100

        class _Message:
            chat = _Chat()
            from_user = _User()
            message_id = 12
            message_thread_id = None
            text = "should not become a user message"

        class _CallbackQuery:
            data = "ix:missing:b0"
            message = _Message()
            from_user = _User()
            id = "cb-2"

            async def answer(self) -> None:
                return None

        class _Update:
            callback_query = _CallbackQuery()
            effective_message = _Message()

        await self.endpoint._on_update(_Update(), None)

        self.assertEqual(self.endpoint.poll(), [])

    async def test_expired_callback_query_edits_message_and_returns_none(self) -> None:
        self.endpoint._interactive_messages["reset_1"] = {
            "chat_id": 100,
            "message_id": 12,
            "interaction_kind": "reset_confirm",
            "expires_at_monotonic": 0.0,
            "actions": {
                "b0": {
                    "action_key": "control.reset.confirm",
                    "action_args": {"request_id": "reset_1"},
                }
            },
        }

        edits: list[dict[str, object]] = []

        class _AppBot:
            async def edit_message_text(self, **kwargs):
                edits.append(dict(kwargs))

        class _App:
            bot = _AppBot()

        self.endpoint.application = _App()

        class _User:
            id = 42

        class _Chat:
            id = 100

        class _Message:
            chat = _Chat()
            message_id = 12
            message_thread_id = None

        class _CallbackQuery:
            data = "ix:reset_1:b0"
            message = _Message()
            from_user = _User()
            id = "cb-expired"

            async def answer(self) -> None:
                return None

        class _Update:
            callback_query = _CallbackQuery()

        payload = await self.endpoint._interaction_result_from_update(_Update())

        self.assertIsNone(payload)
        self.assertNotIn("reset_1", self.endpoint._interactive_messages)
        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]["text"], "This reset request expired.")
