"""Model selection must preserve replay and cross the idle boundary once."""
import asyncio
import json
import tempfile
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from pal.control.contracts import ControlAction, ControlCommandInvocation
from pal.core.compaction import CompactionRunResult
from pal.llm import EndpointResolver, LLMRuntime
from pal.llm.contracts import generation_result_from_values
from pal.llm.ir import (GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole,
                        LLMResponseUpdate, LLMResponseDeltaKind, PromptRegionIR, ReplayEnvelope, TextPartIR, WireShape)
from pal.llm.transport import LLMEndpointSpecStaleError
from pal.llm.model_hooks import ModelHook, ModelHookRegistry
from pal.llm.runtime import LLMRequestPreparationError
from pal.llm.projection_contracts import AttemptKey, HistoryCursor, OwnerFence
from pal.llm.projection_session import HistoryView
from pal.llm.shapes.openai_response import OpenAIResponseCodec
from pal.memory.contracts import MemoryCompactResult
from tests import test_control_plane as control_fixture
from tests.test_runtime_compaction import _valid_pal_payload


def endpoint(name="a", model="gpt-5.6-sol", **changes):
    return SimpleNamespace(**{
        "endpoint_id": name, "model_id": model, "display_name": name,
        "provider": "openai", "wire_shape": "openai_response",
        "base_url": "https://api.openai.com/v1", "auth_kind": "api_key_ref",
        "credential_ref": "SHARED_TEST_KEY", "context_window": 100_000,
        "max_output_tokens": 4096, "thinking_levels_blob": ["low", "high"],
        "default_thinking_level": "low", "supports_tools": True,
        "supports_streaming": True, "supports_vision": True,
        "input_modalities_blob": ["text"], "output_modalities_blob": ["text"],
        "priority": 0, "enabled": True, "notes": None,
        "capabilities_blob": {"prompt_cache": {"enabled": False}}, **changes,
    })


class Settings:
    def __init__(self, active="a"):
        self.active = active
        self.think = {}

    def get_active_llm_endpoint_id(self):
        return self.active

    def set_active_llm_endpoint_id(self, value):
        self.active = value

    def get_think_level(self, endpoint_id):
        return self.think.get(endpoint_id)

    def set_think_level(self, endpoint_id, level):
        self.think[endpoint_id] = level

    @contextmanager
    def selection_transaction(self):
        previous = self.active, dict(self.think)
        try:
            yield
        except BaseException:
            self.active, self.think = previous
            raise


def reasoning_message(source="a", model="gpt-5.6-sol"):
    return LLMMessageIR(
        MessageRole.ASSISTANT, (TextPartIR("answer"),), message_id="answer",
        prompt_region=PromptRegionIR.SETTLED_HISTORY,
        replay=ReplayEnvelope(WireShape.OPENAI_RESPONSE, source, model, {
            "output": [
                {"id": "r1", "type": "reasoning", "encrypted_content": "opaque-original", "summary": []},
                {"id": "m1", "type": "message", "role": "assistant",
                 "content": [{"type": "output_text", "text": "answer"}]},
            ],
        }),
    )


