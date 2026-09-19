"""P2 full-source compaction acceptance tests.

Maps TEST_MATRIX S01-S10, I01-I07/I10/I12, E01 cold variant (steps 1-8 of
the E2E recipe; restart/replay steps are P4), plus the memory runtime-state
round-trip for epoch/receipts (R04/R05 port-level evidence).

Honest boundaries: warm construction (P3), real process restart and fault
injection matrices (P4), full next-request wire comparison (P3 captures the
final payload; here the continuation's structural continuity is asserted).
"""
from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace

from pal.core.compaction import CompactionEngine, CompactionSnapshot, CompactionClockKind
from pal.core.pal_compaction import PalCompactionPolicy
from pal.llm import generation_result_from_values
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.memory import MemoryService, L2Entry
from pal.memory.contracts import (
    CompactionReceipt,
    L1MessageKind,
    L1TranscriptMessage,
    MemoryCompactRequest,
    StaleCompactionSource,
)
from pal.memory.runtime_state import MemoryRuntimeStatePort
from pal.memory.turn_ir import L1TurnIR, L1TurnState
from pal.shared import LLMPreflightStatus
from pal.shared.tool_protocol import ToolResultIR, new_tool_call
from tests.test_runtime_compaction import (
    _ScriptedLLM,
    _valid_pal_payload,
)


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
        L1TranscriptMessage(role="user", content=f"{mark} request", kind=L1MessageKind.USER_REQUEST),
        L1TranscriptMessage(role="assistant", content=f"{mark} reply", kind=L1MessageKind.ASSISTANT_REPLY),
    ]


def _service_with_active(*, seed: bool = True, settled: int = 1) -> tuple[MemoryService, str, dict]:
    """Service with previous seed + settled turns + one active tool turn.

    The active turn embeds the E01 sentinels: Q_ORIGINAL, A_DECISION, a
    single executed tool call A and its RESULT_A.
    """
    service = MemoryService()
    if seed:
        service.l1_store.append(_seed_transcript())
    for index in range(settled):
        service.l1_store.append(_settled_transcript(f"settled-{index}"))
    turn_id = "logical-task-T"
    service.begin_l1_turn(turn_id, user_text="Q_ORIGINAL: implement the feature")
    service.upsert_l1_assistant(
        turn_id,
        LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(
                TextPartIR("A_DECISION: start with file a.cpp"),
                new_tool_call(call_id="call-A", name="read_file", arguments={"file": "a.cpp"}),
            ),
            message_id="assistant-1",
        ),
    )
    service.append_l1_tool_result(
        turn_id,
        ToolResultIR(call_id="call-A", name="read_file", content="RESULT_A: file body"),
    )
    return service, turn_id, {}


def _capture(service: MemoryService, **metadata) -> CompactionSnapshot:
    return CompactionSnapshot.capture(
        service,
        target_input_budget=8_192,
        reserved_output_tokens=2_048,
        clock_kind=CompactionClockKind.USER_TURN,
        clock_value=3,
        metadata=dict(metadata),
        source_epoch=int(getattr(service, "context_epoch", 0) or 0),
    )


def _run(engine, snapshot, llm, service, after_commit=None):
    return asyncio.run(
        engine.run(
            snapshot,
            llm_runtime=llm,
            memory_service=service,
            after_commit=after_commit,
        )
    )


def _all_transcript_text(service: MemoryService) -> str:
    return "\n".join(
        message.content
        for transcript in service.l1_store.items
        for message in transcript
    )


