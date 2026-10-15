from __future__ import annotations

from pal.shared.diagnostics import exception_report

from pal.execution.tool_semantics import (
    INDIRECT_LOCAL_WRITE,
    INDIRECT_CONTROL,
    INDIRECT_LOCAL_READ,
)
from pal.execution.tool_facade import NextToolHint, ToolGuidance

from pal.execution.generated_tool_models import (
    LspPluginLspManagerPluginProviderCallsInput,
    LspPluginLspManagerPluginProviderSymbolLocationsInput,
    LspPluginLspManagerPluginProviderDiagnosticsInput,
    LspPluginLspManagerPluginProviderDocumentSymbolsInput,
    LspPluginLspManagerPluginProviderHoverInput,
    LspPluginLspManagerPluginProviderPrepareWorkspaceInput,
    LspPluginLspManagerPluginProviderReferencesInput,
    LspPluginLspManagerPluginProviderStatusInput,
    LspPluginLspManagerPluginProviderWorkspaceSymbolsInput,
)

import contextlib
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.execution.contracts import CapabilityCall, CapabilityResult
from pal.foundation.service_logging import current_service_log_sink_description
from pal.foundation.sidecar import python_subprocess_env
from pal.lsp.ipc import LspManagerClient
from pal.lsp.skills import PAL_LSP_TEMPLATE_DEVELOPMENT_SKILL_ID, lsp_declared_skills
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
    IntrospectionCall,
    IntrospectionResult,
    RuntimeStatus,
    capability_action,
    capability_node,
)
from pal.shared.result_rendering import render_titled_structured_for_llm
from pal.shared.inspection import inspection_snapshot
from pal.shared.tool_protocol import ToolAffordance


_MANAGER_RETIRE_TIMEOUT_SECONDS = 5.0


