"""Resident scheduling boundaries exercised through the real core and pump."""
import asyncio
from types import SimpleNamespace

import pytest
from hypothesis import given, strategies as st

from pal.core import register_with_core
from pal.core.scheduling import (
    CoreScheduler, CoreTurnPhase, ResidentTurnKind, SchedulingEvent,
)
from pal.core.turns import TurnContinuation
from pal.foundation import EventEnvelope
from pal.proactive import ProactiveDefinition, ProactiveManager, ProactiveRunner
from pal.proactive.capabilities import register_with_core as register_proactive
from pal.proactive.handler import ProactiveTriggerHandler
from pal.proactive.source import ProactiveEventSource
from pal.shared import EventKind, ProactiveTriggerEvent
from tests.test_btw import Model, make_core
from tests.test_interjection_injection import _make_envelope


@given(st.lists(st.sampled_from(list(SchedulingEvent)), max_size=80))
def test_arbitrary_events_cannot_release_another_generation(events):
    scheduler = CoreScheduler()
    old = None
    for event in events:
        before = scheduler.state
        decision = scheduler.transition(event, turn_id="same-id", lease=old)
        after = scheduler.state
        assert (after.phase == CoreTurnPhase.IDLE) == (after.owner is None)
        if not decision.accepted:
            assert after is before
        if after.owner is not None:
            old = after.owner
        if before.owner is not None and after.owner is None:
            assert before.phase == CoreTurnPhase.FINALIZING
            assert event == SchedulingEvent.TASK_FINISHED


def test_stale_completion_cannot_release_reused_turn_id():
    core, _ = make_core(Model())
    first = core._admit_resident_turn("same", ResidentTurnKind.CHAT)
    core._finish_resident_turn(first)
    second = core._admit_resident_turn("same", ResidentTurnKind.CHAT)
    assert second.generation > first.generation
    core._finish_resident_turn(first)
    assert core.state.scheduler.state.owner == second
    assert not core.state.resident_drained_event.is_set()
    core._finish_resident_turn(second)


@pytest.mark.parametrize("kind", list(ResidentTurnKind))
def test_barriers_block_every_resident_entry(kind):
    core, _ = make_core(Model())
    core.state.memory_maintenance = True
    assert core._admit_resident_turn("blocked", kind) is None
    core.state.resident_quiescing = True
    core.state.memory_maintenance = False
    assert core._admit_resident_turn("blocked", kind) is None
    core.state.resident_quiescing = False
    assert core._admit_resident_turn("allowed", kind) is not None


def test_reset_cannot_release_a_shutdown_barrier():
    async def scenario():
        from pal.memory.persistence.contracts import RESIDENT_HISTORY

        core, _ = make_core(Model())
        entered, resume = asyncio.Event(), asyncio.Event()
        async def reset():
            entered.set()
            await resume.wait()
        core.context.port_registry[RESIDENT_HISTORY.name] = SimpleNamespace(reset_async=reset)
        core.context.require_port("llm:llm").retire_projection_sessions = lambda: None
        task = asyncio.create_task(core._execute_soft_reset_async(None))
        await entered.wait()
        core.state.scheduler.set_barrier("quiescing:shutdown", True)
        resume.set()
        assert await task
        assert core.state.resident_quiescing
        assert core.state.scheduler.state.barriers == {"quiescing:shutdown"}
        assert not core.can_start_proactive_turn()
    asyncio.run(scenario())


def test_user_input_in_pump_has_priority_over_proactive():
    core, _ = make_core(Model())
    core.receive_event(EventEnvelope(
        event_kind=EventKind.USER_MESSAGE, source_kind="channel",
        payload=_make_envelope(turn_id="next", text="user first"),
    ))
    assert not core.can_start_proactive_turn()
    assert core._admit_resident_turn("job", ResidentTurnKind.PROACTIVE) is None
    assert core.state.scheduler.state.phase == CoreTurnPhase.IDLE


