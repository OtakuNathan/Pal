"""Failed output contracts retain evidence without repeating tool execution."""
from pathlib import Path
import asyncio
import json

import pytest

from pal.core import PalCore
from pal.execution.contracts import ToolCallBudget
from pal.execution.tool_facade import InvocationMode, ToolHandlerResult, EffectReceipt, EffectOutcome
from pal.shared.tool_protocol import new_tool_call
from tests.capability_fixture import mount_test_capability
from tests.test_immutable_tool_facade import _echo_kwargs


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("snapshot_fails", [False, True])
def test_output_contract_failure_keeps_operation_guidance_in_unbudgeted_metadata(monkeypatch, asynchronous, snapshot_fails):
    from tests.test_result_guidance_budget import _core, _mount_boom

    core = _core()
    runtime = core.context.execution_runtime
    body = "\n".join(f"item_{i}: {'written; do not repeat' if i == 91 else 'skipped'}" for i in range(190))
    receipt = {"applied_indices": list(range(200)), "failed_indices": [200]}
    recovery = "Inspect every applied item before retrying. " * 50
    calls = []

    def handler(_):
        calls.append(1)
        return ToolHandlerResult(output={"invalid_output": True}, llm_text=body,
            effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED, receipt=receipt),
            recovery_hint=recovery)

    async def async_handler(value):
        return handler(value)

    _mount_boom(runtime, handler=async_handler if asynchronous else handler, mode=InvocationMode.INDIRECT)
    if snapshot_fails:
        def fail(*args, **kwargs):
            raise OSError("snapshot storage unavailable")
        monkeypatch.setattr(runtime.result_snapshots, "capture", fail)
    try:
        call = new_tool_call(name="call_tool", args={"name": "boom", "args": {}})
        budget = ToolCallBudget(max_output_chars=1, preview_chars=1)
        result = (asyncio.run(runtime.execute_tool_async(call, budget=budget)) if asynchronous
                  else runtime.execute_tool(call, budget=budget))
        info = json.loads(result.llm_text.rsplit("Tool result metadata: ", 1)[1])
        assert calls == [1]
        assert info["effect"] == "applied"
        assert info["operation_result"] == body
        assert info["effect_receipt"] == receipt
        assert recovery in info["recovery"]
        assert "Do not repeat side effects" in info["recovery"]
        assert "Traceback" not in result.llm_text
        if snapshot_fails:
            assert not result.snapshot_refs
    finally:
        core.close()


def test_output_contract_error_preserves_large_result_in_snapshot():
    core = PalCore()
    runtime = core.context.execution_runtime
    calls = []
    body = "RESULT_BEGIN" + "x" * 20000 + "RESULT_END"
    def handler(value):
        calls.append(value)
        return ToolHandlerResult(output={"wrong": "OUTPUT_EVIDENCE"}, llm_text=body)
    mount_test_capability(runtime, **_echo_kwargs(mode=InvocationMode.DIRECT, handler=handler))
    try:
        result = runtime.execute_tool(new_tool_call(name="echo", args={"value": "x"}),
            budget=ToolCallBudget(max_output_chars=2500, preview_chars=1000))
        assert not result.ok
        assert len(calls) == 1
        assert "output contract error" in result.llm_text
        assert "Traceback" not in result.llm_text
        assert "not the task arguments" in result.llm_text
        assert "Do not repeat side effects" in result.llm_text
        saved = Path(result.snapshot_refs[0].path).read_text()
        assert body in saved
        assert "OUTPUT_EVIDENCE" in saved
        assert "Field required" in saved
        assert "not a task argument error" in saved
    finally:
        runtime.shutdown()


def test_non_json_output_error_remains_deliverable():
    core = PalCore()
    runtime = core.context.execution_runtime
    mount_test_capability(runtime, **_echo_kwargs(mode=InvocationMode.DIRECT,
        handler=lambda _: ToolHandlerResult(output={"wrong": object()}, llm_text="original body")))
    try:
        result = runtime.execute_tool(new_tool_call(name="echo", args={"value": "x"}))
        assert not result.ok
        assert result.status == "output_validation_failed"
        saved = Path(result.snapshot_refs[0].path).read_text()
        assert "original body" in saved
        assert "wrong" in saved
        assert "output contract error" in result.llm_text
    finally:
        runtime.shutdown()
