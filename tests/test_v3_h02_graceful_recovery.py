"""G3/H02+H03 (owner level): graceful-stop persistence and recovery.

H02 · a graceful stop (SIGTERM -> resident checkpoint) now carries the
        two-segment owner state with the turns: the restored process sees
        the COMPLETE old or new root (identical L/R split, incarnation,
        cut identity, left-generation chain), native replay envelopes
        survive so projection associations rebuild, an in-flight compact
        run is abandoned (never resumed or re-run), and old turns are
        never automatically re-executed.  Corrupted owner state fails
        closed BEFORE any install.
H03 · a failed shutdown write is reported honestly: the previous complete
        checkpoint stays readable, no half-written file is left behind,
        and nothing claims success.

Layering: the app-level save/restore chain (publish -> encrypted envelope
-> restore -> consume) is exercised with the inherited resident-checkpoint
harness; engine/owner round-trips use the real MemoryService and port with
a network-side fake only (H01 pattern).
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from pal.core.resident_checkpoint import ResidentCheckpointStore
from pal.llm.ir import (
    LLMMessageIR,
    MessageRole,
    ReplayEnvelope,
    TextPartIR,
    WireShape,
)
from pal.memory import MemoryService
from pal.memory.contracts import L1MessageKind, L1TranscriptMessage
from pal.memory.runtime_state import (
    MEMORY_RUNTIME_STATE_SCHEMA_VERSION,
    MemoryRuntimeStatePort,
)

from tests.test_resident_checkpoint import _build_app


def _seed() -> list[L1TranscriptMessage]:
    return [L1TranscriptMessage(role="assistant", content="SEED0",
                                kind=L1MessageKind.RUNTIME_CONTEXT_SUMMARY)]


def _settled(mark: str) -> list[L1TranscriptMessage]:
    return [
        L1TranscriptMessage(role="user", content=f"{mark} request",
                            kind=L1MessageKind.USER_REQUEST),
        L1TranscriptMessage(role="assistant", content=f"{mark} reply",
                            kind=L1MessageKind.ASSISTANT_REPLY),
    ]


def _service_with_history() -> MemoryService:
    service = MemoryService()
    service.l1_store.append(_seed())
    service.l1_store.append(_settled("s0"))
    service.l1_store.append(_settled("s1"))
    return service


def _root_facts(service: MemoryService) -> dict:
    root = service.history_root
    return {
        "incarnation": root.incarnation,
        "left_revision": root.left_revision,
        "left_generation": root.left_generation,
        "cut_id": root.cut.cut_id,
        "turn_count": root.cut.turn_count,
        "intra_messages": root.cut.intra_messages,
        "left_ids": [t.turn_id for t in root.left_turns()],
        "right_ids": [t.turn_id for t in root.right_turns()],
        "left_text": " ".join(m.text for m in root.left_messages()),
        "right_text": " ".join(m.text for m in root.right_messages()),
    }


class H02GracefulRecoveryTests(unittest.TestCase):
    def test_H02_app_level_old_root_survives_graceful_restart(self):
        """Full app chain: stop-save -> new process reads the complete OLD
        root (promote-only cut) with L/R split, authority chain, and native
        replay envelopes intact; the previously active turn is interrupted,
        never re-run."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = _build_app(root)
            service = source.handle.memory_service
            service.l1_store.append(_settled("closed-0"))
            service.l1_store.append(_settled("closed-1"))
            service.begin_l1_turn(
                "task-R", user_text="RIGHT_SENTINEL graceful stop")
            service.upsert_l1_assistant("task-R", LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(TextPartIR("native wire tail"),),
                message_id="r-native",
                replay=ReplayEnvelope(
                    wire_shape=WireShape.OPENAI_COMPLETION,
                    endpoint_id="endpoint-h02",
                    model_id="model-h02",
                    payload={"container": [{"role": "assistant",
                                            "content": "native wire tail"}]},
                ),
            ))
            # Old root: request-boundary promote only, no compaction.
            service.history_root.promote()
            before = _root_facts(service)

            asyncio.run(source._publish_checkpoint_async())

            restored = _build_app(root)
            asyncio.run(restored._restore_checkpoint_async())
            self.assertEqual(restored.last_checkpoint_status, "restored")
            after = _root_facts(restored.handle.memory_service)
            self.assertEqual(after, before)
            # Native association survives the restart: the replay envelope
            # rides the restored R message byte-comparable.
            restored_message = next(
                m for m in restored.handle.memory_service.history_root.right_messages()
                if m.message_id == "r-native"
            )
            self.assertIsNotNone(restored_message.replay)
            self.assertEqual(restored_message.replay.endpoint_id, "endpoint-h02")
            self.assertEqual(
                dict(restored_message.replay.payload["container"][0]),
                {"role": "assistant", "content": "native wire tail"},
            )
            # Old turns are never re-run: the work tail came back
            # interrupted (app-level marker), not active.
            tail = restored.handle.memory_service.l1_store.turns.get("task-R")
            self.assertEqual(tail.state.value, "interrupted")
            self.assertEqual(tail.metadata["interrupt_reason"],
                             "resident process restart")

    def test_H02_port_level_new_root_round_trip_after_compact(self):
        """New root: after a real engine install, snapshot -> prepare ->
        install reproduces the installed L (summary seed), the verbatim R
        tail, and the bumped left-generation chain (H01 harness pattern)."""
        service = _service_with_history()
        root = service.history_root
        service.begin_l1_turn("task-R", user_text="RIGHT_SENTINEL work")

        from pal.core.compaction import (
            CompactionClockKind,
            CompactionEngine,
            CompactionSnapshot,
        )
        from pal.core.pal_compaction import PalCompactionPolicy
        from pal.llm import generation_result_from_values
        from tests.test_runtime_compaction import _valid_pal_payload

        class _Network:
            async def agenerate(self, request, *a, **kw):
                return generation_result_from_values(
                    text=_valid_pal_payload("H02 new root summary"))

        async def scenario():
            root.promote()
            left_snapshot = service.begin_left_compaction("run-h02", reason="auto")
            snapshot = CompactionSnapshot.capture_left(
                service, left_snapshot,
                target_input_budget=1_000_000, reserved_output_tokens=1024,
                clock_kind=CompactionClockKind.USER_TURN, clock_value=1,
                metadata={"compaction_op_id": "run-h02"},
            )
            engine = CompactionEngine(policy=PalCompactionPolicy(),
                                      max_attempts=1, timeout_seconds=10.0)
            return await engine.run(snapshot, llm_runtime=_Network(),
                                    memory_service=service)

        result = asyncio.run(scenario())
        self.assertTrue(result.success, f"compact failed: {result.failures}")
        self.assertGreaterEqual(root.left_generation, 1)
        before = _root_facts(service)
        self.assertIn("H02 new root summary", before["left_text"])

        payload = dict(MemoryRuntimeStatePort(service).snapshot_state())
        self.assertIsNotNone(payload["history_root"])
        restored_service = MemoryService()
        port = MemoryRuntimeStatePort(restored_service)
        prepared = port.prepare_restore_state(payload)
        port.install_prepared_state(prepared)
        self.assertEqual(_root_facts(restored_service), before)

    def test_H02_live_compact_run_is_abandoned_not_resumed(self):
        """A compact run still live at save time is recorded and abandoned:
        the restored root has a free lane and never re-runs the old work."""
        service = _service_with_history()
        root = service.history_root
        root.promote()
        service.begin_left_compaction("run-live", reason="auto")
        self.assertIsNotNone(root.active_run)

        payload = dict(MemoryRuntimeStatePort(service).snapshot_state())
        abandoned = payload["history_root"]["abandoned_run"]
        self.assertEqual(abandoned["run_id"], "run-live")

        restored_service = MemoryService()
        port = MemoryRuntimeStatePort(restored_service)
        port.install_prepared_state(port.prepare_restore_state(payload))
        restored_root = restored_service.history_root
        self.assertIsNone(restored_root.active_run)
        # The lane is free: a NEW compact run opens immediately, and the
        # abandoned one has no power over it (no resume, no re-run).
        restored_service.begin_l1_turn("task-next", user_text="next work")
        restored_service.upsert_l1_assistant("task-next", LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(TextPartIR("round closed"),), message_id="a-next"))
        restored_service.settle_l1_turn("task-next")
        restored_root.promote()
        restored_service.begin_left_compaction("run-new", reason="auto")
        self.assertEqual(restored_root.active_run.run_id, "run-new")

    def test_H02_corrupt_owner_state_fails_closed_before_install(self):
        service = _service_with_history()
        service.history_root.promote()
        payload = dict(MemoryRuntimeStatePort(service).snapshot_state())

        def variant(**changes):
            bad = dict(payload)
            bad["history_root"] = dict(bad["history_root"], **changes)
            return bad

        fresh = MemoryService()
        cases = [
            variant(unknown_field=1),
            variant(cut={"turn_count": 0, "intra_messages": 0,
                        "revision": 1, "cut_id": "cut-x", "extra": True}),
            variant(incarnation=""),
            variant(left_generation=-1),
            {**payload, "history_root": "not-an-object"},
            # Cut pointing past the restored history.
            variant(cut={"turn_count": 99, "intra_messages": 0,
                         "revision": 1, "cut_id": "cut-x"}),
            # Intra cut splitting messages that never existed.
            variant(cut={"turn_count": 1, "intra_messages": 99,
                         "revision": 1, "cut_id": "cut-x"}),
        ]
        for case in cases:
            with self.subTest(case=str(case.get("history_root"))[:60]):
                port = MemoryRuntimeStatePort(fresh)
                with self.assertRaises(ValueError):
                    port.prepare_restore_state(case)
                # Nothing installed: the service still has its own history.
                self.assertEqual(fresh.l1_store.turns.turns, ())

    def test_H02_v2_era_payload_migrates_explicitly(self):
        """A payload without history_root (pre-two-segment authority) is an
        explicit migration: turns restore, the authority stays lazy/fresh —
        no cut is invented."""
        service = _service_with_history()
        root = service.history_root
        root.promote()
        original_incarnation = root.incarnation
        payload = dict(MemoryRuntimeStatePort(service).snapshot_state())
        self.assertIn("history_root", payload)
        legacy = {k: v for k, v in payload.items() if k != "history_root"}

        restored_service = MemoryService()
        port = MemoryRuntimeStatePort(restored_service)
        port.install_prepared_state(port.prepare_restore_state(legacy))
        # Turns restored; authority fresh and uncut.
        self.assertEqual(
            len(restored_service.l1_store.turns.turns),
            len(service.l1_store.turns.turns),
        )
        fresh_root = restored_service.history_root
        self.assertNotEqual(fresh_root.incarnation, original_incarnation)
        self.assertEqual(fresh_root.cut.turn_count, 0)
        self.assertEqual(MEMORY_RUNTIME_STATE_SCHEMA_VERSION, "3")

    def test_H03_failed_publish_keeps_previous_checkpoint_readable(self):
        """H03: a shutdown write that fails mid-replace leaves the previous
        complete checkpoint readable, cleans its temp file, and reports the
        failure by raising — never a silent success."""
        import os as _os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            source = _build_app(root)
            service = source.handle.memory_service
            service.begin_l1_turn("turn-a", user_text="first checkpoint")
            service.settle_l1_turn("turn-a")
            asyncio.run(source._publish_checkpoint_async())
            store = ResidentCheckpointStore(root)
            first = store.read()
            self.assertEqual(first["sequence"], 1)

            service.begin_l1_turn("turn-b", user_text="second checkpoint")
            service.settle_l1_turn("turn-b")
            real_replace = _os.replace

            def failing_replace(src, dst, *a, **kw):
                if str(dst) == str(store.path):
                    raise OSError("disk went away mid-replace")
                return real_replace(src, dst, *a, **kw)

            with patch("pal.core.resident_checkpoint.os.replace",
                       side_effect=failing_replace):
                with self.assertRaises(OSError):
                    asyncio.run(source._publish_checkpoint_async())

            # The previous complete checkpoint is still readable and
            # authenticated; no half-written file or temp residue.
            still = store.read()
            self.assertEqual(still["sequence"], 1)
            self.assertEqual(
                still["modules"]["memory"]["payload"]["l1_turns"][0]["turn_id"],
                "turn-a",
            )
            leftovers = [
                p.name for p in root.iterdir()
                if p.name.endswith(".tmp") or p.name.startswith(".")
            ]
            self.assertEqual(leftovers, [])
            # The failed write never replaced the old file: turn-b is absent.
            self.assertNotIn("turn-b",
                             str(still["modules"]["memory"]["payload"]))


if __name__ == "__main__":
    unittest.main()
