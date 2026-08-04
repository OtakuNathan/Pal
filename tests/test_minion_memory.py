from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, ToolResultIR, new_tool_call

import asyncio
import shutil
import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace

from pal.core import CompactionClockKind, CompactionSnapshot
from pal.core.module_registry import ModuleHandle, ModuleRegistry
from pal.core.runtime_state import RuntimeSnapshotCoordinator, RuntimeSnapshotIdentity
from pal.llm import (
    generation_result_from_values,
)
from pal.llm.ir import LLMMessageIR, MessageRole, ReasoningPartIR, TextPartIR
from pal.memory import (
    L3ProviderSelector,
    L1MessageKind,
    L1TranscriptMessage,
    MemoryCommitRequest,
    MemoryCompactRequest,
    MemoryPackRequest,
    MemoryQuery,
    MemoryService,
)
from pal.memory.prompt import MemoryPromptFragmentProvider
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.minion.compact import (
    MinionCompactionPolicy,
)
from pal.minion.checkpoint import (
    open_agent_session_checkpoint,
    seal_agent_session_checkpoint,
)
from pal.minion.runner import (
    MinionAgentLoopState,
    MinionLLMRetryableError,
    MinionRunner,
    _minion_llm_request_metadata,
)
from pal.plugins.l3 import MockL3Plugin
from pal.shared import (
    LLMFinishReason,
    MinionInvocationPack,
    PromptAssemblyContext,
    RuntimeStatus,
)
from pal.core.turns import EffectResult


async def _noop_write_event(_event):
    return None


async def _noop_read_decision(_timeout):
    return None


