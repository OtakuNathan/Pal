import asyncio

import pytest

from pal.channel.runtime import ChannelRuntime
from tests.test_channel_lifecycle import _endpoint


def test_failed_candidate_cleanup_is_retained_before_old_transport_can_restart():
    async def scenario():
        runtime = ChannelRuntime()
        old, candidate = _endpoint("demo"), _endpoint("demo")
        runtime.register_endpoint(old)
        await runtime.start_async()
        events = []
        blocked = True
        async def start_old():
            events.append("restart_old")
        async def start_candidate():
            events.append("start_candidate")
            raise RuntimeError("startup failed")
        async def close_candidate():
            events.append("close_candidate")
            if blocked:
                raise RuntimeError("cleanup pending")
        old.start_async = start_old
        candidate.start_async = start_candidate
        candidate.stop_async = close_candidate
        with pytest.raises(RuntimeError, match="startup failed"):
            await runtime.replace_endpoint_async(candidate)
        assert "restart_old" not in events
        assert runtime.owns_endpoint_transport("demo")
        with pytest.raises(RuntimeError, match="cleanup"):
            await runtime.replace_endpoint_async(_endpoint("demo"))
        blocked = False
        assert await runtime.remove_endpoint_async("demo")
        assert events.count("close_candidate") == 2
        assert not runtime.owns_endpoint_transport("demo")
    asyncio.run(scenario())


def test_cancelled_removal_waiter_does_not_duplicate_stop():
    async def scenario():
        runtime = ChannelRuntime()
        endpoint = _endpoint("demo")
        runtime.register_endpoint(endpoint)
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def stop():
            calls.append("stop")
            started.set()
            await release.wait()
        endpoint.stop_async = stop
        waiter = asyncio.create_task(runtime.remove_endpoint_async("demo"))
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        retry = asyncio.create_task(runtime.remove_endpoint_async("demo"))
        await asyncio.sleep(0)
        assert calls == ["stop"]
        release.set()
        assert await retry
        assert runtime.get_endpoint("demo") is None
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["prepare", "old_stop"])
def test_candidate_is_owned_when_failure_precedes_startup(failure):
    async def scenario():
        runtime = ChannelRuntime()
        old, candidate = _endpoint("demo"), _endpoint("demo")
        runtime.register_endpoint(old)
        await runtime.start_async()
        blocked = True
        calls = []
        async def fail(*args):
            raise RuntimeError("replacement failed")
        async def close():
            calls.append("close")
            if blocked:
                raise RuntimeError("cleanup failed")
        candidate.stop_async = close
        if failure == "prepare":
            candidate.prepare_replacement = fail
        else:
            old.stop_async = fail
        with pytest.raises(RuntimeError, match="replacement failed"):
            await runtime.replace_endpoint_async(candidate)
        assert runtime._pending_endpoint_cleanup["demo"][0] is candidate
        assert runtime.get_endpoint("demo") is old
        blocked = False
        async def stop_old():
            pass
        old.stop_async = stop_old
        await runtime.remove_endpoint_async("demo")
        assert calls == ["close", "close"]
        assert not runtime.owns_endpoint_transport("demo")
    asyncio.run(scenario())


def test_replacement_cancellation_waits_for_owned_startup_to_settle():
    async def scenario():
        runtime = ChannelRuntime()
        old, candidate = _endpoint("demo"), _endpoint("demo")
        runtime.register_endpoint(old)
        await runtime.start_async()
        started, release = asyncio.Event(), asyncio.Event()
        async def start():
            started.set()
            await release.wait()
        candidate.start_async = start
        task = asyncio.create_task(runtime.replace_endpoint_async(candidate))
        await started.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.get_endpoint("demo") is candidate
        assert not runtime._replacement_tasks
        await runtime.stop_async()
    asyncio.run(scenario())


def test_failed_initial_candidate_keeps_hub_until_cleanup_finishes():
    from pal.channel.lifecycle import EndpointHubInvariantError
    async def scenario():
        runtime = ChannelRuntime()
        recovery, candidate = _endpoint("recovery"), _endpoint("demo")
        runtime.register_endpoint(recovery)
        runtime.set_recovery_endpoint("recovery")
        runtime.ensure_endpoint_hub("demo", provider_id="provider")
        await runtime.start_async()
        blocked = True
        async def start():
            raise RuntimeError("start failed")
        async def stop():
            if blocked:
                raise RuntimeError("cleanup failed")
        candidate.start_async, candidate.stop_async = start, stop
        with pytest.raises(RuntimeError, match="start failed"):
            await runtime.replace_endpoint_async(candidate)
        assert runtime.get_endpoint("demo") is None
        with pytest.raises(EndpointHubInvariantError, match="cleanup"):
            runtime.remove_endpoint_hub("demo")
        blocked = False
        assert await runtime.remove_endpoint_async("demo")
        assert runtime.remove_endpoint_hub("demo")
        await runtime.stop_async()
    asyncio.run(scenario())


@pytest.mark.parametrize("inside_loop", [False, True])
def test_unstarted_sync_replacement_keeps_failed_preparation_cleanup(inside_loop):
    runtime = ChannelRuntime()
    old, candidate = _endpoint("demo"), _endpoint("demo")
    runtime.register_endpoint(old)
    blocked = True
    def prepare(previous):
        raise RuntimeError("prepare failed")
    async def stop():
        if blocked:
            raise RuntimeError("cleanup failed")
    candidate.prepare_replacement, candidate.stop_async = prepare, stop
    def replace():
        with pytest.raises(RuntimeError, match="prepare failed"):
            runtime.replace_endpoint(candidate)
        assert runtime._pending_endpoint_cleanup["demo"][0] is candidate
        assert runtime.get_endpoint("demo") is old
    async def scenario():
        nonlocal blocked
        if inside_loop:
            replace()
        blocked = False
        await runtime.remove_endpoint_async("demo")
        assert not runtime.owns_endpoint_transport("demo")
    if not inside_loop:
        replace()
    asyncio.run(scenario())


def test_unstarted_async_replacement_registers_transport_in_hub():
    async def scenario():
        runtime = ChannelRuntime()
        candidate = _endpoint("demo")
        runtime.ensure_endpoint_hub("demo")
        await runtime.replace_endpoint_async(candidate)
        assert runtime.get_endpoint("demo") is candidate
        assert runtime.inspect_endpoint_hub("demo")["transport_present"]
        await runtime.remove_endpoint_async("demo")
    asyncio.run(scenario())
