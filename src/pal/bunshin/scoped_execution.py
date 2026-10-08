from __future__ import annotations
from pal.bunshin.review_receipts import _review_tool_evidence_ref

from pal.shared.tool_protocol import ToolCallIR

from pal.shared.tool_protocol import new_tool_call

from pal.execution.generated_tool_models import (
    ExecutionShellExecShellExecCapabilityMixinShellInput,
    BunshinScopedExecutionOpBunshinArtifactWriteInput,
)

import asyncio
import inspect
import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from weakref import WeakValueDictionary

from pal.execution.runtime import ExecutionRuntime
from pydantic import Field, create_model
from pal.execution.contracts import CapabilityCall, CapabilityDescriptor, CapabilityResult
from pal.execution.tool_facade import (
    CompleteResult,
    EffectKind,
    EffectOutcome,
    EffectReceipt,
    FailedResult,
    Idempotency,
    InvocationMode,
    PagingMode,
    RejectedResult,
    RetryPolicy,
    StrictToolModel,
    ToolExecutionSemantics,
    ToolGuidance,
    ToolHandlerResult,
    ToolInvocationResult,
    rejection,
)
from pal.execution.tool_registry import _example_from_schema
from pal.shared import ToolExecutionResult
from pal.bunshin.profiles import filter_bunshin_allowed_capabilities, is_bunshin_capability_denied
from pal.bunshin.tool_guidance import (
    bunshin_tool_guidance,
    merge_tool_guidance_overrides,
)
from pal.bunshin.tool_admission import (
    admit_bunshin_tool_call,
    effective_bunshin_capability_name,
    effective_bunshin_tool_args,
)
from pal.bunshin.ask_question import (
    ASK_QUESTION_CAPABILITY,
    ASK_QUESTION_TOOL_SPEC,
    ask_question_tool_result,
)
from pal.bunshin.contract_submission import (
    CONTRACT_SUBMIT_CAPABILITY,
    CONTRACT_SUBMIT_TOOL_SPEC,
    contract_submit_tool_result,
)
from pal.bunshin.candidate_builder import (
    CANDIDATE_BUILDER_TOOL_SPECS,
    candidate_builder_tool_result,
    is_candidate_builder_capability,
)
from pal.bunshin.review_findings import (
    ADD_FINDING_CAPABILITY,
    ADD_FINDING_TOOL_SPEC,
    review_finding_tool_result,
    is_review_finding_capability,
)
from pal.bunshin.review_submission import (
    REVIEW_SUBMIT_CAPABILITY,
    REVIEW_SUBMIT_TOOL_SPEC,
    review_submit_tool_result,
)
from pal.bunshin.work_items import (
    REMOVE_FINDING_CAPABILITY,
    REMOVE_FINDING_TOOL_SPEC,
    UPDATE_FINDING_CAPABILITY,
    UPDATE_FINDING_TOOL_SPEC,
    UPDATE_CHECKLIST_CAPABILITY,
    UPDATE_CHECKLIST_TOOL_SPEC,
    update_checklist_tool_result,
)
from pal.bunshin.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
from pal.bunshin.swe_verification import (
    SWE_VERIFICATION_TOOL_SPECS,
    is_swe_verification_capability,
    swe_verification_tool_result,
)
from pal.bunshin.verification_builder import (
    SHELL_EVIDENCE_CAPABILITIES,
    VERIFICATION_BUILDER_TOOL_SPECS,
    is_verification_builder_capability,
    verification_builder_tool_result,
)
from pal.bunshin.workspace_tools import _append_unique_artifact, _workspace_tool_result
from pal.shared import (
    BoundCapabilityAction,
    MountedSubtreeHandle,
    RuntimeStatus,
    SINGLETON_TARGET,
    default_tool_result_text,
)


class BunshinMemoryCandidatesInput(StrictToolModel):
    candidates: list[dict[str, Any]]


class BunshinScopedExecutionOpBunshinArtifactEditInput(StrictToolModel):
    """Reloadable contract matching the Bunshin-owned artifact edit handler."""

    relative_path: str
    content: Any
    operation: Literal["append", "replace"] = "append"
    create_if_missing: bool = True
    title: str | None = None
    role: str | None = None
    mime_type: str | None = None


class BunshinScopedExecutionShellInput(
    ExecutionShellExecShellExecCapabilityMixinShellInput
):
    timeout_ms: int | None = Field(
        180_000,
        ge=1,
        le=600_000,
        description=(
            "Timeout in milliseconds. Defaults to 180000 for builds and tests "
            "on slower task workers and cannot exceed 600000; set a different "
            "value only when the command has a known tighter or longer bound."
        ),
    )


