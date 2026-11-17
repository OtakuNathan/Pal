"""Local calls retain their module until lifecycle writers can safely retire it."""
import asyncio
import threading

import pytest

from pal.control.configuration import CoreConfigurationService
from pal.control.contracts import ControlAction
from pal.core.capabilities import register_with_core as register_core
from pal.core.control_action_registry import ControlActionSpec
from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
from pal.execution.contracts import CapabilityResult
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput, LifecycleAccess
from pal.execution.tool_semantics import INDIRECT_LIFECYCLE
from pal.plugins.host import PluginHost
from tests.capability_fixture import mount_test_capability
from tests.core_factory import build_core
from tests.test_plugin_publication_rollback import MemoryPluginRepository
from tests.test_plugin_raii import _write_plugin


def test_cancelled_shutdown_drains_started_rpc_without_cancelling_its_operation(tmp_path):
    from pal.control.configuration import configuration_endpoint
    from pal.foundation.lifecycle_gate import WriterPreferredRWGate
    from pal.foundation.sidecar import SidecarRpcClient
    async def scenario():
        entered, release, finished, cancelled = (asyncio.Event() for _ in range(4))
        async def operation(params):
            entered.set()
            try:
                await release.wait()
                finished.set()
                return {}
            except asyncio.CancelledError:
                cancelled.set()
                raise
        service = CoreConfigurationService(tmp_path, WriterPreferredRWGate(), {'slow': ('owner', operation)})
        await service.start_async()
        client = SidecarRpcClient(configuration_endpoint(tmp_path))
        request = asyncio.create_task(client.request('slow', {}))
        stopping = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            stopping = asyncio.create_task(service.stop_async())
            await asyncio.sleep(0.02)
            stopping.cancel()
            await asyncio.sleep(0)
            stopping.cancel()
            await asyncio.sleep(0.02)
            assert not cancelled.is_set()
            assert not stopping.done()
        finally:
            release.set()
            if stopping is not None:
                with pytest.raises(asyncio.CancelledError):
                    await stopping
            await asyncio.gather(request, return_exceptions=True)
            await service.stop_async()
        assert finished.is_set()
        assert not service._clients
        assert service._server is None
    asyncio.run(scenario())


@pytest.fixture
def local_calls(tmp_path, monkeypatch):
    root = tmp_path / "builtin"
    _write_plugin(root, "local_calls")
    source = root / "local_calls_runtime.py"
    code = source.read_text().replace(
        "from pal.core.module_registry",
        "from pal.core.control_action_registry import ControlActionSpec\nfrom pal.core.module_registry")
    code = code.replace("        scope.context.register_module(handle)", '''        async def slow(value):
            self.ledger[0]["entered"].set()
            await self.ledger[0]["release"].wait()
            self.ledger.append((self.plugin_id, "effect", None))
            return {"status": "ok"}
        handle.control_action_handlers = {"local_slow": ControlActionSpec(slow)}
        handle.configuration_handlers = {"local.configure": slow}
        handle.control_observers = {"local.changed": slow}
        scope.context.register_module(handle)''')
    source.write_text(code)
    monkeypatch.syspath_prepend(str(root))
    core = build_core()
    state = {"entered": asyncio.Event(), "release": asyncio.Event()}
    ledger = [state]
    host = PluginHost(core.context, tmp_path, builtin_root=root,
                      services={"ledger": ledger}, third_party_repository=MemoryPluginRepository())
    host.rescan()
    assert host.attach("local_calls")["status"] == "ok"
    yield core, host, state, ledger
    host.shutdown()
    core.close()
    core.context.execution_runtime.shutdown()


async def invoke(core, service, mode):
    if mode == "slash":
        return await core.handle_control_action_async(ControlAction("local_slow", "local_calls"))
    if mode == "observer":
        return await core.control_runtime.notify("local.changed", {})
    return await service._call_method("local.configure", {})


@pytest.mark.parametrize("mode", ["slash", "rpc", "observer"])
def test_unload_waits_for_started_local_call(local_calls, mode):
    core, host, state, ledger = local_calls

    async def run():
        service = CoreConfigurationService(host.runtime_root, core.context.execution_runtime.lifecycle_gate,
                                           core.context.configuration_operations)
        await service.start_async()
        call = asyncio.create_task(invoke(core, service, mode))
        writer = None
        try:
            await asyncio.wait_for(state["entered"].wait(), 2)
            attempted = threading.Event()
            def detach():
                attempted.set()
                with host.package_lifecycle.gate():
                    return host.detach("local_calls")
            writer = asyncio.create_task(asyncio.to_thread(detach))
            assert await asyncio.to_thread(attempted.wait, 2)
            await asyncio.sleep(0.02)
            assert not writer.done()
            assert not any(row[1] == "cleanup" for row in ledger[1:])
            state["release"].set()
            await asyncio.wait_for(call, 2)
            assert (await asyncio.wait_for(writer, 2))["status"] == "ok"
            assert [row[1] for row in ledger[1:]] == ["start", "effect", "cleanup"]
        finally:
            state["release"].set()
            await asyncio.gather(call, *([writer] if writer is not None else []), return_exceptions=True)
            await service.stop_async()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["slash", "rpc", "observer"])
