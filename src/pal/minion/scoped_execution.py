from __future__ import annotations

from pal.execution.generated_tool_models import (
    ExecutionShellExecShellExecCapabilityMixinShellInput,
    MinionScopedExecutionOpMinionArtifactWriteInput,
)

import asyncio
import hashlib
import inspect
import json
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from uuid import uuid4

from pal.execution.runtime import ExecutionRuntime
from pydantic import Field, create_model
from pal.execution.tool_facade import (
    CompleteResult,
    EffectKind,
    EffectOutcome,
    EffectReceipt,
    FailedResult,
    Idempotency,
    InvocationMode,
    PagingMode,
    PagedResult,
    RejectedResult,
    RetryPolicy,
    StrictToolModel,
    Tool,
    ToolExecutionSemantics,
    ToolGuidance,
    ToolHandlerResult,
    ToolInvocationResult,
)
from pal.execution.tool_registry import _example_from_schema
from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.profiles import filter_minion_allowed_capabilities, is_minion_capability_denied
from pal.minion.tool_guidance import (
    minion_tool_guidance,
    normalize_tool_guidance_overrides,
)
from pal.minion.tool_admission import (
    admit_minion_tool_call,
    effective_minion_capability_name,
    effective_minion_tool_args,
)
from pal.minion.v2.contract_builder import (
    CONTRACT_BUILDER_TOOL_SPECS,
    contract_builder_tool_result,
    is_contract_builder_capability,
)
from pal.minion.v2.candidate_builder import (
    CANDIDATE_BUILDER_TOOL_SPECS,
    candidate_builder_tool_result,
    is_candidate_builder_capability,
)
from pal.minion.v2.skeleton_builder import (
    SKELETON_BUILDER_TOOL_SPECS,
    ask_question_tool_result,
    is_skeleton_builder_capability,
    skeleton_builder_tool_result,
)
from pal.minion.v2.review_findings import (
    ADD_FINDING_CAPABILITY,
    ADD_FINDING_TOOL_SPEC,
    add_finding_tool_result,
    is_review_finding_capability,
)
from pal.minion.v2.swe_verification import (
    SWE_VERIFICATION_TOOL_SPECS,
    is_swe_verification_capability,
    swe_verification_tool_result,
)
from pal.minion.v2.verification_builder import (
    VERIFICATION_BUILDER_TOOL_SPECS,
    is_verification_builder_capability,
    verification_builder_tool_result,
)
from pal.minion.workspace_tools import _append_unique_artifact, _workspace_tool_result
from pal.shared import (
    MountedSubtreeHandle,
    RuntimeStatus,
    default_tool_result_text,
)


class MinionScopedExecutionOpMinionArtifactEditInput(StrictToolModel):
    """Reloadable contract matching the Minion-owned artifact edit handler."""

    relative_path: str
    content: Any
    operation: Literal["append", "replace"] = "append"
    create_if_missing: bool = True
    title: str | None = None
    role: str | None = None
    mime_type: str | None = None


class MinionScopedExecutionShellInput(
    ExecutionShellExecShellExecCapabilityMixinShellInput
):
    cmd: str = Field(
        ...,
        description=(
            "Task-scoped shell command for bounded discovery, tests, builds, "
            "scripts, process probes, and ordinary file operations inside the "
            "assigned worktree."
        ),
    )


_WORKSPACE_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_minion_artifact_write": {
        "alias": "artifact_write",
        "description": "Write the profile-declared structured output artifact. Architecture roles must use their bound Contract Builder instead.",
        "InputModel": MinionScopedExecutionOpMinionArtifactWriteInput,
    },
    "op_minion_artifact_edit": {
        "alias": "artifact_edit",
        "description": (
            "Append to or replace one existing profile output artifact. Supply relative_path, "
            "the complete content to write, and operation=append|replace; set "
            "create_if_missing=false when absence must be rejected. This tool does not accept "
            "old_string/new_string exact-replacement arguments."
        ),
        "InputModel": MinionScopedExecutionOpMinionArtifactEditInput,
        "examples": (
            {
                "relative_path": "report.md",
                "content": "# Report\n",
                "operation": "replace",
                "create_if_missing": False,
            },
        ),
    },
    **CONTRACT_BUILDER_TOOL_SPECS,
    **CANDIDATE_BUILDER_TOOL_SPECS,
    **SKELETON_BUILDER_TOOL_SPECS,
    **VERIFICATION_BUILDER_TOOL_SPECS,
    ADD_FINDING_CAPABILITY: ADD_FINDING_TOOL_SPEC,
}

