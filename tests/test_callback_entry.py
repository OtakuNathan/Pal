"""Callback admission drops retired registrations without replay or lifetime pins."""
import asyncio
from unittest.mock import patch

import pytest

from pal.core.dispatcher import EventDispatcher
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.execution.contracts import CapabilityCall
from pal.foundation import EventEnvelope
from pal.plugins.host import PluginHost
from tests.core_factory import build_core
from tests.test_plugin_publication_rollback import MemoryPluginRepository
from tests.test_plugin_raii import _write_plugin


@pytest.fixture
def callbacks(tmp_path, monkeypatch):
    root = tmp_path / "builtin"
    _write_plugin(root, "callback_owner")
    source = root / "callback_owner_runtime.py"
    text = source.read_text().replace("from pal.core.module_registry",
        "from pal.control.runtime_contracts import CORE_CONTROL\nfrom pal.core.module_registry")
    text = text.replace("        scope.context.register_module(handle)", '''        ledger = self.ledger
        async def run(kind):
            ledger[0]["entered"].set()
            await ledger[0]["release"].wait()
            ledger.append((self.plugin_id, kind, None))
        async def timer():
            await run("timer")
        timers = scope.context.require_port(CORE_CONTROL).open_timer_scope()
        handle.timer_scope = timers
        timers.schedule(0 if ledger[0]["mode"] == "timer" else 3600, timer)
        class Handler:
            def can_handle(self, kind): return kind == "test.callback"
            async def handle(self, event, context):
                await run("event")
                return []
        handle.event_handlers = {"test.callback": [Handler()]}
        scope.context.register_module(handle)''')
    source.write_text(text)
    monkeypatch.syspath_prepend(str(root))
    core = build_core()
    state = {"mode": "timer", "entered": asyncio.Event(), "release": asyncio.Event()}
    ledger = [state]
    host = PluginHost(core.context, tmp_path, builtin_root=root, services={"ledger": ledger},
                      third_party_repository=MemoryPluginRepository())
    host.rescan()
    host.publish_management_capabilities()
    yield core, host, state, ledger
    host.shutdown()
    assert not host.shutdown_errors
    core.close()
    core.context.execution_runtime.shutdown()


@pytest.mark.parametrize("mode", ["timer", "event"])
def test_logically_unloaded_owner_is_ignored_even_if_cleanup_has_not_withdrawn_callbacks(callbacks, mode):
    core, host, state, ledger = callbacks

    async def run():
        state["mode"] = mode
        state["release"].set()
        assert host.attach("callback_owner")["status"] == "ok"
        owner = host.generations["callback_owner"].handle
        with patch.object(core.context.module_publications, "unsubscribe", side_effect=OSError("cleanup unavailable")):
            assert host.detach("callback_owner")["status"] == "error"
        assert not host._record("callback_owner").attached
        assert not owner.timer_scope.closed
        if mode == "event":
            core.receive_event(EventEnvelope(event_kind="test.callback", source_kind="test", payload={}))
        await core.run_until_idle_async()
        assert not state["entered"].is_set()
        assert all(row[1] == "start" for row in ledger[1:])
        assert host.detach("callback_owner")["status"] == "ok"
    asyncio.run(run())


@pytest.mark.parametrize("asynchronous", [False, True])
def test_captured_event_registration_is_skipped_after_replacement(asynchronous):
    core = build_core()
    calls = []
    registry = core.context.event_handler_registry
    event = EventEnvelope(event_kind="test.replace", source_kind="test", payload={})

    class Target:
        def can_handle(self, _kind):
            return True
        def handle(self, _event, _context):
            calls.append("target")
            return []
    shared_handler = Target()
    old = ModuleHandle("target", MODULE_TIER_DETACHABLE)
    core.context.register_module(old)
    replacement = ModuleHandle("target", MODULE_TIER_DETACHABLE)

    class Replace:
        replaced = False
        def can_handle(self, _kind):
            return True
        def handle(self, _event, context):
            if not self.replaced:
                self.replaced = True
                context.unregister_module(old)
                context.register_module(replacement)
                registry.register("test.replace", shared_handler, owner=replacement, module_id="target")
            return []
    registry.register("test.replace", Replace(), owner=None)
    registry.register("test.replace", shared_handler, owner=old, module_id="target")

    def dispatch():
        dispatcher = EventDispatcher()
        if asynchronous:
            return asyncio.run(dispatcher.dispatch_async(event, core.context))
        return dispatcher.dispatch(event, core.context)
    try:
        dispatch()
        assert calls == []
        registry.detach_module("target", expected_owner=old)
        dispatch()
        assert calls == ["target"]
    finally:
        core.close()
        core.context.execution_runtime.shutdown()


@pytest.mark.parametrize("mode", ["timer", "event"])
def test_rejected_unload_keeps_current_callbacks_valid(callbacks, mode):
    core, host, state, ledger = callbacks

    async def run():
        state["mode"] = mode
        state["release"].set()
        assert host.attach("callback_owner")["status"] == "ok"
        with patch.object(core.context.execution_runtime.implementation, "unmount_subtree", side_effect=OSError("registry unavailable")):
            assert host.detach("callback_owner")["status"] == "error"
        assert host._record("callback_owner").attached
        if mode == "event":
            core.receive_event(EventEnvelope(event_kind="test.callback", source_kind="test", payload={}))
        await core.run_until_idle_async()
        assert state["entered"].is_set()
        assert [row[1] for row in ledger[1:]] == ["start", mode]
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["timer", "event"])
def test_callback_already_entered_can_finish_after_unload(callbacks, mode):
    core, host, state, ledger = callbacks

    async def run():
        state["mode"] = mode
        assert host.attach("callback_owner")["status"] == "ok"
        if mode == "event":
            core.receive_event(EventEnvelope(event_kind="test.callback", source_kind="test", payload={}))
        task = asyncio.create_task(core.drain_once_async())
        try:
            await asyncio.wait_for(state["entered"].wait(), 2)
            result = await core.context.execution_runtime.execute_async(CapabilityCall(
                name="manage_plugin", args={"name": "callback_owner", "operation": "detach"}))
            assert result.status == "ok", result
            state["release"].set()
            await asyncio.wait_for(task, 2)
            assert [row[1] for row in ledger[1:]] == ["start", "cleanup", mode]
        finally:
            state["release"].set()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())
