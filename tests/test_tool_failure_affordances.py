"""Exercise the final tool text and receipts, including wrapper and delivery paths."""
from __future__ import annotations

import asyncio
import json
import shlex
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pal.core import PalCore
from pal.core.turns import ToolCallEffect
from pal.execution import register_with_core
from pal.execution.contracts import CapabilityCall, CapabilityResult, ToolCallBudget
from pal.execution.file_state import FileContentChangedError
from pal.execution.tool_facade import EffectOutcome, EffectReceipt, EmptyToolInput, EmptyToolOutput, RetryDirective, ToolExecutionError, ToolHandlerResult, ToolRejectedError
from pal.execution.tool_semantics import DIRECT_LOCAL_READ, DIRECT_LOCAL_WRITE, DIRECT_NONE, INDIRECT_EXTERNAL_READ, INDIRECT_LOCAL_READ
from pal.shared import RuntimeStatus, ToolExecutionResult
from pal.shared.tool_protocol import new_tool_call
from pal.web_fetch.browser_service import BrowserServiceError
from pal.web_fetch.capabilities import WebFetchIntrospectionProvider
from tests.capability_fixture import mount_test_capability
from tests.turn_fakes import continuation


@pytest.fixture
def runtime(tmp_path):
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities("execution")
    runtime = core.context.execution_runtime
    runtime.configure_runtime_root(tmp_path / "runtime")
    runtime.begin_tool_result_turn(turn_id="review", scope_key="failure-review")
    yield runtime
    runtime.shutdown()


def invoke(runtime, alias, args, *, asynchronous=False):
    call = new_tool_call(name=alias, args=args)
    if alias in runtime.registry_generation.indirect_aliases:
        call = new_tool_call(name="call_tool", args={"name": alias, "args": args})
    if asynchronous:
        return asyncio.run(runtime.execute_tool_async(call, turn_id="review"))
    return runtime.execute_tool(call, turn_id="review")


def metadata(result):
    return json.loads(result.llm_text.rsplit("Tool result metadata: ", 1)[1])


def read(runtime, path, **args):
    result = invoke(runtime, "read_file", {"file_path": str(path), **args})
    assert result.ok, result.llm_text
    runtime.commit_tool_delivery(turn_id="review", result_id=result.call_id,
                                 context_delivery=dict(result.context_delivery or {}))
    return result


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("directory,code", [(False, "FILE_NOT_FOUND"), (True, "NOT_A_FILE")])
def test_read_failure_has_no_effect(runtime, tmp_path, asynchronous, directory, code):
    result = invoke(runtime, "read_file", {"file_path": str(tmp_path if directory else tmp_path / "absent")},
                    asynchronous=asynchronous)
    assert not result.ok
    assert metadata(result)["effect"] == "none"
    assert metadata(result)["error_code"] == code
    assert "correct the path" in metadata(result)["recovery"].lower()


def test_decode_failure_preserves_actual_error(runtime, tmp_path):
    path = tmp_path / "invalid-utf8"
    path.write_bytes(b"text\xff")
    result = invoke(runtime, "read_file", {"file_path": str(path)})
    assert not result.ok
    assert "UnicodeDecodeError" in result.llm_text
    assert "position 4" in result.llm_text
    assert metadata(result)["error_code"] == "UNSUPPORTED_TEXT_ENCODING"
    assert metadata(result)["effect"] == "none"


def test_read_os_failure_preserves_complete_cause(runtime, tmp_path, monkeypatch):
    path = tmp_path / "source"
    path.write_text("text")
    def fail(_):
        try:
            raise ValueError("underlying read cause")
        except ValueError as cause:
            raise OSError("read failure " + "x" * 5000 + " diagnostic-tail") from cause
    monkeypatch.setattr("pal.execution.file_read.read_utf8_text_exact", fail)
    result = invoke(runtime, "read_file", {"file_path": str(path)})
    assert not result.ok
    assert "underlying read cause" in result.llm_text and "diagnostic-tail" in result.llm_text
    assert metadata(result)["error_code"] == "READ_FAILED"
    assert metadata(result)["effect"] == "none"


