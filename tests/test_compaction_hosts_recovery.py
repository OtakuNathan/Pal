"""P4 host/recovery acceptance tests.

Covers: Bunshin-shaped scoped gates with lock-free loop state (I17/Q15
lite), real-file ingress staging + memory runtime-state restore (R01/R02,
port-level), receipt-driven redelivery suppression (R03 API level), and
cancellation mid-generation on the auto path (X01/E03 subset).

Honest boundaries (recorded for acceptance_status):
- Full E03 barrier matrix and E04 Manager-proxy lanes are NOT run here;
  the covered barrier points are claim/generate/commit/cancel/replay.
- The receipt-vs-checkpoint crash window (receipt written, L1 append lost
  because no checkpoint covered it) remains an explicitly open P4 gap:
  receipts suppress redelivery, so that window needs the P4 outbox work.
"""
from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.core.compaction import CompactionEngine
from pal.core.compaction_coordinator import CompactionGate
from pal.core.contracts import CoreRuntimeState
from pal.core.ingress_staging import (
    IngressStagingError,
    IngressStagingStore,
    StagedIngressRecord,
)
from pal.core.pal_compaction import PalCompactionPolicy
from pal.core.runtime import PalCore
from pal.core.turns import MemoryCompactEffect
from pal.llm import generation_result_from_values
from pal.memory import MemoryService
from pal.memory.runtime_state import MemoryRuntimeStatePort
from tests.test_full_compaction_source import _service_with_active
from tests.test_runtime_compaction import (
    _ScriptedLLM,
    _memory_with_turns,
    _valid_pal_payload,
)


class _BarrierEngine:
    def __init__(self, *, status: str = "compacted") -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.status = status
        self.policy = PalCompactionPolicy()

    async def run(self, snapshot, *, llm_runtime=None, memory_service=None,
                  after_commit=None, replay_guard=None, commit_guard=None):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        memory_result = SimpleNamespace(
            summary="host summary", projected_entries=[],
            metadata={"projected_entry_count": 0, "compact_summary_count": 1,
                      "retired_count": 0},
        )
        from pal.core.compaction import CompactionRunResult
        return CompactionRunResult(
            status=self.status, attempts=self.calls,
            memory_result=memory_result if self.status == "compacted" else None,
        )


class BunshinScopeGateTests(unittest.TestCase):
    def test_bunshin_shaped_gate_is_scoped_and_lock_free(self) -> None:
        """I17/Q15: a Bunshin executor (no resident lock fields on its loop
        state) claims under its own carrier; the resident scope is untouched."""
        async def scenario():
            core = PalCore()
            service, turn_id, _ = _service_with_active()
            engine = _BarrierEngine()
            core.context.port_registry["memory:memory"] = service
            core.context.port_registry["llm:llm"] = object()
            core.turn_executor._compaction_engine = engine
            # Bunshin shape: plain loop state without transition locks and a
            # dedicated gate carrier, exactly as the runner attaches them.
            core.turn_executor.state = SimpleNamespace(pending_channel_turns=None)
            carrier = CoreRuntimeState()
            core.turn_executor._compaction_gate = CompactionGate(
                carrier, transition_lock=carrier.channel_turn_transition_lock
            )
            core.turn_executor._compaction_scope = "bunshin:workorder-1"

            effect = MemoryCompactEffect(
                assembly_context=None,
                target_input_budget=8_192,
                reserved_output_tokens=2_048,
            )
            continuation = SimpleNamespace(
                turn_id=turn_id, waiting_effect_id=None,
                interrupted=False, interrupt_reason="",
            )
            task = asyncio.create_task(
                core.turn_executor.execute_turn_effect_async(continuation, effect)
            )
            await engine.entered.wait()
            self.assertTrue(carrier.compaction_tickets["bunshin:workorder-1"].phase.value == "generating")
            # The resident runtime's scope is untouched: no cross-scope leak.
            self.assertNotIn("pal:resident", core.state.compaction_tickets)
            engine.release.set()
            result = await task
            self.assertEqual(result.status.name, "OK")
            self.assertFalse(carrier.compaction_tickets)
            # The barrier engine stub performed the generation; install
            # semantics are covered by the real-engine suites.
            self.assertEqual(engine.calls, 1)
        asyncio.run(scenario())


