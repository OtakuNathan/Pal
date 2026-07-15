from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.execution import CapabilityDescriptor, CapabilityResult
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_result_pager import ToolResultPageTool
from pal.execution.tool_search import ToolCallTool, ToolReadTool, ToolSearchTool
from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.profiles import filter_minion_allowed_capabilities, is_minion_capability_denied
from pal.minion.tool_admission import (
    admit_minion_tool_call,
    effective_minion_capability_name,
    effective_minion_tool_args,
)
from pal.minion.v2.contract_builder import (
    CONTRACT_BUILDER_CAPABILITIES,
    CONTRACT_BUILDER_TOOL_SPECS,
    contract_builder_tool_result,
    is_contract_builder_capability,
)
from pal.minion.v2.candidate_builder import (
    CANDIDATE_BUILDER_CAPABILITIES,
    CANDIDATE_BUILDER_TOOL_SPECS,
    candidate_builder_tool_result,
    is_candidate_builder_capability,
)
from pal.minion.v2.skeleton_builder import (
    SKELETON_BUILDER_CAPABILITIES,
    SKELETON_BUILDER_TOOL_SPECS,
    is_skeleton_builder_capability,
    skeleton_builder_tool_result,
)
from pal.minion.v2.verification_builder import (
    VERIFICATION_BUILDER_CAPABILITIES,
    VERIFICATION_BUILDER_TOOL_SPECS,
    VERIFICATION_TOOL_CAPABILITIES,
    is_verification_builder_capability,
    verification_builder_tool_result,
)
from pal.minion.workspace_file_tools import (
    WORKSPACE_FILE_TOOL_SPECS,
    bound_input_tool_result,
    workspace_file_tool_result,
)
from pal.minion.workspace_tools import _append_unique_artifact, _workspace_tool_result
from pal.shared import (
    RuntimeStatus,
    default_tool_result_text,
    llm_tool_name,
    replace_internal_tool_names,
    replace_internal_tool_names_in_value,
)


MINION_DISCOVERY_TOOL_SURFACE = (
    "op_tool_search",
    "op_tool_read",
    "op_tool_result_page",
    "op_tool_call",
)

MINION_CODE_INTEL_TOOL_SURFACE = (
    "op_lsp_status",
    "op_lsp_doctor",
    "op_lsp_hover",
    "op_lsp_definition",
    "op_lsp_implementation",
    "op_lsp_references",
    "op_lsp_prepare_call_hierarchy",
    "op_lsp_incoming_calls",
    "op_lsp_outgoing_calls",
    "op_lsp_document_symbols",
    "op_lsp_workspace_symbols",
    "op_lsp_diagnostics",
)

MINION_DIRECT_WORK_TOOL_SURFACE = (
    "op_file_read",
    "op_minion_input_read",
    "op_file_edit",
    "op_file_write",
    "op_path_delete",
    "op_file_state",
    "op_git",
    "op_exec_shell",
    "op_tree",
    "op_search",
    "op_minion_artifact_write",
    "op_minion_artifact_edit",
    *CONTRACT_BUILDER_CAPABILITIES,
    *CANDIDATE_BUILDER_CAPABILITIES,
    *SKELETON_BUILDER_CAPABILITIES,
    *VERIFICATION_TOOL_CAPABILITIES,
    "op_web_search",
    "op_web_read",
    "op_memory_recall",
    *MINION_CODE_INTEL_TOOL_SURFACE,
)