@pytest.mark.parametrize("case,code", [
    ("unread", "NOT_READ"), ("partial", "PARTIAL_READ"), ("stale", "STALE_FILE"),
    ("missing_match", "NOT_FOUND_MATCH"), ("ambiguous", "MULTIPLE_MATCHES"),
])
def test_edit_preconditions_reject_before_effect(runtime, tmp_path, case, code):
    path = tmp_path / "source"
    path.write_text("alpha\nbeta\nalpha\n")
    if case != "unread":
        read(runtime, path, **({"limit": 1} if case == "partial" else {}))
    if case == "stale":
        path.write_text("external change\n")
    before = path.read_bytes()
    old = "alpha" if case == "ambiguous" else "absent" if case == "missing_match" else "beta"
    result = invoke(runtime, "edit_file", {"file_path": str(path), "edits": [{"old_string": old, "new_string": "new"}]})
    assert not result.ok
    info = metadata(result)
    assert (info["kind"], info["effect"], info["retry"], info["error_code"]) == (
        "rejected", "not_started", "correct_input", code)
    assert path.read_bytes() == before
    assert "WRITE_FAILED" not in info["recovery"]


def test_partial_edit_leads_with_failure_and_recovers_only_failed_items(runtime, tmp_path):
    path = tmp_path / "source"
    path.write_text("alpha\nbeta\n")
    read(runtime, path)
    result = invoke(runtime, "edit_file", {"file_path": str(path), "edits": [
        {"old_string": "alpha", "new_string": "changed"},
        {"old_string": "absent", "new_string": "unused"},
    ]})
    assert result.ok
    assert result.llm_text.startswith("Applied 1 of 2 edits; edit 1 failed")
    assert metadata(result)["effect"] == "applied"
    assert "do not resubmit applied edits" in metadata(result)["recovery"]
    assert result.structured["applied_edit_indices"] == [0]
    assert result.structured["failed_edits"][0]["error_code"] == "NOT_FOUND_MATCH"
    assert path.read_text() == "changed\nbeta\n"


@pytest.mark.parametrize("case,code", [("stale", "STALE_FILE"), ("parent_file", "PARENT_NOT_DIRECTORY")])
def test_write_preconditions_reject_before_effect(runtime, tmp_path, case, code):
    path = tmp_path / "source"
    path.write_text("original")
    if case == "stale":
        read(runtime, path)
        path.write_text("external")
    else:
        path = path / "child"
    result = invoke(runtime, "write_file", {"file_path": str(path), "content": "new"})
    assert not result.ok
    assert (metadata(result)["effect"], metadata(result)["error_code"]) == ("not_started", code)


def test_parent_creation_before_stale_write_is_not_reported_as_not_started(runtime, tmp_path, monkeypatch):
    path = tmp_path / "new-parent" / "source"
    def changed(*args, **kwargs):
        raise FileContentChangedError("changed at commit")
    monkeypatch.setattr("pal.execution.file_write.atomic_compare_and_swap_utf8", changed)
    result = invoke(runtime, "write_file", {"file_path": str(path), "content": "new"})
    assert path.parent.is_dir()
    assert metadata(result)["kind"] == "failed"
    assert metadata(result)["effect"] == "unknown"
    assert metadata(result)["error_code"] == "STALE_FILE"


@pytest.mark.parametrize("directory", [False, True])
def test_delete_refusal_preserves_path_and_explains_intent(runtime, tmp_path, directory):
    path = tmp_path / "keep"
    if directory:
        path.mkdir()
        args = {"file_path": str(path)}
        code = "DIRECTORY_REQUIRES_RECURSIVE"
    else:
        path.write_text("keep")
        args = {"file_path": str(path), "expected_sha256": "0" * 64}
        code = "SHA256_MISMATCH"
    result = invoke(runtime, "delete_path", args)
    assert path.exists()
    assert metadata(result)["error_code"] == code
    assert metadata(result)["effect"] == "not_started"
    if directory:
        assert "only if" in result.llm_text


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("alias,args", [
    ("read_file", {"path": "wrong"}),
    ("read_file", {"file_path": "unused", "offset": 0}),
    ("delete_path", {"path": "wrong"}),
])
def test_validation_is_compact_and_has_metadata_through_both_modes(runtime, asynchronous, alias, args):
    result = invoke(runtime, alias, args, asynchronous=asynchronous)
    assert not result.ok
    info = metadata(result)
    assert (info["kind"], info["error_code"], info["effect"], info["retry"]) == (
        "rejected", "invalid_arguments", "not_started", "correct_input")
    assert "Accepted fields:" in result.llm_text
    assert "errors.pydantic.dev" not in result.llm_text
    assert "ExecutionFileCapabilities" not in result.llm_text
    assert "read_tool" not in result.llm_text
    assert "recovery" not in info


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("direct", [False, True])
def test_unexpected_exception_is_complete_redacted_and_read_label_is_not_a_receipt(runtime, asynchronous, direct):
    def fail(_):
        raise KeyError("missing configuration; token=hidden " + "x" * 5000 + " diagnostic-tail")
    mount_test_capability(runtime, alias="broken_reader", canonical_path="op_test_broken_reader",
                          InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=fail,
                          execution=DIRECT_LOCAL_READ if direct else INDIRECT_LOCAL_READ)
    result = invoke(runtime, "broken_reader", {}, asynchronous=asynchronous)
    assert not result.ok
    assert "missing configuration" in result.llm_text
    assert "hidden" not in result.llm_text
    assert "diagnostic-tail" in result.llm_text
    assert "Traceback" not in result.llm_text
    from pathlib import Path
    assert "Traceback" in Path(result.snapshot_refs[0].path).read_text()
    assert metadata(result)["error_code"] == "handler_exception"
    assert metadata(result)["effect"] == "unknown"


