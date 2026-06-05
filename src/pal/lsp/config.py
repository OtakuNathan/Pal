from __future__ import annotations

import json
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class LspServerConfig:
    server_id: str
    command: tuple[str, ...]
    args: tuple[str, ...] = ()
    display_name: str = ""
    enabled: bool = True
    extensions: tuple[str, ...] = ()
    language_ids: tuple[str, ...] = ()
    workspace_markers: tuple[str, ...] = ()
    install_hint: str = ""
    startup_timeout_ms: int = 10_000
    request_timeout_ms: int = 30_000
    diagnostics_timeout_ms: int = 2_000

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "command": list(self.command),
            "args": list(self.args),
            "display_name": self.display_name,
            "enabled": self.enabled,
            "extensions": list(self.extensions),
            "language_ids": list(self.language_ids),
            "workspace_markers": list(self.workspace_markers),
            "install_hint": self.install_hint,
            "startup_timeout_ms": self.startup_timeout_ms,
            "request_timeout_ms": self.request_timeout_ms,
            "diagnostics_timeout_ms": self.diagnostics_timeout_ms,
        }


@dataclass(frozen=True)
class LspServerFileConfig:
    config: LspServerConfig
    source: str = "runtime"
    config_path: str = ""

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def to_record_config(self) -> dict[str, Any]:
        payload = self.config.to_dict()
        payload["source"] = self.source
        payload["config_path"] = self.config_path
        return payload


def lsp_config_root(runtime_root: Path) -> Path:
    return Path(runtime_root) / "plugins" / "lsp" / "servers"


def load_lsp_server_file(path: Path, *, source: str = "runtime") -> tuple[LspServerFileConfig, ...]:
    payload = _load_payload(path)
    if isinstance(payload.get("lspServers"), dict):
        return tuple(
            _file_config_from_payload({**dict(value or {}), "server_id": str(server_id)}, path, source=source)
            for server_id, value in payload["lspServers"].items()
            if isinstance(value, dict)
        )
    if isinstance(payload.get("servers"), list):
        return tuple(
            _file_config_from_payload(dict(item or {}), path, source=source)
            for item in payload["servers"]
            if isinstance(item, dict)
        )
    return (_file_config_from_payload(payload, path, source=source),)


def load_builtin_lsp_templates() -> tuple[LspServerFileConfig, ...]:
    root = resources.files("pal.lsp").joinpath("server_templates")
    result: list[LspServerFileConfig] = []
    for item in sorted(root.iterdir(), key=lambda entry: entry.name):
        if item.suffix != ".toml":
            continue
        payload = tomllib.loads(item.read_text(encoding="utf-8"))
        result.append(_file_config_from_payload(payload, Path(str(item)), source="builtin_template"))
    return tuple(result)


def _load_payload(path: Path) -> dict[str, Any]:
    if path.suffix.lower() == ".json":
        return dict(json.loads(path.read_text(encoding="utf-8")))
    return dict(tomllib.loads(path.read_text(encoding="utf-8")))


def _file_config_from_payload(payload: dict[str, Any], path: Path, *, source: str) -> LspServerFileConfig:
    data = dict(payload or {})
    server_id = str(data.get("server_id") or data.get("id") or "").strip()
    if not server_id:
        raise ValueError(f"LSP server config lacks server_id: {path}")
    command = _string_tuple(data.get("command"))
    if not command:
        command = (server_id,)
    args = _string_tuple(data.get("args"))
    config = LspServerConfig(
        server_id=server_id,
        command=command,
        args=args,
        display_name=str(data.get("display_name") or server_id).strip(),
        enabled=bool(data.get("enabled", True)),
        extensions=_normalized_extensions(data.get("extensions")),
        language_ids=_string_tuple(data.get("language_ids")),
        workspace_markers=_string_tuple(data.get("workspace_markers")),
        install_hint=str(data.get("install_hint") or "").strip(),
        startup_timeout_ms=_int(data.get("startup_timeout_ms"), 10_000),
        request_timeout_ms=_int(data.get("request_timeout_ms"), 30_000),
        diagnostics_timeout_ms=_int(data.get("diagnostics_timeout_ms"), 2_000),
    )
    return LspServerFileConfig(config=config, source=source, config_path=str(path))


def _string_tuple(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if not isinstance(value, list | tuple):
        return ()
    result: list[str] = []
    for item in value:
        text = str(item or "").strip()
        if text:
            result.append(text)
    return tuple(result)


def _normalized_extensions(value: Any) -> tuple[str, ...]:
    result = []
    for item in _string_tuple(value):
        text = item if item.startswith(".") else f".{item}"
        result.append(text.lower())
    return tuple(result)


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