def test_proactive_source_before_user_source_still_gives_user_priority():
    async def scenario():
        model = Model(blocked=True)
        core, _ = make_core(model)
        register_with_core(core)
        manager = ProactiveManager()
        register_proactive(core.context, manager, ProactiveRunner())
        manager.hydrate(ProactiveDefinition(proactive_id="quiz", goal="quiz"))
        manager.enqueue_trigger(ProactiveTriggerEvent(proactive_id="quiz", trigger_kind="manual"))
        class UserSource:
            source_id = "test.user"
            ready = True
            def prepare(self, context):
                return self.ready
            def drain(self, context):
                self.ready = False
                return [EventEnvelope(
                    event_kind=EventKind.USER_MESSAGE, source_kind="channel",
                    payload=_make_envelope(turn_id="user", text="user first"),
                )]
        core.context.event_source_registry.attach("test", UserSource())
        await core.drain_once_async()  # proactive was drained first, then requeued
        assert len(manager.pending_triggers) == 1
        assert core.state.scheduler.state.owner is None
        await core.drain_once_async()
        await asyncio.wait_for(model.entered.wait(), 2)
        assert core.state.scheduler.state.owner.turn_id == "user"
        assert len(manager.pending_triggers) == 1
        model.release.set()
        await asyncio.wait_for(core.run_until_idle_async(max_iterations=128), 5)
        assert core.context.require_port("memory:memory").history.turns[0].turn_id == "user"
        assert not manager.pending_triggers
    asyncio.run(scenario())


def test_proactive_waits_through_io_and_finalization_then_follows_user_queue():
    async def scenario():
        model = Model(blocked=True)
        core, _ = make_core(model)
        register_with_core(core)
        manager, runner = ProactiveManager(), ProactiveRunner()
        register_proactive(core.context, manager, runner)
        manager.hydrate(ProactiveDefinition(proactive_id="quiz", goal="QUIZ_SENTINEL"))
        memory = core.context.require_port("memory:memory")
        entered, release = asyncio.Event(), asyncio.Event()
        commit = core._schedule_post_turn_commit_async

        async def finalization(outcome, **kwargs):
            if outcome.turn_id == "chat":
                entered.set()
                await release.wait()
            return await commit(outcome, **kwargs)

        core._schedule_post_turn_commit_async = finalization
        await core.schedule_channel_turn_async(_make_envelope(turn_id="chat", text="CHAT_SENTINEL"))
        task = core.state.turn_tasks["chat"]
        await asyncio.wait_for(model.entered.wait(), 2)
        manager.enqueue_trigger(ProactiveTriggerEvent(proactive_id="quiz", trigger_kind="manual"))
        await core.drain_once_async()
        assert len(manager.pending_triggers) == 1
        assert [t.turn_id for t in memory.history.turns] == ["chat"]
        assert not runner.results
        model.release.set()
        await asyncio.wait_for(entered.wait(), 2)
        assert core.state.scheduler.state.phase == CoreTurnPhase.FINALIZING
        assert core.turn_manager.latest_active_turn_id() == "chat"
        assert not core.can_start_proactive_turn()
        assert not core.state.resident_drained_event.is_set()
        await core.schedule_channel_turn_async(_make_envelope(turn_id="next", text="USER_NEXT_SENTINEL"))
        await core.drain_once_async()
        assert len(manager.pending_triggers) == 1
        release.set()
        await asyncio.wait_for(task, 2)
        await asyncio.wait_for(core.run_until_idle_async(max_iterations=128), 5)
        turns = memory.history.turns
        assert [t.turn_id for t in turns[:2]] == ["chat", "next"]
        assert len(turns) == 3
        assert all(t.state != "active" for t in turns)
        assert "QUIZ_SENTINEL" in repr(turns[2].messages)
        assert not manager.pending_triggers
        assert core.state.scheduler.state.phase == CoreTurnPhase.IDLE
        assert core.state.active_turn_id is None
        assert core.state.resident_drained_event.is_set()
        assert not core.state.diagnostics
        proactive_requests = [r for r in model.requests if r.metadata.get("history_visibility") == "active_turn"]
        assert len(proactive_requests) == 1
        assert "CHAT_SENTINEL" not in repr(proactive_requests[0].messages)
        assert "USER_NEXT_SENTINEL" not in repr(proactive_requests[0].messages)
    asyncio.run(scenario())