_WORKSPACE_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_input_read": {
        "name": "op_minion_input_read",
        "description": (
            "Read one immutable input explicitly bound to this invocation by name. Use this before making claims from a bound artifact; "
            "it cannot read the project repo or undeclared paths. For a bound directory, provide its root-relative path."
        ),
        "parameters_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Bound immutable input name shown in the invocation prompt."},
                "path": {"type": "string", "default": "", "description": "Optional root-relative file path for a bound directory."},
                "start_line": {"type": "integer", "default": 1},
                "limit_lines": {"type": "integer", "default": 2000},
            },
            "required": ["name"],
            "additionalProperties": False,
        },
    },
    "op_tree": {
        "name": "op_tree",
        "description": "List project or declared read-only reference paths without shell traversal.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "."},
                "root": {"type": "string"},
                "reference_name": {"type": "string"},
                "max_depth": {"type": "integer", "default": 2},
                "limit": {"type": "integer", "default": 200},
            },
        },
    },
    "op_search": {
        "name": "op_search",
        "description": "Search text in the project or a declared read-only truth-source reference.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "path": {"type": "string", "default": "."},
                "root": {"type": "string"},
                "reference_name": {"type": "string"},
                "limit": {"type": "integer", "default": 50},
            },
            "required": ["query"],
        },
    },
    "op_minion_artifact_write": {
        "name": "op_minion_artifact_write",
        "description": "Write the profile-declared structured output artifact. Architecture roles must use their bound Contract Builder instead.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string"},
                "content": {},
                "artifact_type": {"type": "string"},
                "role": {"type": "string"},
            },
            "required": ["relative_path", "content"],
        },
    },
    "op_minion_artifact_edit": {
        "name": "op_minion_artifact_edit",
        "description": "Edit an existing profile output artifact by exact text replacement.",
        "parameters_schema": {
            "type": "object",
            "properties": {
                "relative_path": {"type": "string"},
                "old_string": {"type": "string"},
                "new_string": {"type": "string"},
            },
            "required": ["relative_path", "old_string", "new_string"],
        },
    },
    **WORKSPACE_FILE_TOOL_SPECS,
    **CONTRACT_BUILDER_TOOL_SPECS,
    **CANDIDATE_BUILDER_TOOL_SPECS,
    **SKELETON_BUILDER_TOOL_SPECS,
    **VERIFICATION_BUILDER_TOOL_SPECS,
}


@dataclass
class _HydratedTool:
    name: str
    spec: dict[str, Any]
    handler: Any
    display_name: str = ""
    family: str = "workflow"
    tags: tuple[str, ...] = ("workflow",)
    keywords: tuple[str, ...] = ()

    @property
    def public_name(self) -> str:
        configured = str(self.spec.get("name") or self.name)
        if configured != self.name:
            return configured
        projected = llm_tool_name(self.name)
        if projected.startswith("minion_"):
            return projected.removeprefix("minion_")
        return projected

    @property
    def aliases(self) -> tuple[str, ...]:
        return tuple(str(item) for item in list(self.spec.get("aliases") or []))

    @property
    def description(self) -> str:
        return str(self.spec.get("description") or self.name)

    @property
    def args_schema(self) -> dict[str, Any]:
        return dict(self.spec.get("parameters_schema") or {"type": "object", "properties": {}})

    @property
    def result_schema(self) -> dict[str, Any]:
        return dict(self.spec.get("result_schema") or {"type": "object", "properties": {}})

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        return CapabilityResult(status=RuntimeStatus.ERROR, text="async execution required", llm_text="async execution required")

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        raw = kwargs.get("tool_call")
        call = raw if isinstance(raw, CanonicalToolCall) else CanonicalToolCall(name=self.name, args=dict(args or {}))
        result = self.handler(call, kwargs)
        if inspect.isawaitable(result):
            result = await result
        if not isinstance(result, CanonicalToolResult):
            text = str(result or "tool completed")
            return CapabilityResult(status=RuntimeStatus.OK, text=text, llm_text=text)
        llm_text = default_tool_result_text(result, fallback_ok="tool completed", fallback_error="tool failed")
        return CapabilityResult(
            status=result.status or (RuntimeStatus.OK if result.ok else RuntimeStatus.ERROR),
            text=result.text or llm_text,
            structured=dict(result.structured or {}),
            llm_text=llm_text,
        )


