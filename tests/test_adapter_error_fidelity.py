"""Plugin, LSP and worker adapters deliver failure causes and execution facts."""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pal.bunshin.scoped_execution import _WORKSPACE_TOOL_SPECS, _workflow_capability
from pal.bunshin.submission_errors import SubmissionValidationError, submission_error_result
from pal.core import PalCore
from pal.execution.contracts import CapabilityCall
from pal.execution.capability_compiler import _attach_effect_receipt
from pal.execution.tool_semantics import DIRECT_LOCAL_READ
from pal.lsp.config import LspServerConfig, LspServerFileConfig
from pal.lsp.connector import LspProtocolError
from pal.lsp.manager import LspManager, LspServerState, _attach_error_detail
from pal.lsp.plugin import LspManagerPluginProvider
from pal.plugins import PluginHost
from pal.plugins.capabilities import PluginsIntrospectionProvider
from pal.shared import RuntimeStatus, ToolExecutionResult
from pal.shared.tool_protocol import ToolContextMessageIR, new_tool_call
from tests.test_plugin_raii import _write_plugin
from tests.test_tool_failure_affordances import runtime, invoke, metadata
from tests.test_tool_result_fidelity import mount


def chained_failure(label="outer failure"):
    try:
        raise OSError("underlying cause; token=hidden-token")
    except OSError as cause:
        raise RuntimeError(label) from cause


@pytest.fixture
def host(tmp_path, monkeypatch):
    root = tmp_path / "builtin"
    _write_plugin(root, "fidelity_plugin")
    _write_plugin(root, "fidelity_dependent", requires=("fidelity_plugin",))
    monkeypatch.syspath_prepend(str(root))
    core = PalCore()
    host = PluginHost(core.context, tmp_path, builtin_root=root, services={"ledger": []})
    host.rescan()
    yield host
    host.shutdown()
    core.close()


@pytest.mark.parametrize("action", ["attach", "enable", "reattach"])
def test_plugin_load_failure_delivers_cause_now(runtime, host, monkeypatch, action):
    monkeypatch.setattr(host, "_call_plugin_factory", lambda *a, **kw: chained_failure("plugin start failed"))
    provider = PluginsIntrospectionProvider(host)
    mount(runtime, "plugin_load_probe", lambda _: _attach_effect_receipt(getattr(provider, action)(
        CapabilityCall(name="probe", args={"name": "fidelity_plugin"})), DIRECT_LOCAL_READ))
    result = invoke(runtime, "plugin_load_probe", {})
    assert not result.ok
    assert "plugin start failed" in result.llm_text and "underlying cause" in result.llm_text
    assert "hidden-token" not in result.llm_text
    assert metadata(result)["error_code"] == "plugin_load_failed"


@pytest.mark.parametrize("action,dependent", [("detach", False), ("disable", False), ("reattach", True)])
def test_plugin_detach_refusal_reports_current_state_and_blocker(host, monkeypatch, action, dependent):
    assert host.attach("fidelity_dependent" if dependent else "fidelity_plugin")["status"] == "ok"
    with monkeypatch.context() as patcher:
        patcher.setattr(type(host.context.execution_runtime), "check_detach", lambda *_: chained_failure("detach refused"))
        result = getattr(host, action)("fidelity_plugin")
    assert result["status"] == "error"
    assert result["attached"] is True
    assert "detach refused" in result["error"] and "underlying cause" in result["error"]
    if dependent:
        assert result["blocked_by"] == "fidelity_dependent"


def test_plugin_start_rollback_keeps_both_failures(host, monkeypatch):
    def factory(*args, **kwargs):
        def start(scope):
            scope.defer(lambda: chained_failure("rollback cleanup failed"))
            chained_failure("plugin initialization failed")
        return SimpleNamespace(start=start)
    monkeypatch.setattr(host, "_call_plugin_factory", factory)
    result = host.attach("fidelity_plugin")
    assert result["status"] == "error"
    assert "plugin initialization failed" in result["error"]
    assert "rollback cleanup failed" in result["error"]
    assert "underlying cause" in result["error"]


def test_plugin_restore_dependent_failure_is_not_success(host, monkeypatch):
    assert host.attach("fidelity_dependent")["status"] == "ok"
    assert host.detach("fidelity_plugin")["status"] == "ok"
    original = host._attach_plugin
    def attach(plugin_id):
        if plugin_id == "fidelity_dependent":
            host._set_state(plugin_id, attached=False, status="load_failed", error="dependent could not restart")
            return RuntimeStatus.ERROR
        return original(plugin_id)
    monkeypatch.setattr(host, "_attach_plugin", attach)
    result = host.attach("fidelity_plugin")
    assert result["status"] == "error"
    assert result["attached"] is True
    assert "dependent could not restart" in result["error"]


