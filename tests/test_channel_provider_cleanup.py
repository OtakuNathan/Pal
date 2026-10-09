"""Provider cleanup keeps the exact owner until all teardown work completes."""
import asyncio
from types import SimpleNamespace

import pytest

from pal.channel.provider_manager import (
    ChannelEndpointProviderManager, ChannelProviderBuildContext,
    DiscoveredChannelProvider, RuntimeChannelProviderHandle, RuntimeChannelProviderManifest,
)
from pal.channel.runtime import ChannelRuntime


@pytest.fixture
def owned_provider(tmp_path):
    runtime = ChannelRuntime()
    manager = ChannelEndpointProviderManager(runtime, SimpleNamespace(list_all=lambda: []), tmp_path)
    manifest = RuntimeChannelProviderManifest("demo", "provider.py", "1", True, str(tmp_path))
    events = []
    provider = SimpleNamespace(provider_id="demo", endpoint_types=("demo",),
                               detach=lambda context: events.append("detach"))
    context = ChannelProviderBuildContext(tmp_path, tmp_path, manifest, manager)
    handle = RuntimeChannelProviderHandle(manifest, provider, (), context.cleanup_callbacks, context, True)
    manager.discovered_runtime_providers["demo"] = DiscoveredChannelProvider(manifest, ("demo",))
    manager.runtime_provider_handles["demo"] = handle
    manager.register_provider(provider)
    return manager, handle, events


@pytest.mark.parametrize("asynchronous", [False, True])
def test_provider_failed_cleanup_retains_owner_and_retries_once(owned_provider, asynchronous):
    manager, handle, events = owned_provider
    blocked = True

    def cleanup():
        events.append("cleanup")
        if blocked:
            raise RuntimeError("busy resource")

    handle.cleanup_callbacks.append(cleanup)
    def unload():
        if asynchronous:
            asyncio.run(manager.stop_async())
            return manager.shutdown_errors
        return manager._unload_runtime_provider("demo")
    assert unload()
    assert manager.runtime_provider_handles.get("demo") is handle
    assert "demo" not in manager.providers
    with pytest.raises(RuntimeError, match="cleanup"):
        manager._ensure_provider_loaded("demo")
    blocked = False
    assert not unload()
    assert "demo" not in manager.runtime_provider_handles
    assert events == ["detach", "cleanup", "cleanup"]


def test_provider_detach_failure_defers_resource_cleanup(owned_provider):
    manager, handle, events = owned_provider
    blocked = True
    def detach(context):
        events.append("detach")
        if blocked:
            raise RuntimeError("detach pending")
    handle.provider.detach = detach
    handle.cleanup_callbacks.append(lambda: events.append("cleanup"))
    assert manager._unload_runtime_provider("demo")
    assert events == ["detach"]
    blocked = False
    assert not manager._unload_runtime_provider("demo")
    assert events == ["detach", "detach", "cleanup"]


def test_failed_factory_cleanup_is_retained_before_a_handle_can_be_built(owned_provider, monkeypatch):
    import pal.channel.provider_manager as module
    manager, handle, events = owned_provider
    manager.runtime_provider_handles.clear()
    manager.unregister_provider("demo")
    blocked = True
    def cleanup():
        events.append("cleanup")
        if blocked:
            raise RuntimeError("build cleanup pending")
    def factory(_module, *, context):
        context.register_cleanup(cleanup)
        raise RuntimeError("build failed")
    monkeypatch.setattr(module, "_runtime_provider_entrypoint_path", lambda manifest: manager.runtime_root / "provider.py")
    monkeypatch.setattr(module, "_load_source_module", lambda *args: object())
    monkeypatch.setattr(module, "_provider_from_module", factory)
    with pytest.raises(RuntimeError, match="build failed"):
        manager._build_runtime_provider_handle(handle.manifest)
    assert "demo" in manager.runtime_provider_handles
    blocked = False
    assert not manager._unload_runtime_provider("demo")
    assert events == ["cleanup", "cleanup"]


