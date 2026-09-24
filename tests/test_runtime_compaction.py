from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, ToolResultIR, new_tool_call

import asyncio
import json
import unittest
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace

from pal.control import ControlAction, ControlRoute
from pal.core import (
    CompactionClockKind,
    CompactionEngine,
    CompactionSnapshot,
    L1CommitPayload,
    MemoryCompactEffect,
    PalCore,
    TurnContinuation,
    TurnOutcome,
    register_with_core as register_core_with_core,
)
from pal.core.compaction import build_compaction_units
from pal.core.pal_compaction import PalCompactionPolicy
from pal.core.runtime_config import RuntimeConfig
from pal.core.turns import (
    EffectResult,
    LLMPreflightEffect,
    agent_turn_program,
)
from pal.core.turn_events import TURN_END
from pal.foundation import EventEnvelope
from pal.execution.session_state import FileDeliveryManifest, FileDeliverySpan
from pal.llm import (
    generation_result_from_values,
    LLMPreflightAdvice,
)
from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
    PromptRegionIR,
    TextPartIR,
)
from pal.llm.runtime import LLMRuntime
from pal.memory import (
    L1MessageKind,
    L1TranscriptMessage,
    L2Entry,
    MemoryPackRequest,
    MemoryService,
    register_with_core as register_memory_with_core,
)
from pal.memory.tool_protocol import l1_tool_protocol_transcript
from pal.memory.turn_ir import L1TurnIR
from pal.core.pal_compaction import COMPACT_PAL_STRUCTURED_SYSTEM
from pal.bunshin.compact import BUNSHIN_COMPACTION_SYSTEM_PROMPT, BunshinCompactionPolicy
from pal.shared import (
    ChannelEnvelope,
    EndpointConfig,
    EventKind,
    LLMFinishReason,
    LLMPreflightStatus,
    PromptAssemblyContext,
    ResponseHandle,
    RuntimeStatus,
    SourceKind,
)


def _valid_pal_payload(
    summary: str = "compacted prior context",
    *,
    memory_candidates: list[dict[str, object]] | None = None,
) -> str:
    return json.dumps(
        {
            "schema": "pal.compaction.continuity.v1",
            "kind": "pal",
            "continuity": {
                "constraints": [],
                "state": [
                    "shared compaction",
                    "continue the active request",
                    "finish the compaction refactor",
                    "run focused tests"
                ],
                "decisions": [],
                "references": []
            },
            "summary": {"summary": summary},
            "memory_candidates": list(memory_candidates or []),
        },
        ensure_ascii=False,
    )


def _valid_bunshin_payload() -> str:
    return json.dumps(
        {
            "schema": "pal.compaction.continuity.v1",
            "kind": "bunshin",
            "continuity": {
                "constraints": [],
                "state": [
                    "goal: finish compaction; target: src/pal/core/compaction.py; action: run tests; status: active",
                    "symptom: one schema test fails; latest_evidence: missing next_actions; current_hypothesis: invalid fixture",
                    "issue: checkpoint restore; known_facts: ['the L1 checkpoint is complete']; status: open; excluded_paths: ['do not rebuild from truncated prompt']",
                    "action: rerun focused tests; target: tests/test_runtime_compaction.py; expected_result: pass"
                ],
                "decisions": [
                    "route: shared engine; rationale: one retry and commit boundary"
                ],
                "references": []
            },
            "summary": {"summary": "Bunshin is testing the shared compaction engine."},
        },
        ensure_ascii=False,
    )


class _ScriptedLLM:
    def __init__(
        self,
        outcomes: list[generation_result_from_values | Exception],
        *,
        preflight=None,
    ) -> None:
        self.outcomes = list(outcomes)
        self.preflight_hook = preflight
        self.preflight_requests = []
        self.generate_requests = []

    async def apreflight(self, request):
        self.preflight_requests.append(request)
        if self.preflight_hook is not None:
            return self.preflight_hook(request)
        return LLMPreflightAdvice(status=LLMPreflightStatus.READY)

    async def agenerate(self, request):
        self.generate_requests.append(request)
        if not self.outcomes:
            raise RuntimeError("no scripted outcome")
        value = self.outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class _SlowLLM(_ScriptedLLM):
    async def agenerate(self, request):
        self.generate_requests.append(request)
        await asyncio.sleep(1)
        return generation_result_from_values(text=_valid_pal_payload())