class RealFileRecoveryTests(unittest.TestCase):
    def test_r01_pending_and_old_root_survive_restore(self) -> None:
        service, turn_id, _ = _service_with_active()
        with __import__("tempfile").TemporaryDirectory() as tmp:
            staging = IngressStagingStore(Path(tmp) / "staging.json")
            from tests.test_compaction_gate import _envelope
            envelope = _envelope("m-r01", "SURVIVING_CORRECTION")
            staging.enqueue(StagedIngressRecord.from_channel_envelope(envelope, scope="s"))

            # Crash before install: the durable artifacts are the staging
            # file and the last memory snapshot (old root).
            port = MemoryRuntimeStatePort(service)
            payload = dict(port.snapshot_state())
            restored = MemoryService()
            restored_port = MemoryRuntimeStatePort(restored)
            restored_port.install_prepared_state(
                restored_port.prepare_restore_state(payload)
            )
            reloaded = IngressStagingStore(Path(tmp) / "staging.json")
            records = reloaded.pending_records()
            self.assertEqual([record.event_id for record in records], ["m-r01"])
            self.assertIn("SURVIVING_CORRECTION", json.dumps(records[0].payload))
            # Old root intact: epoch unchanged, active turn kept, and the
            # only summary is the pre-existing SEED0 (no half install).
            self.assertEqual(restored.context_epoch, 0)
            self.assertEqual(restored.compaction_receipts, {})
            self.assertIsNotNone(restored.active_l1_turn(turn_id))
            from pal.memory.contracts import L1MessageKind
            kinds = [m.kind for t in restored.l1_store.items for m in t]
            self.assertEqual(kinds.count(L1MessageKind.RUNTIME_CONTEXT_SUMMARY), 1)

    def test_r02_committed_root_restores_without_recompaction(self) -> None:
        service, turn_id, _ = _service_with_active()
        engine = CompactionEngine(PalCompactionPolicy())
        snapshot = __import__("pal.core.compaction", fromlist=["CompactionSnapshot"]).CompactionSnapshot.capture(
            service,
            target_input_budget=8_192,
            reserved_output_tokens=2_048,
            clock_kind=__import__("pal.core.compaction", fromlist=["CompactionClockKind"]).CompactionClockKind.USER_TURN,
            clock_value=1,
            metadata={"compaction_op_id": "op-r02"},
            source_epoch=service.context_epoch,
        )
        result = asyncio.run(engine.run(
            snapshot,
            llm_runtime=_ScriptedLLM([
                generation_result_from_values(text=_valid_pal_payload("r02 seed"))
            ]),
            memory_service=service,
        ))
        self.assertTrue(result.success, result.failures)

        payload = dict(MemoryRuntimeStatePort(service).snapshot_state())
        restored = MemoryService()
        restored_port = MemoryRuntimeStatePort(restored)
        restored_port.install_prepared_state(
            restored_port.prepare_restore_state(payload)
        )
        self.assertEqual(restored.context_epoch, 1)
        receipt = restored.compaction_receipts["op-r02"]
        self.assertEqual(receipt.status, "committed")
        self.assertEqual(
            [turn.turn_id for turn in restored.l1_store.turns.turns],
            ["compact-summary", turn_id],
        )
        # Redelivery of the same op replays idempotently: no second epoch,
        # no second receipt, no model call.
        entry = PalCompactionPolicy().validate_checkpoint(
            _valid_pal_payload("r02 seed"), snapshot
        )
        from pal.memory.contracts import MemoryCompactRequest
        replayed = restored.compact(MemoryCompactRequest(
            target_input_budget=8_192, reserved_output_tokens=2_048,
            summary_entry=entry, op_id="op-r02",
            source_stamp=snapshot.source_stamp,
            active_turn_id=snapshot.active_turn_ids[0],
            expected_epoch=snapshot.source_epoch,
        ))
        self.assertTrue(replayed.metadata.get("compaction_replayed"))
        self.assertEqual(restored.context_epoch, 1)
        self.assertEqual(len(restored.compaction_receipts), 1)

    def test_r03_receipt_suppresses_redelivery_after_removal(self) -> None:
        with __import__("tempfile").TemporaryDirectory() as tmp:
            store = IngressStagingStore(Path(tmp) / "staging.json")
            record = StagedIngressRecord.from_channel_envelope(
                __import__("tests.test_compaction_gate", fromlist=["_envelope"])._envelope(
                    "m-r03", "accepted once"
                ),
                scope="s",
            )
            store.enqueue(record)
            store.record_receipt("m-r03", turn_id="t1")
            store.remove("m-r03")
            # Channel redelivery after the queue removal: the receipt is the
            # dedup authority, not the (compacted-away) transcript text.
            try:
                store.enqueue(record)
                raise AssertionError("receipt must suppress re-queueing")
            except IngressStagingError:
                pass
            self.assertEqual(store.pending_records(), ())
            # NOTE(P4 gap): a receipt written before the L1 append reached a
            # checkpoint would also suppress the channel redelivery that is
            # the only way to recover that append. Closing that window needs
            # receipt/checkpoint sequencing (outbox), tracked for P4+.


