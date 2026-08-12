from __future__ import annotations

import tempfile
from pathlib import Path

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

    result = provider.attach()

    assert result.status == RuntimeStatus.ERROR
    assert result.text == "lsp manager attach failed"
    assert result.structured is not None
    assert "sidecar unavailable" in str(result.structured)


def test_prepare_workspace_result_points_to_indirect_lsp_tools() -> None:
    provider = LspManagerPluginProvider(runtime_root=Path(tempfile.mkdtemp()))
    provider._request_or_error = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "status": "ok",
        "workspace_root": "/workspace",
        "ready": True,
    }

    result = provider.prepare_workspace(
        CapabilityCall(name="lsp_prepare_workspace", args={"workspace_root": "/workspace"})
    )

    assert result.status == "ok"
    assert result.structured is not None
    assert result.structured["next_tools"]["map_code"] == [
        "lsp_document_symbols",
        "lsp_workspace_symbols",
    ]
    assert "call_tool" in result.llm_text
    assert "lsp_diagnostics" in result.llm_text


def test_partial_prepare_points_to_readiness_tools_before_navigation() -> None:
    provider = LspManagerPluginProvider(runtime_root=Path(tempfile.mkdtemp()))
    provider._request_or_error = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "status": "partial",
        "workspace_root": "/workspace",
        "primary_server": "clangd",
        "primary_probe_ready": False,
        "servers": [{"server_id": "yaml", "status": "ok"}],
    }

    result = provider.prepare_workspace(
        CapabilityCall(name="lsp_prepare_workspace", args={"workspace_root": "/workspace"})
    )

    assert result.status == "ok"
    assert result.structured is not None
    assert result.structured["status"] == "partial"
    assert result.structured["next_tools"] == {
        "inspect_readiness": ["lsp_status", "lsp_doctor"],
        "refresh_configuration": ["lsp_rescan"],
    }
    assert "primary language server is not ready" in result.llm_text
    assert "lsp_document_symbols" not in result.llm_text


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
        assert "lsp_prepare_workspace" in names
        assert "lsp_diagnostics" not in names
        assert "lsp_definition" not in names
        assert "lsp_status" not in names
    finally:
        handle.shutdown_sync()