class _PurposeAwareLLM:
    def __init__(
        self,
        *,
        memory_candidates: list[dict[str, object]] | None = None,
    ) -> None:
        self.requests = []
        self.memory_candidates = list(memory_candidates or [])

    def preflight(self, request):
        self.requests.append(("preflight", request))
        return LLMPreflightAdvice(status=LLMPreflightStatus.READY)

    def generate(self, request):
        self.requests.append(("generate", request))
        if "compaction" in str(request.metadata.get("purpose") or ""):
            return generation_result_from_values(
                text=_valid_pal_payload(
                    "manual structured compact summary",
                    memory_candidates=self.memory_candidates,
                )
            )
        return generation_result_from_values(text="done")


def _memory_with_turns(count: int = 4) -> MemoryService:
    service = MemoryService()
    for index in range(count):
        service.l1_store.append(
            [
                L1TranscriptMessage(
                    role="user",
                    content=f"old request {index}",
                    kind=L1MessageKind.USER_REQUEST,
                ),
                L1TranscriptMessage(
                    role="assistant",
                    content=f"old reply {index}",
                    kind=L1MessageKind.ASSISTANT_REPLY,
                ),
            ]
        )
    service.history_root.promote()  # Existing fixture turns represent submitted history.
    return service



def _attach_hot_cache(llm, service):
    service.begin_l1_turn("cached-turn", user_text="cached request")
    anchor = service.active_l1_turn("cached-turn").messages[0]
    service.upsert_l1_assistant(
        "cached-turn", LLMMessageIR(role=MessageRole.ASSISTANT, parts=(TextPartIR("cached reply"),)),
    )
    service.settle_l1_turn("cached-turn")
    # v3 warm replay (W02/W03): a provider-confirmed anchor request carries
    # the FULL conversation prefix that was actually cached, ending exactly
    # at the anchor message, plus the session continuity id.
    conversation = [
        message
        for turn in service.l1_store.turns.turns
        for message in turn.messages
        if message.role.value not in {"system", "developer"}
    ]
    anchor_index = [m.message_id for m in conversation].index(anchor.message_id)
    prefix_through_anchor = list(conversation[: anchor_index + 1])
    prefix_through_anchor[-1] = replace(
        anchor, prompt_region=PromptRegionIR.ACTIVE_INPUT
    )
    from pal.memory.contracts import MemoryPackRequest

    continuity_id = str(
        service.build_pack(
            MemoryPackRequest(turn_kind="chat", include_l1_recent_context=False)
        ).metadata.get("continuity_id", "")
        or ""
    )
    cached_request = LLMRequestIR(
        messages=tuple(prefix_through_anchor),
        tools=(), policy=GenerationPolicyIR(max_output_tokens=1024),
        model_hint="gpt-5.6-luna", logical_scope_id="pal:resident",
        metadata={"continuity_id": continuity_id} if continuity_id else {},
    )
    live = {"eligible": True, "anchor_epoch": "epoch-a", "anchor_remaining_ttl_seconds": 1800}
    llm.prompt_cache_warm_deadline_snapshot = lambda: live
    llm.prompt_cache_confirmed_anchor_request = lambda **kwargs: {
        "request": cached_request, "anchor_message_id": anchor.message_id,
        "dialect": "openrouter_openai_explicit", "wire_shape": "openai_response",
    }
    return live



def _idle_program():
    if False:
        yield None


def _closed_protocol(content: str) -> list[dict[str, object]]:
    return [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-closed",
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-closed",
            "content": content,
            "_pal_result_state": {
                "ok": True,
                "kind": "complete",
                "effect": "none",
            },
        },
    ]


