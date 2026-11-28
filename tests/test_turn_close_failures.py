"""A failed Memory close must remain visible to resident admission."""
import asyncio

from pal.memory.contracts import MEMORY
from pal.memory.service import MemoryService
from tests.core_factory import build_core
from tests.turn_fakes import continuation


def test_failed_memory_close_is_retained_and_retried_before_next_admission():
    core = build_core()
    memory = MemoryService()
    core.context.port_registry.register(MEMORY, memory)
    turn = continuation(turn_id="closing")
    memory.begin_l1_turn(turn.turn_id, user_text="Keep this input")
    core.state.active_turns[turn.turn_id] = turn
    def reject(_turns):
        raise OSError("memory publication unavailable")
    def changed(old, new):
        pass
    memory.history.add_change_listener(changed, validate=reject)
    try:
        assert core.turn_manager.cleanup_interrupted(turn.turn_id) is False
        assert core.state.active_turns[turn.turn_id] is turn
        assert memory.active_l1_turn(turn.turn_id) is not None
        assert not core.control_runtime.can_start_background_turn()
        assert len(core.state.diagnostics) == 1
        assert core.state.diagnostics[-1]["cleanup_pending"] is True
        assert "memory publication unavailable" in core.state.diagnostics[-1]["error"]
        memory.history.remove_change_listener(changed)
        assert core.control_runtime.can_start_background_turn()
        assert not core.state.active_turns
        assert not core.turn_manager.pending_closures
        saved, = memory.history.turns
        assert saved.state == "interrupted"
        assert saved.messages[0].text == "Keep this input"
    finally:
        memory.history.remove_change_listener(changed)
        core.close()
        core.context.execution_runtime.shutdown()


def test_stop_cancels_model_even_when_memory_cannot_close():
    from tests.test_btw import Model, ROUTE, make_core
    from tests.test_interjection_injection import _make_envelope

    async def run():
        model = Model(blocked=True)
        core, events = make_core(model)
        memory = core.context.require_port(MEMORY)
        def reject(_turns):
            raise OSError("memory publication unavailable")
        def changed(old, new):
            pass
        try:
            await core.schedule_channel_turn_async(_make_envelope(turn_id="running", text="hello"))
            await model.entered.wait()
            tasks = tuple(core.state.turn_tasks.values())
            memory.history.add_change_listener(changed, validate=reject)
            from pal.control.contracts import ControlAction
            from unittest.mock import AsyncMock
            core._complete_action_reply_async = AsyncMock()
            await core._handle_interrupt_turn_async(ControlAction("interrupt_turn", "runtime", route=ROUTE))
            assert "could not be finalized" in core._complete_action_reply_async.await_args.args[1]
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
            assert not core.state.turn_tasks
            assert sum(topic == "turn.end" and payload["turn_id"] == "running"
                       for topic, payload in events) == 1
            assert "running" not in core.state.turn_scopes
            assert core.turn_manager.pending_closures
            assert not core.control_runtime.can_start_background_turn()
            memory.history.remove_change_listener(changed)
            assert core.control_runtime.can_start_background_turn()
            assert memory.active_l1_turn("running") is None
        finally:
            memory.history.remove_change_listener(changed)
            core.close()
            core.context.execution_runtime.shutdown()
    asyncio.run(run())