def test_package_wrapper_delivers_exception_chain(runtime):
    provider = PluginsIntrospectionProvider(SimpleNamespace())
    mount(runtime, "package_failure_probe", lambda _: _attach_effect_receipt(
        provider._package_result(chained_failure), DIRECT_LOCAL_READ))
    result = invoke(runtime, "package_failure_probe", {})
    assert "underlying cause" in result.llm_text
    assert "hidden-token" not in result.llm_text
    assert metadata(result)["error_code"] == "package_operation_failed"


def test_lsp_rpc_failure_keeps_cause_and_remote_payload(runtime, tmp_path, monkeypatch):
    from pal.lsp.ipc import LspManagerRpcError
    provider = LspManagerPluginProvider(runtime_root=tmp_path)
    def fail():
        try:
            chained_failure("LSP connection failed")
        except RuntimeError as cause:
            raise LspManagerRpcError("LSP wrapper", payload={"stage": "open transport"}) from cause
    monkeypatch.setattr(provider, "_ensure_manager_started", fail)
    mount(runtime, "lsp_failure_probe", lambda _: provider.definition(CapabilityCall(
        name="probe", args={"file": "test.py", "line": 0, "character": 0})))
    result = invoke(runtime, "lsp_failure_probe", {})
    assert not result.ok and "underlying cause" in result.llm_text
    assert "open transport" in result.llm_text
    assert "hidden-token" not in result.llm_text
    assert metadata(result)["error_code"] == "lsp_rpc_failed"


def test_lsp_attach_diagnostic_does_not_recrop_retained_stderr():
    stderr = "retained-stderr-start" + "x" * 4000 + "retained-stderr-end"
    try:
        chained_failure("LSP initialization failed")
    except RuntimeError as exc:
        detail = _attach_error_detail(exc, SimpleNamespace(stderr_tail_text=lambda: stderr))
    assert "underlying cause" in detail and stderr in detail
    assert "hidden-token" not in detail


@pytest.mark.parametrize("eventual_success", [False, True])
def test_lsp_retries_preserve_each_failed_attempt(tmp_path, monkeypatch, eventual_success):
    manager = LspManager(tmp_path)
    state = LspServerState(file_config=LspServerFileConfig(
        config=LspServerConfig(server_id="probe", command=(sys.executable,), language_ids=("python",)),
        source="test", config_path=str(tmp_path / "probe.toml")), config_path=tmp_path / "probe.toml")
    monkeypatch.setattr(manager, "_select_state", lambda _: state)
    monkeypatch.setattr(manager, "_unavailable_reason", lambda *a: "")
    monkeypatch.setattr(manager, "_ensure_attached", AsyncMock())
    monkeypatch.setattr(manager, "_discard_workspace_session_locked", AsyncMock())
    monkeypatch.setattr(manager, "_connector_for_workspace", lambda *a: object())
    attempts = []
    async def operation(*args, **kwargs):
        attempts.append(1)
        if eventual_success and len(attempts) == 2:
            return {"status": "ok", "result": ["recovered symbol"]}
        try:
            raise OSError(f"underlying attempt {len(attempts)}")
        except OSError as cause:
            raise LspProtocolError(f"request attempt {len(attempts)} failed") from cause
    monkeypatch.setattr(manager, "_run_lsp_operation_with_connector", operation)
    payload = asyncio.run(manager.run_lsp_operation("workspace_symbols", {"workspace_root": str(tmp_path)}))
    from pal.execution import register_with_core
    from pal.lsp import build_lsp_plugin
    core = PalCore()
    register_with_core(core.context)
    handle = build_lsp_plugin(runtime_root=tmp_path).register_with_core(core.context)
    monkeypatch.setattr(handle.introspection_provider, "_request_or_error", lambda *args: payload)
    core.publish_module_capabilities("execution")
    core.publish_module_capabilities("lsp")
    try:
        result = invoke(core.context.execution_runtime, "search_lsp_workspace_symbols",
            {"workspace_root": str(tmp_path), "query": "symbol"})
    finally:
        core.close()
    assert result.ok is eventual_success
    assert "underlying attempt 1" in result.llm_text
    assert len(payload["attempt_errors"]) == (1 if eventual_success else 2)
    assert payload["status"] == ("ok" if eventual_success else "unavailable")
    if not eventual_success:
        assert "underlying attempt 2" in result.llm_text


@pytest.mark.parametrize("kind,effect,retry", [("failed", "applied", "do_not_retry"), ("rejected", "not_started", "correct_input")])
def test_worker_adapter_preserves_untyped_failure_and_attachments(runtime, kind, effect, retry):
    context = (ToolContextMessageIR(content="worker diagnostic attachment", semantic_kind="reference"),)
    ref = runtime.result_snapshots.capture("worker complete diagnostic", call_id="worker-original", lifetime="review")
    descriptor, binding = _workflow_capability(name="op_bunshin_artifact_write",
        spec=_WORKSPACE_TOOL_SPECS["op_bunshin_artifact_write"], handler=lambda *a: ToolExecutionResult(
            name="write_artifact", ok=False, text="original backend failure", llm_text="worker summary",
            structured={"error_code": "worker_specific_error", "kind": kind, "effect": effect,
                        "retry": retry, "diagnostic": "worker backend detail", "recovery": "preserve existing artifact"},
            context_messages=context, snapshot_refs=(ref,)))
    # Exercise the adapter without coupling this test to artifact-write inputs.
    raw = asyncio.run(binding.async_callable(CapabilityCall(name=descriptor.canonical_path)))
    mount(runtime, "worker_adapter_probe", lambda _: raw)
    result = invoke(runtime, "worker_adapter_probe", {})
    assert not result.ok
    assert "original backend failure" in result.llm_text and "worker backend detail" in result.llm_text
    assert "preserve existing artifact" in result.llm_text
    info = metadata(result)
    assert (info["kind"], info["effect"], info["retry"], info["error_code"]) == (kind, effect, retry, "worker_specific_error")
    assert result.context_messages == context and ref in result.snapshot_refs


