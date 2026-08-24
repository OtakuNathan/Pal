from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, ToolResultIR, new_tool_call

import asyncio
import json
import unittest
from copy import deepcopy
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
from pal.foundation import EventEnvelope
from pal.execution.session_state import FileDeliveryManifest, FileDeliverySpan
from pal.llm import (
    generation_result_from_values,
    LLMPreflightAdvice,
)
from pal.llm.ir import (
    LLMMessageIR,
    MessageRole,
)
from pal.llm.runtime import LLMRuntime
from pal.memory import (
    L1MessageKind,
    L1TranscriptMessage,
    L2Entry,
    MemoryCompactRequest,
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
            "schema": "pal.compaction.pal.v2",
            "kind": "pal",
            "continuity": {
                "current_focus": "shared compaction",
                "primary_request_and_intent": "continue the active request",
                "active_operating_instructions": [],
                "active_requests": ["finish the compaction refactor"],
                "temporary_task_state": [],
                "key_decisions": [],
                "pending_questions": [],
                "recent_raw_turns": [],
                "warm_compressed_turns": [],
                "retired_or_superseded_context": [],
                "optional_next_step": "run focused tests",
            },
            "summary": {
                "summary": summary,
                "search_text": "shared compaction focused tests",
            },
            "memory_candidates": list(memory_candidates or []),
        },
        ensure_ascii=False,
    )


