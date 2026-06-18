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
    run_blocking,
    start_sidecar_server,
)


def lsp_runtime_dir(runtime_root: Path) -> Path:
    return Path(runtime_root) / "data" / "lsp"


def lsp_log_path(runtime_root: Path) -> Path:
    return lsp_runtime_dir(runtime_root) / "manager.log"


def lsp_config_root(runtime_root: Path) -> Path:
    return Path(runtime_root) / "plugins" / "lsp" / "servers"


class LspManagerRpcError(SidecarRpcError):
    pass


@dataclass
class LspManagerClient:
    runtime_root: Path
    request_timeout_seconds: float = 180.0
    _client: SidecarRpcClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = SidecarRpcClient(endpoint=_lsp_endpoint(self.runtime_root), request_timeout_seconds=self.request_timeout_seconds)

    @property
    def socket_path(self) -> Path:
        return _lsp_endpoint(self.runtime_root).socket_path

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await self._client.request(method, params)
        except SidecarRpcError as exc:
            raise LspManagerRpcError(str(exc), kind=exc.kind, payload=exc.payload) from exc

    def request_sync(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return run_blocking(self.request(method, params))

    def health_sync(self) -> dict[str, Any]:
        return self.request_sync("health")

    def shutdown_sync(self) -> dict[str, Any]:
        return self.request_sync("shutdown")

    def rescan_sync(self) -> dict[str, Any]:
        return self.request_sync("rescan")

    def status_sync(self) -> dict[str, Any]:
        return self.request_sync("status")

    def doctor_sync(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.request_sync("doctor", params)

    def operation_sync(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        return self.request_sync(method, params)


async def open_manager_connection(runtime_root: Path):
    return await open_sidecar_connection(_lsp_endpoint(runtime_root))


async def start_manager_server(runtime_root: Path, handler):
    return await start_sidecar_server(_lsp_endpoint(runtime_root), handler)


async def cleanup_manager_endpoint(runtime_root: Path) -> None:
    await cleanup_sidecar_endpoint(_lsp_endpoint(runtime_root))


def _lsp_endpoint(runtime_root: Path) -> SidecarEndpoint:
    return SidecarEndpoint(runtime_root=Path(runtime_root), name="lsp")
