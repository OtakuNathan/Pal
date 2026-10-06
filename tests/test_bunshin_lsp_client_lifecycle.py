"""Real role/provider lifecycles with synthetic storage and mocked LSP transport."""
from __future__ import annotations

import asyncio
import errno
import os
import socket
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest

from pal.bunshin.ipc import BUNSHIN_RUNTIME_DB_PATH_ENV
from pal.bunshin.runner_components import runtime_build
from pal.core import MainContext
from pal.execution.contracts import CapabilityCall
from pal.foundation import PalV2Database
from pal.foundation.sidecar import SidecarRpcClient
from pal.lsp import build_lsp_plugin
from pal.lsp.plugin import LspManagerPluginProvider
from pal.memory.storage import MemoryStorage
from pal.web_fetch.browser_service import BrowserServiceManager
from pal.wizard.runtime import ALL_MODELS


class _HostLsp:
    def __init__(self, root: Path) -> None:
        self.available = True
        self.health = {
            "ok": True,
            "health_source": "lsp_manager",
            "lifecycle_protocol": "plugin_raii.v1",
            "manager_pid": 900_001,
            "shutdown_requested": False,
        }
        self.calls: list[tuple[str, dict]] = []
        self.unlinks: list[Path] = []
        self.endpoint_paths = (
            root / "data" / "lsp" / "manager.sock",
            root / "data" / "lsp" / "manager.port",
        )
        self.endpoint_paths[0].parent.mkdir(parents=True)
        for path in self.endpoint_paths:
            path.write_text("resident-owned endpoint", encoding="utf-8")

    def assert_untouched(self) -> None:
        assert not self.unlinks
        assert not any(method in {"shutdown", "rescan"} for method, _ in self.calls)
        for path in self.endpoint_paths:
            assert path.read_text(encoding="utf-8") == "resident-owned endpoint"


@pytest.fixture
def host_lsp(tmp_path, monkeypatch):
    host = _HostLsp(tmp_path)

    async def request(client, method, params=None):
        assert client.endpoint.runtime_root == tmp_path
        assert client.endpoint.name == "lsp"
        host.calls.append((method, dict(params or {})))
        if not host.available:
            raise ConnectionError("resident LSP manager unavailable")
        if method == "health":
            return dict(host.health)
        if method in {"shutdown", "rescan"}:
            raise AssertionError(f"role attempted host lifecycle RPC: {method}")
        return {"status": "ok", "operation": method, "result": []}

    # Keep the actual provider, client and sync/async RPC bridge. Replace only
    # the transport boundary; any unexpected real socket/process use must fail.
    monkeypatch.setattr(SidecarRpcClient, "_request_once", request)
    monkeypatch.setattr(socket.socket, "connect", Mock(side_effect=AssertionError("socket forbidden")))
    spawn = Mock(side_effect=AssertionError("process creation forbidden"))
    kill = Mock(side_effect=AssertionError("process termination forbidden"))
    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(os, "killpg", kill)
    original_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path in host.endpoint_paths:
            host.unlinks.append(path)
            raise OSError(errno.EROFS, "Read-only file system", str(path))
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    yield host
    host.assert_untouched()
    spawn.assert_not_called()
    kill.assert_not_called()


@pytest.mark.parametrize("operation", ["status", "diagnostics", "definition", "prepare_workspace"])
def test_client_only_provider_queries_reuse_host_without_taking_ownership(tmp_path, host_lsp, operation):
    provider = LspManagerPluginProvider(runtime_root=tmp_path, client_only=True)
    args = {"file": "sample.py", "workspace_root": str(tmp_path), "name": "pyright"}

    result = getattr(provider, operation)(CapabilityCall(name=operation, args=args))

    assert result.status == "ok"
    assert result.structured["operation"] == operation
    assert host_lsp.calls == [
        ("health", {}),
        (operation, {"file": "sample.py", "workspace_root": str(tmp_path), "server_id": "pyright"}),
    ]
    assert provider.process is None
    assert provider._status_payload()["manager_owned"] is False
    provider.stop_manager()
    assert len(host_lsp.calls) == 2


@pytest.mark.parametrize("failure", ["missing", "invalid_health", "incompatible", "shutting_down"])
def test_client_only_provider_reports_unusable_host_without_repair(tmp_path, host_lsp, failure):
    provider = LspManagerPluginProvider(runtime_root=tmp_path, client_only=True)
    if failure == "missing":
        host_lsp.available = False
    elif failure == "invalid_health":
        host_lsp.health["ok"] = False
    elif failure == "incompatible":
        host_lsp.health["lifecycle_protocol"] = "unsupported"
    else:
        host_lsp.health["shutdown_requested"] = True
    provider.last_health = {"status": "ok", "result": "stale-success"}

    result = provider.diagnostics(CapabilityCall(name="read_lsp_diagnostics", args={"file": "sample.py"}))

    assert result.status == result.structured["status"] == "error"
    assert result.structured["operation"] == "diagnostics"
    assert result.structured["error"]
    assert "result" not in result.structured
    assert "stale-success" not in result.llm_text
    assert provider.process is None
    provider.stop_manager()
    assert host_lsp.calls == [("health", {})]


