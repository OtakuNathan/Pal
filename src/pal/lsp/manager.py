from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.foundation import utc_now
from pal.foundation.sidecar import dispatch_sidecar_request, handle_sidecar_client
from pal.lsp.config import LspServerFileConfig, load_builtin_lsp_templates, load_lsp_server_file, lsp_config_root
from pal.lsp.connector import AsyncLspConnector, LspProtocolError
from pal.lsp.ipc import cleanup_manager_endpoint, start_manager_server


@dataclass
class LspServerState:
    file_config: LspServerFileConfig
    config_path: Path
    connector: AsyncLspConnector | None = None
    attached: bool = False
    last_error: str = ""
    last_attached_at: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def server_id(self) -> str:
        return self.file_config.config.server_id


@dataclass
class LspManager:
    runtime_root: Path
    logger: logging.Logger = field(default_factory=lambda: logging.getLogger("pal.lsp.manager"))
    server: asyncio.base_events.Server | None = None
    endpoint_info: dict[str, Any] = field(default_factory=dict)
    states: dict[str, LspServerState] = field(default_factory=dict)
    started_at: str = field(default_factory=utc_now)
    last_rescan_at: str = ""
    last_error: str = ""
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    _manager_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def run(self) -> None:
        self.server, self.endpoint_info = await start_manager_server(self.runtime_root, self._handle_client)
        await self.rescan()
        async with self.server:
            serve_task = asyncio.create_task(self.server.serve_forever(), name="lsp-manager-serve")
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

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await handle_sidecar_client(reader, writer, self._dispatch)

    async def _dispatch(self, request: dict[str, Any]) -> dict[str, Any]:
        return await dispatch_sidecar_request(
            request,
            self._call_method,
            error_kind=lambda exc: "protocol" if isinstance(exc, LspProtocolError) else "manager",
            logger=self.logger,
        )

    async def _call_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "health":
            return self.health()
        if method == "status":
            return self.status()
        if method == "rescan":
            return await self.rescan()
        if method == "doctor":
            return await self.doctor(dict(params or {}))
        if method in {
            "hover",
            "definition",
            "implementation",
            "references",
            "document_symbols",
            "workspace_symbols",
            "diagnostics",
            "prepare_call_hierarchy",
            "incoming_calls",
            "outgoing_calls",
        }:
            return await self.run_lsp_operation(method, dict(params or {}))
        if method == "shutdown":
            self._shutdown_event.set()
            return {"ok": True}
        raise ValueError(f"unknown LSP manager method: {method}")

    def health(self) -> dict[str, Any]:
        return {
            "ok": True,
            "started_at": self.started_at,
            "last_rescan_at": self.last_rescan_at,
            "last_error": self.last_error,
            "server_count": len(self.states),
            "attached_count": len([state for state in self.states.values() if state.attached]),
            "config_root": str(lsp_config_root(self.runtime_root)),
            **dict(self.endpoint_info),
        }

    def status(self) -> dict[str, Any]:
        return {
            **self.health(),
            "servers": [self._server_summary(state) for state in sorted(self.states.values(), key=lambda item: item.server_id)],
        }

    async def rescan(self) -> dict[str, Any]:
        async with self._manager_lock:
            discovered: dict[str, tuple[LspServerFileConfig, Path]] = {}
            errors: list[str] = []
            for template in load_builtin_lsp_templates():
                discovered[template.config.server_id] = (template, Path(template.config_path))
            root = lsp_config_root(self.runtime_root)
            root.mkdir(parents=True, exist_ok=True)
            for path in sorted((*root.glob("*.toml"), *root.glob("*.json"))):
                try:
                    for config in load_lsp_server_file(path):
                        discovered[config.config.server_id] = (config, path)
                except Exception as exc:
                    errors.append(f"{path}:{exc}")
                    self.logger.exception("failed to read LSP config: %s", path)
            for server_id in sorted(set(self.states) - set(discovered)):
                await self._detach_state(self.states[server_id])
                self.states.pop(server_id, None)
            for server_id, (file_config, path) in discovered.items():
                state = self.states.get(server_id)
                if state is None:
                    state = LspServerState(file_config=file_config, config_path=path)
                    self.states[server_id] = state
                elif state.file_config.to_record_config() != file_config.to_record_config():
                    await self._detach_state(state)
                    state.file_config = file_config
                    state.config_path = path
                    state.last_error = ""
            self.last_rescan_at = utc_now()
            self.last_error = "; ".join(errors)
            return {"status": "ok" if not errors else "error", "errors": errors, **self.status()}

    async def doctor(self, params: dict[str, Any]) -> dict[str, Any]:
        state = self._select_state(params)
        workspace_root = self._workspace_root(params)
        binary = shutil.which(state.file_config.config.command[0])
        checks = [
            {"name": "enabled", "status": "ok" if state.file_config.enabled else "disabled"},
            {"name": "binary", "status": "ok" if binary else "missing_binary", "command": state.file_config.config.command[0]},
            {"name": "workspace_root", "status": "ok" if workspace_root.exists() else "missing", "workspace_root": str(workspace_root)},
        ]
        marker_checks = []
        for marker in state.file_config.config.workspace_markers:
            marker_checks.append({"marker": marker, "present": (workspace_root / marker).exists()})
        checks.append({"name": "workspace_markers", "status": "ok" if any(item["present"] for item in marker_checks) or not marker_checks else "warning", "items": marker_checks})
        if not state.file_config.enabled or not binary or not workspace_root.exists():
            return {"status": "unavailable", "server": self._server_summary(state), "checks": checks}
        try:
            await self._ensure_attached(state, workspace_root)
            checks.append({"name": "initialize", "status": "ok", "server_info": state.connector.server_info if state.connector else {}})
            return {"status": "ok", "server": self._server_summary(state), "checks": checks}
        except Exception as exc:
            checks.append({"name": "initialize", "status": "error", "error": f"{exc.__class__.__name__}: {exc}"})
            return {"status": "error", "server": self._server_summary(state), "checks": checks}

    async def run_lsp_operation(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        state = self._select_state(params)
        file_path = self._file_path(params, required=operation != "workspace_symbols")
        workspace_root = self._workspace_root(params, file_path=file_path)
        unavailable = self._unavailable_reason(state, workspace_root)
        if unavailable:
            return {"status": "unavailable", "operation": operation, "reason": unavailable, "server": self._server_summary(state)}
        async with state.lock:
            await self._ensure_attached(state, workspace_root)
            assert state.connector is not None
            if operation == "workspace_symbols":
                query = str(params.get("query") or "")
                result = await state.connector.request("workspace/symbol", {"query": query})
                return self._evidence(operation, state, workspace_root, None, params, result.get("value", result))
            language_id = self._language_id(state, file_path)
            document = await state.connector.ensure_document_open(file_path, language_id=language_id)
            text_document = {"uri": document["uri"]}
            if operation == "diagnostics":
                result = await state.connector.diagnostics(file_path, language_id=language_id)
                return self._evidence(operation, state, workspace_root, file_path, params, result, file_sha256=str(document["file_sha256"]))
            if operation == "prepare_call_hierarchy":
                result = await state.connector.request(
                    "textDocument/prepareCallHierarchy",
                    {"textDocument": text_document, "position": _position(params)},
                )
                return self._evidence(operation, state, workspace_root, file_path, params, result.get("value", result), file_sha256=str(document["file_sha256"]))
            if operation in {"incoming_calls", "outgoing_calls"}:
                result = await self._call_hierarchy_calls(
                    state.connector,
                    operation=operation,
                    text_document=text_document,
                    params=params,
                )
                return self._evidence(operation, state, workspace_root, file_path, params, result, file_sha256=str(document["file_sha256"]))
            lsp_method = {
                "hover": "textDocument/hover",
                "definition": "textDocument/definition",
                "implementation": "textDocument/implementation",
                "references": "textDocument/references",
                "document_symbols": "textDocument/documentSymbol",
            }[operation]
            request_params: dict[str, Any] = {"textDocument": text_document}
            if operation != "document_symbols":
                request_params["position"] = _position(params)
            if operation == "references":
                request_params["context"] = {"includeDeclaration": bool(params.get("include_declaration", True))}
            result = await state.connector.request(lsp_method, request_params)
            return self._evidence(operation, state, workspace_root, file_path, params, result.get("value", result), file_sha256=str(document["file_sha256"]))

    async def _call_hierarchy_calls(
        self,
        connector: AsyncLspConnector,
        *,
        operation: str,
        text_document: dict[str, Any],
        params: dict[str, Any],
    ) -> dict[str, Any]:
        prepared = await connector.request(
            "textDocument/prepareCallHierarchy",
            {"textDocument": text_document, "position": _position(params)},
        )
        items = _result_list(prepared.get("value", prepared))
        method = {
            "incoming_calls": "callHierarchy/incomingCalls",
            "outgoing_calls": "callHierarchy/outgoingCalls",
        }[operation]
        calls: list[dict[str, Any]] = []
        for item in items:
            call_result = await connector.request(method, {"item": item})
            calls.append({"item": item, "calls": _result_list(call_result.get("value", call_result))})
        return {"items": items, "calls": calls}

    async def close_all(self) -> None:
        for state in list(self.states.values()):
            await self._detach_state(state)

    async def _ensure_attached(self, state: LspServerState, workspace_root: Path) -> None:
        if state.connector is not None and state.attached and state.connector.workspace_root == workspace_root:
            return
        await self._detach_state(state)
        connector = AsyncLspConnector(state.file_config.config, workspace_root=workspace_root)
        try:
            await connector.initialize()
        except Exception as exc:
            await connector.close()
            state.last_error = f"{exc.__class__.__name__}: {exc}"
            raise
        state.connector = connector
        state.attached = True
        state.last_error = ""
        state.last_attached_at = utc_now()

    async def _detach_state(self, state: LspServerState) -> None:
        connector = state.connector
        state.connector = None
        state.attached = False
        if connector is not None:
            await connector.close()

    def _select_state(self, params: dict[str, Any]) -> LspServerState:
        server_id = str(params.get("server_id") or "").strip()
        if server_id:
            state = self.states.get(server_id)
            if state is None:
                raise KeyError(f"unknown LSP server: {server_id}")
            return state
        file_text = str(params.get("file") or params.get("path") or "").strip()
        suffix = Path(file_text).suffix.lower() if file_text else ""
        for state in self.states.values():
            if suffix and suffix in state.file_config.config.extensions:
                return state
        for state in self.states.values():
            if state.file_config.enabled:
                return state
        raise KeyError("no LSP server configured")

    def _workspace_root(self, params: dict[str, Any], *, file_path: Path | None = None) -> Path:
        explicit = str(params.get("workspace_root") or params.get("repo_path") or "").strip()
        if explicit:
            return Path(explicit).expanduser().resolve()
        if file_path is not None:
            return _detect_workspace_root(file_path)
        return self.runtime_root

    def _file_path(self, params: dict[str, Any], *, required: bool) -> Path:
        raw = str(params.get("file") or params.get("path") or "").strip()
        if not raw:
            if required:
                raise ValueError("file is required")
            return Path()
        path = Path(raw).expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"LSP file not found: {path}")
        return path

    def _unavailable_reason(self, state: LspServerState, workspace_root: Path) -> str:
        if not state.file_config.enabled:
            return "disabled"
        if not shutil.which(state.file_config.config.command[0]):
            return "missing_binary"
        if not workspace_root.exists():
            return "missing_workspace_root"
        return ""

    def _language_id(self, state: LspServerState, file_path: Path) -> str:
        language_ids = state.file_config.config.language_ids
        if language_ids:
            return language_ids[0]
        suffix = file_path.suffix.lower().lstrip(".")
        return suffix or "plaintext"

    def _server_summary(self, state: LspServerState) -> dict[str, Any]:
        binary = shutil.which(state.file_config.config.command[0])
        return {
            "server_id": state.server_id,
            "display_name": state.file_config.config.display_name,
            "source": state.file_config.source,
            "config_path": str(state.config_path),
            "enabled": state.file_config.enabled,
            "attached": state.attached,
            "binary_status": "ok" if binary else "missing_binary",
            "command": list(state.file_config.config.command),
            "args": list(state.file_config.config.args),
            "extensions": list(state.file_config.config.extensions),
            "language_ids": list(state.file_config.config.language_ids),
            "last_error": state.last_error,
            "last_attached_at": state.last_attached_at,
            "install_hint": state.file_config.config.install_hint,
        }

    def _evidence(
        self,
        operation: str,
        state: LspServerState,
        workspace_root: Path,
        file_path: Path | None,
        params: dict[str, Any],
        result: Any,
        *,
        file_sha256: str = "",
    ) -> dict[str, Any]:
        method = {
            "hover": "textDocument/hover",
            "definition": "textDocument/definition",
            "implementation": "textDocument/implementation",
            "references": "textDocument/references",
            "document_symbols": "textDocument/documentSymbol",
            "workspace_symbols": "workspace/symbol",
            "diagnostics": "textDocument/publishDiagnostics",
            "prepare_call_hierarchy": "textDocument/prepareCallHierarchy",
            "incoming_calls": "callHierarchy/incomingCalls",
            "outgoing_calls": "callHierarchy/outgoingCalls",
        }.get(operation, operation)
        evidence = {
            "evidence_id": f"lsp_{utc_now().replace(':', '').replace('-', '').replace('.', '')}",
            "server_id": state.server_id,
            "method": method,
            "workspace_root": str(workspace_root),
            "file": str(file_path) if file_path else "",
            "file_sha256": file_sha256,
            "position": _position(params)
            if operation in {"hover", "definition", "implementation", "references", "prepare_call_hierarchy", "incoming_calls", "outgoing_calls"}
            else {},
            "timestamp": utc_now(),
            "freshness": "fresh" if file_sha256 else "workspace",
            "result": result,
        }
        return {"status": "ok", "operation": operation, "evidence": evidence, "result": result, "server": self._server_summary(state)}


def _result_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _position(params: dict[str, Any]) -> dict[str, int]:
    return {"line": _int(params.get("line"), 0), "character": _int(params.get("character"), 0)}


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _detect_workspace_root(file_path: Path) -> Path:
    current = Path(file_path).resolve().parent
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists() or (candidate / "compile_commands.json").exists() or (candidate / "pyproject.toml").exists():
            return candidate
    return current