_WORKSPACE_TOOL_SPECS.update(SWE_VERIFICATION_TOOL_SPECS)


class _WorkflowToolOutput(StrictToolModel):
    payload: dict[str, Any]


def _scoped_workspace_tool_spec(
    name: str,
    spec: dict[str, Any],
    *,
    workspace: dict[str, Any],
) -> dict[str, Any]:
    value = dict(spec)
    if name == "op_minion_architecture_review_pass":
        contract = dict(
            dict(workspace.get("minion_v2") or {}).get(
                "architecture_review_tool_contract"
            )
            or {}
        )
        example = dict(contract.get("pass_example") or {})
        if example:
            value["examples"] = (example,)
            value["InputModel"].model_validate(example, strict=True)
        return value
    if name != ADD_FINDING_CAPABILITY:
        return value
    role = str(dict(workspace.get("minion_v2") or {}).get("role") or "")
    mode = str(dict(workspace.get("minion_v2") or {}).get("mode") or "")
    examples = tuple(dict(item) for item in list(value.get("examples") or []))
    if role == "reviewer" and mode == "architecture":
        base_model = value["InputModel"]
        fields = {
            field_name: (
                Literal[
                    "requirements_defect",
                    "contract_defect",
                    "architecture_defect",
                ]
                if field_name == "finding_kind"
                else model_field.annotation,
                model_field,
            )
            for field_name, model_field in base_model.model_fields.items()
        }
        value["InputModel"] = create_model(
            "ScopedArchitectureReviewAddFindingInput",
            __base__=StrictToolModel,
            **fields,
        )
        value["examples"] = examples[:1]
    elif examples:
        value["examples"] = examples[1:2]
    for example in tuple(value.get("examples") or ()):
        value["InputModel"].model_validate(example, strict=True)
    return value