_WORKSPACE_TOOL_SPECS: dict[str, dict[str, Any]] = {
    "op_bunshin_memory_candidate_write": {
        "alias": "propose_memories",
        "guidance": {
            "purpose": "Replace this invocation's memory proposals with at most five most useful durable candidates; zero is allowed.",
            "use_when": 'Submit the complete selected batch. Template: {"candidates":[{"kind":"fact","title":"...","summary":"...","source_excerpt":"...","topics":[],"why_durable":"..."}]}. Cases also require star with situation/task/action/result; preserve evidence and uncertainty.',
            "do_not_use_when": "This is a proposal, not a memory write. The host will ask the user to review each item and explicitly submit the batch.",
            "failure_next_steps": "Correct the reported fields and resubmit the complete batch; never invent missing evidence.",
        },
        "InputModel": BunshinMemoryCandidatesInput,
    },
    "op_bunshin_artifact_write": {
        "alias": "write_workflow_artifact",
        "guidance": {
            "purpose": "Write the profile-declared structured output artifact.",
            "use_when": "The current role must create or replace its declared structured output artifact.",
            "do_not_use_when": "Architect roles must edit the Manager-preseeded architect.yaml instead. Do not write undeclared outputs.",
            "failure_next_steps": "Correct the declared artifact type, relative path, or complete content from the returned validation error.",
        },
        "InputModel": BunshinScopedExecutionOpBunshinArtifactWriteInput,
    },
    "op_bunshin_artifact_edit": {
        "alias": "edit_workflow_artifact",
        "guidance": {
            "purpose": "Append to or replace one existing profile output artifact.",
            "use_when": "Supply relative_path, complete content, and operation=append|replace for a profile-owned output artifact.",
            "do_not_use_when": "Do not use old_string/new_string exact replacement arguments or edit product source through this artifact tool.",
            "failure_next_steps": "Correct the relative path, complete content, or create_if_missing policy from the returned validation error.",
        },
        "InputModel": BunshinScopedExecutionOpBunshinArtifactEditInput,
        "examples": (
            {
                "relative_path": "report.md",
                "content": "# Report\n",
                "operation": "replace",
                "create_if_missing": False,
            },
        ),
    },
    **CANDIDATE_BUILDER_TOOL_SPECS,
    **VERIFICATION_BUILDER_TOOL_SPECS,
    ASK_QUESTION_CAPABILITY: ASK_QUESTION_TOOL_SPEC,
    UPDATE_CHECKLIST_CAPABILITY: UPDATE_CHECKLIST_TOOL_SPEC,
    CONTRACT_SUBMIT_CAPABILITY: CONTRACT_SUBMIT_TOOL_SPEC,
    REVIEW_SUBMIT_CAPABILITY: REVIEW_SUBMIT_TOOL_SPEC,
    ADD_FINDING_CAPABILITY: ADD_FINDING_TOOL_SPEC,
    UPDATE_FINDING_CAPABILITY: UPDATE_FINDING_TOOL_SPEC,
    REMOVE_FINDING_CAPABILITY: REMOVE_FINDING_TOOL_SPEC,
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
    if name != ADD_FINDING_CAPABILITY:
        return value
    role = str(dict(workspace.get("bunshin_v2") or {}).get("role") or "")
    mode = str(dict(workspace.get("bunshin_v2") or {}).get("mode") or "")
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


def _workflow_capability(
    *,
    name: str,
    spec: dict[str, Any],
    handler: Any,
    guidance_patch: dict[str, str] | None = None,
) -> tuple[CapabilityDescriptor, BoundCapabilityAction]:
    input_model = spec.get("InputModel")
    if not isinstance(input_model, type) or not issubclass(input_model, StrictToolModel):
        raise TypeError(f"workflow tool {name!r} requires a strict Pydantic InputModel")
    alias = str(spec.get("alias") or "").strip()
    if not alias:
        raise ValueError(f"workflow tool {name!r} must declare exactly one non-empty alias")
    effect_kind = _WORKFLOW_EFFECTS[name]
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

    async def invoke(call: CapabilityCall) -> ToolHandlerResult | ToolInvocationResult | CapabilityResult:
        meta = dict(call.meta)
        provider_call = meta.get("tool_call")
        tool_call = new_tool_call(
            name=name,
            args=dict(call.args),
            call_id=str(getattr(provider_call, "call_id", "") or "") or None,
        )
        result = handler(tool_call, meta)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, ToolExecutionResult):
            if isinstance(
                result.invocation_result,
                (CompleteResult, RejectedResult, FailedResult),
            ):
                return result.invocation_result
            if not result.ok:
                details = dict(result.structured or {})
                outcome = details.get("effect")
                receipt = (EffectReceipt(outcome=EffectOutcome(outcome))
                           if isinstance(outcome, str) and outcome in {item.value for item in EffectOutcome} else None)
                return CapabilityResult(
                    status=RuntimeStatus.ERROR, text=result.text,
                    llm_text=result.llm_text, structured=details, effect_receipt=receipt,
                    recovery_hint=str(details.get("recovery_hint") or details.get("recovery") or ""),
                    snapshot_refs=result.snapshot_refs, context_messages=result.context_messages,
                )
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

    raw_spec_guidance = spec.get("guidance")
    if isinstance(raw_spec_guidance, ToolGuidance):
        base_guidance = raw_spec_guidance
    else:
        base_guidance = ToolGuidance.model_validate(raw_spec_guidance, strict=True)
    guidance = bunshin_tool_guidance(
        name,
        base_guidance,
        guidance_patch,
    )
    examples = (
        tuple(dict(item) for item in list(spec.get("examples") or []))
        or (
            (_example_from_schema(input_model.model_json_schema(mode="validation")),)
            if input_model.model_json_schema(mode="validation").get("properties")
            else ()
        )
    )
    descriptor = CapabilityDescriptor(
        name=alias,
        canonical_path=name,
        aliases=(alias,),
        InputModel=input_model,
        OutputModel=_WorkflowToolOutput,
        guidance=guidance,
        execution=ToolExecutionSemantics(
            invocation_mode=InvocationMode.DIRECT,
            effect_kind=effect_kind,
            idempotency=idempotency,
            retry_policy=retry_policy,
            paging=PagingMode.SUPPORTED,
        ),
        examples=examples,
        module_id="workflow_scoped",
        family="workflow",
        source="workflow:scoped-worker",
        target_id=SINGLETON_TARGET,
        metadata={
            "namespace": "operation",
            "scope": "workflow",
            "examples_generated": not bool(spec.get("examples")),
            "allow_missing_next_tool_hints": True,
            "scoped_projection": "bunshin",
            "delegates_execution": name in SHELL_EVIDENCE_CAPABILITIES,
        },
    )
    action = BoundCapabilityAction(
        canonical_path=name,
        target_id=SINGLETON_TARGET,
        descriptor=descriptor,
        callable=invoke,
        async_callable=invoke,
    )
    return descriptor, action