@pytest.mark.parametrize("cancel", [False, True])
def test_pending_async_provider_cleanup_is_not_started_twice(owned_provider, monkeypatch, cancel):
    from pal.channel.cleanup import OwnedLifecycleStep
    manager, handle, events = owned_provider
    original = OwnedLifecycleStep.run_async
    async def bounded(self):
        return await original(self, timeout=0.01)
    monkeypatch.setattr(OwnedLifecycleStep, "run_async", bounded)

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        async def cleanup():
            events.append("cleanup")
            started.set()
            await release.wait()
        handle.cleanup_callbacks.append(cleanup)
        first = asyncio.create_task(manager.stop_async())
        await started.wait()
        if cancel:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            await first
            assert manager.shutdown_errors
        assert manager.runtime_provider_handles["demo"] is handle
        await manager.stop_async()
        assert manager.shutdown_errors
        assert events == ["detach", "cleanup"]
        release.set()
        await manager.stop_async()
        assert not manager.shutdown_errors
        assert not manager.runtime_provider_handles
        assert events == ["detach", "cleanup"]
    asyncio.run(scenario())


def test_transport_stop_failure_prevents_provider_disposal(owned_provider, monkeypatch):
    from tests.test_channel_lifecycle import _endpoint
    manager, handle, events = owned_provider
    endpoint = _endpoint("one")
    manager.runtime.ensure_endpoint_hub("one", provider_id="demo", channel_kind="demo")
    manager.runtime.register_endpoint(endpoint)
    def failed(*args, **kwargs):
        raise RuntimeError("transport still running")
    monkeypatch.setattr(manager.runtime, "remove_endpoint", failed)
    _, errors = manager._stop_provider_transports("demo", reason="test")
    assert errors
    assert manager.runtime.get_endpoint("one") is endpoint
    assert manager._unload_runtime_provider("demo")
    assert manager.runtime_provider_handles["demo"] is handle
    assert events == []


def test_cleanup_completion_racing_waiter_cancellation_is_not_repeated():
    from pal.channel.cleanup import OwnedLifecycleStep
    async def scenario():
        release, started = asyncio.Event(), asyncio.Event()
        calls = []
        async def cleanup():
            calls.append("cleanup")
            started.set()
            await release.wait()
            return "receipt"
        step = OwnedLifecycleStep(cleanup)
        waiter = asyncio.create_task(step.run_async())
        await started.wait()
        release.set()
        await asyncio.sleep(0)
        assert step.pending.done()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert await step.run_async() == "receipt"
        assert calls == ["cleanup"]
    asyncio.run(scenario())


def test_sync_provider_cleanup_retains_task_returned_on_owner_loop(owned_provider):
    manager, handle, events = owned_provider

    async def scenario():
        release = asyncio.Event()
        async def cleanup():
            events.append("cleanup")
            await release.wait()
        calls = []
        def schedule_cleanup():
            calls.append("schedule")
            return asyncio.create_task(cleanup())
        handle.cleanup_callbacks.append(schedule_cleanup)
        assert manager._unload_runtime_provider("demo")
        assert handle.cleanup_step.pending is not None
        assert manager._unload_runtime_provider("demo")
        assert calls == ["schedule"]
        release.set()
        await manager.stop_async()
        assert not manager.shutdown_errors
        assert "demo" not in manager.runtime_provider_handles
        assert events == ["detach", "cleanup"]
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", [RuntimeError, TimeoutError])
def test_cross_loop_cleanup_failure_can_be_retried(failure, monkeypatch):
    from pal.channel.cleanup import OwnedLifecycleStep

    async def scenario():
        release = asyncio.Event()
        calls = []
        async def cleanup():
            calls.append("cleanup")
            await release.wait()
            if len(calls) == 1:
                raise failure("callback failed")
            return "receipt"
        step = OwnedLifecycleStep(cleanup)
        with pytest.raises(TimeoutError):
            await step.run_async(timeout=0.001)
        pending = step.pending
        # Release only after the sync waiter has selected the pending-task
        # cross-loop path, independent of thread scheduling speed.
        original = asyncio.run_coroutine_threadsafe
        def bridge_then_release(coroutine, loop):
            bridge = original(coroutine, loop)
            loop.call_soon_threadsafe(release.set)
            return bridge
        monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", bridge_then_release)
        with pytest.raises(failure, match="callback failed"):
            await asyncio.to_thread(step.run)
        assert pending.done()
        assert step.pending is None
        assert await step.run_async() == "receipt"
        assert calls == ["cleanup", "cleanup"]
    asyncio.run(scenario())