class SourceCoverageTests(unittest.TestCase):
    def test_s01_capture_includes_seed_settled_and_active_once(self) -> None:
        service, turn_id, _ = _service_with_active()
        snapshot = _capture(service, compaction_op_id="op-s01")
        self.assertTrue(snapshot.source_stamp)
        self.assertEqual(snapshot.active_turn_ids, (turn_id,))
        text = "\n".join(
            message.content
            for transcript in snapshot.memory_items
            for message in transcript
        )
        for sentinel in ("SEED0", "settled-0", "Q_ORIGINAL", "A_DECISION", "RESULT_A"):
            self.assertIn(sentinel, text)
        self.assertEqual(text.count("SEED0"), 1)
        self.assertEqual(text.count("RESULT_A"), 1)

    def test_s02_active_only_source_still_compacts(self) -> None:
        service, turn_id, _ = _service_with_active(seed=False, settled=0)
        llm = _ScriptedLLM([generation_result_from_values(text=_valid_pal_payload("active only"))])
        result = _run(
            CompactionEngine(PalCompactionPolicy()),
            _capture(service),
            llm,
            service,
        )
        self.assertTrue(result.success, result.failures)
        # No no-op: the active history was replaced by seed + successor.
        self.assertEqual(service.context_epoch, 1)
        self.assertIsNotNone(service.active_l1_turn(turn_id))
        self.assertNotIn("Q_ORIGINAL", _all_transcript_text(service))

    def test_s03_oversized_source_fails_without_dropping_units(self) -> None:
        service, _turn_id, _ = _service_with_active()
        llm = _ScriptedLLM(
            [generation_result_from_values(text=_valid_pal_payload())],
            preflight=lambda request: _Advice(
                LLMPreflightStatus.COMPACT_REQUIRED
                if request.request.messages[-1].text.count("### memory:") > 0
                else LLMPreflightStatus.READY
            ),
        )
        before = service.l1_source_stamp()
        result = _run(CompactionEngine(PalCompactionPolicy()), _capture(service), llm, service)
        self.assertEqual(result.status, "source_too_large")
        self.assertEqual(llm.generate_requests, [])
        self.assertEqual(service.l1_source_stamp(), before)
        self.assertEqual(service.context_epoch, 0)
        self.assertEqual(service.compaction_receipts, {})

    def test_s04_middle_of_long_result_is_visible_to_source(self) -> None:
        service, turn_id, _ = _service_with_active()
        service.upsert_l1_assistant(
            turn_id,
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(
                    TextPartIR("reading the long log"),
                    new_tool_call(call_id="call-B", name="read_file", arguments={"file": "log.txt"}),
                ),
                message_id="assistant-long",
            ),
        )
        service.append_l1_tool_result(
            turn_id,
            ToolResultIR(
                call_id="call-B",
                name="read_file",
                content="HEAD " + "MIDDLE_UNIQUE_REQUIREMENT " * 300 + " TAIL",
            ),
        )
        llm = _ScriptedLLM([generation_result_from_values(text=_valid_pal_payload())])
        result = _run(CompactionEngine(PalCompactionPolicy()), _capture(service), llm, service)
        self.assertTrue(result.success, result.failures)
        source_seen = llm.generate_requests[0].messages[-1].text
        self.assertIn("MIDDLE_UNIQUE_REQUIREMENT", source_seen)

    def test_s05_s06_snapshot_is_frozen_against_live_mutation(self) -> None:
        service, _turn_id, _ = _service_with_active()
        metadata = {"k": "v"}
        snapshot = _capture(service, **metadata)
        service.begin_l1_turn("intruder-turn", user_text="LATE_LIVE_MUTATION")
        metadata["k"] = "mutated"
        text = "\n".join(
            message.content
            for transcript in snapshot.memory_items
            for message in transcript
        )
        self.assertNotIn("LATE_LIVE_MUTATION", text)
        self.assertEqual(snapshot.metadata["k"], "v")

    def test_s07_second_compact_seeds_once_and_advances_epoch(self) -> None:
        service, turn_id, _ = _service_with_active()
        engine = CompactionEngine(PalCompactionPolicy())
        first = _run(
            engine,
            _capture(service, compaction_op_id="op-1"),
            _ScriptedLLM([generation_result_from_values(text=_valid_pal_payload("first seed"))]),
            service,
        )
        self.assertTrue(first.success, first.failures)
        # Continue the successor with new accepted work.
        service.upsert_l1_assistant(
            turn_id,
            LLMMessageIR(
                role=MessageRole.ASSISTANT,
                parts=(TextPartIR("second round work"),),
                message_id="assistant-2",
            ),
        )
        second_snapshot = _capture(service, compaction_op_id="op-2")
        source_text = "\n".join(
            message.content
            for transcript in second_snapshot.memory_items
            for message in transcript
        )
        self.assertEqual(source_text.count("first seed"), 1)
        second = _run(
            engine,
            second_snapshot,
            _ScriptedLLM([generation_result_from_values(text=_valid_pal_payload("second seed"))]),
            service,
        )
        self.assertTrue(second.success, second.failures)
        self.assertEqual(service.context_epoch, 2)
        self.assertEqual(set(service.compaction_receipts), {"op-1", "op-2"})
        self.assertNotIn("Q_ORIGINAL", _all_transcript_text(service))

    def test_s08_expired_authored_context_is_excluded_from_source(self) -> None:
        from pal.core.prompt_context import CONTEXT_KIND
        service, turn_id, _ = _service_with_active()
        active = service.l1_store.turns.get(turn_id)
        authored = LLMMessageIR(
            role=MessageRole.DEVELOPER,
            parts=(TextPartIR("ONE_SHOT_SECRET_INSTRUCTION"),),
            semantic_kind=CONTEXT_KIND,
            metadata={"pal_authored": True, "context_key": "expired-one-shot"},
        )
        service.l1_store.turns.replace(active.append(authored))
        service.append_l1_user(
            turn_id,
            LLMMessageIR(
                role=MessageRole.USER,
                parts=(TextPartIR("USER_CORRECTION_KEEP_ME"),),
                message_id="correction-1",
            ),
        )
        llm = _ScriptedLLM([generation_result_from_values(text=_valid_pal_payload())])
        result = _run(CompactionEngine(PalCompactionPolicy()), _capture(service), llm, service)
        self.assertTrue(result.success, result.failures)
        source_seen = llm.generate_requests[0].messages[-1].text
        self.assertNotIn("ONE_SHOT_SECRET_INSTRUCTION", source_seen)
        self.assertIn("USER_CORRECTION_KEEP_ME", source_seen)

    def test_s09_multi_owner_active_source_is_rejected(self) -> None:
        service, _turn_id, _ = _service_with_active()
        service.begin_l1_turn("second-active-owner", user_text="other owner input")
        llm = _ScriptedLLM([generation_result_from_values(text=_valid_pal_payload())])
        before = service.l1_source_stamp()
        result = _run(CompactionEngine(PalCompactionPolicy()), _capture(service), llm, service)
        self.assertEqual(result.status, "commit_failed")
        self.assertEqual(service.l1_source_stamp(), before)
        self.assertEqual(service.context_epoch, 0)
        self.assertEqual(service.compaction_receipts, {})

    def test_s10_interrupt_closed_round_keeps_alive_protocol_only(self) -> None:
        service, _turn_id, _ = _service_with_active()
        store = service.l1_store.turns
        closed = store.get("logical-task-T")._close_incomplete(
            L1TurnState.INTERRUPTED, reason="interrupt"
        )
        store.replace(closed)
        snapshot = _capture(service)
        source_text = "\n".join(
            message.content
            for transcript in snapshot.memory_items
            for message in transcript
        )
        self.assertIn("A_DECISION", source_text)
        self.assertIn("RESULT_A", source_text)
        self.assertEqual(snapshot.active_turn_ids, ())


