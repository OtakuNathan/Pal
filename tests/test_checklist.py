from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from pal.checklist.capabilities import (
    ChecklistIntrospectionProvider,
    register_with_core as register_checklist_with_core,
)
from pal.checklist.prompt import ChecklistPromptFragmentProvider
from pal.checklist.service import ChecklistService
from pal.core import PalCore
from pal.core.capabilities import register_with_core as register_core_with_core
from pal.core.turn_executor import TurnExecutor
from pal.core.turns import MailboxReplyEffect, MailboxReplyStreamUpdateEffect, TurnContinuation, channel_turn_program
from pal.execution import register_with_core as register_execution_with_core
from pal.execution.contracts import CapabilityCall
from pal.foundation import EventEnvelope
from pal.shared import (
    ChannelEnvelope,
    ChannelMessage,
    ChannelStreamUpdateKind,
    EndpointConfig,
    EventKind,
    PromptAssemblyContext,
    ResponseHandle,
    RuntimeStatus,
    SourceKind,
    TurnDeliveryBinding,
)
from pal.shared.tool_protocol import ToolExecutionResult


def _continuation() -> TurnContinuation:
    event = EventEnvelope(
        event_kind=EventKind.USER_MESSAGE,
        source_kind=SourceKind.CHANNEL,
        payload={"text": "test"},
        event_id="test-turn",
    )
    envelope = ChannelEnvelope(
        event=event,
        endpoint=EndpointConfig("test", "memory", "memory://test"),
        response_handle=ResponseHandle("test", {}),
    )
    binding = TurnDeliveryBinding.from_envelope(envelope, control_scope_key="test")
    return TurnContinuation(
        turn_id="test-turn",
        opening_event=event,
        delivery_binding=binding,
        program=channel_turn_program(event),
        correlation_id="test",
    )


def _tool_result(name: str, *, structured: dict | None) -> ToolExecutionResult:
    return ToolExecutionResult(
        name=name,
        ok=True,
        llm_text="ok",
        text="ok",
        structured=structured or {},
        call_id="call-1",
    )


class TestChecklistService:
    def test_upsert_show_clear_lifecycle(self):
        service = ChecklistService()
        assert service.show() is None

        snapshot = service.upsert(
            [
                {"step": "inspect the contract", "status": "completed"},
                {"step": "implement the behavior"},
                {"step": "run the tests"},
            ]
        )
        assert snapshot.active is True
        assert snapshot.done == 1
        assert snapshot.total == 3
        assert [item["step"] for item in snapshot.plan] == [
            "inspect the contract",
            "implement the behavior",
            "run the tests",
        ]
        assert "✅ inspect the contract" in snapshot.markdown
        assert "⬜ implement the behavior" in snapshot.markdown
        assert snapshot.markdown.startswith("Checklist progress 1/3")

        shown = service.show()
        assert shown is not None and shown.total == 3

        assert service.clear() is True
        assert service.show() is None
        assert service.clear() is False

    def test_check_marks_completed_and_is_idempotent(self):
        service = ChecklistService()
        service.upsert([{"step": "step one"}, {"step": "step two"}])

        outcome = service.check("step one")
        assert outcome.changed is True
        assert outcome.found is True
        assert outcome.snapshot is not None
        assert outcome.snapshot.done == 1
        assert outcome.snapshot.plan[0]["status"] == "completed"

        again = service.check("step one")
        assert again.changed is False
        assert again.found is True
        assert again.snapshot is not None and again.snapshot.done == 1

    def test_check_unknown_step_and_no_active(self):
        service = ChecklistService()
        assert service.check("anything").found is False
        assert service.check("anything").snapshot is None

        service.upsert([{"step": "known"}])
        outcome = service.check("missing")
        assert outcome.found is False
        assert outcome.changed is False
        assert outcome.snapshot is not None and outcome.snapshot.done == 0

    def test_upsert_replaces_previous_plan(self):
        service = ChecklistService()
        service.upsert([{"step": "old"}])
        snapshot = service.upsert([{"step": "new one"}, {"step": "new two"}])
        assert [item["step"] for item in snapshot.plan] == ["new one", "new two"]

    def test_upsert_validation(self):
        service = ChecklistService()
        with pytest.raises(ValueError):
            service.upsert([])
        with pytest.raises(ValueError):
            service.upsert([{"step": "  "}])
        with pytest.raises(ValueError):
            service.upsert([{"step": "ok", "status": "bogus"}])
        with pytest.raises(ValueError):
            service.upsert([{"step": f"x{i}"} for i in range(65)])


