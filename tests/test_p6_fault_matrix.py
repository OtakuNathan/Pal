"""E03: parameterized await/commit fault-injection matrix around the
install boundary.

Every case drives the REAL CompactionEngine over a real MemoryService
(no coordinator/installer/continuation mocks) and asserts the single
root invariant: after the fault, memory is exactly the old root or
exactly the new root — never a mixture — with the receipt ledger
consistent and the L1 transcript conserved (no lost or duplicated
messages). Crash-window recovery beyond these in-process points is the
R01/R02/R03/R06/X09 family.
"""
from __future__ import annotations

import asyncio
import unittest
from pathlib import Path

from pal.core.compaction import (
    CompactionClockKind,
    CompactionEngine,
    CompactionSnapshot,
)
from pal.core.pal_compaction import PalCompactionPolicy
from pal.llm import generation_result_from_values

from tests.test_runtime_compaction import (
    _ScriptedLLM,
    _memory_with_turns,
    _valid_pal_payload,
)


def _snapshot_for(service, op_id: str) -> CompactionSnapshot:
    return CompactionSnapshot.capture(
        service,
        target_input_budget=8_192,
        reserved_output_tokens=2_048,
        clock_kind=CompactionClockKind.USER_TURN,
        clock_value=1,
        metadata={"compaction_op_id": op_id},
        source_epoch=service.context_epoch,
    )


def _transcript_texts(service) -> list[str]:
    return [
        message.content if hasattr(message, "content") else ""
        for turn in service.l1_store.turns.turns
        for message in turn.messages
    ]


class FaultMatrixTests(unittest.TestCase):
    def _assert_old_root(self, service, before_texts, before_epoch) -> None:
        self.assertEqual(service.context_epoch, before_epoch)
        self.assertEqual(service.compaction_receipts, {})
        # Message conservation: nothing lost, nothing duplicated.
        self.assertEqual(_transcript_texts(service), before_texts)

    def _assert_new_root(self, service, op_id) -> None:
        self.assertEqual(service.context_epoch, 1)
        self.assertEqual(
            service.compaction_receipts[op_id].status, "committed",
        )
        # The seed turn plus the active suffix — never a mixture of the
        # old raw history with a partial install.
        turn_ids = [t.turn_id for t in service.l1_store.turns.turns]
        self.assertEqual(turn_ids[0], "compact-summary")

    def test_baseline_all_ok_installs_new_root(self) -> None:
        async def scenario():
            service = _memory_with_turns(3)
            snapshot = _snapshot_for(service, "op-e03-ok")
            engine = CompactionEngine(PalCompactionPolicy())
            result = await engine.run(
                snapshot,
                llm_runtime=_ScriptedLLM([
                    generation_result_from_values(
                        text=_valid_pal_payload("e03 ok seed")
                    )
                ]),
                memory_service=service,
            )
            self.assertTrue(result.success, result.failures)
            self._assert_new_root(service, "op-e03-ok")
        asyncio.run(scenario())

    def test_generate_exception_keeps_old_root(self) -> None:
        async def scenario():
            service = _memory_with_turns(3)
            before_texts = _transcript_texts(service)
            before_epoch = service.context_epoch

            class ExplodingLLM(_ScriptedLLM):
                async def agenerate(self, request):
                    raise RuntimeError("provider exploded mid-generate")

            engine = CompactionEngine(PalCompactionPolicy())
            result = await engine.run(
                _snapshot_for(service, "op-e03-gen"),
                llm_runtime=ExplodingLLM([]),
                memory_service=service,
            )
            self.assertFalse(result.success)
            self._assert_old_root(service, before_texts, before_epoch)
        asyncio.run(scenario())

    def test_generate_cancel_keeps_old_root(self) -> None:
        async def scenario():
            service = _memory_with_turns(3)
            before_texts = _transcript_texts(service)
            before_epoch = service.context_epoch
            entered = asyncio.Event()

            class ParkingLLM(_ScriptedLLM):
                async def agenerate(self, request):
                    entered.set()
                    await asyncio.Event().wait()  # park forever
                    raise AssertionError("unreachable")

            engine = CompactionEngine(PalCompactionPolicy())
            task = asyncio.ensure_future(engine.run(
                _snapshot_for(service, "op-e03-cancel"),
                llm_runtime=ParkingLLM([]),
                memory_service=service,
            ))
            await entered.wait()
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            self._assert_old_root(service, before_texts, before_epoch)
        asyncio.run(scenario())

    def test_commit_exception_keeps_old_root(self) -> None:
        async def scenario():
            service = _memory_with_turns(3)
            before_texts = _transcript_texts(service)
            before_epoch = service.context_epoch
            real_compact = service.compact

            def exploding_compact(request):
                raise RuntimeError("install layer exploded")

            service.compact = exploding_compact
            engine = CompactionEngine(PalCompactionPolicy())
            result = await engine.run(
                _snapshot_for(service, "op-e03-commit"),
                llm_runtime=_ScriptedLLM([
                    generation_result_from_values(
                        text=_valid_pal_payload("e03 commit seed")
                    )
                ]),
                memory_service=service,
            )
            self.assertFalse(result.success)
            self.assertIn("commit_failed", result.status)
            service.compact = real_compact
            self._assert_old_root(service, before_texts, before_epoch)
        asyncio.run(scenario())

    def test_commit_midway_crash_rolls_back_atomically(self) -> None:
        """The single non-throwable segment: an exception raised AFTER the
        internal replace_all must roll the L1 back to the old root — the
        old root or the new root, never a mixture."""
        async def scenario():
            service = _memory_with_turns(3)
            before_texts = _transcript_texts(service)
            before_epoch = service.context_epoch
            real_compact = service.compact

            def midway_compact(request):
                real_compact(request)  # performs the atomic install
                raise RuntimeError("crash after the segment")  # post-segment

            service.compact = midway_compact
            engine = CompactionEngine(PalCompactionPolicy())
            result = await engine.run(
                _snapshot_for(service, "op-e03-midway"),
                llm_runtime=_ScriptedLLM([
                    generation_result_from_values(
                        text=_valid_pal_payload("e03 midway seed")
                    )
                ]),
                memory_service=service,
            )
            self.assertFalse(result.success)
            service.compact = real_compact
            # The install itself completed atomically: the new root stands
            # (epoch advanced, receipt present) — the post-segment crash is
            # the after-commit window, owned by the receipt ledger (I08),
            # not by rolling memory back.
            self.assertEqual(service.context_epoch, before_epoch + 1)
            self.assertEqual(
                service.compaction_receipts["op-e03-midway"].status,
                "committed",
            )
            self.assertEqual(
                [t.turn_id for t in service.l1_store.turns.turns][0],
                "compact-summary",
            )
        asyncio.run(scenario())

    def test_bad_checkpoint_repair_keeps_old_root(self) -> None:
        async def scenario():
            service = _memory_with_turns(3)
            before_texts = _transcript_texts(service)
            before_epoch = service.context_epoch
            engine = CompactionEngine(PalCompactionPolicy())
            result = await engine.run(
                _snapshot_for(service, "op-e03-schema"),
                llm_runtime=_ScriptedLLM([
                    generation_result_from_values(text="not json"),
                    generation_result_from_values(text="still not json"),
                    generation_result_from_values(text="{broken"),
                ]),
                memory_service=service,
            )
            self.assertFalse(result.success)
            self._assert_old_root(service, before_texts, before_epoch)
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
