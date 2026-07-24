from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.foundation import utc_now
from pal.foundation.sidecar import dispatch_sidecar_request, handle_sidecar_client
from pal.lsp.config import LspServerFileConfig, load_builtin_lsp_templates, load_lsp_server_file, lsp_config_root
from pal.lsp.connector import AsyncLspConnector, LspProtocolError
from pal.lsp.environment import (
    detect_workspace_languages,
    normalize_lsp_language,
    prepare_workspace_lsp_environment,
)
from pal.lsp.ipc import cleanup_manager_endpoint, lsp_runtime_dir, start_manager_server


@dataclass
class LspWorkspaceSession:
    workspace_root: Path
    connector: AsyncLspConnector
    attached_at: str
    last_used_at: float = field(default_factory=time.monotonic)
    last_used_timestamp: str = field(default_factory=utc_now)

    def touch(self) -> None:
        self.last_used_at = time.monotonic()
        self.last_used_timestamp = utc_now()


@dataclass
class LspServerState:
    file_config: LspServerFileConfig
    config_path: Path
    sessions: dict[str, LspWorkspaceSession] = field(default_factory=dict)
    connector: AsyncLspConnector | None = None
    attached: bool = False
    last_error: str = ""
    last_attached_at: str = ""
    last_attach_failed_at: float = 0.0
    last_attach_failed_workspace_root: str = ""
    attach_failures: dict[str, tuple[float, str]] = field(default_factory=dict)
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
    workspace_environments: dict[str, dict[str, Any]] = field(default_factory=dict)
    attach_failure_cooldown_seconds: float = 60.0
    idle_session_timeout_seconds: float = 30 * 60.0
    idle_eviction_interval_seconds: float = 60.0
    _shutdown_event: asyncio.Event = field(default_factory=asyncio.Event)
    _manager_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _environment_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def run(self) -> None:
        self.server, self.endpoint_info = await start_manager_server(self.runtime_root, self._handle_client)
        await self.rescan()
        async with self.server:
            serve_task = asyncio.create_task(self.server.serve_forever(), name="lsp-manager-serve")
            eviction_task = asyncio.create_task(self._evict_idle_sessions_forever(), name="lsp-manager-idle-evict")
            try:
                await self._shutdown_event.wait()
            finally:
                serve_task.cancel()
                eviction_task.cancel()
                self.server.close()
                await self.server.wait_closed()
                with contextlib.suppress(asyncio.CancelledError):
                    await serve_task
                with contextlib.suppress(asyncio.CancelledError):
                    await eviction_task
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
        if method == "prepare_workspace":
            return await self.prepare_workspace(dict(params or {}))
        if method == "release_workspace":
            return await self.release_workspace(dict(params or {}))
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
            "attached_count": sum(_attached_session_count(state) for state in self.states.values()),
            "prepared_workspace_count": len(self.workspace_environments),
            "idle_session_timeout_seconds": self.idle_session_timeout_seconds,
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
        if not self._has_server_selector(params):
            return {
                "status": "unavailable",
                "reason": "server_id_or_file_required",
                "message": "LSP doctor requires server_id or file/path so it does not inspect an arbitrary configured server.",
                "servers": [self._server_summary(state) for state in sorted(self.states.values(), key=lambda item: item.server_id)],
                **self.health(),
            }
        workspace_root = self._workspace_root(params)
        params = self._with_prepared_environment(params, workspace_root)
        state = self._select_state(params)
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
        project_context = _project_context_for(state, params)
        if project_context:
            checks.append(
                {
                    "name": "project_context",
                    "status": str(project_context.get("status") or "unavailable"),
                    "context": project_context,
                }
            )
        if not state.file_config.enabled or not binary or not workspace_root.exists():
            return {"status": "unavailable", "server": self._server_summary(state), "checks": checks}
        context_reason = _project_context_unavailable_reason(state, params, workspace_root)
        if context_reason:
            return {
                "status": "unavailable",
                "reason": context_reason,
                "server": self._server_summary(state),
                "checks": checks,
            }
        async with state.lock:
            recent_failure = self._recent_attach_failure_reason(state, workspace_root)
            if recent_failure:
                checks.append({"name": "initialize", "status": "skipped_recent_failure", "reason": recent_failure})
                return {"status": "unavailable", "reason": recent_failure, "server": self._server_summary(state), "checks": checks}
            try:
                extra_args = _project_context_session_args(state, params)
                if extra_args:
                    await self._ensure_attached(state, workspace_root, extra_args=extra_args)
                else:
                    await self._ensure_attached(state, workspace_root)
                connector = self._connector_for_workspace(state, workspace_root)
                checks.append({"name": "initialize", "status": "ok", "server_info": connector.server_info if connector else {}})
                return {"status": "ok", "server": self._server_summary(state), "checks": checks}
            except Exception as exc:
                checks.append({"name": "initialize", "status": "error", "error": state.last_error or f"{exc.__class__.__name__}: {exc}"})
                return {"status": "error", "server": self._server_summary(state), "checks": checks}

    async def prepare_workspace(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_workspace = str(params.get("workspace_root") or params.get("repo_path") or "").strip()
        if not raw_workspace:
            raise ValueError("prepare_workspace requires workspace_root")
        workspace_root = Path(raw_workspace).expanduser().resolve()
        if not workspace_root.is_dir():
            return {
                "status": "unavailable",
                "reason": "missing_workspace_root",
                "workspace_root": str(workspace_root),
            }

        declared_languages = [
            normalize_lsp_language(value)
            for value in _language_tokens(
                params.get("languages")
                or params.get("workspace_languages")
                or params.get("language")
            )
        ]
        primary_language = normalize_lsp_language(str(params.get("primary_language") or ""))
        detected_languages, scanned_files = detect_workspace_languages(workspace_root)
        languages = _dedupe_strings(
            [primary_language, *declared_languages, *detected_languages]
        )
        if not primary_language and languages:
            primary_language = languages[0]
        if not primary_language:
            return {
                "status": "unavailable",
                "reason": "workspace_language_unknown",
                "workspace_root": str(workspace_root),
                "scanned_files": scanned_files,
            }

        preparation_request = {
            "primary_language": primary_language,
            "languages": languages,
            "lsp_secondary_languages": [
                language for language in languages if language != primary_language
            ],
            **{
                key: params[key]
                for key in (
                    "compile_commands_path",
                    "include_paths",
                    "stub_include_paths",
                    "cpp_standard",
                    "lsp_compile_flags",
                )
                if params.get(key) not in (None, "", [], {})
            },
        }
        setup, unavailable = prepare_workspace_lsp_environment(
            workspace_root=workspace_root,
            primary_language=primary_language,
            context_root=lsp_runtime_dir(self.runtime_root) / "contexts",
            workspace=preparation_request,
        )
        fingerprint_payload = {
            "workspace_root": str(workspace_root),
            "setup": setup,
            "unavailable": unavailable,
        }
        fingerprint = _stable_fingerprint(fingerprint_payload)
        key = _workspace_session_key(workspace_root)
        async with self._environment_lock:
            previous = self._workspace_environment(workspace_root)
            changed = str(previous.get("fingerprint") or "") != fingerprint
            record = {
                "schema_version": "1",
                "workspace_root": str(workspace_root),
                "primary_language": primary_language,
                "languages": languages,
                "scanned_files": scanned_files,
                "setup": setup,
                "unavailable": unavailable,
                "fingerprint": fingerprint,
                "prepared_at": utc_now(),
            }
            self.workspace_environments[key] = record
            _write_workspace_environment(self.runtime_root, workspace_root, record)
            if changed:
                await self.release_workspace({"workspace_root": str(workspace_root)})
                for state in self.states.values():
                    state.attach_failures.pop(key, None)
                    if state.last_attach_failed_workspace_root == str(workspace_root):
                        state.last_attach_failed_at = 0.0
                        state.last_attach_failed_workspace_root = ""

        prewarm_results: list[dict[str, Any]] = []
        if bool(params.get("prewarm", True)):
            for server_id in list(setup.get("servers") or []):
                if server_id not in self.states:
                    prewarm_results.append(
                        {
                            "server_id": server_id,
                            "status": "unavailable",
                            "reason": "server_not_configured",
                        }
                    )
                    continue
                result = await self.doctor(
                    {
                        "server_id": server_id,
                        "workspace_root": str(workspace_root),
                    }
                )
                server_result = {
                    "server_id": server_id,
                    "status": str(result.get("status") or "unavailable"),
                    **(
                        {"reason": str(result.get("reason") or "")}
                        if str(result.get("reason") or "").strip()
                        else {}
                    ),
                }
                if server_result["status"] == "ok":
                    probe = await self._probe_workspace_server(
                        self.states[server_id],
                        workspace_root,
                    )
                    server_result["probe"] = probe
                    if probe.get("status") != "ok":
                        server_result["status"] = "unavailable"
                        server_result["reason"] = (
                            "recognition_probe_failed:"
                            + str(probe.get("reason") or "unknown")
                        )
                prewarm_results.append(server_result)

        ready_count = len(
            [result for result in prewarm_results if result.get("status") == "ok"]
        )
        if not bool(params.get("prewarm", True)):
            status = "ok" if str(setup.get("status") or "") == "ready" else "unavailable"
        elif prewarm_results and ready_count == len(prewarm_results):
            status = "ok"
        elif ready_count:
            status = "partial"
        else:
            status = "unavailable"
        return {
            "status": status,
            "workspace_root": str(workspace_root),
            "primary_language": primary_language,
            "languages": languages,
            "scanned_files": scanned_files,
            "environment_fingerprint": fingerprint,
            "environment_changed": changed,
            "prepared_at": record["prepared_at"],
            "servers": prewarm_results,
            "unavailable": unavailable,
        }

    async def _probe_workspace_server(
        self,
        state: LspServerState,
        workspace_root: Path,
    ) -> dict[str, Any]:
        probe_file = _workspace_probe_file(workspace_root, state)
        if probe_file is None:
            return {
                "status": "unavailable",
                "operation": "diagnostics",
                "reason": "no_matching_source_file",
            }
        result = await self.run_lsp_operation(
            "diagnostics",
            {
                "server_id": state.server_id,
                "workspace_root": str(workspace_root),
                "file": str(probe_file),
            },
        )
        if str(result.get("status") or "") != "ok":
            return {
                "status": "unavailable",
                "operation": "diagnostics",
                "file": str(probe_file),
                "reason": str(
                    result.get("reason")
                    or result.get("error")
                    or "operation_unavailable"
                ),
            }
        diagnostic_result = result.get("result")
        if not isinstance(diagnostic_result, dict):
            return {
                "status": "unavailable",
                "operation": "diagnostics",
                "file": str(probe_file),
                "reason": "invalid_diagnostics_result",
            }
        diagnostics_state = str(
            diagnostic_result.get("diagnostics_state")
            or diagnostic_result.get("status")
            or ""
        ).strip()
        if diagnostics_state in {"pending", "timed_out"}:
            return {
                "status": "unavailable",
                "operation": "diagnostics",
                "file": str(probe_file),
                "reason": f"diagnostics_not_ready:{diagnostics_state}",
            }
        diagnostics = diagnostic_result.get("diagnostics")
        if not isinstance(diagnostics, list):
            return {
                "status": "unavailable",
                "operation": "diagnostics",
                "file": str(probe_file),
                "reason": "invalid_diagnostics_payload",
            }
        blockers = [
            item
            for item in diagnostics
            if isinstance(item, dict)
            and _is_project_recognition_blocker(state, item)
        ]
        if blockers:
            blocker = blockers[0]
            return {
                "status": "unavailable",
                "operation": "diagnostics",
                "file": str(probe_file),
                "reason": (
                    "unresolved_project_include:"
                    + str(blocker.get("message") or blocker.get("code") or "unknown")
                ),
                "diagnostic": blocker,
            }
        return {
            "status": "ok",
            "operation": "diagnostics",
            "file": str(probe_file),
            "recognized": True,
            "diagnostic_count": len(diagnostics),
        }

    async def run_lsp_operation(self, operation: str, params: dict[str, Any]) -> dict[str, Any]:
        file_path = self._file_path(params, required=operation != "workspace_symbols")
        workspace_root = self._workspace_root(params, file_path=file_path)
        params = self._with_prepared_environment(params, workspace_root)
        state = self._select_state(params)
        unavailable = self._unavailable_reason(state, workspace_root, params)
        if unavailable:
            return self._unavailable_payload(operation, unavailable, state, workspace_root)
        async with state.lock:
            unavailable = self._unavailable_reason(state, workspace_root, params)
            if unavailable:
                return self._unavailable_payload(operation, unavailable, state, workspace_root)
            for attempt in range(2):
                try:
                    extra_args = _project_context_session_args(state, params)
                    if extra_args:
                        await self._ensure_attached(state, workspace_root, extra_args=extra_args)
                    else:
                        await self._ensure_attached(state, workspace_root)
                except Exception as exc:
                    detail = state.last_error or f"{exc.__class__.__name__}: {exc}"
                    if attempt == 0:
                        await self._discard_workspace_session_locked(state, workspace_root)
                        state.attach_failures.pop(_workspace_session_key(workspace_root), None)
                        continue
                    return self._unavailable_payload(
                        operation,
                        f"attach_failed_after_retry:{detail}",
                        state,
                        workspace_root,
                    )
                connector = self._connector_for_workspace(state, workspace_root)
                if connector is None:
                    detail = "LspProtocolError: no connector for prepared workspace"
                    await self._discard_workspace_session_locked(state, workspace_root)
                    if attempt == 0:
                        continue
                    return self._unavailable_payload(
                        operation,
                        f"request_failed_after_restart:{detail}",
                        state,
                        workspace_root,
                    )
                try:
                    return await self._run_lsp_operation_with_connector(
                        operation,
                        params,
                        state=state,
                        connector=connector,
                        workspace_root=workspace_root,
                        file_path=file_path,
                    )
                except (LspProtocolError, asyncio.TimeoutError, BrokenPipeError, ConnectionError) as exc:
                    detail = f"{exc.__class__.__name__}: {exc}"
                    state.last_error = detail
                    await self._discard_workspace_session_locked(state, workspace_root)
                    if attempt == 0:
                        continue
                    return self._unavailable_payload(
                        operation,
                        f"request_failed_after_restart:{detail}",
                        state,
                        workspace_root,
                    )
        return self._unavailable_payload(
            operation,
            "lsp_operation_exhausted",
            state,
            workspace_root,
        )

    async def _run_lsp_operation_with_connector(
        self,
        operation: str,
        params: dict[str, Any],
        *,
        state: LspServerState,
        connector: AsyncLspConnector,
        workspace_root: Path,
        file_path: Path,
    ) -> dict[str, Any]:
        if operation == "workspace_symbols":
            query = str(params.get("query") or "")
            result = await connector.request("workspace/symbol", {"query": query})
            return self._evidence(operation, state, workspace_root, None, params, result.get("value", result))
        language_id = self._language_id(state, file_path, params)
        document = await connector.ensure_document_open(file_path, language_id=language_id)
        text_document = {"uri": document["uri"]}
        if operation == "diagnostics":
            result = await connector.diagnostics(file_path, language_id=language_id)
            return self._evidence(operation, state, workspace_root, file_path, params, result, file_sha256=str(document["file_sha256"]))
        if operation == "prepare_call_hierarchy":
            result = await connector.request(
                "textDocument/prepareCallHierarchy",
                {"textDocument": text_document, "position": _position(params)},
            )
            return self._evidence(operation, state, workspace_root, file_path, params, result.get("value", result), file_sha256=str(document["file_sha256"]))
        if operation in {"incoming_calls", "outgoing_calls"}:
            result = await self._call_hierarchy_calls(
                connector,
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
        result = await connector.request(lsp_method, request_params)
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

    async def release_workspace(self, params: dict[str, Any]) -> dict[str, Any]:
        raw_workspace = str(params.get("workspace_root") or "").strip()
        if not raw_workspace:
            raise ValueError("release_workspace requires workspace_root")
        workspace_root = Path(raw_workspace).expanduser().resolve()
        key = _workspace_session_key(workspace_root)
        released: list[dict[str, Any]] = []
        for state in list(self.states.values()):
            async with state.lock:
                session = state.sessions.pop(key, None)
                if session is None:
                    continue
                if state.connector is session.connector:
                    state.connector = None
                if not any(
                    candidate.connector is session.connector
                    for candidate in state.sessions.values()
                ):
                    await session.connector.close()
                released.append(
                    {
                        "server_id": state.server_id,
                        "workspace_root": str(session.workspace_root),
                    }
                )
                _refresh_state_attachment(state)
        return {
            "status": "ok",
            "workspace_root": str(workspace_root),
            "released_count": len(released),
            "released": released,
        }

    async def evict_idle_sessions(
        self,
        *,
        now: float | None = None,
        idle_seconds: float | None = None,
    ) -> dict[str, Any]:
        current = time.monotonic() if now is None else float(now)
        timeout = self.idle_session_timeout_seconds if idle_seconds is None else float(idle_seconds)
        evicted: list[dict[str, Any]] = []
        for state in list(self.states.values()):
            async with state.lock:
                for key, session in list(state.sessions.items()):
                    if current - session.last_used_at < timeout:
                        continue
                    state.sessions.pop(key, None)
                    if state.connector is session.connector:
                        state.connector = None
                    await session.connector.close()
                    evicted.append(
                        {
                            "server_id": state.server_id,
                            "workspace_root": str(session.workspace_root),
                            "idle_seconds": max(0.0, current - session.last_used_at),
                        }
                    )
                _refresh_state_attachment(state)
        return {"status": "ok", "evicted_count": len(evicted), "evicted": evicted}

    async def _evict_idle_sessions_forever(self) -> None:
        while not self._shutdown_event.is_set():
            try:
                await asyncio.wait_for(
                    self._shutdown_event.wait(),
                    timeout=max(1.0, self.idle_eviction_interval_seconds),
                )
                return
            except asyncio.TimeoutError:
                pass
            try:
                await self.evict_idle_sessions()
            except Exception:
                self.logger.exception("failed to evict idle LSP sessions")

    async def _ensure_attached(
        self,
        state: LspServerState,
        workspace_root: Path,
        *,
        extra_args: tuple[str, ...] = (),
    ) -> None:
        key = _workspace_session_key(workspace_root)
        session = state.sessions.get(key)
        if (
            session is not None
            and bool(getattr(session.connector, "healthy", True))
            and session.connector.workspace_root == workspace_root
            and tuple(getattr(session.connector, "extra_args", ())) == tuple(extra_args)
        ):
            session.touch()
            state.connector = session.connector
            state.attached = True
            state.last_attached_at = session.attached_at
            return
        if session is not None:
            await self._discard_workspace_session_locked(state, workspace_root)
        if (
            state.connector is not None
            and state.attached
            and bool(getattr(state.connector, "healthy", True))
            and state.connector.workspace_root == workspace_root
            and tuple(getattr(state.connector, "extra_args", ())) == tuple(extra_args)
        ):
            state.sessions[key] = LspWorkspaceSession(
                workspace_root=workspace_root,
                connector=state.connector,
                attached_at=state.last_attached_at or utc_now(),
            )
            state.sessions[key].touch()
            return
        connector = (
            AsyncLspConnector(
                state.file_config.config,
                workspace_root=workspace_root,
                extra_args=tuple(extra_args),
            )
            if extra_args
            else AsyncLspConnector(
                state.file_config.config,
                workspace_root=workspace_root,
            )
        )
        try:
            await connector.initialize()
        except Exception as exc:
            await connector.close()
            state.last_error = _attach_error_detail(exc, connector)
            state.last_attach_failed_at = time.monotonic()
            state.last_attach_failed_workspace_root = str(workspace_root)
            state.attach_failures[key] = (state.last_attach_failed_at, state.last_error)
            raise
        attached_at = utc_now()
        state.sessions[key] = LspWorkspaceSession(
            workspace_root=workspace_root,
            connector=connector,
            attached_at=attached_at,
        )
        state.connector = connector
        state.attached = True
        state.last_error = ""
        state.last_attached_at = attached_at
        state.last_attach_failed_at = 0.0
        state.last_attach_failed_workspace_root = ""
        state.attach_failures.pop(key, None)

    async def _discard_workspace_session_locked(
        self,
        state: LspServerState,
        workspace_root: Path,
    ) -> None:
        key = _workspace_session_key(workspace_root)
        session = state.sessions.pop(key, None)
        connector = session.connector if session is not None else None
        if connector is None and state.connector is not None and state.connector.workspace_root == workspace_root:
            connector = state.connector
        if connector is not None and state.connector is connector:
            state.connector = None
        if connector is not None and not any(
            candidate.connector is connector for candidate in state.sessions.values()
        ):
            await connector.close()
        _refresh_state_attachment(state)

    async def _detach_state(self, state: LspServerState) -> None:
        connectors: list[AsyncLspConnector] = []
        for session in list(state.sessions.values()):
            if all(session.connector is not existing for existing in connectors):
                connectors.append(session.connector)
        if state.connector is not None and all(state.connector is not existing for existing in connectors):
            connectors.append(state.connector)
        state.sessions.clear()
        state.connector = None
        state.attached = False
        for connector in connectors:
            await connector.close()

    def _connector_for_workspace(self, state: LspServerState, workspace_root: Path) -> AsyncLspConnector | None:
        session = state.sessions.get(_workspace_session_key(workspace_root))
        if session is not None:
            session.touch()
            return session.connector
        connector = state.connector
        if connector is not None and state.attached and connector.workspace_root == workspace_root:
            return connector
        return None

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
        for language in _workspace_languages(params):
            for state in self.states.values():
                if _state_supports_language(state, language):
                    return state
        for state in self.states.values():
            if state.file_config.enabled:
                return state
        raise KeyError("no LSP server configured")

    def _has_server_selector(self, params: dict[str, Any]) -> bool:
        return bool(str(params.get("server_id") or "").strip()) or bool(str(params.get("file") or params.get("path") or "").strip())

    def _workspace_root(self, params: dict[str, Any], *, file_path: Path | None = None) -> Path:
        explicit = str(params.get("workspace_root") or params.get("repo_path") or "").strip()
        if explicit:
            return Path(explicit).expanduser().resolve()
        if file_path is not None:
            return _detect_workspace_root(file_path)
        return self.runtime_root

    def _workspace_environment(self, workspace_root: Path) -> dict[str, Any]:
        key = _workspace_session_key(workspace_root)
        cached = self.workspace_environments.get(key)
        if cached is not None:
            return dict(cached)
        loaded = _read_workspace_environment(self.runtime_root, workspace_root)
        if loaded:
            self.workspace_environments[key] = loaded
        return dict(loaded)

    def _with_prepared_environment(
        self,
        params: dict[str, Any],
        workspace_root: Path,
    ) -> dict[str, Any]:
        effective = dict(params)
        prepared = self._workspace_environment(workspace_root)
        setup = prepared.get("setup")
        if not isinstance(setup, dict):
            return effective
        effective["_prepared_lsp_environment"] = dict(setup)
        languages = list(prepared.get("languages") or [])
        if languages:
            effective["workspace_languages"] = languages
        primary_language = str(prepared.get("primary_language") or "").strip()
        if primary_language:
            effective["primary_language"] = primary_language
        return effective

    def _file_path(self, params: dict[str, Any], *, required: bool) -> Path:
        raw = str(params.get("file") or params.get("path") or "").strip()
        if not raw:
            if required:
                raise ValueError("file is required")
            return Path()
        path = Path(raw).expanduser()
        if not path.is_absolute():
            base = str(params.get("workspace_root") or params.get("repo_path") or "").strip()
            if base:
                path = Path(base).expanduser() / path
        path = path.resolve()
        if not path.is_file():
            raise FileNotFoundError(f"LSP file not found: {path}")
        return path

    def _unavailable_reason(
        self,
        state: LspServerState,
        workspace_root: Path,
        params: dict[str, Any] | None = None,
    ) -> str:
        if not state.file_config.enabled:
            return "disabled"
        if not shutil.which(state.file_config.config.command[0]):
            return "missing_binary"
        if not workspace_root.exists():
            return "missing_workspace_root"
        context_reason = _project_context_unavailable_reason(
            state,
            dict(params or {}),
            workspace_root,
        )
        if context_reason:
            return context_reason
        recent_failure = self._recent_attach_failure_reason(state, workspace_root)
        if recent_failure:
            return recent_failure
        return ""

    def _recent_attach_failure_reason(self, state: LspServerState, workspace_root: Path) -> str:
        key = _workspace_session_key(workspace_root)
        session_failure = state.attach_failures.get(key)
        if session_failure is not None and key not in state.sessions:
            failed_at, detail = session_failure
            elapsed = max(0.0, time.monotonic() - failed_at)
            remaining = self.attach_failure_cooldown_seconds - elapsed
            if remaining > 0:
                return f"recent_attach_failure:{detail or 'unknown attach failure'}; retry_after_seconds={remaining:.1f}"
            state.attach_failures.pop(key, None)
        if not state.last_attach_failed_at or state.attached:
            return ""
        if state.last_attach_failed_workspace_root != str(workspace_root):
            return ""
        elapsed = max(0.0, time.monotonic() - state.last_attach_failed_at)
        remaining = self.attach_failure_cooldown_seconds - elapsed
        if remaining <= 0:
            return ""
        detail = state.last_error or "unknown attach failure"
        return f"recent_attach_failure:{detail}; retry_after_seconds={remaining:.1f}"

    def _unavailable_payload(
        self,
        operation: str,
        reason: str,
        state: LspServerState,
        workspace_root: Path,
    ) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "operation": operation,
            "reason": reason,
            "workspace_root": str(workspace_root),
            "server": self._server_summary(state),
        }

    def _language_id(self, state: LspServerState, file_path: Path, params: dict[str, Any] | None = None) -> str:
        language = _language_id_from_extension(file_path.suffix.lower(), _workspace_languages(params or {}))
        if language:
            return language
        for language in _workspace_languages(params or {}):
            if _state_supports_language(state, language):
                return _lsp_language_id(language)
        language_ids = state.file_config.config.language_ids
        if language_ids:
            return language_ids[0]
        suffix = file_path.suffix.lower().lstrip(".")
        return suffix or "plaintext"

    def _server_summary(self, state: LspServerState) -> dict[str, Any]:
        binary = shutil.which(state.file_config.config.command[0])
        attached_workspaces = sorted(
            str(session.workspace_root)
            for session in state.sessions.values()
        )
        workspace_sessions = [
            {
                "workspace_root": str(session.workspace_root),
                "attached_at": session.attached_at,
                "last_used_at": session.last_used_timestamp,
                "extra_args": list(getattr(session.connector, "extra_args", ())),
            }
            for session in sorted(state.sessions.values(), key=lambda item: str(item.workspace_root))
        ]
        if not attached_workspaces and state.attached and state.connector is not None:
            attached_workspaces = [str(state.connector.workspace_root)]
        return {
            "server_id": state.server_id,
            "display_name": state.file_config.config.display_name,
            "source": state.file_config.source,
            "config_path": str(state.config_path),
            "enabled": state.file_config.enabled,
            "attached": bool(attached_workspaces),
            "attached_count": len(attached_workspaces),
            "attached_workspaces": attached_workspaces,
            "workspace_sessions": workspace_sessions,
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
            "environment_fingerprint": str(
                self._workspace_environment(workspace_root).get("fingerprint") or ""
            ),
            "result": result,
        }
        return {"status": "ok", "operation": operation, "evidence": evidence, "result": result, "server": self._server_summary(state)}


def _workspace_environment_path(runtime_root: Path, workspace_root: Path) -> Path:
    key = hashlib.sha256(
        str(Path(workspace_root).expanduser().resolve()).encode("utf-8")
    ).hexdigest()
    return lsp_runtime_dir(runtime_root) / "workspaces" / f"{key}.json"


def _workspace_probe_file(
    workspace_root: Path,
    state: LspServerState,
    *,
    scan_limit: int = 5000,
) -> Path | None:
    extensions = {
        str(extension).lower()
        for extension in state.file_config.config.extensions
        if str(extension).strip()
    }
    if not extensions:
        return None
    scanned = 0
    best_candidate: Path | None = None
    best_priority = 10
    ignored = {".git", ".hg", ".svn", ".venv", "node_modules", "build", "dist", "__pycache__"}
    for directory, directory_names, file_names in os.walk(workspace_root):
        directory_names[:] = sorted(
            name for name in directory_names if name not in ignored
        )
        for file_name in sorted(file_names):
            scanned += 1
            candidate = Path(directory) / file_name
            if candidate.suffix.lower() in extensions:
                priority = _workspace_probe_priority(state, candidate)
                if priority < best_priority:
                    best_candidate = candidate.resolve()
                    best_priority = priority
                if priority == 0:
                    return best_candidate
            if scanned >= scan_limit:
                return best_candidate
    return best_candidate


def _workspace_probe_priority(
    state: LspServerState,
    candidate: Path,
) -> int:
    if state.server_id != "clangd":
        return 0
    return (
        0
        if candidate.suffix.lower()
        in {".c", ".cc", ".cpp", ".cxx", ".m", ".mm"}
        else 1
    )


def _is_project_recognition_blocker(
    state: LspServerState,
    diagnostic: dict[str, Any],
) -> bool:
    if state.server_id != "clangd":
        return False
    code = str(diagnostic.get("code") or "").strip().lower()
    message = str(diagnostic.get("message") or "").strip().lower()
    return code == "pp_file_not_found" or (
        "file not found" in message
        and ("#include" in message or "included file" in message)
    )


def _write_workspace_environment(
    runtime_root: Path,
    workspace_root: Path,
    record: dict[str, Any],
) -> None:
    path = _workspace_environment_path(runtime_root, workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_workspace_environment(
    runtime_root: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    path = _workspace_environment_path(runtime_root, workspace_root)
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(value, dict):
        return {}
    expected_root = str(Path(workspace_root).expanduser().resolve())
    if str(value.get("workspace_root") or "") != expected_root:
        return {}
    return dict(value)


def _stable_fingerprint(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return result


def _workspace_session_key(workspace_root: Path) -> str:
    return str(Path(workspace_root).expanduser().resolve())


def _attached_session_count(state: LspServerState) -> int:
    if state.sessions:
        return len(state.sessions)
    if state.attached and state.connector is not None:
        return 1
    return 0


def _refresh_state_attachment(state: LspServerState) -> None:
    if state.sessions:
        latest = max(state.sessions.values(), key=lambda session: session.last_used_at)
        state.connector = latest.connector
        state.attached = True
        state.last_attached_at = latest.attached_at
        return
    state.connector = None
    state.attached = False


def _result_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    return [value]


def _position(params: dict[str, Any]) -> dict[str, int]:
    return {"line": _int(params.get("line"), 0), "character": _int(params.get("character"), 0)}


_PROJECT_CONTEXT_SESSION_ARG_PREFIXES = {
    "clangd": ("--compile-commands-dir=",),
}


def _project_context_for(state: LspServerState, params: dict[str, Any]) -> dict[str, Any]:
    setup = params.get("_prepared_lsp_environment")
    if not isinstance(setup, dict):
        return {}
    contexts = setup.get("project_contexts")
    if isinstance(contexts, dict) and isinstance(contexts.get(state.server_id), dict):
        return dict(contexts[state.server_id])
    environments = setup.get("environments")
    if isinstance(environments, dict) and isinstance(environments.get(state.server_id), dict):
        context = dict(environments[state.server_id]).get("project_context")
        if isinstance(context, dict):
            return dict(context)
    return {}


def _project_context_unavailable_reason(
    state: LspServerState,
    params: dict[str, Any],
    workspace_root: Path,
) -> str:
    setup = params.get("_prepared_lsp_environment")
    if not isinstance(setup, dict) or not bool(setup.get("require_project_context")):
        return ""
    context = _project_context_for(state, params)
    if str(context.get("status") or "") != "ready":
        return f"project_context_unavailable:{context.get('reason') or 'missing'}"
    bound_root = str(context.get("workspace_root") or "").strip()
    if bound_root and Path(bound_root).expanduser().resolve() != workspace_root.resolve():
        return "project_context_workspace_mismatch"
    source_path = str(context.get("source_path") or "").strip()
    if source_path and not Path(source_path).expanduser().is_file():
        return "project_context_source_missing"
    try:
        _project_context_session_args(state, params)
    except ValueError as exc:
        return f"invalid_project_context:{exc}"
    return ""


def _project_context_session_args(
    state: LspServerState,
    params: dict[str, Any],
) -> tuple[str, ...]:
    context = _project_context_for(state, params)
    raw_args = context.get("session_args")
    if raw_args is None:
        return ()
    if not isinstance(raw_args, list):
        raise ValueError("session_args must be an array")
    allowed_prefixes = _PROJECT_CONTEXT_SESSION_ARG_PREFIXES.get(state.server_id, ())
    result: list[str] = []
    for raw in raw_args:
        value = str(raw or "").strip()
        if not value or "\0" in value or "\n" in value or "\r" in value:
            raise ValueError("session_args contains an invalid value")
        if not any(value.startswith(prefix) for prefix in allowed_prefixes):
            raise ValueError(f"session argument is not allowed for {state.server_id}: {value}")
        if value not in result:
            result.append(value)
    return tuple(result)


_LANGUAGE_ALIASES = {
    "bash": "shellscript",
    "c": "c",
    "c++": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "cpp": "cpp",
    "css": "css",
    "go": "go",
    "golang": "go",
    "html": "html",
    "javascript": "javascript",
    "js": "javascript",
    "json": "json",
    "objective-c": "objective-c",
    "objective-c++": "objective-cpp",
    "objective-cpp": "objective-cpp",
    "objc": "objective-c",
    "objcpp": "objective-cpp",
    "py": "python",
    "python": "python",
    "rs": "rust",
    "rust": "rust",
    "sh": "shellscript",
    "shell": "shellscript",
    "shellscript": "shellscript",
    "ts": "typescript",
    "typescript": "typescript",
    "yaml": "yaml",
    "yml": "yaml",
}

_EXTENSION_LANGUAGE_IDS = {
    ".c": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".css": "css",
    ".go": "go",
    ".html": "html",
    ".htm": "html",
    ".hxx": "cpp",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".js": "javascript",
    ".jsx": "javascript",
    ".json": "json",
    ".jsonc": "json",
    ".m": "objective-c",
    ".mm": "objective-cpp",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".sh": "shellscript",
    ".bash": "shellscript",
    ".zsh": "shellscript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".yaml": "yaml",
    ".yml": "yaml",
}

_AMBIGUOUS_EXTENSION_LANGUAGE_IDS = {
    ".h": {"c", "cpp", "objective-c", "objective-cpp"},
}


def _workspace_languages(params: dict[str, Any]) -> list[str]:
    raw_values = [
        params.get("workspace_languages"),
        params.get("languages"),
        params.get("language"),
        params.get("primary_language"),
    ]
    prepared_environment = params.get("_prepared_lsp_environment")
    if isinstance(prepared_environment, dict):
        raw_values.append(prepared_environment.get("languages"))
    result: list[str] = []
    for raw in raw_values:
        for item in _language_tokens(raw):
            language = _normalize_language_id(item)
            if language and language not in result:
                result.append(language)
    return result


def _language_tokens(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, dict):
        collected: list[str] = []
        for key in ("language", "name", "id", "value"):
            collected.extend(_language_tokens(value.get(key)))
        return collected
    if isinstance(value, (list, tuple, set)):
        collected = []
        for item in value:
            collected.extend(_language_tokens(item))
        return collected
    text = str(value or "").strip().lower()
    if not text:
        return []
    for separator in ("\n", "\t", ",", ";", "/", "|"):
        text = text.replace(separator, ",")
    text = text.replace(" and ", ",").replace(" & ", ",")
    return [part.strip().replace("_", "-") for part in text.split(",") if part.strip()]


def _normalize_language_id(value: str) -> str:
    text = "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum() or ch in {"-", "+", "#", "."}).strip()
    return _LANGUAGE_ALIASES.get(text, text)


def _lsp_language_id(language: str) -> str:
    return _normalize_language_id(language)


def _language_id_from_extension(suffix: str, workspace_languages: list[str]) -> str:
    normalized_suffix = str(suffix or "").lower()
    if normalized_suffix in _AMBIGUOUS_EXTENSION_LANGUAGE_IDS:
        allowed = _AMBIGUOUS_EXTENSION_LANGUAGE_IDS[normalized_suffix]
        for language in workspace_languages:
            if language in allowed:
                return _lsp_language_id(language)
    return _EXTENSION_LANGUAGE_IDS.get(normalized_suffix, "")


def _state_supports_language(state: LspServerState, language: str) -> bool:
    normalized = _normalize_language_id(language)
    if not normalized:
        return False
    advertised = {_normalize_language_id(item) for item in state.file_config.config.language_ids}
    if normalized in advertised:
        return True
    for extension, extension_language in _EXTENSION_LANGUAGE_IDS.items():
        if extension_language == normalized and extension in state.file_config.config.extensions:
            return True
    if normalized in _AMBIGUOUS_EXTENSION_LANGUAGE_IDS.get(".h", set()) and ".h" in state.file_config.config.extensions:
        return True
    return False


def _attach_error_detail(exc: Exception, connector: AsyncLspConnector) -> str:
    detail = f"{exc.__class__.__name__}: {exc}"
    stderr_tail_getter = getattr(connector, "stderr_tail_text", None)
    stderr_tail = stderr_tail_getter() if callable(stderr_tail_getter) else ""
    if stderr_tail:
        if len(stderr_tail) > 1200:
            stderr_tail = "..." + stderr_tail[-1200:]
        detail = f"{detail}; stderr_tail={stderr_tail}"
    return detail


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