class TestChecklistCapabilities:
    def setup_method(self) -> None:
        self.provider = ChecklistIntrospectionProvider(service=ChecklistService())

    def test_tool_guidance_is_a_concise_local_contract(self):
        blueprint = ChecklistIntrospectionProvider.upsert.__capability_action_blueprints__[0]
        assert blueprint.guidance is not None
        assert blueprint.guidance.purpose == "Create or replace Pal's active execution-cursor checklist."
        assert "before the first mutation" in blueprint.guidance.use_when
        assert "at least two concrete execution steps" in blueprint.guidance.use_when
        assert "Strongly prefer" not in blueprint.guidance.use_when
        assert "bunshin_start_workflow" not in blueprint.guidance.use_when
        assert "remember_memory" not in blueprint.guidance.do_not_use_when

    def test_upsert_returns_structured_snapshot(self):
        result = self.provider.upsert(
            CapabilityCall(name="checklist_upsert", args={"plan": [{"step": "a"}, {"step": "b"}]})
        )
        assert result.status == RuntimeStatus.OK
        assert result.structured is not None
        assert result.structured["active"] is True
        assert result.structured["total"] == 2
        assert result.structured["echo"]["tag"] == "checklist"
        assert result.structured["echo"]["payload"]["action"] == "upsert"
        assert result.llm_text.strip()

    def test_identical_upsert_is_a_noop_without_broadcast(self):
        call = CapabilityCall(name="checklist_upsert", args={"plan": [{"step": "a"}]})
        self.provider.upsert(call)
        repeated = self.provider.upsert(call)
        assert repeated.structured is not None
        assert repeated.structured["changed"] is False
        assert "echo" not in repeated.structured

    def test_check_emits_echo_declaration(self):
        self.provider.upsert(CapabilityCall(name="checklist_upsert", args={"plan": [{"step": "a"}, {"step": "b"}]}))
        result = self.provider.check(CapabilityCall(name="checklist_check", args={"step": "a"}))
        assert result.status == RuntimeStatus.OK
        assert result.structured is not None
        assert result.structured["changed"] is True
        echo = result.structured["echo"]
        assert echo["tag"] == "checklist"
        assert echo["payload"]["action"] == "check"
        assert echo["payload"]["done"] == 1
        assert "✅ a" in echo["markdown"]

    def test_check_without_active_returns_error(self):
        result = self.provider.check(CapabilityCall(name="checklist_check", args={"step": "a"}))
        assert result.status == RuntimeStatus.ERROR
        assert result.structured is not None and result.structured["error"] == "no_active_checklist"

    def test_check_unknown_step_returns_error_without_echo(self):
        self.provider.upsert(CapabilityCall(name="checklist_upsert", args={"plan": [{"step": "a"}]}))
        result = self.provider.check(CapabilityCall(name="checklist_check", args={"step": "zzz"}))
        assert result.status == RuntimeStatus.ERROR
        assert result.structured is not None and result.structured["error"] == "step_not_found"
        assert "echo" not in result.structured

    def test_show_and_clear(self):
        self.provider.upsert(CapabilityCall(name="checklist_upsert", args={"plan": [{"step": "a"}]}))
        shown = self.provider.show(CapabilityCall(name="checklist_show", args={}))
        assert shown.status == RuntimeStatus.OK
        assert shown.structured is not None and shown.structured["total"] == 1

        cleared = self.provider.clear(CapabilityCall(name="checklist_clear", args={}))
        assert cleared.status == RuntimeStatus.OK
        assert cleared.structured is not None
        assert cleared.structured["cleared"] is True
        assert cleared.structured["retired_checklist"]["active"] is False
        assert cleared.structured["retired_checklist"]["plan"] == [
            {"step": "a", "status": "pending"},
        ]
        assert cleared.structured["echo"] == {
            "markdown": "Checklist cleared.",
            "tag": "checklist",
            "payload": {
                "action": "clear",
                "active": False,
                "plan": [],
                "done": 0,
                "total": 0,
            },
        }
        repeated = self.provider.clear(CapabilityCall(name="checklist_clear", args={}))
        assert repeated.structured == {"cleared": False}

        clear_blueprint = ChecklistIntrospectionProvider.clear.__capability_action_blueprints__[0]
        assert "cancelled, replaced, or made stale" in clear_blueprint.guidance.use_when
        assert "still expected to continue" in clear_blueprint.guidance.do_not_use_when