@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="module",
    kind="module",
    source="builtin:lsp",
    target_kind="module",
    path_module_id="lsp",
)
@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="lsp",
    kind="provider",
    source="builtin:lsp",
    target_kind="lsp_provider",
    path_module_id="lsp",
)
@dataclass
class LspManagerPluginProvider:
    runtime_root: Path
    # Bunshin roles may query the resident's manager but never own its lifecycle.
    client_only: bool = field(default=False, kw_only=True)
    client: LspManagerClient = field(init=False)
    process: subprocess.Popen | None = field(default=None, init=False, repr=False)
    _lifecycle_lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )
    last_error: str = ""
    last_health: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.client = LspManagerClient(runtime_root=self.runtime_root)

    def _process_status(self) -> tuple[int, int | None] | None:
        process = self.process
        if process is None:
            return None
        returncode = process.poll()
        if returncode is not None and self.process is process:
            self.process = None
        return int(process.pid), returncode

    def declared_skills(self):
        return lsp_declared_skills(module_id="lsp")

    @capability_action(
        namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="status",
        guidance=ToolGuidance(
            search_objects=('status', 'provider', 'server', 'servers', 'workspace'),
            search_terms=('state', 'health', 'show', 'check', 'doctor', 'readiness', 'binary', 'initialization'),
            purpose="Inspect LSP host health, a workspace's recorded readiness, or one selected server's complete diagnosis.",
            use_when="Omit selectors for passive host health. Supply workspace_root for passive workspace readiness. Supply name or file to actively check one server's binary, project context and initialization, optionally within workspace_root. Cached observations are historical. An absent preparation record does not block ordinary queries.",
            do_not_use_when="Routine code navigation or source diagnostics: call the relevant tool directly; it starts or reuses its server. Source errors and warnings belong to read_lsp_diagnostics.",
            failure_next_steps="Follow the selected object's cause. Correct missing binaries or project configuration. Use prepare_lsp_workspace only when environment setup is needed; use manage_plugin operation=reload name=lsp for manager lifecycle recovery.",
        ),
        InputModel=LspPluginLspManagerPluginProviderStatusInput,
        aliases=("inspect_lsp_status",), execution=INDIRECT_LOCAL_READ,
    )
    def status(self, call: CapabilityCall) -> CapabilityResult:
        args = {key: value for key, value in dict(call.args or {}).items() if value is not None}
        if not args:
            return _capability_from_rpc("LSP host status", inspection_snapshot(
                self._status_payload(), source="resident_lsp_host"))
        active = bool(args.get("name") or args.get("file"))
        payload = self._request_or_error("doctor" if active else "status", args, start_manager=active)
        payload = inspection_snapshot(payload, source="lsp_manager")
        payload["manager"] = self._status_payload()
        if payload.get("status") in {RuntimeStatus.ERROR, "unavailable"}:
            section = "server" if active else "workspace"
            payload["diagnostics"].update(partial=True, unavailable_sections={section: payload.get("error") or payload.get("reason")})
        return _capability_from_rpc("LSP status", payload)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="lsp",
        family="lsp",
        action_name="prepare_workspace",
        guidance=ToolGuidance(
            search_objects=('workspace', 'workspaces'),
            search_terms=('configure', 'setup', 'initialize', 'project',),
            purpose="Configure a workspace's LSP project environment and optionally prewarm its language servers.",
            use_when='Project environment setup is needed, such as C/C++ compile commands or include paths; those settings changed; a query reported missing project context; or workspace prewarming is explicitly requested. name pins the server and workspace_root pins the root. Usable project compilation databases, compile_flags.txt and .clangd take precedence over generated fallback flags.',
            do_not_use_when="Routine navigation or diagnostics with usable project configuration: call the relevant LSP tool directly; it starts or reuses its server. Selecting a project alone does not require preparation. Not for non-LSP projects.",
            failure_next_steps="Follow result-specific recovery. Use inspect_lsp_status with the workspace and optional server name; correct the reported environment or server problem before retrying. Reuse known tool contracts.",
            next_tool_hints=(
                NextToolHint(name="inspect_lsp_status", use_when="Preparation was partial or failed and workspace-wide readiness must be inspected."),
            ),
        ),
        InputModel=LspPluginLspManagerPluginProviderPrepareWorkspaceInput,
        aliases=("prepare_lsp_workspace",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def prepare_workspace(self, call: CapabilityCall) -> CapabilityResult:
        payload = self._request_or_error("prepare_workspace", dict(call.args or {}))
        return _prepare_workspace_result(payload)

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="diagnostics",
        guidance=ToolGuidance(
            search_objects=('diagnostic', 'diagnostics', 'error', 'errors', 'warning', 'warnings'),
            search_terms=('check', 'problems',),
            purpose="Read diagnostics (errors/warnings) for a file.",
            use_when='Checking compile errors or type issues after editing a file. A successful empty result means no diagnostics were reported. Diagnose readiness only when the operation reports an error.',
            do_not_use_when="Reading file content (use read_file). Searching code (use run_shell rg). No LSP server available.",
            failure_next_steps="A successful empty result means no diagnostics were reported. Diagnose readiness only when the operation reports an error.",
        ), InputModel=LspPluginLspManagerPluginProviderDiagnosticsInput, aliases=("read_lsp_diagnostics",), execution=INDIRECT_LOCAL_READ)
    def diagnostics(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP diagnostics", self._request_or_error("diagnostics", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="hover",
        guidance=ToolGuidance(search_intents=('read symbol type signature and documentation', 'inspect hover documentation at source position'),
            search_objects=('hover', 'information', 'type', 'types'),
            search_terms=('docs', 'documentation', 'signature',),
            purpose="Read hover information (type, docs) at a file position.",
            use_when="Checking a symbol's type signature or documentation at a specific location. A successful empty result means no hover information was returned at this position. Diagnose readiness only on a reported error.",
            do_not_use_when="Finding definitions (use find_lsp_symbol_locations). Reading file content (use read_file).",
            failure_next_steps="A successful empty result means no hover information was returned at this position. Diagnose readiness only on a reported error.",
        ), InputModel=LspPluginLspManagerPluginProviderHoverInput, aliases=("read_lsp_hover",), execution=INDIRECT_LOCAL_READ)
    def hover(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP hover", self._request_or_error("hover", dict(call.args or {})))

    @capability_action(
        namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="symbol_locations",
        guidance=ToolGuidance(
            search_objects=('definition', 'definitions', 'implementation', 'implementations', 'symbol'),
            search_terms=('goto', 'jump', 'concrete', 'override', 'overrides'),
            search_enum_fields=('kind',),
            purpose="Find a symbol's definition or concrete implementations at a file position.",
            use_when="Choose kind=definition to locate where a symbol is defined; kind=implementation to locate concrete implementations of an interface or abstract method. Both use the same file position.",
            do_not_use_when="Finding usage locations: use find_lsp_references. Locating a symbol by name: use search_lsp_workspace_symbols.",
            failure_next_steps="A successful empty result means no locations were returned. Check the position or use text search; inspect LSP readiness only on a reported error.",
        ), InputModel=LspPluginLspManagerPluginProviderSymbolLocationsInput,
        aliases=("find_lsp_symbol_locations",), execution=INDIRECT_LOCAL_READ,
    )
    def symbol_locations(self, call: CapabilityCall) -> CapabilityResult:
        args = dict(call.args)
        operation = {"definition": "definition", "implementation": "implementation"}[args.pop("kind")]
        return _capability_from_rpc("LSP symbol locations", self._request_or_error(operation, args))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="references",
        guidance=ToolGuidance(search_intents=('find symbol reference locations', 'list source usages of a symbol'),
            search_objects=('reference', 'references'),
            search_terms=('usage', 'usages',),
            purpose="Find references at a file position.",
            use_when='Finding all places that reference a symbol. A successful empty result means no references were returned. Diagnose readiness only on a reported error.',
            do_not_use_when="Finding definitions (use find_lsp_symbol_locations). Call hierarchy (use find_lsp_calls).",
            failure_next_steps="A successful empty result means no references were returned. Diagnose readiness only on a reported error.",
        ), InputModel=LspPluginLspManagerPluginProviderReferencesInput, aliases=("find_lsp_references",), execution=INDIRECT_LOCAL_READ)
    def references(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP references", self._request_or_error("references", dict(call.args or {})))

    @capability_action(
        namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="calls",
        guidance=ToolGuidance(
            search_objects=('call', 'calls', 'caller', 'callers', 'callee', 'callees'),
            search_terms=('incoming', 'outgoing', 'calling', 'called', 'invokes', 'invoked', 'callgraph', 'hierarchy'),
            search_enum_fields=('direction',),
            purpose="Find callers or callees of a symbol at a file position, preparing its call hierarchy internally.",
            use_when="Choose direction=callers for functions calling this symbol; direction=callees for functions this symbol calls. No separate preparation is required. All candidate hierarchy items are handled.",
            do_not_use_when="Finding all usage locations: use find_lsp_references.",
            failure_next_steps="A successful empty result means no call relationships were returned. Inspect LSP readiness only on a reported error.",
        ), InputModel=LspPluginLspManagerPluginProviderCallsInput,
        aliases=("find_lsp_calls",), execution=INDIRECT_LOCAL_READ,
    )
    def calls(self, call: CapabilityCall) -> CapabilityResult:
        args = dict(call.args)
        operation = {"callers": "incoming_calls", "callees": "outgoing_calls"}[args.pop("direction")]
        return _capability_from_rpc("LSP calls", self._request_or_error(operation, args))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="document_symbols",
        guidance=ToolGuidance(
            search_objects=('symbol', 'symbols', 'function', 'functions', 'class', 'classes', 'variable', 'variables'),
            search_terms=('file', 'outline',),
            purpose="List document symbols (functions, classes, variables) for a file.",
            use_when='Mapping the structure of a file before reading it in detail. A successful empty result means no document symbols were returned. Read the file if its contents are needed; diagnose readiness only on a reported error.',
            do_not_use_when="Workspace-wide symbol search (use search_lsp_workspace_symbols). Reading file content (use read_file).",
            failure_next_steps="A successful empty result means no document symbols were returned. Read the file if its contents are needed; diagnose readiness only on a reported error.",
        ), InputModel=LspPluginLspManagerPluginProviderDocumentSymbolsInput, aliases=("list_lsp_document_symbols",), execution=INDIRECT_LOCAL_READ)
    def document_symbols(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP document symbols", self._request_or_error("document_symbols", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="workspace_symbols",
        guidance=ToolGuidance(
            search_terms=('lookup', 'locate', 'symbol', 'name', 'workspace'),
            search_intents=('locate a symbol by name across the project',),
            search_objects=('symbol', 'symbols'),
            purpose="Search workspace symbols by name.",
            use_when='Finding where a symbol is defined across the entire workspace. A successful empty result means no matching symbols were returned. Refine the query or use text search if needed; diagnose readiness only on a reported error.',
            do_not_use_when="One file's symbols (use list_lsp_document_symbols). Text search (use run_shell rg).",
            failure_next_steps="A successful empty result means no matching symbols were returned. Refine the query or use text search if needed; diagnose readiness only on a reported error.",
        ), InputModel=LspPluginLspManagerPluginProviderWorkspaceSymbolsInput, aliases=("search_lsp_workspace_symbols",), execution=INDIRECT_LOCAL_READ)
    def workspace_symbols(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP workspace symbols", self._request_or_error("workspace_symbols", dict(call.args or {})))

    def start_manager(self) -> None:
        try:
            self._ensure_manager_started()
            if not self.client_only:
                self.last_health = self.client.rescan_sync()
            self.last_error = ""
        except Exception as exc:
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            self.last_health = {"healthy": False, "startup_error": self.last_error}
            raise

    def stop_manager(self) -> None:
        self._stop_manager()

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="management", action_name="rescan",
        guidance=ToolGuidance(
            search_objects=('server', 'servers'),
            search_terms=('scan', 'refresh',),
            purpose="Rescan LSP server configs and refresh health.",
            use_when="After adding or modifying LSP server configuration.",
            do_not_use_when="Restarting the manager (use manage_plugin operation=reload with name='lsp').",
            failure_next_steps="Use inspect_lsp_status to reconcile manager readiness. Correct LSP config syntax or server availability before retrying.",
        ), aliases=("rescan_lsp_servers",), execution=INDIRECT_CONTROL)
    def rescan(self, call: IntrospectionCall | None = None) -> IntrospectionResult:
        _ = call
        try:
            if self.client_only:
                raise PermissionError("client-only LSP providers cannot rescan the resident manager")
            self._ensure_manager_started()
            payload = self.client.rescan_sync()
            self.last_health = dict(payload)
            status = payload.get("status") or RuntimeStatus.OK
            self.last_error = "; ".join(str(error) for error in payload.get("errors", ())) if status != RuntimeStatus.OK else ""
            return IntrospectionResult(status=status, text="lsp rescan" if status == RuntimeStatus.OK else "lsp rescan failed", structured=payload, llm_text=render_titled_structured_for_llm("LSP rescan", payload))
        except Exception as exc:
            payload = self._rpc_error("rescan", exc)
            return IntrospectionResult(status=RuntimeStatus.ERROR, text="lsp rescan failed", structured=payload, llm_text=render_titled_structured_for_llm("LSP rescan failed", payload))

    def _ensure_manager_started(self) -> None:
        with self._lifecycle_lock:
            self._ensure_manager_started_locked()

    def _ensure_manager_started_locked(self) -> None:
        if self.client_only:
            # The host alone may start, replace or clean up the shared endpoint.
            # A missing/unhealthy host must remain an error, never trigger repair
            # from a role whose shared LSP runtime directory is read-only.
            health = self._validate_health(self.client.health_sync())
            if bool(health.get("shutdown_requested")):
                raise RuntimeError("lsp manager is shutting down")
            self.last_health = health
            self.last_error = ""
            return
        status = self._process_status()
        if status is not None and status[1] is None:
            try:
                health = self._validate_health(self.client.health_sync())
                if self._pid_from_health(health) != status[0]:
                    raise RuntimeError("lsp manager endpoint is not owned by this plugin attachment")
                if bool(health.get("shutdown_requested")):
                    raise RuntimeError("lsp manager is shutting down")
                self.last_health = health
                return
            except Exception:
                self._stop_process_only()
        elif status is not None:
            self._stop_process_only()
        try:
            existing_health = self.client.health_sync()
        except Exception:
            existing_health = None
        if existing_health is not None:
            self._retire_existing_manager(existing_health)
        self._cleanup_stale_endpoint()
        process = subprocess.Popen(
            [sys.executable, "-m", "pal.lsp.manager_main", "--runtime-root", str(self.runtime_root)],
            env=python_subprocess_env(),
            start_new_session=os.name != "nt",
        )
        self.process = process
        for _ in range(100):
            current = self._process_status()
            if current is None or current[1] is not None:
                self._stop_process_only()
                raise RuntimeError("lsp manager exited during startup")
            try:
                health = self._validate_health(self.client.health_sync())
                if self._pid_from_health(health) != current[0]:
                    raise RuntimeError("lsp manager endpoint is not owned by this plugin attachment")
                if bool(health.get("shutdown_requested")):
                    raise RuntimeError("lsp manager is shutting down")
                self.last_health = health
                self.last_error = ""
                return
            except Exception:
                time.sleep(0.1)
        self._stop_process_only()
        raise RuntimeError("lsp manager failed to start")

    def _stop_manager(self) -> None:
        with self._lifecycle_lock:
            self._stop_manager_locked()

    def _stop_manager_locked(self) -> None:
        if self.client_only:
            self.last_health = {}
            return
        try:
            health = self.client.health_sync()
        except Exception:
            health = None
        try:
            if health is not None:
                self._retire_existing_manager(health)
        finally:
            self._stop_process_only()
        self._cleanup_stale_endpoint()
        self.last_health = {}

    @staticmethod
    def _validate_health(health: dict[str, Any]) -> dict[str, Any]:
        if not bool(health.get("ok")) or str(health.get("health_source") or "") != "lsp_manager":
            raise RuntimeError("lsp manager health check failed")
        if str(health.get("lifecycle_protocol") or "") != "plugin_raii.v1":
            raise RuntimeError("lsp manager lifecycle protocol is incompatible")
        return dict(health)

    @staticmethod
    def _pid_from_health(health: dict[str, Any]) -> int | None:
        try:
            pid = int(health.get("manager_pid") or 0)
        except (TypeError, ValueError):
            return None
        return pid if pid > 1 and pid != os.getpid() else None

    def _retire_existing_manager(self, health: dict[str, Any]) -> None:
        self._validate_health(health)
        with contextlib.suppress(Exception):
            self.client.shutdown_sync()
        deadline = time.monotonic() + _MANAGER_RETIRE_TIMEOUT_SECONDS
        while self._manager_is_responding() and time.monotonic() < deadline:
            time.sleep(0.05)
        if self._manager_is_responding():
            raise RuntimeError("existing lsp manager did not stop")
        self._cleanup_stale_endpoint()

    def _manager_is_responding(self) -> bool:
        try:
            health = self.client.health_sync()
        except Exception:
            return False
        return bool(health.get("ok")) and str(health.get("health_source") or "") == "lsp_manager"

    def _cleanup_stale_endpoint(self) -> None:
        for path in (self.client.socket_path, self.client.port_path):
            with contextlib.suppress(FileNotFoundError):
                path.unlink()

    def _stop_process_only(self) -> None:
        with self._lifecycle_lock:
            process = self.process
            if process is None:
                return
            self.process = None
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError):
                    if os.name == "nt":
                        process.kill()
                    else:
                        os.killpg(process.pid, signal.SIGKILL)
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(
                    "LSP manager did not exit after its one-shot termination; replacement remains fenced"
                ) from exc

    def _request_or_error(self, method: str, params: dict[str, Any] | None = None, *, start_manager: bool = True) -> dict[str, Any]:
        try:
            if start_manager or self.client_only:
                self._ensure_manager_started()
            routed_params = dict(params or {})
            server_name = str(routed_params.pop("name", "") or "").strip()
            if server_name:
                routed_params["server_id"] = server_name
            payload = self.client.operation_sync(method, routed_params)
            for item in list(payload.get("servers") or []):
                if isinstance(item, dict) and "name" not in item:
                    item["name"] = str(item.get("server_id") or "")
            server = payload.get("server")
            if isinstance(server, dict) and "name" not in server:
                server["name"] = str(server.get("server_id") or "")
            self.last_health = dict(payload)
            self.last_error = ""
            return payload
        except Exception as exc:
            return self._rpc_error(method, exc)

    def _rpc_error(self, method: str, exc: Exception) -> dict[str, Any]:
        self.last_error = exception_report(exc)
        # Keep cached observations available through inspect_lsp_status, never as this
        # request's operation/result or as evidence of a running process.
        current = self._status_payload()
        current.pop("cached_snapshot", None)
        return {**current, "status": RuntimeStatus.ERROR, "operation": method,
                "error": self.last_error, "error_code": "lsp_rpc_failed",
                **({"protocol_details": dict(exc.payload)} if getattr(exc, "payload", None) else {})}

    def _status_payload(self) -> dict[str, Any]:
        process_status = self._process_status()
        return {
            "module_id": "lsp",
            "client_only": self.client_only,
            "manager_running": process_status is not None and process_status[1] is None,
            "manager_owned": process_status is not None and process_status[1] is None,
            "log_sink": current_service_log_sink_description(),
            "last_error": self.last_error,
            "cached_snapshot": {key: value for key, value in dict(self.last_health or {}).items()
                                if key in {"ok", "health_source", "manager_pid", "started_at", "last_rescan_at", "last_error",
                                           "server_count", "attached_count", "prepared_workspace_count", "shutdown_requested", "config_root"}},
        }

