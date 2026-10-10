from __future__ import annotations

from pal.shared.diagnostics import exception_report

from pal.execution.tool_semantics import (
    DIRECT_LOCAL_WRITE,
    INDIRECT_CONTROL,
    INDIRECT_LOCAL_READ,
)
from pal.execution.tool_facade import NextToolHint, ToolGuidance

from pal.execution.generated_tool_models import (
    LspPluginLspManagerPluginProviderDefinitionInput,
    LspPluginLspManagerPluginProviderDiagnosticsInput,
    LspPluginLspManagerPluginProviderDoctorInput,
    LspPluginLspManagerPluginProviderDocumentSymbolsInput,
    LspPluginLspManagerPluginProviderHoverInput,
    LspPluginLspManagerPluginProviderImplementationInput,
    LspPluginLspManagerPluginProviderIncomingCallsInput,
    LspPluginLspManagerPluginProviderOutgoingCallsInput,
    LspPluginLspManagerPluginProviderPrepareCallHierarchyInput,
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

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show",
        guidance=ToolGuidance(
            search_objects=('provider', 'providers'),
            purpose="Show LSP provider status.",
            use_when="Diagnosing LSP system health — manager process, server count, last error.",
            do_not_use_when="Checking workspace readiness (use inspect_lsp_status). Running server health check (use diagnose_lsp_server).",
            failure_next_steps="Inspect the reported cause. Correct configuration errors before rescanning; use reload_plugin name='lsp' when the manager lifecycle needs recovery. cached_snapshot is historical, not a live health check.",
        ), aliases=("inspect_lsp_provider",))
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self._status_payload()
        return IntrospectionResult(status=RuntimeStatus.OK, text="lsp status", structured=payload, llm_text=render_titled_structured_for_llm("LSP status", payload))

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="lsp",
        family="lsp",
        action_name="status",
        guidance=ToolGuidance(
            search_objects=('status',),
            purpose="Report recorded workspace preparation and LSP server health. An absent preparation record alone does not block individual queries.",
            use_when="Inspecting workspace-wide preparation and server health, or diagnosing a readiness problem reported by an LSP operation.",
            do_not_use_when="Routine navigation or diagnostics with usable project configuration: call the relevant LSP tool directly; it starts or reuses its server. A recent result already establishes readiness for the unchanged workspace. Module-level status (use inspect_lsp_provider). One server health (use diagnose_lsp_server).",
            failure_next_steps="Follow the reported cause. workspace_not_prepared means no preparation record exists, not that every query requires preparation. Use prepare_lsp_workspace when project environment setup is needed or changed; use diagnose_lsp_server for a specific server failure.",
            next_tool_hints=(
                NextToolHint(name="list_lsp_document_symbols", use_when="A known file's symbol structure must be mapped."),
                NextToolHint(name="search_lsp_workspace_symbols", use_when="A symbol must be located by name across the workspace."),
                NextToolHint(name="find_lsp_definitions", use_when="A known symbol's declaration or definition must be located."),
                NextToolHint(name="find_lsp_references", use_when="Consumers of a known symbol must be found."),
                NextToolHint(name="read_lsp_hover", use_when="Type or documentation at a known position is needed."),
                NextToolHint(name="find_lsp_incoming_calls", use_when="Callers of a symbol at a known file position are needed; preparation is internal."),
                NextToolHint(name="find_lsp_outgoing_calls", use_when="Callees of a symbol at a known file position are needed; preparation is internal."),
                NextToolHint(name="read_lsp_diagnostics", use_when="A source file needs language-server diagnostics."),
            ),
        ),
        InputModel=LspPluginLspManagerPluginProviderStatusInput,
        aliases=("inspect_lsp_status",),
        execution=INDIRECT_LOCAL_READ,
    )
    def status(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc(
            "LSP status",
            self._request_or_error("status", dict(call.args or {})),
        )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="lsp",
        family="lsp",
        action_name="prepare_workspace",
        guidance=ToolGuidance(
            search_objects=('workspace', 'workspaces'),
            purpose="Configure a workspace's LSP project environment and optionally prewarm its language servers.",
            use_when='Project environment setup is needed, such as C/C++ compile commands or include paths; those settings changed; a query reported missing project context; or workspace prewarming is explicitly requested. name pins the server and workspace_root pins the root. Usable project compilation databases, compile_flags.txt and .clangd take precedence over generated fallback flags.',
            do_not_use_when="Routine navigation or diagnostics with usable project configuration: call the relevant LSP tool directly; it starts or reuses its server. Selecting a project alone does not require preparation. Not for non-LSP projects.",
            failure_next_steps="Follow result-specific recovery. Use diagnose_lsp_server for one failing server or inspect_lsp_status when workspace-wide diagnosis is needed; correct the reported environment or server problem before retrying. Reuse known tool contracts.",
            next_tool_hints=(
                NextToolHint(name="inspect_lsp_status", use_when="Preparation was partial or failed and workspace-wide readiness must be inspected."),
                NextToolHint(name="diagnose_lsp_server", use_when="Preparation was partial or failed and one selected language server needs diagnosis."),
            ),
        ),
        InputModel=LspPluginLspManagerPluginProviderPrepareWorkspaceInput,
        aliases=("prepare_lsp_workspace",),
        execution=DIRECT_LOCAL_WRITE,
    )
    def prepare_workspace(self, call: CapabilityCall) -> CapabilityResult:
        payload = self._request_or_error("prepare_workspace", dict(call.args or {}))
        return _prepare_workspace_result(payload)

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="doctor",
        guidance=ToolGuidance(
            search_objects=('server', 'servers'),
            purpose="Check one LSP server's binary, workspace, and initialization readiness.",
            use_when="Diagnosing why a specific language server is not working.",
            do_not_use_when="Workspace-wide readiness (use inspect_lsp_status). Module status (use inspect_lsp_provider).",
            failure_next_steps="If server not found or binary missing, check LSP config and install the language server.",
        ), InputModel=LspPluginLspManagerPluginProviderDoctorInput, aliases=("diagnose_lsp_server",), execution=INDIRECT_LOCAL_READ)
    def doctor(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP doctor", self._request_or_error("doctor", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="diagnostics",
        guidance=ToolGuidance(
            search_objects=('diagnostic', 'diagnostics', 'error', 'errors', 'warning', 'warnings'),
            purpose="Read diagnostics (errors/warnings) for a file.",
            use_when='Checking compile errors or type issues after editing a file. A successful empty result means no diagnostics were reported. Diagnose readiness only when the operation reports an error.',
            do_not_use_when="Reading file content (use read_file). Searching code (use run_shell rg). No LSP server available.",
            failure_next_steps="A successful empty result means no diagnostics were reported. Diagnose readiness only when the operation reports an error.",
        ), InputModel=LspPluginLspManagerPluginProviderDiagnosticsInput, aliases=("read_lsp_diagnostics",), execution=INDIRECT_LOCAL_READ)
    def diagnostics(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP diagnostics", self._request_or_error("diagnostics", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="hover",
        guidance=ToolGuidance(
            search_objects=('hover', 'information', 'type', 'types'),
            purpose="Read hover information (type, docs) at a file position.",
            use_when="Checking a symbol's type signature or documentation at a specific location. A successful empty result means no hover information was returned at this position. Diagnose readiness only on a reported error.",
            do_not_use_when="Finding definitions (use find_lsp_definitions). Reading file content (use read_file).",
            failure_next_steps="A successful empty result means no hover information was returned at this position. Diagnose readiness only on a reported error.",
        ), InputModel=LspPluginLspManagerPluginProviderHoverInput, aliases=("read_lsp_hover",), execution=INDIRECT_LOCAL_READ)
    def hover(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP hover", self._request_or_error("hover", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="definition",
        guidance=ToolGuidance(
            search_objects=('definition', 'definitions'),
            purpose="Find definitions at a file position.",
            use_when='Jumping to where a symbol is defined. A successful empty result means no definition was returned. Check the requested position or use text search if needed; diagnose readiness only on a reported error.',
            do_not_use_when="Finding references (use find_lsp_references). Finding implementations (use find_lsp_implementations).",
            failure_next_steps="A successful empty result means no definition was returned. Check the requested position or use text search if needed; diagnose readiness only on a reported error.",
        ), InputModel=LspPluginLspManagerPluginProviderDefinitionInput, aliases=("find_lsp_definitions",), execution=INDIRECT_LOCAL_READ)
    def definition(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP definition", self._request_or_error("definition", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="implementation",
        guidance=ToolGuidance(
            search_objects=('implementation', 'implementations'),
            purpose="Find implementations at a file position.",
            use_when='Finding concrete implementations of an interface or abstract method. A successful empty result means no implementations were returned. Diagnose readiness only on a reported error.',
            do_not_use_when="Finding definitions (use find_lsp_definitions). Finding references (use find_lsp_references).",
            failure_next_steps="A successful empty result means no implementations were returned. Diagnose readiness only on a reported error.",
        ), InputModel=LspPluginLspManagerPluginProviderImplementationInput, aliases=("find_lsp_implementations",), execution=INDIRECT_LOCAL_READ)
    def implementation(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP implementation", self._request_or_error("implementation", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="references",
        guidance=ToolGuidance(
            search_objects=('reference', 'references'),
            purpose="Find references at a file position.",
            use_when='Finding all places that reference a symbol. A successful empty result means no references were returned. Diagnose readiness only on a reported error.',
            do_not_use_when="Finding definitions (use find_lsp_definitions). Call hierarchy (use find_lsp_incoming_calls).",
            failure_next_steps="A successful empty result means no references were returned. Diagnose readiness only on a reported error.",
        ), InputModel=LspPluginLspManagerPluginProviderReferencesInput, aliases=("find_lsp_references",), execution=INDIRECT_LOCAL_READ)
    def references(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP references", self._request_or_error("references", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="prepare_call_hierarchy",
        guidance=ToolGuidance(
            search_objects=('hierarchy', 'hierarchies', 'item', 'items'),
            purpose="Prepare call hierarchy items at a file position.",
            use_when='Inspecting the candidate call hierarchy symbols at a file position when those items themselves are needed. If empty, the position may not be a callable symbol.',
            do_not_use_when="Finding callers or callees at a known file position: call find_lsp_incoming_calls or find_lsp_outgoing_calls directly; each prepares its items internally.",
            failure_next_steps="If empty, the position may not be a callable symbol.",
            next_tool_hints=(
                NextToolHint(name="find_lsp_incoming_calls", use_when="The prepared item needs its callers."),
                NextToolHint(name="find_lsp_outgoing_calls", use_when="The prepared item needs its callees."),
            ),
        ), InputModel=LspPluginLspManagerPluginProviderPrepareCallHierarchyInput, aliases=("prepare_lsp_call_hierarchy",), execution=INDIRECT_LOCAL_READ)
    def prepare_call_hierarchy(self, call: CapabilityCall) -> CapabilityResult:
        return _call_hierarchy_result(
            self._request_or_error("prepare_call_hierarchy", dict(call.args or {})),
            dict(call.args or {}),
        )

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="incoming_calls",
        guidance=ToolGuidance(
            search_objects=('call', 'calls', 'caller', 'callers'),
            purpose="Find callers (incoming calls) for a symbol.",
            use_when='Tracing who calls a specific function or method at a known file position. Preparation is internal; no prior prepare_lsp_call_hierarchy call is needed. A successful empty result means no callers were returned. Diagnose readiness only on a reported error.',
            do_not_use_when="Finding callees (use find_lsp_outgoing_calls). Finding references (use find_lsp_references).",
            failure_next_steps="A successful empty result means no callers were returned. Diagnose readiness only on a reported error.",
        ), InputModel=LspPluginLspManagerPluginProviderIncomingCallsInput, aliases=("find_lsp_incoming_calls",), execution=INDIRECT_LOCAL_READ)
    def incoming_calls(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP incoming calls", self._request_or_error("incoming_calls", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="outgoing_calls",
        guidance=ToolGuidance(
            search_objects=('call', 'calls', 'callee', 'callees'),
            purpose="Find callees (outgoing calls) for a symbol.",
            use_when='Tracing what a specific function or method calls at a known file position. Preparation is internal; no prior prepare_lsp_call_hierarchy call is needed. A successful empty result means no callees were returned. Diagnose readiness only on a reported error.',
            do_not_use_when="Finding callers (use find_lsp_incoming_calls). Finding references (use find_lsp_references).",
            failure_next_steps="A successful empty result means no callees were returned. Diagnose readiness only on a reported error.",
        ), InputModel=LspPluginLspManagerPluginProviderOutgoingCallsInput, aliases=("find_lsp_outgoing_calls",), execution=INDIRECT_LOCAL_READ)
    def outgoing_calls(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP outgoing calls", self._request_or_error("outgoing_calls", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="document_symbols",
        guidance=ToolGuidance(
            search_objects=('symbol', 'symbols', 'function', 'functions', 'class', 'classes', 'variable', 'variables'),
            purpose="List document symbols (functions, classes, variables) for a file.",
            use_when='Mapping the structure of a file before reading it in detail. A successful empty result means no document symbols were returned. Read the file if its contents are needed; diagnose readiness only on a reported error.',
            do_not_use_when="Workspace-wide symbol search (use search_lsp_workspace_symbols). Reading file content (use read_file).",
            failure_next_steps="A successful empty result means no document symbols were returned. Read the file if its contents are needed; diagnose readiness only on a reported error.",
        ), InputModel=LspPluginLspManagerPluginProviderDocumentSymbolsInput, aliases=("list_lsp_document_symbols",), execution=INDIRECT_LOCAL_READ)
    def document_symbols(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP document symbols", self._request_or_error("document_symbols", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="workspace_symbols",
        guidance=ToolGuidance(
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
            purpose="Rescan LSP server configs and refresh health.",
            use_when="After adding or modifying LSP server configuration.",
            do_not_use_when="Restarting the manager (use reload_plugin with name='lsp').",
            failure_next_steps="Use inspect_lsp_provider and inspect_lsp_status to reconcile manager readiness. Correct LSP config syntax or server availability before retrying.",
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

    def _request_or_error(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
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
        # Keep cached observations available through inspect_lsp_provider, never as this
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
            "cached_snapshot": dict(self.last_health or {}),
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
                    tool="diagnose_lsp_server",
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


def _call_hierarchy_result(payload: dict[str, Any], args: dict[str, Any]) -> CapabilityResult:
    """Bind call-hierarchy continuation to the position that produced the item.

    Exactly one prepared item supports an unambiguous callers/callees
    continuation at the same position; empty or ambiguous results stay facts.
    """
    result = _capability_from_rpc("LSP prepare call hierarchy", payload)
    affordances: list[ToolAffordance] = []
    items = payload.get("items")
    raw_status = str(payload.get("status") or RuntimeStatus.OK)
    if raw_status == RuntimeStatus.OK and isinstance(items, list) and len(items) == 1:
        continuation: dict[str, Any] = {
            key: args[key] for key in ("file", "line", "character") if key in args
        }
        if args.get("workspace_root"):
            continuation["workspace_root"] = args["workspace_root"]
        if args.get("name"):
            continuation["name"] = args["name"]
        affordances.extend(
            (
                ToolAffordance(
                    tool="find_lsp_incoming_calls",
                    arguments=dict(continuation),
                    reason="The prepared item can resolve its callers at this position.",
                ),
                ToolAffordance(
                    tool="find_lsp_outgoing_calls",
                    arguments=dict(continuation),
                    reason="The prepared item can resolve its callees at this position.",
                ),
            )
        )
    return CapabilityResult(
        status=result.status,
        text=result.text,
        structured=result.structured,
        llm_text=result.llm_text,
        affordances=tuple(affordances),
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
    evidence = payload.get("evidence")
    if isinstance(evidence, dict) and "result" in payload and evidence.get("result") == payload["result"]:
        projection["evidence"] = {key: value for key, value in evidence.items() if key != "result"}
    return CapabilityResult(status=status, text=title, structured=payload,
                            llm_text=render_titled_structured_for_llm(title, projection),
                            recovery_hint=str(payload.get("next_step") or "") if status != RuntimeStatus.OK else "")