class TestChecklistPrompt:
    def test_task_flow_matches_checklist_lifecycle_guidance(self):
        provider = ChecklistPromptFragmentProvider(service=ChecklistService())

        fragments = provider.build_prompt_fragments(PromptAssemblyContext())

        assert len(fragments) == 2
        operating_rule, task_flow = fragments
        assert operating_rule.section == "operating_guidance"
        assert operating_rule.metadata["prompt_target"] == "developer"
        assert "you must use Pal's checklist as the work cursor" in operating_rule.content
        assert "before the first mutating action" in operating_rule.content
        assert task_flow.section == "task_flow"
        assert "checklist_check" in task_flow.content
        assert "checklist_clear" in task_flow.content
        assert "Cancellation, replacement, or staleness" in task_flow.content
        assert "do not finish remaining items" in task_flow.content
        assert "summarize to the user what was done" in task_flow.content
        assert "simple answer" in task_flow.content
        assert "never truth, evidence, or permission" in task_flow.content

    def test_active_checklist_is_projected_only_in_runtime_reminder_tail(self):
        service = ChecklistService()
        service.upsert(
            [
                {"step": "inspect the current state", "status": "completed"},
                {"step": "apply <the change>"},
            ]
        )
        provider = ChecklistPromptFragmentProvider(service=service)

        fragments = provider.build_prompt_fragments(PromptAssemblyContext())

        assert len(fragments) == 3
        reminder = fragments[2]
        assert reminder.metadata["prompt_target"] == "runtime_reminder"
        assert reminder.metadata["block_id"] == "checklist_state"
        assert "Checklist progress 1/2" in reminder.content
        assert "✅ inspect the current state" in reminder.content
        assert "⬜ apply &lt;the change&gt;" in reminder.content
        assert "⬜ apply <the change>" not in reminder.content
        assert 'authority="execution_cursor"' in reminder.content
        assert 'trusted_as_evidence="false"' in reminder.content
        assert "On cancellation, replacement, or staleness" in reminder.content
        assert "retired checklist" in reminder.content

    def test_clear_retires_checklist_from_next_prompt_tail(self):
        core = PalCore()
        register_core_with_core(core)
        service = ChecklistService()
        handle = register_checklist_with_core(core.context, service)
        service.upsert([{"step": "do the work"}])

        active_prompt = core.build_canonical_prompt(PromptAssemblyContext())

        assert handle.prompt_fragment_providers
        assert active_prompt.messages[1].role.value == "developer"
        assert "<task_flow>" in str(active_prompt.messages[1].text)
        assert active_prompt.metadata["reminder_sections"] == ("checklist_state",)
        assert "Checklist progress 0/1" in active_prompt.metadata["runtime_reminder_text"]

        service.clear()
        retired_prompt = core.build_canonical_prompt(PromptAssemblyContext())

        assert active_prompt.messages[0].text == retired_prompt.messages[0].text
        assert active_prompt.messages[1].text == retired_prompt.messages[1].text
        assert "Checklist work cursor" in active_prompt.messages[1].text
        assert "before the first mutating action" in active_prompt.messages[1].text
        assert retired_prompt.metadata["reminder_sections"] == ()
        assert retired_prompt.metadata["runtime_reminder_text"] == ""

    def test_progress_and_retirement_tools_are_direct_but_show_stays_indirect(self):
        core = PalCore()
        register_execution_with_core(core.context)
        register_checklist_with_core(core.context, ChecklistService())
        core.publish_module_capabilities("execution")
        core.publish_module_capabilities("checklist")

        generation = core.context.execution_runtime.registry_generation

        for alias in ("checklist_upsert", "checklist_check", "checklist_clear"):
            assert alias in generation.direct_aliases
        assert "checklist_show" in generation.indirect_aliases