class _ExecutionOverlay:
    def __init__(self, delegate: Any) -> None:
        self.delegate = delegate
        self.runtime = ExecutionRuntime(
            runtime_root=getattr(delegate, "runtime_root", None),
            sync_executor=getattr(delegate, "sync_executor", None),
        )

    def register_tool(self, tool: Any) -> None:
        canonical = str(tool.name)
        public_name = llm_tool_name(getattr(tool, "public_name", "") or canonical)
        self.runtime.register_tool(tool)
        self.runtime.register_capability(
            CapabilityDescriptor(
                name=public_name,
                canonical_path=canonical,
                display_name=str(getattr(tool, "display_name", "") or canonical),
                family=str(getattr(tool, "family", "") or "minion"),
                description=replace_internal_tool_names(
                    _replace_worker_internal_tool_names(getattr(tool, "description", "") or canonical)
                ),
                source="workflow:scoped-worker",
                aliases=tuple(getattr(tool, "aliases", ()) or ()),
                parameters_schema=dict(getattr(tool, "args_schema", {}) or {}),
                result_schema=dict(getattr(tool, "result_schema", {}) or {}),
                module_id="workflow_scoped",
            ),
            lambda _call: CapabilityResult(status=RuntimeStatus.ERROR, text="missing async binding"),
        )

    def resolve_llm_tool_name(self, name: object) -> str:
        raw = str(name or "")
        local = self.runtime.resolve_llm_tool_name(raw)
        if local != raw or self.runtime.has_registered_capability(local):
            return local
        resolver = getattr(self.delegate, "resolve_llm_tool_name", None)
        return str(resolver(raw) if callable(resolver) else raw)

    def list_capability_specs(self) -> list[dict[str, Any]]:
        values = list(getattr(self.delegate, "list_capability_specs", lambda: [])() or [])
        known = {str(item.get("canonical_path") or item.get("name") or "") for item in values if isinstance(item, dict)}
        values.extend(
            item for item in self.runtime.list_capability_specs() if str(item.get("canonical_path") or item.get("name") or "") not in known
        )
        return [dict(item) for item in values if isinstance(item, dict)]

    def get_capability_spec(self, name: str) -> dict[str, Any] | None:
        local = self.runtime.get_capability_spec(name)
        if local is not None:
            return dict(local)
        getter = getattr(self.delegate, "get_capability_spec", None)
        value = getter(name) if callable(getter) else None
        return dict(value) if isinstance(value, dict) else None

    async def execute_tool_async(self, call: CanonicalToolCall, **kwargs: Any) -> CanonicalToolResult:
        canonical = self.runtime.resolve_llm_tool_name(call.name)
        if self.runtime.compiled_capability_index.by_canonical.get(canonical):
            return await self.runtime.execute_tool_async(
                CanonicalToolCall(name=canonical, args=dict(call.args or {}), call_id=call.call_id),
                allow_tools=bool(kwargs.get("allow_tools", True)),
                turn_id=kwargs.get("turn_id"),
            )
        execute = getattr(self.delegate, "execute_tool_async", None)
        if callable(execute):
            return await execute(call, **_supported_kwargs(execute, kwargs))
        return _error_result(call, "unknown tool", "unknown_tool")

    def begin_tool_result_turn(self, **kwargs: Any) -> None:
        begin = getattr(self.delegate, "begin_tool_result_turn", None)
        if callable(begin):
            begin(**kwargs)

    def read_tool_result_page(self, **kwargs: Any) -> Any:
        read = getattr(self.delegate, "read_tool_result_page", None)
        return read(**kwargs) if callable(read) else None


class _OriginalAdapter:
    def __init__(self, owner: "MinionScopedExecutionRuntime") -> None:
        self.owner = owner

    async def execute_tool_async(self, call: CanonicalToolCall, **kwargs: Any) -> CanonicalToolResult:
        return await self.owner._execute_original(call, **kwargs)


