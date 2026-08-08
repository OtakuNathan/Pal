from __future__ import annotations

from pal.execution.tool_semantics import (
    INDIRECT_CONTROL,
    INDIRECT_LOCAL_WRITE,
    INDIRECT_UNSAFE_LOCAL_WRITE,
)
from pal.execution.tool_facade import ToolGuidance

from pal.execution.generated_tool_models import (
    MinionV2CapabilitiesMinionV2PublicProviderAnswerQuestionInput,
    MinionV2CapabilitiesMinionV2PublicProviderArchiveWorkflowInput,
    MinionV2CapabilitiesMinionV2PublicProviderControlWorkflowInput,
    MinionV2CapabilitiesMinionV2PublicProviderReadInput,
    MinionV2CapabilitiesMinionV2PublicProviderRefreshInput,
    MinionV2CapabilitiesMinionV2PublicProviderResetFamilyOverrideInput,
    MinionV2CapabilitiesMinionV2PublicProviderResetProfileOverrideInput,
    MinionV2CapabilitiesMinionV2PublicProviderResolveTriageInput,
    MinionV2CapabilitiesMinionV2PublicProviderRestartExecutionInput,
    MinionV2CapabilitiesMinionV2PublicProviderResumeWorkflowInput,
    MinionV2CapabilitiesMinionV2PublicProviderSearchInput,
    MinionV2CapabilitiesMinionV2PublicProviderSetFamilyOverrideInput,
    MinionV2CapabilitiesMinionV2PublicProviderSetProfileOverrideInput,
    MinionV2CapabilitiesMinionV2PublicProviderStatusInput,
    MinionV2CapabilitiesMinionV2PublicProviderSubmitArtifactInput,
)

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from pydantic import Field

from pal.execution.contracts import CapabilityCall, CapabilityResult
from pal.execution.tool_facade import StrictToolModel, ToolGuidance
from pal.execution.tool_semantics import DIRECT_CONTROL
from pal.minion.v2.service import MinionV2WorkflowService
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

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


MINION_START_WORKFLOW_PURPOSE = (
    "Start one durable Minion workflow and bind its future delivery to the channel that owns the current turn."
)

MINION_START_WORKFLOW_GUIDANCE = ToolGuidance(
    purpose=MINION_START_WORKFLOW_PURPOSE,
    use_when=(
        "Use when the requested work is identity-light executor work: a medium-to-large or long-running project, work "
        "that benefits from architect/coder/verifier gates, or a task the user explicitly asks Minion to perform. "
        "Choose by identity binding and task nature, not by prose length or step count. Before calling, inspect "
        "skill_search with read_tool and invoke it through call_tool to find a relevant operation manual. If any are "
        "found, ask whether to provide them and wait; pass only explicitly approved names in skill_refs. Supply the "
        "canonical profile, short goal, narrow workspace, and complete task_spec. "
        "Use review_then_execute with an external architecture artifact, execute_trusted only for a Manager-trusted "
        "artifact, standalone_review for review-only work, and review_and_repair for bounded repair."
    ),
    do_not_use_when=(
        "Do not use for work strongly bound to Pal's relationship with the user, including conversation, Q&A, "
        "check-ins, quizzes, personal technical discussion, a single-point code investigation, or reading code and "
        "giving conclusions. In the gray area Pal handles the task directly unless the user explicitly requests Minion. "
        "Do not inspect or implement the target in the foreground first. After acceptance, do not poll status or create "
        "a proactive polling task; callbacks deliver clarification, review, and completion events."
    ),
    failure_next_steps=(
        "Correct invalid task, profile, workspace, operation, artifact, or approved skill input. If creation may have "
        "succeeded, reconcile with minion_task_status before retrying; do not start a duplicate workflow."
    ),
)


class MinionV2StartWorkflowWorkspace(StrictToolModel):
    kind: Literal["new_project", "existing_repo"] = Field(
        description=(
            "Required workspace intent. Use new_project when Pal should create a "
            "missing repo_path; use existing_repo only when repo_path already exists."
        ),
    )
    repo_path: str | None = Field(
        default=None,
        description=(
            "Exact project repository the workflow may inspect and modify. Bind the "
            "narrowest repository that owns the deliverable; never pass a parent "
            "container merely because it also contains requirements, benchmark "
            "comparators, or sibling implementations. Those inputs must remain "
            "outside the worker workspace and be supplied explicitly when relevant."
        ),
    )
    repo_root: str | None = Field(
        default=None,
        description=(
            "Legacy path spelling accepted only as an alias for repo_path. It is not "
            "a broader search root and must identify the same exact project repository."
        ),
    )
    primary_language: str | None = None