def test_long_exception_budget_retains_complete_diagnostic_in_readable_snapshot(runtime):
    def fail(_):
        try:
            raise ValueError("original cause")
        except ValueError as cause:
            raise RuntimeError("token=hidden " + "x" * 6000 + " diagnostic-tail") from cause
    mount_test_capability(runtime, alias="long_failure", canonical_path="op_test_long_failure",
                          InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=fail,
                          execution=DIRECT_LOCAL_READ)
    result = runtime.execute_tool(new_tool_call(name="long_failure", args={}), turn_id="review",
        budget=ToolCallBudget(max_output_chars=1200, preview_chars=400))
    assert not result.ok
    assert result.snapshot_refs
    from pathlib import Path
    complete = Path(result.snapshot_refs[0].path).read_text()
    assert "original cause" in complete and "diagnostic-tail" in complete
    assert "hidden" not in complete
    assert "Traceback" in complete
    assert result.snapshot_refs[0].path in result.llm_text
    assert metadata(result)["error_code"] == "handler_exception"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_capability_entrypoint_preserves_full_declared_failure(runtime, asynchronous):
    def fail(_):
        raise ToolExecutionError("original cause " + "x" * 5000 + " diagnostic-tail",
            error_code="specific_failure", effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_APPLIED),
            recovery_hint="repair the specific cause", retry=RetryDirective.DO_NOT_RETRY,
            details={"item": "original item"})
    mount_test_capability(runtime, alias="declared_failure", canonical_path="op_test_declared_failure",
                          InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=fail,
                          execution=INDIRECT_LOCAL_READ)
    call = CapabilityCall(name="declared_failure")
    result = asyncio.run(runtime.execute_async(call)) if asynchronous else runtime.execute(call)
    assert "original cause" in result.llm_text and "diagnostic-tail" in result.llm_text
    assert result.structured["error_code"] == "specific_failure"
    assert result.structured["retry"] == "do_not_retry"
    assert result.structured["item"] == "original item"
    assert "original item" in result.llm_text
    assert result.effect_receipt.outcome is EffectOutcome.NOT_APPLIED
    assert result.recovery_hint == "repair the specific cause"


def test_handler_summary_cannot_hide_structured_error_details(runtime):
    def fail(_):
        return CapabilityResult(status=RuntimeStatus.ERROR, text="operation failed", llm_text="operation failed",
            structured={"error_code": "backend_error", "error": {
                "code": "backend_error", "message": "actual underlying cause", "retryable": False,
            }, "stderr": "complete backend diagnostic"})
    mount_test_capability(runtime, alias="hidden_diagnostic", canonical_path="op_test_hidden_diagnostic",
                          InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=fail,
                          execution=INDIRECT_LOCAL_READ)
    result = invoke(runtime, "hidden_diagnostic", {})
    assert result.llm_text.startswith("operation failed")
    assert "actual underlying cause" in result.llm_text
    assert "complete backend diagnostic" in result.llm_text
    assert '"retryable":false' in result.llm_text
    assert metadata(result)["error_code"] == "backend_error"