class TestToolEchoFanOut:
    def _executor_with_echo_capture(self, captured: list):
        executor = object.__new__(TurnExecutor)

        async def fake_execute(continuation, effect):
            captured.append((continuation, effect))

        executor.execute_turn_effect_async = fake_execute  # type: ignore[attr-defined]
        return executor

    def test_echo_fans_out_mailbox_reply(self):
        captured: list = []
        executor = self._executor_with_echo_capture(captured)
        continuation = _continuation()
        result = _tool_result(
            "checklist_check",
            structured={"echo": {"markdown": "Checklist progress 1/1\n✅ a", "dedupe_key": "checklist:check:a"}},
        )
        asyncio.run(executor._maybe_echo_tool_result_async(continuation, result, result))
        assert len(captured) == 1
        effect = captured[0][1]
        assert effect.text == "Checklist progress 1/1\n✅ a"
        assert effect.terminal is False
        assert not hasattr(effect, "delivery_binding")
        assert "checklist:check:a" in continuation.echoed_keys

    def test_streaming_tagged_echo_uses_ordered_message_event(self):
        captured: list = []
        executor = self._executor_with_echo_capture(captured)
        continuation = _continuation()
        continuation.channel_stream_active = True
        result = _tool_result(
            "checklist_check",
            structured={
                "echo": {
                    "markdown": "Checklist progress 1/1",
                    "dedupe_key": "checklist:stream:a",
                    "tag": "checklist",
                    "payload": {"action": "check", "done": 1, "total": 1},
                }
            },
        )

        asyncio.run(executor._maybe_echo_tool_result_async(continuation, result, result))

        assert len(captured) == 1
        effect = captured[0][1]
        assert isinstance(effect, MailboxReplyStreamUpdateEffect)
        assert effect.update.kind == ChannelStreamUpdateKind.MESSAGE
        assert effect.update.text == "Checklist progress 1/1"
        assert effect.update.message == ChannelMessage(
            text="Checklist progress 1/1",
            tag="checklist",
            payload={"action": "check", "done": 1, "total": 1},
        )
        assert continuation.emitted_reply_texts == ["Checklist progress 1/1"]

    def test_nonterminal_echo_marks_only_the_queued_reply_as_continuing(self):
        captured: list[tuple[TurnDeliveryBinding, ChannelMessage]] = []

        class _OutputPort:
            def queue_reply(self, binding: TurnDeliveryBinding, message: ChannelMessage) -> str:
                captured.append((binding, message))
                return "reply-1"

        executor = object.__new__(TurnExecutor)
        executor.context = SimpleNamespace(
            port_registry={"agent_io:output": _OutputPort()}
        )
        executor._debug_log_reply = lambda *_args: None
        continuation = _continuation()

        asyncio.run(
            executor._handle_mailbox_reply(
                MailboxReplyEffect(text="Checklist progress 1/3", terminal=False),
                continuation,
            )
        )

        assert captured[0][0].response_handle.reply_target["_pal_turn_continues"] is True
        assert captured[0][1].text == "Checklist progress 1/3"
        assert "_pal_turn_continues" not in continuation.delivery_binding.response_handle.reply_target
        assert continuation.emitted_reply_texts == ["Checklist progress 1/3"]

    def test_no_echo_without_declaration(self):
        captured: list = []
        executor = self._executor_with_echo_capture(captured)
        continuation = _continuation()
        result = _tool_result("checklist_check", structured={"changed": True})
        asyncio.run(executor._maybe_echo_tool_result_async(continuation, result, result))
        assert captured == []

    def test_empty_or_oversized_markdown_ignored(self):
        captured: list = []
        executor = self._executor_with_echo_capture(captured)
        continuation = _continuation()
        for markdown in ("", "   "):
            result = _tool_result("t", structured={"echo": {"markdown": markdown, "dedupe_key": "k"}})
            asyncio.run(executor._maybe_echo_tool_result_async(continuation, result, result))
        assert captured == []
        oversized = _tool_result(
            "t", structured={"echo": {"markdown": "x" * 5000, "dedupe_key": "k2"}}
        )
        asyncio.run(executor._maybe_echo_tool_result_async(continuation, oversized, oversized))
        assert captured == []

    def test_dedupe_prevents_repeated_echo(self):
        captured: list = []
        executor = self._executor_with_echo_capture(captured)
        continuation = _continuation()
        result = _tool_result(
            "checklist_check",
            structured={"echo": {"markdown": "m", "dedupe_key": "same"}},
        )
        asyncio.run(executor._maybe_echo_tool_result_async(continuation, result, result))
        asyncio.run(executor._maybe_echo_tool_result_async(continuation, result, result))
        assert len(captured) == 1

    def test_no_envelope_ignores_echo(self):
        captured: list = []
        executor = self._executor_with_echo_capture(captured)
        continuation = _continuation()
        continuation.delivery_binding = None
        result = _tool_result("t", structured={"echo": {"markdown": "m", "dedupe_key": "k"}})
        asyncio.run(executor._maybe_echo_tool_result_async(continuation, result, result))
        assert captured == []

    def test_fallback_dedupe_key_from_tool_call(self):
        captured: list = []
        executor = self._executor_with_echo_capture(captured)
        continuation = _continuation()
        result = _tool_result("checklist_check", structured={"echo": {"markdown": "m"}})
        asyncio.run(executor._maybe_echo_tool_result_async(continuation, result, result))
        assert len(captured) == 1
        assert "checklist_check:call-1" in continuation.echoed_keys
