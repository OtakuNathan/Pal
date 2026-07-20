from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pal.execution.contracts import CapabilityCall, CapabilityResult
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


_STRING_MAP_SCHEMA = {"type": "object", "additionalProperties": {"type": ["string", "null"]}}
_PROFILE_OVERRIDE_CHANGES_SCHEMA = {
    "type": "object",
    "properties": {
        "display_name": {"type": ["string", "null"]},
        "identity_fragment": {"type": ["string", "null"]},
        "behavior_fragment": {"type": ["string", "null"]},
        "output_contract_fragment": {"type": ["string", "null"]},
        "preferred_endpoint_id": {"type": ["string", "null"]},
        "capability_groups": {"type": ["array", "null"], "items": {"type": "string"}},
        "default_allowed_capabilities": {"type": ["array", "null"], "items": {"type": "string"}},
        "skill_refs": {"type": ["array", "null"], "items": {"type": "string"}},
        "default_approval_policy": {"type": ["object", "null"]},
        "workspace_policy": {"type": ["object", "null"]},
        "workspace_environment_policy": {"type": ["object", "null"]},
        "completion_policy": {"type": ["object", "null"]},
        "capability_policy": {"type": ["object", "null"]},
        "capability_description_overrides": {"type": ["object", "null"], "additionalProperties": {"type": "string"}},
        "output_policy": {"type": ["object", "null"]},
        "metadata": {"type": ["object", "null"]},
    },
    "additionalProperties": False,
}
_FAMILY_OVERRIDE_CHANGES_SCHEMA = {
    "type": "object",
    "properties": {
        "display_name": {"type": ["string", "null"]},
        "domain": {"type": ["string", "null"]},
        "domain_keywords": {"type": ["array", "null"], "items": {"type": "string"}},
        "workflow_template": {"type": ["string", "null"]},
        "roles": {**_STRING_MAP_SCHEMA, "type": ["object", "null"]},
        "builders": {**_STRING_MAP_SCHEMA, "type": ["object", "null"]},
        "adapters": {**_STRING_MAP_SCHEMA, "type": ["object", "null"]},
        "policies": {"type": ["object", "null"]},
        "capability_groups": {"type": ["object", "null"]},
        "metadata": {"type": ["object", "null"]},
    },
    "additionalProperties": False,
}


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
        description=(
            "Read the effective Minion profile/family catalog from the attached sidecar. Builtins come from the installed package and explicit "
            "user overrides are marked separately. Use semantic names such as software_engineering.v2_coder; no runtime files or Manager IDs are exposed."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["all", "profiles", "families"], "default": "all"},
                "query": {"type": "string", "default": ""},
                "include_definitions": {"type": "boolean", "default": False},
            },
            "additionalProperties": False,
        },
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
        description=(
            "Atomically patch one Minion profile inside the sidecar. The profile is selected by semantic name; omitted fields retain their current "
            "effective value and null removes an optional field. Existing workflows keep their pinned FamilyBindingArtifact, so this affects only future work."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "profile": {"type": "string", "description": "Semantic profile name, for example software_engineering.v2_coder."},
                "changes": {
                    **_PROFILE_OVERRIDE_CHANGES_SCHEMA,
                    "description": "Typed merge patch for the profile definition; null removes an optional field.",
                },
            },
            "required": ["profile", "changes"],
            "additionalProperties": False,
        },
    )
    def set_profile_override(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, _channel = self._actor_and_channel(call)
            payload = self._request_manager(
                "catalog_set_profile_override",
                {**dict(call.args or {}), "actor": actor},
            )
            return _public_result("minion profile override updated", payload)
        except Exception as exc:
            return _error("minion profile override failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion_catalog",
        action_name="reset_profile_override",
        description="Remove one explicit profile override in the sidecar and restore the current package builtin when one exists.",
        args_schema={
            "type": "object",
            "properties": {"profile": {"type": "string"}},
            "required": ["profile"],
            "additionalProperties": False,
        },
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
        description=(
            "Atomically patch one data-driven Minion family inside the sidecar using its semantic family name. Role references and profile availability "
            "are validated before the override becomes visible to future workflows."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "family": {"type": "string", "description": "Semantic family name, for example software_engineering."},
                "changes": {
                    **_FAMILY_OVERRIDE_CHANGES_SCHEMA,
                    "description": "Typed merge patch for the family definition; null removes an optional field.",
                },
            },
            "required": ["family", "changes"],
            "additionalProperties": False,
        },
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
        args_schema={
            "type": "object",
            "properties": {"family": {"type": "string"}},
            "required": ["family"],
            "additionalProperties": False,
        },
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
        description=(
            "Ask the attached Minion sidecar to reload package builtins, migrate any legacy managed seeds, validate explicit overrides, and return the "
            "new effective catalog generation. The Pal process does not read or modify Minion catalog files."
        ),
        args_schema={"type": "object", "properties": {}, "additionalProperties": False},
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
        description=(
            "Start one durable Minion workflow and bind it to the current actor/channel. Supply the user's exact goal, family, workspace, and optional "
            "workspace-relative source files. The Manager preserves those bytes as the immutable task truth without extracting or normalizing requirements, "
            "and creates or reuses all internal identities. Use "
            "review_then_execute with artifact for a named external architecture, execute_trusted only for a Manager-trusted named artifact, "
            "standalone_review for review-only, and review_and_repair for bounded repair. Never inspect or implement the target in the foreground first."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "Optional natural-language title of an existing Task."},
                "title": {"type": "string"},
                "family_id": {"type": "string", "default": "software_engineering"},
                "operation": {
                    "type": "string",
                    "enum": ["new_requirement", "execute_trusted", "review_then_execute", "standalone_review", "review_and_repair"],
                    "default": "new_requirement",
                },
                "goal": {"type": "string"},
                "workspace": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string"},
                        "repo_path": {"type": "string"},
                        "repo_root": {"type": "string"},
                        "primary_language": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
                "source_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Workspace-relative UTF-8 Markdown or text files whose exact bytes are additional immutable task truth sources. "
                        "The Manager does not extract, normalize, deduplicate, classify, or reinterpret them. Valid only for new_requirement."
                    ),
                },
                "constraints": {
                    "type": "array",
                    "description": (
                        "Optional machine/environment constraints for execution and fingerprinting. Product-visible obligations belong in the goal or source files; "
                        "Minion orchestration policy belongs to the family binding."
                    ),
                },
                "approved_evidence": {
                    "type": "array",
                    "description": "Already-approved evidence entries used when research_mode=none; source_kind must be approved, user_supplied, or input_artifact.",
                },
                "references": {"type": "array"},
                "research_mode": {"type": "string", "enum": ["none", "local_only", "external_allowed"], "default": "local_only"},
                "artifact": {"type": "string", "description": "Natural-language name previously given to op_minion_submit_artifact."},
            },
            "additionalProperties": False,
        },
    )
    def start_workflow(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            args = dict(call.args or {})
            task_selector = str(args.pop("task", "") or "").strip()
            artifact_name = str(args.pop("artifact", "") or "").strip()
            if task_selector:
                args["task_id"] = self.service.resolve_task_selector(selector=task_selector, actor=actor)
            if artifact_name:
                args["artifact_ref"] = self.service.resolve_artifact_name(
                    name=artifact_name,
                    actor=actor,
                    source_channel=channel,
                )
            payload = self.service.start_workflow(
                {
                    **args,
                    "actor": actor,
                    "source_channel": channel,
                    "control_route": self._control_route(call),
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
        description=(
            "Publish a durable artifact under a natural-language name in the current actor/channel. This does not execute it. "
            "Later workflow calls refer to this name; the Manager owns its content hash and internal identity."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "artifact_type": {"type": "string"},
                "schema_version": {"type": "string", "default": "1"},
                "media_type": {"type": "string", "default": "application/json"},
                "content": {},
            },
            "required": ["name", "artifact_type", "content"],
            "additionalProperties": False,
        },
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
        description=(
            "Search the durable Minion V2 Task Ledger across channels for the current actor. Use this before claiming that a workflow cannot be "
            "found, and when the current channel has no bound workflow. Returns semantic Task details and compact Workflow phase/liveness summaries "
            "without exposing Manager identities. An empty query lists the most recently updated Tasks."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "default": ""},
                "family_id": {"type": "string"},
                "include_archived": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
            },
            "additionalProperties": False,
        },
    )
    def search_tasks(self, call: IntrospectionCall) -> IntrospectionResult:
        try:
            actor, channel = self._actor_and_channel(call)
            payload = self.service.search_task_ledger(
                {
                    **dict(call.args or {}),
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
        scope="minion_workflow",
        action_name="status",
        description=(
            "Read the current channel-bound workflow, or select one by natural-language Task title. Returns phase, active module/role, blocker, "
            "next legal actions, user-wait flag, timings, last progress, and liveness without exposing Manager identities. When human_review_available "
            "is true, call this same tool with view=human_review to read the durable Architecture Markdown, reviewer verdict/findings, and available actions."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "view": {
                    "type": "string",
                    "enum": ["status", "human_review"],
                    "default": "status",
                    "description": "status returns the compact projection; human_review returns the durable pending review without internal ids or tokens.",
                },
            },
            "additionalProperties": False,
        },
    )
    def workflow_status(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            workflow_id = self.service.resolve_workflow_selector(
                selector=str(call.args.get("task") or ""), actor=actor, source_channel=channel
            )
            payload = self.service.workflow_status(
                workflow_id,
                view=str(call.args.get("view") or "status"),
            )
            return _public_result("minion workflow status", payload)
        except ValueError as exc:
            return _invalid("minion workflow selection invalid", exc)
        except Exception as exc:
            return _error("minion V2 workflow status failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="resume_workflow",
        description=(
            "Resume a deliberately paused V2 workflow. TRIAGE_REQUIRED work must be handled explicitly with resolve_triage after its "
            "reported blocker has actually been addressed."
        ),
        args_schema={
            "type": "object",
            "properties": {"task": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    def resume_workflow(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            workflow_id = self.service.resolve_workflow_selector(
                selector=str(call.args.get("task") or ""), actor=actor, source_channel=channel
            )
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
        description=(
            "Discard the current execution attempt and restart from its accepted architecture without rerunning Architect first. The Manager "
            "safely cancels and settles the current workflow, creates a new review_then_execute workflow under the same Task with the latest "
            "Family binding, requires Architecture Review and Human Accept, and reuses no Coder candidates. Use this when execution policy or "
            "Coder behavior changed but the accepted architecture remains the intended baseline."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "reason": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Auditable reason the current execution must be discarded and restarted.",
                },
            },
            "required": ["reason"],
            "additionalProperties": False,
        },
    )
    def restart_execution(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            workflow_id = self.service.resolve_workflow_selector(
                selector=str(call.args.get("task") or ""), actor=actor, source_channel=channel
            )
            payload = self.service.restart_execution_from_architecture(
                workflow_id=workflow_id,
                actor=actor,
                source_channel=channel,
                reason=str(call.args.get("reason") or ""),
                control_route=self._control_route(call),
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
        description=(
            "Mark one TRIAGE_REQUIRED workflow item as manually handled and resume it through the Manager's declared RESOLVE_TRIAGE "
            "transition. Supply what was actually fixed or verified. This does not accept a candidate, waive verification, or skip a gate. "
            "When several items need triage, select exactly one by its semantic module or phase name, such as ohos_font or architecture."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "subject": {
                    "type": "string",
                    "description": "Module or phase name. Optional only when the workflow has exactly one TRIAGE_REQUIRED item.",
                },
                "resolution": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Auditable summary of the external or manual action that removed the blocker.",
                },
            },
            "required": ["resolution"],
            "additionalProperties": False,
        },
    )
    def resolve_triage(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            workflow_id = self.service.resolve_workflow_selector(
                selector=str(call.args.get("task") or ""), actor=actor, source_channel=channel
            )
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
        description=(
            "Submit Accept/Edit/Reject for the current channel-bound architecture review. The Manager resolves the unique pending card and "
            "atomically validates its actor, channel, revision, and content before acting. Use this manual path when an inline card expired."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "decision": {"type": "string", "enum": ["accept", "edit", "reject", "clarify"]},
                "edit_scope": {
                    "type": "string",
                    "enum": ["architecture", "requirements"],
                    "default": "architecture",
                    "description": "For decision=edit, choose whether product Requirements change or only the architecture changes.",
                },
                "edit_instruction": {
                    "type": "string",
                    "description": "Required for architecture edits. This changes the architecture, not the immutable task source.",
                },
                "amendment": {
                    "type": "string",
                    "description": "Raw user amendment appended verbatim to the immutable task sources. Valid only for edit_scope=requirements.",
                },
                "source_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Workspace-relative UTF-8 files appended verbatim to the task sources for a requirements edit.",
                },
                "clarification_response": {"type": "string"},
            },
            "required": ["decision"],
            "additionalProperties": False,
        },
    )
    def submit_human_decision(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            workflow_id = self.service.resolve_workflow_selector(
                selector=str(call.args.get("task") or ""), actor=actor, source_channel=channel
            )
            payload = self.service.submit_human_decision(
                {
                    **dict(call.args or {}),
                    "workflow_id": workflow_id,
                    "actor": actor,
                    "source_channel": channel,
                    "control_route": self._control_route(call),
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
        action_name="answer_question",
        description=(
            "Answer the single pending Architect question for the current channel-bound workflow with custom free text. "
            "Use this when the user's response is not one of the inline options. The Architect remains running and receives "
            "the answer through its existing tool call."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "answer": {"type": "string", "minLength": 1},
            },
            "required": ["answer"],
            "additionalProperties": False,
        },
    )
    def answer_question(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            workflow_id = self.service.resolve_workflow_selector(
                selector=str(call.args.get("task") or ""), actor=actor, source_channel=channel
            )
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
        args_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string"},
                "command": {"type": "string", "enum": ["pause", "cancel"]},
                "reason": {"type": "string"},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    )
    def control_workflow(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            workflow_id = self.service.resolve_workflow_selector(
                selector=str(call.args.get("task") or ""), actor=actor, source_channel=channel
            )
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
        args_schema={
            "type": "object",
            "properties": {"task": {"type": "string"}, "reason": {"type": "string"}},
            "additionalProperties": False,
        },
    )
    def archive_workflow(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            workflow_id = self.service.resolve_workflow_selector(
                selector=str(call.args.get("task") or ""), actor=actor, source_channel=channel
            )
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
        channel = str(call.meta.get("channel_id") or "")
        turn_id = str(call.meta.get("turn_id") or "")
        if self.context is not None and turn_id:
            core = self.context.port_registry.get("core:core")
            continuation = getattr(getattr(core, "state", None), "active_turns", {}).get(turn_id)
            envelope = getattr(continuation, "channel_envelope", None)
            if envelope is not None:
                try:
                    from pal.control.routing import route_from_channel_envelope

                    route = route_from_channel_envelope(envelope)
                    channel = f"{route.channel_kind}:{route.endpoint_id}"
                except Exception:
                    pass
        return actor, channel or "local"

    def _control_route(self, call: CapabilityCall) -> dict[str, Any]:
        turn_id = str(call.meta.get("turn_id") or "")
        if self.context is None or not turn_id:
            return {}
        core = self.context.port_registry.get("core:core")
        continuation = getattr(getattr(core, "state", None), "active_turns", {}).get(turn_id)
        envelope = getattr(continuation, "channel_envelope", None)
        if envelope is None:
            return {}
        try:
            from pal.control.routing import route_from_channel_envelope

            route = route_from_channel_envelope(envelope)
            return {
                "endpoint_id": route.endpoint_id,
                "channel_kind": route.channel_kind,
                "reply_target": dict(route.reply_target),
                "control_scope_key": route.control_scope_key,
                "correlation_id": route.correlation_id,
            }
        except Exception:
            return {}

    def _wake(self) -> None:
        if self.wake_manager is None:
            raise RuntimeError("minion sidecar lifecycle is not configured")
        self.wake_manager()

    def _request_manager(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.manager_request is None:
            raise RuntimeError("minion sidecar request transport is not configured")
        return self.manager_request(method, dict(params or {}))


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
        "task_title": "task",
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
