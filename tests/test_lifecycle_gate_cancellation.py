"""Admission leases survive cancellation until their blocking worker exits."""
import asyncio
import threading

import pytest

from pal.foundation.lifecycle_gate import WriterPreferredRWGate


def test_nested_async_reader_can_finish_while_lifecycle_writer_waits(monkeypatch):
    gate = WriterPreferredRWGate()
    waiting = threading.Event()
    acquire = gate._acquire_write
    def write():
        waiting.set()
        acquire()
    monkeypatch.setattr(gate, "_acquire_write", write)

    async def scenario():
        async def retire():
            async with gate.write_async():
                return "retired"
        async with gate.read_async():
            writer = asyncio.create_task(retire())
            assert await asyncio.to_thread(waiting.wait, 2)
            async with gate.read_async():
                assert not writer.done()
        assert await writer == "retired"
        async with gate.write_async():
            pass
    asyncio.run(asyncio.wait_for(scenario(), 5))


def test_child_reader_retains_its_own_lease_after_parent_exits():
    gate = WriterPreferredRWGate()

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def child():
            async with gate.read_async():
                entered.set()
                await release.wait()
        async with gate.read_async():
            task = asyncio.create_task(child())
            await entered.wait()
        assert gate._readers == 1
        release.set()
        await task
        async with gate.write_async():
            pass
    asyncio.run(asyncio.wait_for(scenario(), 5))


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


@pytest.mark.parametrize("name", ["renamed_lifecycle_action", "another_alias"])
def test_channel_mutation_waits_for_admitted_execution_before_entering(name, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from pal.execution.runtime import ExecutionRuntime
    runtime = ExecutionRuntime()
    from pal.execution.tool_semantics import INDIRECT_LIFECYCLE
    attempted, entered = threading.Event(), threading.Event()
    def body(call):
        entered.set()
        return "done"
    from pal.execution.contracts import CapabilityCall
    from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput
    from tests.capability_fixture import mount_test_capability
    mount_test_capability(runtime, alias=name, canonical_path="op_test_lifecycle",
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=body,
        execution=INDIRECT_LIFECYCLE)
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
            future = executor.submit(runtime.call_registered, CapabilityCall(name="op_test_lifecycle", args={}))
            assert attempted.wait(2)
            entered_while_reader_active = entered.wait(0.02)
        assert future.result(timeout=2) == "done"
    assert not entered_while_reader_active, "channel teardown ran concurrently with an admitted execution"


def test_queued_registered_call_cannot_adopt_a_replacements_lock_policy(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from pal.execution.contracts import CapabilityCall
    from pal.execution.runtime import ExecutionRuntime
    from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput
    from pal.execution.tool_semantics import INDIRECT_NONE, INDIRECT_LIFECYCLE
    from tests.capability_fixture import mount_test_capability

    runtime = ExecutionRuntime()
    invoked = []
    waiting = threading.Event()
    options = dict(alias="replaceable", canonical_path="op_test_replaceable",
                   InputModel=EmptyToolInput, OutputModel=EmptyToolOutput)
    old = mount_test_capability(runtime, **options, execution=INDIRECT_NONE,
                               handler=lambda _: invoked.append("old"))
    original = runtime.lifecycle_gate.read
    @contextmanager
    def read():
        waiting.set()
        with original():
            yield
    monkeypatch.setattr(runtime.lifecycle_gate, "read", read)
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with runtime.lifecycle_gate.write():
                future = executor.submit(runtime.call_registered,
                    CapabilityCall(name="op_test_replaceable", args={}))
                assert waiting.wait(2)
                runtime.unmount_subtree(old)
                mount_test_capability(runtime, **options, execution=INDIRECT_LIFECYCLE,
                                      handler=lambda _: invoked.append("replacement"))
            assert future.result(timeout=2).status == "error"
        assert invoked == []
    finally:
        runtime.shutdown()
