"""G3(a): MemoryService two-segment integration (pal-two-segment-v3).

The service exposes the HistoryRoot to its hosts (single store, lazy heal)
and the v3 install path begin_left_compaction/compact_left.  These tests pin
the service-level contract the runtime wiring (G3b) will drive: left-only
capture, R preservation, run arbitration through the service facade, and
continuity of the installed seed with the L1 reader stack.
"""

from __future__ import annotations

import unittest

from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory import MemoryService, L2Entry
from pal.memory.contracts import L1MessageKind, L1TranscriptMessage
from pal.memory.history_root import CandidateConflict, StaleRun, TerminalClosed


def _seed_transcript(text: str = "SEED0 prior summary") -> list[L1TranscriptMessage]:
    return [
        L1TranscriptMessage(
            role="assistant",
            content=text,
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


def _entry(text: str) -> L2Entry:
    return L2Entry(
        entry_id="memory_summary_current",
        kind="summary",
        scope="system",
        title="Conversation Summary",
        source_kind="l1_compaction",
        candidate_state="stable",
        summary=text,
        rendered=text,
        search_text=text,
        payload={"summary": {"summary": text}},
    )


class ServiceTwoSegmentTests(unittest.TestCase):
    def _service(self, *, settled: int = 2) -> MemoryService:
        service = MemoryService()
        service.l1_store.append(_seed_transcript())
        for index in range(settled):
            service.l1_store.append(_settled_transcript(f"s{index}"))
        service.begin_l1_turn("task-T", user_text="Q_ORIGINAL work")
        service.upsert_l1_assistant(
            "task-T",
            LLMMessageIR(role=MessageRole.ASSISTANT,
                         parts=(TextPartIR("A_DECISION"),),
                         message_id="assistant-1"),
        )
        # task-T stays ACTIVE: the work tail is never promoted by default.
        return service

    def test_root_attachment_and_heal(self):
        service = self._service()
        root = service.history_root
        self.assertIs(root.store, service.l1_store.turns)
        self.assertIs(service.history_root, root)
        # Swapping the transcript store heals the root onto the new one.
        service.l1_store.items = [_seed_transcript()]
        self.assertIsNot(service.history_root, root)
        self.assertIs(service.history_root.store, service.l1_store.turns)

    def test_left_transcripts_and_compact_left_flow(self):
        service = self._service()
        root = service.history_root
        root.promote()  # seed + s0 + s1 move; task-T active stays
        snapshot = service.begin_left_compaction(
            "run-1", reason="auto", parent_turn_id="task-2")
        self.assertEqual(len(snapshot.turns), 3)
        left_text = " ".join(
            item.content
            for transcript in service.left_transcripts()
            for item in transcript
        )
        self.assertIn("SEED0", left_text)
        self.assertIn("s0 reply", left_text)
        self.assertNotIn("Q_ORIGINAL", left_text)

        after = []
        outcome = service.compact_left(
            "run-1", _entry("SUMMARY v3"), candidate_id="c1",
            after_commit=lambda: after.append(True),
        )
        self.assertEqual(outcome.status, "committed")
        self.assertEqual(after, [True])
        # Only the left was replaced: task-T survives with identity intact.
        turns = {t.turn_id: t for t in service.l1_store.turns.turns}
        self.assertIn("task-T", turns)
        self.assertNotIn("settled-s0", turns)
        seed_text = service.l1_store.turns.turns[0].messages[0].text
        self.assertIn("SUMMARY v3", seed_text)
        # The L1 reader stack still sees one continuity seed (A03).
        self.assertIsNotNone(service.l1_store.turns.continuity)

    def test_after_commit_failure_keeps_installed_left(self):
        service = self._service()
        root = service.history_root
        root.promote()
        service.begin_left_compaction("run-x", reason="auto")

        def boom() -> None:
            raise RuntimeError("cleanup exploded")

        outcome = service.compact_left(
            "run-x", _entry("S"), candidate_id="c1", after_commit=boom)
        self.assertEqual(outcome.status, "committed")
        self.assertEqual(len(service.failed_retirements), 1)
        self.assertIn("S", service.l1_store.turns.turns[0].messages[0].text)

    def test_duplicate_delivery_is_idempotent_not_conflict(self):
        service = self._service()
        root = service.history_root
        root.promote()
        service.begin_left_compaction("run-d", reason="auto")
        first = service.compact_left("run-d", _entry("D"), candidate_id="cd")
        second = service.compact_left("run-d", _entry("D"), candidate_id="cd")
        self.assertTrue(second.replayed)
        self.assertEqual(second.left_revision, first.left_revision)

    def test_conflicting_candidate_via_service(self):
        service = self._service()
        root = service.history_root
        root.promote()
        service.begin_left_compaction("run-c", reason="auto")
        service.compact_left("run-c", _entry("A"), candidate_id="ca")
        with self.assertRaises(TerminalClosed):
            service.compact_left("run-c", _entry("B"), candidate_id="cb")

    def test_unopened_run_rejected(self):
        service = self._service()
        service.history_root.promote()
        with self.assertRaises(StaleRun):
            service.compact_left("never-opened", _entry("S"))

    def test_empty_entry_rejected_before_ready(self):
        service = self._service()
        service.history_root.promote()
        service.begin_left_compaction("run-e", reason="auto")
        blank = _entry("S")
        object.__setattr__(blank, "summary", "")
        with self.assertRaises(ValueError):
            service.compact_left("run-e", blank, candidate_id="c")
        # The run never reached READY and can still complete properly.
        outcome = service.compact_left("run-e", _entry("S"), candidate_id="c")
        self.assertEqual(outcome.status, "committed")


class OwnerFacadeArbitrationTests(unittest.TestCase):
    """Runtime-facing intent facade over the history root (G3c-2).

    /interrupt and confirmed reset reach the owner through MemoryService,
    never by reaching into history_root internals from the runtime.
    """

    def _service_with_run(self, run_id: str, *, parent_turn_id: str) -> MemoryService:
        service = MemoryService()
        service.l1_store.append(_seed_transcript())
        service.l1_store.append(_settled_transcript("s0"))
        service.begin_l1_turn("task-T", user_text="work")
        service.history_root.promote()
        service.begin_left_compaction(
            run_id, reason="auto", parent_turn_id=parent_turn_id
        )
        return service

    def test_interrupt_falls_back_to_parent_turn_and_cancels(self):
        service = self._service_with_run("run-1", parent_turn_id="task-2")
        # The interrupting client has no active turn id to offer; the owner
        # still cancels a run that belongs to a turn.
        self.assertEqual(service.interrupt_compaction_for_turn(None), "cancelled")

    def test_interrupt_idle_manual_run_is_not_fake_cancelled(self):
        service = self._service_with_run("run-2", parent_turn_id="")
        self.assertEqual(
            service.interrupt_compaction_for_turn(None), "no_active_turn"
        )
        self.assertEqual(
            service.interrupt_compaction_for_turn("someone-else"), "no_active_turn"
        )

    def test_cancel_active_compaction_returns_outcome(self):
        service = self._service_with_run("run-3", parent_turn_id="task-2")
        outcome = service.cancel_active_compaction(reason="reset")
        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.status, "cancelled")

    def test_cancel_active_compaction_without_live_run(self):
        service = MemoryService()
        self.assertIsNone(service.cancel_active_compaction(reason="reset"))


if __name__ == "__main__":
    unittest.main()
