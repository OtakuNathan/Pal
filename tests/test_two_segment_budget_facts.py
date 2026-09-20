"""G4 (owner-level): budget and input-boundary facts for the v3 flow.

Pins the Q/B family facts that live at the two-segment authority and
engine layers with real code; the runtime-level remainder (Q04 BUSY
routing, B04-B08 preflight gates, H02/H04/H06 shutdown recovery and old
schema migration) is honestly NOT_RUN in the acceptance ledger with the
delivery documenting what exists and what does not.
"""

from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from pal.core.compaction import (
    CompactionClockKind,
    CompactionEngine,
    CompactionSnapshot,
)
from pal.core.pal_compaction import PalCompactionPolicy
from pal.memory import MemoryService
from pal.memory.contracts import L1MessageKind, L1TranscriptMessage


def _seed() -> list[L1TranscriptMessage]:
    return [L1TranscriptMessage(role="assistant", content="SEED0",
                                kind=L1MessageKind.RUNTIME_CONTEXT_SUMMARY)]


def _settled(mark: str) -> list[L1TranscriptMessage]:
    return [
        L1TranscriptMessage(role="user", content=f"{mark} request " + "payload " * 400,
                            kind=L1MessageKind.USER_REQUEST),
        L1TranscriptMessage(role="assistant", content=f"{mark} reply",
                            kind=L1MessageKind.ASSISTANT_REPLY),
    ]


class OwnerBudgetFactsTests(unittest.TestCase):
    def test_B01_local_history_may_exceed_any_window(self):
        """Appends are never rejected for size; only sends are gated (I13)."""

        service = MemoryService()
        service.l1_store.append(_seed())
        for index in range(20):
            service.l1_store.append(_settled(f"bulk-{index}"))
        # No token accounting exists at the owner; every append landed.
        self.assertEqual(len(service.l1_store.turns.turns), 21)

    def test_B03_handoff_source_must_fit_the_visible_budget(self):
        """A left segment too large for the compaction window fails the
        engine preflight instead of sending an illegal handoff (B03)."""

        service = MemoryService()
        service.l1_store.append(_seed())
        for index in range(6):
            service.l1_store.append(_settled(f"big-{index}"))
        root = service.history_root
        root.promote()
        left_snapshot = service.begin_left_compaction("run-b03", reason="auto")
        snapshot = CompactionSnapshot.capture_left(
            service, left_snapshot,
            target_input_budget=1,  # deliberately impossible
            reserved_output_tokens=1024,
            clock_kind=CompactionClockKind.USER_TURN, clock_value=1,
            metadata={"compaction_op_id": "run-b03"},
        )
        engine = CompactionEngine(policy=PalCompactionPolicy(),
                                  max_attempts=1, timeout_seconds=10.0)

        class _NoNetwork:
            async def agenerate(self, request, *a, **kw):  # pragma: no cover
                raise AssertionError("preflight must refuse before any send")

        result = asyncio.run(engine.run(
            snapshot, llm_runtime=_NoNetwork(), memory_service=service))
        self.assertFalse(result.success)
        # The left segment is untouched after the refused run.
        root.fail("run-b03", reason="preflight budget")
        left_text = " ".join(
            m.text for t in service.history_root.left_turns() for m in t.messages)
        self.assertIn("big-0 reply", left_text)

    def test_Q03_queued_input_never_enters_the_left_source(self):
        """Input that has not been admitted to history cannot appear in a
        left snapshot: the source is derived from stored turns only."""

        service = MemoryService()
        service.l1_store.append(_seed())
        service.l1_store.append(_settled("s0"))
        root = service.history_root
        root.promote()
        snapshot = service.begin_left_compaction("run-q03", reason="auto")
        blob = repr([m for t in snapshot.turns for m in t.messages])
        self.assertNotIn("UNADMITTED_QUEUE_TEXT", blob)
        # An R turn opened after capture is equally invisible to it.
        service.begin_l1_turn("late", user_text="UNADMITTED_QUEUE_TEXT")
        self.assertNotIn("UNADMITTED_QUEUE_TEXT",
                         repr([m for t in snapshot.turns for m in t.messages]))


if __name__ == "__main__":
    unittest.main()