@dataclass
class MinionScopedExecutionRuntime:
    base_runtime: Any
    allowed_capabilities: list[str]
    workspace: dict[str, Any] = field(default_factory=dict)
    produced_artifacts: list[dict[str, Any]] = field(default_factory=list)
    memory_l3: Any | None = None
    capability_description_overrides: dict[str, str] = field(default_factory=dict)
    _original_runtime: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._original_runtime = self.base_runtime if callable(getattr(self.base_runtime, "execute_tool_async", None)) else ExecutionRuntime()
        self.base_runtime = _ExecutionOverlay(self._original_runtime)
        self.allowed_capabilities = filter_minion_allowed_capabilities(list(self.allowed_capabilities or []))
        self.capability_description_overrides = {
            str(key).strip(): str(value).strip()
            for key, value in dict(self.capability_description_overrides or {}).items()
            if str(key).strip() and str(value).strip()
        }
        if self.allowed_capabilities and "op_tool_result_page" not in self.allowed_capabilities:
            self.allowed_capabilities.append("op_tool_result_page")
        self._original_adapter = _OriginalAdapter(self)
        self._register_tools()

    def _register_tools(self) -> None:
        allowed = set(self.allowed_capabilities)
        if "op_tool_search" in allowed:
            self.base_runtime.register_tool(ToolSearchTool(runtime=self))
        if "op_tool_read" in allowed:
            self.base_runtime.register_tool(ToolReadTool(runtime=self))
        if "op_tool_result_page" in allowed:
            self.base_runtime.register_tool(ToolResultPageTool(runtime=self))
        if "op_tool_call" in allowed:
            self.base_runtime.register_tool(ToolCallTool(runtime=self))
        for name, spec in _WORKSPACE_TOOL_SPECS.items():
            if name not in allowed or is_minion_capability_denied(name):
                continue
            handler = self._handler(name)
            if handler is not None:
                self.base_runtime.register_tool(_HydratedTool(name=name, spec=spec, handler=handler))

    def _handler(self, name: str) -> Any | None:
        if is_candidate_builder_capability(name):
            return lambda call, ctx: candidate_builder_tool_result(
                call,
                self.workspace,
                self.produced_artifacts,
                original_adapter=self._original_adapter,
                turn_id=str(ctx.get("turn_id") or "") or None,
            )
        if is_skeleton_builder_capability(name):
            return lambda call, _ctx: skeleton_builder_tool_result(call, self.workspace, self.produced_artifacts)
        if is_verification_builder_capability(name):
            return lambda call, ctx: verification_builder_tool_result(
                call,
                self.workspace,
                self.produced_artifacts,
                original_adapter=self._original_adapter,
                turn_id=str(ctx.get("turn_id") or "") or None,
            )
        if is_contract_builder_capability(name):
            return lambda call, _ctx: contract_builder_tool_result(call, self.workspace, self.produced_artifacts)
        if name == "op_minion_input_read":
            return lambda call, ctx: bound_input_tool_result(
                call,
                self.workspace,
                self._original_adapter,
                allow_tools=bool(ctx.get("allow_tools", True)),
                budget=ctx.get("budget"),
                turn_id=str(ctx.get("turn_id") or "") or None,
            )
        if name in WORKSPACE_FILE_TOOL_SPECS:
            return lambda call, ctx: workspace_file_tool_result(
                call,
                self.workspace,
                self._original_adapter,
                allow_tools=bool(ctx.get("allow_tools", True)),
                budget=ctx.get("budget"),
                turn_id=str(ctx.get("turn_id") or "") or None,
            )
        if name in {"op_tree", "op_search", "op_minion_artifact_write", "op_minion_artifact_edit"}:
            return lambda call, _ctx: asyncio.to_thread(_workspace_tool_result, call, self.workspace)
        return None

    async def _execute_original(self, call: CanonicalToolCall, **kwargs: Any) -> CanonicalToolResult:
        execute = getattr(self._original_runtime, "execute_tool_async", None)
        if callable(execute):
            return await execute(call, **_supported_kwargs(execute, kwargs))
        return _error_result(call, "unknown tool", "unknown_tool")

    def begin_tool_result_turn(self, **kwargs: Any) -> None:
        self.base_runtime.begin_tool_result_turn(**kwargs)

    def read_tool_result_page(self, **kwargs: Any) -> Any:
        return self.base_runtime.read_tool_result_page(**kwargs)

    def resolve_llm_tool_name(self, name: object) -> str:
        return self.base_runtime.resolve_llm_tool_name(name)

    def list_capability_specs(self) -> list[dict[str, Any]]:
        allowed = set(self.allowed_capabilities)
        result = []
        for spec in self.base_runtime.list_capability_specs():
            canonical = str(spec.get("canonical_path") or spec.get("name") or "")
            if canonical in allowed and not is_minion_capability_denied(canonical):
                result.append(_scrub_spec(spec, self.capability_description_overrides))
        return result

    def get_capability_spec(self, name: str) -> dict[str, Any] | None:
        canonical = self.resolve_llm_tool_name(name)
        if canonical not in set(self.allowed_capabilities) or is_minion_capability_denied(canonical):
            return None
        spec = self.base_runtime.get_capability_spec(canonical)
        return _scrub_spec(spec, self.capability_description_overrides) if spec else None

    async def execute_tool_async(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        budget: Any = None,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
        admission = admit_minion_tool_call(
            call,
            self.allowed_capabilities,
            resolve_name=self.resolve_llm_tool_name,
            require_effective_target=False,
        )
        if not admission.ok:
            return admission.to_result()
        if not allow_tools:
            return _error_result(admission.call, "tool execution disabled in finalization mode", "finalization_only")
        result = await self.base_runtime.execute_tool_async(
            admission.call,
            allow_tools=True,
            budget=budget,
            turn_id=turn_id,
        )
        if admission.call.name in {"op_minion_artifact_write", "op_minion_artifact_edit"} and result.ok:
            artifact = dict((result.structured or {}).get("artifact") or {})
            if artifact:
                _append_unique_artifact(self.produced_artifacts, artifact)
        return result


def _supported_kwargs(callable_obj: Any, values: dict[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(callable_obj)
    except (TypeError, ValueError):
        return dict(values)
    if any(item.kind == inspect.Parameter.VAR_KEYWORD for item in signature.parameters.values()):
        return dict(values)
    return {key: value for key, value in values.items() if key in signature.parameters}


def _scrub_spec(spec: dict[str, Any], description_overrides: dict[str, str] | None = None) -> dict[str, Any]:
    value = dict(spec)
    canonical = str(value.get("canonical_path") or value.get("name") or "")
    value["canonical_path"] = canonical
    value["name"] = llm_tool_name(value.get("name") or canonical)
    override = str(dict(description_overrides or {}).get(canonical) or "").strip()
    value["description"] = replace_internal_tool_names(
        _replace_worker_internal_tool_names(override or value.get("description") or canonical)
    )
    for key in ("parameters_schema", "result_schema"):
        if key in value:
            value[key] = replace_internal_tool_names_in_value(
                _replace_worker_internal_tool_names_in_value(dict(value.get(key) or {}))
            )
    return value


_WORKER_INTERNAL_TOOL_RE = re.compile(r"\bop_minion_([A-Za-z0-9_]+)\b")


def _replace_worker_internal_tool_names(value: object) -> str:
    return _WORKER_INTERNAL_TOOL_RE.sub(
        lambda match: llm_tool_name(f"op_{match.group(1)}"),
        str(value or ""),
    )


def _replace_worker_internal_tool_names_in_value(value: object) -> object:
    if isinstance(value, str):
        return _replace_worker_internal_tool_names(value)
    if isinstance(value, dict):
        return {key: _replace_worker_internal_tool_names_in_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_replace_worker_internal_tool_names_in_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_worker_internal_tool_names_in_value(item) for item in value)
    return value


def _error_result(call: CanonicalToolCall, text: str, reason: str) -> CanonicalToolResult:
    return CanonicalToolResult(
        name=call.name,
        ok=False,
        text=text,
        structured={"reason": reason},
        call_id=call.call_id,
        llm_text=text,
        status=RuntimeStatus.ERROR,
    )


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _effective_capability_name(tool_call: CanonicalToolCall) -> str:
    return effective_minion_capability_name(tool_call)


def _effective_tool_args(tool_call: CanonicalToolCall) -> dict[str, Any]:
    return effective_minion_tool_args(tool_call)


def _review_tool_evidence_ref(
    target_name: str,
    tool_call: CanonicalToolCall,
    result: CanonicalToolResult,
) -> dict[str, Any]:
    if not str(target_name).startswith(("op_exec_shell", "op_git", "op_lsp_")):
        return {}
    return {
        "evidence_ref_id": f"tev_{uuid4().hex[:12]}",
        "kind": "lsp" if str(target_name).startswith("op_lsp_") else "command",
        "tool_name": str(target_name),
        "call_id": str(tool_call.call_id or ""),
        "ok": bool(result.ok),
        "status": str(result.status or ""),
        "args": _effective_tool_args(tool_call),
        "summary": str(result.text or result.llm_text or "")[:500],
    }
