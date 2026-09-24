from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, ToolResultIR, new_tool_call

import asyncio
import shutil
import tempfile
import unittest
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from pal.core import CompactionClockKind, CompactionSnapshot, MainContext
from pal.core.module_registry import ModuleHandle, ModuleRegistry
from pal.core.runtime_state import RuntimeSnapshotCoordinator, RuntimeSnapshotIdentity
from pal.core.turn_executor import TurnExecutor
from pal.execution.runtime import ExecutionRuntime
from pal.llm import (
    GenerationPolicyIR,
    LLMRequestIR,
    generation_result_from_values,
)
from pal.llm.ir import LLMMessageIR, MessageRole, ReasoningPartIR, TextPartIR
from pal.memory import (
    L3ProviderSelector,
    L1MessageKind,
    L1TranscriptMessage,
    MemoryCommitRequest,
    MemoryPackRequest,
    MemoryQuery,
    MemoryService,
)
from pal.memory.prompt import MemoryPromptFragmentProvider
from pal.bunshin.prompt_adapter import BunshinPromptFragmentProvider
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.bunshin.compact import (
    BunshinCompactionPolicy,
)
from pal.bunshin.checkpoint import (
    open_agent_session_checkpoint,
    seal_agent_session_checkpoint,
)
from pal.bunshin.runner import (
    BunshinAgentLoopState,
    BunshinLLMRetryableError,
    BunshinRunner,
    _bunshin_llm_request_metadata,
    _bunshin_prompt_context,
)
from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
from pal.plugins.l3 import MockL3Plugin
from pal.shared import (
    LLMFinishReason,
    BunshinInvocationPack,
    PromptAssemblyContext,
    RuntimeStatus,
)
from pal.core.turns import EffectResult


async def _noop_write_event(_event):
    return None


async def _noop_read_decision(_timeout):
    return None


