"""Owner-authored errors and receipts are independent of the output budget."""

from pathlib import Path
import asyncio
import json

import pytest

from pal.execution.contracts import CapabilityResult, ToolCallBudget
from pal.execution.tool_facade import (
    ToolHandlerResult, CompleteResult, EffectOutcome, InvocationMode, ToolExecutionError,
)
from pal.shared import RuntimeStatus
from pal.shared.diagnostics import diagnostic_text
from pal.shared.tool_protocol import (
    FailedResult, RejectedResult, RetryDirective, ToolAffordance, new_tool_call,
)
from tests.test_result_guidance_budget import _core, _mount_boom


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("source", ["capability", "failed", "rejected", "exception", "delivery"])
def test_public_error_receipts_survive_delivery(source, wrapped, asynchronous):
    core = _core()
    runtime = core.context.execution_runtime
    message = "Write failed: disk full.\n" + "\n".join(
        f"item_{i}: {'written; do not repeat' if i == 91 else 'skipped'}"
        for i in range(190)
    )
    recovery = "Reconcile every written item before retrying skipped items."
    calls = []

    def handler(_):
        calls.append("executed")
        if source == "exception":
            raise ToolExecutionError(message, recovery_hint=recovery)
        if source == "capability":
            return CapabilityResult(status=RuntimeStatus.ERROR, text=message,
                llm_text=message, recovery_hint=recovery)
        if source == "delivery":
            return CompleteResult(output={"ok": True}, effect=EffectOutcome.APPLIED,
                llm_text="Completed writes.", output_error=message, recovery_hint=recovery)
        result_type = RejectedResult if source == "rejected" else FailedResult
        return result_type(error_code="write_failed", error="disk full", llm_text=message,
            effect=EffectOutcome.NOT_STARTED if source == "rejected" else EffectOutcome.APPLIED,
            retry=RetryDirective.DO_NOT_RETRY, recovery_hint=recovery)

    async def async_handler(value):
        return handler(value)

    _mount_boom(runtime, handler=async_handler if asynchronous else handler,
        mode=InvocationMode.INDIRECT if wrapped else InvocationMode.DIRECT)
    call = new_tool_call(name="call_tool" if wrapped else "boom",
        args={"name": "boom", "args": {}} if wrapped else {})
    try:
        budget = ToolCallBudget(max_output_chars=1, preview_chars=1)
        result = (asyncio.run(runtime.execute_tool_async(call, budget=budget)) if asynchronous
                  else runtime.execute_tool(call, budget=budget))
        assert calls == ["executed"]
        assert result.ok == (source == "delivery")
        invocation = result.invocation_result
        if source == "delivery":
            assert invocation.output_error == message
            metadata = json.loads(result.llm_text.rsplit("Tool result metadata: ", 1)[1])
            assert metadata["delivery_error"] == message
            assert metadata["effect"] == "applied"
        else:
            assert message in result.llm_text
            assert invocation.llm_text == ("boom: " + message if source == "exception" else message)
            assert not result.snapshot_refs
        assert invocation.recovery_hint == recovery
        assert recovery in result.llm_text
    finally:
        core.close()


@pytest.mark.parametrize("budget_chars", [1, 1000])
@pytest.mark.parametrize("source", ["provider", "guidance", "candidate"])
def test_success_delivery_errors_remain_visible_outside_output_budget(monkeypatch, source, budget_chars, caplog):
    from pal.execution.runtime import ExecutionRuntime

    core = _core()
    runtime = core.context.execution_runtime
    body = "business result " * 30
    calls = []
    message = "secondary delivery failed; token=PRIVATE_CANARY"

    def handler(_):
        calls.append("executed")
        return CompleteResult(output={"ok": True}, effect=EffectOutcome.APPLIED, llm_text=body,
            output_error=message if source == "provider" else "",
            affordances=[ToolAffordance(tool="boom", arguments={"value": "follow-up"}, reason="Inspect")]
                if source == "candidate" else [])

    _mount_boom(runtime, handler=handler)
    if source == "guidance":
        def fail(*args, **kwargs):
            raise RuntimeError(message)
        monkeypatch.setattr(ExecutionRuntime, "_resolve_result_guidance", fail)
    elif source == "candidate":
        original = ExecutionRuntime._validate_invocation_input
        def fail(record, arguments):
            if arguments == {"value": "follow-up"}:
                raise RuntimeError(message)
            return original(record, arguments)
        monkeypatch.setattr(ExecutionRuntime, "_validate_invocation_input", staticmethod(fail))
    try:
        result = runtime.execute_tool(new_tool_call(name="boom", args={}),
            budget=ToolCallBudget(max_output_chars=budget_chars, preview_chars=1))
        assert result.ok
        assert calls == ["executed"]
        metadata = json.loads(result.llm_text.rsplit("Tool result metadata: ", 1)[1])
        assert metadata["effect"] == "applied"
        assert "secondary delivery failed" in metadata["delivery_error"]
        assert "PRIVATE_CANARY" not in result.llm_text
        assert "Traceback" not in metadata["delivery_error"]
        if budget_chars == 1000:
            assert result.invocation_result.llm_text == body
        else:
            assert any(Path(ref.path).read_text() == body for ref in result.snapshot_refs)
        if source != "provider":
            assert "Traceback" in caplog.text
            assert "PRIVATE_CANARY" not in caplog.text
    finally:
        core.close()