def _valid_bunshin_payload() -> str:
    return json.dumps(
        {
            "schema": "pal.compaction.bunshin.v3",
            "kind": "bunshin",
            "continuity": {
                "technical_route": [
                    {
                        "route": "shared engine",
                        "rationale": "one retry and commit boundary",
                    }
                ],
                "active_work": [
                    {
                        "goal": "finish compaction",
                        "target": "src/pal/core/compaction.py",
                        "action": "run tests",
                        "status": "active",
                    }
                ],
                "active_errors": [
                    {
                        "symptom": "one schema test fails",
                        "latest_evidence": "missing next_actions",
                        "current_hypothesis": "invalid fixture",
                    }
                ],
                "active_issues": [
                    {
                        "issue": "checkpoint restore",
                        "known_facts": ["the L1 checkpoint is complete"],
                        "status": "open",
                        "excluded_paths": ["do not rebuild from truncated prompt"],
                    }
                ],
                "next_actions": [
                    {
                        "action": "rerun focused tests",
                        "target": "tests/test_runtime_compaction.py",
                        "expected_result": "pass",
                    }
                ],
            },
            "summary": {
                "summary": "Bunshin is testing the shared compaction engine.",
                "search_text": "compaction.py schema restore focused tests",
            },
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
    return service


def _snapshot(
    service: MemoryService,
    *,
    protocol_messages=(),
    l1_input: str = "continue",
    clock_kind: CompactionClockKind = CompactionClockKind.USER_TURN,
) -> CompactionSnapshot:
    if l1_input:
        service.l1_store.append(
            [
                L1TranscriptMessage(
                    role="user",
                    content=l1_input,
                    kind=L1MessageKind.USER_REQUEST,
                )
            ]
        )
    if protocol_messages:
        protocol_transcript, _ = l1_tool_protocol_transcript(
            [dict(item) for item in protocol_messages]
        )
        if protocol_transcript:
            service.l1_store.append(protocol_transcript)
    return CompactionSnapshot.capture(
        service,
        target_input_budget=8_192,
        reserved_output_tokens=2_048,
        clock_kind=clock_kind,
        clock_value=7,
    )


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
        self.assertIn("pal.compaction.pal.v2", COMPACT_PAL_STRUCTURED_SYSTEM)
        self.assertIn("memory_candidates", COMPACT_PAL_STRUCTURED_SYSTEM)
        self.assertIn("active_operating_instructions", COMPACT_PAL_STRUCTURED_SYSTEM)
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

    def test_first_success_uses_standard_agenerate_and_commits_once(self) -> None:
        service = _memory_with_turns()
        llm = _ScriptedLLM(
            [generation_result_from_values(text=_valid_pal_payload())]
        )

        result = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                _snapshot(service),
                llm_runtime=llm,
                memory_service=service,
            )
        )

        self.assertEqual(result.status, "compacted")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(llm.generate_requests), 1)
        request = llm.generate_requests[0]
        self.assertEqual(request.policy.temperature, 0.0)
        self.assertEqual(request.tools, ())
        self.assertEqual(request.logical_scope_id, "pal:resident:compaction")
        self.assertEqual(
            [message.prompt_region.value for message in request.messages],
            ["stable_system", "active_dynamic"],
        )
        self.assertFalse(request.metadata["max_output_recovery_enabled"])
        self.assertEqual(
            request.metadata["compaction_clock_kind"],
            CompactionClockKind.USER_TURN,
        )
        self.assertEqual(len(service.l1_store.items), 1)
        self.assertEqual(
            service.l1_store.items[0][0].kind,
            L1MessageKind.RUNTIME_CONTEXT_SUMMARY,
        )

    def test_compaction_cache_scope_is_isolated_per_bunshin_run(self) -> None:
        service = _memory_with_turns()
        snapshot = CompactionSnapshot.capture(
            service,
            target_input_budget=8_192,
            reserved_output_tokens=2_048,
            clock_kind=CompactionClockKind.LLM_ROUND,
            clock_value=3,
            metadata={"prompt_cache_scope_id": "bunshin:run-7"},
        )
        engine = CompactionEngine(BunshinCompactionPolicy())

        request = engine._request(snapshot, "compact this run", attempt=0)

        self.assertEqual(request.logical_scope_id, "bunshin:run-7:compaction")
        self.assertEqual(
            [message.prompt_region.value for message in request.messages],
            ["stable_system", "active_dynamic"],
        )

    def test_endpoint_retry_usage_does_not_inflate_engine_attempts(self) -> None:
        service = _memory_with_turns()
        llm = _ScriptedLLM(
            [
                generation_result_from_values(
                    text=_valid_pal_payload(),
                    provider_response_count=3,
                )
            ]
        )

        result = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                _snapshot(service),
                llm_runtime=llm,
                memory_service=service,
            )
        )

        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(llm.generate_requests), 1)

    def test_three_schema_attempts_are_independent_and_can_recover(self) -> None:
        service = _memory_with_turns()
        llm = _ScriptedLLM(
            [
                generation_result_from_values(text="not json"),
                generation_result_from_values(
                    text=json.dumps(
                        {
                            "schema": "wrong",
                            "summary": {"summary": "bad"},
                        }
                    )
                ),
                generation_result_from_values(text=_valid_pal_payload("third works")),
            ]
        )

        result = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                _snapshot(service),
                llm_runtime=llm,
                memory_service=service,
            )
        )

        self.assertEqual(result.status, "compacted")
        self.assertEqual(result.attempts, 3)
        self.assertIn("third works", result.memory_result.summary)
        self.assertIn(
            "Previous Output Validation Error",
            llm.generate_requests[1].messages[-1].text,
        )

    def test_preflight_shrinks_without_spending_model_attempts(self) -> None:
        service = _memory_with_turns(6)
        compact_sources: list[int] = []

        def preflight(request):
            source = request.request.messages[-1].text
            unit_count = source.count("### memory:")
            if unit_count > 2:
                compact_sources.append(len(source))
                return LLMPreflightAdvice(
                    status=LLMPreflightStatus.COMPACT_REQUIRED
                )
            return LLMPreflightAdvice(status=LLMPreflightStatus.READY)

        llm = _ScriptedLLM(
            [generation_result_from_values(text=_valid_pal_payload())],
            preflight=preflight,
        )

        result = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                _snapshot(service),
                llm_runtime=llm,
                memory_service=service,
            )
        )

        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(llm.generate_requests), 1)
        self.assertGreaterEqual(len(compact_sources), 1)
        self.assertTrue(
            all(
                left > right
                for left, right in zip(
                    compact_sources,
                    compact_sources[1:],
                )
            )
        )

    def test_endpoint_compact_required_retries_with_strictly_smaller_source(self) -> None:
        service = _memory_with_turns(5)
        llm = _ScriptedLLM(
            [
                generation_result_from_values(
                    finish_reason=LLMFinishReason.COMPACT_REQUIRED,
                    target_input_budget=512,
                    reserved_output_tokens=64,
                    preferred_endpoint_id="small-endpoint",
                    preferred_model_id="small-model",
                ),
                generation_result_from_values(text=_valid_pal_payload()),
            ]
        )

        result = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                _snapshot(service),
                llm_runtime=llm,
                memory_service=service,
            )
        )

        self.assertEqual(result.status, "compacted")
        self.assertEqual(result.attempts, 2)
        self.assertLess(result.source_sizes[1], result.source_sizes[0])
        retry = llm.generate_requests[1]
        self.assertEqual(
            retry.metadata["preferred_endpoint_id"],
            "small-endpoint",
        )
        self.assertEqual(retry.model_hint, "small-model")
        self.assertEqual(retry.policy.max_output_tokens, 64_000)
        self.assertEqual(
            retry.messages[0].text,
            llm.generate_requests[0].messages[0].text,
        )
        self.assertNotIn("256 tokens", retry.messages[0].text)

    def test_compactor_uses_provider_output_ceiling_for_reasoning_headroom(self) -> None:
        service = _memory_with_turns(2)
        llm = _ScriptedLLM(
            [generation_result_from_values(text=_valid_pal_payload())]
        )
        llm.resolve_endpoint_facts = lambda **_kwargs: {
            "max_output_tokens": 32_000,
            "max_output_tokens_upper_limit": 128_000,
        }

        result = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                _snapshot(service),
                llm_runtime=llm,
                memory_service=service,
            )
        )

        self.assertEqual(result.status, "compacted")
        self.assertEqual(llm.generate_requests[0].policy.max_output_tokens, 128_000)
        self.assertNotIn("4,096 tokens", llm.generate_requests[0].messages[0].text)

    def test_output_truncation_disables_continuation_and_shrinks_source(self) -> None:
        service = _memory_with_turns(6)
        llm = _ScriptedLLM(
            [
                generation_result_from_values(
                    text='{"schema":',
                    finish_reason="max_tokens",
                ),
                generation_result_from_values(text=_valid_pal_payload()),
            ]
        )

        result = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                _snapshot(service),
                llm_runtime=llm,
                memory_service=service,
            )
        )

        self.assertEqual(result.status, "compacted")
        self.assertLess(result.source_sizes[1], result.source_sizes[0])
        self.assertTrue(
            all(
                request.metadata["max_output_recovery_enabled"] is False
                for request in llm.generate_requests
            )
        )

    def test_checkpoint_over_half_the_input_budget_is_retried(self) -> None:
        oversized = json.loads(_valid_pal_payload())
        oversized["summary"]["summary"] = "界" * 4_097
        llm = _ScriptedLLM(
            [
                generation_result_from_values(
                    text=json.dumps(oversized, ensure_ascii=False)
                ),
                generation_result_from_values(text=_valid_pal_payload("bounded")),
            ]
        )
        service = _memory_with_turns(2)

        result = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                _snapshot(service),
                llm_runtime=llm,
                memory_service=service,
            )
        )

        self.assertEqual(result.status, "compacted")
        self.assertEqual(result.attempts, 2)
        self.assertIn(
            "4,096-token visible output limit",
            llm.generate_requests[1].messages[-1].text,
        )

    def test_checkpoint_limit_retains_the_absolute_twenty_thousand_cap(self) -> None:
        service = _memory_with_turns(1)
        original = _snapshot(service)
        snapshot = CompactionSnapshot(
            target_input_budget=100_000,
            reserved_output_tokens=original.reserved_output_tokens,
            clock_kind=original.clock_kind,
            clock_value=original.clock_value,
            memory_items=original.memory_items,
            metadata=original.metadata,
        )
        oversized = json.loads(_valid_pal_payload())
        oversized["summary"]["summary"] = "界" * 20_001
        llm = _ScriptedLLM(
            [
                generation_result_from_values(
                    text=json.dumps(oversized, ensure_ascii=False)
                ),
                generation_result_from_values(text=_valid_pal_payload("bounded")),
            ]
        )

        result = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                snapshot,
                llm_runtime=llm,
                memory_service=service,
            )
        )

        self.assertEqual(result.status, "compacted")
        self.assertEqual(result.attempts, 2)
        self.assertNotIn("20,000 tokens", llm.generate_requests[0].messages[0].text)
        self.assertIn("20,000-token visible output limit", llm.generate_requests[1].messages[-1].text)

    def test_three_failures_leave_memory_unchanged(self) -> None:
        service = _memory_with_turns()
        snapshot = _snapshot(service)
        before_l1 = deepcopy(service.l1_store.items)
        before_l2 = deepcopy(service.l2_store.items)
        llm = _ScriptedLLM(
            [
                RuntimeError("transient"),
                generation_result_from_values(text="bad json"),
                generation_result_from_values(
                    text="{}",
                    finish_reason=LLMFinishReason.STOP,
                ),
            ]
        )

        result = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                snapshot,
                llm_runtime=llm,
                memory_service=service,
            )
        )

        self.assertEqual(result.status, "failed")
        self.assertFalse(result.success)
        self.assertEqual(result.attempts, 3)
        self.assertIsNone(result.summary_entry)
        self.assertIsNone(result.memory_result)
        self.assertEqual(service.l1_store.items, before_l1)
        self.assertEqual(service.l2_store.items, before_l2)

    def test_failure_preserves_previous_checkpoint_and_new_l1(self) -> None:
        service = _memory_with_turns(2)
        initial_payload = json.loads(_valid_pal_payload("stable seed"))
        initial_payload["continuity"]["active_requests"] = [
            "preserve this active request"
        ]
        initial = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                _snapshot(service),
                llm_runtime=_ScriptedLLM(
                    [
                        generation_result_from_values(
                            text=json.dumps(initial_payload),
                        )
                    ]
                ),
                memory_service=service,
            )
        )
        self.assertEqual(initial.status, "compacted")
        service.l1_store.append(
            [
                L1TranscriptMessage(
                    role="user",
                    content="new work after the stable seed",
                    kind=L1MessageKind.USER_REQUEST,
                )
            ]
        )

        snapshot = _snapshot(service, l1_input="")
        before_l1 = deepcopy(service.l1_store.items)
        before_l2 = deepcopy(service.l2_store.items)
        failed = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                snapshot,
                llm_runtime=_ScriptedLLM(
                    [
                        RuntimeError("one"),
                        RuntimeError("two"),
                        RuntimeError("three"),
                    ]
                ),
                memory_service=service,
            )
        )

        self.assertEqual(failed.status, "failed")
        self.assertEqual(service.l1_store.items, before_l1)
        self.assertEqual(service.l2_store.items, before_l2)

    def test_timeout_consumes_attempts_then_leaves_memory_unchanged(self) -> None:
        service = _memory_with_turns()
        snapshot = _snapshot(service)
        before_l1 = deepcopy(service.l1_store.items)
        before_l2 = deepcopy(service.l2_store.items)
        llm = _SlowLLM([])

        result = asyncio.run(
            CompactionEngine(
                PalCompactionPolicy(),
                timeout_seconds=0.01,
            ).run(
                snapshot,
                llm_runtime=llm,
                memory_service=service,
            )
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.attempts, 3)
        self.assertEqual(len(llm.generate_requests), 3)
        self.assertEqual(service.l1_store.items, before_l1)
        self.assertEqual(service.l2_store.items, before_l2)

    def test_base_context_over_budget_fails_without_committing(self) -> None:
        service = _memory_with_turns(1)
        snapshot = _snapshot(service)
        before_l1 = deepcopy(service.l1_store.items)
        before_l2 = deepcopy(service.l2_store.items)
        llm = _ScriptedLLM(
            [],
            preflight=lambda _request: LLMPreflightAdvice(
                status=LLMPreflightStatus.COMPACT_REQUIRED,
                target_input_budget=128,
            ),
        )

        result = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                snapshot,
                llm_runtime=llm,
                memory_service=service,
            )
        )

        self.assertEqual(result.status, "failed")
        self.assertEqual(result.attempts, 0)
        self.assertIn("input:base_context_over_budget", result.failures)
        self.assertEqual(llm.generate_requests, [])
        self.assertEqual(service.l1_store.items, before_l1)
        self.assertEqual(service.l2_store.items, before_l2)

    def test_closed_failed_mutation_can_be_dropped_to_fit_compaction(self) -> None:
        service = _memory_with_turns(1)
        def preflight(request):
            source = request.request.messages[-1].text
            return LLMPreflightAdvice(
                status=(
                    LLMPreflightStatus.COMPACT_REQUIRED
                    if len(source) > 1_000
                    else LLMPreflightStatus.READY
                )
            )

        llm = _ScriptedLLM(
            [generation_result_from_values(text=_valid_pal_payload())],
            preflight=preflight,
        )
        snapshot = _snapshot(
            service,
            l1_input="",
            protocol_messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "unknown-write",
                            "type": "function",
                            "function": {
                                "name": "edit_file",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "unknown-write",
                    "content": "x" * 4_000,
                    "_pal_result_state": {
                        "ok": False,
                        "kind": "failed",
                        "effect": "unknown",
                    },
                },
            ],
        )
        result = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                snapshot,
                llm_runtime=llm,
                memory_service=service,
            )
        )

        self.assertEqual(result.status, "compacted")
        self.assertEqual(result.attempts, 1)
        self.assertEqual(len(llm.generate_requests), 1)
        self.assertEqual(len(service.l1_store.items), 1)
        self.assertEqual(
            service.l1_store.items[0][0].kind,
            L1MessageKind.RUNTIME_CONTEXT_SUMMARY,
        )

    def test_atomic_l1_unit_includes_unknown_effect_and_ignores_active_batch(self) -> None:
        service = _memory_with_turns(1)
        unknown_protocol = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "edit-1",
                        "type": "function",
                        "function": {
                            "name": "edit_file",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "edit-1",
                "content": "write outcome unknown",
                "_pal_result_state": {
                    "ok": False,
                    "kind": "failed",
                    "effect": "unknown",
                },
            },
        ]

        units = build_compaction_units(
            _snapshot(service, protocol_messages=unknown_protocol)
        )
        tool_unit = units[-1]

        self.assertEqual(tool_unit.source, "memory")
        self.assertIn("write outcome unknown", tool_unit.text)

        incomplete_service = _memory_with_turns(1)
        incomplete_protocol, _ = l1_tool_protocol_transcript(
            [
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "read-2",
                            "type": "function",
                            "function": {
                                "name": "read_file",
                                "arguments": "{}",
                            },
                        }
                    ],
                }
            ]
        )
        # Simulate corrupted restored state by bypassing the normal L1 commit
        # boundary, which rejects this transcript.
        incomplete_service.l1_store.turns.turns.append(
            L1TurnIR(
                turn_id="corrupted-active-tool-call",
                messages=(
                    LLMMessageIR(
                        role=MessageRole.ASSISTANT,
                        parts=(
                            new_tool_call(
                                call_id="read-2",
                                name="read_file",
                                arguments={},
                            ),
                        ),
                        semantic_kind=L1MessageKind.ASSISTANT_TOOL_CALL,
                    ),
                ),
            )
        )
        incomplete_snapshot = CompactionSnapshot.capture(
            incomplete_service,
            target_input_budget=8_192,
            reserved_output_tokens=2_048,
            clock_kind=CompactionClockKind.USER_TURN,
            clock_value=7,
        )
        self.assertFalse(
            any(
                message.tool_call_id == "read-2"
                for transcript in incomplete_snapshot.memory_items
                for message in transcript
            )
        )
        result = asyncio.run(
            CompactionEngine(PalCompactionPolicy()).run(
                incomplete_snapshot,
                llm_runtime=_ScriptedLLM(
                    [generation_result_from_values(text=_valid_pal_payload())]
                ),
                memory_service=incomplete_service,
            )
        )
        self.assertTrue(result.success)
        self.assertEqual(
            incomplete_service.active_l1_turn("corrupted-active-tool-call").pending_call_ids,
            frozenset({"read-2"}),
        )

    def test_closed_tool_body_is_bounded_but_visible_to_compaction(self) -> None:
        service = _memory_with_turns(1)
        body = "HEAD-" + ("x" * 20_000) + "-TAIL"
        protocol = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "read-large",
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
                "tool_call_id": "read-large",
                "content": body,
                "_pal_result_state": {
                    "ok": True,
                    "kind": "complete",
                    "effect": "none",
                },
            },
        ]

        unit = build_compaction_units(
            _snapshot(service, protocol_messages=protocol)
        )[-1]

        self.assertNotIn("full result retired", unit.text)
        self.assertIn("HEAD-", unit.text)
        self.assertIn("-TAIL", unit.text)
        self.assertIn("head/tail projection only", unit.text)
        self.assertLess(len(unit.text), len(body))

    def test_compaction_snapshot_has_no_provider_protocol_projection(self) -> None:
        service = _memory_with_turns(1)
        snapshot = _snapshot(service)
        self.assertFalse(hasattr(snapshot, "protocol_messages"))
        self.assertTrue(all(unit.source == "memory" for unit in build_compaction_units(snapshot)))

    def test_memory_commit_rolls_back_l1_and_l2(self) -> None:
        service = _memory_with_turns(1)
        service.l2_store.items["memory_summary_current"] = L2Entry(
            entry_id="memory_summary_current",
            kind="summary",
            scope="system",
            title="old",
            summary="old",
        )
        before_l2 = dict(service.l2_store.items)
        entry = PalCompactionPolicy().validate_checkpoint(
            _valid_pal_payload(),
            _snapshot(service),
        )
        before_l1 = deepcopy(service.l1_store.items)
        original_remove = service.remove_projected_entries

        def fail_remove(_entry_ids):
            raise RuntimeError("storage failure")

        service.remove_projected_entries = fail_remove
        self.addCleanup(
            setattr,
            service,
            "remove_projected_entries",
            original_remove,
        )

        with self.assertRaises(RuntimeError):
            service.compact(
                MemoryCompactRequest(
                    target_input_budget=512,
                    reserved_output_tokens=128,
                    summary_entry=entry,
                )
            )

        self.assertEqual(service.l1_store.items, before_l1)
        self.assertEqual(service.l2_store.items, before_l2)

    def test_memory_service_rejects_unvalidated_request_without_mutation(self) -> None:
        service = _memory_with_turns(1)
        before = deepcopy(service.l1_store.items)

        with self.assertRaises(ValueError):
            service.compact(
                MemoryCompactRequest(
                    target_input_budget=512,
                    reserved_output_tokens=128,
                    metadata={"semantic_summary": "legacy path"},
                )
            )

        self.assertEqual(service.l1_store.items, before)

    def test_memory_compact_strips_transient_provider_payload_from_summary(self) -> None:
        service = _memory_with_turns(1)
        summary_entry = L2Entry(
            entry_id="memory_summary_current",
            kind="summary",
            scope="system",
            title="summary",
            summary="provider-neutral compact summary",
            rendered="<compact_context>provider-neutral compact summary</compact_context>",
            payload={
                "schema": "pal.compaction.test.v1",
                "provider_specific_fields": {
                    "reasoning_content": "must not enter durable L1"
                },
                "reasoning_content": "must not enter durable L1",
            },
        )

        service.compact(
            MemoryCompactRequest(
                target_input_budget=512,
                reserved_output_tokens=128,
                summary_entry=summary_entry,
            )
        )

        compacted = service.l1_store.items[0][0]
        self.assertEqual(
            compacted.payload,
            {"schema": "pal.compaction.test.v1"},
        )


