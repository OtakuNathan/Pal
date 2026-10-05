from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from pal.core import PalCore, register_with_core as register_core_with_core
from pal.execution import register_with_core as register_execution_with_core
from pal.execution.contracts import CapabilityCall
from pal.lsp import build_lsp_plugin
from pal.lsp.plugin import LspManagerPluginProvider
from pal.shared import RuntimeStatus


def test_attach_failure_is_reported_as_failure_not_applied_success() -> None:
    provider = LspManagerPluginProvider(runtime_root=Path(tempfile.mkdtemp()))

    def fail_startup() -> None:
        raise RuntimeError("sidecar unavailable")

    provider._ensure_manager_started = fail_startup  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="sidecar unavailable"):
        provider.start_manager()

    assert "sidecar unavailable" in provider.last_error
    assert provider.last_health["healthy"] is False


def test_prepare_workspace_ready_reports_facts_without_navigation_menu() -> None:
    provider = LspManagerPluginProvider(runtime_root=Path(tempfile.mkdtemp()))
    provider._request_or_error = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "status": "ok",
        "workspace_root": "/workspace",
        "ready": True,
    }

    result = provider.prepare_workspace(
        CapabilityCall(name="prepare_lsp_workspace", args={"workspace_root": "/workspace"})
    )

    assert result.status == "ok"
    assert result.structured is not None
    assert result.structured["workspace_root"] == "/workspace"
    # A ready preparation is complete guidance-free: no navigation menu, no
    # discovery reminder, no trailing direction text.
    assert "next_tools" not in result.structured
    assert not result.affordances
    assert result.recovery_hint == ""
    for token in ("call_tool", "read_tool", "read_lsp_diagnostics", "list_lsp_document_symbols"):
        assert token not in result.llm_text


def test_partial_prepare_offers_bound_doctor_not_a_menu() -> None:
    provider = LspManagerPluginProvider(runtime_root=Path(tempfile.mkdtemp()))
    provider._request_or_error = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "status": "partial",
        "workspace_root": "/workspace",
        "primary_server": "clangd",
        "primary_probe_ready": False,
        "servers": [
            {"server_id": "yaml", "status": "ok"},
            {"server_id": "clangd", "status": "init_failed"},
        ],
    }

    result = provider.prepare_workspace(
        CapabilityCall(name="prepare_lsp_workspace", args={"workspace_root": "/workspace"})
    )

    assert result.status == "ok"
    assert result.structured is not None
    assert result.structured["status"] == "partial"
    assert "next_tools" not in result.structured
    tools = {item.tool: item for item in result.affordances}
    assert set(tools) == {"diagnose_lsp_server"}
    doctor = tools["diagnose_lsp_server"]
    assert doctor.arguments["workspace_root"] == "/workspace"
    assert doctor.arguments["name"] == "clangd"
    assert "list_lsp_document_symbols" not in result.llm_text


def test_prepare_workspace_is_only_resident_lsp_tool() -> None:
    core = PalCore()
    register_core_with_core(core)
    register_execution_with_core(core.context)
    handle = build_lsp_plugin(runtime_root=Path(tempfile.mkdtemp())).register_with_core(core.context)
    try:
        core.publish_module_capabilities("execution")
        core.publish_module_capabilities("lsp")
        names = {
            contract["function"]["name"]
            for contract in core.tool_surface.build_llm_tool_contracts()
        }
        assert "prepare_lsp_workspace" in names
        assert "read_lsp_diagnostics" not in names
        assert "find_lsp_definitions" not in names
        assert "inspect_lsp_status" not in names
    finally:
        handle.shutdown_sync()


def test_failed_request_does_not_replay_cached_success(tmp_path, monkeypatch):
    from unittest.mock import Mock
    provider = LspManagerPluginProvider(runtime_root=tmp_path)
    monkeypatch.setattr(provider, "_ensure_manager_started", Mock())
    monkeypatch.setattr(provider.client, "operation_sync", Mock(return_value={
        "status": "ok", "operation": "hover", "result": {"value": "OLD_HOVER"}, "ok": True,
    }))
    provider._request_or_error("hover", {})
    monkeypatch.setattr(provider, "_ensure_manager_started", Mock(side_effect=RuntimeError("startup failed")))
    result = provider.definition(CapabilityCall(name="find_lsp_definitions", args={"file": "a.py", "line": 0, "character": 0}))
    assert result.status == "error"
    assert result.structured["operation"] == "definition"
    assert "result" not in result.structured
    assert "OLD_HOVER" not in result.llm_text
    assert "startup failed" in result.llm_text
    shown = provider.show(CapabilityCall(name="inspect_lsp_provider", args={}))
    assert shown.structured["manager_running"] is False
    assert shown.structured["cached_snapshot"]["result"]["value"] == "OLD_HOVER"
    assert "startup failed" in shown.structured["last_error"]


def test_rescan_preserves_manager_failure_and_current_exception(tmp_path, monkeypatch):
    from unittest.mock import Mock
    provider = LspManagerPluginProvider(runtime_root=tmp_path)
    monkeypatch.setattr(provider, "_ensure_manager_started", Mock())
    rescan = Mock(return_value={"status": "error", "errors": ["bad.toml: invalid TOML"]})
    monkeypatch.setattr(provider.client, "rescan_sync", rescan)
    failed = provider.rescan()
    assert failed.status == "error"
    assert "bad.toml" in failed.llm_text
    assert "bad.toml" in provider.last_error
    rescan.return_value = {"status": "ok", "errors": []}
    assert provider.rescan().status == "ok"
    assert provider.last_error == ""
    rescan.side_effect = RuntimeError("current transport failure")
    failed = provider.rescan()
    assert failed.status == failed.structured["status"] == "error"
    assert failed.structured["operation"] == "rescan"
    assert "current transport failure" in failed.llm_text


@pytest.mark.parametrize("returncode,expected_running", [(None, True), (1, False)])
def test_manager_liveness_comes_from_process_not_cache(tmp_path, returncode, expected_running):
    from unittest.mock import Mock
    provider = LspManagerPluginProvider(runtime_root=tmp_path)
    provider.last_health = {"ok": True, "manager_running": True, "last_error": "old"}
    provider.last_error = "current"
    provider.process = Mock(pid=123, poll=Mock(return_value=returncode))
    shown = provider._status_payload()
    assert shown["manager_running"] is expected_running
    assert shown["manager_owned"] is expected_running
    assert shown["last_error"] == "current"
    if not expected_running:
        assert provider._manager_running() is False
