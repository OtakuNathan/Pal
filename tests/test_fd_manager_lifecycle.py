from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

import pytest

from pal.lsp.manager import LspManager
from pal.lsp.plugin import LspManagerPluginProvider
from pal.mcp.manager import McpManager
from pal.mcp.plugin import McpManagerPluginProvider


ProviderFactory = Callable[[Path], Any]


class _ManagerClientStub:
    def __init__(self, root: Path, *, health_source: str, manager_pid: int = 900_001) -> None:
        self.socket_path = root / "manager.sock"
        self.port_path = root / "manager.port"
        self.health_source = health_source
        self.manager_pid = manager_pid
        self.alive = True
        self.shutdown_calls = 0

    def health_sync(self) -> dict[str, Any]:
        if not self.alive:
            raise ConnectionError("manager stopped")
        return {
            "ok": True,
            "health_source": self.health_source,
            "lifecycle_protocol": "plugin_raii.v1",
            "manager_pid": self.manager_pid,
            "shutdown_requested": False,
        }

    def shutdown_sync(self) -> dict[str, Any]:
        self.shutdown_calls += 1
        self.alive = False
        return {"ok": True}

    def publish_spawn(self, pid: int) -> None:
        self.manager_pid = pid
        self.alive = True


class _ProcessStub:
    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.returncode: int | None = None

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.returncode = -15

    def kill(self) -> None:
        self.returncode = -9

    def wait(self, timeout: float | None = None) -> int:
        _ = timeout
        if self.returncode is None:
            self.returncode = 0
        return self.returncode


@pytest.mark.parametrize(
    "provider_factory",
    (
        pytest.param(
            lambda root: McpManagerPluginProvider(
                runtime_root=root,
                core_context=None,
            ),
            id="mcp",
        ),
        pytest.param(
            lambda root: LspManagerPluginProvider(runtime_root=root),
            id="lsp",
        ),
    ),
)
def test_manager_stop_waits_for_in_progress_start(
    tmp_path: Path,
    provider_factory: ProviderFactory,
) -> None:
    provider = provider_factory(tmp_path)
    start_entered = threading.Event()
    release_start = threading.Event()
    stop_attempted = threading.Event()
    events: list[str] = []

    def blocked_start() -> None:
        events.append("start_entered")
        start_entered.set()
        assert release_start.wait(timeout=2.0)
        events.append("start_released")

    def record_stop() -> None:
        events.append("stop")

    provider._ensure_manager_started_locked = blocked_start
    provider._stop_manager_locked = record_stop

    start_thread = threading.Thread(target=provider._ensure_manager_started)

    def stop() -> None:
        stop_attempted.set()
        provider._stop_manager()

    stop_thread = threading.Thread(target=stop)
    start_thread.start()
    assert start_entered.wait(timeout=1.0)
    stop_thread.start()
    assert stop_attempted.wait(timeout=1.0)
    time.sleep(0.05)
    assert events == ["start_entered"]

    release_start.set()
    start_thread.join(timeout=2.0)
    stop_thread.join(timeout=2.0)

    assert not start_thread.is_alive()
    assert not stop_thread.is_alive()
    assert events == ["start_entered", "start_released", "stop"]


@pytest.mark.parametrize(
    ("manager_factory", "health_source"),
    (
        pytest.param(McpManager, "mcp_manager", id="mcp"),
        pytest.param(LspManager, "lsp_manager", id="lsp"),
    ),
)
def test_manager_health_identifies_the_serving_process(
    tmp_path: Path,
    manager_factory: Callable[..., Any],
    health_source: str,
) -> None:
    health = manager_factory(runtime_root=tmp_path).health()

    assert health["ok"] is True
    assert health["health_source"] == health_source
    assert health["lifecycle_protocol"] == "plugin_raii.v1"
    assert health["manager_pid"] == os.getpid()
    assert health["shutdown_requested"] is False


