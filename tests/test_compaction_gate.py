"""P1 compaction gate admission/backpressure/control tests.

Barrier-driven (asyncio.Event), zero real sleeps. Maps to TEST_MATRIX IDs:
A06/A07/A08/A09/A10/A11, Q01/Q02/Q03/Q04/Q05/Q06/Q07/Q10/Q11/Q13/Q14,
X01(idle)/X04/X05 plus the round-safety predicate (A01-A05 unit level).

Honest boundaries recorded in P1_DESIGN.md: A04's effect-unknown ledger and
the full crash matrix (R-class) are P4; Bunshin lanes are P4.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pal.control.contracts import ControlAction
from pal.core.compaction import CompactionClockKind, CompactionRunResult
from pal.core.compaction_coordinator import (
    CompactionGate,
    CompactionPhase,
    CompactionTrigger,
)
from pal.core.interjection import inject_pending_interjection_async
from pal.core.runtime import RESIDENT_COMPACTION_SCOPE, PalCore
from pal.foundation.io import EventEnvelope
from pal.llm.ir import LLMMessageIR, MessageRole, MessageState, TextPartIR
from pal.memory import MemoryService
from pal.memory.turn_ir import L1TurnIR, L1TurnState
from pal.shared import EventKind, SourceKind
from pal.shared.agent_io import ChannelEnvelope, EndpointConfig, ResponseHandle
from pal.shared.tool_protocol import ToolResultIR, new_tool_call

from tests.test_cache_warm_deadline import _route
from tests.test_runtime_compaction import (
    _ScriptedLLM,
    _memory_with_turns,
    _valid_pal_payload,
)


# ── harness ─────────────────────────────────────────────────────────────


class _BarrierEngine:
    """Stub compaction engine: parks inside run() until released."""

    def __init__(self, *, status: str = "compacted") -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0
        self.status = status
        self.policy = SimpleNamespace(
            policy_id="gate-test",
            clock_kind=CompactionClockKind.USER_TURN,
            accepts_memory_candidates=False,
        )
        self.max_attempts = 3
        self.timeout_seconds = 30.0

    async def run(self, snapshot, *, llm_runtime=None, memory_service=None,
                  after_commit=None, replay_guard=None, commit_guard=None):
        self.calls += 1
        self.entered.set()
        await self.release.wait()
        # This gate-only stub does not install a candidate. Release the
        # history owner's lane as a real engine would before waking turns.
        root = memory_service.history_root
        if root.active_run is not None:
            root.fail(root.active_run.run_id, reason="gate fixture completed")
        memory_result = SimpleNamespace(
            summary="gate summary",
            projected_entries=[],
            metadata={"projected_entry_count": 0, "compact_summary_count": 1,
                      "retired_count": 0},
        )
        return CompactionRunResult(
            status=self.status,
            attempts=self.calls,
            memory_result=memory_result if self.status == "compacted" else None,
        )


class _BlockingLLM:
    """LLM port whose generation parks; keeps started turns alive."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.requests = 0

    async def apreflight(self, request):
        from pal.llm import LLMPreflightAdvice
        from pal.shared import LLMPreflightStatus
        return LLMPreflightAdvice(status=LLMPreflightStatus.READY.value, breakdown={})

    async def agenerate(self, request):
        self.requests += 1
        await self.release.wait()
        raise AssertionError("blocking llm must never finish in these tests")


class _ChannelRecorder:
    def __init__(self) -> None:
        self.statuses: list[tuple[str, dict]] = []
        self.replies: list[str] = []

    def queue_status(self, binding, kind, payload=None):
        self.statuses.append((str(kind), dict(payload or {})))

    def queue_reply(self, binding, text, **kwargs):
        self.replies.append(str(text))

    def last_user_route(self):
        return None


def _envelope(turn_id: str, text: str = "hello during compaction") -> ChannelEnvelope:
    return ChannelEnvelope(
        event=EventEnvelope(
            event_kind=EventKind.USER_MESSAGE,
            source_kind=SourceKind.CHANNEL,
            payload={"text": text, "session_id": "gate-session"},
            correlation_id=turn_id,
            event_id=turn_id,
        ),
        endpoint=EndpointConfig(
            endpoint_id="gate_endpoint",
            channel_kind="memory",
            binding_key="memory://gate",
        ),
        response_handle=ResponseHandle(
            endpoint_id="gate_endpoint",
            reply_target={"session_id": "gate-session"},
        ),
    )


