from __future__ import annotations

import asyncio
import json
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
    ControlDelivery,
    ControlEvent,
    ControlPlane,
    ControlRoute,
    InteractionButtonSpec,
    InteractionMessageSpec,
    InteractionResult,
    derive_control_scope_key,
    register_with_core as register_control_with_core,
    route_from_channel_envelope,
)
from pal.control import interactions as control_interactions
from pal.core import PalCore, TurnContinuation, register_with_core as register_core_with_core
from pal.core.contracts import PendingControlRequest
from pal.core.runtime_config import RuntimeConfig
from pal.core.turns import ToolObservation, channel_turn_program
from pal.execution import CapabilityResult
from pal.foundation import EventEnvelope
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalLLMRequest
from pal.llm.repository import DEFAULT_THINK_LEVEL
from pal.memory import L1MessageKind, MemoryService, register_with_core as register_memory_with_core
from pal.shared import EventKind, PromptAssemblyContext, RuntimeStatus, SourceKind
from pal.stream_events import NormalizedLLMStreamEvent


class _StubEndpoint(ChannelEndpointQueueBase):
    def normalize_raw(self, payload):
        return dict(payload or {})

    def send_reply(self, response_handle: ResponseHandle, text: str) -> None:
        if not hasattr(self, "sent_replies"):
            self.sent_replies = []
        self.sent_replies.append((str(text), dict(response_handle.reply_target)))

    def send_status(self, response_handle: ResponseHandle, kind: str, payload: dict[str, object]) -> None:
        if not hasattr(self, "sent_statuses"):
            self.sent_statuses = []
        self.sent_statuses.append((str(kind), dict(payload), dict(response_handle.reply_target)))
        super().send_status(response_handle, kind, payload)

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
    refresh_payload: dict[str, object] | None = None
    refresh_calls: int = 0

    def refresh_runtime_settings(self) -> None:
        self.think_level = self.settings_repository.think_level

    def refresh_llm_endpoints(self) -> dict[str, object]:
        self.refresh_calls += 1
        return self.refresh_payload or {
            "enabled_count": 1,
            "configured_active_endpoint_id": "stub",
            "active_endpoint_id": "stub",
            "primary_endpoint_id": "stub",
            "added_endpoint_ids": [],
            "removed_endpoint_ids": [],
        }


