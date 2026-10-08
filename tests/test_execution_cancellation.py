"""Cancellation cannot let lifecycle cleanup overtake a synchronous handler."""
from __future__ import annotations

import asyncio
import inspect
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from pal.execution.contracts import CapabilityCall
from pal.execution.tool_facade import EffectOutcome, EffectReceipt, EmptyToolInput, EmptyToolOutput, ToolHandlerResult
from pal.execution.tool_semantics import DIRECT_LOCAL_READ, DIRECT_LOCAL_WRITE, INDIRECT_LOCAL_READ
from pal.shared.tool_protocol import new_tool_call
from tests.capability_fixture import mount_test_capability
from tests.test_tool_failure_affordances import runtime


def mount(runtime, alias, canonical_path, handler, *, indirect=False, execution=None):
    mount_test_capability(runtime, alias=alias, canonical_path=canonical_path,
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=handler,
        execution=execution or (INDIRECT_LOCAL_READ if indirect else DIRECT_LOCAL_READ))


async def invoke(runtime, entrypoint):
    if entrypoint == "registered":
        return await runtime.call_registered_async(
            CapabilityCall(name="op_test_cancelled_worker", args={}))
    call = new_tool_call(name="cancelled_worker", args={})
    if entrypoint == "indirect":
        call = new_tool_call(name="call_tool", args={"name": "cancelled_worker", "args": {}})
    return await runtime.execute_tool_async(call)


@pytest.mark.parametrize("entrypoint", ["direct", "indirect", "registered"])
@pytest.mark.parametrize("worker_fails", [False, True])
def test_cancelled_worker_finishes_before_plugin_cleanup(runtime, caplog, entrypoint, worker_fails):
    started = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    cleaned = threading.Event()
    order = []
    cleanup_while_active = []

    def worker(_):
        started.set()
        try:
            assert release.wait(5), "test worker was not released"
            if worker_fails:
                try:
                    raise ValueError("underlying worker failure")
                except ValueError as cause:
                    raise RuntimeError("late worker failure; token=CANCEL_TEST_SECRET") from cause
            return {}
        finally:
            order.append("worker_exit")
            exited.set()

    def cleanup(_):
        cleanup_while_active.append(not exited.is_set())
        order.append("plugin_cleanup")
        cleaned.set()
        return ToolHandlerResult(output={}, effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED))

    mount(runtime, "cancelled_worker", "op_test_cancelled_worker", worker,
          indirect=entrypoint == "indirect")
    mount(runtime, "reload_plugin", "op_plugin_mgmt_reattach", cleanup, execution=DIRECT_LOCAL_WRITE)

    async def scenario():
        task = asyncio.create_task(invoke(runtime, entrypoint))
        replacement = None
        try:
            assert await asyncio.to_thread(started.wait, 2)
            for reason in ("user cancelled the turn", "second cancellation", "third cancellation"):
                task.cancel(reason)
                await asyncio.sleep(0)
            replacement = asyncio.create_task(runtime.execute_tool_async(
                new_tool_call(name="reload_plugin", args={})))
            await asyncio.to_thread(cleaned.wait, 0.1)
        finally:
            release.set()
            assert await asyncio.to_thread(exited.wait, 2)
            try:
                old_result = await task
            except asyncio.CancelledError as exc:
                old_result = exc
            results = [old_result]
            if replacement is not None:
                results.extend(await asyncio.gather(replacement, return_exceptions=True))
        assert isinstance(results[0], asyncio.CancelledError), results
        assert results[0].args == ("user cancelled the turn",)
        assert replacement is not None and results[1].ok
        assert cleanup_while_active == [False]
        assert order == ["worker_exit", "plugin_cleanup"]

    asyncio.run(asyncio.wait_for(scenario(), timeout=6))
    if worker_fails:
        assert "underlying worker failure" in caplog.text
        assert "late worker failure" in caplog.text
        assert "CANCEL_TEST_SECRET" not in caplog.text


@pytest.mark.parametrize("entrypoint", ["direct", "registered"])
def test_cancelled_queued_worker_does_not_run_handler(runtime, monkeypatch, entrypoint):
    release = threading.Event()
    busy = threading.Event()
    submitted = threading.Event()
    invoked = threading.Event()
    executor = ThreadPoolExecutor(max_workers=1)
    previous_executor = runtime.sync_executor

    def occupy_executor():
        busy.set()
        assert release.wait(5)

    executor.submit(occupy_executor)
    assert busy.wait(2)
    original_submit = executor.submit

    def observe_submit(*args, **kwargs):
        result = original_submit(*args, **kwargs)
        submitted.set()
        return result

    monkeypatch.setattr(executor, "submit", observe_submit)
    runtime.sync_executor = executor
    mount(runtime, "cancelled_worker", "op_test_cancelled_worker",
          lambda _: invoked.set() or {})

    async def scenario():
        task = asyncio.create_task(invoke(runtime, entrypoint))
        try:
            assert await asyncio.to_thread(submitted.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
            results = await asyncio.gather(task, return_exceptions=True)
        assert isinstance(results[0], asyncio.CancelledError)
        assert not invoked.is_set()

    try:
        asyncio.run(asyncio.wait_for(scenario(), timeout=6))
    finally:
        release.set()
        executor.shutdown(wait=True)
        runtime.sync_executor = previous_executor


def test_cancelled_sync_handler_closes_unstarted_coroutine(runtime):
    started = threading.Event()
    release = threading.Event()
    returned = []

    async def deferred():
        raise AssertionError("cancelled coroutine should not execute")

    def worker(_):
        started.set()
        assert release.wait(5)
        result = deferred()
        returned.append(result)
        return result

    mount(runtime, "cancelled_worker", "op_test_cancelled_worker", worker)

    async def scenario():
        task = asyncio.create_task(invoke(runtime, "direct"))
        try:
            assert await asyncio.to_thread(started.wait, 2)
            task.cancel()
            await asyncio.sleep(0)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
        assert len(returned) == 1
        assert inspect.getcoroutinestate(returned[0]) == inspect.CORO_CLOSED

    try:
        asyncio.run(asyncio.wait_for(scenario(), timeout=6))
    finally:
        for result in returned:
            result.close()