@pytest.mark.parametrize("started", [False, True])
def test_worker_submission_error_keeps_underlying_cause(runtime, started):
    try:
        try:
            raise OSError("submission storage root cause")
        except OSError as cause:
            raise SubmissionValidationError("submission parsing wrapper") from cause
    except SubmissionValidationError as exc:
        raw = submission_error_result(new_tool_call(name="submit", args={}), exc,
            submission_started=started, invalid_code="bad_submission", correction="fix content")
    mount(runtime, "submission_failure_probe", lambda _: raw.invocation_result)
    result = invoke(runtime, "submission_failure_probe", {})
    assert "submission storage root cause" in result.llm_text
    assert metadata(result)["kind"] == "failed"
    assert metadata(result)["effect"] == ("unknown" if started else "not_started")


def test_package_background_job_keeps_exception_chain_and_long_details(tmp_path):
    from pal.packages.jobs import PackageJobs
    jobs = PackageJobs(SimpleNamespace(runtime_root=tmp_path))
    def fail():
        try:
            raise OSError("package root cause")
        except OSError as cause:
            raise RuntimeError("package failure start " + "x" * 5000 + " package failure end") from cause
    jobs.service = SimpleNamespace(prepare=fail, status=lambda: {})
    try:
        result = jobs.start("prepare", wait_ms=1000)
        assert result["status"] == "failed"
        assert "package root cause" in result["error"]
        assert "package failure start" in result["error"] and "package failure end" in result["error"]
    finally:
        jobs.shutdown()


@pytest.mark.parametrize("timeout", [False, True])
def test_package_command_failure_keeps_output_before_tail(tmp_path, timeout):
    import subprocess
    from pal.packages.process import PackageError, error_text, run_command
    code = "import sys,time; print('COMMAND_OUTPUT_START' + 'x'*6000 + 'COMMAND_OUTPUT_END', flush=True); "
    code += "time.sleep(5)" if timeout else "sys.exit(2)"
    with pytest.raises(subprocess.TimeoutExpired if timeout else PackageError) as caught:
        run_command([sys.executable, "-c", code], cwd=tmp_path, timeout=0.5 if timeout else 5)
    delivered = error_text(caught.value)
    assert "COMMAND_OUTPUT_START" + "x" * 6000 + "COMMAND_OUTPUT_END" in delivered


def test_plugin_manifest_scan_reports_nested_cause(host, monkeypatch):
    monkeypatch.setattr(host, "_read_manifest", lambda *_: chained_failure("manifest parse failed"))
    result = host.rescan()
    assert "underlying cause" in "\n".join(result["scan_errors"])


@pytest.mark.parametrize("action", ["read_server", "rescan", "image_prepare"])
def test_mcp_management_error_cannot_be_overwritten_by_cached_success(runtime, tmp_path, monkeypatch, action):
    from pal.mcp.plugin import McpManagerPluginProvider
    provider = McpManagerPluginProvider(runtime_root=tmp_path, core_context=SimpleNamespace())
    provider.last_health = {"status": "ok", "error": "old error", "manager_running": True, "result": "OLD_RESULT"}
    monkeypatch.setattr(provider, "_ensure_manager_started", lambda: chained_failure("current MCP failure"))
    monkeypatch.setattr(provider, "_prepare_image_payload", lambda *a, **kw: chained_failure("current MCP failure"))
    def handler(_):
        raw = getattr(provider, action)(CapabilityCall(name="probe", args={"name": "server"}))
        return _attach_effect_receipt(raw, DIRECT_LOCAL_READ)
    mount(runtime, "mcp_management_probe", handler)
    result = invoke(runtime, "mcp_management_probe", {})
    assert not result.ok
    assert "current MCP failure" in result.llm_text and "underlying cause" in result.llm_text
    assert "OLD_RESULT" not in result.llm_text
    assert "hidden-token" not in result.llm_text
    assert metadata(result)["error_code"] == ("mcp_image_prepare_failed" if action == "image_prepare" else "mcp_manager_failed")
    shown = provider._status_payload()
    assert shown["manager_running"] is False
    assert shown["cached_snapshot"]["result"] == "OLD_RESULT"
