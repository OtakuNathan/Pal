"""Operation facts survive concise owner-authored error delivery."""
from pal.foundation.diagnostics import exception_reason, diagnostic_reason, public_diagnostics
from pal.execution.tool_facade import ToolExecutionError, EffectReceipt
from pal.shared.tool_protocol import EffectOutcome


def test_root_cause_is_brief_and_full_chain_is_logged(caplog):
    try:
        try:
            raise PermissionError("cannot read /tmp/report; token=PRIVATE_CANARY")
        except PermissionError as cause:
            raise RuntimeError("provider wrapper") from cause
    except RuntimeError as error:
        text = exception_reason(error)
    assert "PermissionError" in text and "/tmp/report" in text
    assert "provider wrapper" not in text and "Traceback" not in text
    assert "provider wrapper" in caplog.text and "Traceback" in caplog.text
    assert "PRIVATE_CANARY" not in text + caplog.text


def test_owner_reason_and_execution_receipt_survive_lower_transport_error():
    try:
        raise ConnectionError("socket closed")
    except ConnectionError as cause:
        error = ToolExecutionError("Write completed; refreshing the index failed",
            effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED),
            recovery_hint="Refresh the index; do not repeat the write")
        error.__cause__ = cause
    assert exception_reason(error) == "Write completed; refreshing the index failed"
    assert error.effect_receipt.outcome is EffectOutcome.APPLIED
    assert error.recovery_hint == "Refresh the index; do not repeat the write"


def test_owner_authored_per_item_receipts_are_not_truncated_as_a_cause():
    import json
    text = 'Already applied; persistence failed.\n' + json.dumps({
        'applied_edit_indices': list(range(200)), 'failed_edits': [{'edit_index': 200, 'error_code': 'not_found'}]})
    error = ToolExecutionError(text, effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED))
    assert exception_reason(error) == text


def test_wrapped_public_error_and_group_keep_every_item_receipt():
    text = "Write failed.\n" + "\n".join(
        f"item_{i}: {'applied; do not retry' if i == 91 else 'skipped'}"
        for i in range(190))
    error = ToolExecutionError(text)
    wrapper = RuntimeError("transport wrapper")
    wrapper.__cause__ = error
    assert exception_reason(wrapper) == text
    group = ExceptionGroup("batch failed", [wrapper, OSError("cleanup unavailable")])
    assert exception_reason(group) == text + "; OSError: cleanup unavailable"


def test_user_blocker_retains_the_owner_message_without_a_second_length_cut():
    from pal.failure import FailureDraft, FailureRuntime, VerificationResult

    body = "Already applied. " * 90 + "Do not repeat item_91." + " Inspect remaining items." * 90
    draft = FailureDraft(subsystem="llm", component="provider", failure_kind="provider_failure",
        severity="medium", primary_blocker=body)
    feedback = FailureRuntime().render_user_feedback(draft,
        verification=VerificationResult(status="failed", reason="provider unavailable"))
    assert feedback.blocker == body


def test_exception_group_preserves_distinct_root_failures():
    error = ExceptionGroup("receivers failed", [OSError("display disconnected"), ValueError("unsupported emotion")])
    text = exception_reason(error)
    assert "display disconnected" in text and "unsupported emotion" in text
    assert "ExceptionGroup" not in text


def test_remote_exception_group_preserves_each_branch_root(caplog):
    from pal.foundation.diagnostics import exception_report

    wrapper = RuntimeError("provider wrapper")
    wrapper.__cause__ = OSError("camera offline")
    group = ExceptionGroup("parallel calls failed", [wrapper,
        ExceptionGroup("nested", [ValueError("unsupported expression")])])
    reason = diagnostic_reason(exception_report(group))
    assert reason == "OSError: camera offline; ValueError: unsupported expression"
    assert "provider wrapper" in caplog.text


def test_java_causes_and_native_frames_do_not_hide_the_root():
    report = ('java.lang.RuntimeException: wrapper\n    at app.main(App.java:3)\n'
              'Caused by: java.net.ConnectException: refused\n    at client.connect(Native Method)')
    assert diagnostic_reason(report) == "java.net.ConnectException: refused"
    assert diagnostic_reason('System.IO.IOException: denied\n   at App.Save() in app.cs:line 12') == 'System.IO.IOException: denied'


def test_diagnostic_projection_recognizes_stack_field_spellings():
    for key in ('stackTrace', 'StackTrace', 'stack_trace', 'stack-trace', 'TRACEBACK'):
        original = {'error': {'message': 'denied', key: 'PRIVATE_FRAME'}}
        assert public_diagnostics(original) == {'error': {'message': 'denied'}}
        assert original['error'][key] == 'PRIVATE_FRAME'


def test_encoded_diagnostic_records_keep_causes_and_execution_facts():
    import json
    report = 'Traceback (most recent call last):\n  File "private.py", line 7\nPermissionError: denied'
    original = {'error': {'stackTrace': report, 'code': 'EACCES'}, 'applied': [0, 2], 'retry': False}
    text = diagnostic_reason(json.dumps(original))
    assert diagnostic_reason(original) == text
    assert json.loads(text) == {'error': {'code': 'EACCES', 'reason': 'PermissionError: denied'},
                               'applied': [0, 2], 'retry': False}
    assert 'private.py' not in text and 'Traceback' not in text


def test_failure_inspection_removes_java_frames_from_summary():
    from pal.failure.capabilities import _report_summary

    report = {'current_blocker': 'Error: unavailable\n    at request (/private/app.js:1:2)'}
    assert _report_summary(report)['current_blocker'] == 'Error: unavailable'


