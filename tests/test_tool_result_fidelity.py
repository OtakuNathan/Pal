"""Failures retain their execution facts across adapters and output delivery."""
from __future__ import annotations

from pathlib import Path
import shlex

import pytest

from pal.execution.contracts import CapabilityCall, CapabilityResult, ToolCallBudget
from pal.execution.shell_exec import ShellExecTool, _ShellProcessSupervisor
from pal.execution.tool_facade import (
    EffectOutcome, EffectReceipt, EmptyToolInput, EmptyToolOutput, FailedResult,
    RejectedResult, RetryDirective, ToolExecutionError, ToolRejectedError,
)
from pal.execution.tool_semantics import DIRECT_LOCAL_READ, INDIRECT_LOCAL_READ
from pal.shared import RuntimeStatus
from pal.shared.tool_protocol import ToolContextMessageIR, new_tool_call
from tests.capability_fixture import mount_test_capability
from tests.test_tool_failure_affordances import runtime, invoke, metadata


def mount(runtime, alias, handler, *, direct=True):
    mount_test_capability(runtime, alias=alias, canonical_path=f"op_test_{alias}",
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=handler,
        execution=DIRECT_LOCAL_READ if direct else INDIRECT_LOCAL_READ)


def test_failure_summary_cannot_hide_text_or_unfamiliar_details(runtime):
    mount(runtime, "failure_details", lambda _: CapabilityResult(
        status=RuntimeStatus.ERROR, text="actual backend failure", llm_text="short summary",
        structured={"error_code": "backend_error", "diagnostic": "full backend diagnostic",
                    "failed_stage": "upload chunk 42", "retry": "do_not_retry"}))
    result = invoke(runtime, "failure_details", {})
    assert "actual backend failure" in result.llm_text
    assert "full backend diagnostic" in result.llm_text
    assert "upload chunk 42" in result.llm_text
    assert metadata(result)["retry"] == "do_not_retry"


@pytest.mark.parametrize("rejected", [False, True])
@pytest.mark.parametrize("direct", [False, True])
def test_typed_failure_delivers_error_and_details_once(runtime, rejected, direct):
    cls = RejectedResult if rejected else FailedResult
    raw = cls(error_code="specific_error", error="actual typed cause", llm_text="short typed summary",
        retry=RetryDirective.CORRECT_INPUT,
        details={"diagnostic": "corrective detail", "token": "hidden"},
        **({} if rejected else {"effect": EffectOutcome.UNKNOWN}))
    mount(runtime, "typed_failure", lambda _: raw, direct=direct)
    result = invoke(runtime, "typed_failure", {})
    assert "actual typed cause" in result.llm_text
    assert "corrective detail" in result.llm_text
    assert "hidden" not in result.llm_text
    assert result.llm_text.count("Tool result metadata:") == 1


@pytest.mark.parametrize("rejected", [False, True])
def test_capability_relay_preserves_rejection_and_explicit_retry(runtime, rejected):
    def fail(_):
        if rejected:
            raise ToolRejectedError("invalid choice", error_code="specific_rejection")
        raise ToolExecutionError("permanent failure", retry=RetryDirective.DO_NOT_RETRY,
            effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_APPLIED))
    mount(runtime, "relay_target", fail, direct=False)
    mount(runtime, "relay", lambda _: runtime.execute(CapabilityCall(name="relay_target")))
    result = invoke(runtime, "relay", {})
    assert metadata(result)["kind"] == ("rejected" if rejected else "failed")
    assert metadata(result)["retry"] == ("correct_input" if rejected else "do_not_retry")