class CompactionPolicyTests(unittest.TestCase):
    def test_pal_v2_keeps_candidates_for_approval(self) -> None:
        service = _memory_with_turns(1)
        candidate = {
            "kind": "fact",
            "title": "Compaction approval",
            "summary": "Candidates require approval.",
            "source_excerpt": "requires approval",
        }

        entry = PalCompactionPolicy().validate_checkpoint(
            _valid_pal_payload(memory_candidates=[candidate]),
            _snapshot(service),
        )

        self.assertEqual(entry.payload["memory_candidates"], [candidate])
        self.assertIn(
            '<compact_context kind="pal" authority="conversation_continuity">',
            entry.rendered,
        )

    def test_bunshin_v3_preserves_work_cursor_without_role_task_or_candidates(self) -> None:
        service = _memory_with_turns(1)
        snapshot = _snapshot(
            service,
            l1_input="Implement the entire secret role assignment.",
            clock_kind=CompactionClockKind.LLM_ROUND,
        )

        entry = BunshinCompactionPolicy().validate_checkpoint(
            _valid_bunshin_payload(),
            snapshot,
        )

        self.assertEqual(
            entry.payload["schema"],
            "pal.compaction.bunshin.v3",
        )
        self.assertIn(
            "src/pal/core/compaction.py",
            json.dumps(entry.payload),
        )
        self.assertIn("missing next_actions", json.dumps(entry.payload))
        self.assertIn("L1 checkpoint", json.dumps(entry.payload))
        self.assertNotIn("memory_candidates", entry.payload)
        self.assertNotIn("secret role assignment", entry.rendered)
        self.assertIn(
            '<compact_context kind="bunshin" authority="work_checkpoint">',
            entry.rendered,
        )

    def test_bunshin_v3_rejects_memory_candidates_and_chain_of_thought(self) -> None:
        service = _memory_with_turns(1)
        payload = json.loads(_valid_bunshin_payload())
        payload["memory_candidates"] = []
        with self.assertRaisesRegex(ValueError, "memory_candidates"):
            BunshinCompactionPolicy().validate_checkpoint(
                json.dumps(payload),
                _snapshot(service),
            )
        payload.pop("memory_candidates")
        payload["continuity"]["chain_of_thought"] = "private"
        with self.assertRaisesRegex(ValueError, "reasoning"):
            BunshinCompactionPolicy().validate_checkpoint(
                json.dumps(payload),
                _snapshot(service),
            )

    def test_pal_v2_rejects_an_empty_shell_checkpoint(self) -> None:
        service = _memory_with_turns(1)
        with self.assertRaisesRegex(ValueError, "continuity"):
            PalCompactionPolicy().validate_checkpoint(
                json.dumps(
                    {
                        "schema": "pal.compaction.pal.v2",
                        "kind": "pal",
                        "summary": {
                            "summary": "looks complete but loses continuity",
                            "search_text": "empty shell",
                        },
                        "memory_candidates": [],
                    }
                ),
                _snapshot(service),
            )

    def test_bunshin_v3_rejects_scalar_continuity_fields(self) -> None:
        service = _memory_with_turns(1)
        payload = json.loads(_valid_bunshin_payload())
        payload["continuity"]["active_work"] = "still editing compaction.py"
        with self.assertRaisesRegex(ValueError, "active_work.*array"):
            BunshinCompactionPolicy().validate_checkpoint(
                json.dumps(payload),
                _snapshot(service),
            )