def _failing_servers(payload: dict[str, Any]) -> list[str]:
    failing: list[str] = []
    for item in list(payload.get("servers") or []):
        if isinstance(item, dict):
            name = str(item.get("name") or item.get("server_id") or "")
            status = str(item.get("status") or "")
            if name and status not in {"ok", "ready"}:
                failing.append(name)
    return failing


def _prepare_workspace_result(payload: dict[str, Any]) -> CapabilityResult:
    """Guidance follows this result's facts, not a standing menu (v2 §7.3-§7.4).

    A ready preparation reports readiness facts only. Partial or failed
    preparations offer recovery bound to the workspace and — when exactly one
    failing server is identifiable — that server. No navigation menu is
    attached to any branch.
    """
    projected = dict(payload)
    raw_status = str(projected.get("status") or RuntimeStatus.OK)
    affordances: list[ToolAffordance] = []
    recovery_hint = ""
    if raw_status != RuntimeStatus.OK:
        workspace_root = str(projected.get("workspace_root") or "").strip()
        failing = _failing_servers(projected)
        primary = str(projected.get("primary_server") or "").strip()
        target: str | None = None
        if len(failing) == 1:
            target = failing[0]
        elif primary and primary in failing:
            target = primary
        if target:
            arguments: dict[str, Any] = {"name": target}
            if workspace_root:
                arguments["workspace_root"] = workspace_root
            affordances.append(
                ToolAffordance(
                    tool="inspect_lsp_status",
                    arguments=arguments,
                    reason=(
                        f"Language server {target!r} is not ready for this workspace; "
                        "diagnose that server before navigation."
                    ),
                )
            )
        elif workspace_root:
            affordances.append(
                ToolAffordance(
                    tool="inspect_lsp_status",
                    arguments={"workspace_root": workspace_root},
                    reason="Workspace preparation did not reach readiness; inspect its server readiness.",
                )
            )
        else:
            recovery_hint = "Preparation did not reach readiness; inspect workspace readiness with inspect_lsp_status."
    result = _capability_from_rpc("LSP workspace preparation", projected)
    return CapabilityResult(
        status=result.status,
        text=result.text,
        structured=result.structured,
        llm_text=result.llm_text,
        affordances=tuple(affordances),
        recovery_hint=recovery_hint,
    )


