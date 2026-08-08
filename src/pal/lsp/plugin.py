from __future__ import annotations

from pal.execution.tool_semantics import (
    DIRECT_LOCAL_WRITE,
    INDIRECT_CONTROL,
    INDIRECT_LOCAL_READ,
    INDIRECT_LOCAL_WRITE,
)
from pal.execution.tool_facade import ToolGuidance

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
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.behavior.decorators import affordance
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


def _file_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "workspace_root": {"type": "string"},
            "server_id": {"type": "string"},
        },
        "required": ["file"],
    }


def _doctor_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "file": {"type": "string"},
            "path": {"type": "string"},
            "workspace_root": {"type": "string"},
            "server_id": {"type": "string"},
        },
    }


def _prepare_workspace_schema() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "workspace_root": {
                "type": "string",
                "description": "Canonical project/worktree root to prepare for later LSP queries.",
            },
            "primary_language": {
                "type": "string",
                "description": "Optional primary language; omit to detect it from workspace source files.",
            },
            "languages": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional additional workspace languages.",
            },
            "compile_commands_path": {
                "type": "string",
                "description": "Optional existing compile_commands.json path for C/C++/Objective-C.",
            },
            "include_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Existing project include directories, relative to workspace_root or absolute.",
            },
            "stub_include_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Existing caller-created SDK/stub include directories; LSP never fabricates these APIs.",
            },
            "cpp_standard": {
                "type": "string",
                "description": "Optional C/C++ language standard such as c++17.",
            },
            "lsp_compile_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional fallback compile flags when no project compile database exists.",
            },
            "prewarm": {
                "type": "boolean",
                "default": True,
                "description": "Initialize matching language servers immediately after preparing the environment.",
            },
        },
        "required": ["workspace_root"],
    }