_WORKFLOW_READ_CAPABILITIES = frozenset(
    {
        "op_bunshin_verification_draft_status",
    }
)
_WORKFLOW_CONTROL_CAPABILITIES = frozenset(
    {
        "op_bunshin_ask_question",
    }
)
_WORKFLOW_EFFECTS = {
    name: (
        EffectKind.LOCAL_READ
        if name in _WORKFLOW_READ_CAPABILITIES
        else EffectKind.CONTROL
        if name in _WORKFLOW_CONTROL_CAPABILITIES
        else EffectKind.LOCAL_WRITE
    )
    for name in _WORKSPACE_TOOL_SPECS
}

_FINDING_EDIT_CAPABILITIES = (UPDATE_FINDING_CAPABILITY, REMOVE_FINDING_CAPABILITY)
_VERIFIER_AUTHORING_LOCKS: WeakValueDictionary[tuple[str, str, str, int], asyncio.Lock] = WeakValueDictionary()
_VERIFIER_WRITER_GUIDANCE = (
    "Verifier authoring is available only before durable submission. Complete corpus-writing "
    "commands in the foreground before submitting; detached/background writers and external "
    "filesystem edits are outside this invocation's authoring serialization."
)


def _bound_verifier_context(workspace: dict[str, Any]) -> SubmissionDraftContext | None:
    if dict(workspace.get("bunshin_v2") or {}).get("role") != "verifier":
        return None
    try:
        return SubmissionDraftContext.from_workspace(workspace, draft_kind="verification")
    except ValueError:
        return None


def _verifier_authoring_lock(workspace: dict[str, Any]) -> asyncio.Lock:
    """Share foreground authoring across runtime views of one fenced invocation.

    This process-local lock does not exclude external filesystem writers. The
    Manager must still validate the final corpus when it accepts the receipt.
    """
    context = _bound_verifier_context(workspace)
    if context is None:
        return asyncio.Lock()
    key = (str(Path(str(workspace.get("runtime_root") or "")).resolve()),
           context.workflow_id, context.invocation_id, context.fencing_token)
    lock = _VERIFIER_AUTHORING_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _VERIFIER_AUTHORING_LOCKS[key] = lock
    return lock


def _profile_allows_capability(name: str, policy: dict[str, Any]) -> bool:
    # Explicit persisted denials also apply to new internal workflow tools.
    # Their internal allowlist bypasses the generic default prefix exclusions.
    return not (
        is_bunshin_capability_denied(name, capability_policy=policy)
        or any(name.startswith(str(prefix)) for prefix in (policy.get("deny_prefixes") or ()) if prefix)
        or any(str(fragment) in name for fragment in (policy.get("deny_fragments") or ()) if fragment)
    )


def _with_verifier_writer_guidance(descriptor: CapabilityDescriptor) -> CapabilityDescriptor:
    guidance = descriptor.guidance.model_copy(update={
        "do_not_use_when": f"{descriptor.guidance.do_not_use_when} {_VERIFIER_WRITER_GUIDANCE}",
    })
    return replace(descriptor, guidance=guidance)


def _requires_verifier_authoring_lock(runtime: "BunshinScopedExecutionRuntime", name: str) -> bool:
    binding = dict(runtime.workspace.get("bunshin_v2") or {})
    if binding.get("role") != "verifier" or not any(key in binding for key in (
        "workflow_id", "invocation_id", "fencing_token", "lease_resource_key",
        "lease_resource", "authoring_input_fingerprint", "authoring_contract_version",
    )):
        return False
    if name in {ASK_QUESTION_CAPABILITY, "op_bunshin_memory_candidate_write"}:
        return False
    if is_review_finding_capability(name) or name == "op_exec_shell" or name.startswith("op_lsp_"):
        return True
    if runtime.role_execution_sessions is not None and runtime.role_execution_sessions.handles_capability(name):
        return True
    index = runtime.registry_generation.capability_index
    return any(index.records[alias].execution.effect_kind in {
        EffectKind.LOCAL_WRITE, EffectKind.EXTERNAL_WRITE, EffectKind.CONTROL,
    } for alias in index.by_canonical.get(name, ()))


async def _no_cancel_check() -> None:
    pass


