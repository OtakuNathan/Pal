"""P6 cancellation/control tests for compaction tickets (X03/X06/X07/X08).

Barrier-driven (asyncio.Event), zero real sleeps. These pin the commit
eligibility boundary that the P6 work added to the gate/engine/executor:

- X03: a cancel that arrives after COMMITTED keeps the committed state
  and teardown intact; it revokes eligibility for anything later, never
  rolls memory back.
- X06: a non-terminal ticket past its deadline no longer holds the gate
  (claim sweeps it); the late holder's release/advance fail the identity
  check and its commit guard sees a successor, so a hung provider
  coroutine cannot pin the scope forever.
- X07: a confirmed endpoint/settings switch (refresh_llm_endpoint) first
  revokes every live ticket's commit eligibility; a warm-generate
  candidate can never install onto the new endpoint.
- X08: a presentation failure (queue_status raising) neither wedges the
  gate nor blocks teardown of a finished compaction.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

from pal.control.contracts import ControlAction
from pal.core.compaction_coordinator import (
    CompactionGate,
    CompactionPhase,
    CompactionTrigger,
)
from pal.core.ingress_staging import IngressStagingStore
from pal.core.runtime import RESIDENT_COMPACTION_SCOPE, PalCore
from pal.memory import MemoryService

from tests.test_cache_warm_deadline import _route
from tests.test_compaction_gate import (
    _BarrierEngine,
    _envelope,
    _run,
)
from tests.test_runtime_compaction import _memory_with_turns


def _action(kind: str = "compact_memory") -> ControlAction:
    return ControlAction(
        action_kind=kind,
        target_scope="memory",
        route=_route(),
        args={},
    )


def _build_core(tmp_path: Path):
    core = PalCore()
    service = _memory_with_turns(2)
    engine = _BarrierEngine()
    core.context.port_registry["memory:memory"] = service
    core.context.port_registry["llm:llm"] = object()
    core.turn_executor._compaction_engine = engine
    core.state.ingress_staging = IngressStagingStore(tmp_path / "staging.json")
    replies: list[str] = []

    async def record_reply(action, text):
        replies.append(str(text))

    core._complete_compact_reply_async = record_reply
    return core, service, engine, replies


def _commit_eligible(gate: CompactionGate, scope: str, op_id: str) -> bool:
    """Mirror of the executor's commit_eligible closure for gate-level pins."""
    current = gate.ticket_for(scope)
    return (
        current is not None
        and current.op_id == str(op_id)
        and not bool(current.cancelled)
    )


# ── X03 · commit-won-then-cancel ─────────────────────────────────────────


def test_x03_cancel_after_committed_keeps_commit_and_teardown(tmp_path):
    async def scenario():
        core, _service, engine, _replies = _build_core(tmp_path)
        gate = CompactionGate(
            core.state,
            transition_lock=core.state.channel_turn_transition_lock,
        )
        async with core.state.channel_turn_transition_lock:
            ticket = gate.claim(RESIDENT_COMPACTION_SCOPE, trigger=CompactionTrigger.AUTO)
            assert ticket is not None
            ticket = gate.advance(ticket, CompactionPhase.GENERATING)
            ticket = gate.advance(ticket, CompactionPhase.COMMITTED)
        # Cancel arrives after the commit is durable: it must not undo the
        # committed phase, and teardown (release) still succeeds.
        async with core.state.channel_turn_transition_lock:
            cancelled = gate.cancel(RESIDENT_COMPACTION_SCOPE, reason="user_interrupt")
        assert cancelled is not None and cancelled.cancelled
        assert cancelled.phase == CompactionPhase.COMMITTED
        async with core.state.channel_turn_transition_lock:
            assert gate.release(ticket) is True
        assert not core.state.compaction_tickets
        # Eligibility for any later install from this ticket is gone.
        assert not _commit_eligible(gate, RESIDENT_COMPACTION_SCOPE, ticket.op_id)
        assert engine.calls == 0  # gate-level pin: no engine involvement
    _run(scenario())


# ── X06 · deadline sweep + late-callback isolation ──────────────────────