def _action(args: dict | None = None) -> ControlAction:
    return ControlAction(
        action_kind="compact_memory",
        target_scope="memory",
        route=_route(),
        args=dict(args or {}),
    )


def _build_core(tmp_path: Path, *, engine_status: str = "compacted"):
    core = PalCore()
    service = _memory_with_turns(2)
    engine = _BarrierEngine(status=engine_status)
    core.context.port_registry["memory:memory"] = service
    core.context.port_registry["llm:llm"] = object()
    core.turn_executor._compaction_engine = engine
    replies: list[str] = []

    async def record_reply(action, text):
        replies.append(str(text))

    core._complete_compact_reply_async = record_reply
    return core, service, engine, replies


def _run(coro):
    return asyncio.run(coro)


# ── A · admission ───────────────────────────────────────────────────────


def test_manual_busy_with_active_turn_no_engine_no_ticket(tmp_path):
    async def scenario():
        core, _service, engine, replies = _build_core(tmp_path)
        core.turn_manager.latest_active_turn_id = Mock(return_value="turn-live")
        await core._handle_compact_memory_async(_action())
        assert replies and "unavailable" in replies[0]
        assert engine.calls == 0
        assert not core.state.compaction_tickets
    _run(scenario())


def test_manual_busy_while_previous_turn_teardown_unfinished(tmp_path):
    async def scenario():
        core, _service, engine, replies = _build_core(tmp_path)
        gate_event = asyncio.Event()
        teardown = asyncio.create_task(gate_event.wait())
        core.state.turn_tasks["old-turn"] = teardown
        try:
            await core._handle_compact_memory_async(_action())
            assert replies and "still finishing" in replies[0]
            assert engine.calls == 0
            assert not core.state.compaction_tickets
        finally:
            gate_event.set()
            teardown.cancel()
    _run(scenario())


def test_manual_busy_when_earlier_input_already_queued(tmp_path):
    async def scenario():
        core, _service, engine, replies = _build_core(tmp_path)
        core.state.pending_channel_turns.append(_envelope("m-earlier"))
        await core._handle_compact_memory_async(_action())
        assert replies and "postponed" in replies[0]
        assert engine.calls == 0
        assert not core.state.compaction_tickets
        assert len(core.state.pending_channel_turns) == 1
    _run(scenario())


def test_manual_no_op_on_minimal_memory_zero_engine(tmp_path):
    async def scenario():
        core = PalCore()
        service = MemoryService()
        core.context.port_registry["memory:memory"] = service
        core.context.port_registry["llm:llm"] = object()
        replies: list[str] = []

        async def record_reply(action, text):
            replies.append(str(text))

        core._complete_compact_reply_async = record_reply
        await core._handle_compact_memory_async(_action())
        assert replies and "minimal" in replies[0]
        assert not core.state.compaction_tickets
    _run(scenario())


def test_claim_waits_for_transition_lock_and_is_atomic(tmp_path):
    """A08: claim and admission checks share one critical section."""
    async def scenario():
        core, _service, engine, _replies = _build_core(tmp_path)
        await core.state.channel_turn_transition_lock.acquire()
        manual = asyncio.create_task(core._handle_compact_memory_async(_action()))
        for _ in range(4):
            await asyncio.sleep(0)
        # Lock held: admission has not claimed, engine has not run.
        assert not core.state.compaction_tickets
        assert engine.calls == 0
        core.state.channel_turn_transition_lock.release()
        await engine.entered.wait()
        assert core.state.compaction_tickets[RESIDENT_COMPACTION_SCOPE].phase == CompactionPhase.GENERATING
        engine.release.set()
        await manual
        assert not core.state.compaction_tickets
    _run(scenario())


def test_duplicate_manual_single_ticket_single_generate(tmp_path):
    async def scenario():
        core, _service, engine, replies = _build_core(tmp_path)
        first = asyncio.create_task(core._handle_compact_memory_async(_action()))
        await engine.entered.wait()
        await core._handle_compact_memory_async(_action())
        assert replies and "already in progress" in replies[0]
        assert engine.calls == 1
        ticket = core.state.compaction_tickets[RESIDENT_COMPACTION_SCOPE]
        assert ticket is not None and ticket.trigger == CompactionTrigger.MANUAL
        engine.release.set()
        await first
        assert engine.calls == 1
        assert not core.state.compaction_tickets
    _run(scenario())