class RuntimeCompactionIntegrationTests(unittest.TestCase):
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
        original_compact = service.compact

        def fail_compact(_request):
            raise RuntimeError("commit failed")

        service.compact = fail_compact
        self.addCleanup(setattr, service, "compact", original_compact)
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

    def test_transactional_compaction_rolls_back_l1_when_dependent_commit_fails(self) -> None:
        service = _memory_with_turns(2)
        entry = PalCompactionPolicy().validate_checkpoint(
            _valid_pal_payload(),
            _snapshot(service, l1_input=""),
        )
        before_l1 = deepcopy(service.l1_store.items)

        def fail_dependent_commit():
            raise RuntimeError("logical state unavailable")

        with self.assertRaisesRegex(RuntimeError, "logical state unavailable"):
            service.compact_transactionally(
                MemoryCompactRequest(
                    target_input_budget=8_192,
                    reserved_output_tokens=2_048,
                    summary_entry=entry,
                ),
                after_commit=fail_dependent_commit,
            )
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
        self.assertEqual(replies[-1].split(".")[0], "Context compacted")
        self.assertEqual(statuses[-1][0], "interactive_open")
        spec = statuses[-1][1]["spec"]
        self.assertIn("Compact candidates need approval", spec.text)

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
        core.context.port_registry["llm:llm"] = _PurposeAwareLLM()
        asyncio.run(
            core.turn_executor.compact_memory_async(
                service,
                target_input_budget=8_192,
                reserved_output_tokens=2_048,
            )
        )
        self.assertEqual(core.state.compaction_user_turn_count, 2)

    def test_configured_compaction_retry_count_is_not_silently_clamped(self) -> None:
        core = PalCore(
            config=RuntimeConfig(llm_compaction_retry_attempts=4)
        )
        self.assertEqual(core.agent_turn_runtime.compaction_engine.max_attempts, 4)


if __name__ == "__main__":
    unittest.main()