def _verifier_execution_delegate(runtime: "BunshinScopedExecutionRuntime", name: str, delegate: Any) -> Any:
    """Await native completion under the outer lock, without scoped reentry."""
    sessions = runtime.role_execution_sessions
    if (_bound_verifier_context(runtime.workspace) is not None and sessions is not None
            and sessions.handles_capability(name)):
        return sessions.execution_delegate(delegate, runtime.check_cancel or _no_cancel_check)
    return delegate


def _capture_legacy_finding_generation(runtime: ExecutionRuntime, subtree: MountedSubtreeHandle) -> Any:
    """Keep strict persisted-call dispatch outside the live discovery surface."""
    handle = SimpleNamespace(mounted_subtree=subtree)
    runtime.mount_subtree(handle)
    generation = runtime.registry_generation
    runtime.unmount_subtree(handle)
    return generation


def _scoped_tool_destination(name: str, workspace: dict[str, Any], primary: Any, legacy: Any) -> Any:
    if name == ADD_FINDING_CAPABILITY and dict(workspace.get("bunshin_v2") or {}).get("role") == "verifier":
        return legacy if _bound_verifier_context(workspace) is not None else None
    return primary


async def _execute_legacy_finding(runtime: "BunshinScopedExecutionRuntime", call: ToolCallIR,
                                  *, budget: Any, turn_id: str) -> ToolExecutionResult:
    alias = str(ADD_FINDING_TOOL_SPEC["alias"])
    provider_call = new_tool_call(name=alias, args=dict(call.args or {}), call_id=call.call_id)
    execution = runtime.base_runtime.runtime
    invocation = await execution.invoke_direct_tool_async(provider_call, allow_tools=True,
        budget=budget, turn_id=turn_id, generation=runtime._legacy_finding_generation)
    return execution._canonical_result_from_invocation(alias, call.call_id, invocation)


class _ExecutionOverlay:
    def __init__(
        self,
        delegate: Any,
        allowed_capabilities: list[str],
        *,
        guidance_overrides: dict[str, dict[str, str]],
        verifier_authoring: bool = False,
    ) -> None:
        self.delegate = delegate
        self.verifier_authoring = verifier_authoring
        self.runtime = ExecutionRuntime(
            runtime_root=getattr(delegate, "runtime_root", None),
            sync_executor=getattr(delegate, "sync_executor", None),
        )
        # The overlay changes only the immutable registry view. Output snapshots,
        # file delivery grants, and the user-turn clock belong to the same
        # logical role session as the delegate runtime.
        delegate_sessions = getattr(delegate, "execution_sessions", None)
        delegate_state = getattr(delegate, "logical_state", None)
        if delegate_sessions is not None:
            self.runtime.execution_sessions = delegate_sessions
            self.runtime.result_snapshots = delegate.result_snapshots
        if delegate_state is not None:
            self.runtime.logical_state = delegate_state
        project = getattr(delegate, "project_execution_view", None)
        if project is not None:
            self.runtime = project(self.runtime)
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
        subtree = MountedSubtreeHandle(module_id="bunshin_allowed")
        for record in (*generation.direct_aliases.values(), *generation.indirect_aliases.values()):
            if record.canonical_path not in allowed:
                continue
            # Role-native tools deliberately replace the main runtime
            # implementation with a workflow-aware handler.  Do not
            # also copy the inherited descriptor into this generation: the
            # immutable replacement is compiled below as one registry record.
            if record.canonical_path in _WORKSPACE_TOOL_SPECS:
                continue
            descriptor = _scope_descriptor(
                record.binding.descriptor,
                guidance_overrides=guidance_overrides,
            )
            descriptor = getattr(self.delegate, "project_role_descriptor", lambda d: d)(descriptor)
            if self.verifier_authoring and (
                record.canonical_path == "op_exec_shell"
                or descriptor.execution.effect_kind in {EffectKind.LOCAL_WRITE, EffectKind.EXTERNAL_WRITE}
            ):
                descriptor = _with_verifier_writer_guidance(descriptor)
            binding = replace(record.binding, descriptor=descriptor)
            subtree.descriptors.append(descriptor)
            subtree.bound_actions.append(binding)
            subtree.bound_action_keys.append((record.binding.canonical_path, record.binding.target_id))
            subtree.search_record_ids.append(record.binding.descriptor.name)
        discovery = {"op_tool_search", "op_tool_read", "op_tool_call"}
        if not any(descriptor.execution.invocation_mode == InvocationMode.INDIRECT
                   for descriptor in subtree.descriptors if descriptor.canonical_path not in discovery):
            subtree.descriptors = [d for d in subtree.descriptors if d.canonical_path not in discovery]
            subtree.bound_actions = [a for a in subtree.bound_actions if a.canonical_path not in discovery]
            subtree.bound_action_keys = [key for key in subtree.bound_action_keys if key[0] not in discovery]
            subtree.search_record_ids = [d.name for d in subtree.descriptors]
        if subtree.descriptors:
            self.runtime.mount_subtree(SimpleNamespace(mounted_subtree=subtree))

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

    async def execute_tool_async(self, call: ToolCallIR, **kwargs: Any) -> ToolExecutionResult:
        canonical = self.runtime.resolve_capability_address(call.name)
        if self.runtime.compiled_capability_index.by_canonical.get(canonical):
            facade_call = _manager_call_to_facade(
                self.runtime,
                new_tool_call(name=canonical, args=dict(call.args or {}), call_id=call.call_id),
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
                new_tool_call(name=canonical, args=dict(call.args or {}), call_id=call.call_id),
            )
            return await execute(facade_call, **_supported_kwargs(execute, kwargs))
        return _error_result(call, "unknown tool", "unknown_tool")

    def begin_tool_result_turn(self, **kwargs: Any) -> None:
        begin = getattr(self.delegate, "begin_tool_result_turn", None)
        if callable(begin):
            begin(**kwargs)