@pytest.mark.parametrize("asynchronous", [False, True])
def test_shell_spawn_refusal_has_not_started_receipt(runtime, tmp_path, asynchronous):
    result = invoke(runtime, "run_shell", {"cmd": "printf unreachable", "cwd": str(tmp_path / "absent")},
        asynchronous=asynchronous)
    assert not result.ok
    assert metadata(result)["error_code"] == "shell_spawn_failed"
    assert metadata(result)["effect"] == "not_started"
    assert metadata(result)["retry"] == "safe"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_shell_failure_after_execution_never_claims_spawn_failed(runtime, tmp_path, monkeypatch, asynchronous):
    marker = tmp_path / "ran"
    original_read = Path.read_bytes
    def fail_stdout(path):
        if path.name == "stdout" and path.parent.name.startswith("shell-"):
            try:
                raise PermissionError("underlying output read cause")
            except PermissionError as cause:
                raise OSError("stdout unavailable") from cause
        return original_read(path)
    monkeypatch.setattr(Path, "read_bytes", fail_stdout)
    result = invoke(runtime, "run_shell", {"cmd": f"printf once > {shlex.quote(str(marker))}; printf preserved-stderr >&2"},
        asynchronous=asynchronous)
    assert marker.read_text() == "once"
    assert not result.ok
    assert metadata(result)["error_code"] != "shell_spawn_failed"
    assert metadata(result)["effect"] == "unknown"
    assert metadata(result)["retry"] == "reconcile_first"
    assert "could not start" not in result.llm_text
    assert "underlying output read cause" in result.llm_text
    assert "preserved-stderr" in result.llm_text
    assert result.invocation_result.details["returncode"] == 0


@pytest.mark.parametrize("exception_type", [OSError, RuntimeError])
def test_shell_capture_failure_retains_original_streams(runtime, tmp_path, monkeypatch, exception_type):
    def fail(*args, **kwargs):
        try:
            raise PermissionError("underlying capture cause")
        except PermissionError as cause:
            raise exception_type("capture failed") from cause
    monkeypatch.setattr(runtime.result_snapshots, "capture_chunks", fail)
    result = ShellExecTool(output_root=tmp_path / "shell").invoke(
        {"cmd": "printf ORIGINAL_STDOUT; printf ORIGINAL_STDERR >&2"}, runtime=runtime,
        turn_id="review", call_id="capture-failure", budget=ToolCallBudget(max_output_chars=10, preview_chars=10))
    assert result.structured["returncode"] == 0
    assert result.structured["error_code"] == "output_snapshot_failed"
    assert result.structured["stdout"] == "ORIGINAL_STDOUT"
    assert result.structured["stderr"] == "ORIGINAL_STDERR"
    assert "underlying capture cause" in result.llm_text


def test_cancelled_shell_before_spawn_is_not_an_unknown_effect(tmp_path):
    supervisor = _ShellProcessSupervisor(argv=["unused"], cwd=None, timeout_ms=1000, output_root=tmp_path)
    supervisor.terminate()
    execution = supervisor.run()
    result = ShellExecTool._execution_result("unused", None, 1000, execution)
    assert result.effect_receipt.outcome is EffectOutcome.NOT_STARTED


def test_exception_group_keeps_all_errors(runtime):
    def fail(_):
        nested = ValueError("deepest-group-cause")
        for index in range(12):
            nested = ExceptionGroup(f"level {index}", [nested])
        raise ExceptionGroup("batch failed", [ValueError(f"item-{i}-failed") for i in range(20)] + [nested])
    mount(runtime, "group_failure", fail)
    result = invoke(runtime, "group_failure", {})
    assert "item-19-failed" in result.llm_text
    assert "deepest-group-cause" in result.llm_text


def test_browser_output_save_failure_keeps_full_capture(runtime, monkeypatch):
    from pal.web_fetch.capabilities import WebFetchIntrospectionProvider
    def fail(*args, **kwargs):
        try:
            raise PermissionError("underlying browser storage cause")
        except PermissionError as cause:
            raise OSError("save failed") from cause
    monkeypatch.setattr(runtime.result_snapshots, "capture", fail)
    raw = CapabilityResult(status=RuntimeStatus.OK, text="Browser read", llm_text="Browser read",
        structured={"document": {"preview": "preview only", "_full_text": "complete captured browser output"}})
    result = WebFetchIntrospectionProvider(service=None)._document_result(
        CapabilityCall(name="read_browser", meta={"execution_runtime": runtime, "turn_id": "review"}), raw)
    assert "complete captured browser output" in result.llm_text
    assert "underlying browser storage cause" in result.llm_text
    assert not result.snapshot_refs


