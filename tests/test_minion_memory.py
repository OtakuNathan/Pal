from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
import json
from pathlib import Path
from types import SimpleNamespace

from pal.core import CompactionClockKind, CompactionSnapshot
from pal.core.turn_executor import TurnExecutor
from pal.execution.tool_facade import EffectOutcome, FailedResult, RetryDirective
from pal.llm import (
    CanonicalLLMOutcome,
    CanonicalToolCall,
    CanonicalToolResult,
)
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
from pal.minion.compact import (
    MinionCompactionPolicy,
    project_minion_tool_protocol,
)
from pal.minion.runner import (
    MinionAgentLoopState,
    MinionRunner,
    _minion_llm_request_metadata,
    _restore_minion_memory_state,
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

    def test_tool_protocol_projection_retires_only_closed_successful_batches(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-read",
                "content": "x" * 5_000,
                "_pal_result_state": {
                    "ok": True,
                    "kind": "complete",
                    "effect": "none",
                },
            },
            {"role": "assistant", "content": "continue from the validated read"},
        ]

        projected = project_minion_tool_protocol(messages, max_chars=4_096)

        self.assertEqual(projected[-1], messages[-1])
        self.assertIn("<minion_tool_continuity", projected[0]["content"])
        self.assertIn("read_file", projected[0]["content"])
        self.assertFalse(any(item.get("role") == "tool" for item in projected))

    def test_tool_protocol_projection_keeps_recovery_batches_verbatim(self) -> None:
        failed_batch = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-edit",
                        "type": "function",
                        "function": {"name": "edit_file", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call-edit",
                "content": "validation failed" + ("!" * 5_000),
                "_pal_result_state": {
                    "ok": False,
                    "kind": "failed",
                    "effect": "unknown",
                },
            },
        ]

        projected = project_minion_tool_protocol(failed_batch, max_chars=4_096)

        self.assertEqual(projected, failed_batch)

    def test_turn_executor_records_unknown_effect_as_enum_value(self) -> None:
        executor = object.__new__(TurnExecutor)
        call = CanonicalToolCall(name="edit_file", args={}, call_id="call-edit")
        invocation = FailedResult(
            error_code="handler_exception",
            error="write outcome unavailable",
            effect=EffectOutcome.UNKNOWN,
            retry=RetryDirective.RECONCILE_FIRST,
            llm_text="write outcome unavailable",
        )
        result = CanonicalToolResult(
            name="edit_file",
            ok=False,
            llm_text="write outcome unavailable",
            call_id="call-edit",
            status="handler_exception",
            invocation_result=invocation,
        )
        continuation = SimpleNamespace(
            pending_tool_call_batch=[call],
            pending_tool_results=[result],
            pending_assistant_tool_text="",
            pending_assistant_provider_specific_fields={},
            tool_protocol_messages=[],
            tool_delivery_records={},
        )

        executor._flush_tool_protocol_messages(continuation)

        tool_message = continuation.tool_protocol_messages[-1]
        self.assertEqual(tool_message["_pal_result_state"]["effect"], "unknown")
        self.assertEqual(
            project_minion_tool_protocol(
                continuation.tool_protocol_messages,
                max_chars=4_096,
            ),
            continuation.tool_protocol_messages,
        )

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
                    payload=CanonicalLLMOutcome(
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
                    payload=CanonicalLLMOutcome(
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
                    payload=CanonicalLLMOutcome(
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
                    payload=CanonicalLLMOutcome(
                        tool_calls=[
                            CanonicalToolCall(
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

    def test_checkpoint_atomically_restores_complete_l1(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal_minion_compact_checkpoint_"))
        self.addCleanup(shutil.rmtree, root, True)
        checkpoint_path = root / "attempt" / "continuation.json"
        pack = MinionInvocationPack(
            invocation_id="checkpoint-compact",
            goal="continue module work",
            metadata={
                "agent_session": {
                    "session_id": "checkpoint-compact",
                    "response_key": "effect-1",
                    "fencing_token": 2,
                    "scope_kind": "module_run",
                    "subject_key": "module-a",
                    "continuation_output_path": str(checkpoint_path),
                }
            },
        )
        runner = MinionRunner(
            runtime_root=root,
            pack=pack,
            minion_id="checkpoint-compact",
            run_id="checkpoint-run",
            write_event=_noop_write_event,
            read_decision=_noop_read_decision,
        )
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
        service.commit_l1(
            MemoryCommitRequest(
                turn_id="checkpoint-run",
                transcript=[
                    L1TranscriptMessage(
                        role="user",
                        content="new work after compact",
                        kind=L1MessageKind.USER_REQUEST,
                    )
                ],
            )
        )
        state = SimpleNamespace(
            llm_round_count=6,
            tool_call_count=2,
            execution_runtime=SimpleNamespace(registry_generation=None),
            memory_service=service,
        )
        continuation = SimpleNamespace(
            pending_tool_call_batch=[],
            pending_tool_results=[],
            tool_protocol_messages=[
                {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "read-1",
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
                    "tool_call_id": "read-1",
                    "content": "validated content",
                },
            ],
            tool_delivery_records={},
            tool_batch_count=1,
            preferred_llm_endpoint_id=None,
            preferred_llm_model_id=None,
            turn_settings_snapshot={},
            l1_input_committed=True,
            l1_protocol_committed_count=2,
        )
        runner._persist_agent_session_checkpoint(
            pack.workspace,
            state,
            continuation,
            initial_instruction="continue module work",
            response_keys=["effect-1"],
            max_output_tokens=2048,
        )

        payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["schema_version"], "5")
        self.assertEqual(payload["llm_round_count"], 6)
        serialized_text = json.dumps(payload["l1_items"])
        self.assertIn("restorable compact cursor", serialized_text)
        self.assertIn("new work after compact", serialized_text)
        self.assertFalse(checkpoint_path.with_name("protocol.jsonl").exists())

        restored_service, _restored_provider = self._memory_service()
        _restore_minion_memory_state(restored_service, payload)
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


if __name__ == "__main__":
    unittest.main()
