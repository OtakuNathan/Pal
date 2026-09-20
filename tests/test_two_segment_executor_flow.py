"""G3(c): two-segment executor/engine flow (pal-two-segment-v3).

The executor's ``compaction_mode='two_segment'`` path promotes closed
history, opens the run on the history owner, captures the LEFT segment
only, and installs through ``compact_left`` — the right side never enters
the summary source and survives the install byte-identically.  The LLM is
scripted (network-side fake only); the engine, validator, memory service,
and history root under test are real.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from pal.core.compaction import CompactionClockKind, CompactionEngine, CompactionSnapshot
from pal.core.pal_compaction import PalCompactionPolicy
from pal.llm import generation_result_from_values
from pal.memory import MemoryService
from pal.memory.contracts import L1MessageKind, L1TranscriptMessage


def _seed_transcript(text: str = "SEED0 prior summary") -> list[L1TranscriptMessage]:
    return [
        L1TranscriptMessage(
            role="assistant", content=text,
            kind=L1MessageKind.RUNTIME_CONTEXT_SUMMARY,
        )
    ]


def _settled_transcript(mark: str) -> list[L1TranscriptMessage]:
    return [
        L1TranscriptMessage(role="user", content=f"{mark} request",
                            kind=L1MessageKind.USER_REQUEST),
        L1TranscriptMessage(role="assistant", content=f"{mark} reply",
                            kind=L1MessageKind.ASSISTANT_REPLY),
    ]


class _ScriptedLLM:
    """Fake provider network only; the engine drives the real validator."""

    def __init__(self, text: str) -> None:
        self.text = text
        self.requests: list[Any] = []

    async def __call__(self, request, **kwargs):
        self.requests.append(request)
        if isinstance(self.text, Exception):
            raise self.text
        return generation_result_from_values(text=str(self.text))


def _service_with_history() -> MemoryService:
    service = MemoryService()
    service.l1_store.append(_seed_transcript())
    for mark in ("s0", "s1"):
        service.l1_store.append(_settled_transcript(mark))
    return service


class TwoSegmentExecutorFlowTests(unittest.TestCase):
    def test_capture_left_and_engine_install_left_only(self):
        service = _service_with_history()
        root = service.history_root
        # Right side: an active work tail with real content.
        service.begin_l1_turn("task-R", user_text="RIGHT_SENTINEL work")

        engine = CompactionEngine(policy=PalCompactionPolicy(), max_attempts=1,
                                  timeout_seconds=10.0)
        from tests.test_runtime_compaction import _valid_pal_payload
        llm = _ScriptedLLM(_valid_pal_payload("SUMMARY v3 executor flow"))
        runtime = SimpleNamespace(
            generate_llm_async=None,
            preferred_endpoint_id="",
        )

        async def scenario():
            root.promote()
            left_snapshot = service.begin_left_compaction(
                "run-exec", reason="auto", parent_turn_id="task-R")
            snapshot = CompactionSnapshot.capture_left(
                service, left_snapshot,
                target_input_budget=1_000_000,
                reserved_output_tokens=1024,
                clock_kind=CompactionClockKind.USER_TURN,
                clock_value=1,
                metadata={"compaction_op_id": "run-exec"},
            )
            return await engine.run(
                snapshot, llm_runtime=_Transport(llm),
                memory_service=service,
            )

        result = asyncio.run(scenario())
        self.assertTrue(
            result.success,
            f"engine run failed: {result.failures}",
        )
        # Only one generation left the building, and it carried L only.
        self.assertEqual(len(llm.requests), 1)
        turns = {t.turn_id: t for t in service.l1_store.turns.turns}
        self.assertIn("task-R", turns)
        self.assertNotIn("settled-s0", turns)
        self.assertNotIn("settled-s1", turns)
        seed_text = service.l1_store.turns.turns[0].messages[0].text
        self.assertIn("SUMMARY v3 executor flow", seed_text)
        # The install kept the right tail verbatim.
        right = root.right_turns()
        self.assertEqual([t.turn_id for t in right], ["task-R"])
        self.assertIn("RIGHT_SENTINEL", right[0].messages[0].text)
        # promote bumped the revision once, the install once more.
        self.assertEqual(root.left_revision, 2)
        self.assertTrue(result.memory_result.metadata.get("two_segment"))
        self.assertEqual(result.memory_result.metadata.get("status"), "committed")

    def test_no_benefit_run_reports_without_installing(self):
        service = _service_with_history()
        root = service.history_root
        root.promote()
        service.begin_left_compaction("run-1", reason="auto")
        service.compact_left("run-1", _entry("S1"), candidate_id="c1")
        # Left is now the minimal seed again.
        with self.assertRaises(Exception) as ctx:
            service.begin_left_compaction("run-2", reason="auto")
        self.assertEqual(type(ctx.exception).__name__, "NoBeneficialCompaction")

    def test_late_completion_after_cancel_fails_engine(self):
        service = _service_with_history()
        root = service.history_root
        root.promote()
        left_snapshot = service.begin_left_compaction("run-x", reason="auto")
        snapshot = CompactionSnapshot.capture_left(
            service, left_snapshot,
            target_input_budget=1_000_000, reserved_output_tokens=1024,
            clock_kind=CompactionClockKind.USER_TURN, clock_value=1,
            metadata={"compaction_op_id": "run-x"},
        )
        # Interrupt wins before the network answer exists.
        verdict = root.interrupt_compaction_for_turn("task-R")
        self.assertEqual(verdict, "no_active_turn")
        root.cancel("run-x", reason="interrupt")
        from pal.memory.history_root import TerminalClosed
        with self.assertRaises(TerminalClosed):
            service.compact_left("run-x", _entry("late"), candidate_id="late")
        self.assertNotIn("SUMMARY", service.l1_store.turns.turns[0].messages[0].text)


def _entry(text: str):
    from pal.memory import L2Entry

    return L2Entry(
        entry_id="memory_summary_current", kind="summary", scope="system",
        title="Conversation Summary", source_kind="l1_compaction",
        candidate_state="stable", summary=text, rendered=text,
        search_text=text, payload={"summary": {"summary": text}},
    )


class _Transport:
    """Engine-facing runtime shim: routes agenerate to the scripted LLM."""

    def __init__(self, llm: _ScriptedLLM) -> None:
        self._llm = llm

    async def agenerate(self, request, *args, **kwargs):
        return await self._llm(request, **kwargs)


if __name__ == "__main__":
    unittest.main()