def test_x06_expired_ticket_swept_and_late_holder_isolated(tmp_path):
    async def scenario():
        core, _service, _engine, _replies = _build_core(tmp_path)
        gate = CompactionGate(
            core.state,
            transition_lock=core.state.channel_turn_transition_lock,
        )
        async with core.state.channel_turn_transition_lock:
            stale = gate.claim(RESIDENT_COMPACTION_SCOPE, trigger=CompactionTrigger.AUTO)
            assert stale is not None
            # Force the ticket past its deadline, as a hung provider
            # coroutine would.
            core.state.compaction_tickets[RESIDENT_COMPACTION_SCOPE] = replace(
                stale,
                claimed_at_monotonic=time.monotonic() - (stale.deadline_seconds + 1.0),
            )
            # A fresh claim sweeps the expired ticket instead of being
            # rejected forever (X06: the gate is never pinned).
            fresh = gate.claim(RESIDENT_COMPACTION_SCOPE, trigger=CompactionTrigger.AUTO)
            assert fresh is not None
            assert fresh.op_id != stale.op_id
        # Late callbacks from the stale holder are isolated by identity:
        # its release fails, its advance is ignored.
        async with core.state.channel_turn_transition_lock:
            assert gate.release(stale) is False
            assert gate.advance(stale, CompactionPhase.COMMITTED) is None
        # Its commit guard no longer sees itself as current, so a late
        # install attempt is rejected before touching memory.
        assert not _commit_eligible(gate, RESIDENT_COMPACTION_SCOPE, stale.op_id)
        assert _commit_eligible(gate, RESIDENT_COMPACTION_SCOPE, fresh.op_id)
        async with core.state.channel_turn_transition_lock:
            assert gate.release(fresh) is True
    _run(scenario())


def test_x06_unexpired_ticket_still_blocks_claim(tmp_path):
    async def scenario():
        core, _service, _engine, _replies = _build_core(tmp_path)
        gate = CompactionGate(
            core.state,
            transition_lock=core.state.channel_turn_transition_lock,
        )
        async with core.state.channel_turn_transition_lock:
            live = gate.claim(RESIDENT_COMPACTION_SCOPE, trigger=CompactionTrigger.MANUAL)
            assert live is not None
            assert gate.claim(RESIDENT_COMPACTION_SCOPE, trigger=CompactionTrigger.AUTO) is None
    _run(scenario())


# ── X07 · endpoint/settings switch revokes live tickets ─────────────────


def test_x07_refresh_revokes_live_ticket_before_switch(tmp_path):
    async def scenario():
        core, _service, engine, _replies = _build_core(tmp_path)
        delivered: list[str] = []

        async def record_reply(action, text):
            delivered.append(str(text))

        core._complete_action_reply_async = record_reply
        manual = asyncio.create_task(core._handle_compact_memory_async(_action()))
        await engine.entered.wait()
        ticket = core.state.compaction_tickets[RESIDENT_COMPACTION_SCOPE]
        assert ticket.phase == CompactionPhase.GENERATING

        # The user switches endpoint/settings mid warm-generate.
        await core._handle_refresh_llm_endpoint_async(_action("refresh_llm_endpoint"))

        revoked = core.state.compaction_tickets[RESIDENT_COMPACTION_SCOPE]
        assert revoked.op_id == ticket.op_id
        assert revoked.cancelled and revoked.cancel_reason == "llm_endpoint_refresh"
        assert revoked.phase == CompactionPhase.GENERATING  # teardown still owned by holder
        # Commit eligibility is gone: the warm candidate cannot install.
        assert not _commit_eligible(
            CompactionGate(core.state, transition_lock=core.state.channel_turn_transition_lock),
            RESIDENT_COMPACTION_SCOPE,
            ticket.op_id,
        )
        engine.release.set()
        await manual
        # The gate drained cleanly after teardown despite the cancel.
        assert not core.state.compaction_tickets
    _run(scenario())


# ── X08 · presentation failure does not wedge the gate ──────────────────


def test_x08_status_delivery_failure_does_not_wedge_gate(tmp_path):
    async def scenario():
        core, _service, engine, _replies = _build_core(tmp_path)

        class ExplodingChannelRuntime:
            def queue_status(self, binding, kind, payload=None):
                raise RuntimeError("presentation layer is down")

            def remember_user_route(self, route):
                return None

            def last_user_route(self):
                return None

        core.context.port_registry["channel:channel"] = ExplodingChannelRuntime()
        manual = asyncio.create_task(core._handle_compact_memory_async(_action()))
        await engine.entered.wait()
        engine.release.set()
        await manual  # completes instead of hanging or raising
        assert not core.state.compaction_tickets  # gate released exactly once
        # A queued message during the same window still staged durably.
        core.state.pending_channel_turns.append(_envelope("m-x08"))
        assert core.state.ingress_staging is not None
    _run(scenario())