def test_shell_cleanup_failure_keeps_completed_result(runtime, monkeypatch):
    import tempfile
    cleanup = tempfile.TemporaryDirectory.cleanup
    def fail_cleanup(directory):
        cleanup(directory)
        if Path(directory.name).name.startswith("shell-"):
            raise OSError("shell temporary cleanup failed")
    monkeypatch.setattr(tempfile.TemporaryDirectory, "cleanup", fail_cleanup)
    result = invoke(runtime, "run_shell", {"cmd": "printf completed-stdout; printf completed-stderr >&2"})
    assert not result.ok
    assert metadata(result)["error_code"] == "shell_cleanup_failed"
    assert metadata(result)["effect"] == "applied"
    assert metadata(result)["retry"] == "do_not_retry"
    assert "completed-stdout" in result.llm_text and "completed-stderr" in result.llm_text
    assert "shell temporary cleanup failed" in result.llm_text
    assert result.invocation_result.details["returncode"] == 0


@pytest.mark.parametrize("output_contract_failure", [False, True])
def test_failure_preserves_context_evidence(runtime, output_contract_failure):
    context = (ToolContextMessageIR(content="backend diagnostic attachment", semantic_kind="reference"),)
    mount(runtime, "failure_context", lambda _: CapabilityResult(
        status=RuntimeStatus.OK if output_contract_failure else RuntimeStatus.ERROR,
        text="operation result", llm_text="operation result", structured={"unexpected_field": 42},
        effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED), context_messages=context))
    result = invoke(runtime, "failure_context", {})
    assert not result.ok
    assert result.context_messages == context


@pytest.mark.parametrize("exception_type", [OSError, RuntimeError])
def test_output_storage_exception_preserves_operation_result(runtime, monkeypatch, exception_type):
    def fail(*args, **kwargs):
        raise exception_type("storage adapter failed")
    monkeypatch.setattr(runtime.result_snapshots, "capture", fail)
    body = "begin " * 500 + "UNIQUE_MIDDLE_EVIDENCE" + " end" * 500
    calls = []
    def handler(_):
        calls.append("executed")
        return CapabilityResult(
            status=RuntimeStatus.OK, llm_text=body, structured={},
            effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED))
    mount(runtime, "storage_failure", handler)
    result = runtime.execute_tool(new_tool_call(name="storage_failure", args={}), turn_id="review",
        budget=ToolCallBudget(max_output_chars=1200, preview_chars=300))
    assert result.ok
    assert metadata(result)["effect"] == "applied"
    assert "storage adapter failed" in result.llm_text
    assert body in result.llm_text
    assert "full result is shown" in result.llm_text
    assert not result.snapshot_refs
    assert result.invocation_result.output_error
    assert calls == ["executed"]


def test_failed_snapshot_read_saves_diagnostic_separately_from_source(runtime, monkeypatch):
    ref = runtime.result_snapshots.capture("original snapshot body", call_id="source", lifetime="review")
    def unreadable(_):
        raise PermissionError("snapshot read error " + "x" * 4000 + " complete-error-tail")
    monkeypatch.setattr("pal.execution.file_read.read_utf8_text_exact", unreadable)
    result = runtime.execute_tool(new_tool_call(name="read_file", args={"file_path": ref.path}), turn_id="review",
        budget=ToolCallBudget(max_output_chars=1500, preview_chars=300))
    assert not result.ok
    assert result.snapshot_refs[-1].path != ref.path
    saved = Path(result.snapshot_refs[-1].path).read_text()
    assert "complete-error-tail" in saved
    assert "snapshot read error" in saved