class _FakeCapabilityRuntime:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = []

    async def execute_async(self, call):
        self.calls.append(call)
        return CapabilityResult(status=RuntimeStatus.OK, text=self.text, llm_text=self.text)


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
        self.assertIn("/think [off|minimal|low|balanced|deep|xhigh]", rendered)
        self.assertIn("/log [start|end]", rendered)
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

    def test_log_without_argument_shows_current_status(self) -> None:
        plane = ControlPlane()
        action = plane.parse_event(
            ControlEvent(
                event_kind=EventKind.SLASH_COMMAND,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "/log"},
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_kind, "show_log")

    def test_log_start_and_end_parse_to_set_log(self) -> None:
        plane = ControlPlane()
        start = plane.parse_event(
            ControlEvent(
                event_kind=EventKind.SLASH_COMMAND,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "/log start"},
            )
        )
        end = plane.parse_event(
            ControlEvent(
                event_kind=EventKind.SLASH_COMMAND,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "/log end"},
            )
        )

        self.assertIsNotNone(start)
        self.assertIsNotNone(end)
        assert start is not None and end is not None
        self.assertEqual(start.action_kind, "set_log")
        self.assertTrue(start.args["prompt_log_enabled"])
        self.assertEqual(end.action_kind, "set_log")
        self.assertFalse(end.args["prompt_log_enabled"])

    def test_refresh_llm_endpoint_slash_command_bypasses_llm(self) -> None:
        plane = ControlPlane()
        action = plane.parse_event(
            ControlEvent(
                event_kind=EventKind.SLASH_COMMAND,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "/refresh_llm_endpoint"},
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_kind, "refresh_llm_endpoint")
        self.assertEqual(action.target_scope, "runtime")
        self.assertIn("/refresh_llm_endpoint", plane.render_panel_text())

    def test_refresh_tool_surface_slash_command_bypasses_llm(self) -> None:
        plane = ControlPlane()
        action = plane.parse_event(
            ControlEvent(
                event_kind=EventKind.SLASH_COMMAND,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "/refresh_tool_surface"},
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_kind, "refresh_tool_surface")
        self.assertEqual(action.target_scope, "runtime")
        self.assertIn("/refresh_tool_surface", plane.render_panel_text())

    def test_telegram_bot_mention_suffix_is_ignored_for_slash_commands(self) -> None:
        plane = ControlPlane()
        action = plane.parse_event(
            ControlEvent(
                event_kind=EventKind.SLASH_COMMAND,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "/refresh_llm_endpoint@PalDevBot"},
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_kind, "refresh_llm_endpoint")

    def test_invalid_log_subcommand_is_invalid_command(self) -> None:
        plane = ControlPlane()
        action = plane.parse_event(
            ControlEvent(
                event_kind=EventKind.SLASH_COMMAND,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "/log maybe"},
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_kind, "invalid_command")
        self.assertEqual(action.notes, "Use /log start or /log end.")

    def test_log_interactions_generate_typed_actions(self) -> None:
        plane = ControlPlane()
        route = ControlRoute(
            endpoint_id="telegram_main",
            channel_kind="telegram",
            reply_target={"chat_id": "42"},
            control_scope_key="telegram:telegram_main:42:root",
        )

        open_action = plane.handle_interaction(
            InteractionResult(
                interaction_id="ctl_panel_1",
                interaction_kind="control_panel",
                action_key="control.log.open",
                route=route,
            )
        )
        start_action = plane.handle_interaction(
            InteractionResult(
                interaction_id="ctl_panel_1",
                interaction_kind="control_panel",
                action_key="control.log.start",
                route=route,
            )
        )
        end_action = plane.handle_interaction(
            InteractionResult(
                interaction_id="ctl_panel_1",
                interaction_kind="control_panel",
                action_key="control.log.end",
                route=route,
            )
        )

        self.assertIsNotNone(open_action)
        self.assertIsNotNone(start_action)
        self.assertIsNotNone(end_action)
        assert open_action is not None and start_action is not None and end_action is not None
        self.assertEqual(open_action.action_kind, "show_log")
        self.assertEqual(start_action.action_kind, "set_log")
        self.assertTrue(start_action.args["prompt_log_enabled"])
        self.assertEqual(start_action.args["interaction_origin"], "button")
        self.assertEqual(end_action.action_kind, "set_log")
        self.assertFalse(end_action.args["prompt_log_enabled"])

    def test_unknown_interaction_action_normalizes_to_invalid_command(self) -> None:
        plane = ControlPlane()
        route = ControlRoute(
            endpoint_id="telegram_main",
            channel_kind="telegram",
            reply_target={"chat_id": "42"},
            control_scope_key="telegram:telegram_main:42:root",
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

    def test_route_prefers_provider_supplied_control_scope_key(self) -> None:
        envelope = ChannelEnvelope(
            event=EventEnvelope(
                event_kind=EventKind.USER_MESSAGE,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "hello", "chat_id": "42"},
                correlation_id="msg-1",
                event_id="turn-1",
            ),
            endpoint=EndpointConfig(endpoint_id="telegram_main", channel_kind="telegram", binding_key=""),
            response_handle=ResponseHandle(
                endpoint_id="telegram_main",
                reply_target={
                    "chat_id": "42",
                    "message_id": "99",
                    "control_scope_key": "telegram:telegram_main:42:root",
                },
            ),
        )

        route = route_from_channel_envelope(envelope)

        self.assertEqual(route.control_scope_key, "telegram:telegram_main:42:root")

    def test_control_scope_fallback_is_channel_neutral(self) -> None:
        scope = derive_control_scope_key(
            endpoint_id="demo_main",
            channel_kind="demo_chat",
            reply_target={"conversation_id": "conv-42", "message_id": "99"},
            payload={"text": "hello"},
        )

        self.assertEqual(scope, "channel:demo_main:conversation_id=conv-42")


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
                reply_target={
                    "session_id": session_id,
                    "request_id": request_id,
                    "control_scope_key": f"socket:socket_main:{session_id}",
                },
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

    async def test_turn_prompt_budget_snapshot_counts_tools_schema(self) -> None:
        envelope = self._make_channel_envelope(turn_id="turn-budget-tools", request_id="req-budget-tools", text="hello")
        continuation = self.core.turn_manager.start(envelope)
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "op_test_tool",
                    "description": "tool schema budget",
                    "parameters": {
                        "type": "object",
                        "properties": {"payload": {"type": "string", "description": "x" * 120}},
                    },
                },
            }
        ]

        prompt = self.core.turn_executor.build_turn_prompt(
            continuation,
            PromptAssemblyContext(event=envelope.event, core_mode="default"),
            max_output_tokens=64,
            tools=tools,
        )

        snapshot = prompt.metadata["prompt_budget_snapshot"]
        expected_tool_chars = len(json.dumps(tools, ensure_ascii=False, sort_keys=True))
        self.assertEqual(snapshot["tools_schema_chars"], expected_tool_chars)
        self.assertEqual(
            snapshot["hard_keep_chars"],
            snapshot["system_chars"] + snapshot["current_user_chars"] + snapshot["tool_protocol_chars"] + expected_tool_chars,
        )

    async def test_set_log_updates_future_turn_snapshot_only(self) -> None:
        first = self._make_channel_envelope(turn_id="turn-log-1", request_id="req-log-1")
        continuation = self.core.turn_manager.start(first)
        self.assertFalse(continuation.turn_settings_snapshot["prompt_log_enabled"])

        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="set_log",
                target_scope="runtime",
                args={"prompt_log_enabled": True},
                route=self.route,
            )
        )

        self.assertFalse(continuation.turn_settings_snapshot["prompt_log_enabled"])
        second = self._make_channel_envelope(turn_id="turn-log-2", request_id="req-log-2")
        second_continuation = self.core.turn_manager.start(second)
        self.assertTrue(second_continuation.turn_settings_snapshot["prompt_log_enabled"])

    async def test_refresh_llm_endpoint_control_action_updates_runtime_directly(self) -> None:
        self.llm_runtime.refresh_payload = {
            "enabled_count": 2,
            "configured_active_endpoint_id": "old",
            "active_endpoint_id": None,
            "primary_endpoint_id": "new",
            "added_endpoint_ids": ["new"],
            "removed_endpoint_ids": ["old"],
        }

        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="refresh_llm_endpoint",
                target_scope="runtime",
                route=self.route,
            )
        )

        self.assertEqual(self.llm_runtime.refresh_calls, 1)
        self.assertIn("LLM endpoints refreshed.", self.endpoint.outbox[-1].text)
        self.assertIn("Primary endpoint for future turns: new", self.endpoint.outbox[-1].text)
        self.assertIn("Removed/disabled: old", self.endpoint.outbox[-1].text)

    async def test_refresh_tool_surface_control_action_updates_runtime_directly(self) -> None:
        calls = []

        def reload_config() -> dict[str, object]:
            calls.append("reload")
            return {
                "singleton_count": 3,
                "dynamic_count": 1,
                "resident_tool_count": 2,
                "resident_tool_names": ["op_tool_search", "op_tool_call"],
            }

        self.core.tool_surface.reload_config = reload_config  # type: ignore[method-assign]

        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="refresh_tool_surface",
                target_scope="runtime",
                route=self.route,
            )
        )

        self.assertEqual(calls, ["reload"])
        self.assertIn("Tool surface refreshed.", self.endpoint.outbox[-1].text)
        self.assertIn("Resident tools for future turns: 2", self.endpoint.outbox[-1].text)
        self.assertIn("op_tool_search, op_tool_call", self.endpoint.outbox[-1].text)

    async def test_slash_command_with_telegram_bot_suffix_runs_end_to_end(self) -> None:
        self.core.bind_async_wakeup_sources()
        self.endpoint.accept_raw(
            {"text": "/control@PalDevBot", "session_id": "sess-1", "request_id": "req-mention"},
            event_kind=EventKind.USER_MESSAGE,
            correlation_id="req-mention",
            reply_target={"session_id": "sess-1", "request_id": "req-mention"},
        )

        processed = await self.core.run_until_idle_async(max_iterations=16)

        self.assertIn(EventKind.SLASH_COMMAND, [item.event_kind for item in processed])
        self.assertTrue(hasattr(self.endpoint, "sent_replies"))
        self.assertIn("Pal Control Panel", self.endpoint.sent_replies[-1][0])

    async def test_show_log_reports_current_status(self) -> None:
        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="show_log",
                target_scope="runtime",
                route=self.route,
            )
        )

        statuses = list(self.endpoint.status_outbox)
        self.assertGreaterEqual(len(statuses), 2)
        self.assertEqual(statuses[-2].kind, "interactive_update")
        self.assertEqual(statuses[-2].payload["spec"].text, "Prompt log: off\nUse /log start or /log end. Changes apply to new turns only.")

    async def test_base_channel_falls_back_to_interaction_text(self) -> None:
        delivery = ControlDelivery(
            delivery_kind="interactive_open",
            route=self.route,
            interaction=control_interactions.build_terminal_interaction(
                interaction_id="ix-fallback",
                interaction_kind="control_panel",
                route=self.route,
                text="Fallback text.",
            ),
        )

        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="interactive_open",
                target_scope="interaction",
                route=self.route,
                delivery=delivery,
            )
        )
        self.endpoint.flush_status_outbox()

        self.assertEqual(self.endpoint.sent_replies[-1][0], "Fallback text.")

    async def test_button_backed_set_log_resolves_interaction(self) -> None:
        route = ControlRoute(
            endpoint_id="socket_main",
            channel_kind="telegram",
            reply_target={"chat_id": "42", "message_id": "12"},
            control_scope_key="telegram:telegram_main:42:root",
            correlation_id="tg-1",
        )

        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="set_log",
                target_scope="runtime",
                args={
                    "prompt_log_enabled": True,
                    "interaction_origin": "button",
                    "interaction_id": "ctl_panel_1",
                    "interaction_kind": "control_panel",
                },
                route=route,
            )
        )

        statuses = list(self.endpoint.status_outbox)
        self.assertGreaterEqual(len(statuses), 2)
        self.assertTrue(self.core.state.prompt_log_enabled)
        self.assertEqual(statuses[-2].kind, "interactive_resolve")
        self.assertEqual(statuses[-2].payload["spec"].text, "Prompt debug logging enabled for new turns.")
        self.assertEqual(statuses[-1].kind, "working_stop")

    async def test_log_panel_marks_current_status(self) -> None:
        route = ControlRoute(
            endpoint_id="socket_main",
            channel_kind="telegram",
            reply_target={"chat_id": "42", "message_id": "12"},
            control_scope_key="telegram:telegram_main:42:root",
            correlation_id="tg-1",
        )
        self.core.state.prompt_log_enabled = True

        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="show_log",
                target_scope="runtime",
                route=route,
            )
        )

        statuses = list(self.endpoint.status_outbox)
        self.assertGreaterEqual(len(statuses), 2)
        self.assertEqual(statuses[-2].kind, "interactive_update")
        spec = statuses[-2].payload["spec"]
        flattened = [button for row in spec.buttons for button in row]
        self.assertEqual(spec.text, "Prompt log: on\nUse /log start or /log end. Changes apply to new turns only.")
        self.assertTrue(any(button.label == "> Start logging" and button.action_key == "control.log.start" for button in flattened))
        self.assertTrue(any(button.label == "Stop logging" and button.action_key == "control.log.end" for button in flattened))

    async def test_prompt_log_writes_when_turn_snapshot_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.core.config = RuntimeConfig(runtime_root=Path(tmp))
            envelope = self._make_channel_envelope(turn_id="turn-log-file", request_id="req-log-file")
            continuation = TurnContinuation(
                turn_id="turn-log-file",
                channel_envelope=envelope,
                program=channel_turn_program(envelope),
                correlation_id="req-log-file",
                control_scope_key="socket:socket_main:sess-1",
                turn_settings_snapshot={"think_level": "balanced", "prompt_log_enabled": True},
            )
            request = CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=64,
                tools=[{"type": "function", "function": {"name": "demo", "parameters": {}}}],
            )

            self.core._debug_log_prompt(continuation, request)
            self.core._debug_log_outcome(continuation, CanonicalLLMOutcome(text="world"))
            self.core._debug_log_reply(continuation, "final")

            content = (Path(tmp) / "pal.log").read_text(encoding="utf-8")
            self.assertIn("=== PAL PROMPT DEBUG ===", content)
            self.assertIn("=== PAL LLM OUTCOME ===", content)
            self.assertIn("=== PAL TG REPLY ===", content)

    async def test_prompt_log_skips_when_turn_snapshot_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            self.core.config = RuntimeConfig(runtime_root=Path(tmp))
            envelope = self._make_channel_envelope(turn_id="turn-log-file-off", request_id="req-log-file-off")
            continuation = TurnContinuation(
                turn_id="turn-log-file-off",
                channel_envelope=envelope,
                program=channel_turn_program(envelope),
                correlation_id="req-log-file-off",
                control_scope_key="socket:socket_main:sess-1",
                turn_settings_snapshot={"think_level": "balanced", "prompt_log_enabled": False},
            )
            request = CanonicalLLMRequest(messages=[{"role": "user", "content": "hello"}], max_output_tokens=64)

            self.core._debug_log_prompt(continuation, request)

            self.assertFalse((Path(tmp) / "pal.log").exists())

    async def test_core_publishes_control_catalog_to_channel_endpoint(self) -> None:
        await self.core.publish_control_catalog_async(endpoint_id="socket_main")

        self.assertTrue(self.endpoint.has_queued_status())
        queued = list(self.endpoint.status_outbox)
        self.assertEqual(len(queued), 1)
        self.assertEqual(queued[0].kind, "control_catalog")
        commands = list(queued[0].payload.get("commands") or [])
        self.assertTrue(any(item.get("command") == "control" for item in commands))
        self.assertTrue(any(item.get("command") == "refresh_llm_endpoint" for item in commands))
        self.assertTrue(any(item.get("command") == "refresh_tool_surface" for item in commands))

    async def test_generic_channel_endpoint_handles_interaction_buttons_without_telegram(self) -> None:
        spec = InteractionMessageSpec(
            interaction_id="generic_panel",
            interaction_kind="control_panel",
            route=self.route,
            text="Generic control panel",
            buttons=((InteractionButtonSpec(label="Think", action_key="control.think.open"),),),
        )
        self.endpoint.queue_status(
            "interactive_update",
            payload={"spec": spec},
            response_handle=self.endpoint.build_response_handle(reply_target=self.route.reply_target),
        )

        self.endpoint.flush_status_outbox()

        self.assertIn("generic_panel", self.endpoint._interactive_messages)
        self.assertEqual(self.endpoint.sent_replies[-1][0], "Generic control panel")
        result = self.endpoint.interaction_result_from_token("generic_panel", "b0")
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.action_key, "control.think.open")
        envelope = self.endpoint.emit_interaction_result(
            result,
            correlation_id="generic-callback",
            reply_target=self.route.reply_target,
        )

        self.assertIsNotNone(envelope)
        drained = self.endpoint.poll()
        self.assertEqual(drained[-1].event.event_kind, EventKind.INTERACTION_RESULT)
        self.assertIsInstance(drained[-1].event.payload, InteractionResult)

    async def test_channel_attach_replays_cached_control_catalog(self) -> None:
        await self.core.publish_control_catalog_async(endpoint_id="socket_main")
        self.endpoint.flush_status_outbox()
        self.endpoint.sent_statuses.clear()

        self.channel_runtime.detach_endpoint("socket_main")
        self.channel_runtime.attach_endpoint("socket_main")
        self.endpoint.flush_status_outbox()

        catalog_statuses = [item for item in self.endpoint.sent_statuses if item[0] == "control_catalog"]
        self.assertTrue(catalog_statuses)
        commands = list(catalog_statuses[-1][1].get("commands") or [])
        self.assertTrue(any(item.get("command") == "refresh_llm_endpoint" for item in commands))
        self.assertTrue(any(item.get("command") == "refresh_tool_surface" for item in commands))

    async def test_channel_replace_replays_cached_control_catalog_when_started(self) -> None:
        await self.channel_runtime.start_async()
        await self.core.publish_control_catalog_async(endpoint_id="socket_main")
        replacement = _StubEndpoint(
            endpoint=EndpointConfig(endpoint_id="socket_main", channel_kind="socket", binding_key="runtime.sock")
        )
        replacement.sent_statuses = []

        await self.channel_runtime.replace_endpoint_async(replacement)
        replacement.flush_status_outbox()

        catalog_statuses = [item for item in replacement.sent_statuses if item[0] == "control_catalog"]
        self.assertTrue(catalog_statuses)
        commands = list(catalog_statuses[-1][1].get("commands") or [])
        self.assertTrue(any(item.get("command") == "refresh_llm_endpoint" for item in commands))
        self.assertTrue(any(item.get("command") == "refresh_tool_surface" for item in commands))

    async def test_channel_replace_from_running_channel_loop_schedules_reload(self) -> None:
        await self.channel_runtime.start_async()
        replacement = _StubEndpoint(
            endpoint=EndpointConfig(endpoint_id="socket_main", channel_kind="socket", binding_key="runtime.sock")
        )

        self.channel_runtime.replace_endpoint(replacement)
        await asyncio.sleep(0)

        self.assertIs(self.channel_runtime.get_endpoint("socket_main"), replacement)

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
        envelope = self._make_channel_envelope(turn_id="turn-quiescing", request_id="req-1")

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

    async def test_non_interactive_capability_reply_preserves_full_text(self) -> None:
        text = "capability result " + ("x" * 320)
        runtime = _FakeCapabilityRuntime(text)
        self.core.context.execution_runtime = runtime

        await self.core.handle_control_action_async(
            ControlAction(
                action_kind="invoke_capability",
                target_scope="execution",
                target_id="op_demo_long",
                route=self.route,
            )
        )

        self.assertEqual(runtime.calls[0].name, "op_demo_long")
        self.assertTrue(self.endpoint.has_queued_replies())
        self.assertEqual(self.endpoint.outbox[-1].text, text)

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
            control_scope_key="telegram:telegram_main:42:root",
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
            control_scope_key="telegram:telegram_main:42:root",
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

        spec = control_interactions.build_control_panel_interaction(self.control_plane, self.route)
        flattened = [button for row in spec.buttons for button in row]

        self.assertTrue(any(button.label == "Think" and button.action_key == "control.think.open" for button in flattened))
        self.assertTrue(any(button.label == "Log" and button.action_key == "control.log.open" for button in flattened))
        self.assertTrue(any(button.label == "Compact" and button.action_key == "control.compact.run" for button in flattened))
        self.assertTrue(any(button.label == "Interrupt" and button.action_key == "control.interrupt.run" for button in flattened))
        self.assertTrue(any(button.label == "Reset Memory" and button.action_key == "control.reset.open" for button in flattened))
        self.assertTrue(
            any(
                button.label == "Refresh LLM"
                and button.action_key == "control.command.run"
                and button.action_args == {"command_name": "refresh_llm_endpoint"}
                for button in flattened
            )
        )
        self.assertTrue(
            any(
                button.label == "Refresh Tools"
                and button.action_key == "control.command.run"
                and button.action_args == {"command_name": "refresh_tool_surface"}
                for button in flattened
            )
        )
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
        continuation.tool_protocol_messages.extend(
            [
                {
                    "role": "assistant",
                    "content": "checking the runtime",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "op_exec_shell", "arguments": "{\"cmd\": \"date\"}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "shell result before interrupt",
                },
            ]
        )
        continuation.tool_observations.append(
            ToolObservation(tool_name="op_exec_shell", ok=True, summary="shell result before interrupt")
        )
        continuation.emitted_reply_texts.append("I found a runtime clue before interruption.")

        interrupted = await self.core.turn_manager.interrupt_by_scope(scope_key)

        self.assertTrue(interrupted)
        self.assertTrue(continuation.interrupted)
        self.assertTrue(self.endpoint._stream_sessions[id(response_handle)]["closed"])
        self.assertEqual(len(self.memory_service.l1_store.items), 1)
        checkpoint = self.memory_service.l1_store.items[-1]
        kinds = [item.kind for item in checkpoint]
        self.assertIn(L1MessageKind.USER_REQUEST, kinds)
        self.assertIn(L1MessageKind.ASSISTANT_TOOL_CALL, kinds)
        self.assertIn(L1MessageKind.TOOL_RESULT, kinds)
        self.assertIn(L1MessageKind.ASSISTANT_REPLY, kinds)
        self.assertIn(L1MessageKind.TURN_INTERRUPTED, kinds)
        summary = next(item.content for item in checkpoint if item.kind == L1MessageKind.TURN_INTERRUPTED)
        self.assertIn("This is recovery context", summary)
        self.assertIn("op_exec_shell", summary)
        self.assertIn("turn_outcome: not committed", summary)
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_unhandled_turn_exception_commits_aborted_checkpoint(self) -> None:
        envelope = self._make_channel_envelope(
            turn_id="turn-aborted",
            request_id="req-aborted",
            text="please do the thing",
        )

        def _crashing_program():
            if False:
                yield None
            raise RuntimeError("boom")

        continuation = TurnContinuation(
            turn_id="turn-aborted",
            channel_envelope=envelope,
            program=_crashing_program(),
            correlation_id="req-aborted",
            control_scope_key=self.route.control_scope_key,
            turn_settings_snapshot={"think_level": "balanced"},
        )

        with self.assertRaises(RuntimeError):
            await self.core.run_turn_continuation_async(continuation)

        self.assertEqual(len(self.memory_service.l1_store.items), 1)
        checkpoint = self.memory_service.l1_store.items[-1]
        kinds = [item.kind for item in checkpoint]
        self.assertIn(L1MessageKind.USER_REQUEST, kinds)
        self.assertIn(L1MessageKind.TURN_ABORTED, kinds)
        summary = next(item.content for item in checkpoint if item.kind == L1MessageKind.TURN_ABORTED)
        self.assertIn("status: aborted", summary)
        self.assertIn("RuntimeError: boom", summary)
        self.assertIn("turn_outcome: not committed", summary)

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