class ShutdownTwoSidedRecoveryTests(unittest.TestCase):
    """X09: shutdown once before the commit and once after it; each reboot
    restores its own legal root, pending input survives, and no old mutation
    is auto-resent."""

    def test_x09_both_sides_recover_legal_roots_pending_survives(self) -> None:
        import tempfile

        from pal.memory.contracts import MemoryCompactRequest
        from pal.memory.runtime_state import MemoryRuntimeStatePort
        from tests.test_compaction_gate import _envelope

        with tempfile.TemporaryDirectory() as tmp:
            service, turn_id, _ = _service_with_active()
            staging = IngressStagingStore(Path(tmp) / "staging.json")
            staging.enqueue(StagedIngressRecord.from_channel_envelope(
                _envelope("m-x09", "PENDING_INPUT_X09"), scope="s"
            ))

            # Side A: shutdown before the commit — old root must return.
            port = MemoryRuntimeStatePort(service)
            payload_a = dict(port.snapshot_state())
            restored_a = MemoryService()
            restored_a_port = MemoryRuntimeStatePort(restored_a)
            restored_a_port.install_prepared_state(
                restored_a_port.prepare_restore_state(payload_a)
            )
            self.assertEqual(restored_a.context_epoch, 0)
            self.assertEqual(restored_a.compaction_receipts, {})
            records_a = IngressStagingStore(Path(tmp) / "staging.json").pending_records()
            self.assertEqual([r.event_id for r in records_a], ["m-x09"])
            self.assertIn("PENDING_INPUT_X09", json.dumps(records_a[0].payload))

            # Side B: a real commit, then shutdown — new root must return.
            engine = CompactionEngine(PalCompactionPolicy())
            from pal.core.compaction import CompactionClockKind, CompactionSnapshot
            snapshot = CompactionSnapshot.capture(
                service,
                target_input_budget=8_192,
                reserved_output_tokens=2_048,
                clock_kind=CompactionClockKind.USER_TURN,
                clock_value=1,
                metadata={"compaction_op_id": "op-x09"},
                source_epoch=service.context_epoch,
            )
            result = asyncio.run(engine.run(
                snapshot,
                llm_runtime=_ScriptedLLM([
                    generation_result_from_values(text=_valid_pal_payload("x09 seed"))
                ]),
                memory_service=service,
            ))
            self.assertTrue(result.success, result.failures)

            payload_b = dict(MemoryRuntimeStatePort(service).snapshot_state())
            restored_b = MemoryService()
            restored_b_port = MemoryRuntimeStatePort(restored_b)
            restored_b_port.install_prepared_state(
                restored_b_port.prepare_restore_state(payload_b)
            )
            self.assertEqual(restored_b.context_epoch, 1)
            self.assertEqual(restored_b.compaction_receipts["op-x09"].status, "committed")
            self.assertEqual(
                [turn.turn_id for turn in restored_b.l1_store.turns.turns],
                ["compact-summary", turn_id],
            )
            # Pending input still reachable after the second reboot.
            records_b = IngressStagingStore(Path(tmp) / "staging.json").pending_records()
            self.assertEqual([r.event_id for r in records_b], ["m-x09"])
            # No auto-resend of the old mutation: exactly one receipt and
            # the replay stays idempotent without a second install.
            self.assertEqual(len(restored_b.compaction_receipts), 1)
            replay_entry = PalCompactionPolicy().validate_checkpoint(
                _valid_pal_payload("x09 seed"), snapshot
            )
            replayed = restored_b.compact(MemoryCompactRequest(
                target_input_budget=8_192, reserved_output_tokens=2_048,
                summary_entry=replay_entry, op_id="op-x09",
                source_stamp=snapshot.source_stamp,
                active_turn_id=snapshot.active_turn_ids[0],
                expected_epoch=snapshot.source_epoch,
            ))
            self.assertTrue(replayed.metadata.get("compaction_replayed"))
            self.assertEqual(restored_b.context_epoch, 1)


class CancellationTests(unittest.TestCase):
    def test_x01_cancel_during_generate_releases_and_installs_nothing(self) -> None:
        async def scenario():
            core = PalCore()
            service, turn_id, _ = _service_with_active()
            engine = _BarrierEngine(status="compacted")
            core.context.port_registry["memory:memory"] = service
            core.context.port_registry["llm:llm"] = object()
            core.turn_executor._compaction_engine = engine
            stamp_before = service.l1_source_stamp()
            effect = MemoryCompactEffect(
                assembly_context=None,
                target_input_budget=8_192,
                reserved_output_tokens=2_048,
            )
            continuation = SimpleNamespace(
                turn_id=turn_id, waiting_effect_id=None,
                interrupted=False, interrupt_reason="",
            )
            task = asyncio.create_task(
                core.turn_executor.execute_turn_effect_async(continuation, effect)
            )
            await engine.entered.wait()
            # The auto ticket is live; interrupt-style cancellation lands on
            # the awaiting coroutine (X01).
            ticket = core.state.compaction_tickets.get("pal:resident")
            self.assertIsNotNone(ticket)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # The dying coroutine released its own ticket; nothing installed.
            self.assertFalse(core.state.compaction_tickets)
            self.assertEqual(service.l1_source_stamp(), stamp_before)
            self.assertEqual(service.context_epoch, 0)
            self.assertEqual(service.compaction_receipts, {})
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