# ── Q · backpressure, queue, ownership ──────────────────────────────────


def test_q04_busy_retry_receipt_for_input_queued_behind_compaction(tmp_path):
    """Q04: input arriving mid-compaction gets an explicit BUSY_RETRY
    receipt — "not in the conversation yet" — while staying queued with no
    L/R write, no new turn, and no durable staging."""
    async def scenario():
        core, service, engine, _replies = _build_core(tmp_path)
        recorder = _ChannelRecorder()
        core.context.port_registry["channel:channel"] = recorder
        manual = asyncio.create_task(core._handle_compact_memory_async(_action()))
        await engine.entered.wait()
        l1_before = list(service.l1_store.items)
        envelope = _envelope("m-q04", "BUSY_DURING_COMPACT")
        await core.schedule_channel_turn_async(envelope)
        # Queued exactly once, explicitly acknowledged as NOT admitted.
        assert [item.event.event_id for item in core.state.pending_channel_turns] == ["m-q04"]
        assert len(recorder.replies) == 1
        assert "NOT entered the conversation" in recorder.replies[0]
        # No L/R write, no turn, no durable staging (the queue is in-memory).
        assert list(service.l1_store.items) == l1_before
        assert not core.state.turn_tasks
        engine.release.set()
        await manual
    _run(scenario())


def test_gate_holds_new_messages_out_of_live_l1(tmp_path):
    """Q01/Q02: during GENERATING new input stays queued, L1 frozen."""
    async def scenario():
        core, service, engine, _replies = _build_core(tmp_path)
        manual = asyncio.create_task(core._handle_compact_memory_async(_action()))
        await engine.entered.wait()
        l1_before = list(service.l1_store.items)
        envelope = _envelope("m-during", "ACTUAL_NEW_CORRECTION")
        await core.schedule_channel_turn_async(envelope)
        assert len(core.state.pending_channel_turns) == 1
        assert list(service.l1_store.items) == l1_before
        assert not core.state.turn_tasks
        assert [item.event.event_id for item in core.state.pending_channel_turns] == ["m-during"]
        engine.release.set()
        await manual
    _run(scenario())


def test_interjection_doors_blocked_while_gate_held(tmp_path):
    """Q03 (door semantics): ticket active -> no snapshot, no L1 append."""
    async def scenario():
        core, service, engine, _replies = _build_core(tmp_path)
        manual = asyncio.create_task(core._handle_compact_memory_async(_action()))
        await engine.entered.wait()
        core.state.pending_channel_turns.append(_envelope("m-inject", "queued text"))
        append_calls: list[str] = []
        original = service.append_l1_user

        def counting_append(turn_id, message):
            append_calls.append(str(turn_id))
            return original(turn_id, message)

        service.append_l1_user = counting_append
        continuation = SimpleNamespace(turn_id="turn-live", delivery_binding=None)
        await inject_pending_interjection_async(
            context=core.context, state=core.state, continuation=continuation,
        )
        assert append_calls == []
        assert len(core.state.pending_channel_turns) == 1
        engine.release.set()
        await manual
    _run(scenario())


def test_claim_waits_for_inflight_interjection_commit(tmp_path):
    """Q04: an append+ack already inside the critical section finishes first."""
    async def scenario():
        core, service, engine, _replies = _build_core(tmp_path)
        envelope = _envelope("m-commit", "in flight")
        core.state.pending_channel_turns.append(envelope)
        service.l1_store.turns.append(_active_turn("turn-live", [LLMMessageIR(
            role=MessageRole.USER,
            parts=(TextPartIR("opening question"),),
            message_id="u1",
            state=MessageState.COMPLETE,
        )]))
        append_proceed = asyncio.Event()
        original = service.append_l1_user
        order: list[str] = []

        async def blocking_append(turn_id, message):
            order.append("append-entered")
            await append_proceed.wait()
            return original(turn_id, message)

        service.append_l1_user = blocking_append
        continuation = SimpleNamespace(turn_id="turn-live", delivery_binding=None)
        inject_task = asyncio.create_task(inject_pending_interjection_async(
            context=core.context, state=core.state, continuation=continuation))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert order == ["append-entered"]

        async def try_claim():
            async with core.state.channel_turn_transition_lock:
                order.append("claim-acquired")
                return core._compaction_gate().claim(
                    RESIDENT_COMPACTION_SCOPE, trigger=CompactionTrigger.AUTO)

        claim_task = asyncio.create_task(try_claim())
        for _ in range(4):
            await asyncio.sleep(0)
        # The claim is still waiting: the short commit holds the lock.
        assert order == ["append-entered"]
        append_proceed.set()
        ticket = await claim_task
        assert ticket is not None
        assert order == ["append-entered", "claim-acquired"]
        await inject_task
        # The accepted message left the queue before the claim observed it.
        assert len(core.state.pending_channel_turns) == 0
        engine.release.set()
    _run(scenario())