class BunshinMemoryIntegrationTests(unittest.TestCase):
    def _runner(self) -> BunshinRunner:
        runtime_root = Path(tempfile.mkdtemp(prefix="pal_bunshin_memory_"))
        self.addCleanup(shutil.rmtree, runtime_root, True)
        return BunshinRunner(
            runtime_root=runtime_root,
            pack=BunshinInvocationPack(
                invocation_id="memory-invocation",
                goal="Use prior project knowledge.",
            ),
            bunshin_id="memory-bunshin",
            run_id="memory-run",
            write_event=_noop_write_event,
            read_decision=_noop_read_decision,
        )

    @staticmethod
    def _memory_service(*records: dict[str, object]) -> tuple[MemoryService, MockL3Plugin]:
        provider = MockL3Plugin(provider_id="shared_l3", records=[dict(item) for item in records])
        service = MemoryService(
            l3_selector=L3ProviderSelector(
                resolver=lambda _provider_id: provider,
                active_provider_id=provider.provider_id,
            )
        )
        provider.service = service
        return service, provider

    def test_bunshin_registers_the_main_memory_prompt_provider(self) -> None:
        providers = self._runner()._build_bunshin_prompt_fragment_registry().list_for_prompt()

        memory_providers = [provider for provider in providers if isinstance(provider, MemoryPromptFragmentProvider)]
        self.assertEqual(len(memory_providers), 1)
        self.assertTrue(memory_providers[0].include_l1_recent_context)

    def test_bunshin_preserves_shared_tool_guidance_authority(self) -> None:
        providers = self._runner()._build_bunshin_prompt_fragment_registry().list_for_prompt()
        prompt_provider = next(
            provider for provider in providers if isinstance(provider, BunshinPromptFragmentProvider)
        )

        fragments = prompt_provider.build_prompt_fragments(
            PromptAssemblyContext(core_mode="bunshin", turn_kind="bunshin")
        )
        policy = next(fragment for fragment in fragments if fragment.section == "tool_policy")
        routing = next(fragment for fragment in fragments if fragment.section == "tool_routing")

        self.assertEqual(policy.title, "Tool Policy")
        self.assertEqual(policy.metadata["prompt_target"], "system")
        self.assertIn("result-specific recovery affordances", policy.content)
        self.assertIn("never blindly retry a mutation", policy.content)
        self.assertIn("each tool call as one RPC", policy.content)
        self.assertIn("point-in-time observations", policy.content)
        self.assertIn("Replaying a stored result does not refresh", policy.content)
        self.assertEqual(routing.title, "Tool Routing")
        self.assertEqual(routing.metadata["prompt_target"], "developer")
        self.assertIn("returned affordances as its continuation contract", routing.content)
        self.assertIn("suggested next tool only when", routing.content)

    def test_bunshin_puts_shared_tool_efficiency_before_every_role_contract(self) -> None:
        providers = self._runner()._build_bunshin_prompt_fragment_registry().list_for_prompt()
        prompt_provider = next(
            provider for provider in providers if isinstance(provider, BunshinPromptFragmentProvider)
        )

        fragments = prompt_provider.build_prompt_fragments(
            PromptAssemblyContext(core_mode="bunshin", turn_kind="bunshin")
        )
        efficiency = next(fragment for fragment in fragments if fragment.section == "tool_efficiency")

        self.assertEqual(efficiency.title, "Tool Efficiency")
        self.assertLess(efficiency.priority, 20)
        self.assertIn("do not require separate model rounds for independent reads", efficiency.content)
        self.assertIn("Batch independent tool calls in one response", efficiency.content)
        self.assertIn("do not serialize every file or field", efficiency.content)
        self.assertIn("If read_file reports unchanged content", efficiency.content)
        self.assertIn("Avoid dumping large files", efficiency.content)

    def test_bunshin_role_timeout_is_forwarded_to_the_host_llm_request(self) -> None:
        pack = BunshinInvocationPack(
            invocation_id="timeout-invocation",
            goal="Preserve the logical role timeout.",
            metadata={"llm_round_timeout_seconds": 3000},
        )

        metadata = _bunshin_llm_request_metadata(pack, "timeout-run")

        self.assertEqual(metadata["timeout_seconds"], 3000.0)
        self.assertFalse(metadata["max_output_recovery_enabled"])

    @staticmethod
    def _turn_executor(context: MainContext, *, canonical_prompt) -> TurnExecutor:
        return TurnExecutor(
            context,
            SimpleNamespace(diagnostics=[]),
            SimpleNamespace(),
            call_port_async=lambda *args, **kwargs: None,
            build_canonical_prompt=canonical_prompt,
            debug_log_prompt=lambda *args, **kwargs: None,
            debug_log_outcome=lambda *args, **kwargs: None,
            debug_log_reply=lambda *args, **kwargs: None,
            build_llm_tool_contracts=lambda: [],
            handle_failure_async=lambda *args, **kwargs: None,
            render_failure_feedback_text=lambda value: str(value),
            should_enter_failure_flow_for_tool_result=lambda value: False,
        )

    def test_bunshin_prompt_uses_run_scoped_cache_and_artifact_identity(self) -> None:
        service, _provider = self._memory_service()
        turn_id = "memory-run:invocation:input"
        service.commit_l1(
            MemoryCommitRequest(
                turn_id="memory-run:invocation:prior",
                transcript=[
                    L1TranscriptMessage(role="user", content="prior assignment step"),
                    L1TranscriptMessage(role="assistant", content="prior step completed"),
                ],
            )
        )
        service.begin_l1_turn(turn_id, user_text="inspect the assigned module")
        context = MainContext(
            execution_runtime=ExecutionRuntime(),
            port_registry={"memory:memory": service},
        )
        executor = self._turn_executor(
            context,
            canonical_prompt=lambda *_args, **_kwargs: LLMRequestIR(
                messages=(
                    LLMMessageIR(
                        role=MessageRole.SYSTEM,
                        parts=(TextPartIR("stable bunshin contract"),),
                    ),
                    LLMMessageIR(
                        role=MessageRole.USER,
                        parts=(TextPartIR("dynamic role working state"),),
                    ),
                ),
                tools=(),
                policy=GenerationPolicyIR(max_output_tokens=256),
                metadata={"compiler_only": True},
            ),
        )
        assembly = _bunshin_prompt_context(
            self._runner().pack,
            run_id="memory-run",
            metadata={},
        )
        continuation = SimpleNamespace(
            turn_id=turn_id,
            preferred_llm_endpoint_id=None,
            preferred_llm_model_id=None,
            turn_settings_snapshot={},
            tool_observations=[],
            finalization_only=False,
        )

        request = executor.build_turn_prompt(
            continuation,
            assembly,
            max_output_tokens=256,
        )

        self.assertEqual(request.logical_scope_id, "bunshin:memory-run")
        self.assertEqual(request.metadata["prompt_cache_scope_id"], "bunshin:memory-run")
        self.assertEqual(request.metadata["artifact_scope_key"], "bunshin:memory-run")
        self.assertEqual(
            [message.prompt_region.value for message in request.messages],
            [
                "stable_system",
                "settled_history",
                "settled_history",
                "active_input",
                "active_history",
            ],
        )
        self.assertEqual(
            [message.text for message in request.messages],
            [
                "stable bunshin contract",
                "prior assignment step",
                "prior step completed",
                "inspect the assigned module",
                request.messages[-1].text,
            ],
        )
        self.assertIn("dynamic role working state", request.messages[-1].text)
        self.assertIn('<pal_context kind="state"', request.messages[-1].text)

    def test_bunshin_prompt_reconciles_settled_and_active_tool_results_together(
        self,
    ) -> None:
        service, _provider = self._memory_service()
        prior_turn_id = "memory-run:invocation:prior"
        current_turn_id = "memory-run:invocation:current"
        call = new_tool_call(
            name="read_file",
            args={"file_path": "README.md"},
            call_id="prior-read",
        )
        service.begin_l1_turn(prior_turn_id, user_text="read the file")
        service.upsert_l1_assistant(
            prior_turn_id,
            LLMMessageIR(role=MessageRole.ASSISTANT, parts=(call,)),
        )
        service.append_l1_tool_result(
            prior_turn_id,
            ToolResultIR(
                call_id=call.call_id,
                name=call.name,
                content="settled file evidence",
            ),
        )
        service.settle_l1_turn(prior_turn_id)
        service.begin_l1_turn(current_turn_id, user_text="continue")
        context = MainContext(
            execution_runtime=ExecutionRuntime(),
            port_registry={"memory:memory": service},
        )
        executor = self._turn_executor(
            context,
            canonical_prompt=lambda *_args, **_kwargs: LLMRequestIR(
                messages=(
                    LLMMessageIR(
                        role=MessageRole.SYSTEM,
                        parts=(TextPartIR("stable bunshin contract"),),
                    ),
                ),
                tools=(),
                policy=GenerationPolicyIR(max_output_tokens=256),
            ),
        )
        continuation = SimpleNamespace(
            turn_id=current_turn_id,
            preferred_llm_endpoint_id=None,
            preferred_llm_model_id=None,
            turn_settings_snapshot={},
            tool_observations=[],
            finalization_only=False,
        )

        prompt = executor.build_turn_prompt(
            continuation,
            _bunshin_prompt_context(
                self._runner().pack,
                run_id="memory-run",
                metadata={},
            ),
            max_output_tokens=256,
        )

        projected_results = [
            part.call_id
            for message in prompt.messages
            for part in message.parts
            if isinstance(part, ToolResultIR)
        ]
        self.assertEqual(projected_results, ["prior-read"])

    def test_bunshin_settlement_preserves_result_owned_authority(self) -> None:
        base_runtime = ExecutionRuntime()
        retired: list[dict[str, object]] = []

        def record_retirement(**kwargs):
            retired.append(dict(kwargs))
            return tuple(kwargs.get("result_ids") or ())

        base_runtime.retire_tool_results = record_retirement
        scoped_runtime = BunshinScopedExecutionRuntime(
            base_runtime,
            [],
            workspace={"run_id": "memory-run"},
        )
        service, _provider = self._memory_service()
        turn_id = "memory-run:invocation:input"
        call = new_tool_call(name="read_file", args={"file_path": "README.md"}, call_id="read-1")
        service.begin_l1_turn(turn_id, user_text="read the file")
        service.upsert_l1_assistant(
            turn_id,
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(call,),
            ),
        )
        service.append_l1_tool_result(
            turn_id,
            ToolResultIR(
                call_id=call.call_id,
                name=call.name,
                content="file contents",
            ),
        )
        context = MainContext(
            execution_runtime=scoped_runtime,
            port_registry={"memory:memory": service},
        )
        executor = self._turn_executor(
            context,
            canonical_prompt=lambda *_args, **_kwargs: None,
        )

        asyncio.run(
            executor.schedule_post_turn_commit_async(
                SimpleNamespace(commit_payload=SimpleNamespace(turn_id=turn_id))
            )
        )

        self.assertEqual(retired, [])
        settled = service.l1_store.turns.get(turn_id)
        self.assertEqual(str(settled.state), "settled")
        results = [
            part
            for message in settled.messages
            for part in message.parts
            if isinstance(part, ToolResultIR)
        ]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].content, "file contents")

    def test_bunshin_recovery_aborts_stale_turn_but_preserves_its_result(self) -> None:
        service, _provider = self._memory_service()
        active_turn_id = "memory-run:invocation:new"
        stale_turn_id = "memory-run:invocation:stale"
        service.begin_l1_turn(active_turn_id, user_text="new input")
        service.begin_l1_turn(stale_turn_id, user_text="stale input")
        call = new_tool_call(name="read_file", args={"file_path": "README.md"}, call_id="stale-read")
        service.upsert_l1_assistant(
            stale_turn_id,
            LLMMessageIR(role=MessageRole.ASSISTANT, parts=(call,)),
        )
        service.append_l1_tool_result(
            stale_turn_id,
            ToolResultIR(
                call_id=call.call_id,
                name=call.name,
                content="stale contents",
            ),
        )

        BunshinRunner._abort_stale_l1_turns(
            service,
            active_turn_id=active_turn_id,
        )

        self.assertEqual(str(service.l1_store.turns.get(stale_turn_id).state), "aborted")
        stale_result = next(
            part
            for message in service.l1_store.turns.get(stale_turn_id).messages
            for part in message.parts
            if isinstance(part, ToolResultIR)
        )
        self.assertEqual(stale_result.content, "stale contents")
        self.assertIsNotNone(service.active_l1_turn(active_turn_id))

    def test_bunshin_snapshots_survive_tool_clock_advancement(self):
        base = ExecutionRuntime()
        scoped = BunshinScopedExecutionRuntime(base, [], workspace={"run_id": "memory-run"})
        turn_id = "memory-run:input"
        scoped.begin_tool_result_turn(turn_id=turn_id, scope_key="bunshin:memory-run", input_id="input")
        ref = base.result_snapshots.capture("full result", call_id="c", lifetime="bunshin:memory-run")
        for i in range(20):
            scoped.advance_tool_result_clock(turn_id=turn_id, clock_id=f"tool:{i}", retention_steps=5)
        self.assertEqual(Path(ref.path).read_text(), "full result")
        base.shutdown()

    def test_bunshin_renders_closed_tool_protocol_with_result_body(self) -> None:
        service, _provider = self._memory_service()
        service.commit_l1(
            MemoryCommitRequest(
                turn_id="memory-run",
                transcript=[
                    L1TranscriptMessage(
                        role="assistant",
                        content="calling read_file",
                        kind=L1MessageKind.ASSISTANT_TOOL_CALL,
                        tool_calls=[
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "read_file",
                                    "arguments": '{"file_path":"README.md"}',
                                },
                            }
                        ],
                    ),
                    L1TranscriptMessage(
                        role="tool",
                        content="README contents",
                        kind=L1MessageKind.TOOL_RESULT,
                        tool_call_id="call-1",
                    ),
                ],
            )
        )
        pack = service.build_pack(
            MemoryPackRequest(turn_kind="bunshin", work_order_id="memory-invocation")
        )
        context = PromptAssemblyContext(
            core_mode="bunshin",
            turn_kind="bunshin",
            work_order_id="memory-invocation",
            metadata={"memory_pack": pack},
        )

        rendered = "\n".join(
            fragment.content
            for prompt_provider in self._runner()._build_bunshin_prompt_fragment_registry().list_for_prompt()
            for fragment in prompt_provider.build_prompt_fragments(context)
        )

        self.assertEqual(rendered.count("calling read_file"), 1)
        self.assertEqual(rendered.count("README contents"), 1)
        self.assertNotIn("full result retired", rendered)

    def test_active_input_is_filtered_from_l1_prompt_projection_only(self) -> None:
        service, _provider = self._memory_service()
        service.commit_l1(
            MemoryCommitRequest(
                turn_id="memory-run",
                transcript=[
                    L1TranscriptMessage(
                        role="user",
                        content="prior input remains visible",
                        kind=L1MessageKind.USER_REQUEST,
                        payload={"_pal_input_id": "prior"},
                    ),
                    L1TranscriptMessage(
                        role="user",
                        content="current input is supplied by the turn",
                        kind=L1MessageKind.USER_REQUEST,
                        payload={"_pal_input_id": "current"},
                    ),
                ],
            )
        )

        pack = service.build_pack(
            MemoryPackRequest(
                turn_kind="bunshin",
                work_order_id="memory-invocation",
                active_input_id="current",
            )
        )

        self.assertEqual(
            [message.content for message in pack.l1_recent_context],
            ["prior input remains visible"],
        )
        self.assertEqual(
            service.l1_store.items[0][1].content,
            "current input is supplied by the turn",
        )

    def test_recall_keeps_cache_without_duplicate_bunshin_prompt_projection(self) -> None:
        service, provider = self._memory_service(
            {
                "document_id": "case:queue-repair",
                "document_kind": "case",
                "scope": "project",
                "title": "Queue repair",
                "summary": "Use the bounded wake-up protocol after closing the queue.",
                "search_text": "bounded queue close wake repair",
                "canonical_key": "queue-repair",
            }
        )
        provider.recall(
            MemoryQuery(
                queries=["bounded queue close"],
                scope="project",
                limit=3,
            )
        )
        pack = service.build_pack(
            MemoryPackRequest(turn_kind="bunshin", work_order_id="memory-invocation")
        )
        context = PromptAssemblyContext(
            core_mode="bunshin",
            turn_kind="bunshin",
            work_order_id="memory-invocation",
            metadata={"memory_pack": pack},
        )

        fragments = [
            fragment
            for prompt_provider in self._runner()._build_bunshin_prompt_fragment_registry().list_for_prompt()
            for fragment in prompt_provider.build_prompt_fragments(context)
        ]
        recalled = [fragment for fragment in fragments if fragment.title == "Recalled memories"]

        self.assertEqual(recalled, [])
        self.assertTrue(pack.l2_working_memory)
        self.assertFalse(any("Use the bounded wake-up protocol" in fragment.content for fragment in fragments))


    def test_pending_memory_candidates_are_not_rendered_as_recalled_memory(self) -> None:
        service, _provider = self._memory_service()
        runner = self._runner()
        candidate_sink = runner._runner_memory_candidate_sink()
        candidate_sink.records.append(
            {
                "document_id": "case:pending",
                "document_kind": "case",
                "scope": "task",
                "title": "Unreviewed candidate",
                "summary": "This candidate has not been accepted by Manager.",
            }
        )
        pack = service.build_pack(
            MemoryPackRequest(turn_kind="bunshin", work_order_id="memory-invocation")
        )
        context = PromptAssemblyContext(
            core_mode="bunshin",
            turn_kind="bunshin",
            work_order_id="memory-invocation",
            metadata={"memory_pack": pack},
        )

        rendered = "\n".join(
            fragment.content
            for prompt_provider in runner._build_bunshin_prompt_fragment_registry().list_for_prompt()
            for fragment in prompt_provider.build_prompt_fragments(context)
        )

        self.assertNotIn("Unreviewed candidate", rendered)
        self.assertNotIn("This candidate has not been accepted", rendered)

    def test_bunshin_clock_counts_only_consumable_llm_rounds(self) -> None:
        runner = self._runner()
        service, _provider = self._memory_service()
        state = BunshinAgentLoopState(
            execution_runtime=SimpleNamespace(),
            memory_service=service,
            memory_candidate_sink=SimpleNamespace(),
            llm_round_count=5,
        )

        compact_required = asyncio.run(
            runner._postprocess_bunshin_llm_round(
                state,
                EffectResult(
                    status=RuntimeStatus.OK,
                    payload=generation_result_from_values(
                        finish_reason=LLMFinishReason.COMPACT_REQUIRED
                    )
                ),
            )
        )
        self.assertEqual(
            compact_required.payload.finish_reason,
            LLMFinishReason.COMPACT_REQUIRED,
        )
        self.assertEqual(state.llm_round_count, 4)
        self.assertEqual(runner.blocked_summary, "")

        asyncio.run(
            runner._postprocess_bunshin_llm_round(
                state,
                EffectResult(
                    status=RuntimeStatus.OK,
                    payload=generation_result_from_values(
                        text="partial",
                        finish_reason="max_tokens",
                    )
                ),
            )
        )
        self.assertEqual(state.llm_round_count, 3)

        asyncio.run(
            runner._postprocess_bunshin_llm_round(
                state,
                EffectResult(
                    status=RuntimeStatus.OK,
                    payload=generation_result_from_values(
                        text="",
                        finish_reason=LLMFinishReason.STOP,
                    ),
                ),
            )
        )
        self.assertEqual(state.llm_round_count, 2)

        runner.blocked_summary = ""
        asyncio.run(
            runner._postprocess_bunshin_llm_round(
                state,
                EffectResult(
                    status=RuntimeStatus.OK,
                    payload=generation_result_from_values(
                        tool_calls=[
                            new_tool_call(
                                name="read_file",
                                args={"file_path": "README.md"},
                            )
                        ],
                        finish_reason="tool_calls",
                    )
                ),
            )
        )
        self.assertEqual(state.llm_round_count, 2)

    def test_completed_round_reports_current_tool_batch_size(self) -> None:
        events: list[dict[str, object]] = []

        async def capture_event(event: dict[str, object]) -> None:
            events.append(event)

        runner = self._runner()
        runner.write_event = capture_event
        service, _provider = self._memory_service()
        state = BunshinAgentLoopState(
            execution_runtime=SimpleNamespace(),
            memory_service=service,
            memory_candidate_sink=SimpleNamespace(),
            llm_round_count=3,
            tool_call_count=17,
        )

        asyncio.run(
            runner._postprocess_bunshin_llm_round(
                state,
                EffectResult(
                    status=RuntimeStatus.OK,
                    payload=generation_result_from_values(
                        tool_calls=[
                            new_tool_call(name="read_file", args={"file_path": "a.py"}),
                            new_tool_call(name="read_file", args={"file_path": "b.py"}),
                        ],
                        finish_reason="tool_calls",
                    ),
                ),
            )
        )

        completed = [
            event
            for event in events
            if event["event_kind"] == "progress"
            and event["payload"]["phase"] == "llm_round_completed"
        ]
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["payload"]["tool_call_count"], 2)
        self.assertEqual(state.tool_call_count, 17)

    def test_truncated_bunshin_round_is_discarded_and_retried_with_bounded_tool_note(self) -> None:
        runner = self._runner()
        service, _provider = self._memory_service()
        turn_id = "length-recovery-turn"
        service.begin_l1_turn(turn_id, user_text="implement the module")
        prior_call = new_tool_call(
            call_id="read-1",
            name="read_file",
            args={"file_path": "owned.cpp"},
        )
        service.upsert_l1_assistant(
            turn_id,
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(prior_call,),
                message_id="prior-assistant",
            ),
        )
        service.append_l1_tool_result(
            turn_id,
            ToolResultIR(
                call_id="read-1",
                name="read_file",
                content="owned source",
            ),
        )
        truncated = generation_result_from_values(
            text="a very large uncommitted response",
            finish_reason=LLMFinishReason.LENGTH,
        )
        service.upsert_l1_assistant(turn_id, truncated.response.message)
        state = BunshinAgentLoopState(
            execution_runtime=SimpleNamespace(),
            memory_service=service,
            memory_candidate_sink=SimpleNamespace(),
            llm_round_count=8,
            tool_call_count=1,
        )

        asyncio.run(
            runner._postprocess_bunshin_llm_round(
                state,
                EffectResult(status=RuntimeStatus.OK, payload=truncated),
                continuation=SimpleNamespace(turn_id=turn_id),
            )
        )

        active = service.active_l1_turn(turn_id)
        self.assertIsNotNone(active)
        message_ids = [message.message_id for message in active.messages]
        self.assertIn("prior-assistant", message_ids)
        self.assertNotIn(truncated.response.message.message_id, message_ids)
        self.assertEqual(active.pending_call_ids, frozenset())
        self.assertEqual(state.llm_round_count, 7)
        self.assertEqual(state.output_length_recovery_count, 1)
        self.assertIn("bounded action", state.pending_output_length_recovery_note)
        self.assertIn(
            "complete tool calls",
            runner._build_bunshin_retry_note(
                truncated,
                [SimpleNamespace()],
                retry_count=2,
                state=state,
            ),
        )
        self.assertEqual(runner.blocked_summary, "")

    def test_truncated_round_with_committed_tool_item_is_kept_for_execution(self) -> None:
        runner = self._runner()
        service, _provider = self._memory_service()
        turn_id = "committed-length-turn"
        service.begin_l1_turn(turn_id, user_text="implement the module")
        truncated = generation_result_from_values(
            tool_calls=[
                new_tool_call(
                    call_id="write-1",
                    name="write_file",
                    args={"file_path": "owned.cpp", "content": "int x;"},
                )
            ],
            finish_reason=LLMFinishReason.LENGTH,
        )
        truncated = replace(
            truncated,
            response=replace(
                truncated.response,
                message=replace(
                    truncated.response.message,
                    metadata={
                        "committed_items": [
                            {"item_id": "write-item", "item_kind": "tool_call"}
                        ]
                    },
                ),
            ),
        )
        service.upsert_l1_assistant(turn_id, truncated.response.message)
        state = BunshinAgentLoopState(
            execution_runtime=SimpleNamespace(),
            memory_service=service,
            memory_candidate_sink=SimpleNamespace(),
            llm_round_count=2,
        )

        result = asyncio.run(
            runner._postprocess_bunshin_llm_round(
                state,
                EffectResult(status=RuntimeStatus.OK, payload=truncated),
                continuation=SimpleNamespace(turn_id=turn_id),
            )
        )

        self.assertEqual([call.call_id for call in result.payload.tool_calls], ["write-1"])
        self.assertEqual(state.llm_round_count, 2)
        self.assertEqual(state.output_length_recovery_count, 0)
        self.assertEqual(state.pending_output_length_recovery_note, "")
        active = service.active_l1_turn(turn_id)
        self.assertIn(truncated.response.message.message_id, [item.message_id for item in active.messages])

    def test_retryable_llm_error_does_not_become_a_blocked_completion(self) -> None:
        runner = self._runner()
        service, _provider = self._memory_service()
        state = BunshinAgentLoopState(
            execution_runtime=SimpleNamespace(),
            memory_service=service,
            memory_candidate_sink=SimpleNamespace(),
            llm_round_count=1,
        )

        with self.assertRaises(BunshinLLMRetryableError):
            asyncio.run(
                runner._postprocess_bunshin_llm_round(
                    state,
                    EffectResult(
                        status=RuntimeStatus.OK,
                        payload=generation_result_from_values(
                            text="endpoint failed",
                            finish_reason=LLMFinishReason.ERROR,
                        ),
                    ),
                )
            )
        self.assertEqual(runner.blocked_summary, "")
        self.assertEqual(state.llm_round_count, 0)

    def test_closed_checkpoint_reopens_a_distinct_active_l1_turn(self) -> None:
        runner = self._runner()
        service, _provider = self._memory_service()
        service.begin_l1_turn("run:invocation:input", user_text="work")
        service.settle_l1_turn("run:invocation:input")

        recovered = runner._resume_or_reopen_l1_turn(
            service,
            run_id="run",
            active_input_id="input",
            user_text="work",
            fencing_token=2,
        )

        self.assertEqual(recovered, "run:invocation:input:recovery:2")
        self.assertIsNotNone(service.active_l1_turn(recovered))
        self.assertIsNotNone(service.l1_store.turns.get("run:invocation:input"))

    def test_new_assignment_does_not_resume_prior_active_l1_turn(self) -> None:
        runner = self._runner()
        service, _provider = self._memory_service()
        stale_id = "run:invocation:assignment-old"
        service.begin_l1_turn(stale_id, user_text="workspace=/attempts/fence-1")

        selected = runner._resume_or_reopen_l1_turn(
            service,
            run_id="run",
            active_input_id="assignment-new",
            user_text="workspace=/attempts/fence-4",
            fencing_token=4,
            reuse_active=False,
        )
        runner._abort_stale_l1_turns(
            service,
            active_turn_id=selected,
        )

        self.assertEqual(selected, "run:invocation:assignment-new")
        self.assertEqual(
            str(service.l1_store.turns.get(stale_id).state),
            "aborted",
        )



class BunshinProposalCapabilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_proposal_tool_replaces_batch_without_touching_shared_memory(self):
        sink = MockL3Plugin(provider_id="candidate-only")
        runtime = BunshinScopedExecutionRuntime(ExecutionRuntime(),
            ["op_bunshin_memory_candidate_write"], workspace={"invocation_id": "proposal-test"}, memory_candidate_sink=sink)
        contracts = runtime.build_llm_tool_contracts()
        self.assertTrue(any(item["function"]["name"] == "propose_memories" for item in contracts))
        from pal.bunshin.runner import _memory_candidates_from_sink
        candidate = {"kind": "fact", "title": "explicit", "summary": "  exact\n",
                     "source_excerpt": "source", "topics": ["api"]}
        result = await runtime.execute_tool_async(new_tool_call(name="propose_memories",
            args={"candidates": [candidate]}))
        self.assertTrue(result.ok, result.text)
        self.assertEqual(_memory_candidates_from_sink(sink)[0]["summary"], "  exact\n")
        result = await runtime.execute_tool_async(new_tool_call(name="propose_memories",
            args={"candidates": []}))
        self.assertTrue(result.ok)
        self.assertEqual(sink.records, [])