def test_recovery_failure_is_not_replaced_by_the_error_being_recovered(caplog):
    try:
        raise ConnectionError("submission confirmation lost")
    except ConnectionError:
        try:
            raise OSError("journal query unavailable")
        except OSError as error:
            text = exception_reason(error)
    assert text == "OSError: journal query unavailable"
    assert "submission confirmation lost" in caplog.text


def test_external_traceback_is_not_default_tool_text():
    report = 'Traceback (most recent call last):\n  File "private.py", line 7\n    connect()\nConnectionError: refused'
    assert diagnostic_reason(report) == "ConnectionError: refused"
    record = {"last_error": report, "attached": False, "pending": 0}
    visible = public_diagnostics(record)
    assert visible == {"last_error": "ConnectionError: refused", "attached": False, "pending": 0}
    assert record["last_error"] == report
    assert public_diagnostics({"description": report}) == {"description": report}


def test_prefixed_tracebacks_do_not_bypass_diagnostic_projection():
    report = 'Traceback (most recent call last):\n  File "private.py", line 7\n    connect()\nConnectionError: refused'
    for prefix in ("text: ", "Post-commit cleanup failed: ", "WARNING service: "):
        assert diagnostic_reason(prefix + report) == "ConnectionError: refused"


def test_mcp_javascript_stack_keeps_cause_and_logs_frames(caplog):
    report = 'Error: connection refused\n    at connect (/srv/client.js:12:3)\n    at async main (/srv/index.js:9:5)'
    assert diagnostic_reason(report) == "Error: connection refused"
    assert "/srv/client.js:12:3" in caplog.text


def test_lsp_business_result_remains_intact_beside_short_health_errors():
    import json
    from pal.lsp.plugin import _capability_from_rpc

    report = 'Traceback (most recent call last):\n  File "server.py", line 2\nOSError: disconnected'
    business = {"stack": report, "contents": {"error": report}}
    result = _capability_from_rpc("LSP hover", {
        "status": "ok", "result": business, "evidence": {"last_error": report}})
    visible = json.loads(result.llm_text.split("\n", 1)[1])
    assert visible["result"] == business
    assert visible["evidence"]["last_error"] == "OSError: disconnected"
    assert result.structured["evidence"]["last_error"] == report


def test_lsp_recovery_does_not_repeat_the_server_stack():
    from pathlib import Path
    from types import SimpleNamespace
    from pal.lsp.connector import LspProtocolError
    from pal.lsp.manager import LspManager
    from pal.lsp.plugin import _capability_from_rpc

    report = 'Traceback (most recent call last):\n  File "private.py", line 2\nPermissionError: denied'
    manager = SimpleNamespace(_server_summary=lambda _: {"id": "audit"})
    for code in (-32603, -32601):
        payload = LspManager._server_error_payload(manager, "hover",
            LspProtocolError(report, code=code, method="textDocument/hover"),
            state=SimpleNamespace(server_id="audit"), workspace_root=Path("/tmp/audit"))
        result = _capability_from_rpc("LSP hover", payload)
        assert "PermissionError: denied" in result.llm_text
        assert "Traceback" not in result.user_text
        assert "private.py" not in result.user_text
        assert "Traceback" not in result.recovery_hint
        assert result.structured["server_error"]["message"] == report


def test_failure_group_with_shared_cause_and_cycle_terminates():
    error = RuntimeError("cycle")
    error.__cause__ = error
    assert "cycle" in exception_reason(error)


def test_compaction_success_does_not_repeat_recovered_attempt_errors():
    from pal.core.compaction import CompactionRunResult

    result = CompactionRunResult(status="compacted", attempts=2,
        failures=("schema:invalid JSON",), failure_details=("ValueError: invalid JSON",))
    assert result.user_notice == ""
    assert "invalid JSON" in result.diagnostic_details


def test_provider_failure_body_is_not_rehydrated_from_private_metadata():
    from pal.execution.contracts import CapabilityResult
    from pal.shared import RuntimeStatus
    from pal.shared.tool_protocol import new_tool_call
    from tests.test_result_guidance_budget import _core, _mount_boom

    core = _core()
    try:
        runtime = core.context.execution_runtime
        _mount_boom(runtime, handler=lambda _: CapabilityResult(
            status=RuntimeStatus.ERROR, llm_text="Permission denied: /tmp/report",
            structured={"error_code": "permission_denied", "diagnostic": "PRIVATE_STACK"},
            recovery_hint="Correct directory permissions"))
        result = runtime.execute_tool(new_tool_call(name="boom", args={}))
        assert result.llm_text.startswith("Permission denied: /tmp/report")
        assert "PRIVATE_STACK" not in result.llm_text
        assert "Correct directory permissions" in result.llm_text
        assert result.structured["details"]["diagnostic"] == "PRIVATE_STACK"
    finally:
        core.close()


def test_declared_exception_keeps_private_details_out_of_public_text():
    from pal.shared.tool_protocol import new_tool_call
    from tests.test_result_guidance_budget import _core, _mount_boom

    core = _core()
    def fail(_call):
        raise ToolExecutionError("Access denied", details={"diagnostic": "PRIVATE_STACK"},
            effect_receipt=EffectReceipt(outcome=EffectOutcome.NOT_STARTED),
            recovery_hint="Fix access permissions")
    try:
        runtime = core.context.execution_runtime
        _mount_boom(runtime, handler=fail)
        result = runtime.execute_tool(new_tool_call(name="boom", args={}))
        assert "Access denied" in result.llm_text
        assert "PRIVATE_STACK" not in result.llm_text
        assert "Fix access permissions" in result.llm_text
        assert result.structured["details"]["diagnostic"] == "PRIVATE_STACK"
    finally:
        core.close()