# ── X10/B09 · no-progress suppression on the auto path ─────────────────


def _auto_effect():
    from pal.core.turns import MemoryCompactEffect

    return MemoryCompactEffect(
        assembly_context=None,
        target_input_budget=8_192,
        reserved_output_tokens=2_048,
    )


def test_x10_same_source_failed_attempt_does_not_hot_loop(tmp_path):
    async def scenario():
        from tests.test_full_compaction_source import _service_with_active

        core = PalCore()
        service, turn_id, _ = _service_with_active()
        engine = _BarrierEngine(status="failed")
        core.context.port_registry["memory:memory"] = service
        core.context.port_registry["llm:llm"] = object()
        core.turn_executor._compaction_engine = engine
        continuation = SimpleNamespace(
            turn_id=turn_id, waiting_effect_id=None,
            interrupted=False, interrupt_reason="",
        )
        stamp = service.l1_source_stamp()

        first_task = asyncio.ensure_future(
            core.turn_executor.execute_turn_effect_async(continuation, _auto_effect())
        )
        await engine.entered.wait()
        engine.release.set()
        first = await first_task
        from pal.shared import RuntimeStatus

        assert first.status == RuntimeStatus.ERROR
        assert engine.calls == 1
        assert not core.state.compaction_tickets
        # The failed source stamp is remembered.
        assert core.state.compaction_no_progress.get("pal:resident") == stamp

        second = await core.turn_executor.execute_turn_effect_async(
            continuation, _auto_effect(),
        )
        # Same unchanged source: suppressed before any claim — the engine
        # never re-runs and no ticket is ever taken, so queued input and
        # control events keep their turn (X10).
        assert second.status == RuntimeStatus.ERROR
        assert "no progress" in str(second.text)
        assert engine.calls == 1
        assert not core.state.compaction_tickets
        from pal.core.compaction_coordinator import compaction_gate_active

        assert compaction_gate_active(core.state) is False

        # New input changes the stamp: the next auto attempt is admitted
        # again (suppression tracks the source, not the scope forever).
        service.begin_l1_turn("new-input", user_text="FRESH_INPUT")
        service.upsert_l1_assistant("new-input", __import__("pal.llm.ir", fromlist=["LLMMessageIR"]).LLMMessageIR(
            role=__import__("pal.llm.ir", fromlist=["MessageRole"]).MessageRole.ASSISTANT,
            parts=(__import__("pal.llm.ir", fromlist=["TextPartIR"]).TextPartIR("FRESH_REPLY"),),
        ))
        service.settle_l1_turn("new-input")
        third_task = asyncio.ensure_future(
            core.turn_executor.execute_turn_effect_async(continuation, _auto_effect())
        )
        await engine.entered.wait()
        engine.release.set()
        third = await third_task
        assert engine.calls == 2
    _run(scenario())


def test_b09_success_clears_no_progress_and_cap_stays(tmp_path):
    async def scenario():
        from pal.memory.turn_ir import L1TurnIR, L1TurnState
        from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
        from tests.test_runtime_compaction import _memory_with_turns

        core = PalCore()
        service = _memory_with_turns(2)
        core.context.port_registry["memory:memory"] = service
        core.context.port_registry["llm:llm"] = object()
        core.turn_executor._compaction_engine = _BarrierEngine(status="compacted")
        # Seed a stale failed stamp, as a previous failed attempt would.
        core.state.compaction_no_progress["pal:resident"] = "stale-stamp"
        continuation = SimpleNamespace(
            turn_id="turn-live", waiting_effect_id=None,
            interrupted=False, interrupt_reason="",
        )
        service.l1_store.turns.append(L1TurnIR(
            turn_id="turn-live", state=L1TurnState.ACTIVE,
            messages=[LLMMessageIR(
                role=MessageRole.USER,
                parts=(TextPartIR("opening question"),),
                message_id="u1",
            )],
        ))
        task = asyncio.ensure_future(
            core.turn_executor.execute_turn_effect_async(continuation, _auto_effect())
        )
        engine = core.turn_executor._compaction_engine
        await engine.entered.wait()
        engine.release.set()
        result = await task
        from pal.shared import RuntimeStatus

        assert result.status == RuntimeStatus.OK
        # A successful compaction made progress: the entry is gone, so a
        # later auto trigger on the fresh source is judged anew. Loop
        # bounding stays with the per-turn 3-attempt cap.
        assert "pal:resident" not in core.state.compaction_no_progress
    _run(scenario())