def test_long_diagnostic_preserves_text_and_redacts_url_credentials():
    text = "x" * 50_000 + " https://user:PRIVATE_CANARY@example.invalid/report"
    redacted = diagnostic_text(text, limit=None)
    assert redacted == "x" * 50_000 + " https://[redacted]@example.invalid/report"


def test_nested_exception_group_summary_preserves_each_cause_without_frames():
    from pal.foundation.diagnostics import diagnostic_summary, exception_report

    try:
        raise OSError("backend root cause; token=PRIVATE_CANARY")
    except OSError as cause:
        try:
            raise RuntimeError("first provider failed") from cause
        except RuntimeError as first:
            group = ExceptionGroup("providers failed", [
                ExceptionGroup("nested provider", [first]), ValueError("second provider failed"),
            ])
    try:
        raise group
    except ExceptionGroup as exc:
        report = exception_report(exc)
    summary = diagnostic_summary(report)
    for message in ("providers failed", "nested provider", "backend root cause",
                    "first provider failed", "second provider failed"):
        assert message in summary
    assert "Traceback" not in summary
    assert 'File "' not in summary
    assert "PRIVATE_CANARY" not in summary
    assert "Traceback" in report
    assert diagnostic_summary("| ordinary message") == "| ordinary message"


def test_tiny_output_budget_does_not_shorten_actionable_failure():
    core = _core()
    runtime = core.context.execution_runtime
    message = "Cannot open /tmp/report: permission denied. Correct the directory permissions."
    _mount_boom(runtime, handler=lambda _: CapabilityResult(
        status=RuntimeStatus.ERROR, text=message, llm_text=message,
        structured={"error_code": "permission_denied"}))
    try:
        result = runtime.execute_tool(new_tool_call(name="boom", args={}),
            budget=ToolCallBudget(max_output_chars=1, preview_chars=1))
        assert message in result.llm_text
        assert not result.snapshot_refs
        assert "permission_denied" in result.llm_text
    finally:
        core.close()


def test_failure_summary_keeps_structured_cause_when_body_is_a_title():
    core = _core()
    runtime = core.context.execution_runtime
    cause = {"code": "EACCES", "message": "cannot open report", "path": "/tmp/report"}
    _mount_boom(runtime, handler=lambda _: CapabilityResult(
        status=RuntimeStatus.ERROR, text="Provider result", llm_text="EACCES: cannot open report /tmp/report",
        structured={"error": cause, "provider_trace": "x" * 4000}))
    try:
        result = runtime.execute_tool(new_tool_call(name="boom", args={}),
            budget=ToolCallBudget(max_output_chars=1))
        assert "cannot open report" in result.llm_text
        assert "EACCES" in result.llm_text and "/tmp/report" in result.llm_text
        assert "x" * 4000 not in result.llm_text
        assert result.structured["details"]["provider_trace"] == "x" * 4000
    finally:
        core.close()


def test_exception_summary_shows_causes_and_retains_stack_separately(caplog):
    core = _core()
    runtime = core.context.execution_runtime
    def fail(_):
        try:
            raise PermissionError("cannot open /tmp/report; token=PRIVATE_CANARY")
        except PermissionError as exc:
            raise RuntimeError("provider unavailable") from exc
    _mount_boom(runtime, handler=fail)
    try:
        result = runtime.execute_tool(new_tool_call(name="boom", args={}),
            budget=ToolCallBudget(max_output_chars=1))
        assert "RuntimeError: provider unavailable" not in result.llm_text
        assert "PermissionError: cannot open /tmp/report" in result.llm_text
        assert "Traceback" not in result.llm_text
        assert "PRIVATE_CANARY" not in result.llm_text
        full = caplog.text
        assert "Traceback" in full
        assert "PRIVATE_CANARY" not in full
        assert len(result.llm_text) < 1000
    finally:
        core.close()


def test_recovery_does_not_spend_normal_output_budget():
    core = _core()
    runtime = core.context.execution_runtime
    body = "b" * 950
    recovery = "Check pending items. " * 100 + "Do not repeat completed writes."
    _mount_boom(runtime, handler=lambda _: ToolHandlerResult(
        output={"ok": True}, llm_text=body, recovery_hint=recovery))
    try:
        result = runtime.execute_tool(new_tool_call(name="boom", args={}),
            budget=ToolCallBudget(max_output_chars=1000, preview_chars=100))
        assert result.ok
        assert result.invocation_result.llm_text == body
        assert result.invocation_result.recovery_hint == recovery
        assert recovery in result.llm_text
        assert not result.snapshot_refs
    finally:
        core.close()