def test_already_drained_proactive_event_requeues_if_chat_won_admission():
    async def scenario():
        core, _ = make_core(Model())
        register_with_core(core)
        manager = ProactiveManager()
        manager.hydrate(ProactiveDefinition(proactive_id="quiz", goal="quiz"))
        handler = ProactiveTriggerHandler(manager)
        source = ProactiveEventSource(manager)
        manager.enqueue_trigger(ProactiveTriggerEvent(proactive_id="quiz", trigger_kind="manual"))
        assert source.prepare(core.context)
        event, = source.drain(core.context)
        lease = core._admit_resident_turn("chat", ResidentTurnKind.CHAT)
        handler.handle(event, core.context)
        assert len(manager.pending_triggers) == 1
        assert not handler.tasks
        assert not core.context.require_port("memory:memory").history.turns
        assert not source.prepare(core.context)
        core._finish_resident_turn(lease)
        assert source.prepare(core.context)
    asyncio.run(scenario())


def test_cancellation_before_task_start_releases_lease_without_opening_l1():
    async def scenario():
        core, events = make_core(Model())
        continuation = TurnContinuation("job", iter(()), "job")
        task = core.start_background_turn(continuation, lambda: asyncio.sleep(60))
        assert task is not None
        assert not core.can_start_proactive_turn()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert core.state.scheduler.state.phase == CoreTurnPhase.IDLE
        assert not core.state.active_turns
        assert not core.context.require_port("memory:memory").history.turns
        assert [topic for topic, _ in events] == ["turn.start", "turn.end"]
    asyncio.run(scenario())


def test_channel_cancelled_before_first_step_still_closes_turn_activity():
    async def scenario():
        core, events = make_core(Model())
        await core.schedule_channel_turn_async(_make_envelope(turn_id="chat", text="hello"))
        task = core.state.turn_tasks["chat"]
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        assert core.state.scheduler.state.phase == CoreTurnPhase.IDLE
        assert not core.state.turn_scopes
        assert not core.context.require_port("memory:memory").history.turns
        assert [topic for topic, _ in events] == ["turn.start", "turn.end"]
    asyncio.run(scenario())


def test_task_creation_failure_rolls_back_only_its_reservation(monkeypatch):
    async def scenario():
        core, _ = make_core(Model())
        continuation = TurnContinuation("job", iter(()), "job")
        def fail(coroutine):
            raise RuntimeError("creation failed")
        monkeypatch.setattr(asyncio, "create_task", fail)
        with pytest.raises(RuntimeError, match="creation failed"):
            core.start_background_turn(continuation, lambda: asyncio.sleep(0))
        assert core.state.scheduler.state.phase == CoreTurnPhase.IDLE
        assert not core.state.turn_tasks
        assert core.can_start_proactive_turn()
    asyncio.run(scenario())


@pytest.mark.parametrize("interruption", [
    [{"role": "user", "content": [{"type": "text", "text": "foreign question"}]}],
    [{"role": "assistant", "content": [{"type": "text", "text": "foreign reply"}]}],
])
def test_sendable_gate_rejects_nonadjacent_anthropic_tool_pair(interruption):
    from pal.llm.projection_contracts import PreparedRequest, ProjectionContractError
    from pal.llm.ir import WireShape
    from tests.test_projection_third_review import make_session, key

    session = make_session(WireShape.ANTHROPIC_MESSAGES)
    call = {"role": "assistant", "content": [
        {"type": "tool_use", "id": "call", "name": "lookup", "input": {}},
    ]}
    result = {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": "call", "content": "success"},
    ]}
    PreparedRequest.build(key(session, "valid"), session.frontier, {"messages": [call, result]})
    with pytest.raises(ProjectionContractError, match="immediately following"):
        PreparedRequest.build(key(session, "invalid"), session.frontier,
                              {"messages": [call, *interruption, result]})