@pytest.mark.parametrize("available", [True, False])
def test_client_only_attachment_start_and_shutdown_do_not_manage_host(tmp_path, host_lsp, available):
    host_lsp.available = available
    handle = build_lsp_plugin(runtime_root=tmp_path, client_only=True).register_with_core(MainContext())
    provider = handle.ports["lsp"]
    assert provider.client_only is True
    handle.shutdown_sync()
    assert host_lsp.calls == []
    if available:
        provider.start_manager()
    else:
        with pytest.raises(ConnectionError, match="resident LSP manager unavailable"):
            provider.start_manager()
        assert provider.last_health["healthy"] is False
    assert host_lsp.calls == [("health", {})]
    # Repeated role shutdown must remain harmless even if the host disappears.
    handle.shutdown_sync()
    host_lsp.available = False
    handle.shutdown_sync()
    assert host_lsp.calls == [("health", {})]


def test_client_only_query_reports_host_loss_and_reuses_recovered_host(tmp_path, host_lsp):
    provider = LspManagerPluginProvider(runtime_root=tmp_path, client_only=True)
    call = CapabilityCall(name="read_lsp_diagnostics", args={"file": "sample.py"})
    assert provider.diagnostics(call).status == "ok"
    host_lsp.available = False
    failed = provider.diagnostics(call)
    assert failed.status == "error"
    assert "resident LSP manager unavailable" in failed.structured["error"]
    assert "result" not in failed.structured
    host_lsp.available = True
    host_lsp.health["manager_pid"] = 900_002
    assert provider.diagnostics(call).status == "ok"
    assert provider.last_error == ""
    assert provider.process is None
    provider.stop_manager()
    assert [method for method, _ in host_lsp.calls] == [
        "health", "diagnostics", "health", "health", "diagnostics",
    ]


def test_client_only_rescan_is_rejected_without_contacting_host(tmp_path, host_lsp):
    provider = LspManagerPluginProvider(runtime_root=tmp_path, client_only=True)

    result = provider.rescan()

    assert result.status == result.structured["status"] == "error"
    assert result.structured["operation"] == "rescan"
    assert "cannot rescan the resident manager" in result.structured["error"]
    assert host_lsp.calls == []
    provider.stop_manager()
    assert host_lsp.calls == []


def test_resident_plugin_still_defaults_to_owning_mode(tmp_path):
    bundle = build_lsp_plugin(runtime_root=tmp_path)
    handle = bundle.register_with_core(MainContext())
    assert bundle.client_only is False
    assert handle.ports["lsp"].client_only is False


@pytest.mark.parametrize("llm_authority", ["manager_proxy", "host", "none"])
@pytest.mark.parametrize("available", [True, False])
def test_all_slim_runtime_modes_query_and_close_without_owning_lsp(
    tmp_path, monkeypatch, host_lsp, llm_authority, available,
):
    database = PalV2Database(tmp_path / "pal.sqlite3")
    database.initialize(ALL_MODELS)
    database.close()
    storage = MemoryStorage(tmp_path)
    storage.create_initial()
    storage.pin("synthetic-workflow")
    monkeypatch.setenv(BUNSHIN_RUNTIME_DB_PATH_ENV, str(tmp_path / "pal.sqlite3"))
    monkeypatch.setenv("PAL_BUNSHIN_WEB_BROKER", "0")
    if llm_authority == "host":
        monkeypatch.delenv("PAL_BUNSHIN_SANDBOXED", raising=False)
    else:
        monkeypatch.setenv("PAL_BUNSHIN_SANDBOXED", "1")
    monkeypatch.setattr(runtime_build, "build_role_llm", Mock(return_value=None))
    monkeypatch.setattr("pal.execution.worker_extensions.activate_worker_extension", Mock())
    monkeypatch.setattr(BrowserServiceManager, "shutdown_async", AsyncMock())
    host_lsp.available = available

    bundle = runtime_build.build_slim_bunshin_runtime(
        tmp_path, run_id="synthetic-role", llm_authority=llm_authority,
        memory_workflow_id="synthetic-workflow", snapshot_root=tmp_path / "output",
    )
    try:
        provider = bundle.module_registry.require("lsp").ports["lsp"]
        assert isinstance(provider, LspManagerPluginProvider)
        assert provider.client_only is True
        assert provider.client._client.unix_only is (llm_authority != "host")
        result = provider.diagnostics(CapabilityCall(
            name="read_lsp_diagnostics", args={"file": "sample.py"},
        ))
        assert result.status == ("ok" if available else "error")
        assert provider.process is None
        calls_before_close = list(host_lsp.calls)
    finally:
        # Do not patch LSP shutdown: this is the actual slim-runtime close path.
        asyncio.run(bundle.close())
    assert host_lsp.calls == calls_before_close
    assert [method for method, _ in host_lsp.calls] == (
        ["health", "diagnostics"] if available else ["health"]
    )