def test_queued_local_call_cannot_enter_a_replacement(local_calls, mode, monkeypatch):
    core, host, state, ledger = local_calls
    gate = core.context.execution_runtime.lifecycle_gate
    attempted = threading.Event()
    acquire = gate._acquire_read
    def read():
        attempted.set()
        acquire()
    monkeypatch.setattr(gate, "_acquire_read", read)

    async def run():
        service = CoreConfigurationService(host.runtime_root, gate, core.context.configuration_operations)
        await service.start_async()
        try:
            async with gate.write_async():
                call = asyncio.create_task(invoke(core, service, mode))
                assert await asyncio.to_thread(attempted.wait, 2)
                assert host.detach("local_calls")["status"] == "ok"
                assert host.attach("local_calls")["status"] == "ok"
            result, = await asyncio.wait_for(asyncio.gather(call, return_exceptions=True), 2)
            if mode == "rpc":
                assert isinstance(result, ValueError) and "withdrawn" in str(result)
            else:
                assert not isinstance(result, BaseException)
            assert not state["entered"].is_set()
            assert not any(row[1] == "effect" for row in ledger[1:])
        finally:
            state["release"].set()
            await service.stop_async()
    asyncio.run(run())


@pytest.mark.parametrize("mode", ["slash", "rpc"])
def test_cancelled_local_call_releases_its_lifecycle_lease(local_calls, mode):
    core, host, state, ledger = local_calls

    async def run():
        gate = core.context.execution_runtime.lifecycle_gate
        service = CoreConfigurationService(host.runtime_root, gate, core.context.configuration_operations)
        await service.start_async()
        try:
            call = asyncio.create_task(invoke(core, service, mode))
            await asyncio.wait_for(state["entered"].wait(), 2)
            call.cancel()
            with pytest.raises(asyncio.CancelledError):
                await call
            async with asyncio.timeout(2), gate.write_async():
                assert host.detach("local_calls")["status"] == "ok"
            assert not any(row[1] == "effect" for row in ledger[1:])
        finally:
            state["release"].set()
            await service.stop_async()
    asyncio.run(run())


def test_core_tool_forwarding_preserves_exclusive_lifecycle_access():
    core = build_core()
    register_core(core)
    runtime = core.context.execution_runtime
    calls = []
    mount_test_capability(runtime, alias="retire_test", canonical_path="op_test_retire",
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, execution=INDIRECT_LIFECYCLE,
        handler=lambda _: (calls.append("retire") or CapabilityResult(status="ok", llm_text="retired")))
    try:
        asyncio.run(asyncio.wait_for(core.handle_control_action_async(
            ControlAction("invoke_capability", "runtime", target_id="op_test_retire")), 2))
        assert calls == ["retire"]
    finally:
        core.close()
        runtime.shutdown()


def test_exclusive_control_can_notify_module_observers():
    core = build_core()
    calls = []
    async def observe(_payload):
        calls.append("observed")
    async def exclusive(_action):
        assert not await core.control_runtime.notify("local.changed", {})
    core.context.register_module(ModuleHandle(
        "exclusive_control", MODULE_TIER_DETACHABLE, detachable=True,
        control_action_handlers={"exclusive": ControlActionSpec(exclusive, lifecycle_access=LifecycleAccess.EXCLUSIVE)},
        control_observers={"local.changed": observe}))
    try:
        asyncio.run(asyncio.wait_for(core.handle_control_action_async(ControlAction("exclusive", "runtime")), 2))
        assert calls == ["observed"]
    finally:
        core.close()
        core.context.execution_runtime.shutdown()


def test_interrupt_remains_available_during_a_lifecycle_writer():
    from tests.test_btw import Model, ROUTE, make_core
    from tests.test_interjection_injection import _make_envelope

    async def run():
        model = Model(blocked=True)
        core, _ = make_core(model)
        try:
            await core.schedule_channel_turn_async(_make_envelope(turn_id="interruptible", text="hello"))
            await asyncio.wait_for(model.entered.wait(), 2)
            tasks = tuple(core.state.turn_tasks.values())
            async with core.context.execution_runtime.lifecycle_gate.write_async():
                await asyncio.wait_for(core.handle_control_action_async(
                    ControlAction("interrupt_turn", "runtime", route=ROUTE)), 2)
            await asyncio.gather(*tasks, return_exceptions=True)
            assert not core.state.active_turns
        finally:
            core.close()
            core.context.execution_runtime.shutdown()
    asyncio.run(run())
