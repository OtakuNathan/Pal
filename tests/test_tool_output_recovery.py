"""Failed output contracts retain evidence without repeating tool execution."""
from pathlib import Path

from pal.core import PalCore
from pal.execution.contracts import ToolCallBudget
from pal.execution.tool_facade import InvocationMode, ToolHandlerResult
from pal.shared.tool_protocol import new_tool_call
from tests.capability_fixture import mount_test_capability
from tests.test_immutable_tool_facade import _echo_kwargs


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
        assert "original body" in result.llm_text
        assert "wrong" in result.llm_text
    finally:
        runtime.shutdown()
