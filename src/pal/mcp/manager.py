from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.foundation import utc_now
from pal.foundation.sidecar import dispatch_sidecar_request, handle_sidecar_client
from pal.mcp.config import McpServerFileConfig, load_mcp_server_file
from pal.mcp.connector import AsyncStdioMcpConnector
from pal.mcp.ipc import cleanup_manager_endpoint, mcp_config_root, start_manager_server
from pal.mcp.model import McpDiscoverySnapshot, McpProtocolError, McpServerConfig


@dataclass
class McpServerState:
    file_config: McpServerFileConfig
    config_path: Path
    connector: AsyncStdioMcpConnector | None = None
    snapshot: McpDiscoverySnapshot | None = None
    attached: bool = False
    last_error: str = ""
    last_attached_at: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def config(self) -> McpServerConfig:
        return self.file_config.config


@dataclass
class McpManager:
    runtime_root: Path
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("pal.mcp.manager"))
    server: asyncio.base_events.Server | None = None
    endpoint_info: dict[str, Any] = field(default_factory=dict)
    states: dict[str, McpServerState] = field(default_factory=dict)
    started_at: str = field(default_factory=utc_now)
    last_rescan_at: str = ""
    last_error: str = ""
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    _manager_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def run(self) -> None:
        self.server, self.endpoint_info = await start_manager_server(self.runtime_root, self._handle_client)
        self.logger.info("mcp manager listening: %s", self.endpoint_info)
        await self.rescan()
        async with self.server:
            serve_task = asyncio.create_task(self.server.serve_forever(), name="mcp-manager-serve")
            try:
                await self._shutdown_event.wait()
            finally:
                serve_task.cancel()
                self.server.close()
                await self.server.wait_closed()
                with contextlib.suppress(asyncio.CancelledError):
                    await serve_task
                await self.close_all()
                await cleanup_manager_endpoint(self.runtime_root)
                self.logger.info("mcp manager stopped")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_sidecar_client(reader, writer, self._dispatch)

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        return await dispatch_sidecar_request(
            request,
            self._call_method,
            error_kind=lambda exc: "protocol" if isinstance(exc, McpProtocolError) else "manager",
            logger=self.logger,
        )

    async def _call_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "health":
            return self.health()
        if method == "rescan":
            return await self.rescan()
        if method == "list_servers":
            return {"items": [self._server_summary(state) for state in sorted(self.states.values(), key=lambda item: item.config.server_id)]}
        if method == "read_server":
            return self.read_server(str(params.get("server_id") or ""))
        if method == "snapshot":
            return self.snapshot()
        if method == "attach_server":
            return await self.attach_server(str(params.get("server_id") or ""))
        if method == "detach_server":
            return await self.detach_server(str(params.get("server_id") or ""))
        if method == "call_tool":
            return await self.call_tool(
                str(params.get("server_id") or ""),
                str(params.get("tool_name") or ""),
                dict(params.get("arguments") or {}),
            )
        if method == "render_prompt":
            return await self.render_prompt(
                str(params.get("server_id") or ""),
                str(params.get("prompt_name") or ""),
                dict(params.get("arguments") or {}),
            )
        if method == "shutdown":
            self._shutdown_event.set()
            return {"ok": True}
        raise ValueError(f"unknown MCP manager method: {method}")

    def health(self) -> dict[str, Any]:
        attached = [state for state in self.states.values() if state.attached]
        return {
            "ok": True,
            "health_source": "mcp_manager",
            "lifecycle_protocol": "plugin_raii.v1",
            "manager_pid": os.getpid(),
            "shutdown_requested": self._shutdown_event.is_set(),
            "started_at": self.started_at,
            "last_rescan_at": self.last_rescan_at,
            "last_error": self.last_error,
            "server_count": len(self.states),
            "attached_count": len(attached),
            "tool_count": sum(len(state.snapshot.tools) for state in attached if state.snapshot is not None),
            "prompt_count": sum(len(state.snapshot.prompts) for state in attached if state.snapshot is not None),
            "config_root": str(mcp_config_root(self.runtime_root)),
            **dict(self.endpoint_info),
        }

    async def rescan(self) -> dict[str, Any]:
        async with self._manager_lock:
            root = mcp_config_root(self.runtime_root)
            root.mkdir(parents=True, exist_ok=True)
            discovered: dict[str, tuple[McpServerFileConfig, Path]] = {}
            errors: list[str] = []
            for path in sorted((*root.glob("*.toml"), *root.glob("*.json"))):
                try:
                    loaded_configs = load_mcp_server_file(path)
                    if any(config.config.env for config in loaded_configs):
                        # MCP env blocks commonly hold bearer tokens.  Keep
                        # them private even when a config was copied in under
                        # a permissive umask.
                        path.chmod(0o600)
                    for config in loaded_configs:
                        discovered[config.config.server_id] = (config, path)
                except Exception as exc:
                    errors.append(f"{path}:{exc}")
                    self.logger.exception("failed to read MCP config: %s", path)
            for server_id in sorted(set(self.states) - set(discovered)):
                await self._detach_state(self.states[server_id])
                self.states.pop(server_id, None)
            for server_id, (file_config, path) in discovered.items():
                state = self.states.get(server_id)
                if state is None:
                    state = McpServerState(file_config=file_config, config_path=path)
                    self.states[server_id] = state
                elif state.file_config.to_record_config() != file_config.to_record_config():
                    await self._detach_state(state)
                    state.file_config = file_config
                    state.config_path = path
                    state.snapshot = None
                    state.last_error = ""
                if file_config.enabled:
                    await self._attach_state(state)
                elif state.attached:
                    await self._detach_state(state)
            self.last_rescan_at = utc_now()
            self.last_error = "; ".join(errors)
            return {"server_count": len(self.states), "errors": errors, **self.health()}

    async def attach_server(self, server_id: str) -> dict[str, Any]:
        state = self._require_state(server_id)
        await self._attach_state(state)
        return self.read_server(server_id)

    async def detach_server(self, server_id: str) -> dict[str, Any]:
        state = self._require_state(server_id)
        await self._detach_state(state)
        return self.read_server(server_id)

    async def call_tool(self, server_id: str, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        state = self._require_attached_state(server_id)
        assert state.connector is not None
        async with state.lock:
            return await state.connector.call_tool(tool_name, arguments)

    async def render_prompt(self, server_id: str, prompt_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        state = self._require_attached_state(server_id)
        assert state.connector is not None
        async with state.lock:
            return await state.connector.get_prompt(prompt_name, arguments)

    def read_server(self, server_id: str) -> dict[str, Any]:
        state = self._require_state(server_id)
        payload = self._server_summary(state)
        payload.update(
            {
                "config": _config_summary(state.config),
                "config_path": str(state.config_path),
                "server_info": dict(state.snapshot.server_info) if state.snapshot else {},
                "server_capabilities": dict(state.snapshot.server_capabilities) if state.snapshot else {},
                "tools": [item.name for item in state.snapshot.tools] if state.snapshot else [],
                "prompts": [item.name for item in state.snapshot.prompts] if state.snapshot else [],
                "snapshot": state.snapshot.to_dict() if state.snapshot else None,
            }
        )
        if state.connector is not None:
            payload["stderr_tail"] = list(getattr(state.connector, "_stderr_lines", [])[-20:])
        return payload

    def snapshot(self) -> dict[str, Any]:
        snapshots = [
            state.snapshot.to_dict()
            for state in sorted(self.states.values(), key=lambda item: item.config.server_id)
            if state.attached and state.snapshot is not None
        ]
        return {"snapshots": snapshots, **self.health()}

    async def close_all(self) -> None:
        for state in list(self.states.values()):
            await self._detach_state(state)

    async def _attach_state(self, state: McpServerState) -> None:
        async with state.lock:
            if state.attached and state.connector is not None and state.snapshot is not None:
                return
            await self._detach_state_locked(state)
            connector = AsyncStdioMcpConnector(state.config)
            try:
                await connector.initialize()
                tools = await connector.list_tools_all()
                prompts = (
                    await connector.list_prompts_all()
                    if _server_has_prompts_capability(connector)
                    else ()
                )
                snapshot = McpDiscoverySnapshot(
                    server_id=state.config.server_id,
                    transport=state.config.transport,
                    server_info={**dict(connector.server_info), "remote": _config_summary(state.config)},
                    server_capabilities=dict(connector.server_capabilities),
                    tools=tuple(tools),
                    prompts=tuple(prompts),
                    discovered_at=utc_now(),
                ).with_hash()
            except Exception as exc:
                await connector.close()
                state.last_error = f"{exc.__class__.__name__}: {exc}"
                state.attached = False
                state.connector = None
                state.snapshot = None
                self.logger.exception("failed to attach MCP server: %s", state.config.server_id)
                return
            state.connector = connector
            state.snapshot = snapshot
            state.attached = True
            state.last_error = ""
            state.last_attached_at = utc_now()

    async def _detach_state(self, state: McpServerState) -> None:
        async with state.lock:
            await self._detach_state_locked(state)

    async def _detach_state_locked(self, state: McpServerState) -> None:
        connector = state.connector
        if connector is not None:
            await connector.close()
        state.connector = None
        state.snapshot = None
        state.attached = False

    def _require_state(self, server_id: str) -> McpServerState:
        state = self.states.get(server_id)
        if state is None:
            raise KeyError(f"unknown MCP server: {server_id}")
        return state

    def _require_attached_state(self, server_id: str) -> McpServerState:
        state = self._require_state(server_id)
        if not state.attached or state.connector is None:
            raise McpProtocolError(f"MCP server is not attached: {server_id}")
        return state

    def _server_summary(self, state: McpServerState) -> dict[str, Any]:
        return {
            "server_id": state.config.server_id,
            "enabled": state.file_config.enabled,
            "attached": state.attached,
            "transport": state.config.transport,
            "remote_type": state.config.transport,
            "remote_address": _remote_address(state.config),
            "tool_count": len(state.snapshot.tools) if state.snapshot else 0,
            "prompt_count": len(state.snapshot.prompts) if state.snapshot else 0,
            "last_error": state.last_error,
            "last_attached_at": state.last_attached_at,
        }


def _config_summary(config: McpServerConfig) -> dict[str, Any]:
    return {
        "server_id": config.server_id,
        "transport": config.transport,
        "remote_type": config.transport,
        "remote_address": _remote_address(config),
        "command": list(config.command),
        "cwd": config.cwd,
        "trust_level": config.trust_level,
    }


def _server_has_prompts_capability(connector: AsyncStdioMcpConnector) -> bool:
    return "prompts" in connector.server_capabilities


def _remote_address(config: McpServerConfig) -> str:
    if config.transport == "stdio":
        return " ".join(config.command)
    return config.transport