def test_timed_out_attach_is_joined_before_detach(owned_provider, monkeypatch):
    import threading
    from pal.channel.cleanup import OwnedLifecycleStep
    manager, handle, events = owned_provider
    manager.unregister_provider("demo")
    handle.attached = False
    release = threading.Event()
    ready = threading.Event()
    loop = asyncio.new_event_loop()
    manager.runtime._loop = loop
    def serve():
        asyncio.set_event_loop(loop)
        ready.set()
        loop.run_forever()
    thread = threading.Thread(target=serve)
    thread.start()
    ready.wait(2)
    async def attach(context):
        events.append("attach")
        while not release.is_set():
            await asyncio.sleep(0.001)
        events.append("attached")
        return lambda: events.append("cleanup")
    handle.provider.attach = attach
    original = OwnedLifecycleStep.run
    def bounded(self, loop=None, **kwargs):
        return original(self, loop, timeout=0.01)
    monkeypatch.setattr(OwnedLifecycleStep, "run", bounded)
    try:
        with pytest.raises(TimeoutError):
            manager._activate_runtime_provider_handle(handle)
        assert manager.runtime_provider_handles["demo"] is handle
        assert events == ["attach"]
        release.set()
        monkeypatch.setattr(OwnedLifecycleStep, "run", original)
        assert not manager._unload_runtime_provider("demo")
        assert events == ["attach", "attached", "detach", "cleanup"]
    finally:
        release.set()
        monkeypatch.setattr(OwnedLifecycleStep, "run", original)
        manager._unload_runtime_provider("demo")
        loop.call_soon_threadsafe(loop.stop)
        thread.join(2)
        loop.close()


def test_discovery_failure_disposes_already_built_candidate(owned_provider, monkeypatch):
    manager, handle, events = owned_provider
    manager.unregister_provider("demo")
    manager.discovered_runtime_providers.clear()
    handle.attached = False
    handle.cleanup_callbacks.append(lambda: events.append("cleanup"))
    monkeypatch.setattr(type(manager), "_scan_runtime_provider_manifests", lambda self: {
        "enabled": {"demo": handle.manifest}, "disabled": set(), "seen_paths": set(), "errors": [],
    })
    monkeypatch.setattr(type(manager), "_build_runtime_provider_handle", lambda self, manifest: handle)
    def fail(*args):
        raise RuntimeError("discovery failed")
    monkeypatch.setattr(type(manager), "_register_discovered_provider", fail)
    result = manager.rescan_providers()
    assert "discovery failed" in str(result)
    assert events == ["cleanup"]
    assert not manager.runtime_provider_handles


@pytest.mark.parametrize("failure", ["plugin", "channel", "resident"])
def test_runtime_shutdown_keeps_database_until_children_finish(failure):
    from pal.bootstrap.service import StubRuntimeHandle
    async def scenario():
        events = []
        blocked = True
        plugin = SimpleNamespace(shutdown_errors=[])
        manager = SimpleNamespace(shutdown_errors=[])
        def stop_plugin():
            events.append("plugin")
            plugin.shutdown_errors = ["pending plugin"] if blocked and failure == "plugin" else []
        async def stop_channel():
            events.append("channel")
            manager.shutdown_errors = ["pending channel"] if blocked and failure == "channel" else []
        async def stop_resident():
            events.append("resident")
            if blocked and failure == "resident":
                raise RuntimeError("pending resident")
        plugin.shutdown, manager.stop_async = stop_plugin, stop_channel
        resident = SimpleNamespace(shutdown_async=stop_resident, shutdown_sync=None)
        runtime = SimpleNamespace(
            plugin_host=plugin, channel_provider_manager=manager,
            database=SimpleNamespace(close=lambda: events.append("database")),
            core=SimpleNamespace(context=SimpleNamespace(port_registry={},
                module_registry=SimpleNamespace(modules={"resident": resident}))),
        )
        with pytest.raises(RuntimeError):
            await StubRuntimeHandle.stop_async(runtime)
        assert "database" not in events
        if failure != "resident":
            assert "resident" not in events
        if failure == "plugin":
            assert "channel" not in events
        blocked = False
        await StubRuntimeHandle.stop_async(runtime)
        assert events[-3:] == ["channel", "resident", "database"]
        assert resident.shutdown_async is None
    asyncio.run(scenario())
