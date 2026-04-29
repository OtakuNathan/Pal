from __future__ import annotations

import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from pal.mcp.model import McpServerConfig


@dataclass(frozen=True)
class McpServerFileConfig:
    config: McpServerConfig
    enabled: bool = True

    def to_record_config(self) -> dict[str, Any]:
        payload = asdict(self.config)
        payload["command"] = list(self.config.command)
        payload["enabled"] = self.enabled
        return payload


def load_mcp_server_file(path: Path) -> tuple[McpServerFileConfig, ...]:
    payload = _read_config_payload(path)
    if not isinstance(payload, dict):
        raise ValueError("MCP config root must be an object")
    servers = payload.get("mcpServers")
    if isinstance(servers, dict):
        return tuple(
            _server_from_payload(server_id=str(server_id), payload=_expect_mapping(config, f"mcpServers.{server_id}"))
            for server_id, config in sorted(servers.items(), key=lambda item: str(item[0]))
        )
    return (_server_from_payload(server_id=str(payload.get("server_id") or path.stem), payload=payload),)


def _read_config_payload(path: Path) -> dict[str, Any]:
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        return json.loads(text)
    if suffix == ".toml":
        return tomllib.loads(text)
    raise ValueError(f"unsupported MCP config file type: {suffix}")


def _server_from_payload(*, server_id: str, payload: dict[str, Any]) -> McpServerFileConfig:
    enabled = bool(payload.get("enabled", True))
    command = _normalize_command(payload)
    env = payload.get("env") or {}
    if not isinstance(env, dict):
        raise ValueError(f"MCP server {server_id} env must be an object")
    config = McpServerConfig(
        server_id=server_id,
        command=command,
        cwd=_optional_str(payload.get("cwd")),
        env={str(key): str(value) for key, value in env.items()},
        transport=str(payload.get("transport") or "stdio"),
        protocol_version=str(payload.get("protocol_version") or payload.get("protocolVersion") or "2025-06-18"),
        startup_timeout_ms=int(payload.get("startup_timeout_ms") or payload.get("startupTimeoutMs") or 10_000),
        request_timeout_ms=int(payload.get("request_timeout_ms") or payload.get("requestTimeoutMs") or 30_000),
        shutdown_timeout_ms=int(payload.get("shutdown_timeout_ms") or payload.get("shutdownTimeoutMs") or 5_000),
        kill_on_close=bool(payload.get("kill_on_close", payload.get("killOnClose", True))),
        trust_level=str(payload.get("trust_level") or payload.get("trustLevel") or "unknown"),
    )
    if config.transport != "stdio":
        raise ValueError(f"MCP server {server_id} uses unsupported transport: {config.transport}")
    return McpServerFileConfig(config=config, enabled=enabled)


def _normalize_command(payload: dict[str, Any]) -> tuple[str, ...]:
    command = payload.get("command")
    args = payload.get("args") or ()
    if isinstance(command, str):
        command_parts = (command,)
    elif isinstance(command, list) and all(isinstance(item, str) for item in command):
        command_parts = tuple(command)
    else:
        raise ValueError("MCP server command must be a string or list of strings")
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ValueError("MCP server args must be a list of strings")
    return (*command_parts, *tuple(args))


def _expect_mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)