def test_queue_drains_fifo_after_manual_success(tmp_path):
    """Q05-lite: after release, the earliest queued event starts first."""
    async def scenario():
        core, _service, engine, _replies = _build_core(tmp_path)
        llm = _BlockingLLM()
        core.context.port_registry["llm:llm"] = llm
        manual = asyncio.create_task(core._handle_compact_memory_async(_action()))
        await engine.entered.wait()
        await core.schedule_channel_turn_async(_envelope("m1", "first"))
        await core.schedule_channel_turn_async(_envelope("m2", "second"))
        await core.schedule_channel_turn_async(_envelope("m3", "third"))
        engine.release.set()
        await manual
        # Manual success drained the queue: m1 started, m2/m3 still queued.
        assert "m1" in core.state.turn_tasks
        assert [item.event.event_id for item in core.state.pending_channel_turns] == ["m2", "m3"]
        llm.release.set()
        for task in list(core.state.turn_tasks.values()):
            task.cancel()
    _run(scenario())


def test_failed_compaction_keeps_history_and_queue(tmp_path):
    """Q06: generation failure leaves the authoritative context; queued
    input is not lost and proceeds under the old context."""
    async def scenario():
        core, service, engine, replies = _build_core(tmp_path, engine_status="generation_failed")
        llm = _BlockingLLM()
        core.context.port_registry["llm:llm"] = llm
        manual = asyncio.create_task(core._handle_compact_memory_async(_action()))
        await engine.entered.wait()
        await core.schedule_channel_turn_async(_envelope("m-kept", "keep me"))
        l1_before = list(service.l1_store.items)
        engine.release.set()
        await manual
        assert engine.calls == 1
        assert replies and any("failed" in text.lower() for text in replies)
        # Old authoritative history is preserved byte-for-byte as a prefix;
        # the only L1 change is the drained message's own acceptance.
        items_after = list(service.l1_store.items)
        assert items_after[:len(l1_before)] == l1_before
        assert len(items_after) == len(l1_before) + 1
        assert items_after[-1][-1].content == "keep me"
        assert not core.state.compaction_tickets
        # Queued input was not lost: the release follow-up started it.
        assert "m-kept" in core.state.turn_tasks
        assert len(core.state.pending_channel_turns) == 0
        llm.release.set()
        for task in list(core.state.turn_tasks.values()):
            task.cancel()
    _run(scenario())


def test_compaction_release_never_clears_other_quiesce_owners(tmp_path):
    """Q13: compact owns its ticket only; another owner's quiesce survives."""
    async def scenario():
        core, _service, _engine, replies = _build_core(tmp_path)
        gate = core._compaction_gate()
        async with core.state.channel_turn_transition_lock:
            ticket = gate.claim(RESIDENT_COMPACTION_SCOPE, trigger=CompactionTrigger.AUTO)
        assert ticket is not None
        # A second quiesce owner (dream) becomes active while the ticket lives.
        core.state.memory_maintenance = True
        async with core.state.channel_turn_transition_lock:
            assert gate.release(ticket) is True
        assert core.state.memory_maintenance is True
        assert not core.state.compaction_tickets
        # And manual admission respects that owner while it is active.
        await core._handle_compact_memory_async(_action())
        assert replies and "quiescing" in replies[0]
        core.state.memory_maintenance = False
    _run(scenario())