class MinionV2CapabilitiesMinionV2PublicProviderStartWorkflowInput(StrictToolModel):
    """Reloadable public contract for starting one Minion workflow."""

    task: str | None = Field(
        default=None,
        description="Optional natural-language title of an existing Task.",
    )
    title: str | None = None
    profile: str | None = Field(
        default=None,
        description=(
            "Canonical Task profile, such as software_engineering.v2_coder or "
            "lifestyle.nutritionist. Required when task is omitted and immutable after "
            "Task creation."
        ),
    )
    operation: Literal[
        "new_requirement",
        "execute_trusted",
        "review_then_execute",
        "standalone_review",
        "review_and_repair",
    ] = "new_requirement"
    goal: str | None = Field(
        default=None,
        description=(
            "Short routing objective. For new_requirement, task_spec is the complete "
            "semantic source of truth."
        ),
    )
    workspace: MinionV2StartWorkflowWorkspace | None = None
    task_spec: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Complete structured task specification for new_requirement. This becomes "
            "original in the immutable task.yaml ledger; later user-authorized changes "
            "are append-only revisions."
        ),
    )
    skill_refs: list[str] | None = Field(
        default=None,
        description=(
            "Exact active Pal skill names returned by skill_search that the user explicitly approved for this "
            "workflow. The Manager injects their manuals as user-side system reminders "
            "when each logical role session is first spawned."
        ),
    )
    constraints: list[Any] | None = Field(
        default=None,
        description=(
            "Optional machine/environment constraints for execution and fingerprinting. "
            "Product-visible obligations belong in task_spec; Minion orchestration policy "
            "belongs to the family binding."
        ),
    )
    approved_evidence: list[Any] | None = Field(
        default=None,
        description=(
            "Already-approved evidence entries used when research_mode=none; source_kind "
            "must be approved, user_supplied, or input_artifact."
        ),
    )
    references: list[Any] | None = None
    research_mode: Literal["none", "local_only", "external_allowed"] = "local_only"
    artifact: str | None = Field(
        default=None,
        description="Natural-language name previously given to minion_submit_artifact.",
    )


class MinionV2CapabilitiesMinionV2PublicProviderSubmitHumanDecisionInput(StrictToolModel):
    """Reloadable public contract for the architecture human-review decision."""

    task: str | None = Field(
        default=None,
        description="Human-readable Task title. Required when more than one active Task exists.",
    )
    decision: Literal["accept", "edit", "reject"]
    edit_instruction: str | None = Field(
        default=None,
        description=(
            "Required for decision=edit. Correct only architecture drift from the "
            "already pinned task; new requirements or preferences must use normal Pal "
            "communication and a new workflow revision."
        ),
    )


class MinionV2CapabilitiesMinionV2PublicProviderRebindTaskDeliveryInput(StrictToolModel):
    """Public semantic address for changing only a Task's reply target."""

    task: str = Field(description="Exact human-readable Task title.")
    channel_name: str = Field(
        description=(
            "Enabled channel endpoint name returned by channel_list that should receive future Task notifications. "
            "Provider-specific reply-target fields are Manager-owned and are not accepted."
        )
    )