def test_l1_tool_protocol_discards_provider_specific_fields() -> None:
    transcript, _ = l1_tool_protocol_transcript(
        [
            {
                "role": "assistant",
                "content": "",
                "provider_specific_fields": {
                    "reasoning_content": "inspect before calling the tool"
                },
                "tool_calls": [
                    {
                        "id": "call-provider-fields",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-provider-fields",
                "content": "ok",
            },
        ]
    )

    assert transcript[0].payload == {}


class SharedCompactionEngineTests(unittest.TestCase):
    def test_policy_prompt_owns_schema_and_llm_runtime_has_no_host_api(self) -> None:
        self.assertIn("pal.compaction.continuity.v1", COMPACT_PAL_STRUCTURED_SYSTEM)
        self.assertIn("memory_candidates", COMPACT_PAL_STRUCTURED_SYSTEM)
        self.assertIn("constraints", COMPACT_PAL_STRUCTURED_SYSTEM)
        self.assertFalse(hasattr(LLMRuntime, "compact_memory_structured"))
        self.assertFalse(hasattr(LLMRuntime, "summarize_compaction"))

    def test_compaction_system_prompts_do_not_depend_on_request_budget(self) -> None:
        low_budget = CompactionSnapshot(
            target_input_budget=12_000,
            reserved_output_tokens=1_000,
            clock_kind=CompactionClockKind.USER_TURN,
            clock_value=1,
        )
        high_budget = CompactionSnapshot(
            target_input_budget=32_000,
            reserved_output_tokens=1_000,
            clock_kind=CompactionClockKind.USER_TURN,
            clock_value=2,
        )

        self.assertEqual(
            PalCompactionPolicy().system_prompt(low_budget),
            PalCompactionPolicy().system_prompt(high_budget),
        )
        self.assertEqual(
            BunshinCompactionPolicy().system_prompt(low_budget),
            BunshinCompactionPolicy().system_prompt(high_budget),
        )
        self.assertNotIn("must not exceed", COMPACT_PAL_STRUCTURED_SYSTEM)
        self.assertNotIn("must not exceed", BUNSHIN_COMPACTION_SYSTEM_PROMPT)
























class RuntimeCompactionIntegrationTests(unittest.TestCase):
    def test_compaction_failure_stops_and_requests_manual_compact_then_resend(self) -> None:
        program = agent_turn_program(
            turn_id="failed-compact",
            build_assembly_context=lambda _frame: PromptAssemblyContext(),
            render_final_text=lambda _outcome: "",
            build_commit_payload=lambda final, observations, _replies: L1CommitPayload(
                turn_id="failed-compact",
                transcript=[
                    L1TranscriptMessage(
                        role="assistant",
                        content=final,
                        kind=L1MessageKind.ASSISTANT_REPLY,
                    )
                ],
                tool_observations=list(observations),
            ),
        )
        effect = next(program)
        self.assertIsInstance(effect, LLMPreflightEffect)
        effect = program.send(
            EffectResult(
                status=RuntimeStatus.OK,
                payload=LLMPreflightAdvice(status=LLMPreflightStatus.COMPACT_REQUIRED),
            )
        )
        self.assertIsInstance(effect, MemoryCompactEffect)

        with self.assertRaises(StopIteration) as stopped:
            program.send(
                EffectResult(
                    status=RuntimeStatus.ERROR,
                    text="Automatic compaction request failed.",
                )
            )

        self.assertIn("Automatic compaction request failed", stopped.exception.value.final_reply)
        self.assertIn("`/compact` manually", stopped.exception.value.final_reply)
        self.assertIn("resend your request", stopped.exception.value.final_reply)

    def test_one_logical_turn_stops_after_three_compaction_generations(self) -> None:
        program = agent_turn_program(
            turn_id="bounded-compact",
            build_assembly_context=lambda _frame: PromptAssemblyContext(),
            render_final_text=lambda _outcome: "",
            build_commit_payload=lambda final, observations, _replies: L1CommitPayload(
                turn_id="bounded-compact",
                transcript=[
                    L1TranscriptMessage(
                        role="assistant",
                        content=final,
                        kind=L1MessageKind.ASSISTANT_REPLY,
                    )
                ],
                tool_observations=list(observations),
            ),
        )
        effect = next(program)
        for _ in range(3):
            self.assertIsInstance(effect, LLMPreflightEffect)
            effect = program.send(
                EffectResult(
                    status=RuntimeStatus.OK,
                    payload=LLMPreflightAdvice(
                        status=LLMPreflightStatus.COMPACT_REQUIRED
                    ),
                )
            )
            self.assertIsInstance(effect, MemoryCompactEffect)
            effect = program.send(EffectResult(status=RuntimeStatus.OK))

        self.assertIsInstance(effect, LLMPreflightEffect)
        with self.assertRaises(StopIteration) as stopped:
            program.send(
                EffectResult(
                    status=RuntimeStatus.OK,
                    payload=LLMPreflightAdvice(
                        status=LLMPreflightStatus.COMPACT_REQUIRED
                    ),
                )
            )
        self.assertIn(
            "three atomic L1 compactions",
            stopped.exception.value.final_reply,
        )

    def test_abort_closes_the_same_l1_turn_without_duplicate_suffix(self) -> None:
        service = MemoryService()
        service.begin_l1_turn(
            "settled-exit",
            user_text="continue",
            metadata={"_pal_input_id": "input-1"},
        )
        service.upsert_l1_assistant(
            "settled-exit",
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(
                    new_tool_call(
                        call_id="call-closed",
                        name="read_file",
                        arguments={},
                    ),
                ),
                semantic_kind="assistant_tool_call",
            ),
        )
        service.append_l1_tool_result(
            "settled-exit",
            ToolResultIR(
                call_id="call-closed",
                name="read_file",
                content="safe tail",
            ),
        )

        closed = service.abort_l1_turn("settled-exit", reason="test")

        self.assertEqual(closed.state.value, "aborted")
        self.assertEqual(closed.pending_call_ids, frozenset())
        self.assertEqual(len(closed.messages), 4)
        self.assertEqual(closed.messages[-1].role, MessageRole.ASSISTANT)
        self.assertEqual(closed.metadata["_pal_input_id"], "input-1")

    def test_effect_commit_failure_leaves_protocol_and_memory_unchanged(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        service = _memory_with_turns(2)
        register_memory_with_core(core.context, service)
        core.context.port_registry["llm:llm"] = _ScriptedLLM(
            [generation_result_from_values(text=_valid_pal_payload())]
        )
        original_compact = service.compact_left

        def fail_compact(run_id, summary_entry, **kwargs):
            raise RuntimeError("commit failed")

        service.compact_left = fail_compact
        self.addCleanup(setattr, service, "compact_left", original_compact)
        service.l1_store.append(l1_tool_protocol_transcript(_closed_protocol("safe tail"))[0])
        before_l1 = deepcopy(service.l1_store.items)

        result = asyncio.run(
            core.turn_executor.compact_memory_async(
                service,
                target_input_budget=8_192,
                reserved_output_tokens=2_048,
            )
        )

        self.assertEqual(result.status, "commit_failed")
        self.assertEqual(service.l1_store.items, before_l1)


    def test_semantic_compactor_sees_closed_tool_evidence(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        service = _memory_with_turns(1)
        register_memory_with_core(core.context, service)
        llm = _ScriptedLLM(
            [generation_result_from_values(text=_valid_pal_payload())]
        )
        core.context.port_registry["llm:llm"] = llm
        marker = "exact-error-evidence-before-projection"
        service.l1_store.append(l1_tool_protocol_transcript(_closed_protocol(marker))[0])

        async def run_compact():
            return await core.turn_executor.compact_memory_async(
                service,
                target_input_budget=8_192,
                reserved_output_tokens=2_048,
            )

        run_result = asyncio.run(run_compact())

        self.assertTrue(run_result.success)
        compaction_source = llm.generate_requests[0].messages[-1].text
        self.assertIn(marker, compaction_source)
        self.assertNotIn("full result retired", compaction_source)
        self.assertFalse(hasattr(core.turn_executor, "_tool_protocol_projector"))

    def test_manual_compact_replays_confirmed_resident_anchor_and_settled_tail(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        service = MemoryService()
        register_memory_with_core(core.context, service)
        service.begin_l1_turn("warm-turn", user_text="warm user request")
        active = service.active_l1_turn("warm-turn")
        self.assertIsNotNone(active)
        anchor = active.messages[0]
        service.upsert_l1_assistant(
            "warm-turn",
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(TextPartIR("settled final reply"),),
            ),
        )
        service.settle_l1_turn("warm-turn")
        cached_request = LLMRequestIR(
            messages=(
                LLMMessageIR(
                    role=MessageRole.SYSTEM,
                    parts=(TextPartIR("resident stable system"),),
                    prompt_region=PromptRegionIR.STABLE_SYSTEM,
                ),
                replace(
                    anchor,
                    prompt_region=PromptRegionIR.ACTIVE_INPUT,
                ),
            ),
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=1024),
            model_hint="gpt-5.6-luna",
            logical_scope_id="pal:resident",
        )
        llm = _ScriptedLLM(
            [generation_result_from_values(text=_valid_pal_payload())]
        )
        llm.prompt_cache_confirmed_anchor_request = lambda **_kwargs: {
            "request": cached_request,
            "anchor_message_id": anchor.message_id,
            "dialect": "openrouter_openai_explicit",
            "wire_shape": "openai_response",
        }
        core.context.port_registry["llm:llm"] = llm

        result = asyncio.run(
            core.turn_executor.compact_memory_async(
                service,
                target_input_budget=8_192,
                reserved_output_tokens=2_048,
            )
        )

        self.assertTrue(result.success)
        request = llm.generate_requests[0]
        self.assertEqual(request.logical_scope_id, "pal:resident")
        self.assertEqual(request.messages[:2], cached_request.messages)
        self.assertEqual(request.messages[2].text, "settled final reply")
        self.assertEqual(request.messages[-1].role, MessageRole.USER)
        self.assertIn("Do not call tools", request.messages[-1].text)
        self.assertEqual(
            sum("warm user request" in message.text for message in request.messages),
            1,
        )

    def test_compaction_does_not_reproject_retained_active_results(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        service = _memory_with_turns(1)
        register_memory_with_core(core.context, service)
        core.context.port_registry["llm:llm"] = _ScriptedLLM(
            [generation_result_from_values(text=_valid_pal_payload())]
        )
        turn_id = "active-result-owner"
        content = "alpha\nbeta"
        delivery = FileDeliveryManifest(
            file_key="/workspace/active.txt",
            digest="digest-active",
            total_lines=2,
            spans=(
                FileDeliverySpan(0, 5, 1, 1, 0, 5, 5),
                FileDeliverySpan(6, 10, 2, 2, 0, 4, 4),
            ),
            complete_file=True,
        ).to_dict()
        runtime = core.context.execution_runtime
        runtime.begin_tool_result_turn(
            turn_id=turn_id,
            scope_key="pal:resident",
            input_id="active-input",
        )
        call = new_tool_call(
            name="read_file",
            args={"file_path": "/workspace/active.txt"},
            call_id="read-active",
        )
        service.begin_l1_turn(turn_id, user_text="continue")
        service.upsert_l1_assistant(
            turn_id,
            LLMMessageIR(role=MessageRole.ASSISTANT, parts=(call,)),
        )
        service.append_l1_tool_result(
            turn_id,
            ToolResultIR(
                call_id=call.call_id,
                name=call.name,
                content=content,
                context_delivery=delivery,
            ),
        )
        runtime.commit_tool_delivery(
            turn_id=turn_id,
            context_delivery=delivery,
            result_id=call.call_id,
        )
        before = runtime.logical_state.file_grant(
            execution_lifetime_id="pal:resident",
            file_key="/workspace/active.txt",
            digest="digest-active",
        )
        self.assertIsNotNone(before)
        self.assertTrue(before.complete)
        continuation = SimpleNamespace(
            turn_id=turn_id,
            pending_tool_call_batch=[],
            pending_tool_results=[],
            pending_assistant_tool_text="",
            preferred_llm_endpoint_id=None,
            preferred_llm_model_id=None,
        )

        result = asyncio.run(
            core.turn_executor.compact_memory_async(
                service,
                target_input_budget=8_192,
                reserved_output_tokens=2_048,
                continuation=continuation,
            )
        )

        self.assertTrue(result.success)
        after = runtime.logical_state.file_grant(
            execution_lifetime_id="pal:resident",
            file_key="/workspace/active.txt",
            digest="digest-active",
        )
        self.assertIsNotNone(after)
        self.assertEqual(after.covered_ranges, before.covered_ranges)
        self.assertTrue(after.complete)

    def test_manual_compact_retires_removed_result_authority_by_lifetime(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        service = _memory_with_turns(1)
        register_memory_with_core(core.context, service)
        core.context.port_registry["llm:llm"] = _ScriptedLLM(
            [generation_result_from_values(text=_valid_pal_payload())]
        )
        runtime = core.context.execution_runtime
        turn_id = "old-result-owner"
        runtime.begin_tool_result_turn(
            turn_id=turn_id,
            scope_key="pal:resident",
            input_id="old-input",
        )
        delivery = FileDeliveryManifest(
            file_key="/workspace/input.txt",
            digest="digest-a",
            total_lines=1,
            spans=(FileDeliverySpan(0, 8, 1, 1, 0, 8, 8),),
            complete_file=True,
        ).to_dict()
        runtime.commit_tool_delivery(
            turn_id=turn_id,
            context_delivery=delivery,
            result_id="read-compact",
        )
        call = new_tool_call(
            name="read_file",
            args={"file_path": "/workspace/input.txt"},
            call_id="read-compact",
        )
        service.begin_l1_turn(turn_id, user_text="read the file")
        service.upsert_l1_assistant(
            turn_id,
            LLMMessageIR(role=MessageRole.ASSISTANT, parts=(call,)),
        )
        service.append_l1_tool_result(
            turn_id,
            ToolResultIR(
                call_id=call.call_id,
                name=call.name,
                content="1: alpha",
                context_delivery=delivery,
            ),
        )
        service.settle_l1_turn(turn_id)
        runtime.tool_result_pager._turn_contexts.pop(turn_id, None)
        self.assertIsNotNone(
            runtime.logical_state.file_grant(
                execution_lifetime_id="pal:resident",
                file_key="/workspace/input.txt",
                digest="digest-a",
            )
        )

        result = asyncio.run(
            core.turn_executor.compact_memory_async(
                service,
                target_input_budget=8_192,
                reserved_output_tokens=2_048,
            )
        )

        self.assertTrue(result.success)
        self.assertIsNone(
            runtime.logical_state.file_grant(
                execution_lifetime_id="pal:resident",
                file_key="/workspace/input.txt",
                digest="digest-a",
            )
        )

    def test_bunshin_compact_retires_authority_from_bound_execution_lifetime(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        service = _memory_with_turns(1)
        register_memory_with_core(core.context, service)
        core.turn_executor._compaction_engine = CompactionEngine(
            BunshinCompactionPolicy()
        )
        core.context.port_registry["llm:llm"] = _ScriptedLLM(
            [generation_result_from_values(text=_valid_bunshin_payload())]
        )
        runtime = core.context.execution_runtime
        turn_id = "bunshin-result-owner"
        execution_lifetime_id = "agent-session-123"
        runtime.begin_tool_result_turn(
            turn_id=turn_id,
            scope_key=execution_lifetime_id,
            input_id="bunshin-input",
        )
        delivery = FileDeliveryManifest(
            file_key="/workspace/bunshin-input.txt",
            digest="digest-bunshin",
            total_lines=1,
            spans=(FileDeliverySpan(0, 8, 1, 1, 0, 8, 8),),
            complete_file=True,
        ).to_dict()
        runtime.commit_tool_delivery(
            turn_id=turn_id,
            context_delivery=delivery,
            result_id="read-bunshin-compact",
        )
        call = new_tool_call(
            name="read_file",
            args={"file_path": "/workspace/bunshin-input.txt"},
            call_id="read-bunshin-compact",
        )
        service.begin_l1_turn(turn_id, user_text="read the bunshin file")
        service.upsert_l1_assistant(
            turn_id,
            LLMMessageIR(role=MessageRole.ASSISTANT, parts=(call,)),
        )
        service.append_l1_tool_result(
            turn_id,
            ToolResultIR(
                call_id=call.call_id,
                name=call.name,
                content="1: bunshin",
                context_delivery=delivery,
            ),
        )
        service.settle_l1_turn(turn_id)
        service.history_root.promote()  # Submitted result belongs to the compact source.
        continuation = SimpleNamespace(
            turn_id=turn_id,
            pending_tool_call_batch=[],
            pending_tool_results=[],
            pending_assistant_tool_text="",
            preferred_llm_endpoint_id=None,
            preferred_llm_model_id=None,
        )

        result = asyncio.run(
            core.turn_executor.compact_memory_async(
                service,
                target_input_budget=8_192,
                reserved_output_tokens=2_048,
                assembly_context=PromptAssemblyContext(
                    core_mode="bunshin",
                    turn_kind="bunshin",
                    work_order_id="work-1",
                    metadata={"prompt_cache_scope_id": "bunshin:run-1"},
                ),
                continuation=continuation,
            )
        )

        self.assertTrue(result.success)
        self.assertIsNone(
            runtime.logical_state.file_grant(
                execution_lifetime_id=execution_lifetime_id,
                file_key="/workspace/bunshin-input.txt",
                digest="digest-bunshin",
            )
        )
        self.assertNotIn(
            "bunshin:run-1",
            runtime.logical_state.snapshot_state()["sessions"],
        )

    def test_manual_compact_uses_same_engine_and_opens_candidate_approval(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        service = _memory_with_turns(2)
        register_memory_with_core(core.context, service)
        candidate = {
            "kind": "fact",
            "title": "Compact candidates need approval",
            "summary": "Compact candidates are reviewed.",
            "source_excerpt": "review compact candidates",
        }
        llm = _PurposeAwareLLM(memory_candidates=[candidate])
        core.context.port_registry["llm:llm"] = llm
        replies: list[str] = []
        statuses: list[tuple[str, dict[str, object]]] = []

        async def capture_reply(_route, text: str) -> None:
            replies.append(text)

        async def capture_status(
            _route,
            kind: str,
            payload: dict[str, object],
        ) -> None:
            statuses.append((kind, payload))

        core._reply_to_route_async = capture_reply
        core._status_to_route_async = capture_status

        asyncio.run(
            core._handle_compact_memory_async(
                ControlAction(
                    action_kind="compact_memory",
                    target_scope="memory",
                    route=ControlRoute(
                        endpoint_id="memory",
                        channel_kind="memory",
                    ),
                )
            )
        )

        summary = service.build_pack(MemoryPackRequest()).current_summary
        self.assertIsNotNone(summary)
        self.assertIn("manual structured compact summary", summary.rendered)
        compaction_requests = [
            request
            for kind, request in llm.requests
            if kind == "generate"
            and "compaction"
            in str(request.metadata.get("purpose") or "")
        ]
        self.assertEqual(len(compaction_requests), 1)
        self.assertEqual(replies[-1], "Context compacted. Continuity summary updated.")
        self.assertEqual(statuses[-1][0], "interactive_open")
        spec = statuses[-1][1]["spec"]
        self.assertIn("Nothing is saved until final submission", spec.text)
        self.assertIn("Reopen: /memory_review", spec.text)
        self.assertEqual(len(spec.items), 1)
        self.assertEqual(len(spec.buttons), 0)
        self.assertEqual(spec.items[0].item_id, "c1")
        self.assertEqual([button.label for button in spec.items[0].buttons[0]], ["Accept", "Reject", "Edit"])
        button = spec.items[0].buttons[0][0]
        self.assertEqual(button.action_args["action_kind"], "memory_candidate_decision")
        self.assertEqual(button.action_args["args"]["decision"], "accept")

    def test_cache_reminder_compact_is_consumed_before_llm_and_runs_once(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        service = _memory_with_turns(2)
        register_memory_with_core(core.context, service)
        candidate = {
            "kind": "fact",
            "title": "One candidate batch",
            "summary": "A duplicate click must not compact twice.",
            "source_excerpt": "duplicate click",
        }
        llm = _PurposeAwareLLM(memory_candidates=[candidate])
        _attach_hot_cache(llm, service)
        core.context.port_registry["llm:llm"] = llm
        statuses: list[tuple[str, dict[str, object]]] = []
        claimed_epochs: list[str] = []

        def claim_once(epoch: str) -> bool:
            claimed_epochs.append(epoch)
            return len(claimed_epochs) == 1

        async def capture_status(
            _route,
            kind: str,
            payload: dict[str, object],
        ) -> None:
            statuses.append((kind, payload))

        core.cache_warm_deadline.claim_compaction = claim_once
        core._status_to_route_async = capture_status
        action = ControlAction(
            action_kind="compact_memory",
            target_scope="memory",
            route=ControlRoute(
                endpoint_id="telegram_main",
                channel_kind="telegram",
                reply_target={"chat_id": "42"},
            ),
            args={
                "cache_epoch": "epoch-a",
                "interaction_origin": "button",
                "interaction_id": "cache_warm_epoch-a",
                "interaction_kind": "cache_warm_deadline",
            },
        )

        async def run_duplicate_clicks() -> None:
            await core._handle_compact_memory_async(action)
            await core._handle_compact_memory_async(action)

        asyncio.run(run_duplicate_clicks())

        compaction_requests = [
            request
            for kind, request in llm.requests
            if kind == "generate"
            and "compaction" in str(request.metadata.get("purpose") or "")
        ]
        self.assertEqual(claimed_epochs, ["epoch-a", "epoch-a"])
        self.assertEqual(len(compaction_requests), 1)
        self.assertEqual(statuses[0][0], "interactive_update")
        pending_spec = statuses[0][1]["spec"]
        self.assertEqual(pending_spec.buttons, ())
        self.assertIn("正在利用热缓存", pending_spec.text)
        candidate_notices = [
            payload["spec"]
            for kind, payload in statuses
            if kind == "interactive_open"
            and payload["spec"].interaction_kind == "memory_candidate_approval"
        ]
        self.assertEqual(len(candidate_notices), 1)

    def test_manual_compact_is_rejected_while_resident_turn_is_active(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        service = _memory_with_turns(1)
        register_memory_with_core(core.context, service)
        llm = _PurposeAwareLLM()
        core.context.port_registry["llm:llm"] = llm
        core.state.active_turn_id = "busy-turn"
        core.state.active_turns["busy-turn"] = SimpleNamespace(turn_id="busy-turn")
        replies: list[str] = []

        async def capture_reply(_route, text: str) -> None:
            replies.append(text)

        core._reply_to_route_async = capture_reply

        asyncio.run(
            core._handle_compact_memory_async(
                ControlAction(
                    action_kind="compact_memory",
                    target_scope="memory",
                    route=ControlRoute(
                        endpoint_id="memory",
                        channel_kind="memory",
                    ),
                )
            )
        )

        self.assertIn("current turn finishes", replies[-1])
        self.assertEqual(llm.requests, [])
        self.assertEqual(len(service.l1_store.turns.turns), 1)

    def test_pal_clock_advances_only_after_successful_user_turn_commit(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        service = MemoryService()
        register_memory_with_core(core.context, service)
        core.context.port_registry["llm:llm"] = _PurposeAwareLLM()
        scheduled_deadlines: list[dict[str, object]] = []
        core.cache_warm_deadline.schedule_after_turn_commit = (
            lambda **kwargs: scheduled_deadlines.append(dict(kwargs)) or True
        )

        def envelope(turn_id: str) -> ChannelEnvelope:
            return ChannelEnvelope(
                event=EventEnvelope(
                    event_kind="user.message",
                    source_kind="channel",
                    payload={"text": f"turn {turn_id}"},
                    event_id=turn_id,
                ),
                endpoint=EndpointConfig(
                    endpoint_id="memory",
                    channel_kind="memory",
                    binding_key="memory",
                ),
                response_handle=ResponseHandle(endpoint_id="memory"),
            )

        asyncio.run(core.process_channel_turn_async(envelope("one")))
        asyncio.run(core.process_channel_turn_async(envelope("two")))

        self.assertEqual(core.state.compaction_user_turn_count, 2)
        self.assertEqual(len(scheduled_deadlines), 2)
        self.assertEqual(
            scheduled_deadlines[-1]["route"].endpoint_id,
            "memory",
        )
        self.assertEqual(scheduled_deadlines[-1]["turn_id"], "two")

        proactive_outcome = TurnOutcome(
            turn_id="proactive",
            final_reply="background update",
            commit_payload=L1CommitPayload(
                turn_id="proactive",
                transcript=[
                    L1TranscriptMessage(
                        role="assistant",
                        content="background update",
                    )
                ],
            ),
        )
        service.begin_l1_turn("proactive", user_text="timer")
        asyncio.run(
            core._schedule_post_turn_commit_async(
                proactive_outcome,
                event=EventEnvelope(
                    event_kind=EventKind.PROACTIVE_TRIGGER,
                    source_kind=SourceKind.PROACTIVE,
                    payload={"text": "timer"},
                ),
            )
        )
        self.assertEqual(core.state.compaction_user_turn_count, 2)
        self.assertEqual(len(scheduled_deadlines), 2)
        core.context.port_registry["llm:llm"] = _PurposeAwareLLM()
        asyncio.run(
            core.turn_executor.compact_memory_async(
                service,
                target_input_budget=8_192,
                reserved_output_tokens=2_048,
            )
        )
        self.assertEqual(core.state.compaction_user_turn_count, 2)
        self.assertEqual(len(scheduled_deadlines), 2)

    def test_configured_compaction_retry_count_is_not_silently_clamped(self) -> None:
        core = PalCore(
            config=RuntimeConfig(llm_compaction_retry_attempts=4)
        )
        self.assertEqual(core.agent_turn_runtime.compaction_engine.max_attempts, 4)

    def test_cache_deadline_schedule_failure_cannot_fail_committed_user_turn(self) -> None:
        core = PalCore()
        register_core_with_core(core)
        service = MemoryService()
        register_memory_with_core(core.context, service)
        core.context.port_registry["llm:llm"] = _PurposeAwareLLM()

        def fail_schedule(**_kwargs):
            raise RuntimeError("timer unavailable")

        core.cache_warm_deadline.schedule_after_turn_commit = fail_schedule
        envelope = ChannelEnvelope(
            event=EventEnvelope(
                event_kind=EventKind.USER_MESSAGE,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "committed despite timer"},
                event_id="timer-failure-turn",
            ),
            endpoint=EndpointConfig(
                endpoint_id="memory",
                channel_kind="memory",
                binding_key="memory",
            ),
            response_handle=ResponseHandle(endpoint_id="memory"),
        )

        outcome = asyncio.run(core.process_channel_turn_async(envelope))

        self.assertEqual(outcome.turn_id, "timer-failure-turn")
        self.assertTrue(
            any(
                item.get("kind") == "cache_warm_deadline.schedule_failed"
                for item in core.state.diagnostics
            )
        )

    def test_background_cache_deadline_starts_after_successful_turn_end(self) -> None:
        async def run() -> list[str]:
            core = PalCore()
            register_core_with_core(core)
            service = MemoryService()
            register_memory_with_core(core.context, service)
            core.context.port_registry["llm:llm"] = _PurposeAwareLLM()
            order: list[str] = []
            core.context.turn_event_bus.subscribe(
                TURN_END,
                lambda _topic, _event: order.append("turn_end"),
            )
            core.cache_warm_deadline.schedule_after_turn_commit = (
                lambda **_kwargs: order.append("deadline") or True
            )
            envelope = ChannelEnvelope(
                event=EventEnvelope(
                    event_kind=EventKind.USER_MESSAGE,
                    source_kind=SourceKind.CHANNEL,
                    payload={"text": "background turn"},
                    event_id="background-deadline-order",
                ),
                endpoint=EndpointConfig(
                    endpoint_id="memory",
                    channel_kind="memory",
                    binding_key="memory",
                ),
                response_handle=ResponseHandle(endpoint_id="memory"),
            )

            await core.schedule_channel_turn_async(envelope)
            task = core.state.turn_tasks[envelope.event.event_id]
            await task
            await asyncio.sleep(0)
            return order

        self.assertEqual(asyncio.run(run()), ["turn_end", "deadline"])


if __name__ == "__main__":
    unittest.main()