@pytest.mark.parametrize(
    ("provider_factory", "health_source", "popen_target"),
    (
        pytest.param(
            lambda root: McpManagerPluginProvider(runtime_root=root, core_context=None),
            "mcp_manager",
            "pal.mcp.plugin.subprocess.Popen",
            id="mcp",
        ),
        pytest.param(
            lambda root: LspManagerPluginProvider(runtime_root=root),
            "lsp_manager",
            "pal.lsp.plugin.subprocess.Popen",
            id="lsp",
        ),
    ),
)
def test_manager_start_retires_orphan_instead_of_adopting_it(
    tmp_path: Path,
    provider_factory: ProviderFactory,
    health_source: str,
    popen_target: str,
) -> None:
    provider = provider_factory(tmp_path)
    client = _ManagerClientStub(tmp_path, health_source=health_source)
    provider.client = client
    spawned = _ProcessStub(pid=900_002)

    def spawn(*args: Any, **kwargs: Any) -> _ProcessStub:
        _ = (args, kwargs)
        client.publish_spawn(spawned.pid)
        return spawned

    killpg_target = popen_target.replace("subprocess.Popen", "os.killpg")

    def kill_group(_pid: int, _signal: int) -> None:
        spawned.kill()

    with patch(popen_target, side_effect=spawn), patch(killpg_target, side_effect=kill_group):
        provider._ensure_manager_started()

    assert client.shutdown_calls == 1
    assert provider._process_status() == (spawned.pid, None)
    assert provider.last_health["manager_pid"] == spawned.pid

    provider._stop_manager()
    assert client.shutdown_calls == 2
    assert provider._process_status() is None


@pytest.mark.parametrize(
    ("provider_factory", "health_source"),
    (
        pytest.param(
            lambda root: McpManagerPluginProvider(runtime_root=root, core_context=None),
            "mcp_manager",
            id="mcp",
        ),
        pytest.param(
            lambda root: LspManagerPluginProvider(runtime_root=root),
            "lsp_manager",
            id="lsp",
        ),
    ),
)
def test_manager_detach_stops_orphan_without_local_process_owner(
    tmp_path: Path,
    provider_factory: ProviderFactory,
    health_source: str,
) -> None:
    provider = provider_factory(tmp_path)
    client = _ManagerClientStub(tmp_path, health_source=health_source)
    provider.client = client

    provider._stop_manager()

    assert client.shutdown_calls == 1
    assert client.alive is False
    assert provider._process_status() is None


@pytest.mark.parametrize(
    ("provider_factory", "health_source"),
    (
        pytest.param(
            lambda root: McpManagerPluginProvider(runtime_root=root, core_context=None),
            "mcp_manager",
            id="mcp",
        ),
        pytest.param(
            lambda root: LspManagerPluginProvider(runtime_root=root),
            "lsp_manager",
            id="lsp",
        ),
    ),
)
def test_manager_endpoint_is_not_reused_before_old_process_exits(
    tmp_path: Path,
    provider_factory: ProviderFactory,
    health_source: str,
) -> None:
    provider = provider_factory(tmp_path)
    client = _ManagerClientStub(tmp_path, health_source=health_source)
    provider.client = client
    client.socket_path.write_text("old-generation", encoding="utf-8")
    client.port_path.write_text("1234", encoding="utf-8")
    health = client.health_sync()
    client.shutdown_sync = lambda: {"ok": True}  # type: ignore[method-assign]
    timeout_target = (
        "pal.mcp.plugin._MANAGER_RETIRE_TIMEOUT_SECONDS"
        if health_source == "mcp_manager"
        else "pal.lsp.plugin._MANAGER_RETIRE_TIMEOUT_SECONDS"
    )

    with patch(timeout_target, 0.01):
        with pytest.raises(RuntimeError, match="did not stop"):
            provider._retire_existing_manager(health)

    assert client.socket_path.read_text(encoding="utf-8") == "old-generation"
    assert client.port_path.read_text(encoding="utf-8") == "1234"