@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="minion_workflow",
    path_module_id="minion_workflow",
    kind="module",
    source="builtin:minion-v2",
    target_kind="workflow",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="minion_task",
    path_module_id="minion_task",
    kind="module",
    source="builtin:minion-v2",
    target_kind="task",
)
@capability_node(
    namespace=INTROSPECTION_NAMESPACE,
    scope="minion_catalog",
    path_module_id="minion_catalog",
    kind="module",
    source="builtin:minion-v2",
    target_kind="catalog",
)
@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="minion_catalog",
    path_module_id="minion_catalog",
    kind="module",
    source="builtin:minion-v2",
    target_kind="catalog",
)
@capability_node(
    namespace=OPERATION_NAMESPACE,
    scope="minion",
    kind="module",
    source="builtin:minion-v2",
    target_kind="module",
)
@dataclass
class MinionV2PublicProvider:
    runtime_root: Path
    context: MainContext | None = None
    wake_manager: Callable[[], None] | None = None
    attach_manager: Callable[[], dict[str, Any]] | None = None
    detach_manager: Callable[[], None] | None = None
    manager_request: Callable[[str, dict[str, Any] | None], dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        self.service = MinionV2WorkflowService(Path(self.runtime_root))

    def attach(self, call: IntrospectionCall | None = None) -> IntrospectionResult:
        _ = call
        if self.attach_manager is None:
            raise RuntimeError("minion sidecar lifecycle is not configured")
        health = self.attach_manager()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="minion sidecar attached",
            structured={"manager_running": bool(health.get("ok")), **dict(health)},
            llm_text="Minion sidecar attached.",
        )

    def detach(self, call: IntrospectionCall | None = None) -> IntrospectionResult:
        _ = call
        if self.detach_manager is None:
            raise RuntimeError("minion sidecar lifecycle is not configured")
        self.detach_manager()
        return IntrospectionResult(
            status=RuntimeStatus.OK,
            text="minion sidecar detached",
            structured={"manager_running": False},
            llm_text="Minion sidecar detached.",
        )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion_catalog",
        action_name="read",
        description="Read the effective Minion profile/family catalog from the attached sidecar. Builtins come from the installed package and explicit user overrides are marked separately. Use semantic names such as software_engineering.v2_coder; no runtime files or Manager IDs are exposed.",
        guidance=ToolGuidance(
            purpose="Read the effective Minion profile/family catalog from the sidecar.",
            use_when="Checking available Minion profiles and families before starting a workflow.",
            do_not_use_when="Starting a workflow (use minion_start_workflow). Checking task status (use minion_task_status).",
            failure_next_steps="If sidecar not responding, check plugins_list for the minion plugin attach status.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderReadInput,
        aliases=("minion_catalog_read",),
    )
    def read_catalog(self, call: IntrospectionCall) -> IntrospectionResult:
        try:
            payload = self._request_manager("catalog_snapshot", dict(call.args or {}))
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text="minion catalog",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Minion catalog", payload),
            )
        except Exception as exc:
            payload = {"error": str(exc), "error_type": exc.__class__.__name__}
            return IntrospectionResult(
                status=RuntimeStatus.ERROR,
                text="minion catalog read failed",
                structured=payload,
                llm_text=render_titled_structured_for_llm("Minion catalog read failed", payload),
            )

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion_catalog",
        action_name="set_profile_override",
        description="Atomically patch one Minion profile inside the sidecar. The profile is selected by semantic name; omitted fields retain their current effective value and null removes an optional field. Existing Tasks keep their pinned FamilyBindingArtifact, so this affects only future Tasks.",
        guidance=ToolGuidance(
            purpose="Patch one Minion profile override in the sidecar.",
            use_when="Customizing a profile (e.g. changing model, constraints, or instructions) for future Tasks.",
            do_not_use_when="Removing an override (use minion_catalog_reset_profile_override). Existing Tasks are unaffected.",
            failure_next_steps="If profile name invalid, check minion_catalog_read for available profiles.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderSetProfileOverrideInput,
        aliases=("minion_catalog_set_profile_override",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_profile_override(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, _channel = self._actor_and_channel(call)
            args = dict(call.args or {})
            changes = dict(args.get("changes") or {})
            if "preferred_endpoint_name" in changes:
                changes["preferred_endpoint_id"] = changes.pop("preferred_endpoint_name")
            args["changes"] = changes
            payload = self._request_manager(
                "catalog_set_profile_override",
                {**args, "actor": actor},
            )
            return _public_result("minion profile override updated", payload)
        except Exception as exc:
            return _error("minion profile override failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion_catalog",
        action_name="reset_profile_override",
        description="Remove one explicit profile override in the sidecar and restore the current package builtin when one exists.",
        guidance=ToolGuidance(
            purpose="Remove one profile override and restore the package builtin.",
            use_when="Reverting a profile customization back to defaults.",
            do_not_use_when="Patching a profile (use minion_catalog_set_profile_override).",
            failure_next_steps="If no override exists, this is a no-op.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderResetProfileOverrideInput,
        aliases=("minion_catalog_reset_profile_override",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def reset_profile_override(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, _channel = self._actor_and_channel(call)
            payload = self._request_manager(
                "catalog_reset_profile_override",
                {"profile": str(call.args.get("profile") or ""), "actor": actor},
            )
            return _public_result("minion profile override reset", payload)
        except Exception as exc:
            return _error("minion profile override reset failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion_catalog",
        action_name="set_family_override",
        description="Atomically patch one data-driven Minion family inside the sidecar using its semantic family name. Four role bindings and profile availability are validated before the override becomes visible to future Tasks.",
        guidance=ToolGuidance(
            purpose="Patch one Minion family override in the sidecar.",
            use_when="Customizing role bindings or profile availability for a family.",
            do_not_use_when="Removing a family override (use minion_catalog_reset_family_override).",
            failure_next_steps="If family name invalid, check minion_catalog_read for available families.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderSetFamilyOverrideInput,
        aliases=("minion_catalog_set_family_override",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def set_family_override(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, _channel = self._actor_and_channel(call)
            payload = self._request_manager(
                "catalog_set_family_override",
                {**dict(call.args or {}), "actor": actor},
            )
            return _public_result("minion family override updated", payload)
        except Exception as exc:
            return _error("minion family override failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion_catalog",
        action_name="reset_family_override",
        description="Remove one explicit family override in the sidecar and restore the current package builtin when one exists.",
        guidance=ToolGuidance(
            purpose="Remove one family override and restore the package builtin.",
            use_when="Reverting a family customization back to defaults.",
            do_not_use_when="Patching a family (use minion_catalog_set_family_override).",
            failure_next_steps="If no override exists, this is a no-op.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderResetFamilyOverrideInput,
        aliases=("minion_catalog_reset_family_override",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def reset_family_override(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, _channel = self._actor_and_channel(call)
            payload = self._request_manager(
                "catalog_reset_family_override",
                {"family": str(call.args.get("family") or ""), "actor": actor},
            )
            return _public_result("minion family override reset", payload)
        except Exception as exc:
            return _error("minion family override reset failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion_catalog",
        action_name="refresh",
        description="Ask the attached Minion sidecar to reload package builtins, validate explicit overrides, and return the new effective catalog generation. The Pal process does not read or modify Minion catalog files.",
        guidance=ToolGuidance(
            purpose="Reload Minion package builtins and validate overrides.",
            use_when="After upgrading the Minion package or when catalog changes are not reflected.",
            do_not_use_when="Reading the catalog (use minion_catalog_read). Patching profiles (use minion_catalog_set_profile_override).",
            failure_next_steps="If refresh fails, the sidecar may need restart.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderRefreshInput,
        aliases=("minion_catalog_refresh",),
        execution=INDIRECT_LOCAL_WRITE,
    )
    def refresh_catalog(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, _channel = self._actor_and_channel(call)
            payload = self._request_manager("catalog_refresh", {"actor": actor})
            return _public_result("minion catalog refreshed", payload)
        except Exception as exc:
            return _error("minion catalog refresh failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="start_workflow",
        description=MINION_START_WORKFLOW_PURPOSE,
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderStartWorkflowInput,
        aliases=("minion_start_workflow",),
        guidance=MINION_START_WORKFLOW_GUIDANCE,
        execution=DIRECT_CONTROL,
    )
    def start_workflow(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            args = dict(call.args or {})
            task_selector = str(args.pop("task", "") or "").strip()
            artifact_name = str(args.pop("artifact", "") or "").strip()
            args["skill_refs"] = _dedupe_strings(args.get("skill_refs"))
            if task_selector:
                args["task_id"] = self.service.resolve_task_selector(selector=task_selector, actor=actor)
            if artifact_name:
                args["artifact_ref"] = self.service.resolve_artifact_name(
                    name=artifact_name,
                    actor=actor,
                )
            payload = self._request_manager(
                "v2_start_workflow",
                {
                    **args,
                    "actor": actor,
                    "source_channel": channel,
                    "delivery_binding": self._capture_delivery_binding(call),
                }
            )
            self._wake()
            return _public_result("minion workflow created", payload)
        except ValueError as exc:
            return _invalid("minion V2 workflow request invalid", exc)
        except Exception as exc:
            return _error("minion V2 workflow start failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="submit_artifact",
        description="Publish a durable artifact under a natural-language name.",
        guidance=ToolGuidance(
            purpose="Publish a durable artifact under a natural-language name for the current actor.",
            use_when="When the user has a named architecture or design to reference in later workflow calls.",
            do_not_use_when="This does not execute the artifact. Not for unnamed or ad-hoc content.",
            failure_next_steps="Correct invalid input; the Manager owns content hash and internal identity.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderSubmitArtifactInput,
        aliases=("minion_submit_artifact",),
        execution=INDIRECT_UNSAFE_LOCAL_WRITE,
    )
    def submit_artifact(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            payload = self.service.submit_artifact(
                {
                    **dict(call.args or {}),
                    "actor": actor,
                    "source_channel": channel,
                    "trusted_internal_source": False,
                }
            )
            return _public_result("minion artifact published", payload)
        except ValueError as exc:
            return _invalid("minion V2 artifact invalid", exc)
        except Exception as exc:
            return _error("minion V2 artifact publish failed", exc)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion_task",
        action_name="search",
        description="Search the durable Minion V2 Task Ledger for the current actor.",
        guidance=ToolGuidance(
            purpose="Search the durable Minion V2 Task Ledger for the current actor.",
            use_when="Before claiming a workflow cannot be found. Empty query lists most recently updated Tasks.",
            do_not_use_when="Not for live workflow state (use minion_task_status).",
            failure_next_steps="Correct invalid input; try a broader query if no results.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderSearchInput,
        aliases=("minion_task_search",),
    )
    def search_tasks(self, call: IntrospectionCall) -> IntrospectionResult:
        try:
            actor, channel = self._actor_and_channel(call)
            args = dict(call.args or {})
            family = str(args.pop("family", "") or "").strip()
            payload = self.service.search_task_ledger(
                {
                    **args,
                    "family_id": family,
                    "actor": actor,
                    "source_channel": channel,
                }
            )
            public = _public_payload(payload)
            return IntrospectionResult(
                status=RuntimeStatus.OK,
                text="minion task search",
                structured=public,
                llm_text=render_titled_structured_for_llm("Minion task search", public),
            )
        except ValueError as exc:
            return IntrospectionResult(
                status=RuntimeStatus.INVALID,
                text="minion V2 task search invalid",
                structured={"error": str(exc), "error_type": exc.__class__.__name__},
                llm_text=render_titled_structured_for_llm(
                    "Minion V2 task search invalid",
                    {"error": str(exc), "error_type": exc.__class__.__name__},
                ),
            )
        except Exception as exc:
            return IntrospectionResult(
                status=RuntimeStatus.ERROR,
                text="minion V2 task search failed",
                structured={"error": str(exc), "error_type": exc.__class__.__name__},
                llm_text=render_titled_structured_for_llm(
                    "Minion V2 task search failed",
                    {"error": str(exc), "error_type": exc.__class__.__name__},
                ),
            )

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion_task",
        action_name="status",
        description="Read a Task by title and attach its current workflow state.",
        guidance=ToolGuidance(
            purpose="Read a Task by natural-language title, then mechanically attach its single current workflow when one exists.",
            use_when=(
                "Checking workflow phase, module Coder/Verifier state, blockers, next legal actions, timings, or liveness. "
                "When workflow.human_review_available is true, call with view=human_review to read the durable review."
            ),
            do_not_use_when="Do not poll repeatedly after workflow acceptance; completion returns through system callbacks.",
            failure_next_steps="Correct the Task title; reconcile with minion_task_search if the title is uncertain.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderStatusInput,
        aliases=("minion_task_status",),
    )
    def workflow_status(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            task_id, workflow_id = self.service.resolve_task_workflow_selector(
                selector=str(call.args.get("task") or ""),
                actor=actor,
                include_terminal=True,
            )
            view = str(call.args.get("view") or "status")
            if view == "human_review" and not workflow_id:
                raise ValueError("Task has no workflow waiting for human review")
            payload = (
                self.manager_request(
                    "v2_task_status",
                    {"task_id": task_id, "workflow_id": workflow_id, "view": view},
                )
                if self.manager_request is not None
                else self.service.task_status(
                    task_id,
                    workflow_id=workflow_id,
                    view=view,
                )
            )
            return _public_result("minion task status", payload)
        except ValueError as exc:
            return _invalid("minion task selection invalid", exc)
        except Exception as exc:
            return _error("minion V2 workflow status failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="resume_workflow",
        description="Resume a paused workflow or normalize orphaned work into triage items.",
        guidance=ToolGuidance(
            purpose="Resume a deliberately paused V2 workflow, or normalize orphaned worker-owned work into TRIAGE_REQUIRED items.",
            use_when="When a workflow was deliberately paused, or after an interrupted worker disappears.",
            do_not_use_when="Not for triage resolution (use minion_resolve_triage after addressing blockers).",
            failure_next_steps="Correct invalid input; reconcile with minion_task_status before retrying.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderResumeWorkflowInput,
        aliases=("minion_resume_workflow",),
        execution=INDIRECT_CONTROL,
    )
    def resume_workflow(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            _, workflow_id = self.service.resolve_task_workflow_selector(
                selector=str(call.args.get("task") or ""),
                actor=actor,
                include_terminal=False,
            )
            if not workflow_id:
                raise ValueError("Task has no active workflow to resume")
            payload = self.service.resume_workflow(
                workflow_id=workflow_id,
                actor=actor,
                source_channel=channel,
            )
            self._wake()
            return _public_result("minion workflow resume requested", payload)
        except ValueError as exc:
            return _invalid("minion V2 workflow cannot resume", exc)
        except Exception as exc:
            return _error("minion V2 workflow resume failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="restart_execution",
        description="Restart execution from the accepted architecture without rerunning Architect.",
        guidance=ToolGuidance(
            purpose="Discard the current execution attempt and restart from its accepted architecture.",
            use_when="When execution policy or Coder behavior changed but the accepted architecture remains the intended baseline.",
            do_not_use_when="Not for architecture changes (start a new workflow). Not for transient failures (use minion_resolve_triage).",
            failure_next_steps="Correct invalid input; reconcile with minion_task_status before retrying.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderRestartExecutionInput,
        aliases=("minion_restart_execution",),
        execution=INDIRECT_CONTROL,
    )
    def restart_execution(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            _, workflow_id = self.service.resolve_task_workflow_selector(
                selector=str(call.args.get("task") or ""),
                actor=actor,
                include_terminal=False,
            )
            if not workflow_id:
                raise ValueError("Task has no active workflow to restart")
            payload = self.service.restart_execution_from_architecture(
                workflow_id=workflow_id,
                actor=actor,
                source_channel=channel,
                reason=str(call.args.get("reason") or ""),
            )
            self._wake()
            return _public_result("minion execution restart requested", payload)
        except ValueError as exc:
            return _invalid("minion V2 execution restart invalid", exc)
        except Exception as exc:
            return _error("minion V2 execution restart failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="resolve_triage",
        description="Mark one TRIAGE_REQUIRED workflow item as manually handled.",
        guidance=ToolGuidance(
            purpose="Mark one TRIAGE_REQUIRED workflow item as manually handled and resume it.",
            use_when="After the blocker has actually been addressed. Copy the exact semantic subject from workflow status (e.g. module:ohos_font).",
            do_not_use_when="Does not accept a candidate, waive verification, or skip a gate.",
            failure_next_steps="Correct invalid input; reconcile with minion_task_status before retrying.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderResolveTriageInput,
        aliases=("minion_resolve_triage",),
        execution=INDIRECT_CONTROL,
    )
    def resolve_triage(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            _, workflow_id = self.service.resolve_task_workflow_selector(
                selector=str(call.args.get("task") or ""),
                actor=actor,
                include_terminal=False,
            )
            if not workflow_id:
                raise ValueError("Task has no active workflow in triage")
            payload = self.service.resolve_triage(
                workflow_id=workflow_id,
                actor=actor,
                source_channel=channel,
                subject=str(call.args.get("subject") or ""),
                resolution=str(call.args.get("resolution") or ""),
            )
            self._wake()
            return _public_result("minion triage resolved", payload)
        except ValueError as exc:
            return _invalid("minion V2 triage resolution invalid", exc)
        except Exception as exc:
            return _error("minion V2 triage resolution failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="submit_human_decision",
        description="Submit Accept/Edit/Reject for an architecture review.",
        guidance=ToolGuidance(
            purpose="Submit Accept/Edit/Reject for an architecture review.",
            use_when="When an inline review card is unavailable and the user needs to decide manually.",
            do_not_use_when="Not for live workflow state queries (use minion_task_status).",
            failure_next_steps="Correct invalid input; the Manager validates actor, revision, and content atomically.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderSubmitHumanDecisionInput,
        aliases=("minion_submit_human_decision",),
        execution=INDIRECT_CONTROL,
    )
    def submit_human_decision(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            _, workflow_id = self.service.resolve_task_workflow_selector(
                selector=str(call.args.get("task") or ""),
                actor=actor,
                include_terminal=False,
            )
            if not workflow_id:
                raise ValueError("Task has no active workflow awaiting a decision")
            payload = self.service.submit_human_decision(
                {
                    **{
                        key: value
                        for key, value in dict(call.args or {}).items()
                        if key != "task"
                    },
                    "workflow_id": workflow_id,
                    "actor": actor,
                    "source_channel": channel,
                }
            )
            self._wake()
            return _public_result("minion human decision accepted", payload)
        except ValueError as exc:
            return _invalid("minion V2 human decision stale or invalid", exc)
        except Exception as exc:
            return _error("minion V2 human decision failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="rebind_task_delivery",
        description=(
            "Change only where future notifications for one Task are delivered. The Manager keeps the Task, workflow, "
            "workers, graph, and review state unchanged. Use this when the user explicitly asks to receive a running "
            "Task's questions, review cards, or completion on another enabled channel endpoint."
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderRebindTaskDeliveryInput,
        aliases=("minion_rebind_task_delivery",),
        guidance=ToolGuidance(
            purpose="Rebind one Task's Manager-owned reply target without changing its workflow.",
            use_when="Use only after the user explicitly names a Task and destination channel endpoint.",
            do_not_use_when="Do not use merely because the user contacted Pal from another channel; ordinary conversation never moves Task delivery.",
            failure_next_steps="Use channel_list to find endpoint names or correct the exact Task title; never edit workflow state to repair delivery.",
        ),
        execution=DIRECT_CONTROL,
    )
    def rebind_task_delivery(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, _channel = self._actor_and_channel(call)
            task_id = self.service.resolve_task_selector(
                selector=str(call.args.get("task") or ""),
                actor=actor,
            )
            binding = self._resolve_rebind_binding(
                call,
                str(call.args.get("channel_name") or ""),
            )
            payload = self._request_manager(
                "v2_rebind_task_delivery",
                {"task_id": task_id, "binding": binding},
            )
            public = {
                "task": str(call.args.get("task") or "").strip(),
                "channel_name": str(binding.get("channel_id") or "").strip(),
                "channel_id": str(binding.get("channel_id") or "").strip(),
                "changed": bool(payload.get("changed")),
            }
            return _public_result("minion Task delivery rebound", public)
        except ValueError as exc:
            return _invalid("minion Task delivery rebind invalid", exc)
        except Exception as exc:
            return _error("minion Task delivery rebind failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="answer_question",
        description="Answer the pending Architect question for a Task with custom free text.",
        guidance=ToolGuidance(
            purpose="Answer the single pending Architect question for the named Task.",
            use_when="When the user's response is not one of the inline options.",
            do_not_use_when="Not for multiple-choice answers (use inline options when available).",
            failure_next_steps="Correct invalid input; the Architect receives the answer through its existing tool call.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderAnswerQuestionInput,
        aliases=("minion_answer_question",),
        execution=INDIRECT_CONTROL,
    )
    def answer_question(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            _, workflow_id = self.service.resolve_task_workflow_selector(
                selector=str(call.args.get("task") or ""),
                actor=actor,
                include_terminal=False,
            )
            if not workflow_id:
                raise ValueError("Task has no active workflow question")
            if self.manager_request is None:
                raise RuntimeError("minion sidecar request path is unavailable")
            payload = self.manager_request(
                "answer_workflow_question",
                {
                    "workflow_id": workflow_id,
                    "answer": str(call.args.get("answer") or ""),
                },
            )
            return _public_result("minion question answered", payload)
        except ValueError as exc:
            return _invalid("minion question answer invalid", exc)
        except Exception as exc:
            return _error("minion question answer failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="control_workflow",
        description="Request asynchronous pause or cancel for a V2 workflow. Child aggregates stop at safe points before the workflow settles.",
        guidance=ToolGuidance(
            purpose="Request asynchronous pause or cancel for a V2 workflow.",
            use_when="The user wants to stop a running workflow.",
            do_not_use_when="Starting a workflow (use minion_start_workflow). Checking status (use minion_task_status).",
            failure_next_steps="If task not found, verify title with minion_task_search.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderControlWorkflowInput,
        aliases=("minion_control_workflow",),
        execution=INDIRECT_CONTROL,
    )
    def control_workflow(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            _, workflow_id = self.service.resolve_task_workflow_selector(
                selector=str(call.args.get("task") or ""),
                actor=actor,
                include_terminal=False,
            )
            if not workflow_id:
                raise ValueError("Task has no active workflow to control")
            payload = self.service.control_workflow(
                workflow_id=workflow_id,
                command=str(call.args.get("command") or ""),
                actor=actor,
                source_channel=channel,
                reason=str(call.args.get("reason") or ""),
            )
            self._wake()
            return _public_result("minion workflow control requested", payload)
        except ValueError as exc:
            return _invalid("minion V2 workflow control invalid", exc)
        except Exception as exc:
            return _error("minion V2 workflow control failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="archive_workflow",
        description="Archive a terminal V2 workflow. Active workflows must be cancelled and settled first.",
        guidance=ToolGuidance(
            purpose="Archive a terminal V2 workflow.",
            use_when="Cleaning up a completed or cancelled workflow from the active task list.",
            do_not_use_when="Active workflows must be cancelled first (use minion_control_workflow).",
            failure_next_steps="If task not found or not terminal, verify status with minion_task_status.",
        ),
        InputModel=MinionV2CapabilitiesMinionV2PublicProviderArchiveWorkflowInput,
        aliases=("minion_archive_workflow",),
        execution=INDIRECT_CONTROL,
    )
    def archive_workflow(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            _, workflow_id = self.service.resolve_task_workflow_selector(
                selector=str(call.args.get("task") or ""),
                actor=actor,
                include_terminal=True,
            )
            if not workflow_id:
                raise ValueError("Task has no workflow to archive")
            payload = self.service.archive_workflow(
                workflow_id=workflow_id,
                actor=actor,
                reason=str(call.args.get("reason") or ""),
            )
            return _public_result("minion workflow archived", payload)
        except ValueError as exc:
            return _invalid("minion V2 workflow archive invalid", exc)
        except Exception as exc:
            return _error("minion V2 workflow archive failed", exc)

    def _actor_and_channel(self, call: CapabilityCall) -> tuple[str, str]:
        actor = str(call.meta.get("actor_id") or call.meta.get("persona_id") or "pal")
        binding = self._capture_delivery_binding(call)
        return actor, str(binding.get("channel_id") or "local")

    def _capture_delivery_binding(self, call: CapabilityCall) -> dict[str, Any]:
        turn_id = str(call.meta.get("turn_id") or "")
        if self.context is None or not turn_id:
            return {}
        runtime = getattr(self.context, "execution_runtime", None)
        port = getattr(runtime, "provider_registry", {}).get("core:turn_io")
        capture = getattr(port, "capture_delivery_binding", None)
        if not callable(capture):
            return {}
        return dict(capture(turn_id) or {})

    def _resolve_rebind_binding(
        self,
        call: CapabilityCall,
        channel_id: str,
    ) -> dict[str, Any]:
        requested = str(channel_id or "").strip()
        if not requested:
            raise ValueError("channel_id is required")
        current = self._capture_delivery_binding(call)
        if requested == str(current.get("channel_id") or ""):
            return current
        if self.context is None:
            raise ValueError("channel runtime is unavailable")
        runtime = self.context.port_registry.get("channel:channel")
        endpoint = runtime.get_endpoint(requested) if runtime is not None else None
        if endpoint is None:
            raise ValueError(f"channel endpoint {requested!r} was not found")
        if not bool(getattr(endpoint, "attached", False)):
            raise ValueError(f"channel endpoint {requested!r} is detached")
        if not bool(getattr(endpoint, "enabled", False)):
            raise ValueError(f"channel endpoint {requested!r} is disabled")
        reply_target = dict(endpoint.derive_default_reply_target() or {})
        if not reply_target:
            raise ValueError(
                f"channel endpoint {requested!r} has no unambiguous reply target"
            )
        channel_kind = str(endpoint.endpoint.channel_kind or "")
        return {
            "channel_id": requested,
            "channel_kind": channel_kind,
            "reply_target": reply_target,
            "control_scope_key": str(
                reply_target.get("control_scope_key")
                or f"{channel_kind}:{requested}"
            ),
        }

    def _wake(self) -> None:
        if self.wake_manager is None:
            raise RuntimeError("minion sidecar lifecycle is not configured")
        self.wake_manager()

    def _request_manager(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.manager_request is None:
            raise RuntimeError("minion sidecar request transport is not configured")
        return self.manager_request(method, dict(params or {}))


def _dedupe_strings(value: Any) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in list(value or []):
        text = str(item or "").strip()
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def _public_result(title: str, payload: dict[str, Any]) -> CapabilityResult:
    public = _public_payload(payload)
    return CapabilityResult(
        status=RuntimeStatus.OK,
        text=title,
        structured=public,
        llm_text=render_titled_structured_for_llm(title, public),
    )


def _public_payload(value: Any) -> Any:
    if isinstance(value, list):
        return [_public_payload(item) for item in value]
    if not isinstance(value, dict):
        return value
    hidden = {
        "workflow_id",
        "task_id",
        "revision_id",
        "architecture_revision_id",
        "aggregate_id",
        "active_aggregate_id",
        "active_aggregate_type",
        "active_worker",
        "workflow_name",
        "decision_token",
        "actor_id",
        "active_channel_id",
        "route",
        "control_route",
        "request_ref",
        "artifact_ref",
        "last_progress_event_id",
    }
    aliases = {
        "task_name": "task",
        "current_phase": "phase",
        "workflow_state": "state",
        "active_node_state": "active_state",
    }
    result: dict[str, Any] = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        lowered = key.casefold()
        if key in hidden or key.endswith("_ref") or key.endswith("_sha") or "sha256" in lowered:
            continue
        if key == "next_action" and item == "manager_outbox_tick":
            result["next_action"] = "background orchestration"
            continue
        public_key = aliases.get(key, key)
        public_value = _public_payload(item)
        if public_key == "last_progress_event" and isinstance(public_value, dict):
            public_value.pop("aggregate_type", None)
        result[public_key] = public_value
    return result


def _invalid(title: str, exc: Exception) -> CapabilityResult:
    payload = {"error": str(exc), "error_type": exc.__class__.__name__}
    return CapabilityResult(
        status=RuntimeStatus.INVALID,
        text=title,
        structured=payload,
        llm_text=render_titled_structured_for_llm(title, payload),
    )


def _error(title: str, exc: Exception) -> CapabilityResult:
    payload = {"error": str(exc), "error_type": exc.__class__.__name__}
    return CapabilityResult(
        status=RuntimeStatus.ERROR,
        text=title,
        structured=payload,
        llm_text=render_titled_structured_for_llm(title, payload),
    )