def test_builtin_output_failure_retains_original_error_and_output(runtime, monkeypatch):
    def fail(*args):
        raise TypeError("invalid output " + "x" * 5000 + " diagnostic-tail")
    monkeypatch.setattr("pal.execution.runtime.validate_output", fail)
    record = runtime.registry_generation.direct_aliases["search_tools"]
    result = runtime._complete_builtin(record, {"original_output": "complete output"}, llm_text="original summary")
    assert result.error_code == "output_validation_failed"
    assert "diagnostic-tail" in result.llm_text
    assert "complete output" in result.llm_text and "original summary" in result.llm_text
    assert result.details["raw_output"] == {"original_output": "complete output"}


def test_missing_receipt_preserves_handler_evidence(runtime, tmp_path):
    path = tmp_path / "effect"
    def write(_):
        path.write_text("committed")
        return ToolHandlerResult(output={"actual_path": str(path)}, llm_text="handler completed the write")
    mount_test_capability(runtime, alias="unreceipted_write", canonical_path="op_test_unreceipted_write",
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=write, execution=DIRECT_LOCAL_WRITE)
    result = invoke(runtime, "unreceipted_write", {})
    assert not result.ok
    assert metadata(result)["error_code"] == "missing_effect_receipt"
    assert metadata(result)["effect"] == "unknown"
    assert "handler completed the write" in result.llm_text
    assert str(path) in result.llm_text
    assert path.read_text() == "committed"


def test_rejection_preserves_corrective_details(runtime):
    def reject(_):
        raise ToolRejectedError("unknown target", error_code="unknown_target",
            details={"available_names": ["real-target"], "diagnostic": "token=hidden configuration hint"})
    mount_test_capability(runtime, alias="reject_target", canonical_path="op_test_reject_target",
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=reject, execution=DIRECT_LOCAL_READ)
    result = invoke(runtime, "reject_target", {})
    assert metadata(result)["effect"] == "not_started"
    assert "real-target" in result.llm_text
    assert "configuration hint" in result.llm_text
    assert "hidden" not in result.llm_text


@pytest.mark.parametrize("alias", ["edit_file", "write_file"])
@pytest.mark.parametrize("phase", ["read", "commit"])
def test_file_mutation_preserves_revalidation_error(runtime, tmp_path, monkeypatch, alias, phase):
    path = tmp_path / "source"
    path.write_text("original")
    read(runtime, path)
    def fail(*args, **kwargs):
        try:
            raise PermissionError("underlying permission cause")
        except PermissionError as cause:
            if phase == "commit":
                raise FileContentChangedError("could not revalidate source") from cause
            raise OSError("read unavailable") from cause
    module = "file_edit" if alias == "edit_file" else "file_write"
    target = "pal.execution.file_state.read_utf8_text_exact" if phase == "read" else f"pal.execution.{module}.atomic_compare_and_swap_utf8"
    monkeypatch.setattr(target, fail)
    args = {"file_path": str(path), **({"content": "changed"} if alias == "write_file" else
        {"edits": [{"old_string": "original", "new_string": "changed"}]})}
    result = invoke(runtime, alias, args)
    assert not result.ok
    assert metadata(result)["effect"] == "not_started"
    assert metadata(result)["error_code"] == ("READ_FAILED" if phase == "read" else "STALE_FILE")
    assert "underlying permission cause" in result.llm_text
    assert path.read_text() == "original"


def test_edit_write_failure_retains_exception_chain(runtime, tmp_path, monkeypatch):
    path = tmp_path / "source"
    path.write_text("original")
    read(runtime, path)
    def fail(*args, **kwargs):
        path.write_text("changed")
        try:
            raise RuntimeError("underlying persistence cause")
        except RuntimeError as cause:
            raise OSError("fsync failed") from cause
    monkeypatch.setattr("pal.execution.file_edit.atomic_compare_and_swap_utf8", fail)
    result = invoke(runtime, "edit_file", {"file_path": str(path), "edits": [{"old_string": "original", "new_string": "changed"}]})
    assert metadata(result)["effect"] == "unknown"
    assert metadata(result)["retry"] == "reconcile_first"
    assert "underlying persistence cause" in result.llm_text
    assert path.read_text() == "changed"


def test_delete_path_validation_does_not_claim_partial_deletion(runtime, monkeypatch):
    def fail(_):
        raise ValueError("invalid path")
    monkeypatch.setattr("pal.execution.path_delete.resolve_path_entry", fail)
    result = invoke(runtime, "delete_path", {"file_path": "invalid"})
    assert metadata(result)["effect"] == "not_started"
    assert "invalid path" in result.llm_text
    assert "Deletion may be partial" not in result.llm_text


