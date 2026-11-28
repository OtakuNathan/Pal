"""Resident scheduling boundaries exercised through the real core and pump."""
import asyncio
from types import SimpleNamespace

import pytest
from hypothesis import given, strategies as st

from pal.core.scheduling import (
    CoreScheduler, CoreTurnPhase, ResidentTurnKind, SchedulingEvent,
)
from pal.core.turns import TurnContinuation
from pal.foundation import EventEnvelope
from pal.shared import EventKind
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
        task = asyncio.create_task(core._execute_soft_reset_async(None))
        await entered.wait()
        core.state.scheduler.set_barrier("quiescing:shutdown", True)
        resume.set()
        assert await task
        assert core.state.resident_quiescing
        assert core.state.scheduler.state.barriers == {"quiescing:shutdown"}
        assert not core.control_runtime.can_start_background_turn()
    asyncio.run(scenario())


def test_cancelled_reset_notification_releases_its_barrier():
    from tests.cache_reminder_factory import attach_cache_reminder

    async def scenario():
        core, _ = make_core(Model())
        attach_cache_reminder(core)
        try:
            async with core.context.execution_runtime.lifecycle_gate.write_async():
                task = asyncio.create_task(core._execute_soft_reset_async(None))
                async with asyncio.timeout(2):
                    while not core.state.resident_quiescing:
                        await asyncio.sleep(0)
                core.state.scheduler.set_barrier("quiescing:shutdown", True)
                task.cancel()
                await asyncio.sleep(0)
            with pytest.raises(asyncio.CancelledError):
                await task
            assert core.state.scheduler.state.barriers == {"quiescing:shutdown"}
            core.state.scheduler.set_barrier("quiescing:shutdown", False)
            assert core.control_runtime.can_start_background_turn()
        finally:
            core.close()
            core.context.execution_runtime.shutdown()
    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_reset_setup_failure_releases_its_barrier(monkeypatch):
    async def scenario():
        core, _ = make_core(Model())
        memory = core.context.require_port("memory:memory")
        def fail(*, reason):
            raise OSError("compaction cancellation unavailable")
        monkeypatch.setattr(memory, "cancel_active_compaction", fail)
        try:
            with pytest.raises(OSError, match="cancellation unavailable"):
                await core._execute_soft_reset_async(None)
            assert not core.state.scheduler.state.barriers
            assert core.control_runtime.can_start_background_turn()
        finally:
            core.close()
            core.context.execution_runtime.shutdown()
    asyncio.run(scenario())


def test_repeated_reset_cancellation_still_finishes_barrier_cleanup():
    from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE

    async def scenario():
        core, _ = make_core(Model())
        entered = asyncio.Event()
        async def observe(_payload):
            entered.set()
            await asyncio.Event().wait()
        core.context.register_module(ModuleHandle("slow_observer", MODULE_TIER_DETACHABLE,
            control_observers={"core.context_change.started": observe}))
        try:
            task = asyncio.create_task(core._execute_soft_reset_async(None))
            await entered.wait()
            async with core.state.channel_turn_transition_lock:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not core.state.resident_quiescing
            assert core.control_runtime.can_start_background_turn()
        finally:
            core.close()
            core.context.execution_runtime.shutdown()
    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_user_input_in_pump_has_priority_over_background_turn():
    core, _ = make_core(Model())
    core.receive_event(EventEnvelope(
        event_kind=EventKind.USER_MESSAGE, source_kind="channel",
        payload=_make_envelope(turn_id="next", text="user first"),
    ))
    assert not core.control_runtime.can_start_background_turn()
    assert core._admit_resident_turn("job", ResidentTurnKind.BACKGROUND) is None
    assert core.state.scheduler.state.phase == CoreTurnPhase.IDLE


def test_cancellation_before_task_start_releases_lease_without_opening_l1():
    async def scenario():
        core, events = make_core(Model())
        continuation = TurnContinuation("job", iter(()), "job")
        task = core.start_background_turn(continuation, lambda: asyncio.sleep(60), owner=None)
        assert task is not None
        assert not core.control_runtime.can_start_background_turn()
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
            core.start_background_turn(continuation, lambda: asyncio.sleep(0), owner=None)
        assert core.state.scheduler.state.phase == CoreTurnPhase.IDLE
        assert not core.state.turn_tasks
        assert core.control_runtime.can_start_background_turn()
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


def test_proactive_plugin_dispatches_while_resident_awaits_model(tmp_path):
    from pal.foundation import PalV2Database
    from pal.plugins_builtin.proactive.runtime import ProactivePlugin
    from pal.proactive.models import ProactiveDefinitionModel, ProactiveRunModel
    from pal.shared.background_protocol import BACKGROUND_WORKFLOWS

    async def scenario():
        model = Model(blocked=True)
        core, events = make_core(model)
        requests = []

        class Backend:
            async def start_scheduled_run(self, request):
                requests.append(request)
                return {"workflow_id": "background-workflow"}

            async def scheduled_run_status(self, workflow_id):
                return {"workflow": {"workflow_state": "COMPLETED",
                                     "delivery": {"result_text": "finished"}}}

        core.context.port_registry.register(BACKGROUND_WORKFLOWS, Backend())
        handle = ProactivePlugin().start(SimpleNamespace(context=core.context))
        manager = handle.ports["proactive_manager"]
        await core.schedule_channel_turn_async(_make_envelope(turn_id="chat", text="CHAT_SENTINEL"))
        await asyncio.wait_for(model.entered.wait(), 2)
        lease = core.state.scheduler.state.owner
        manager.ensure_silent_once(proactive_id="scheduled", goal="BACKGROUND_SENTINEL", method="")
        for _ in range(10):
            await core.drain_once_async()
            if requests:
                break
        assert len(requests) == 1
        assert requests[0]["goal"] == "BACKGROUND_SENTINEL"
        assert core.state.scheduler.state.owner == lease
        assert manager.repository.latest_run("scheduled").status == "dispatched"
        assert len(model.requests) == 1
        memory = core.context.require_port("memory:memory")
        assert [t.turn_id for t in memory.history.turns] == ["chat"]
        # The durable reconciliation event completes without a Resident turn.
        source, = handle.event_sources
        source.next_poll = 0
        for _ in range(10):
            await core.drain_once_async()
            if not manager.pending_triggers:
                break
        assert manager.repository.latest_run("scheduled").status == "completed"
        assert core.state.scheduler.state.owner == lease
        assert len(model.requests) == 1
        model.release.set()
        await asyncio.wait_for(core.run_until_idle_async(max_iterations=128), 5)
        assert [t.turn_id for t in memory.history.turns] == ["chat"]
        assert not core.state.active_turns
        core.close()

    db = PalV2Database(tmp_path / "runtime.sqlite3")
    db.initialize([ProactiveDefinitionModel, ProactiveRunModel])
    try:
        asyncio.run(scenario())
    finally:
        db.close()