class _OriginalAdapter:
    def __init__(self, owner: "BunshinScopedExecutionRuntime") -> None:
        self.owner = owner

    async def execute_tool_async(self, call: ToolCallIR, **kwargs: Any) -> ToolExecutionResult:
        return await self.owner._execute_original(call, **kwargs)


@dataclass
class BunshinScopedExecutionRuntime:
    base_runtime: Any
    allowed_capabilities: list[str]
    workspace: dict[str, Any] = field(default_factory=dict)
    produced_artifacts: list[dict[str, Any]] = field(default_factory=list)
    capability_guidance_overrides: dict[str, dict[str, str]] = field(default_factory=dict)
    request_user_clarification: Any | None = None
    memory_candidate_sink: Any | None = None
    capability_policy: dict[str, Any] = field(default_factory=dict)
    role_execution_sessions: Any | None = None
    check_cancel: Any | None = None
    _original_runtime: Any = field(default=None, init=False, repr=False)
    _direct_turn_id: str = field(default="", init=False, repr=False)
    _authoring_lock: asyncio.Lock = field(init=False, repr=False)
    _legacy_finding_generation: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self._original_runtime = self.base_runtime if callable(getattr(self.base_runtime, "execute_tool_async", None)) else ExecutionRuntime()
        lifetime_id = str(
            self.workspace.get("invocation_id")
            or self.workspace.get("run_id")
            or ""
        ).strip()
        self._direct_turn_id = f"{lifetime_id}:direct" if lifetime_id else ""
        self._authoring_lock = _verifier_authoring_lock(self.workspace)
        self.allowed_capabilities = filter_bunshin_allowed_capabilities(
            list(self.allowed_capabilities or []), capability_policy=self.capability_policy,
        )
        bound_verifier = _bound_verifier_context(self.workspace) is not None
        legacy_finding_grant = (bound_verifier and ADD_FINDING_CAPABILITY in self.allowed_capabilities
                               and _profile_allows_capability(ADD_FINDING_CAPABILITY, self.capability_policy))
        finding_permissions = [name for name in _FINDING_EDIT_CAPABILITIES
                               if bound_verifier and (name in self.allowed_capabilities or legacy_finding_grant)]
        expand = getattr(self._original_runtime, "role_capabilities", None)
        if expand is not None:
            self.allowed_capabilities = expand(self.allowed_capabilities)
        # Resumed packs retain their immutable allowed list and prompt identity.
        # Hydrate compatible tools only in the consumed runtime projection.
        self.allowed_capabilities = filter_bunshin_allowed_capabilities([
            *[name for name in self.allowed_capabilities if name not in _FINDING_EDIT_CAPABILITIES],
            *finding_permissions,
        ], capability_policy=self.capability_policy)
        self.allowed_capabilities = [name for name in self.allowed_capabilities
                                     if _profile_allows_capability(name, self.capability_policy)]
        if bound_verifier and not legacy_finding_grant:
            self.allowed_capabilities = [name for name in self.allowed_capabilities if name != ADD_FINDING_CAPABILITY]

        verification_contract = dict(
            dict(self.workspace.get("bunshin_v2") or {}).get("verification_tool_contract") or {}
        )
        verification_allowed = {
            str(item) for item in list(verification_contract.get("allowed_capabilities") or [])
        }
        if "allowed_capabilities" in verification_contract:
            # Match the invocation-local guard before mounting either provider
            # contracts or discovery. Role-wide evidence permissions include
            # tools that do not apply to this node. Keep separately governed
            # outcome, checklist, finding, and ordinary execution capabilities.
            self.allowed_capabilities = [
                name for name in self.allowed_capabilities
                if (not is_verification_builder_capability(name)
                    and not (bound_verifier and name == ADD_FINDING_CAPABILITY)) or name in verification_allowed
                or (name in _FINDING_EDIT_CAPABILITIES and ADD_FINDING_CAPABILITY in verification_allowed
                    and str(verification_contract.get("contract_version") or "1") == "1")
            ]
        self.capability_guidance_overrides = merge_tool_guidance_overrides(
            self.capability_guidance_overrides,
            verification_contract.get("guidance_overrides"),
        )
        self.base_runtime = _ExecutionOverlay(
            self._original_runtime,
            self.allowed_capabilities,
            guidance_overrides=self.capability_guidance_overrides,
            verifier_authoring=_bound_verifier_context(self.workspace) is not None,
        )
        self._original_adapter = _OriginalAdapter(self)
        self._mount_scoped_generation()

    def _mount_scoped_generation(self) -> None:
        allowed = set(self.allowed_capabilities)
        subtree = MountedSubtreeHandle(module_id="workflow_scoped")
        legacy = MountedSubtreeHandle(module_id="workflow_scoped_legacy_finding")
        for name, raw_spec in _WORKSPACE_TOOL_SPECS.items():
            if name not in allowed or is_bunshin_capability_denied(name):
                continue
            spec = _scoped_workspace_tool_spec(name, raw_spec, workspace=self.workspace)
            handler = self._handler(name)
            if handler is not None:
                descriptor, action = _workflow_capability(
                    name=name,
                    spec=spec,
                    handler=handler,
                    guidance_patch=self.capability_guidance_overrides.get(name),
                )
                if _bound_verifier_context(self.workspace) is not None and (
                    is_verification_builder_capability(name) or is_swe_verification_capability(name)
                ) and name not in _WORKFLOW_READ_CAPABILITIES:
                    descriptor = _with_verifier_writer_guidance(descriptor)
                    action = replace(action, descriptor=descriptor)
                destination = _scoped_tool_destination(name, self.workspace, subtree, legacy)
                if destination is None:
                    continue
                destination.descriptors.append(descriptor)
                destination.bound_actions.append(action)
                destination.bound_action_keys.append((action.canonical_path, action.target_id))
                destination.search_record_ids.append(descriptor.name)
        if subtree.descriptors:
            self.base_runtime.runtime.mount_subtree(
                SimpleNamespace(mounted_subtree=subtree)
            )
        if legacy.descriptors:
            self._legacy_finding_generation = _capture_legacy_finding_generation(self.base_runtime.runtime, legacy)

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
        if name == "op_bunshin_memory_candidate_write":
            return self._propose_memories
        if is_review_finding_capability(name):
            return lambda call, _ctx: review_finding_tool_result(call, self.workspace)
        if name == UPDATE_CHECKLIST_CAPABILITY:
            return lambda call, _ctx: update_checklist_tool_result(
                call, self.workspace
            )
        if name == CONTRACT_SUBMIT_CAPABILITY:
            return lambda call, _ctx: contract_submit_tool_result(
                call, self.workspace
            )
        if name == REVIEW_SUBMIT_CAPABILITY:
            return lambda call, _ctx: review_submit_tool_result(
                call, self.workspace
            )
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
        if name == ASK_QUESTION_CAPABILITY:
            return lambda call, _ctx: ask_question_tool_result(
                call,
                request_user=self.request_user_clarification,
            )
        if is_verification_builder_capability(name):
            return lambda call, ctx: verification_builder_tool_result(
                call,
                self.workspace,
                self.produced_artifacts,
                original_adapter=self._original_adapter,
                turn_id=str(ctx.get("turn_id") or "") or None,
            )
        if name in {"op_bunshin_artifact_write", "op_bunshin_artifact_edit"}:
            return lambda call, _ctx: asyncio.to_thread(_workspace_tool_result, call, self.workspace)
        return None

    def _propose_memories(self, call, _ctx):
        from pal.memory.proposals import normalize_memory_candidates
        if self.memory_candidate_sink is None:
            return _error_result(call, "Proposal storage is unavailable.", "unavailable")
        candidates, diagnostics = normalize_memory_candidates(call.args.get("candidates"))
        self.memory_candidate_sink.records[:] = [
            {**item, "payload": dict(item.get("star") or {}),
             "search_text": item.get("search_text") or item["source_excerpt"]}
            for item in candidates
        ]
        result = {"status": "proposed", "candidate_count": len(candidates),
            "diagnostics": diagnostics, "requires_host_review": True}
        return ToolExecutionResult(name=call.name, call_id=call.call_id, ok=True,
            text=json.dumps(result), llm_text=json.dumps(result), structured=result)

    async def _execute_original(self, call: ToolCallIR, **kwargs: Any) -> ToolExecutionResult:
        # Semantic execution is awaited by its scoped handler under the outer
        # authoring lock. Reacquiring here would deadlock that nested call.
        delegate = _verifier_execution_delegate(self, call.name, self._original_runtime)
        execute = delegate.execute_tool_async
        facade_call = _manager_call_to_facade(self._original_runtime, call)
        result = await execute(facade_call, **_supported_kwargs(execute, kwargs))
        complete = getattr(self._original_runtime, "complete_evidence", None)
        if complete is not None:
            result = await complete(call, result, **kwargs)
        return result

    def begin_tool_result_turn(self, **kwargs: Any) -> None:
        self.base_runtime.begin_tool_result_turn(**kwargs)

    def prepare_model_context(self, memory, continuation, *, context_view=None):
        hook = getattr(self._original_runtime, "prepare_model_context", None)
        if hook is not None:
            hook(memory, continuation, context_view=context_view)

    def model_response_received(self, continuation):
        hook = getattr(self._original_runtime, "model_response_received", None)
        if hook is not None:
            hook(continuation)

    def observe_tool_delivery(self, call, result):
        hook = getattr(self._original_runtime, "observe_tool_delivery", None)
        if hook is not None:
            hook(call, result)

    def stagnation_payload(self, call, result):
        hook = getattr(self._original_runtime, "stagnation_payload", None)
        return hook(call, result) if hook else {"ok": result.ok, "text": result.text, "structured": result.structured}

    def advance_tool_result_clock(self, **kwargs: Any) -> Any:
        advance = getattr(self._original_runtime, "advance_tool_result_clock", None)
        return advance(**kwargs) if callable(advance) else None

    def retire_tool_results(self, **kwargs: Any) -> tuple[str, ...]:
        """Retire result-owned authority in the underlying role lifetime."""

        retire = getattr(self._original_runtime, "retire_tool_results", None)
        if not callable(retire):
            return ()
        return tuple(retire(**kwargs) or ())

    @property
    def result_snapshots(self):
        return self._original_runtime.result_snapshots

    def bind_result_history(self, memory_service):
        return self._original_runtime.bind_result_history(memory_service)

    def logical_context_for_turn(self, turn_id):
        return self._original_runtime.logical_context_for_turn(turn_id)

    def commit_tool_delivery(self, **kwargs: Any) -> Any:
        commit = getattr(
            self._original_runtime,
            "commit_tool_delivery",
            None,
        )
        return commit(**kwargs) if callable(commit) else None

    async def acknowledge_tool_result_async(self, call_id: str, turn_id: str) -> None:
        acknowledge = getattr(self._original_runtime, "acknowledge_tool_result_async", None)
        if callable(acknowledge):
            await acknowledge(call_id, turn_id)

    async def interrupt_turn(self, turn_id: str) -> None:
        interrupt = getattr(self._original_runtime, "interrupt_turn", None)
        if callable(interrupt):
            await interrupt(turn_id)

    def discard_uncommitted_tool_delivery(self, **kwargs: Any) -> Any:
        discard = getattr(
            self._original_runtime,
            "discard_uncommitted_tool_delivery",
            None,
        )
        return discard(**kwargs) if callable(discard) else None

    def resolve_capability_address(self, name: object) -> str:
        if self._legacy_finding_generation is not None and str(name) == ADD_FINDING_TOOL_SPEC["alias"]:
            return ADD_FINDING_CAPABILITY
        return self.base_runtime.resolve_capability_address(name)

    def list_capability_specs(self) -> list[dict[str, Any]]:
        allowed = set(self.allowed_capabilities)
        result = []
        for spec in self.base_runtime.list_capability_specs():
            canonical = str(spec.get("canonical_path") or spec.get("name") or "")
            if canonical in allowed and not is_bunshin_capability_denied(canonical):
                result.append(
                    _scrub_spec(spec)
                )
        return result

    def get_capability_spec(self, name: str) -> dict[str, Any] | None:
        canonical = self.resolve_capability_address(name)
        if canonical not in set(self.allowed_capabilities) or is_bunshin_capability_denied(canonical):
            return None
        spec = self.base_runtime.get_capability_spec(canonical)
        return (
            _scrub_spec(spec)
            if spec
            else None
        )

    async def execute_tool_async(
        self,
        call: ToolCallIR,
        *,
        allow_tools: bool = True,
        budget: Any = None,
        turn_id: str | None = None,
    ) -> ToolExecutionResult:
        raw_name = str(call.name or "").strip()
        if raw_name.startswith(("op_", "intro_")):
            return _error_result(
                call,
                "unknown tool alias; use the exact scoped alias from the current tool contracts",
                "unknown_tool",
            )
        admission = admit_bunshin_tool_call(
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
        effective_turn_id = str(turn_id or "").strip() or self._direct_turn_id
        if not effective_turn_id:
            return _error_result(
                admission.call,
                "scoped tool execution requires an explicit logical lifetime",
                "missing_execution_lifetime",
            )
        if _requires_verifier_authoring_lock(self, admission.target_name):
            async with self._authoring_lock:
                return await self._execute_verifier_authoring(
                    guarded_call, target_name=admission.target_name,
                    budget=budget, turn_id=effective_turn_id,
                )
        return await self._execute_admitted(guarded_call, budget=budget, turn_id=effective_turn_id)

    async def _execute_verifier_authoring(
        self, call: ToolCallIR, *, target_name: str, budget: Any, turn_id: str,
    ) -> ToolExecutionResult:
        from pal.bunshin.verification_readiness import (
            record_verification_execution, verification_corpus_snapshot,
        )

        is_submit = (is_swe_verification_capability(target_name)
                     or target_name == "op_bunshin_verification_submit")
        if is_submit:
            if self.role_execution_sessions is not None and self.role_execution_sessions.has_work:
                return _error_result(call, "Complete pending execution before submitting verification.",
                                     "verification_execution_pending")
        else:
            try:
                context = SubmissionDraftContext.from_workspace(self.workspace, draft_kind="verification")
                SubmissionDraftStore(Path(str(self.workspace["runtime_root"]))).assert_editable(context)
            except (ValueError, RuntimeError) as exc:
                from pal.bunshin.verifier_tool_diagnostics import record_verifier_failure
                record_verifier_failure(exc)
                result = _error_result(call, str(exc), "verification_authoring_closed")
                return replace(result, invocation_result=rejection("verification_authoring_closed", str(exc),
                                        details={"reason": "verification_authoring_closed"}))
        # Another serialized writer may have changed a path while this call
        # waited. Resolve its write scope again under the authoring lock.
        call, guard_error = _guard_scoped_workspace_mutation(
            call, target_name=target_name, workspace=self.workspace,
        )
        if guard_error is not None:
            return guard_error
        direct_execution = target_name == "op_exec_shell" or target_name.startswith("op_lsp_")
        before = verification_corpus_snapshot(self.workspace) if direct_execution else None
        result = await self._execute_admitted(call, budget=budget, turn_id=turn_id,
            delegate=_verifier_execution_delegate(self, target_name, self.base_runtime))
        if before is not None:
            record_verification_execution(self.workspace, new_tool_call(
                name=target_name, args=effective_bunshin_tool_args(call), call_id=call.call_id,
            ), result, before)
        return result

    async def _execute_admitted(
        self, call: ToolCallIR, *, budget: Any, turn_id: str, delegate: Any | None = None,
    ) -> ToolExecutionResult:
        if call.name == ADD_FINDING_CAPABILITY and self._legacy_finding_generation is not None:
            return await _execute_legacy_finding(self, call, budget=budget, turn_id=turn_id)
        dispatch_call = (_manager_call_to_facade(self.base_runtime.runtime, call)
                         if delegate is not None else call)
        result = await (delegate or self.base_runtime).execute_tool_async(
            dispatch_call,
            allow_tools=True,
            budget=budget,
            turn_id=turn_id,
        )
        if call.name in {"op_bunshin_artifact_write", "op_bunshin_artifact_edit"} and result.ok:
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
    call: ToolCallIR,
    *,
    target_name: str,
    workspace: dict[str, Any],
) -> tuple[ToolCallIR, ToolExecutionResult | None]:
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
    args = effective_bunshin_tool_args(call)
    raw_path = str(args.get("file_path") or "").strip()
    if not repo_raw or not raw_path:
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
    has_compiled_write_scopes = "write_path_scopes" in workspace
    has_read_only_overlays = "read_only_overlay_paths" in workspace
    if not has_compiled_write_scopes and not has_read_only_overlays:
        args["file_path"] = str(target)
        return _tool_call_with_effective_args(call, args), None
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
    call: ToolCallIR,
    args: dict[str, Any],
) -> ToolCallIR:
    if call.name == "op_tool_call":
        outer = dict(call.args or {})
        outer["args"] = args
        return new_tool_call(
            name=call.name,
            args=outer,
            call_id=call.call_id,
        )
    return new_tool_call(name=call.name, args=args, call_id=call.call_id)