class ReplayCompatibilityTests(unittest.TestCase):
    def test_hooks_cannot_hide_incompatible_original_replay(self):
        target = endpoint("b", "gpt-6-astra")
        runtime = LLMRuntime(EndpointResolver(endpoints=(endpoint(), target)), Settings())
        request = LLMRequestIR(messages=(reasoning_message(),), tools=(),
                               policy=GenerationPolicyIR(max_output_tokens=100))
        for transform in (lambda messages: [replace(m, replay=None) for m in messages],
                          lambda messages: []):
            with self.subTest(transform=transform):
                runtime.model_hooks = ModelHookRegistry({target.model_id: ModelHook(
                    target.model_id, adjust_messages=transform,
                )})
                self.assertTrue(runtime.model_switch_advice("b", request).compact_required)
                with self.assertRaisesRegex(LLMRequestPreparationError, "previous model"):
                    runtime._prepare_request(target, request)

    def test_family_codec_replays_original_bytes_without_relabeling(self):
        source, target = endpoint(), endpoint("b", "gpt-5.6-terra")
        message = reasoning_message()
        runtime = LLMRuntime(EndpointResolver(endpoints=(source, target)), Settings())
        request = LLMRequestIR(messages=(message,), tools=(), policy=GenerationPolicyIR(max_output_tokens=100))
        old_plan = runtime.prepare_generation_plan(request)
        old_identity = runtime.endpoint_projection_session("test", plan=old_plan).identity
        advice = runtime.model_switch_advice("b", request)
        self.assertFalse(advice.compact_required)
        plan = runtime.prepare_generation_plan(replace(request, metadata={"preferred_endpoint_id": "b"}))
        self.assertIsNotNone(plan)
        session = runtime.endpoint_projection_session("test", plan=plan)
        wire = OpenAIResponseCodec().encode(plan.prepared.request, session._shape_context())
        self.assertEqual(wire.payload["input"][0]["encrypted_content"], "opaque-original")
        self.assertEqual(message.replay.endpoint_id, "a")
        self.assertEqual(session.binding.endpoint_id, "b")
        self.assertNotEqual(session.identity, old_identity)
        session.begin_round(AttemptKey(session.identity, OwnerFence(0), "next"), requires_native=False)
        projected = session.prepare(HistoryView(cursor=HistoryCursor.initial(), messages=(message,)),
                                    request_shell=plan.prepared.request)
        self.assertEqual(json.loads(projected.payload_json)["input"], wire.payload["input"])

    def test_unknown_family_service_credentials_and_changed_source_require_compact(self):
        for change in (
            {"model_id": "gpt-6-astra"}, {"model_id": "gpt-5.6-invented"},
            {"base_url": "https://gateway.test/v1"}, {"credential_ref": "OTHER_KEY"},
            {"auth_kind": "oauth"},
        ):
            with self.subTest(change=change):
                source, target = endpoint(), endpoint("b", "gpt-5.6-luna", **change)
                runtime = LLMRuntime(EndpointResolver(endpoints=(source, target)), Settings())
                advice = runtime.model_switch_advice("b", LLMRequestIR(messages=(reasoning_message(),), tools=(), policy=GenerationPolicyIR(max_output_tokens=100)))
                self.assertTrue(advice.compact_required)
        runtime = LLMRuntime(EndpointResolver(endpoints=(endpoint(model="changed"), endpoint("b", "gpt-5.6-luna"))), Settings())
        self.assertTrue(runtime.model_switch_advice("b", LLMRequestIR(messages=(reasoning_message(),), tools=(), policy=GenerationPolicyIR(max_output_tokens=100))).compact_required)

    def test_missing_selection_or_legacy_fallback_never_routes_to_another_endpoint(self):
        for active in (None, "deleted"):
            runtime = LLMRuntime(EndpointResolver(endpoints=(endpoint(), endpoint("b"))), Settings(active))
            result = runtime.generate(LLMRequestIR(messages=(), tools=(), policy=GenerationPolicyIR(max_output_tokens=100), metadata={"endpoint_fallback_policy": "enabled"}))
            self.assertIsNone(runtime.active_endpoint())
            self.assertIn("unavailable", result.text)
        runtime = LLMRuntime(EndpointResolver(endpoints=(endpoint(), endpoint("b"))), Settings())
        self.assertEqual([ep.endpoint_id for ep in runtime._enabled_endpoints_for_preference(endpoint_fallback_policy="enabled")], ["a"])
        self.assertEqual(runtime._enabled_endpoints_for_preference(preferred_endpoint_id="missing"), [])

    def test_selection_write_rolls_back_on_activation_failure(self):
        class BrokenActivation:
            def activate_endpoint(self, name):
                if name == "b":
                    raise RuntimeError("activation failed")
        settings = Settings()
        runtime = LLMRuntime(EndpointResolver(endpoints=(endpoint(), endpoint("b"))), settings,
                             endpoint_invoker=BrokenActivation())
        before = dict(settings.think)
        with self.assertRaises(RuntimeError):
            runtime.apply_model_selection("b", "high")
        self.assertEqual(runtime.active_endpoint_id, "a")
        self.assertEqual(settings.active, "a")
        self.assertEqual(settings.think, before)

    def test_target_budget_requires_compact_but_fixed_prompt_overflow_is_rejected(self):
        target = endpoint("b", context_window=8192, max_output_tokens=1024)
        settings = Settings()
        runtime = LLMRuntime(EndpointResolver(endpoints=(endpoint(), target)), settings)
        history = LLMMessageIR(MessageRole.USER, (TextPartIR("word " * 20_000),),
                               prompt_region=PromptRegionIR.SETTLED_HISTORY)
        request = LLMRequestIR(messages=(history,), tools=(), policy=GenerationPolicyIR(max_output_tokens=100))
        self.assertTrue(runtime.model_switch_advice("b", request).compact_required)
        self.assertNotIn("b", settings.think)
        fixed = replace(history, role=MessageRole.SYSTEM, prompt_region=PromptRegionIR.STABLE_SYSTEM)
        with self.assertRaisesRegex(ValueError, "fixed prompt"):
            runtime.model_switch_advice("b", replace(request, messages=(fixed,)))

    def test_database_rolls_back_both_model_and_thinking_on_activation_failure(self):
        from peewee import SqliteDatabase
        from pal.llm.models import PalRuntimeSettingModel
        from pal.llm.repository import RuntimeSettingRepository

        database = SqliteDatabase(":memory:")
        with database.bind_ctx([PalRuntimeSettingModel]), database.connection_context():
            database.create_tables([PalRuntimeSettingModel])
            settings = RuntimeSettingRepository()
            settings.set_active_llm_endpoint_id("a")
            runtime = LLMRuntime(EndpointResolver(endpoints=(endpoint(), endpoint("b"))), settings)
            class Invoker:
                def activate_endpoint(inner, name):
                    if name == "b":
                        raise RuntimeError("activation failed")
            runtime.endpoint_invoker = Invoker()
            with self.assertRaises(RuntimeError):
                runtime.apply_model_selection("b", "high")
            self.assertEqual(settings.get_active_llm_endpoint_id(), "a")
            self.assertIsNone(settings.get_think_level("b"))
            self.assertEqual(runtime.active_endpoint_id, "a")

    def test_compatible_plan_is_revoked_if_source_metadata_changes(self):
        source, target = endpoint(), endpoint("b", "gpt-5.6-luna")
        runtime = LLMRuntime(EndpointResolver(endpoints=(source, target)), Settings())
        request = LLMRequestIR(messages=(reasoning_message(),), tools=(),
            policy=GenerationPolicyIR(max_output_tokens=100), metadata={"preferred_endpoint_id": "b"})
        plan = runtime.prepare_generation_plan(request)
        self.assertIsNotNone(plan)
        source.credential_ref = "CHANGED"
        self.assertFalse(runtime._plan_is_current(plan, target))
        self.assertIsNone(runtime.prepare_generation_plan(request))

    def test_stale_refresh_retry_stays_on_original_endpoint_in_both_paths(self):
        for stream in (False, True):
            with self.subTest(stream=stream):
                calls = []
                class Invoker:
                    def invoke(inner, endpoint, request, **kwargs):
                        calls.append(endpoint.endpoint_id)
                        if len(calls) == 1:
                            raise LLMEndpointSpecStaleError("refresh needed")
                        return generation_result_from_values(text="ok").response, ()

                    def invoke_updates(inner, endpoint, request, **kwargs):
                        response, _ = inner.invoke(endpoint, request, **kwargs)
                        yield LLMResponseUpdate(response, delta_kind=LLMResponseDeltaKind.STATE)
                runtime = LLMRuntime(EndpointResolver(endpoints=(endpoint(), endpoint("b"))),
                                     Settings(), endpoint_invoker=Invoker())
                runtime.refresh_llm_endpoints = lambda: runtime.set_active_endpoint("b")
                request = LLMRequestIR(messages=(), tools=(), policy=GenerationPolicyIR(max_output_tokens=100))
                if stream:
                    response = list(runtime._iter_stream_updates(request))[-1].response
                else:
                    response = runtime.generate(request).response
                self.assertEqual(response.text, "ok")
                self.assertEqual(calls, ["a", "a"])
                self.assertEqual(runtime.active_endpoint_id, "b")


class ModelSwitchControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = control_fixture.PalControlFlowTests()
        await self.fixture.asyncSetUp()
        self.core, self.memory, self.route = self.fixture.core, self.fixture.memory_service, self.fixture.route
        self.settings = Settings()
        self.target = endpoint("b", "gpt-6-astra")
        self.llm = LLMRuntime(EndpointResolver(endpoints=(endpoint(), self.target)), self.settings)
        self.core.context.port_registry["llm:llm"] = self.llm
        self.memory.begin_l1_turn("old", user_message=LLMMessageIR(MessageRole.USER, (TextPartIR("task"),)))
        self.memory.upsert_l1_assistant("old", reasoning_message())
        self.memory.settle_l1_turn("old")
        self.compact_started = asyncio.Event()
        self.compact_release = asyncio.Event()
        self.compact_release.set()
        self.compact_success = True
        self.compact_calls = []
        self.real_compact = self.core.turn_executor.compact_memory_async

        async def compact(memory, **kwargs):
            self.compact_calls.append((self.llm.active_endpoint_id, kwargs))
            self.compact_started.set()
            await self.compact_release.wait()
            if not self.compact_success or not kwargs["commit_guard"]():
                return CompactionRunResult(status="error")
            memory.soft_reset()
            return CompactionRunResult(status="compacted", memory_result=MemoryCompactResult(summary="summary"))
        self.core.turn_executor.compact_memory_async = compact

    async def select(self):
        await self.core.handle_control_action_async(ControlAction(
            action_kind="set_model", target_scope="runtime", route=self.route, args={"endpoint_id": "b"},
        ))
        scope = self.core.state.control_scopes.get(self.route.control_scope_key)
        return scope.pending_requests.get("model_switch") if scope is not None else None

    async def step(self, request, operation, **kwargs):
        await self.core.handle_control_action_async(ControlAction(
            action_kind="model_switch_step", target_scope="runtime", route=self.route,
            args={"request_id": request.request_id, "operation": operation, **kwargs},
        ))

    def replies(self):
        return "\n".join(item.text for item in self.fixture.endpoint.outbox)

    async def _check_button_compact_result(self, *, success):
        from pal.channel.contracts import EndpointConfig, ResponseHandle
        from pal.control import interactions

        self.compact_success = success
        request = await self.select()
        await self.step(request, "confirm")
        with tempfile.TemporaryDirectory(prefix="pal_model_switch_") as root:
            provider = control_fixture.TelegramChannelEndpoint(
                endpoint=EndpointConfig(endpoint_id="telegram_main", channel_kind="telegram",
                                        binding_key="user:42", send_policy={}),
                runtime_root=Path(root), bot_token="token",
            )
            bot = SimpleNamespace(edit_message_text=AsyncMock())
            provider.application = SimpleNamespace(bot=bot)
            spec = interactions.model_switch_delivery(request, {}).interaction
            provider._remember_interaction(spec, {"chat_id": 100, "message_id": 10})

            async def deliver(delivery, **kwargs):
                if delivery.interaction is not None:
                    if delivery.delivery_kind == "interactive_update":
                        await provider._open_or_update_interaction_async(
                            ResponseHandle("telegram_main", {"chat_id": 100}),
                            spec=delivery.interaction, allow_update=True,
                        )
                    elif delivery.delivery_kind == "interactive_resolve":
                        await provider._resolve_interaction_async(delivery.interaction)
                return True

            self.core._deliver_control_delivery_async = deliver
            await self.step(request, "think", think_level="high", interaction_origin="button",
                            interaction_id=request.request_id, interaction_kind="control_panel")
            texts = [call.kwargs["text"] for call in bot.edit_message_text.await_args_list]
            self.assertEqual(len(texts), 2, texts)
            self.assertIn("Compacting", texts[0])
            self.assertIn("Model updated" if success else "Model switch failed", texts[-1])
            self.assertIsNone(provider._restore_interaction(request.request_id))
            self.assertEqual(self.llm.active_endpoint_id, "b" if success else "a")

    async def test_button_compact_delivers_final_success(self):
        await self._check_button_compact_result(success=True)

    async def test_button_compact_delivers_final_failure(self):
        await self._check_button_compact_result(success=False)

    async def test_notification_failure_does_not_report_committed_switch_as_failed(self):
        request = await self.select()
        await self.step(request, "confirm")
        observed = []
        async def resume():
            observed.append((self.llm.active_endpoint_id, self.core._compaction_gate_active()))
        self.core._start_next_queued_turn_async = resume
        original = self.core._complete_action_reply_async
        async def reply(action, text):
            if text.startswith("Model updated"):
                raise RuntimeError("delivery unavailable")
            await original(action, text)
        self.core._complete_action_reply_async = reply
        with self.assertRaisesRegex(RuntimeError, "delivery unavailable"):
            await self.step(request, "think", think_level="high")
        self.assertEqual(self.settings.active, "b")
        self.assertEqual(self.settings.think["b"], "high")
        self.assertEqual(observed, [("b", False)])
        self.assertNotIn("Model switch failed", self.replies())
        self.assertNotIn("model_switch", self.core.state.control_scopes[self.route.control_scope_key].pending_requests)

    async def test_confirm_then_think_compacts_on_old_endpoint_before_switching(self):
        request = await self.select()
        self.assertEqual(request.payload["stage"], "confirm")
        await self.step(request, "think", think_level="high")
        self.assertEqual(self.compact_calls, [])
        await self.step(request, "confirm")
        self.assertEqual(self.llm.active_endpoint_id, "a")
        self.assertNotIn("b", self.settings.think)
        await self.step(request, "think", think_level="high")
        self.assertEqual(self.compact_calls[0][0], "a")
        self.assertEqual(self.compact_calls[0][1]["preferred_endpoint_id"], "a")
        self.assertEqual(self.llm.active_endpoint_id, "b", self.replies())
        self.assertEqual(self.settings.think["b"], "high")
        await self.step(request, "think", think_level="high")
        self.assertEqual(len(self.compact_calls), 1)

    async def test_busy_between_menu_and_submission_does_not_switch_or_queue(self):
        request = await self.select()
        await self.step(request, "confirm")
        self.core.state.pending_channel_turns.append(object())
        await self.step(request, "think", think_level="low")
        self.assertEqual(self.llm.active_endpoint_id, "a")
        self.assertEqual(self.compact_calls, [])
        self.assertNotIn("model_switch", self.core.state.control_scopes[self.route.control_scope_key].pending_requests)

    async def test_failure_preserves_history_and_resumes_old_model(self):
        original = self.memory.l1_source_stamp()
        self.compact_success = False
        observed = []
        async def resume():
            observed.append((self.llm.active_endpoint_id, self.core._compaction_gate_active()))
        self.core._start_next_queued_turn_async = resume
        request = await self.select()
        await self.step(request, "confirm")
        await self.step(request, "think", think_level="high")
        self.assertEqual(observed, [("a", False)])
        self.assertEqual(self.memory.l1_source_stamp(), original)

    async def test_gate_holds_new_input_until_target_is_applied(self):
        self.compact_release.clear()
        observed = []
        async def resume():
            observed.append((self.llm.active_endpoint_id, self.core._compaction_gate_active()))
        self.core._start_next_queued_turn_async = resume
        request = await self.select()
        await self.step(request, "confirm")
        task = asyncio.create_task(self.step(request, "think", think_level="high"))
        await asyncio.wait_for(self.compact_started.wait(), timeout=5)
        self.assertTrue(self.core._compaction_gate_active())
        envelope = self.fixture._make_channel_envelope(turn_id="next", request_id="next", text="next question")
        await self.core.schedule_channel_turn_async(envelope)
        self.assertEqual(len(self.core.state.pending_channel_turns), 1)
        self.assertEqual(observed, [])
        self.compact_release.set()
        await task
        self.assertEqual(observed, [("b", False)], self.replies())

    async def test_cancel_during_compact_fences_late_completion(self):
        self.compact_release.clear()
        request = await self.select()
        await self.step(request, "confirm")
        task = asyncio.create_task(self.step(request, "think", think_level="low"))
        await asyncio.wait_for(self.compact_started.wait(), timeout=5)
        await self.step(request, "cancel")
        self.compact_release.set()
        await task
        self.assertEqual(self.llm.active_endpoint_id, "a")
        self.assertFalse(self.core._compaction_gate_active())

    async def test_cancel_before_generation_does_not_start_compact(self):
        request = await self.select()
        await self.step(request, "confirm")
        async def cancel_before_generation(route):
            await self.step(request, "cancel")
        self.core._flush_control_status_async = cancel_before_generation
        await self.step(request, "think", think_level="low")
        self.assertEqual(self.compact_calls, [])
        self.assertEqual(self.llm.active_endpoint_id, "a")
        self.assertFalse(self.core._compaction_gate_active())

    async def test_configuration_change_and_expiry_invalidate_selection(self):
        request = await self.select()
        self.target.credential_ref = "CHANGED"
        await self.step(request, "confirm")
        self.assertIn("configuration changed", self.replies())
        request = await self.select()
        request.expires_at = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        await self.step(request, "confirm")
        self.assertEqual(self.compact_calls, [])
        self.assertEqual(self.llm.active_endpoint_id, "a")

    async def test_family_switch_and_empty_history_do_not_compact(self):
        self.target.model_id = "gpt-5.6-luna"
        request = await self.select()
        self.assertEqual(request.payload["stage"], "think")
        await self.step(request, "think", think_level="low")
        self.assertEqual(self.compact_calls, [])
        self.assertEqual(self.llm.active_endpoint_id, "b")

    async def test_empty_history_switches_across_providers_without_compact(self):
        self.memory.soft_reset()
        self.target.provider = "another_provider"
        request = await self.select()
        self.assertEqual(request.payload["stage"], "think")
        await self.step(request, "think", think_level="low")
        self.assertEqual(self.llm.active_endpoint_id, "b", self.replies())
        self.assertEqual(self.compact_calls, [])

    async def test_active_turn_and_unfinished_teardown_reject_selection(self):
        with patch.object(self.core.turn_manager, "latest_active_turn_id", return_value="active"):
            self.assertIsNone(await self.select())
        done = asyncio.Event()
        task = asyncio.create_task(done.wait())
        self.core.state.turn_tasks["finishing"] = task
        try:
            self.assertIsNone(await self.select())
            self.assertEqual(self.llm.active_endpoint_id, "a")
        finally:
            done.set()
            await task

    async def test_other_route_cannot_confirm_and_reopening_discards_draft(self):
        request = await self.select()
        other = replace(self.route, control_scope_key="socket:other", endpoint_id="other")
        await self.core.handle_control_action_async(ControlAction(
            action_kind="model_switch_step", target_scope="runtime", route=other,
            args={"request_id": request.request_id, "operation": "confirm"},
        ))
        self.assertEqual(request.payload["stage"], "confirm")
        await self.core.handle_control_action_async(ControlAction(
            action_kind="show_model", target_scope="runtime", route=self.route,
        ))
        await self.step(request, "confirm")
        self.assertEqual(self.compact_calls, [])
        self.assertIn("already consumed", self.replies())

    async def test_apply_failure_reports_committed_summary_and_keeps_old_selection(self):
        request = await self.select()
        await self.step(request, "confirm")
        with patch.object(self.llm, "apply_model_selection", side_effect=RuntimeError("write failed")):
            await self.step(request, "think", think_level="low")
        self.assertEqual(self.llm.active_endpoint_id, "a")
        self.assertEqual(len(self.memory.history.turns), 0)
        self.assertIn("completed Compact remains", self.replies())

    async def test_endpoint_refresh_cancels_compact_before_commit(self):
        original = self.memory.l1_source_stamp()
        self.compact_release.clear()
        request = await self.select()
        await self.step(request, "confirm")
        task = asyncio.create_task(self.step(request, "think", think_level="low"))
        await asyncio.wait_for(self.compact_started.wait(), timeout=5)
        await self.core.handle_control_action_async(ControlAction(
            action_kind="refresh_llm_endpoint", target_scope="runtime", route=self.route,
        ))
        self.compact_release.set()
        await task
        self.assertEqual(self.llm.active_endpoint_id, "a")
        self.assertEqual(self.memory.l1_source_stamp(), original)

    async def test_real_compact_engine_installs_summary_before_model_commit(self):
        calls = []
        class Invoker:
            def invoke(inner, endpoint, request, **kwargs):
                calls.append((endpoint.endpoint_id, request))
                return generation_result_from_values(text=_valid_pal_payload("Keep the task constraints.")).response, ()
        self.llm.endpoint_invoker = Invoker()
        self.core.turn_executor.compact_memory_async = self.real_compact
        request = await self.select()
        await self.step(request, "confirm")
        await self.step(request, "think", think_level="high")
        self.assertEqual(self.llm.active_endpoint_id, "b", self.replies())
        self.assertEqual([name for name, _ in calls], ["a"])
        self.assertIsNotNone(self.memory.l1_store.turns.continuity)
        self.assertNotIn("opaque-original", str(self.memory.history.turns))
        self.assertFalse(self.core._compaction_gate_active())

    async def test_text_commands_follow_the_same_confirmation_steps(self):
        request = await self.select()
        for command in (f"confirm {request.request_id}", f"think {request.request_id} high"):
            action = self.fixture.control_plane._handle_model(ControlCommandInvocation(
                command_name="model", argv=tuple(command.split()), route=self.route,
            ))
            await self.core.handle_control_action_async(action)
        self.assertEqual(self.llm.active_endpoint_id, "b", self.replies())
