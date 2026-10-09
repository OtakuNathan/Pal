"""Admission leases survive cancellation until their blocking worker exits."""
import asyncio
import threading

import pytest

from pal.plugins.lifecycle import WriterPreferredRWGate


@pytest.mark.parametrize("mode", ["read", "write"])
def test_repeated_cancel_before_acquisition_drains_and_balances(mode, monkeypatch):
    gate = WriterPreferredRWGate()
    started = threading.Event()
    acquired = threading.Event()
    original = getattr(gate, "_acquire_" + mode)

    def acquire():
        started.set()
        original()
        acquired.set()

    monkeypatch.setattr(gate, "_acquire_" + mode, acquire)

    async def scenario():
        async def waiting():
            async with getattr(gate, mode + "_async")():
                pytest.fail("cancelled waiter entered its body")

        with gate.write():
            task = asyncio.create_task(waiting())
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel("original cancellation")
            await asyncio.sleep(0)
            task.cancel("repeated cancellation")
            await asyncio.sleep(0)
            completed_early = task.done()
        assert await asyncio.to_thread(acquired.wait, 2)
        result = (await asyncio.gather(task, return_exceptions=True))[0]
        # Inspect before cleanup so a broken gate cannot hang test teardown.
        leaked = (gate._readers, gate._writer, gate._waiting_writers)
        if gate._readers:
            gate._release_read()
        if gate._writer:
            gate._release_write()
        assert not completed_early, "cancelled waiter abandoned its acquisition worker"
        assert leaked == (0, False, 0)
        assert isinstance(result, asyncio.CancelledError)
        # gather synthesizes CancelledError; awaiting directly preserves its text.
        try:
            await task
        except asyncio.CancelledError as exc:
            assert exc.args == ("original cancellation",)
        async with gate.write_async():
            pass
        async with gate.read_async():
            pass

    asyncio.run(asyncio.wait_for(scenario(), 5))


@pytest.mark.parametrize("name", [
    "attach_channel_endpoint", "detach_channel_endpoint", "enable_channel_endpoint",
    "disable_channel_endpoint", "restart_channel_endpoint", "reload_channel_provider",
    "rescan_channel_providers", "op_channel_mgmt_attach", "op_channel_mgmt_detach",
    "op_channel_mgmt_enable", "op_channel_mgmt_disable", "op_channel_mgmt_restart_endpoint",
    "op_channel_mgmt_reload_provider", "op_channel_provider_rescan",
])
def test_channel_mutation_waits_for_admitted_execution_before_entering(name, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from types import SimpleNamespace
    from pal.execution.runtime import ExecutionRuntime
    runtime = ExecutionRuntime()
    attempted, entered = threading.Event(), threading.Event()
    def body(self, call):
        entered.set()
        return "done"
    monkeypatch.setattr(ExecutionRuntime, "_call_registered_unlocked", body)
    for mode in ("read", "write"):
        original = getattr(runtime.lifecycle_gate, mode)
        @contextmanager
        def acquire(original=original):
            attempted.set()
            with original():
                yield
        monkeypatch.setattr(runtime.lifecycle_gate, mode, acquire)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with runtime.lifecycle_gate.read():
            attempted.clear()
            future = executor.submit(runtime.call_registered, SimpleNamespace(name=name))
            assert attempted.wait(2)
            entered_while_reader_active = entered.wait(0.02)
        assert future.result(timeout=2) == "done"
    assert not entered_while_reader_active, "channel teardown ran concurrently with an admitted execution"