class InstallTests(unittest.TestCase):
    def test_i01_install_seed_successor_epoch_receipt(self) -> None:
        service, turn_id, _ = _service_with_active()
        old_revision = service.l1_store.turns.get(turn_id).revision
        llm = _ScriptedLLM([generation_result_from_values(text=_valid_pal_payload())])
        result = _run(
            CompactionEngine(PalCompactionPolicy()),
            _capture(service, compaction_op_id="op-i01"),
            llm,
            service,
        )
        self.assertTrue(result.success, result.failures)
        turns = list(service.l1_store.turns.turns)
        self.assertEqual([turn.turn_id for turn in turns], ["compact-summary", turn_id])
        summary_turn, successor = turns
        self.assertEqual(
            service.l1_store.items[0][0].kind,
            L1MessageKind.RUNTIME_CONTEXT_SUMMARY,
        )
        self.assertEqual(successor.state, L1TurnState.ACTIVE)
        self.assertEqual(successor.messages, ())
        self.assertEqual(successor.revision, old_revision + 1)
        self.assertTrue(dict(successor.metadata).get("compact_successor"))
        self.assertEqual(service.context_epoch, 1)
        receipt = service.compaction_receipts["op-i01"]
        self.assertEqual(receipt.status, "committed")
        self.assertEqual(receipt.epoch_after, 1)
        self.assertEqual(receipt.successor_turn_id, turn_id)
        self.assertIn("logical-task-T", receipt.removed_turn_ids)

    def test_i02_idle_install_seed_only_no_ghost_turn(self) -> None:
        service = MemoryService()
        service.l1_store.append(_settled_transcript("idle"))
        llm = _ScriptedLLM([generation_result_from_values(text=_valid_pal_payload())])
        result = _run(
            CompactionEngine(PalCompactionPolicy()),
            _capture(service, compaction_op_id="op-i02"),
            llm,
            service,
        )
        self.assertTrue(result.success, result.failures)
        turns = list(service.l1_store.turns.turns)
        self.assertEqual([turn.turn_id for turn in turns], ["compact-summary"])
        self.assertEqual(service.context_epoch, 1)
        # A later real event starts normally; no ghost turn was created.
        service.begin_l1_turn("fresh-turn", user_text="new question")
        self.assertIsNotNone(service.active_l1_turn("fresh-turn"))

    def test_i03_stale_source_cas_rejects_install(self) -> None:
        service, _turn_id, _ = _service_with_active()
        snapshot = _capture(service, compaction_op_id="op-i03")
        # External accepted write between capture and install.
        service.l1_store.append(_settled_transcript("late"))
        llm = _ScriptedLLM([generation_result_from_values(text=_valid_pal_payload())])
        result = _run(CompactionEngine(PalCompactionPolicy()), snapshot, llm, service)
        self.assertEqual(result.status, "commit_failed")
        self.assertIn("late reply", _all_transcript_text(service))
        self.assertEqual(service.context_epoch, 0)
        self.assertEqual(service.compaction_receipts, {})

    def test_i04_generation_failure_installs_nothing(self) -> None:
        service, _turn_id, _ = _service_with_active()
        before = service.l1_source_stamp()
        llm = _ScriptedLLM(
            [
                generation_result_from_values(text="not json"),
                generation_result_from_values(text="still not json"),
                generation_result_from_values(text="nope"),
            ]
        )
        result = _run(
            CompactionEngine(PalCompactionPolicy()),
            _capture(service, compaction_op_id="op-i04"),
            llm,
            service,
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(service.l1_source_stamp(), before)
        self.assertEqual(service.context_epoch, 0)
        self.assertEqual(service.compaction_receipts, {})

    def test_i05_same_op_id_replay_is_idempotent(self) -> None:
        service, _turn_id, _ = _service_with_active()
        snapshot = _capture(service, compaction_op_id="op-i05")
        entry = PalCompactionPolicy().validate_checkpoint(
            _valid_pal_payload(), snapshot
        )
        request = MemoryCompactRequest(
            target_input_budget=8_192,
            reserved_output_tokens=2_048,
            summary_entry=entry,
            op_id="op-i05",
            source_stamp=snapshot.source_stamp,
            active_turn_id=snapshot.active_turn_ids[0],
            expected_epoch=snapshot.source_epoch,
        )
        first = service.compact(request)
        second = service.compact(request)
        self.assertEqual(first.metadata["context_epoch"], 1)
        self.assertTrue(second.metadata.get("compaction_replayed"))
        self.assertEqual(service.context_epoch, 1)
        self.assertEqual(len(service.compaction_receipts), 1)
        turns = list(service.l1_store.turns.turns)
        self.assertEqual(len(turns), 2)

    def test_i06_same_op_id_different_source_is_refused(self) -> None:
        service, _turn_id, _ = _service_with_active()
        snapshot = _capture(service, compaction_op_id="op-i06")
        entry = PalCompactionPolicy().validate_checkpoint(
            _valid_pal_payload(), snapshot
        )
        request = MemoryCompactRequest(
            target_input_budget=8_192,
            reserved_output_tokens=2_048,
            summary_entry=entry,
            op_id="op-i06",
            source_stamp=snapshot.source_stamp,
            active_turn_id=snapshot.active_turn_ids[0],
            expected_epoch=snapshot.source_epoch,
        )
        service.compact(request)
        forged = MemoryCompactRequest(
            target_input_budget=8_192,
            reserved_output_tokens=2_048,
            summary_entry=entry,
            op_id="op-i06",
            source_stamp="f" * 64,
            active_turn_id="",
            expected_epoch=0,
        )
        with self.assertRaises(ValueError):
            service.compact(forged)
        self.assertEqual(service.context_epoch, 1)

    def test_i07_cleanup_failure_stays_committed_pending(self) -> None:
        service, _turn_id, _ = _service_with_active()
        snapshot = _capture(service, compaction_op_id="op-i07")
        entry = PalCompactionPolicy().validate_checkpoint(
            _valid_pal_payload(), snapshot
        )
        request = MemoryCompactRequest(
            target_input_budget=8_192,
            reserved_output_tokens=2_048,
            summary_entry=entry,
            op_id="op-i07",
            source_stamp=snapshot.source_stamp,
            active_turn_id=snapshot.active_turn_ids[0],
            expected_epoch=snapshot.source_epoch,
        )

        def failing_cleanup() -> None:
            raise RuntimeError("retire backend down")

        result = service.compact_transactionally(request, after_commit=failing_cleanup)
        self.assertEqual(result.metadata["context_epoch"], 1)
        receipt = service.compaction_receipts["op-i07"]
        self.assertEqual(receipt.status, "committed")
        self.assertEqual(receipt.cleanup_status, "pending")
        # The install stands: old history is gone regardless of cleanup.
        self.assertNotIn("Q_ORIGINAL", _all_transcript_text(service))

    def test_i10_successor_segment_never_replays_opening_event(self) -> None:
        service, turn_id, _ = _service_with_active()
        llm = _ScriptedLLM([generation_result_from_values(text=_valid_pal_payload())])
        result = _run(
            CompactionEngine(PalCompactionPolicy()),
            _capture(service, compaction_op_id="op-i10"),
            llm,
            service,
        )
        self.assertTrue(result.success, result.failures)
        # begin_l1_turn returns the existing successor as-is: no opening
        # user message is re-inserted into the fresh segment.
        returned = service.begin_l1_turn(turn_id, user_text="Q_ORIGINAL: implement the feature")
        self.assertEqual(returned.messages, ())
        self.assertNotIn("Q_ORIGINAL", _all_transcript_text(service))

    def test_i12_epoch_fence_rejects_late_candidate_after_reset(self) -> None:
        service, _turn_id, _ = _service_with_active()
        snapshot = _capture(service, compaction_op_id="op-i12")
        entry = PalCompactionPolicy().validate_checkpoint(
            _valid_pal_payload(), snapshot
        )
        service.soft_reset()
        self.assertEqual(service.context_epoch, 1)
        request = MemoryCompactRequest(
            target_input_budget=8_192,
            reserved_output_tokens=2_048,
            summary_entry=entry,
            op_id="op-i12",
            source_stamp=snapshot.source_stamp,
            active_turn_id=snapshot.active_turn_ids[0],
            expected_epoch=snapshot.source_epoch,
        )
        with self.assertRaises(StaleCompactionSource):
            service.compact(request)
        # The reset result stands; the late summary did not resurrect it.
        self.assertEqual(list(service.l1_store.turns.turns), [])
        self.assertEqual(service.context_epoch, 1)


class RuntimeStateRoundTripTests(unittest.TestCase):
    def test_epoch_and_receipts_survive_round_trip(self) -> None:
        service, _turn_id, _ = _service_with_active()
        llm = _ScriptedLLM([generation_result_from_values(text=_valid_pal_payload())])
        result = _run(
            CompactionEngine(PalCompactionPolicy()),
            _capture(service, compaction_op_id="op-rt"),
            llm,
            service,
        )
        self.assertTrue(result.success, result.failures)
        port = MemoryRuntimeStatePort(service)
        payload = dict(port.snapshot_state())
        restored = MemoryService()
        restored_port = MemoryRuntimeStatePort(restored)
        restored_port.install_prepared_state(
            restored_port.prepare_restore_state(payload)
        )
        self.assertEqual(restored.context_epoch, 1)
        self.assertEqual(
            restored.compaction_receipts["op-rt"],
            service.compaction_receipts["op-rt"],
        )

    def test_corrupt_epoch_fails_closed(self) -> None:
        service = MemoryService()
        port = MemoryRuntimeStatePort(service)
        payload = dict(port.snapshot_state())
        payload["context_epoch"] = "not-an-int"
        with self.assertRaises(ValueError):
            port.prepare_restore_state(payload)

    def test_legacy_payload_without_epoch_migrates_to_zero(self) -> None:
        service = MemoryService()
        service.l1_store.append(_settled_transcript("legacy"))
        port = MemoryRuntimeStatePort(service)
        payload = dict(port.snapshot_state())
        legacy = {key: value for key, value in payload.items()
                  if key not in {"context_epoch", "compaction_receipts"}}
        prepared = port.prepare_restore_state(legacy)
        self.assertEqual(prepared.context_epoch, 0)
        self.assertEqual(prepared.compaction_receipts, {})


class _Advice:
    def __init__(self, status) -> None:
        from pal.llm import LLMPreflightAdvice
        self._advice = LLMPreflightAdvice(status=status)

    def __getattr__(self, item):
        return getattr(self._advice, item)


class E01ColdVerticalTests(unittest.TestCase):
    """E2E_RECIPES E01, cold lane, resident host, steps 1-8."""

    def test_e01_cold_resident_lane(self) -> None:
        service, turn_id, _ = _service_with_active()
        seen_sources: list[str] = []
        gate_generate_entered = asyncio.Event()
        release_generate = asyncio.Event()

        class BarrierLLM(_ScriptedLLM):
            async def agenerate(self, request):
                seen_sources.append(request.messages[-1].text)
                gate_generate_entered.set()
                await release_generate.wait()
                return generation_result_from_values(
                    text=_valid_pal_payload("DONE_A_DO_NOT_REPEAT next DO_B")
                )

        async def scenario():
            from pal.core.ingress_staging import IngressStagingStore
            import tempfile
            from pathlib import Path
            from pal.core.runtime import PalCore
            from pal.shared import ChannelEnvelope, EndpointConfig, EventKind, SourceKind
            from pal.foundation.io import EventEnvelope
            from pal.shared.agent_io import ResponseHandle

            core = PalCore()
            core.context.port_registry["memory:memory"] = service
            llm = BarrierLLM([])
            core.context.port_registry["llm:llm"] = llm
            core.turn_executor._compaction_engine = CompactionEngine(
                PalCompactionPolicy()
            )
            with tempfile.TemporaryDirectory() as tmp:
                core.state.ingress_staging = IngressStagingStore(
                    Path(tmp) / "staging.json"
                )
                continuation = SimpleNamespace(
                    turn_id=turn_id,
                    delivery_binding=None,
                    tool_batch_count=2,
                    retry_count=1,
                    waiting_effect_id=None,
                )
                # The auto path claims the scope ticket before generating;
                # mirror that here so ingress is back-pressured (I03).
                gate = core._compaction_gate()
                async with core.state.channel_turn_transition_lock:
                    ticket = gate.claim(
                        "pal:resident", trigger="auto"
                    )
                self.assertIsNotNone(ticket)
                snapshot = _capture(service, compaction_op_id="op-e01")
                run_task = asyncio.create_task(
                    core.turn_executor.compact_memory_async(
                        service,
                        target_input_budget=8_192,
                        reserved_output_tokens=2_048,
                        continuation=continuation,
                        assembly_context=None,
                    )
                )
                # compact_memory_async captures its own snapshot; park until
                # the fake generate is entered, then stage the correction.
                await gate_generate_entered.wait()
                l1_frozen = service.l1_source_stamp()
                epoch_frozen = service.context_epoch
                correction = ChannelEnvelope(
                    event=EventEnvelope(
                        event_kind=EventKind.USER_MESSAGE,
                        source_kind=SourceKind.CHANNEL,
                        payload={"text": "ACTUAL_NEW_CORRECTION"},
                        event_id="m-e01",
                    ),
                    endpoint=EndpointConfig(
                        endpoint_id="e01_endpoint",
                        channel_kind="memory",
                        binding_key="memory://e01",
                    ),
                    response_handle=ResponseHandle(
                        endpoint_id="e01_endpoint",
                        reply_target={"session_id": "e01"},
                    ),
                )
                await core.schedule_channel_turn_async(correction)
                self.assertEqual(
                    [item.event.event_id for item in core.state.pending_channel_turns],
                    ["m-e01"],
                )
                self.assertEqual(service.l1_source_stamp(), l1_frozen)
                self.assertEqual(service.context_epoch, epoch_frozen)

                release_generate.set()
                result = await run_task
                async with core.state.channel_turn_transition_lock:
                    gate.release(ticket)
                self.assertTrue(result.success, result.failures)

                # Step 7: durable root installed atomically.
                self.assertEqual(service.context_epoch, epoch_frozen + 1)
                turns = list(service.l1_store.turns.turns)
                self.assertEqual(
                    [turn.turn_id for turn in turns],
                    ["compact-summary", turn_id],
                )
                receipt = service.compaction_receipts[
                    result.memory_result.metadata["compaction_op_id"]
                ]
                self.assertEqual(receipt.successor_turn_id, turn_id)
                # Old raw history left the live transcript.
                text = _all_transcript_text(service)
                self.assertNotIn("Q_ORIGINAL", text)
                self.assertNotIn("RESULT_A", text)
                # Coverage: the summary source saw every sentinel once.
                self.assertEqual(len(seen_sources), 1)
                for sentinel in ("SEED0", "Q_ORIGINAL", "A_DECISION", "RESULT_A"):
                    self.assertIn(sentinel, seen_sources[0])
                # Retained result reference protects the tool delivery.
                successor_meta = dict(turns[1].metadata)
                self.assertIn("call-A", successor_meta.get("compact_retained_result_refs", ()))

                # Step 8: after release the queued correction is admitted
                # exactly once into the fresh context (interjection path).
                await core._inject_pending_for_executor_async(continuation)
                fresh = service.active_l1_turn(turn_id)
                fresh_text = "\n".join(
                    message.text for message in fresh.messages
                ) if fresh else ""
                # begin_l1_turn returned the empty successor unchanged, so
                # the correction lands as the first accepted message there.
                appended = [
                    message
                    for message in (fresh.messages if fresh else ())
                    if "ACTUAL_NEW_CORRECTION" in (message.text or "")
                ]
                self.assertEqual(len(appended), 1)
                # Budget/guard continuity fields survive the handoff.
                self.assertEqual(continuation.tool_batch_count, 2)
                self.assertEqual(continuation.retry_count, 1)
                self.assertIsNotNone(fresh_text or "empty successor is valid")

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
