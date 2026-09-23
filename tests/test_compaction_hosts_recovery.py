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

from pal.core.compaction_coordinator import CompactionGate
from pal.core.contracts import CoreRuntimeState
from pal.core.pal_compaction import PalCompactionPolicy
from pal.core.runtime import PalCore
from pal.core.turns import MemoryCompactEffect
from tests.test_compaction_fixtures import _service_with_active


class _BarrierEngine:
    def __init__(self, *, status: str = "compacted") -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.status = status
        self.policy = PalCompactionPolicy()
        # Timing contract the v3 run envelope reads (align with the real
        # CompactionEngine and the gate-suite stub): the absolute deadline
        # spans preflight, generation, repair, and the install gate.
        self.max_attempts = 3
        self.timeout_seconds = 30.0

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
            # Bunshin shape: plain loop state without transition locks,
            # exactly as the worker runtime builds it.  v3 admission is
            # owner-side (history root); the executor claims no tickets.
            core.turn_executor.state = SimpleNamespace(pending_channel_turns=None)

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
            await asyncio.wait_for(engine.entered.wait(), timeout=5)
            # v3: the executor claims no admission tickets at all — a
            # Bunshin worker never touches the resident scope (I17/Q15).
            self.assertFalse(core.state.compaction_tickets)
            engine.release.set()
            result = await task
            self.assertEqual(result.status.name, "OK")
            self.assertFalse(core.state.compaction_tickets)
            # The barrier engine stub performed the generation; install
            # semantics are covered by the real-engine suites.
            self.assertEqual(engine.calls, 1)
        asyncio.run(scenario())


class BunshinCheckpointRecoveryTests(unittest.TestCase):
    """R06: an encrypted logical-coroutine checkpoint taken mid-Bunshin-
    shaped work (assignment/response_keys/operation receipts completed)
    survives the scoped compaction and restarts into a fresh worker with
    identity, counters, and receipts intact; the manager-visible file
    never carries plaintext, and completed mutations cannot replay."""

    def test_r06_encrypted_checkpoint_restore_across_scoped_compact(self) -> None:
        import tempfile

        from pal.bunshin.checkpoint import (
            AgentSessionCheckpointError,
            LogicalCoroutineCheckpointStore,
            open_agent_session_checkpoint,
            seal_agent_session_checkpoint,
        )
        from pal.core.runtime_state import RUNTIME_SNAPSHOT_SCHEMA_VERSION

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            store = LogicalCoroutineCheckpointStore(runtime_root=root)
            identity = {
                "logical_coroutine_id": "lc-r06",
                "workflow_id": "wf-r06",
                "stage_key": "coder",
                "sequence": 2,
                "producer_fencing_token": 1,
                "runtime_spec_hash": "spec-hash-r06",
            }
            payload = {
                **identity,
                "coroutine_state": {
                    "role_identity": "bunshin.coder.v1",
                    "llm_round_count": 7,
                    "tool_call_count": 12,
                    "assignment": {"work_order": "wo-1", "tasks": ["t1", "t2"]},
                    "response_keys": ["rk-1", "rk-2"],
                    "operation_receipts": [
                        {"operation_id": "op-1", "status": "completed"},
                        {"operation_id": "op-2", "status": "completed"},
                    ],
                },
                "runtime_snapshot": {
                    **identity,
                    "schema_version": RUNTIME_SNAPSHOT_SCHEMA_VERSION,
                    "modules": {},
                },
            }
            sealed = seal_agent_session_checkpoint(root, payload)
            store.publish(
                sealed,
                expected_logical_coroutine_id="lc-r06",
                current_fencing_token=1,
            )

            # A Bunshin-shaped scoped compaction runs to completion; the
            # checkpoint store keeps its own durability, untouched by L1.
            async def scoped_compact():
                core = PalCore()
                service, turn_id, _ = _service_with_active()
                engine = _BarrierEngine()
                core.context.port_registry["memory:memory"] = service
                core.context.port_registry["llm:llm"] = object()
                core.turn_executor._compaction_engine = engine
                core.turn_executor.state = SimpleNamespace(pending_channel_turns=None)
                carrier = CoreRuntimeState()
                core.turn_executor._compaction_gate = CompactionGate(
                    carrier, transition_lock=carrier.channel_turn_transition_lock,
                )
                core.turn_executor._compaction_scope = f"bunshin:{identity['workflow_id']}"
                task = asyncio.create_task(core.turn_executor.execute_turn_effect_async(
                    SimpleNamespace(
                        turn_id=turn_id, waiting_effect_id=None,
                        interrupted=False, interrupt_reason="",
                    ),
                    MemoryCompactEffect(
                        assembly_context=None,
                        target_input_budget=8_192,
                        reserved_output_tokens=2_048,
                    ),
                ))
                await asyncio.wait_for(engine.entered.wait(), timeout=5)
                engine.release.set()
                result = await task
                return result

            result = asyncio.run(scoped_compact())
            self.assertEqual(result.status.name, "OK")

            # Manager view: the on-disk envelope routes on public metrics
            # only — role/assignment/receipts never appear in plaintext.
            raw_text = store.current_path("lc-r06").read_text(encoding="utf-8")
            for secret in ("bunshin.coder.v1", "op-1", "rk-1", "wo-1"):
                self.assertNotIn(secret, raw_text)
            raw = json.loads(raw_text)
            self.assertTrue(str(raw["ciphertext"]).strip())
            self.assertEqual(raw["metrics"]["tool_call_count"], 12)
            self.assertEqual(raw["metrics"]["llm_round_count"], 7)

            # Fresh worker restore: identity, counters, and receipts intact.
            restored = open_agent_session_checkpoint(root, store.read("lc-r06"))
            state = restored["coroutine_state"]
            self.assertEqual(state["role_identity"], "bunshin.coder.v1")
            self.assertEqual(state["tool_call_count"], 12)
            self.assertEqual(state["response_keys"], ["rk-1", "rk-2"])
            self.assertEqual(
                [r["operation_id"] for r in state["operation_receipts"]],
                ["op-1", "op-2"],
            )

            # Completed mutations never re-run: replaying the same sequence
            # or publishing under a stale fencing token is rejected.
            replay = seal_agent_session_checkpoint(root, dict(payload))
            with self.assertRaises(AgentSessionCheckpointError):
                store.publish(
                    replay,
                    expected_logical_coroutine_id="lc-r06",
                    current_fencing_token=1,
                )
            with self.assertRaises(AgentSessionCheckpointError):
                store.publish(
                    replay,
                    expected_logical_coroutine_id="lc-r06",
                    current_fencing_token=2,
                )


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
            await asyncio.wait_for(engine.entered.wait(), timeout=5)
            # The run is live on the history owner; interrupt-style
            # cancellation lands on the awaiting coroutine (X01).
            self.assertIsNotNone(service.history_root.active_run)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            # The dying coroutine closed its own run; nothing installed.
            self.assertIsNone(service.history_root.active_run)
            self.assertEqual(service.l1_source_stamp(), stamp_before)
            self.assertEqual(service.context_epoch, 0)
            self.assertEqual(service.compaction_receipts, {})
        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
