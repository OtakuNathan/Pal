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


def mcp_runtime_dir(runtime_root: Path) -> Path:
    return Path(runtime_root) / "data" / "mcp"


def mcp_socket_path(runtime_root: Path) -> Path:
    return _mcp_endpoint(runtime_root).socket_path


def mcp_port_path(runtime_root: Path) -> Path:
    return _mcp_endpoint(runtime_root).port_path


def mcp_config_root(runtime_root: Path) -> Path:
    return Path(runtime_root) / "plugins" / "mcp"


def mcp_log_path(runtime_root: Path) -> Path:
    return Path(runtime_root) / "data" / "mcp" / "manager.log"


class McpManagerRpcError(SidecarRpcError):
    pass


@dataclass
class McpManagerClient:
    runtime_root: Path
    request_timeout_seconds: float = 300.0
    _client: SidecarRpcClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._client = SidecarRpcClient(
            endpoint=_mcp_endpoint(self.runtime_root),
            request_timeout_seconds=self.request_timeout_seconds,
        )

    @property
    def socket_path(self) -> Path:
        return mcp_socket_path(self.runtime_root)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            return await self._client.request(method, params)
        except SidecarRpcError as exc:
            raise McpManagerRpcError(str(exc), kind=exc.kind, payload=exc.payload) from exc

    def request_sync(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return run_blocking(self.request(method, params))

    async def health(self) -> dict[str, Any]:
        return await self.request("health")

    def health_sync(self) -> dict[str, Any]:
        return self.request_sync("health")

    async def rescan(self) -> dict[str, Any]:
        return await self.request("rescan")

    def rescan_sync(self) -> dict[str, Any]:
        return self.request_sync("rescan")

    async def snapshot(self) -> dict[str, Any]:
        return await self.request("snapshot")

    def snapshot_sync(self) -> dict[str, Any]:
        return self.request_sync("snapshot")

    async def list_servers(self) -> dict[str, Any]:
        return await self.request("list_servers")

    def list_servers_sync(self) -> dict[str, Any]:
        return self.request_sync("list_servers")

    async def read_server(self, server_id: str) -> dict[str, Any]:
        return await self.request("read_server", {"server_id": server_id})

    def read_server_sync(self, server_id: str) -> dict[str, Any]:
        return self.request_sync("read_server", {"server_id": server_id})

    async def attach_server(self, server_id: str) -> dict[str, Any]:
        return await self.request("attach_server", {"server_id": server_id})

    def attach_server_sync(self, server_id: str) -> dict[str, Any]:
        return self.request_sync("attach_server", {"server_id": server_id})

    async def detach_server(self, server_id: str) -> dict[str, Any]:
        return await self.request("detach_server", {"server_id": server_id})

    def detach_server_sync(self, server_id: str) -> dict[str, Any]:
        return self.request_sync("detach_server", {"server_id": server_id})

    async def call_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.request("call_tool", {"server_id": server_id, "tool_name": tool_name, "arguments": dict(arguments or {})})

    def call_tool_sync(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request_sync("call_tool", {"server_id": server_id, "tool_name": tool_name, "arguments": dict(arguments or {})})

    async def render_prompt(self, server_id: str, prompt_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self.request("render_prompt", {"server_id": server_id, "prompt_name": prompt_name, "arguments": dict(arguments or {})})

    def render_prompt_sync(self, server_id: str, prompt_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return self.request_sync("render_prompt", {"server_id": server_id, "prompt_name": prompt_name, "arguments": dict(arguments or {})})

    async def shutdown(self) -> dict[str, Any]:
        return await self.request("shutdown")

    def shutdown_sync(self) -> dict[str, Any]:
        return self.request_sync("shutdown")


async def open_manager_connection(runtime_root: Path):
    return await open_sidecar_connection(_mcp_endpoint(runtime_root))


async def start_manager_server(runtime_root: Path, handler):
    return await start_sidecar_server(_mcp_endpoint(runtime_root), handler)


async def cleanup_manager_endpoint(runtime_root: Path) -> None:
    await cleanup_sidecar_endpoint(_mcp_endpoint(runtime_root))


def _mcp_endpoint(runtime_root: Path) -> SidecarEndpoint:
    return SidecarEndpoint(runtime_root=Path(runtime_root), name="mcp")
