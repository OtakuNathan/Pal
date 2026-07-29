from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.core.turn_executor import TurnExecutor
from pal.execution.tool_facade import EffectOutcome, FailedResult, RetryDirective
from pal.llm import CanonicalToolCall, CanonicalToolResult
from pal.memory import (
    CompactionProfile,
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
    build_minion_prior_user_inputs_compaction_payload,
    compact_minion_memory_service,
    project_minion_tool_protocol,
)
from pal.minion.runner import MinionRunner
from pal.plugins.l3 import MockL3Plugin
from pal.shared import MinionInvocationPack, PromptAssemblyContext


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
        self.assertFalse(memory_providers[0].include_l1_recent_context)

    def test_minion_does_not_duplicate_tool_protocol_from_l1(self) -> None:
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

        self.assertNotIn("calling read_file", rendered)
        self.assertNotIn("README contents", rendered)

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
        compact_minion_memory_service(
            service,
            MemoryCompactRequest(
                target_input_budget=2048,
                reserved_output_tokens=512,
                profile=CompactionProfile.MINION,
                metadata={
                    "structured_compaction": build_minion_prior_user_inputs_compaction_payload(
                        [],
                        target_input_budget=2048,
                    )
                },
            ),
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
        self.assertIn('<compact_context kind="minion"', summaries[0].content)
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


if __name__ == "__main__":
    unittest.main()
