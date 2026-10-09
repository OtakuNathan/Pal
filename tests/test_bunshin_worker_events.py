"""Worker IPC failures and backpressure stay outside model/tool execution."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import errno
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pal.bunshin import worker_main
from pal.bunshin.checkpoint import AgentSessionCheckpointError
from pal.bunshin.contracts import PermanentEffectError
from pal.bunshin.runner import BunshinRunner
from pal.bunshin.semantic_orchestration.attempt_process_result import ProcessResult
from pal.bunshin.semantic_orchestration.attempt_models import ExitedRoleProcess
from pal.bunshin.worker_events import JsonLinePipe, WorkerEventDeliveryError, WorkerEventWriter
from pal.execution.runtime import ExecutionRuntime
from pal.shared import BunshinInvocationPack
from tests.test_bunshin_completion_resume import make_bundle, make_runner, noop
from tests.test_bunshin_failure_recovery import SingleToolModel, tool_results


def progress(index):
    return {"event_kind": "progress", "payload": {"round": index}}


@pytest.mark.parametrize("tool_fails", [False, True])
@pytest.mark.parametrize("failure_phase", ["llm_round_started", "llm_round_completed", "invocation_finalizing"])
def test_queued_progress_failure_preserves_tool_result_in_next_model_request(tmp_path, tool_fails, failure_phase):
    async def scenario():
        path = tmp_path / "input.txt"
        path.write_text("real tool content")
        bundle = make_bundle()
        bundle.execution_runtime.mount_subtree(bundle.module_registry.require("execution"))
        bundle.llm_runtime = SingleToolModel("read_file", path)
        template = make_runner(tmp_path, output=tmp_path / "checkpoint.json")
        pack = replace(template.pack, allowed_capabilities=["op_file_read"], workspace={"run_dir": str(tmp_path)})
        failed = []

        async def write(line):
            value = json.loads(line)
            if value["event"]["payload"].get("phase") == failure_phase:
                failed.append(value)
                raise OSError("progress-pipe-down")

        executed = []
        execute = ExecutionRuntime.execute_tool_async

        async def run_tool(runtime, call, **kwargs):
            executed.append(call.name)
            if tool_fails:
                raise ValueError("original tool error")
            return await execute(runtime, call, **kwargs)

        try:
            async with WorkerEventWriter(write) as events:
                runner = BunshinRunner(tmp_path, pack, pack.invocation_id, "queued-progress", events.write_event, noop)
                with patch.object(ExecutionRuntime, "execute_tool_async", run_tool):
                    await runner.components.agent_session.run_agent_loop(bundle)
            assert failed
            assert executed == ["read_file"]
            assert len(bundle.llm_runtime.requests) == 2
            result = next(item for item in tool_results(bundle.llm_runtime.requests[1]) if item.call_id == "call-1")
            assert result.ok is not tool_fails
            assert ("original tool error" if tool_fails else "real tool content") in result.content
            assert "progress-pipe-down" not in result.content
        finally:
            bundle.execution_runtime.shutdown()

    asyncio.run(scenario())


def test_model_finishes_while_progress_sender_is_blocked(tmp_path):
    async def scenario():
        bundle = make_bundle()
        runner = make_runner(tmp_path, output=tmp_path / "checkpoint.json")
        blocked, release = asyncio.Event(), asyncio.Event()
        wire = []

        async def write(line):
            blocked.set()
            await release.wait()
            wire.append(json.loads(line))

        try:
            async with WorkerEventWriter(write) as events:
                runner.write_event = events.write_event
                runner.components.reporter.write_event = events.write_event
                try:
                    text = await asyncio.wait_for(runner.components.agent_session.run_agent_loop(bundle), 5)
                    await asyncio.wait_for(blocked.wait(), 5)
                    assert text == "local synthetic answer 1"
                    assert not wire
                finally:
                    release.set()
            assert any(item["event"]["payload"].get("phase") == "llm_round_completed" for item in wire)
        finally:
            bundle.execution_runtime.shutdown()

    asyncio.run(scenario())


def test_queue_capacity_applies_backpressure_and_preserves_order():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        wire = []

        async def write(line):
            entered.set()
            await release.wait()
            wire.append(json.loads(line)["event"]["payload"]["round"])

        async with WorkerEventWriter(write, capacity=1) as events:
            await events.write_event(progress(1))
            await entered.wait()
            await events.write_event(progress(2))
            producer = asyncio.create_task(events.write_event(progress(3)))
            try:
                await asyncio.sleep(0)
                assert not producer.done()
            finally:
                release.set()
                await asyncio.wait_for(producer, 5)
        assert wire == [1, 2, 3]

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["terminal", "approval_requested", "clarification_requested"])
def test_required_event_waits_for_delivery_and_reports_failure(kind):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def write(line):
            entered.set()
            await release.wait()
            raise BrokenPipeError("Manager closed stdout")

        async with WorkerEventWriter(write) as events:
            producer = asyncio.create_task(events.write_event({"event_kind": kind}))
            await entered.wait()
            assert not producer.done()
            release.set()
            with pytest.raises(WorkerEventDeliveryError, match="Manager closed stdout"):
                await producer

    asyncio.run(scenario())


@pytest.mark.parametrize("confirmed", [False, True])
def test_close_unblocks_full_queue_producers_and_reaps_sender(confirmed):
    async def scenario():
        entered = asyncio.Event()

        async def write(line):
            entered.set()
            await asyncio.Event().wait()

        events = WorkerEventWriter(write, capacity=1, close_timeout_seconds=.05)
        await events.__aenter__()
        await events.write_event(progress(1))
        await entered.wait()
        await events.write_event(progress(2))
        producer = asyncio.create_task(events.write_event({"event_kind": "terminal"} if confirmed else progress(3)))
        await asyncio.sleep(0)
        with pytest.raises(WorkerEventDeliveryError, match="finish flushing"):
            await events.close()
        if confirmed:
            with pytest.raises(WorkerEventDeliveryError, match="closing"):
                await asyncio.wait_for(producer, 5)
        else:
            await asyncio.wait_for(producer, 5)
        await events.close()
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    asyncio.run(scenario())


def test_cancelled_delivery_does_not_leave_an_unobserved_future():
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def write(line):
            entered.set()
            await release.wait()
            raise OSError("late delivery failure")

        async with WorkerEventWriter(write) as events:
            producer = asyncio.create_task(events.write_event({"event_kind": "terminal"}))
            await entered.wait()
            producer.cancel()
            with pytest.raises(asyncio.CancelledError):
                await producer
            release.set()

    asyncio.run(scenario())


def test_close_failure_reports_delivery_error_to_waiting_terminal():
    async def scenario():
        entered = asyncio.Event()

        async def write(line):
            entered.set()
            await asyncio.Event().wait()

        events = WorkerEventWriter(write, close_timeout_seconds=.05)
        await events.__aenter__()
        producer = asyncio.create_task(events.write_event({"event_kind": "terminal"}))
        await entered.wait()
        with pytest.raises(WorkerEventDeliveryError, match="finish flushing"):
            await events.close()
        with pytest.raises(WorkerEventDeliveryError, match="stopped before delivery"):
            await producer

    asyncio.run(scenario())


def test_sender_cancellation_fails_required_delivery_without_stalling_producers():
    async def scenario():
        stopped = asyncio.Event()

        async def write(line):
            stopped.set()
            raise asyncio.CancelledError

        events = WorkerEventWriter(write)
        await events.__aenter__()
        await events.write_event(progress(1))
        await stopped.wait()
        with pytest.raises(WorkerEventDeliveryError, match="closed"):
            await asyncio.wait_for(events.write_event({"event_kind": "terminal"}), 5)
        with pytest.raises(WorkerEventDeliveryError, match="sender was cancelled"):
            await events.close()

    asyncio.run(scenario())


def test_flush_failure_does_not_replace_primary_error():
    async def scenario():
        async def write(line):
            await asyncio.Event().wait()

        with pytest.raises(ValueError, match="primary failure"):
            async with WorkerEventWriter(write, close_timeout_seconds=.05) as events:
                await events.write_event(progress(1))
                raise ValueError("primary failure")

    asyncio.run(scenario())


def test_native_pipe_backpressure_keeps_loop_responsive_and_flushes_complete_frame():
    async def scenario():
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(write_fd, "w")
        pipe = JsonLinePipe(stream)
        line = (json.dumps({"text": "汉" * 100_000}, ensure_ascii=False) + "\n").encode()
        transport = None
        producer = asyncio.create_task(pipe.write(line))
        try:
            await asyncio.sleep(0)
            assert not producer.done()
            reader = asyncio.StreamReader()
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, "rb", buffering=0),
            )
            received = await asyncio.wait_for(reader.readexactly(len(line)), 5)
            await asyncio.wait_for(producer, 5)
            assert received == line
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            pipe.close()
            assert os.get_blocking(stream.fileno())
            stream.close()
            if transport is not None:
                transport.close()
            else:
                os.close(read_fd)

    asyncio.run(scenario())


def test_native_pipe_retries_interruption_and_partial_writes_without_duplicate_frames(monkeypatch):
    async def scenario():
        read_fd, write_fd = os.pipe()
        with os.fdopen(write_fd, "w") as stream:
            pipe = JsonLinePipe(stream)
            write = os.write
            calls = 0

            def short_write(fd, data):
                nonlocal calls
                if fd != pipe.fd:
                    return write(fd, data)
                calls += 1
                if calls == 1:
                    raise InterruptedError(errno.EINTR, "interrupted")
                if calls == 2:
                    raise BlockingIOError(errno.EAGAIN, "full")
                return write(fd, data[:7])

            monkeypatch.setattr(os, "write", short_write)
            line = b'{"kind":"event","event":{"event_kind":"terminal"}}\n'
            try:
                await asyncio.wait_for(pipe.write(line), 5)
                assert os.read(read_fd, len(line)) == line
                assert calls > 3
            finally:
                pipe.close()
                os.close(read_fd)

    asyncio.run(scenario())


def test_interrupted_frame_cannot_be_followed_by_another_frame():
    async def scenario():
        read_fd, write_fd = os.pipe()
        with os.fdopen(write_fd, "w") as stream:
            pipe = JsonLinePipe(stream)
            producer = asyncio.create_task(pipe.write(b"x" * 1_000_000))
            try:
                await asyncio.sleep(0)
                assert not producer.done()
                producer.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await producer
                with pytest.raises(WorkerEventDeliveryError, match="interrupted frame"):
                    await pipe.write(b"another frame\n")
            finally:
                pipe.close()
                os.close(read_fd)

    asyncio.run(scenario())


@pytest.mark.parametrize("message", ["primary failure", "filename-\udcff"])
def test_worker_main_flushes_progress_terminal_and_primary_worker_error(monkeypatch, capsys, message):
    async def run(*args):
        write_event = args[-1]
        await write_event({"event_kind": "progress", "payload": {"phase": "llm_round_completed"}})
        await write_event({"event_kind": "terminal", "payload": {"status": "failed", "error": message}})
        raise ValueError(message)

    monkeypatch.setattr(worker_main, "_run", run)
    assert worker_main.main(["--runtime-root", "unused", "--pack-json", "unused",
        "--bunshin-id", "session", "--run-id", "run"]) == 1
    wire = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [item["event"]["event_kind"] for item in wire[:-1]] == ["progress", "terminal"]
    assert wire[-1]["kind"] == "worker_error"
    assert wire[-1]["error"].endswith(f"ValueError: {message.replace(chr(0xdcff), chr(0xfffd))}")
    assert "Traceback" in wire[-1]["error"]
    wire[-1]["error"].encode("utf-8")
    assert wire[-1]["failure_diagnostic"]["error_type"] == "ValueError"


@pytest.mark.parametrize("message", ["primary failure", "filename-\udcff"])
def test_closed_stdout_preserves_primary_worker_error_on_stderr(monkeypatch, message):
    class ClosedOutput(io.StringIO):
        def write(self, text):
            raise BrokenPipeError("stdout closed")

    async def run(*args):
        raise ValueError(message)

    errors = io.StringIO()
    monkeypatch.setattr(worker_main, "_run", run)
    monkeypatch.setattr("sys.stdout", ClosedOutput())
    monkeypatch.setattr("sys.stderr", errors)
    assert worker_main.main(["--runtime-root", "unused", "--pack-json", "unused",
        "--bunshin-id", "session", "--run-id", "run"]) == 1
    fallback = json.loads(errors.getvalue().splitlines()[-1])
    assert fallback["kind"] == "worker_event_fallback"
    wire = fallback["message"]
    assert wire["kind"] == "worker_error"
    assert wire["error"].endswith(f"ValueError: {message.replace(chr(0xdcff), chr(0xfffd))}")
    assert "Traceback" in wire["error"]
    wire["error"].encode("utf-8")


@pytest.mark.parametrize("permanent", [False, True])
def test_runner_suppressed_terminal_write_failure_retains_root_and_retry_policy(tmp_path, monkeypatch, permanent):
    class ClosedOutput(io.StringIO):
        def write(self, text):
            raise BrokenPipeError("stdout closed")

    bundle = make_bundle()
    original = (AgentSessionCheckpointError if permanent else ValueError)("original runner failure")
    errors = io.StringIO()
    errors.write("library log without a newline")
    monkeypatch.setattr("sys.stdout", ClosedOutput())
    monkeypatch.setattr("sys.stderr", errors)

    async def run(*args):
        runner = make_runner(tmp_path, output=tmp_path / "checkpoint.json", write_event=args[-1])
        runner.runtime_bundle = bundle
        monkeypatch.setattr(runner.components.invocation, "run_invocation", AsyncMock(side_effect=original))
        return await runner.run()

    monkeypatch.setattr(worker_main, "_run", run)
    try:
        assert worker_main.main(["--runtime-root", "unused", "--pack-json", "unused",
            "--bunshin-id", "session", "--run-id", "run"]) == 1
        repository = MagicMock()
        repository.role_assignments.read_role_assignment.return_value = {}
        checkpoints = MagicMock()
        checkpoints.publish_agent_session_checkpoint.return_value = None
        command = SimpleNamespace(fencing_token=1, invocation_id="session")
        admission = SimpleNamespace(assignment_lease=SimpleNamespace(fencing_token=1),
            assignment_lease_resource="lease", attempt={"attempt_id": "attempt"})
        exited = ExitedRoleProcess(events=[], worker_error="", owner=SimpleNamespace(
            returncode=1, stderr=errors.getvalue().encode("utf-8")))
        with pytest.raises(PermanentEffectError if permanent else RuntimeError) as caught:
            asyncio.run(ProcessResult(repository, checkpoints).execute(
                command, admission, SimpleNamespace(continuation_output_path=None),
                SimpleNamespace(pack=None), SimpleNamespace(pal_checkpoint_capable=False),
                SimpleNamespace(assignment={"assignment_id": "assignment"}), exited,
            ))
        assert "original runner failure" in str(caught.value)
        assert "worker_diagnostic=" in str(caught.value)
        if permanent:
            repository.role_retries.queue_role_attempt_retry.assert_not_called()
        else:
            queued = repository.role_retries.queue_role_attempt_retry.call_args.kwargs
            assert queued["error_kind"] == "runner_failure"
            assert "original runner failure" in queued["error_text"]
    finally:
        bundle.execution_runtime.shutdown()


def test_stdout_terminal_policy_takes_precedence_over_stderr_fallback():
    fallback = {"kind": "worker_event_fallback", "message": {"kind": "event", "event": {
        "event_kind": "terminal", "payload": {"status": "failed", "error": "older fallback",
            "error_kind": "invalid_agent_session_checkpoint", "retry_directive": "do_not_retry"},
    }}}
    repository = MagicMock()
    repository.role_assignments.read_role_assignment.return_value = {}
    checkpoints = MagicMock()
    checkpoints.publish_agent_session_checkpoint.return_value = None
    admission = SimpleNamespace(assignment_lease=SimpleNamespace(fencing_token=1),
        assignment_lease_resource="lease", attempt={"attempt_id": "attempt"})
    exited = ExitedRoleProcess(events=[{"event_kind": "terminal", "payload": {
        "status": "failed", "error": "stdout primary", "error_kind": "runner_failure",
        "retry_directive": "reconcile_first",
    }}], worker_error="", owner=SimpleNamespace(returncode=1, stderr=json.dumps(fallback).encode()))
    with pytest.raises(RuntimeError, match="stdout primary"):
        asyncio.run(ProcessResult(repository, checkpoints).execute(
            SimpleNamespace(fencing_token=1, invocation_id="session"), admission,
            SimpleNamespace(continuation_output_path=None), SimpleNamespace(pack=None),
            SimpleNamespace(pal_checkpoint_capable=False), SimpleNamespace(assignment={"assignment_id": "a"}), exited,
        ))
    assert repository.role_retries.queue_role_attempt_retry.call_args.kwargs["error_kind"] == "runner_failure"


@pytest.mark.parametrize("has_receipt", [False, True])
@pytest.mark.parametrize("payload", [
    {"status": "suspended", "manager_restart": True, "summary": "restart safe point"},
    {"status": "killed", "summary": "cancelled by user"},
])
def test_zero_exit_control_terminal_is_recovered_without_overriding_submission_receipt(payload, has_receipt):
    fallback = {"kind": "worker_event_fallback", "message": {"kind": "event", "event": {
        "event_kind": "terminal", "payload": payload,
    }}}
    repository = MagicMock()
    repository.role_assignments.read_role_assignment.return_value = {
        "submission_artifact_ref": {"sha256": "receipt"} if has_receipt else {},
    }
    checkpoints = MagicMock()
    receipt_terminal = {"event_kind": "terminal", "payload": {"status": "completed", "summary": "durable receipt"}}
    checkpoints.terminal_from_assignment_receipt.return_value = receipt_terminal
    admission = SimpleNamespace(assignment_lease=SimpleNamespace(fencing_token=1),
        assignment_lease_resource="lease", attempt={"attempt_id": "attempt"})
    exited = ExitedRoleProcess(events=[], worker_error="",
        owner=SimpleNamespace(returncode=0, stderr=json.dumps(fallback).encode()))
    result = asyncio.run(ProcessResult(repository, checkpoints).execute(
        SimpleNamespace(fencing_token=1, invocation_id="session"), admission,
        SimpleNamespace(continuation_output_path=None), SimpleNamespace(pack=BunshinInvocationPack(
            invocation_id="session", workspace={"output_policy": {"primary_artifact": "result.json"}},
        )),
        SimpleNamespace(pal_checkpoint_capable=True), SimpleNamespace(assignment={"assignment_id": "a"}), exited,
    ))
    assert result.terminal_payload == (receipt_terminal["payload"] if has_receipt else payload)
    assert checkpoints.terminal_from_assignment_receipt.call_count == int(has_receipt)
    repository.role_retries.queue_role_attempt_retry.assert_not_called()


@pytest.mark.parametrize("fails", [False, True])
def test_real_harness_entrypoint_flushes_native_stdout_before_exit(fails):
    module = "pal.bunshin.worker_main"
    script = f'''
import importlib, sys
module = importlib.import_module({module!r})
async def run(*args):
    write = args[-1]
    for index in range(50):
        await write({{"event_kind": "progress", "payload": {{"round": index}}}})
    await write({{"event_kind": "terminal", "payload": {{"status": "completed"}}}})
    if {fails!r}:
        raise ValueError("primary harness failure")
    return 0
module._run = run
sys.argv = ["worker", "--runtime-root", "unused", "--pack-json", "unused",
            "--bunshin-id", "session", "--run-id", "run"]
sys.exit(module.main())
'''
    completed = subprocess.run([sys.executable, "-c", script], capture_output=True,
        text=True, timeout=30, cwd=Path(__file__).resolve().parents[1])
    assert completed.returncode == (1 if fails else 0), completed.stderr
    wire = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [item["event"]["payload"]["round"] for item in wire[:50]] == list(range(50))
    assert wire[50]["event"]["event_kind"] == "terminal"
    assert len(wire) == (52 if fails else 51)
    if fails:
        assert wire[-1]["error"].endswith("ValueError: primary harness failure")
        assert "Traceback" in wire[-1]["error"]
