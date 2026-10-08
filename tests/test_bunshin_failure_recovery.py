"""Failure propagation through the actual Bunshin model/session loop."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from unittest.mock import Mock, patch

import pytest

from pal.bunshin.runner import BunshinRunner
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