def test_long_recovery_preserves_final_constraints_outside_output_budget(runtime):
    from pathlib import Path
    hint = "Check each pending item. " * 90 + "Do not delete the recovery copy."
    def fail(_):
        raise ToolExecutionError("specific failure", recovery_hint=hint)
    mount_test_capability(runtime, alias="long_recovery", canonical_path="op_test_long_recovery",
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=fail, execution=INDIRECT_LOCAL_READ)
    result = runtime.execute_tool(new_tool_call(name="call_tool", args={"name": "long_recovery", "args": {}}),
        turn_id="review", budget=ToolCallBudget(max_output_chars=1500, preview_chars=300))
    assert result.snapshot_refs
    assert hint in Path(result.snapshot_refs[-1].path).read_text()
    assert metadata(result)["recovery"] == hint


def test_snapshot_save_failure_keeps_full_storage_diagnostic(runtime, monkeypatch):
    def unavailable(*args, **kwargs):
        raise OSError("storage cause " + "x" * 2000 + " storage-diagnostic-tail")
    monkeypatch.setattr(runtime.result_snapshots, "capture_chunks", unavailable)
    mount_test_capability(runtime, alias="large_output", canonical_path="op_test_large_output",
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput,
        handler=lambda _: ToolHandlerResult(output={}, llm_text="body" * 2000), execution=DIRECT_NONE)
    result = runtime.execute_tool(new_tool_call(name="large_output", args={}), turn_id="review",
        budget=ToolCallBudget(max_output_chars=1000, preview_chars=200))
    assert result.ok
    assert not result.snapshot_refs
    assert "storage-diagnostic-tail" in result.llm_text
    assert "body" * 2000 in result.llm_text
    assert "full result is shown above beyond the output budget" in result.llm_text


@pytest.mark.parametrize("failed", [False, True])
def test_actual_effect_receipt_overrides_declared_no_effect(runtime, failed):
    def handler(_):
        receipt = EffectReceipt(outcome=EffectOutcome.APPLIED)
        if failed:
            raise ToolExecutionError("failed after effect", effect_receipt=receipt)
        return ToolHandlerResult(output={}, effect_receipt=receipt)
    mount_test_capability(runtime, alias="mislabelled_handler", canonical_path="op_test_mislabelled_handler",
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=handler, execution=DIRECT_NONE)
    result = invoke(runtime, "mislabelled_handler", {})
    assert result.ok is not failed
    assert metadata(result)["effect"] == "applied"


@pytest.mark.parametrize("provisioning", [False, True])
def test_screenshot_failure_preserves_code_retry_and_exception_chain(runtime, provisioning):
    # Keep the injected exception paired with the tool after plugin reload tests.
    from pal.web_fetch.tools import BrowserScreenshotTool, BrowserServiceError
    def execute(**kwargs):
        try:
            raise OSError("underlying screenshot cause")
        except OSError as cause:
            if provisioning:
                raise BrowserServiceError("installing", code="dependency_installing", retryable=True) from cause
            raise RuntimeError("screenshot could not be saved") from cause
    tool = BrowserScreenshotTool(SimpleNamespace(execute=execute))
    async def screenshot(_):
        return await tool.ainvoke({}, session_key="review", persistent=False, runtime=runtime, turn_id="review")
    mount_test_capability(runtime, alias="screenshot_failure", canonical_path="op_test_screenshot_failure",
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=screenshot, execution=INDIRECT_EXTERNAL_READ)
    result = invoke(runtime, "screenshot_failure", {}, asynchronous=True)
    assert not result.ok
    assert metadata(result)["error_code"] == ("dependency_installing" if provisioning else "screenshot_failed")
    assert metadata(result)["retry"] == ("safe" if provisioning else "do_not_retry")
    assert metadata(result)["effect"] == ("not_started" if provisioning else "unknown")
    assert "underlying screenshot cause" in result.llm_text


