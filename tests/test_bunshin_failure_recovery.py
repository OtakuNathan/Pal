"""Failure propagation through the actual Bunshin model/session loop."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from pal.bunshin.runner import BunshinRunner
from pal.bunshin import worker_main
from pal.bunshin.checkpoint import AgentSessionCheckpointError
from pal.bunshin.runner_components.models import _BunshinCooperativeCancel, _BunshinCooperativeRestart
from pal.bunshin.runner_components.reporter import Reporter
from pal.bunshin.runner_components.runtime_build import close_role_runtime
from pal.bunshin.semantic_orchestration.worker_results import _worker_terminal_failure
from pal.execution.runtime import ExecutionRuntime
from pal.llm import generation_result_from_values
from pal.shared.tool_protocol import ToolResultIR, new_tool_call
from tests.llm_fakes import NonStreamingLLM
from tests.test_bunshin_completion_resume import make_bundle, make_runner, noop


class CorrectingModel(NonStreamingLLM):
    def __init__(self, rejected_name, path):
        self.rejected_name = rejected_name
        self.path = path
        self.requests = []

    def generate(self, request, **options):
        self.requests.append(request)
        round_number = len(self.requests)
        if round_number <= 2:
            return generation_result_from_values(
                tool_calls=[new_tool_call(
                    name=self.rejected_name if round_number == 1 else "read_file",
                    args={"file_path": str(self.path)}, call_id=f"call-{round_number}",
                )], finish_reason="tool_calls",
            )
        return generation_result_from_values(text="Corrected and read the file.", finish_reason="stop")


def tool_results(request):
    return [
        part for message in request.messages for part in message.parts
        if isinstance(part, ToolResultIR)
    ]


@pytest.mark.parametrize("rejected_name", ["read_flie", "exec_shell"])
def test_admission_error_reaches_model_and_allows_corrected_call(tmp_path, rejected_name):
    async def scenario():
        path = tmp_path / "input.txt"
        path.write_text("correction succeeded")
        bundle = make_bundle()
        bundle.execution_runtime.mount_subtree(bundle.module_registry.require("execution"))
        bundle.llm_runtime = CorrectingModel(rejected_name, path)
        template = make_runner(tmp_path, output=tmp_path / "checkpoint.json")
        pack = replace(
            template.pack, allowed_capabilities=["op_file_read"],
            workspace={"run_dir": str(tmp_path)},
        )
        runner = BunshinRunner(
            runtime_root=tmp_path, pack=pack, bunshin_id=pack.invocation_id,
            run_id="admission-recovery", write_event=noop, read_decision=noop,
        )
        executed = []
        execute = ExecutionRuntime.execute_tool_async

        async def record_execution(runtime, call, **kwargs):
            executed.append(call.name)
            return await execute(runtime, call, **kwargs)

        try:
            with patch.object(ExecutionRuntime, "execute_tool_async", record_execution):
                await runner.components.invocation.run_invocation(bundle)
            requests = bundle.llm_runtime.requests
            assert len(requests) == 3
            rejection = next(result for result in tool_results(requests[1]) if result.call_id == "call-1")
            assert not rejection.ok
            assert "capability is not allowed" in rejection.content
            correction = next(result for result in tool_results(requests[2]) if result.call_id == "call-2")
            assert correction.ok
            assert "correction succeeded" in correction.content
            assert executed == ["read_file"]  # The rejected capability never executes.
            assert not runner.components.status.blocked_summary
        finally:
            bundle.execution_runtime.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["connection", "missing_gateway"])
def test_receipt_failure_reports_root_error_and_requests_reconciliation(tmp_path, failure):
    async def scenario():
        bundle = make_bundle()
        events = []

        async def record(event):
            events.append(event)

        template = make_runner(tmp_path, output=tmp_path / "checkpoint.json")
        pack = replace(template.pack, metadata={
            **template.pack.metadata,
            "bunshin_v2": {"submission_receipt_required": True},
        })
        runner = BunshinRunner(
            runtime_root=tmp_path, pack=pack, bunshin_id=pack.invocation_id,
            run_id="receipt-recovery", write_event=record, read_decision=noop,
            runtime_bundle=bundle,
        )
        client = Mock()
        client.request_sync.side_effect = ConnectionError("receipt-service-down")
        try:
            with patch("pal.bunshin.role_gateway_client.role_gateway_client_from_env", return_value=client if failure == "connection" else None):
                assert await runner.run() == 1
            terminal = [event["payload"] for event in events if event["event_kind"] == "terminal"][-1]
            assert terminal["status"] == "failed"
            assert terminal["retry_directive"] == "reconcile_first"
            assert terminal["error_type"] == ("ConnectionError" if failure == "connection" else "RuntimeError")
            assert ("receipt-service-down" if failure == "connection" else "role gateway is unavailable") in terminal["error"]
            assert "completion_gate_stalled" not in str(events)
            assert bundle.llm_runtime.calls == 0
            assert not runner.components.status.blocked_summary
        finally:
            bundle.execution_runtime.shutdown()

    asyncio.run(scenario())


def test_receipt_query_distinguishes_absence_and_caches_confirmed_receipt(tmp_path):
    runner = make_runner(tmp_path, output=tmp_path / "checkpoint.json")
    completion = runner.components.completion
    client = Mock()
    client.request_sync.side_effect = [{"recorded": False}, {"recorded": True}, ConnectionError("down")]
    with patch("pal.bunshin.role_gateway_client.role_gateway_client_from_env", return_value=client):
        assert not completion.manager_submission_receipt_present()
        assert completion.manager_submission_receipt_present()
        assert completion.manager_submission_receipt_present()
    assert client.request_sync.call_count == 2


class SingleToolModel(CorrectingModel):
    def generate(self, request, **options):
        self.requests.append(request)
        if len(self.requests) == 1:
            return generation_result_from_values(tool_calls=[new_tool_call(
                name="read_file", args={"file_path": str(self.path)}, call_id="call-1",
            )], finish_reason="tool_calls")
        return generation_result_from_values(text="done", finish_reason="stop")


@pytest.mark.parametrize("phase,tool_fails", [
    ("tool_call_started", False), ("tool_call_started", True),
    ("tool_call_completed", False), ("tool_call_failed", True),
    ("tool_call_waiting", False), ("tool_call_waiting", True),
])
def test_progress_failure_preserves_real_tool_result_in_next_model_request(tmp_path, phase, tool_fails, caplog):
    async def scenario():
        path = tmp_path / "input.txt"
        path.write_text("real tool content")
        bundle = make_bundle()
        bundle.execution_runtime.mount_subtree(bundle.module_registry.require("execution"))
        bundle.llm_runtime = SingleToolModel("read_file", path)
        template = make_runner(tmp_path, output=tmp_path / "checkpoint.json")
        pack = replace(template.pack, allowed_capabilities=["op_file_read"],
            workspace={"run_dir": str(tmp_path)}, metadata={
                **template.pack.metadata, "heartbeat_interval_seconds": .01,
            })
        progress_attempts = []
        ready = asyncio.Event()

        async def write(event):
            if event.get("payload", {}).get("phase") == phase:
                progress_attempts.append(event)
                ready.set()
                raise OSError("progress-transport-down")

        runner = BunshinRunner(runtime_root=tmp_path, pack=pack, bunshin_id=pack.invocation_id,
            run_id="progress-recovery", write_event=write, read_decision=noop)
        executed = []
        execute = ExecutionRuntime.execute_tool_async

        async def record_execution(runtime, call, **kwargs):
            executed.append(call.name)
            if phase == "tool_call_waiting":
                await asyncio.wait_for(ready.wait(), 5)
            if tool_fails:
                raise ValueError("original tool error")
            return await execute(runtime, call, **kwargs)

        try:
            with patch.object(ExecutionRuntime, "execute_tool_async", record_execution):
                await runner.components.agent_session.run_agent_loop(bundle)
            assert progress_attempts
            assert executed == ["read_file"]
            assert len(bundle.llm_runtime.requests) == 2
            result = next(item for item in tool_results(bundle.llm_runtime.requests[1]) if item.call_id == "call-1")
            assert result.ok is not tool_fails
            assert ("original tool error" if tool_fails else "real tool content") in result.content
            assert "progress-transport-down" not in result.content
            assert "Bunshin progress delivery failed" in caplog.text
        finally:
            bundle.execution_runtime.shutdown()

    asyncio.run(scenario())


def test_progress_delivery_preserves_task_cancellation(tmp_path):
    async def scenario():
        pack = make_runner(tmp_path, output=tmp_path / "checkpoint.json").pack
        reporter = Reporter("session", pack, "run", AsyncMock(side_effect=asyncio.CancelledError))
        with pytest.raises(asyncio.CancelledError):
            await reporter.emit_progress_best_effort("tool_call_waiting")
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["checkpoint", "runner", "cancel", "restart"])
def test_worker_wire_preserves_primary_terminal_when_cleanup_fails(tmp_path, failure, monkeypatch, capsys):
    bundle = make_bundle()
    template = make_runner(tmp_path, output=tmp_path / "checkpoint.json")
    errors = {
        "checkpoint": AgentSessionCheckpointError("original checkpoint error"),
        "runner": ValueError("original runner error"),
        "cancel": _BunshinCooperativeCancel({"summary": "original cancellation"}),
        "restart": _BunshinCooperativeRestart({"summary": "original restart"}),
    }
    original = errors[failure]

    async def write(event):
        print(json.dumps({"kind": "event", "event": event}))

    runner = BunshinRunner(runtime_root=tmp_path, pack=template.pack,
        bunshin_id=template.pack.invocation_id, run_id="cleanup-recovery",
        write_event=write, read_decision=noop, runtime_bundle=bundle)
    monkeypatch.setattr(runner.components.invocation, "run_invocation", AsyncMock(side_effect=original))
    monkeypatch.setattr(ExecutionRuntime, "close_role_work", AsyncMock(side_effect=OSError("cleanup failed")))
    bundle.close_async = AsyncMock()

    async def run(*args):
        return await runner.run()

    monkeypatch.setattr(worker_main, "_run", run)
    try:
        assert worker_main.main(["--runtime-root", str(tmp_path), "--pack-json", "unused",
            "--bunshin-id", "session", "--run-id", "run"]) == 1
        wire = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
        events = [item["event"] for item in wire if item["kind"] == "event"]
        terminal = next(event["payload"] for event in events if event["event_kind"] == "terminal")
        assert terminal["status"] == {"cancel": "killed", "restart": "suspended"}.get(failure, "failed")
        assert terminal["cleanup_error"]["error_type"] == "OSError"
        assert terminal["cleanup_error"]["error"] == "cleanup failed"
        assert wire[-1]["kind"] == "worker_error"
        assert "cleanup failed" in wire[-1]["error"]
        if failure in {"checkpoint", "runner"}:
            kind, error, retry = _worker_terminal_failure(events)
            assert str(original) in error
            assert terminal["error_type"] == type(original).__name__
            assert kind == ("invalid_agent_session_checkpoint" if failure == "checkpoint" else "runner_failure")
            assert retry == ("do_not_retry" if failure == "checkpoint" else "reconcile_first")
        else:
            assert str(original) in terminal["summary"]
        bundle.close_async.assert_awaited_once()
    finally:
        bundle.execution_runtime.shutdown()


@pytest.mark.parametrize("other_resources_fail", [False, True])
def test_memory_close_failure_still_closes_remaining_runtime_resources(other_resources_fail):
    async def scenario():
        memory_error = OSError("memory close failed")
        errors = {
            "memory": memory_error,
            **({
                "llm": RuntimeError("LLM close failed"),
                "async_module": RuntimeError("module shutdown failed"),
                "database": OSError("database close failed"),
            } if other_resources_fail else {}),
        }
        closed = []

        def close_resource(name):
            closed.append(name)
            if name in errors:
                raise errors[name]

        async def close_async_module():
            close_resource("async_module")

        context = SimpleNamespace(module_registry=SimpleNamespace(modules={
            "async": SimpleNamespace(shutdown_async=close_async_module),
            "sync": SimpleNamespace(shutdown_sync=lambda: close_resource("sync_module")),
        }))
        with pytest.raises(ExceptionGroup) as caught:
            await close_role_runtime(
                memory_repository_args={"repository": object()},
                l3_plugin=SimpleNamespace(repository=SimpleNamespace(close=lambda: close_resource("memory"))),
                llm_runtime=SimpleNamespace(close=lambda: close_resource("llm")),
                context=context,
                database=SimpleNamespace(close=lambda: close_resource("database")),
            )

        assert sorted(closed) == ["async_module", "database", "llm", "memory", "sync_module"]
        assert caught.value.exceptions == tuple(errors.values())

    asyncio.run(scenario())
