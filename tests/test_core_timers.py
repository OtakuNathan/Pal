"""Timer events respect the main loop, cancellation and module ownership."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from pal.control.contracts import ControlAction, ControlDelivery, ControlRoute
from pal.core.contracts import PendingControlRequest
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from tests.cache_reminder_factory import attach_cache_reminder
from tests.core_factory import build_core
from tests.test_cache_warm_deadline import _snapshot


ROUTE = ControlRoute("test", "socket", control_scope_key="test:timers")


def test_timer_callbacks_wait_for_main_loop_and_cancel_after_enqueue():
    async def run():
        core = build_core()
        seen = []
        async def callback():
            seen.append("fired")
        scope = core.control_runtime.open_timer_scope()
        handle = scope.schedule(0, callback)
        assert seen == []
        for event in core.main_loop.timers.drain_due():
            core.receive_event(event)
        handle.cancel()
        await core.run_until_idle_async()
        assert seen == []
        scope.schedule(0, callback)
        await core.run_until_idle_async()
        assert seen == ["fired"]
        core.close()
    asyncio.run(run())


def test_module_withdrawal_cancels_queued_timers_without_touching_successor():
    async def run():
        core = build_core()
        seen = []
        async def old_callback():
            seen.append("old")
        async def new_callback():
            seen.append("new")
        old_scope = core.control_runtime.open_timer_scope()
        old = ModuleHandle("timer_owner", MODULE_TIER_DETACHABLE, timer_scope=old_scope)
        core.context.register_module(old)
        old_scope.schedule(0, old_callback)
        for event in core.main_loop.timers.drain_due():
            core.receive_event(event)
        core.context.unregister_module(old)
        new_scope = core.control_runtime.open_timer_scope()
        new = ModuleHandle("timer_owner", MODULE_TIER_DETACHABLE, timer_scope=new_scope)
        core.context.register_module(new)
        new_scope.schedule(0, new_callback)
        core.context.withdraw_module_control(old)
        await core.run_until_idle_async()
        assert seen == ["new"]
        core.close()
    asyncio.run(run())


def test_request_renewal_and_execution_cancel_independent_expiry_events():
    async def run():
        core = build_core()
        clock = [100.0]
        core.main_loop.timers.clock = lambda: clock[0]
        core._deliver_control_delivery_async = AsyncMock(return_value=True)
        requests = core.control_runtime.requests
        request = PendingControlRequest("confirmation", "confirm", ROUTE.control_scope_key, ROUTE,
            expires_at=datetime.now(timezone.utc) + timedelta(seconds=10),
            expired_delivery=ControlDelivery("interactive_expire", route=ROUTE))
        requests.put(request)
        assert 9 <= core.next_wakeup_timeout_seconds() <= 10
        clock[0] = 111.0
        for event in core.main_loop.timers.drain_due():
            core.receive_event(event)
        request.expires_at = datetime.now(timezone.utc) + timedelta(seconds=20)
        requests.put(request)
        await core.run_until_idle_async()
        assert requests.is_current(request)
        core._deliver_control_delivery_async.assert_not_awaited()
        requests.set_executing(request, True)
        clock[0] = 200.0
        await core.run_until_idle_async()
        assert requests.is_current(request)
        requests.set_executing(request, False)
        clock[0] = 221.0
        await core.run_until_idle_async()
        assert requests.get(ROUTE.control_scope_key, "confirm") is None
        core._deliver_control_delivery_async.assert_awaited_once()
        core.close()
    asyncio.run(run())


def test_optional_reminder_schedules_separate_show_and_remove_events():
    async def run():
        core = build_core()
        assert core.context.module_registry.get("cache_reminder") is None
        assert not hasattr(core, "cache_warm_deadline")
        clock = [100.0]
        core.main_loop.timers.clock = lambda: clock[0]
        core._deliver_control_delivery_async = AsyncMock(return_value=True)
        manager = attach_cache_reminder(core)
        manager.cache_snapshot = _snapshot
        assert manager.schedule_after_turn_commit(route=ROUTE, turn_id="completed")
        clock[0] = 161.0
        await core.run_until_idle_async()
        delivery = core._deliver_control_delivery_async.await_args.args[0]
        assert delivery.delivery_kind == "interactive_open"
        assert manager.inspect()["last_notified_epoch"] == "epoch-a"
        clock[0] = 191.0
        await core.run_until_idle_async()
        assert core._deliver_control_delivery_async.await_args.args[0].delivery_kind == "interactive_expire"
        assert not manager.inspect()["timer_scheduled"]
        core.close()
    asyncio.run(run())


def test_reminder_ignore_and_disable_deliver_replies_through_core():
    async def run():
        core = build_core()
        core._deliver_control_delivery_async = AsyncMock(return_value=True)
        manager = attach_cache_reminder(core)
        manager.cache_snapshot = _snapshot
        manager.schedule_after_turn_commit(route=ROUTE, turn_id="completed")
        await core.handle_control_action_async(ControlAction(
            "cache_warm_deadline_ignore", "cache_reminder", route=ROUTE,
            args={"cache_epoch": "epoch-a"}))
        assert "Dismissed" in core._deliver_control_delivery_async.await_args.args[0].text
        assert not manager.inspect()["timer_scheduled"]
        await core.handle_control_action_async(ControlAction(
            "cache_warm_deadline_disable", "cache_reminder", route=ROUTE))
        assert "disabled" in core._deliver_control_delivery_async.await_args.args[0].text
        assert not manager.settings().enabled
        owner = core.context.module_registry.require("cache_reminder")
        core.context.unregister_module(owner)
        assert "cache_reminder.inspect" not in core.context.configuration_operations
        assert core.context.control_action_registry.resolve("cache_warm_deadline_ignore") is None
        core.close()
    asyncio.run(run())


def test_timer_wakes_loop_while_background_work_waits():
    async def run():
        core = build_core()
        released = asyncio.Event()
        async def wake():
            released.set()
        task = asyncio.create_task(released.wait())
        core.state.turn_tasks["waiting"] = task
        core.control_runtime.open_timer_scope().schedule(0.01, wake)
        await asyncio.wait_for(core.run_until_idle_async(), timeout=1)
        assert task.done() and task.result()
        core.close()
    asyncio.run(run())


def test_module_context_change_trigger_needs_no_core_registration():
    async def run():
        core = build_core()
        claimed = []
        lease = await core.control_runtime.begin_context_change(
            "example_plugin.context_update", claim=lambda: claimed.append("claimed"))
        assert claimed == ["claimed"] and lease.current()
        await lease.close()
        assert not core.state.compaction_tickets
        core.close()
    asyncio.run(run())


def test_failed_timer_records_cause_and_loop_continues_with_next_event():
    async def run():
        core = build_core()
        seen = []
        async def fail():
            raise RuntimeError("timer callback failed")
        async def succeed():
            seen.append("next")
        scope = core.control_runtime.open_timer_scope()
        scope.schedule(0, fail)
        scope.schedule(0, succeed)
        await core.run_until_idle_async()
        assert seen == ["next"]
        diagnostic = next(item for item in core.state.diagnostics if item["kind"] == "runtime.timer.failed")
        assert "timer callback failed" in diagnostic["error"]
        core.close()
    asyncio.run(run())


def test_proactive_owns_deadline_timer_and_pauses_it_during_maintenance():
    from pal.proactive import ProactiveManager, register_with_core
    core = build_core()
    manager = ProactiveManager()
    handle = register_with_core(core.context, manager)
    source = handle.event_sources[0]
    manager.create_task(proactive_id="timer-job", goal="fixture", schedule={
        "cadence": "once", "run_at_utc": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()})
    assert not source.prepare(core.context)
    assert 0 < core.next_wakeup_timeout_seconds() <= 300
    core.state.memory_maintenance = True
    assert not source.prepare(core.context)
    assert core.next_wakeup_timeout_seconds() is None
    core.state.memory_maintenance = False
    assert not source.prepare(core.context)
    assert core.next_wakeup_timeout_seconds() is not None
    manager.destroy_task("timer-job")
    source.prepare(core.context)
    assert core.next_wakeup_timeout_seconds() is None
    manager.create_task(proactive_id="new-job", goal="fixture", schedule={
        "cadence": "once", "run_at_utc": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()})
    source.prepare(core.context)
    core.context.unregister_module(handle)
    assert core.next_wakeup_timeout_seconds() is None
    core.close()