@pytest.mark.parametrize("code,retryable,unknown,expected_retry,expected_effect", [
    ("dependency_installing", True, False, "safe", "not_started"),
    ("invalid_arguments", False, False, "correct_input", "unknown"),
    ("navigation_failed", True, True, "reconcile_first", "unknown"),
    ("browser_unavailable", False, False, "do_not_retry", "unknown"),
])
def test_browser_failure_lifts_code_and_retry_into_metadata(runtime, code, retryable, unknown, expected_retry, expected_effect):
    def execute(**kwargs):
        raise BrowserServiceError("specific browser cause", code=code, retryable=retryable, state_unknown=unknown)
    provider = WebFetchIntrospectionProvider(service=SimpleNamespace(execute=execute))
    mount_test_capability(runtime, alias="broken_browser", canonical_path="op_test_broken_browser",
                          InputModel=EmptyToolInput, OutputModel=EmptyToolOutput,
                          execution=INDIRECT_EXTERNAL_READ,
                          handler=lambda _: provider.navigate(CapabilityCall(name="navigate", args={"url": "https://example.com"}, meta={"turn_id": "review"})))
    result = invoke(runtime, "broken_browser", {})
    info = metadata(result)
    assert (info["error_code"], info["retry"], info["effect"]) == (code, expected_retry, expected_effect)
    assert "specific browser cause" in result.llm_text
    assert "navigated failed" not in result.llm_text


def test_browser_value_error_is_not_misreported_as_missing_scope(runtime):
    def execute(**kwargs):
        raise ValueError("backend parsing failed")
    provider = WebFetchIntrospectionProvider(service=SimpleNamespace(execute=execute))
    mount_test_capability(runtime, alias="browser_value_error", canonical_path="op_test_browser_value_error",
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, execution=INDIRECT_EXTERNAL_READ,
        handler=lambda _: provider.navigate(CapabilityCall(name="navigate", args={"url": "https://example.com"}, meta={"turn_id": "review"})))
    result = invoke(runtime, "browser_value_error", {})
    assert metadata(result)["error_code"] == "handler_exception"
    assert metadata(result)["effect"] == "unknown"
    assert "backend parsing failed" in result.llm_text


@pytest.mark.parametrize("feedback_failure", [False, True])
def test_failure_feedback_preserves_original_error_and_metadata(feedback_failure):
    async def run():
        core = PalCore()
        executor = core.turn_executor
        original = ToolExecutionResult(name="probe", ok=False, text="KeyError: missing setting",
            llm_text="KeyError: missing setting\nTool result metadata: original receipt",
            structured={"error_code": "handler_exception", "effect": "unknown", "retry": "reconcile_first"})
        assert core._should_enter_failure_flow_for_tool_result(original)
        assert not core._should_enter_failure_flow_for_tool_result(ToolExecutionResult(
            name="probe", ok=False, text="capability execution failed: ordinary task result", llm_text="ordinary task result"))
        executor._build_tool_call_budget = lambda *args, **kwargs: None
        executor._log_tool_call_start = lambda *args: None
        executor._execute_tool_async = AsyncMock(return_value=original)
        executor._handle_failure_async = AsyncMock(return_value=SimpleNamespace(
            user_feedback="additional feedback", verification=SimpleNamespace(status="failed"), report=None))
        if feedback_failure:
            executor._handle_failure_async = AsyncMock(side_effect=RuntimeError("failure recovery crashed"))
        executor._render_failure_feedback_text = lambda feedback: feedback
        captured = []
        def capture(*args):
            captured.append(args[-1])
            raise RuntimeError("captured final result")
        executor._log_tool_call_result = capture
        with pytest.raises(RuntimeError, match="captured final result"):
            await executor._handle_tool_call(ToolCallEffect(tool_call=new_tool_call(name="probe", args={})),
                continuation(turn_id="review", pending_tool_call_batch=[], finalization_only=False))
        result = captured[0]
        assert "missing setting" in result.llm_text
        assert ("failure recovery crashed" if feedback_failure else "additional feedback") in result.llm_text
        assert "original receipt" in result.llm_text
        assert result.structured["error_code"] == "handler_exception"
        assert result.structured["effect"] == "unknown"
    asyncio.run(run())


def test_rg_no_match_keeps_exit_status_and_explains_result(runtime, tmp_path):
    path = tmp_path / "source"
    path.write_text("alpha\n")
    result = invoke(runtime, "run_shell", {"cmd": f"rg -n absent {shlex.quote(str(path))}"})
    assert not result.ok
    assert result.invocation_result.details["returncode"] == 1
    assert metadata(result)["error_code"] == "command_failed"
    assert "no matches" in metadata(result)["recovery"]
    assert "before changing the command" in metadata(result)["recovery"]