def _path_not_writable_result(
    call: ToolCallIR,
    *,
    raw_path: str,
    relative_path: str,
    reason: str,
) -> ToolExecutionResult:
    display = relative_path or raw_path
    text = (
        f"path is not writable in this role: {display}. "
        "Change only the paths in the bound write scope; verifier-owned corpus "
        "must be repaired by its Verifier."
    )
    return ToolExecutionResult(
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
    input_model = (
        BunshinScopedExecutionShellInput
        if canonical == "op_exec_shell" and not descriptor.metadata.get("preserve_role_contract")
        else descriptor.InputModel
    )
    guidance = bunshin_tool_guidance(
        canonical,
        descriptor.guidance,
        guidance_overrides.get(canonical),
    )
    execution = descriptor.execution
    if execution is not None and not descriptor.metadata.get("preserve_role_invocation_mode"):
        execution = execution.model_copy(update={"invocation_mode": InvocationMode.DIRECT})
    return replace(
        descriptor,
        InputModel=input_model,
        guidance=guidance,
        execution=execution,
        metadata={
            **dict(descriptor.metadata or {}),
            "allow_missing_next_tool_hints": True,
            "scoped_projection": "bunshin",
        },
    )


def _manager_call_to_facade(runtime: Any, call: ToolCallIR) -> ToolCallIR:
    """Translate Manager-internal canonical addressing at the facade boundary.

    Bunshin policy and workspace handlers intentionally keep canonical paths as
    stable internal capability identities.  The execution facade never accepts
    those identities, so this boundary captures one generation, selects its
    single alias, and uses ``call_tool`` for an indirect record.
    """

    generation = getattr(runtime, "registry_generation", None)
    if generation is None:
        return call
    if call.name in {"op_tool_call", "call_tool", "op_tool_read", "read_tool"}:
        args = dict(call.args or {})
        target = str(args.get("name") or "")
        targets = [record for record in (*generation.direct_aliases.values(), *generation.indirect_aliases.values())
                   if record.canonical_path == target]
        if len(targets) == 1:
            args["name"] = targets[0].alias
            call = new_tool_call(name=call.name, args=args, call_id=call.call_id)
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
    alias_call = new_tool_call(
        name=record.alias,
        args=dict(call.args or {}),
        call_id=call.call_id,
    )
    if record.execution.invocation_mode is InvocationMode.DIRECT:
        return alias_call
    return new_tool_call(
        name="call_tool",
        args={"name": record.alias, "args": dict(call.args or {})},
        call_id=call.call_id,
    )


def _scrub_spec(spec: dict[str, Any]) -> dict[str, Any]:
    value = dict(spec)
    canonical = str(value.get("canonical_path") or value.get("name") or "")
    value["canonical_path"] = canonical
    value["name"] = str(value.get("name") or canonical).strip()
    value["guidance"] = dict(value.get("guidance") or {})
    return value


def _error_result(call: ToolCallIR, text: str, reason: str) -> ToolExecutionResult:
    return ToolExecutionResult(
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


def _effective_capability_name(tool_call: ToolCallIR) -> str:
    return effective_bunshin_capability_name(tool_call)


def _effective_tool_args(tool_call: ToolCallIR) -> dict[str, Any]:
    return effective_bunshin_tool_args(tool_call)