def test_late_release_cannot_release_successor_ticket(tmp_path):
    """Q14/F07: identity-checked release only removes the current holder."""
    async def scenario():
        core, _service, _engine, _replies = _build_core(tmp_path)
        gate = core._compaction_gate()
        async with core.state.channel_turn_transition_lock:
            first = gate.claim(RESIDENT_COMPACTION_SCOPE, trigger=CompactionTrigger.MANUAL)
            gate.release(first)
            second = gate.claim(RESIDENT_COMPACTION_SCOPE, trigger=CompactionTrigger.AUTO)
        assert first is not None and second is not None
        async with core.state.channel_turn_transition_lock:
            assert gate.release(first) is False
            assert gate.is_active(RESIDENT_COMPACTION_SCOPE)
            assert gate.release(second) is True
            assert not gate.is_active(RESIDENT_COMPACTION_SCOPE)
    _run(scenario())


# ── X · cancel, reset, control ──────────────────────────────────────────


def _active_turn(turn_id: str, messages) -> L1TurnIR:
    return L1TurnIR(
        turn_id=turn_id,
        state=L1TurnState.ACTIVE,
        messages=tuple(messages),
    )


def test_manual_real_engine_vertical_smoke(tmp_path):
    """HANDOFF P1 vertical smoke: real policy engine + scripted LLM + real
    MemoryService + staging, through the full manual handler."""
    from pal.core.compaction import CompactionEngine
    from pal.core.pal_compaction import PalCompactionPolicy
    from pal.llm import generation_result_from_values
    from pal.memory.contracts import L1MessageKind

    async def scenario():
        core = PalCore()
        service = _memory_with_turns(2)
        llm = _ScriptedLLM([
            generation_result_from_values(text=_valid_pal_payload("smoke summary"))
        ])
        core.context.port_registry["memory:memory"] = service
        core.context.port_registry["llm:llm"] = llm
        core.turn_executor._compaction_engine = CompactionEngine(
            policy=PalCompactionPolicy()
        )
        replies: list[str] = []

        async def record_reply(action, text):
            replies.append(str(text))

        core._complete_compact_reply_async = record_reply
        await core._handle_compact_memory_async(_action())
        assert replies and replies[0].startswith("Context compacted.")
        assert not core.state.compaction_tickets
        kinds = [
            message.kind
            for transcript in service.l1_store.items
            for message in transcript
            if message.kind == L1MessageKind.RUNTIME_CONTEXT_SUMMARY
        ]
        assert kinds, "real engine must install the compact summary into L1"
        assert len(llm.generate_requests) == 1
    _run(scenario())


def test_round_safe_requires_paired_tool_protocol(tmp_path):
    async def scenario():
        core = PalCore()
        service = MemoryService()
        core.context.port_registry["memory:memory"] = service
        call = new_tool_call(call_id="call_a", name="read_file", args={"path": "x"})
        assistant = LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(TextPartIR("calling"), call),
            message_id="a1",
            state=MessageState.COMPLETE,
        )
        service.l1_store.turns.append(_active_turn("t1", [assistant]))
        continuation = SimpleNamespace(turn_id="t1", waiting_effect_id=None)
        assert core.turn_executor._round_safe_for_compaction(continuation) is False

        result_message = LLMMessageIR(
            role=MessageRole.TOOL,
            parts=(ToolResultIR(call_id="call_a", name="read_file", content="ok"),),
            message_id="r1",
            state=MessageState.COMPLETE,
        )
        service.l1_store.turns.replace_all([
            _active_turn("t1", [assistant, result_message]),
        ])
        assert core.turn_executor._round_safe_for_compaction(continuation) is True
    _run(scenario())


def test_round_safe_rejects_open_streaming_round_and_in_progress(tmp_path):
    async def scenario():
        core = PalCore()
        service = MemoryService()
        core.context.port_registry["memory:memory"] = service
        assistant = LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(TextPartIR("done"),),
            message_id="a1",
            state=MessageState.COMPLETE,
        )
        service.l1_store.turns.append(_active_turn("t1", [assistant]))
        service.l1_store.turns.stream_assistant("t1", LLMMessageIR(
            role=MessageRole.ASSISTANT,
            parts=(TextPartIR("streaming"),),
            message_id="a2",
            state=MessageState.IN_PROGRESS,
        ))
        continuation = SimpleNamespace(turn_id="t1", waiting_effect_id=None)
        assert core.turn_executor._round_safe_for_compaction(continuation) is False
        # NOTE: waiting_effect_id is deliberately NOT part of the predicate:
        # the effect wrapper sets it while dispatching THIS effect, so any
        # check would reject every legitimate auto claim (sequential-model
        # guarantee covers other-effect concurrency instead).
    _run(scenario())
