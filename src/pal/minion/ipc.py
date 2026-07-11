from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.foundation.sidecar import (
    SidecarEndpoint,
    SidecarRpcClient,
    SidecarRpcError,
    cleanup_sidecar_endpoint,
    open_sidecar_connection,
    python_subprocess_env,
    run_blocking,
    start_sidecar_server,
)


def minion_runtime_dir(runtime_root: Path) -> Path:
    return Path(runtime_root) / "data" / "minion"


def minion_socket_path(runtime_root: Path) -> Path:
    return _minion_endpoint(runtime_root).socket_path


def minion_port_path(runtime_root: Path) -> Path:
    return _minion_endpoint(runtime_root).port_path


class MinionManagerRpcError(SidecarRpcError):
    pass


DEFAULT_MINION_MANAGER_REQUEST_TIMEOUT_SECONDS = 300.0


@dataclass
class MinionManagerClient:
    runtime_root: Path
    request_timeout_seconds: float = DEFAULT_MINION_MANAGER_REQUEST_TIMEOUT_SECONDS
    _client: SidecarRpcClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = SidecarRpcClient(
            endpoint=_minion_endpoint(self.runtime_root),
            request_timeout_seconds=self.request_timeout_seconds,
        )

    @property
    def socket_path(self) -> Path:
        return minion_socket_path(self.runtime_root)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await self._client.request(method, params)
        except SidecarRpcError as exc:
            raise MinionManagerRpcError(str(exc), kind=exc.kind, payload=exc.payload) from exc

    def request_sync(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return run_blocking(self.request(method, params))

    def health_sync(self) -> dict[str, Any]:
        return self.request_sync("health")

    def list_runs_sync(self) -> dict[str, Any]:
        return self.request_sync("list_runs")

    def read_run_sync(self, run_id: str) -> dict[str, Any]:
        return self.request_sync("read_run", {"run_id": run_id})

    def send_decision_sync(self, decision: dict[str, Any]) -> dict[str, Any]:
        return self.request_sync("send_decision", {"decision": dict(decision)})

    def send_clarification_sync(self, clarification: dict[str, Any]) -> dict[str, Any]:
        return self.request_sync("send_clarification", {"clarification": dict(clarification)})

    def reload_runtime_config_sync(self) -> dict[str, Any]:
        return self.request_sync("reload_runtime_config")

    def shutdown_sync(self, *, graceful: bool = True, timeout_seconds: float | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"graceful": graceful}
        if timeout_seconds is not None:
            params["timeout_seconds"] = timeout_seconds
        return self.request_sync("shutdown", params)


async def open_manager_connection(runtime_root: Path):
    return await open_sidecar_connection(_minion_endpoint(runtime_root))


async def start_manager_server(runtime_root: Path, handler):
    return await start_sidecar_server(_minion_endpoint(runtime_root), handler)


async def cleanup_manager_endpoint(runtime_root: Path) -> None:
    await cleanup_sidecar_endpoint(_minion_endpoint(runtime_root))


def _minion_endpoint(runtime_root: Path) -> SidecarEndpoint:
    return SidecarEndpoint(runtime_root=Path(runtime_root), name="minion")