def _position_schema() -> dict[str, Any]:
    schema = _file_schema()
    schema["properties"] = {
        **schema["properties"],
        "line": {"type": "integer", "description": "0-based line number"},
        "character": {"type": "integer", "description": "0-based UTF-16 character offset"},
    }
    schema["required"] = ["file", "line", "character"]
    return schema


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
@affordance(
    affordance_id="declared.lsp.code_intelligence",
    title="LSP code intelligence",
    scenario_text=(
        "Pal is reading, navigating, editing, reviewing, or verifying source code and may benefit from "
        "symbol-aware language-server information."
    ),
    prompt_hint=(
        "After selecting a project or worktree, call lsp_prepare_workspace once before using LSP code "
        "intelligence; call it again only when compile commands, include paths, SDK stubs, or language settings "
        "change. Then use LSP capabilities for symbol-aware navigation and verification: "
        "use lsp_document_symbols/workspace_symbols to map code, lsp_definition/references/hover/call hierarchy "
        "to understand relationships, and lsp_diagnostics after edits when a matching server is available. "
        "Pair LSP with source reads, search, and tests; do not treat LSP as a substitute for inspecting source."
    ),
    activation_terms=(
        "code",
        "source",
        "symbol",
        "definition",
        "references",
        "diagnostics",
        "lsp",
        "language server",
        "python",
        "cpp",
        "typescript",
        "读代码",
        "代码导航",
        "诊断",
    ),
    capability_refs=(
        "lsp_status",
        "lsp_prepare_workspace",
        "lsp_doctor",
        "lsp_diagnostics",
        "lsp_hover",
        "lsp_definition",
        "lsp_implementation",
        "lsp_references",
        "lsp_prepare_call_hierarchy",
        "lsp_incoming_calls",
        "lsp_outgoing_calls",
        "lsp_document_symbols",
        "lsp_workspace_symbols",
    ),
    priority=75,
    activation_threshold=0.15,
)
@affordance(
    affordance_id="declared.skill.pal_lsp_template_development",
    title="Pal LSP template development skill",
    scenario_text=(
        "The user wants to add, repair, test, or hot-load an LSP server template, language server config, "
        "or new programming language LSP support."
    ),
    prompt_hint=(
        "If this route is selected, inject skill `pal.lsp.template.development` before creating "
        "plugins/lsp/servers templates or LSP language server config."
    ),
    activation_terms=(
        "lsp template",
        "language server template",
        "new language lsp",
        "add lsp support",
        "add language support",
        "language server",
        "plugins/lsp/servers",
        "language_ids",
        "op_lsp_mgmt_rescan",
        "lsp 插件",
        "语言服务器",
        "新语言",
    ),
    skill_refs=(PAL_LSP_TEMPLATE_DEVELOPMENT_SKILL_ID,),
    priority=35,
    activation_threshold=0.2,
    metadata={"skill_trigger": True, "resident": False, "runtime_root_layout": "plugins/lsp/servers"},
)
@dataclass
class LspManagerPluginProvider:
    runtime_root: Path
    client: LspManagerClient = field(init=False)
    process: subprocess.Popen | None = None
    last_error: str = ""
    last_health: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.client = LspManagerClient(runtime_root=self.runtime_root)

    def declared_skills(self):
        return lsp_declared_skills(module_id="lsp")

    @capability_action(namespace=INTROSPECTION_NAMESPACE, scope="module", action_name="show", description="Show LSP provider status",
        guidance=ToolGuidance(
            purpose="Show LSP provider status.",
            use_when="Diagnosing LSP system health — manager process, server count, last error.",
            do_not_use_when="Checking workspace readiness (use lsp_status). Running server health check (use lsp_doctor).",
            failure_next_steps="Read-only. If last_error is set, the LSP sidecar failed — try lsp_attach.",
        ), aliases=("lsp_show",))
    def show(self, call: IntrospectionCall) -> IntrospectionResult:
        _ = call
        payload = self._status_payload()
        return IntrospectionResult(status=RuntimeStatus.OK, text="lsp status", structured=payload, llm_text=render_titled_structured_for_llm("LSP status", payload))

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="lsp",
        family="lsp",
        action_name="status",
        description="Report the bound worktree's persisted LSP readiness and recognition-probe result, plus configured server health.",
        guidance=ToolGuidance(
            purpose="Report workspace LSP readiness and server health.",
            use_when="Checking if LSP is ready for navigation before using definition/hover/references.",
            do_not_use_when="Module-level status (use lsp_show). One server health (use lsp_doctor).",
            failure_next_steps="If not ready, run lsp_prepare_workspace first.",
        ),
        InputModel=LspPluginLspManagerPluginProviderStatusInput,
        aliases=("lsp_status",),
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
        description="Prepare and prewarm one workspace before LSP navigation or diagnostics.",
        guidance=ToolGuidance(
            purpose="Prepare and prewarm one workspace before LSP navigation or diagnostics.",
            use_when="Once after selecting a project/worktree. Again when compile commands, include paths, or language settings change. After preparing, follow up with the LSP tool family: lsp_document_symbols/workspace_symbols to map code, lsp_definition/references/hover/call hierarchy to understand relationships, and lsp_diagnostics after edits when a matching server is available.",
            do_not_use_when="Not a substitute for reading source. Not for non-LSP projects.",
            failure_next_steps="Check compile_commands.json and language server availability.",
        ),
        InputModel=LspPluginLspManagerPluginProviderPrepareWorkspaceInput,
        aliases=("lsp_prepare_workspace",),
        execution=DIRECT_LOCAL_WRITE,
    )
    def prepare_workspace(self, call: CapabilityCall) -> CapabilityResult:
        payload = self._request_or_error("prepare_workspace", dict(call.args or {}))
        return _prepare_workspace_result(payload)

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="doctor", description="Check one selected LSP server's binary, workspace, initialize, and diagnostics readiness",
        guidance=ToolGuidance(
            purpose="Check one LSP server's binary, workspace, and initialization readiness.",
            use_when="Diagnosing why a specific language server is not working.",
            do_not_use_when="Workspace-wide readiness (use lsp_status). Module status (use lsp_show).",
            failure_next_steps="If server not found or binary missing, check LSP config and install the language server.",
        ), InputModel=LspPluginLspManagerPluginProviderDoctorInput, aliases=("lsp_doctor",), execution=INDIRECT_LOCAL_READ)
    def doctor(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP doctor", self._request_or_error("doctor", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="diagnostics", description="Read diagnostics for a file from the selected LSP server",
        guidance=ToolGuidance(
            purpose="Read diagnostics (errors/warnings) for a file.",
            use_when="Checking compile errors or type issues after editing a file.",
            do_not_use_when="Reading file content (use read_file). Searching code (use run_shell rg). No LSP server available.",
            failure_next_steps="If empty, the file may have no issues or LSP is not ready — check lsp_status.",
        ), InputModel=LspPluginLspManagerPluginProviderDiagnosticsInput, aliases=("lsp_diagnostics",), execution=INDIRECT_LOCAL_READ)
    def diagnostics(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP diagnostics", self._request_or_error("diagnostics", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="hover", description="Read hover information at a file position",
        guidance=ToolGuidance(
            purpose="Read hover information (type, docs) at a file position.",
            use_when="Checking a symbol's type signature or documentation at a specific location.",
            do_not_use_when="Finding definitions (use lsp_definition). Reading file content (use read_file).",
            failure_next_steps="If empty, LSP may not have hover info for this position — check lsp_status.",
        ), InputModel=LspPluginLspManagerPluginProviderHoverInput, aliases=("lsp_hover",), execution=INDIRECT_LOCAL_READ)
    def hover(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP hover", self._request_or_error("hover", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="definition", description="Find definitions at a file position",
        guidance=ToolGuidance(
            purpose="Find definitions at a file position.",
            use_when="Jumping to where a symbol is defined.",
            do_not_use_when="Finding references (use lsp_references). Finding implementations (use lsp_implementation).",
            failure_next_steps="If empty, LSP may not index this file — check lsp_status.",
        ), InputModel=LspPluginLspManagerPluginProviderDefinitionInput, aliases=("lsp_definition",), execution=INDIRECT_LOCAL_READ)
    def definition(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP definition", self._request_or_error("definition", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="implementation", description="Find implementations at a file position",
        guidance=ToolGuidance(
            purpose="Find implementations at a file position.",
            use_when="Finding concrete implementations of an interface or abstract method.",
            do_not_use_when="Finding definitions (use lsp_definition). Finding references (use lsp_references).",
            failure_next_steps="If empty, no implementations found or LSP not ready.",
        ), InputModel=LspPluginLspManagerPluginProviderImplementationInput, aliases=("lsp_implementation",), execution=INDIRECT_LOCAL_READ)
    def implementation(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP implementation", self._request_or_error("implementation", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="references", description="Find references at a file position",
        guidance=ToolGuidance(
            purpose="Find references at a file position.",
            use_when="Finding all places that reference a symbol.",
            do_not_use_when="Finding definitions (use lsp_definition). Call hierarchy (use lsp_incoming_calls).",
            failure_next_steps="If empty, no references found or LSP not ready.",
        ), InputModel=LspPluginLspManagerPluginProviderReferencesInput, aliases=("lsp_references",), execution=INDIRECT_LOCAL_READ)
    def references(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP references", self._request_or_error("references", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="prepare_call_hierarchy", description="Prepare call hierarchy items at a file position",
        guidance=ToolGuidance(
            purpose="Prepare call hierarchy items at a file position.",
            use_when="Starting a call hierarchy query — get the symbol before finding callers or callees.",
            do_not_use_when="Directly finding callers (use lsp_incoming_calls) or callees (use lsp_outgoing_calls) if you already have the item.",
            failure_next_steps="If empty, the position may not be a callable symbol.",
        ), InputModel=LspPluginLspManagerPluginProviderPrepareCallHierarchyInput, aliases=("lsp_prepare_call_hierarchy",), execution=INDIRECT_LOCAL_READ)
    def prepare_call_hierarchy(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP prepare call hierarchy", self._request_or_error("prepare_call_hierarchy", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="incoming_calls", description="Find callers for the symbol at a file position using LSP call hierarchy",
        guidance=ToolGuidance(
            purpose="Find callers (incoming calls) for a symbol.",
            use_when="Tracing who calls a specific function or method.",
            do_not_use_when="Finding callees (use lsp_outgoing_calls). Finding references (use lsp_references).",
            failure_next_steps="If empty, no callers found or LSP not ready.",
        ), InputModel=LspPluginLspManagerPluginProviderIncomingCallsInput, aliases=("lsp_incoming_calls",), execution=INDIRECT_LOCAL_READ)
    def incoming_calls(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP incoming calls", self._request_or_error("incoming_calls", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="outgoing_calls", description="Find callees for the symbol at a file position using LSP call hierarchy",
        guidance=ToolGuidance(
            purpose="Find callees (outgoing calls) for a symbol.",
            use_when="Tracing what a specific function or method calls.",
            do_not_use_when="Finding callers (use lsp_incoming_calls). Finding references (use lsp_references).",
            failure_next_steps="If empty, no callees found or LSP not ready.",
        ), InputModel=LspPluginLspManagerPluginProviderOutgoingCallsInput, aliases=("lsp_outgoing_calls",), execution=INDIRECT_LOCAL_READ)
    def outgoing_calls(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP outgoing calls", self._request_or_error("outgoing_calls", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="document_symbols", description="List document symbols for a file",
        guidance=ToolGuidance(
            purpose="List document symbols (functions, classes, variables) for a file.",
            use_when="Mapping the structure of a file before reading it in detail.",
            do_not_use_when="Workspace-wide symbol search (use lsp_workspace_symbols). Reading file content (use read_file).",
            failure_next_steps="If empty, LSP may not index this file type — check lsp_status.",
        ), InputModel=LspPluginLspManagerPluginProviderDocumentSymbolsInput, aliases=("lsp_document_symbols",), execution=INDIRECT_LOCAL_READ)
    def document_symbols(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP document symbols", self._request_or_error("document_symbols", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="lsp", action_name="workspace_symbols", description="Search workspace symbols",
        guidance=ToolGuidance(
            purpose="Search workspace symbols by name.",
            use_when="Finding where a symbol is defined across the entire workspace.",
            do_not_use_when="One file's symbols (use lsp_document_symbols). Text search (use run_shell rg).",
            failure_next_steps="If empty, no matches or LSP not ready — check lsp_status.",
        ), InputModel=LspPluginLspManagerPluginProviderWorkspaceSymbolsInput, aliases=("lsp_workspace_symbols",), execution=INDIRECT_LOCAL_READ)
    def workspace_symbols(self, call: CapabilityCall) -> CapabilityResult:
        return _capability_from_rpc("LSP workspace symbols", self._request_or_error("workspace_symbols", dict(call.args or {})))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="management", action_name="attach", description="Attach LSP manager",
        guidance=ToolGuidance(
            purpose="Attach LSP manager — start sidecar and discover servers.",
            use_when="Reconnecting a detached LSP manager.",
            do_not_use_when="The manager is already attached. Preparing a workspace (use lsp_prepare_workspace).",
            failure_next_steps="If sidecar fails to start, check LSP config and binary availability.",
        ), aliases=("lsp_attach",), execution=INDIRECT_CONTROL)
    def attach(self, call: IntrospectionCall | None = None) -> IntrospectionResult:
        _ = call
        try:
            self._ensure_manager_started()
            self.last_health = self.client.rescan_sync()
            self.last_error = ""
        except Exception as exc:
            self.last_error = f"{exc.__class__.__name__}: {exc}"
            self.last_health = {"healthy": False, "startup_error": self.last_error}
        payload = self._status_payload()
        return IntrospectionResult(status=RuntimeStatus.OK, text="lsp manager attached", structured=payload, llm_text=render_titled_structured_for_llm("LSP manager attached", payload))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="management", action_name="detach", description="Detach LSP manager",
        guidance=ToolGuidance(
            purpose="Detach LSP manager — stop sidecar.",
            use_when="Temporarily stopping all LSP functionality.",
            do_not_use_when="Individual navigation still needed. Detaching a channel (use channel_detach).",
            failure_next_steps="Re-attach with lsp_attach.",
        ), aliases=("lsp_detach",), execution=INDIRECT_CONTROL)
    def detach(self, call: IntrospectionCall | None = None) -> IntrospectionResult:
        _ = call
        self._stop_manager()
        payload = self._status_payload()
        return IntrospectionResult(status=RuntimeStatus.OK, text="lsp manager detached", structured=payload, llm_text=render_titled_structured_for_llm("LSP manager detached", payload))

    @capability_action(namespace=OPERATION_NAMESPACE, scope="lsp", family="management", action_name="rescan", description="Rescan LSP configs",
        guidance=ToolGuidance(
            purpose="Rescan LSP server configs and refresh health.",
            use_when="After adding or modifying LSP server configuration.",
            do_not_use_when="Restarting the manager (use lsp_detach then lsp_attach).",
            failure_next_steps="If rescan fails, check LSP config file syntax.",
        ), aliases=("lsp_rescan",), execution=INDIRECT_CONTROL)
    def rescan(self, call: IntrospectionCall | None = None) -> IntrospectionResult:
        _ = call
        try:
            self._ensure_manager_started()
            payload = self.client.rescan_sync()
            self.last_health = dict(payload)
            return IntrospectionResult(status=RuntimeStatus.OK, text="lsp rescan", structured=payload, llm_text=render_titled_structured_for_llm("LSP rescan", payload))
        except Exception as exc:
            payload = {"status": RuntimeStatus.ERROR, "error": f"{exc.__class__.__name__}: {exc}", **self._status_payload()}
            return IntrospectionResult(status=RuntimeStatus.ERROR, text="lsp rescan failed", structured=payload, llm_text=render_titled_structured_for_llm("LSP rescan failed", payload))

    def _ensure_manager_started(self) -> None:
        if self.process is not None and self.process.poll() is None:
            try:
                self.last_health = self.client.health_sync()
                return
            except Exception:
                self._stop_process_only()
        try:
            self.last_health = self.client.health_sync()
            self.last_error = ""
            return
        except Exception:
            pass
        self._cleanup_stale_socket()
        self.process = subprocess.Popen(
            [sys.executable, "-m", "pal.lsp.manager_main", "--runtime-root", str(self.runtime_root)],
            env=python_subprocess_env(),
        )
        for _ in range(100):
            if self.process.poll() is not None:
                raise RuntimeError("lsp manager exited during startup")
            try:
                self.last_health = self.client.health_sync()
                return
            except Exception:
                time.sleep(0.1)
        raise RuntimeError("lsp manager failed to start")

    def _stop_manager(self) -> None:
        process = self.process
        if process is not None and process.poll() is None:
            with contextlib.suppress(Exception):
                self.client.shutdown_sync()
            with contextlib.suppress(Exception):
                process.wait(timeout=2.0)
        self._stop_process_only()
        self.last_health = {}

    def _cleanup_stale_socket(self) -> None:
        socket_path = self.client.socket_path
        if not socket_path.exists():
            return
        try:
            self.client.health_sync()
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                socket_path.unlink()

    def _stop_process_only(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=1.0)
        except Exception:
            with contextlib.suppress(Exception):
                process.kill()
        self.process = None

    def _request_or_error(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            self._ensure_manager_started()
            payload = self.client.operation_sync(method, params or {})
            self.last_health = dict(payload)
            return payload
        except Exception as exc:
            return {"status": RuntimeStatus.ERROR, "error": f"{exc.__class__.__name__}: {exc}", **self._status_payload()}

    def _status_payload(self) -> dict[str, Any]:
        return {
            "module_id": "lsp",
            "manager_running": self._manager_running(),
            "manager_owned": self.process is not None and self.process.poll() is None,
            "log_sink": current_service_log_sink_description(),
            "last_error": self.last_error,
            **dict(self.last_health or {}),
        }

    def _manager_running(self) -> bool:
        if self.process is not None and self.process.poll() is None:
            return True
        return bool((self.last_health or {}).get("ok"))


@dataclass
class LspManagerPluginBundle:
    runtime_root: Path
    plugin_id: str = "lsp"
    version: str = "0.1.0"

    def register_with_core(self, context) -> ModuleHandle:
        provider = LspManagerPluginProvider(runtime_root=self.runtime_root)
        handle = ModuleHandle(
            module_id="lsp",
            tier=MODULE_TIER_DETACHABLE,
            detachable=True,
            mounted=False,
            introspection_provider=provider,
            supports_lifecycle_capabilities=True,
            ports={"lsp": provider},
            shutdown_sync=provider._stop_manager,
        )
        context.register_module(handle)
        return handle


def build_lsp_plugin(*, runtime_root: Path) -> LspManagerPluginBundle:
    return LspManagerPluginBundle(runtime_root=runtime_root)


def _capability_from_rpc(title: str, payload: dict[str, Any]) -> CapabilityResult:
    status = payload.get("status") or RuntimeStatus.OK
    if status == "unavailable":
        status = RuntimeStatus.ERROR
    elif status == "partial":
        status = RuntimeStatus.OK
    return CapabilityResult(status=status, text=title, structured=payload, llm_text=render_titled_structured_for_llm(title, payload))


def _prepare_workspace_result(payload: dict[str, Any]) -> CapabilityResult:
    projected = dict(payload)
    raw_status = str(projected.get("status") or RuntimeStatus.OK)
    if raw_status == RuntimeStatus.OK:
        projected["next_tools"] = {
            "map_code": ["lsp_document_symbols", "lsp_workspace_symbols"],
            "inspect_symbol": ["lsp_hover", "lsp_definition", "lsp_implementation", "lsp_references"],
            "trace_calls": ["lsp_prepare_call_hierarchy", "lsp_incoming_calls", "lsp_outgoing_calls"],
            "verify_edits": ["lsp_diagnostics"],
        }
        direction = (
            "Workspace preparation completed. The LSP tools above are indirect capabilities: "
            "invoke the relevant one with call_tool using its exact alias; use read_tool first when its arguments are unclear."
        )
    elif raw_status == "partial":
        projected["next_tools"] = {
            "inspect_readiness": ["lsp_status", "lsp_doctor"],
            "refresh_configuration": ["lsp_rescan"],
        }
        direction = (
            "Workspace preparation is partial: the primary language server is not ready even though another "
            "detected server may be available. Use call_tool with lsp_status or lsp_doctor before navigation; "
            "after changing server configuration, use lsp_rescan and retry lsp_prepare_workspace."
        )
    else:
        projected["next_tools"] = {
            "inspect_readiness": ["lsp_status", "lsp_doctor"],
            "refresh_configuration": ["lsp_rescan"],
        }
        direction = (
            "Workspace preparation did not become ready. Use call_tool with lsp_status or lsp_doctor; "
            "after changing server configuration, use lsp_rescan and retry lsp_prepare_workspace."
        )
    result = _capability_from_rpc("LSP workspace preparation", projected)
    return CapabilityResult(
        status=result.status,
        text=result.text,
        structured=result.structured,
        llm_text=f"{result.llm_text}\n\n{direction}",
    )