@dataclass
class LspManagerPluginBundle:
    runtime_root: Path
    plugin_id: str = "lsp"
    version: str = "0.1.0"
    client_only: bool = field(default=False, kw_only=True)

    def register_with_core(self, context) -> ModuleHandle:
        provider = LspManagerPluginProvider(
            runtime_root=self.runtime_root, client_only=self.client_only
        )
        handle = ModuleHandle(
            module_id="lsp",
            tier=MODULE_TIER_DETACHABLE,
            detachable=True,
            mounted=False,
            introspection_provider=provider,
            ports={"lsp": provider},
            shutdown_sync=provider.stop_manager,
        )
        context.register_module(handle)
        return handle


def build_lsp_plugin(*, runtime_root: Path, client_only: bool = False) -> LspManagerPluginBundle:
    return LspManagerPluginBundle(runtime_root=runtime_root, client_only=client_only)


def _capability_from_rpc(title: str, payload: dict[str, Any]) -> CapabilityResult:
    status = payload.get("status") or RuntimeStatus.OK
    if status == "unavailable":
        status = RuntimeStatus.ERROR
    elif status == "partial":
        status = RuntimeStatus.OK
    if status != RuntimeStatus.OK and not payload.get("error_code"):
        payload = {**payload, "error_code": "lsp_unavailable" if payload.get("status") == "unavailable" else "lsp_operation_failed"}
    projection = dict(payload)
    if payload.get("status") == "ok" and "result" in payload and isinstance(payload.get("evidence"), dict):
        projection.pop("server", None)
    evidence = payload.get("evidence")
    if isinstance(evidence, dict) and "result" in payload and evidence.get("result") == payload["result"]:
        projection["evidence"] = {key: value for key, value in evidence.items() if key != "result"}
    return CapabilityResult(status=status, text=title, structured=payload,
                            llm_text=render_titled_structured_for_llm(title, projection),
                            recovery_hint=str(payload.get("next_step") or "") if status != RuntimeStatus.OK else "")