def _immutable_workflow_tool(
    *,
    name: str,
    spec: dict[str, Any],
    handler: Any,
    guidance_patch: dict[str, str] | None = None,
) -> Tool:
    input_model = spec.get("InputModel")
    if not isinstance(input_model, type) or not issubclass(input_model, StrictToolModel):
        raise TypeError(f"workflow tool {name!r} requires a strict Pydantic InputModel")
    alias = str(spec.get("alias") or "").strip()
    if not alias:
        raise ValueError(f"workflow tool {name!r} must declare exactly one non-empty alias")
    purpose = str(spec.get("description") or name).strip()
    effect_kind = _workflow_effect_kind(name)
    mutating = effect_kind in {EffectKind.LOCAL_WRITE, EffectKind.EXTERNAL_WRITE, EffectKind.CONTROL}
    idempotency = Idempotency(
        str(
            spec.get("idempotency")
            or (Idempotency.NON_IDEMPOTENT.value if mutating else Idempotency.IDEMPOTENT.value)
        )
    )
    retry_policy = RetryPolicy(
        str(
            spec.get("retry_policy")
            or (RetryPolicy.RECONCILE_FIRST.value if mutating else RetryPolicy.AUTOMATIC.value)
        )
    )

    async def invoke(value: Any, **kwargs: Any) -> ToolHandlerResult | ToolInvocationResult:
        args = value.model_dump(mode="python", exclude_none=True)
        call = CanonicalToolCall(
            name=name,
            args=args,
            call_id=getattr(kwargs.get("tool_call"), "call_id", None),
        )
        result = handler(call, kwargs)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, CanonicalToolResult):
            if isinstance(
                result.invocation_result,
                (CompleteResult, PagedResult, RejectedResult, FailedResult),
            ):
                return result.invocation_result
            if not result.ok:
                raise RuntimeError(result.llm_text or result.text or "workflow tool failed")
            payload = dict(result.structured or {"text": result.text})
            llm_text = default_tool_result_text(result, fallback_ok="tool completed", fallback_error="tool failed")
        else:
            payload = {"value": result}
            llm_text = str(result or "tool completed")
        receipt = (
            None
            if effect_kind is EffectKind.NONE
            else EffectReceipt(outcome=EffectOutcome.APPLIED, receipt={"workflow_handler_completed": True})
        )
        return ToolHandlerResult(
            output={"payload": payload},
            llm_text=llm_text,
            effect_receipt=receipt,
        )

    base_guidance = ToolGuidance(
        purpose=purpose,
        use_when=purpose,
        do_not_use_when="Do not use outside your current assigned task and role.",
        failure_next_steps="Correct invalid input; for execution failures inspect the recovery affordance before retrying.",
    )
    return Tool(
        alias=alias,
        canonical_path=name,
        InputModel=input_model,
        OutputModel=_WorkflowToolOutput,
        guidance=minion_tool_guidance(
            name,
            base_guidance,
            guidance_patch,
        ),
        execution=ToolExecutionSemantics(
            invocation_mode=InvocationMode.DIRECT,
            effect_kind=effect_kind,
            idempotency=idempotency,
            retry_policy=retry_policy,
            paging=PagingMode.SUPPORTED,
        ),
        search_text=f"{alias} {purpose}",
        handler=invoke,
        examples=(
            tuple(dict(item) for item in list(spec.get("examples") or []))
            or (
                (_example_from_schema(input_model.model_json_schema(mode="validation")),)
                if input_model.model_json_schema(mode="validation").get("properties")
                else ()
            )
        ),
        module_id="workflow_scoped",
        family="workflow",
        source="workflow:scoped-worker",
    )


def _workflow_effect_kind(name: str) -> EffectKind:
    lowered = name.lower()
    if any(
        token in lowered
        for token in (
            "_add_",
            "_write",
            "_edit",
            "_commit",
            "_submit",
            "_delete",
            "_update",
            "_remove_",
            "_record_",
            "_report_",
            "_set_",
        )
    ):
        return EffectKind.LOCAL_WRITE
    if any(token in lowered for token in ("_ask_question", "_cancel", "_approve")):
        return EffectKind.CONTROL
    if lowered in {"op_web_search", "op_web_read", "op_memory_recall"}:
        return EffectKind.EXTERNAL_READ
    return EffectKind.LOCAL_READ