class MinionMemoryIntegrationTests(unittest.TestCase):
    def _runner(self) -> MinionRunner:
        runtime_root = Path(tempfile.mkdtemp(prefix="pal_minion_memory_"))
        self.addCleanup(shutil.rmtree, runtime_root, True)
        return MinionRunner(
            runtime_root=runtime_root,
            pack=MinionInvocationPack(
                invocation_id="memory-invocation",
                goal="Use prior project knowledge.",
            ),
            minion_id="memory-minion",
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

    def test_minion_registers_the_main_memory_prompt_provider(self) -> None:
        providers = self._runner()._build_minion_prompt_fragment_registry().list_for_prompt()

        memory_providers = [provider for provider in providers if isinstance(provider, MemoryPromptFragmentProvider)]
        self.assertEqual(len(memory_providers), 1)
        self.assertTrue(memory_providers[0].include_l1_recent_context)

    def test_minion_role_timeout_is_forwarded_to_the_host_llm_request(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="timeout-invocation",
            goal="Preserve the logical role timeout.",
            metadata={"llm_round_timeout_seconds": 3000},
        )

        metadata = _minion_llm_request_metadata(pack, "timeout-run")

        self.assertEqual(metadata["timeout_seconds"], 3000.0)

    def test_minion_renders_closed_tool_protocol_from_l1_once(self) -> None:
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
            MemoryPackRequest(turn_kind="minion", work_order_id="memory-invocation")
        )
        context = PromptAssemblyContext(
            core_mode="minion",
            turn_kind="minion",
            work_order_id="memory-invocation",
            metadata={"memory_pack": pack},
        )

        rendered = "\n".join(
            fragment.content
            for prompt_provider in self._runner()._build_minion_prompt_fragment_registry().list_for_prompt()
            for fragment in prompt_provider.build_prompt_fragments(context)
        )

        self.assertEqual(rendered.count("calling read_file"), 1)
        self.assertEqual(rendered.count("README contents"), 1)

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
                turn_kind="minion",
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

    def test_recall_projects_into_the_same_cache_minion_prompt_renders(self) -> None:
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
            MemoryPackRequest(turn_kind="minion", work_order_id="memory-invocation")
        )
        context = PromptAssemblyContext(
            core_mode="minion",
            turn_kind="minion",
            work_order_id="memory-invocation",
            metadata={"memory_pack": pack},
        )

        fragments = [
            fragment
            for prompt_provider in self._runner()._build_minion_prompt_fragment_registry().list_for_prompt()
            for fragment in prompt_provider.build_prompt_fragments(context)
        ]
        recalled = [fragment for fragment in fragments if fragment.title == "Recalled memories"]

        self.assertEqual(len(recalled), 1)
        self.assertEqual(recalled[0].metadata["block_id"], "memory_recalled_context")
        self.assertIn('<recalled_memories view="summary">', recalled[0].content)
        self.assertIn(
            "[case:queue-repair]: Use the bounded wake-up protocol after closing the queue.",
            recalled[0].content,
        )

    def test_minion_compaction_summary_uses_the_main_memory_prompt_path(self) -> None:
        service, _provider = self._memory_service()
        snapshot = CompactionSnapshot.capture(
            service,
            target_input_budget=2048,
            reserved_output_tokens=512,
            clock_kind=CompactionClockKind.LLM_ROUND,
            clock_value=4,
        )
        summary_entry = MinionCompactionPolicy().validate_checkpoint(
            json.dumps(
                {
                    "schema": "pal.compaction.minion.v3",
                    "kind": "minion",
                    "continuity": {
                        "technical_route": [],
                        "active_work": [
                            {
                                "goal": "continue the bound module",
                                "target": "src/pal/minion/runner.py",
                                "action": "run focused tests",
                                "status": "active",
                            }
                        ],
                        "active_errors": [],
                        "active_issues": [],
                        "next_actions": [],
                    },
                    "summary": {
                        "summary": "Minion is continuing the bound module.",
                        "search_text": "runner.py focused tests",
                    },
                },
                ensure_ascii=False,
            ),
            snapshot,
        )
        service.compact(
            MemoryCompactRequest(
                target_input_budget=2048,
                reserved_output_tokens=512,
                summary_entry=summary_entry,
            )
        )
        pack = service.build_pack(
            MemoryPackRequest(turn_kind="minion", work_order_id="memory-invocation")
        )
        context = PromptAssemblyContext(
            core_mode="minion",
            turn_kind="minion",
            work_order_id="memory-invocation",
            metadata={"memory_pack": pack},
        )

        fragments = [
            fragment
            for prompt_provider in self._runner()._build_minion_prompt_fragment_registry().list_for_prompt()
            for fragment in prompt_provider.build_prompt_fragments(context)
        ]
        summaries = [fragment for fragment in fragments if fragment.title == "Conversation summary"]

        self.assertEqual(len(summaries), 1)
        self.assertIn(
            '<compact_context kind="minion" authority="work_checkpoint">',
            summaries[0].content,
        )
        self.assertIn("src/pal/minion/runner.py", summaries[0].content)
        self.assertEqual(summaries[0].metadata["block_id"], "memory_current_summary")

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
            MemoryPackRequest(turn_kind="minion", work_order_id="memory-invocation")
        )
        context = PromptAssemblyContext(
            core_mode="minion",
            turn_kind="minion",
            work_order_id="memory-invocation",
            metadata={"memory_pack": pack},
        )

        rendered = "\n".join(
            fragment.content
            for prompt_provider in runner._build_minion_prompt_fragment_registry().list_for_prompt()
            for fragment in prompt_provider.build_prompt_fragments(context)
        )

        self.assertNotIn("Unreviewed candidate", rendered)
        self.assertNotIn("This candidate has not been accepted", rendered)

    def test_minion_clock_counts_only_consumable_llm_rounds(self) -> None:
        runner = self._runner()
        service, _provider = self._memory_service()
        state = MinionAgentLoopState(
            execution_runtime=SimpleNamespace(),
            memory_service=service,
            memory_candidate_sink=SimpleNamespace(),
            llm_round_count=5,
        )

        compact_required = asyncio.run(
            runner._postprocess_minion_llm_round(
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
            runner._postprocess_minion_llm_round(
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
            runner._postprocess_minion_llm_round(
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
            runner._postprocess_minion_llm_round(
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

    def test_truncated_minion_round_is_discarded_and_retried_with_bounded_tool_note(self) -> None:
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
        state = MinionAgentLoopState(
            execution_runtime=SimpleNamespace(),
            memory_service=service,
            memory_candidate_sink=SimpleNamespace(),
            llm_round_count=8,
            tool_call_count=1,
        )

        asyncio.run(
            runner._postprocess_minion_llm_round(
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
            runner._build_minion_retry_note(
                truncated,
                [SimpleNamespace()],
                retry_count=2,
                state=state,
            ),
        )
        self.assertEqual(runner.blocked_summary, "")

    def test_retryable_llm_error_does_not_become_a_blocked_completion(self) -> None:
        runner = self._runner()
        service, _provider = self._memory_service()
        state = MinionAgentLoopState(
            execution_runtime=SimpleNamespace(),
            memory_service=service,
            memory_candidate_sink=SimpleNamespace(),
            llm_round_count=1,
        )

        with self.assertRaises(MinionLLMRetryableError):
            asyncio.run(
                runner._postprocess_minion_llm_round(
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

    def test_checkpoint_atomically_restores_complete_l1(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal_minion_compact_checkpoint_"))
        self.addCleanup(shutil.rmtree, root, True)
        service, _provider = self._memory_service()
        snapshot = CompactionSnapshot.capture(
            service,
            target_input_budget=2048,
            reserved_output_tokens=512,
            clock_kind=CompactionClockKind.LLM_ROUND,
            clock_value=6,
        )
        entry = MinionCompactionPolicy().validate_checkpoint(
            json.dumps(
                {
                    "schema": "pal.compaction.minion.v3",
                    "kind": "minion",
                    "continuity": {
                        "technical_route": [],
                        "active_work": [],
                        "active_errors": [],
                        "active_issues": [],
                        "next_actions": [],
                    },
                    "summary": {
                        "summary": "restorable compact cursor",
                        "search_text": "compact cursor",
                    },
                }
            ),
            snapshot,
        )
        service.compact(
            MemoryCompactRequest(
                target_input_budget=2048,
                reserved_output_tokens=512,
                summary_entry=entry,
            )
        )
        service.begin_l1_turn("checkpoint-run", user_text="new work after compact")
        service.upsert_l1_assistant(
            "checkpoint-run",
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(
                    ReasoningPartIR("inspect the file first"),
                    TextPartIR("I will inspect it."),
                    new_tool_call(call_id="read-1", name="read_file", args={}),
                ),
                message_id="assistant-1",
            ),
        )
        service.append_l1_tool_result(
            "checkpoint-run",
            ToolResultIR(
                call_id="read-1",
                name="read_file",
                content="validated content",
            ),
        )
        registry = ModuleRegistry()
        registry.register(
            ModuleHandle(
                module_id="memory",
                tier="test",
                runtime_state_port=MemoryRuntimeStatePort(service),
            )
        )
        identity = RuntimeSnapshotIdentity(
            logical_coroutine_id="checkpoint-compact",
            workflow_id="workflow-1",
            stage_key="module:module-a:implementation",
            sequence=1,
            producer_fencing_token=2,
            runtime_spec_hash="spec-1",
        )
        runtime_snapshot = asyncio.run(
            RuntimeSnapshotCoordinator(registry).snapshot(identity)
        )
        payload = seal_agent_session_checkpoint(
            root,
            {
                **identity.to_dict(),
                "coroutine_state": {
                    "initial_instruction": "continue module work",
                    "response_keys": ["effect-1"],
                    "active_input_id": "checkpoint-active-input",
                    "llm_round_count": 6,
                    "tool_call_count": 2,
                },
                "runtime_snapshot": runtime_snapshot,
            },
        )
        self.assertEqual(payload["schema_version"], "8")
        self.assertNotIn("new work after compact", json.dumps(payload))
        private = open_agent_session_checkpoint(root, payload)
        self.assertEqual(private["coroutine_state"]["llm_round_count"], 6)
        self.assertEqual(
            private["coroutine_state"]["active_input_id"],
            "checkpoint-active-input",
        )
        serialized_text = json.dumps(
            private["runtime_snapshot"]["modules"]["memory"]["payload"]["l1_turns"]
        )
        self.assertIn("new work after compact", serialized_text)
        self.assertIn("inspect the file first", serialized_text)

        restored_service, _restored_provider = self._memory_service()
        restored_registry = ModuleRegistry()
        restored_registry.register(
            ModuleHandle(
                module_id="memory",
                tier="test",
                runtime_state_port=MemoryRuntimeStatePort(restored_service),
            )
        )
        asyncio.run(
            RuntimeSnapshotCoordinator(restored_registry).restore(
                private["runtime_snapshot"],
                expected_identity=identity,
            )
        )
        restored = restored_service.build_pack(
            MemoryPackRequest()
        ).current_summary
        self.assertIsNotNone(restored)
        self.assertEqual(
            restored.payload["schema"],
            "pal.compaction.minion.v3",
        )
        self.assertIn(
            "new work after compact",
            "\n".join(
                message.content
                for transcript in restored_service.l1_store.items
                for message in transcript
            ),
        )
        restored_turn = restored_service.active_l1_turn("checkpoint-run")
        self.assertIsNotNone(restored_turn)
        assert restored_turn is not None
        self.assertEqual(restored_turn.pending_call_ids, frozenset())
        self.assertIn("inspect the file first", restored_turn.messages[1].reasoning_text)
        self.assertEqual(restored_turn.state.value, "active")


if __name__ == "__main__":
    unittest.main()
