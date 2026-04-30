from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.channel.endpoints.socket_protocol import pack_socket_message, read_socket_message


def mcp_runtime_dir(runtime_root: Path) -> Path:
    return Path(runtime_root) / "data" / "mcp"


def mcp_socket_path(runtime_root: Path) -> Path:
    return mcp_runtime_dir(runtime_root) / "manager.sock"


def mcp_port_path(runtime_root: Path) -> Path:
    return mcp_runtime_dir(runtime_root) / "manager.port"


def mcp_config_root(runtime_root: Path) -> Path:
    return Path(runtime_root) / "plugins" / "mcp"


def mcp_log_path(runtime_root: Path) -> Path:
    return Path(runtime_root) / "data" / "mcp" / "manager.log"


class McpManagerRpcError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "protocol", payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.payload = dict(payload or {})


@dataclass
class McpManagerClient:
    runtime_root: Path
    request_timeout_seconds: float = 300.0

    @property
    def socket_path(self) -> Path:
        return mcp_socket_path(self.runtime_root)

    async def request(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = str(uuid4())
        reader, writer = await open_manager_connection(self.runtime_root)
        try:
            writer.write(pack_socket_message({"type": "request", "id": request_id, "method": method, "params": dict(params or {})}))
            await writer.drain()
            response = await asyncio.wait_for(read_socket_message(reader), timeout=self.request_timeout_seconds)
        finally:
            writer.close()
            await writer.wait_closed()
        if str(response.get("id") or "") != request_id:
            raise McpManagerRpcError("MCP manager returned mismatched request id", payload=response)
        if not bool(response.get("ok")):
            error = dict(response.get("error") or {})
            raise McpManagerRpcError(
                str(error.get("message") or "MCP manager request failed"),
                kind=str(error.get("kind") or "protocol"),
                payload=error,
            )
        result = response.get("result")
        return dict(result or {})

    def request_sync(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return _run_blocking(self.request(method, params))

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


def _run_blocking(awaitable):
    if not inspect.isawaitable(awaitable):
        return awaitable
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, awaitable).result()


async def open_manager_connection(runtime_root: Path):
    if hasattr(asyncio, "open_unix_connection"):
        return await asyncio.open_unix_connection(str(mcp_socket_path(runtime_root)))
    port_text = mcp_port_path(runtime_root).read_text(encoding="utf-8").strip()
    return await asyncio.open_connection("127.0.0.1", int(port_text))


async def start_manager_server(runtime_root: Path, handler):
    mcp_runtime_dir(runtime_root).mkdir(parents=True, exist_ok=True)
    if hasattr(asyncio, "start_unix_server"):
        path = mcp_socket_path(runtime_root)
        await _prepare_unix_socket(path)
        server = await asyncio.start_unix_server(handler, path=str(path))
        return server, {"transport": "unix", "socket_path": str(path)}
    port = _choose_loopback_port()
    server = await asyncio.start_server(handler, host="127.0.0.1", port=port)
    mcp_port_path(runtime_root).write_text(str(port), encoding="utf-8")
    return server, {"transport": "tcp_loopback", "host": "127.0.0.1", "port": port}


async def cleanup_manager_endpoint(runtime_root: Path) -> None:
    if hasattr(asyncio, "start_unix_server"):
        path = mcp_socket_path(runtime_root)
        if path.exists():
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
    else:
        path = mcp_port_path(runtime_root)
        if path.exists():
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)


async def _prepare_unix_socket(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        return
    try:
        reader, writer = await asyncio.open_unix_connection(str(path))
    except (FileNotFoundError, ConnectionRefusedError, ConnectionError, OSError):
        os.unlink(path)
        return
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    raise RuntimeError(f"socket already in use: {path}")


def _choose_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])