class _ExecutionOverlay:
    def __init__(
        self,
        delegate: Any,
        allowed_capabilities: list[str],
        *,
        guidance_overrides: dict[str, dict[str, str]],
    ) -> None:
        self.delegate = delegate
        self.runtime = ExecutionRuntime(
            runtime_root=getattr(delegate, "runtime_root", None),
            sync_executor=getattr(delegate, "sync_executor", None),
        )
        self._mount_allowed_generation(
            allowed_capabilities,
            guidance_overrides=guidance_overrides,
        )

    def _mount_allowed_generation(
        self,
        allowed_capabilities: list[str],
        *,
        guidance_overrides: dict[str, dict[str, str]],
    ) -> None:
        generation = getattr(self.delegate, "registry_generation", None)
        if generation is None:
            return
        allowed = set(allowed_capabilities)
        subtree = MountedSubtreeHandle(module_id="minion_allowed")
        registered_tools: set[str] = set()
        for record in (*generation.direct_aliases.values(), *generation.indirect_aliases.values()):
            if record.canonical_path not in allowed:
                continue
            # Role-native tools deliberately replace the main runtime
            # implementation with a workflow-aware handler.  Do not
            # also copy the inherited descriptor into this generation: the
            # immutable replacement is registered below as one whole Tool.
            if record.canonical_path in _WORKSPACE_TOOL_SPECS:
                continue
            if record.facade_tool is not None:
                if record.canonical_path not in registered_tools:
                    facade_tool = record.facade_tool
                    self.runtime.register_tool(
                        replace(
                            facade_tool,
                            InputModel=(
                                MinionScopedExecutionShellInput
                                if record.canonical_path == "op_exec_shell"
                                else facade_tool.InputModel
                            ),
                            guidance=minion_tool_guidance(
                                record.canonical_path,
                                facade_tool.guidance,
                                guidance_overrides.get(record.canonical_path),
                            ),
                        )
                    )
                    registered_tools.add(record.canonical_path)
                continue
            descriptor = _scope_descriptor(
                record.binding.descriptor,
                guidance_overrides=guidance_overrides,
            )
            binding = replace(record.binding, descriptor=descriptor)
            subtree.descriptors.append(descriptor)
            subtree.bound_actions.append(binding)
            subtree.bound_action_keys.append((record.binding.canonical_path, record.binding.target_id))
            subtree.search_record_ids.append(record.binding.descriptor.name)
        if subtree.descriptors:
            self.runtime.mount_subtree(SimpleNamespace(mounted_subtree=subtree))

    def register_tool(self, tool: Any) -> None:
        if not isinstance(tool, Tool):
            raise TypeError("scoped execution accepts only immutable Tool registrations")
        self.runtime.register_tool(tool)

    def resolve_capability_address(self, name: object) -> str:
        raw = str(name or "")
        local = self.runtime.resolve_capability_address(raw)
        if local != raw or self.runtime.has_registered_capability(local):
            return local
        resolver = getattr(self.delegate, "resolve_capability_address", None)
        return str(resolver(raw) if callable(resolver) else raw)

    def list_capability_specs(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self.runtime.list_capability_specs()]

    def get_capability_spec(self, name: str) -> dict[str, Any] | None:
        value = self.runtime.get_capability_spec(name)
        return dict(value) if isinstance(value, dict) else None

    async def execute_tool_async(self, call: CanonicalToolCall, **kwargs: Any) -> CanonicalToolResult:
        canonical = self.runtime.resolve_capability_address(call.name)
        if self.runtime.compiled_capability_index.by_canonical.get(canonical):
            facade_call = _manager_call_to_facade(
                self.runtime,
                CanonicalToolCall(name=canonical, args=dict(call.args or {}), call_id=call.call_id),
            )
            return await self.runtime.execute_tool_async(
                facade_call,
                allow_tools=bool(kwargs.get("allow_tools", True)),
                budget=kwargs.get("budget"),
                turn_id=kwargs.get("turn_id"),
            )
        execute = getattr(self.delegate, "execute_tool_async", None)
        if callable(execute):
            facade_call = _manager_call_to_facade(
                self.delegate,
                CanonicalToolCall(name=canonical, args=dict(call.args or {}), call_id=call.call_id),
            )
            return await execute(facade_call, **_supported_kwargs(execute, kwargs))
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
    capability_guidance_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    request_user_clarification: Any | None = None
    _original_runtime: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._original_runtime = self.base_runtime if callable(getattr(self.base_runtime, "execute_tool_async", None)) else ExecutionRuntime()
        self.allowed_capabilities = filter_minion_allowed_capabilities(list(self.allowed_capabilities or []))
        if self.allowed_capabilities and "op_tool_result_page" not in self.allowed_capabilities:
            self.allowed_capabilities.append("op_tool_result_page")
        self.capability_guidance_overrides = normalize_tool_guidance_overrides(
            self.capability_guidance_overrides
        )
        self.base_runtime = _ExecutionOverlay(
            self._original_runtime,
            self.allowed_capabilities,
            guidance_overrides=self.capability_guidance_overrides,
        )
        self._original_adapter = _OriginalAdapter(self)
        self._register_tools()

    def _register_tools(self) -> None:
        allowed = set(self.allowed_capabilities)
        for name, raw_spec in _WORKSPACE_TOOL_SPECS.items():
            if name not in allowed or is_minion_capability_denied(name):
                continue
            spec = _scoped_workspace_tool_spec(name, raw_spec, workspace=self.workspace)
            handler = self._handler(name)
            if handler is not None:
                self.base_runtime.register_tool(
                    _immutable_workflow_tool(
                        name=name,
                        spec=spec,
                        handler=handler,
                        guidance_patch=self.capability_guidance_overrides.get(name),
                    )
                )

    @property
    def registry_generation(self):
        return self.base_runtime.runtime.registry_generation

    def project_llm_text(self, value: object) -> str:
        return self.registry_generation.project_llm_text(value)

    def project_llm_value(self, value: Any) -> Any:
        return self.registry_generation.project_llm_value(value)

    def build_llm_tool_contracts(self) -> list[dict[str, Any]]:
        generation = self.registry_generation
        return [dict(generation.provider_specs[alias]) for alias in sorted(generation.provider_specs)]

    def _handler(self, name: str) -> Any | None:
        if is_review_finding_capability(name):
            return lambda call, _ctx: add_finding_tool_result(call, self.workspace)
        if is_swe_verification_capability(name):
            return lambda call, _ctx: swe_verification_tool_result(
                call,
                self.workspace,
                self.produced_artifacts,
            )
        if is_candidate_builder_capability(name):
            return lambda call, _ctx: candidate_builder_tool_result(
                call,
                self.workspace,
                self.produced_artifacts,
            )
        if is_skeleton_builder_capability(name):
            if name == "op_minion_ask_question":
                return lambda call, _ctx: ask_question_tool_result(
                    call,
                    self.workspace,
                    self.produced_artifacts,
                    request_user=self.request_user_clarification,
                )
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
        if name in {"op_minion_artifact_write", "op_minion_artifact_edit"}:
            return lambda call, _ctx: asyncio.to_thread(_workspace_tool_result, call, self.workspace)
        return None

    async def _execute_original(self, call: CanonicalToolCall, **kwargs: Any) -> CanonicalToolResult:
        execute = getattr(self._original_runtime, "execute_tool_async", None)
        if callable(execute):
            facade_call = _manager_call_to_facade(self._original_runtime, call)
            return await execute(facade_call, **_supported_kwargs(execute, kwargs))
        return _error_result(call, "unknown tool", "unknown_tool")

    def begin_tool_result_turn(self, **kwargs: Any) -> None:
        self.base_runtime.begin_tool_result_turn(**kwargs)

    def read_tool_result_page(self, **kwargs: Any) -> Any:
        return self.base_runtime.read_tool_result_page(**kwargs)

    def resolve_capability_address(self, name: object) -> str:
        return self.base_runtime.resolve_capability_address(name)

    def list_capability_specs(self) -> list[dict[str, Any]]:
        allowed = set(self.allowed_capabilities)
        result = []
        for spec in self.base_runtime.list_capability_specs():
            canonical = str(spec.get("canonical_path") or spec.get("name") or "")
            if canonical in allowed and not is_minion_capability_denied(canonical):
                result.append(
                    _scrub_spec(spec)
                )
        return result

    def get_capability_spec(self, name: str) -> dict[str, Any] | None:
        canonical = self.resolve_capability_address(name)
        if canonical not in set(self.allowed_capabilities) or is_minion_capability_denied(canonical):
            return None
        spec = self.base_runtime.get_capability_spec(canonical)
        return (
            _scrub_spec(spec)
            if spec
            else None
        )

    async def execute_tool_async(
        self,
        call: CanonicalToolCall,
        *,
        allow_tools: bool = True,
        budget: Any = None,
        turn_id: str | None = None,
    ) -> CanonicalToolResult:
        raw_name = str(call.name or "").strip()
        if raw_name.startswith(("op_", "intro_")):
            return _error_result(
                call,
                "unknown tool alias; use the exact scoped alias from the current tool contracts",
                "unknown_tool",
            )
        admission = admit_minion_tool_call(
            call,
            self.allowed_capabilities,
            resolve_name=self.resolve_capability_address,
            require_effective_target=False,
        )
        if not admission.ok:
            return admission.to_result()
        if not allow_tools:
            return _error_result(admission.call, "tool execution disabled in finalization mode", "finalization_only")
        guarded_call, guard_error = _guard_scoped_workspace_mutation(
            admission.call,
            target_name=admission.target_name,
            workspace=self.workspace,
        )
        if guard_error is not None:
            return guard_error
        result = await self.base_runtime.execute_tool_async(
            guarded_call,
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


_SCOPED_FILE_MUTATIONS = frozenset(
    {
        "op_file_edit",
        "op_file_write",
        "op_path_delete",
    }
)


def _guard_scoped_workspace_mutation(
    call: CanonicalToolCall,
    *,
    target_name: str,
    workspace: dict[str, Any],
) -> tuple[CanonicalToolCall, CanonicalToolResult | None]:
    """Reject host-side file mutations outside the role's compiled write set.

    Shell commands run inside bubblewrap and receive read-only overlays there.
    File tools execute through the host runtime, so they need the same policy at
    the facade boundary instead of discovering an illegal edit during candidate
    snapshotting.
    """

    if target_name not in _SCOPED_FILE_MUTATIONS:
        return call, None
    repo_raw = str(
        workspace.get("repo_path")
        or workspace.get("workspace_path")
        or workspace.get("task_repo_path")
        or workspace.get("target_repo_path")
        or ""
    ).strip()
    args = effective_minion_tool_args(call)
    raw_path = str(args.get("file_path") or "").strip()
    if not repo_raw or not raw_path:
        return call, None
    has_compiled_write_scopes = "write_path_scopes" in workspace
    has_read_only_overlays = "read_only_overlay_paths" in workspace
    if not has_compiled_write_scopes and not has_read_only_overlays:
        return call, None
    repo = Path(repo_raw).expanduser().resolve()
    candidate = Path(raw_path).expanduser()
    target = (
        candidate.resolve()
        if candidate.is_absolute()
        else (repo / candidate).resolve()
    )
    try:
        relative = target.relative_to(repo).as_posix()
    except ValueError:
        return call, _path_not_writable_result(
            call,
            raw_path=raw_path,
            relative_path="",
            reason="path_outside_workspace",
        )
    overlays = [
        str(item or "").replace("\\", "/").strip("/")
        for item in list(workspace.get("read_only_overlay_paths") or [])
        if str(item or "").strip()
    ]
    if any(
        relative == overlay or relative.startswith(overlay + "/")
        for overlay in overlays
    ):
        return call, _path_not_writable_result(
            call,
            raw_path=raw_path,
            relative_path=relative,
            reason="read_only_overlay",
        )
    scopes = [
        dict(item or {})
        for item in list(workspace.get("write_path_scopes") or [])
        if isinstance(item, dict)
    ]
    if has_compiled_write_scopes and not any(
        _workspace_scope_matches(relative, scope) for scope in scopes
    ):
        return call, _path_not_writable_result(
            call,
            raw_path=raw_path,
            relative_path=relative,
            reason="outside_write_scope",
        )
    args["file_path"] = str(target)
    return _tool_call_with_effective_args(call, args), None


def _workspace_scope_matches(path: str, scope: dict[str, Any]) -> bool:
    target = str(scope.get("path") or "").replace("\\", "/").strip("/")
    kind = str(scope.get("kind") or "").strip().lower()
    if not target:
        return False
    if kind == "file":
        return path == target
    if kind == "directory":
        return path == target or path.startswith(target + "/")
    return False


def _tool_call_with_effective_args(
    call: CanonicalToolCall,
    args: dict[str, Any],
) -> CanonicalToolCall:
    if call.name == "op_tool_call":
        outer = dict(call.args or {})
        outer["args"] = args
        return CanonicalToolCall(
            name=call.name,
            args=outer,
            call_id=call.call_id,
        )
    return CanonicalToolCall(name=call.name, args=args, call_id=call.call_id)


def _path_not_writable_result(
    call: CanonicalToolCall,
    *,
    raw_path: str,
    relative_path: str,
    reason: str,
) -> CanonicalToolResult:
    display = relative_path or raw_path
    text = (
        f"path is not writable in this role: {display}. "
        "Change only the paths in the bound write scope; verifier-owned corpus "
        "must be repaired by its Verifier."
    )
    return CanonicalToolResult(
        name=call.name,
        ok=False,
        text=text,
        structured={
            "reason": "path_not_writable",
            "policy_reason": reason,
            "file_path": raw_path,
            "workspace_path": relative_path,
        },
        call_id=call.call_id,
        llm_text=text,
        status=RuntimeStatus.FORBIDDEN,
    )


def _scope_descriptor(
    descriptor: Any,
    *,
    guidance_overrides: dict[str, dict[str, str]],
) -> Any:
    canonical = str(descriptor.canonical_path or descriptor.name)
    guidance = descriptor.guidance
    if guidance is not None:
        guidance = minion_tool_guidance(
            canonical,
            guidance,
            guidance_overrides.get(canonical),
        )
    purpose = str(guidance.purpose if guidance is not None else descriptor.description).strip()
    return replace(
        descriptor,
        description=purpose,
        guidance=guidance,
    )


def _manager_call_to_facade(runtime: Any, call: CanonicalToolCall) -> CanonicalToolCall:
    """Translate Manager-internal canonical addressing at the facade boundary.

    Minion policy and workspace handlers intentionally keep canonical paths as
    stable internal capability identities.  The execution facade never accepts
    those identities, so this boundary captures one generation, selects its
    single alias, and uses ``call_tool`` for an indirect record.
    """

    generation = getattr(runtime, "registry_generation", None)
    if generation is None:
        return call
    exact = generation.record_for_alias(str(call.name or ""))
    if exact is not None:
        return call
    canonical = str(call.name or "").strip()
    target_id = str(dict(call.args or {}).get("target_id") or "__singleton__")
    matches = [
        record
        for record in (*generation.direct_aliases.values(), *generation.indirect_aliases.values())
        if record.canonical_path == canonical and record.target_id == target_id
    ]
    if len(matches) != 1 and target_id == "__singleton__":
        matches = [
            record
            for record in (*generation.direct_aliases.values(), *generation.indirect_aliases.values())
            if record.canonical_path == canonical
        ]
    if len(matches) != 1:
        return call
    record = matches[0]
    alias_call = CanonicalToolCall(
        name=record.alias,
        args=dict(call.args or {}),
        call_id=call.call_id,
    )
    if record.execution.invocation_mode is InvocationMode.DIRECT:
        return alias_call
    return CanonicalToolCall(
        name="call_tool",
        args={"name": record.alias, "args": dict(call.args or {})},
        call_id=call.call_id,
    )


def _scrub_spec(spec: dict[str, Any]) -> dict[str, Any]:
    value = dict(spec)
    canonical = str(value.get("canonical_path") or value.get("name") or "")
    value["canonical_path"] = canonical
    value["name"] = str(value.get("name") or canonical).strip()
    value["description"] = str(value.get("description") or "").strip()
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
    if not (
        str(target_name).startswith(("op_exec_shell", "op_lsp_"))
        or str(target_name) in {
            "op_file_write",
            "op_file_edit",
            "op_minion_verification_scratch_write",
        }
    ):
        return {}
    output_text = str(result.text or result.llm_text or "")
    structured = json.loads(
        json.dumps(dict(result.structured or {}), ensure_ascii=False, default=str)
    )
    args = json.loads(
        json.dumps(_effective_tool_args(tool_call), ensure_ascii=False, default=str)
    )
    encoded = json.dumps(
        {"text": output_text, "structured": structured},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    ).encode("utf-8")
    return {
        "evidence_ref_id": f"tev_{uuid4().hex[:12]}",
        "kind": (
            "test_write"
            if str(target_name) in {
                "op_file_write",
                "op_file_edit",
                "op_minion_verification_scratch_write",
            }
            else "lsp"
            if str(target_name).startswith("op_lsp_")
            else "command"
        ),
        "tool_name": str(target_name),
        "call_id": str(tool_call.call_id or ""),
        "ok": bool(result.ok),
        "status": str(result.status or ""),
        "args": args,
        "summary": output_text[:500],
        "output_sha256": hashlib.sha256(encoded).hexdigest(),
        "output_text": output_text[:65536],
        "structured": structured,
    }
