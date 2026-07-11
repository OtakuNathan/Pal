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
    scope="minion_workflow",
    path_module_id="minion_workflow",
    kind="module",
    source="builtin:minion-v2",
    target_kind="workflow",
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
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="task_create",
        description=(
            "Create one long-lived Minion Task and bind its immutable family. Call this before op_minion_start_workflow when no existing task_id "
            "was supplied. This delegates later repository research, architecture, production, and verification to Minion roles; the foreground "
            "agent should not inspect or implement the target itself before creating the Task. Workflows are execution contracts under this Task."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "objective": {"type": "string"},
                "family_id": {"type": "string"},
                "workspace": {
                    "type": "object",
                    "description": "Task workspace. For an existing repository use kind=existing_repo and repo_path; repo_root/root/path are accepted aliases and normalized.",
                    "properties": {
                        "kind": {"type": "string"},
                        "repo_path": {"type": "string"},
                        "repo_root": {"type": "string"},
                        "primary_language": {"type": "string"},
                    },
                },
                "references": {
                    "type": "array",
                    "description": "Read-only truth-source references. Prefer {name,path,description,required}; local file:// URIs are accepted and normalized to path.",
                    "items": {"type": "object"},
                },
                "policies": {"type": "object"},
            },
            "required": ["title", "objective", "family_id", "workspace"],
        },
    )
    def task_create(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            payload = self.service.create_task({**dict(call.args or {}), "actor": actor, "source_channel": channel})
            return _result("minion Task created", payload)
        except ValueError as exc:
            return _invalid("minion Task request invalid", exc)
        except Exception as exc:
            return _error("minion Task creation failed", exc)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion_task",
        action_name="search",
        description="Find long-lived Minion Tasks by exact task_id, project/repository text, objective, or family.",
        args_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "query": {"type": "string"},
                "family_id": {"type": "string"},
                "include_archived": {"type": "boolean", "default": False},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
            },
        },
    )
    def task_search(self, call: CapabilityCall) -> CapabilityResult:
        try:
            return _result("minion Task search", self.service.search_tasks(dict(call.args or {})))
        except Exception as exc:
            return _error("minion Task search failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="task_update",
        description="Version the project objective, workspace, references, or policy of one active Task. family_id is immutable.",
        args_schema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "objective": {"type": "string"},
                "workspace": {"type": "object"},
                "references": {"type": "array"},
                "policies": {"type": "object"},
            },
            "required": ["task_id"],
        },
    )
    def task_update(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            payload = self.service.update_task({**dict(call.args or {}), "actor": actor, "source_channel": channel})
            return _result("minion Task updated", payload)
        except ValueError as exc:
            return _invalid("minion Task update invalid", exc)
        except Exception as exc:
            return _error("minion Task update failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="task_archive",
        description="Archive an active Task after every child Workflow is terminal.",
        args_schema={
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["task_id"],
        },
    )
    def task_archive(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            payload = self.service.archive_task({**dict(call.args or {}), "actor": actor, "source_channel": channel})
            return _result("minion Task archived", payload)
        except ValueError as exc:
            return _invalid("minion Task archive invalid", exc)
        except Exception as exc:
            return _error("minion Task archive failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="start_workflow",
        description=(
            "Start one durable Minion V2 workflow. Use new_requirement for a user request; execute_trusted only for an "
            "internally trusted ArchitectureContractArtifact; review_then_execute for an external V2 architecture that must "
            "be reviewed first; standalone_review for review-only; review_and_repair for a bounded review/repair loop. "
            "This is the only normal workflow creation entrypoint. It requires the task_id returned by op_minion_task_create or an existing Task. "
            "When the user asks Minion to do the work, call this capability instead of reading the target repository, constructing the architecture, "
            "or implementing the request in the foreground agent."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "task_id": {"type": "string"},
                "operation": {
                    "type": "string",
                    "enum": ["new_requirement", "execute_trusted", "review_then_execute", "standalone_review", "review_and_repair"],
                    "default": "new_requirement",
                },
                "goal": {"type": "string"},
                "requirements": {"type": "array"},
                "constraints": {"type": "array"},
                "approved_evidence": {
                    "type": "array",
                    "description": "Already-approved evidence entries used when research_mode=none; source_kind must be approved, user_supplied, or input_artifact.",
                },
                "references": {"type": "array"},
                "research_mode": {"type": "string", "enum": ["none", "local_only", "external_allowed"], "default": "local_only"},
                "artifact_ref": {"type": "object"},
            },
            "required": ["task_id"],
        },
    )
    def start_workflow(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            payload = self.service.start_workflow(
                {
                    **dict(call.args or {}),
                    "actor": actor,
                    "source_channel": channel,
                    "control_route": self._control_route(call),
                }
            )
            self._wake()
            return _result("minion V2 workflow created", payload)
        except ValueError as exc:
            return _invalid("minion V2 workflow request invalid", exc)
        except Exception as exc:
            return _error("minion V2 workflow start failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="submit_artifact",
        description=(
            "Publish a durable content-addressed V2 artifact before starting or revising a workflow. This does not execute it. "
            "trusted_internal_source may only be set by trusted internal callers."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "artifact_type": {"type": "string"},
                "schema_version": {"type": "string", "default": "1"},
                "media_type": {"type": "string", "default": "application/json"},
                "content": {},
                "provenance": {"type": "object"},
                "metadata": {"type": "object"},
            },
            "required": ["artifact_type", "content"],
        },
    )
    def submit_artifact(self, call: CapabilityCall) -> CapabilityResult:
        try:
            payload = self.service.submit_artifact(dict(call.args or {}))
            return _result("minion V2 artifact published", payload)
        except ValueError as exc:
            return _invalid("minion V2 artifact invalid", exc)
        except Exception as exc:
            return _error("minion V2 artifact publish failed", exc)

    @capability_action(
        namespace=INTROSPECTION_NAMESPACE,
        scope="minion_workflow",
        action_name="status",
        description=(
            "Read the durable V2 workflow projection. Always returns one current phase, active aggregate/worker, blocker, "
            "next legal actions, user-wait flag, timing metrics, last progress event, and liveness."
        ),
        args_schema={
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    )
    def workflow_status(self, call: CapabilityCall) -> CapabilityResult:
        try:
            payload = self.service.workflow_status(str(call.args.get("workflow_id") or ""))
            return _result("minion V2 workflow status", payload)
        except Exception as exc:
            return _error("minion V2 workflow status failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="resume_workflow",
        description=(
            "Resume a deliberately paused V2 workflow or resolve recoverable TRIAGE_REQUIRED child aggregates through their "
            "declared RESOLVE_TRIAGE transitions. This never skips a gate or fabricates a checkpoint."
        ),
        args_schema={
            "type": "object",
            "properties": {"workflow_id": {"type": "string"}},
            "required": ["workflow_id"],
        },
    )
    def resume_workflow(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            payload = self.service.resume_workflow(
                workflow_id=str(call.args.get("workflow_id") or ""),
                actor=actor,
                source_channel=channel,
            )
            self._wake()
            return _result("minion V2 workflow resume requested", payload)
        except ValueError as exc:
            return _invalid("minion V2 workflow cannot resume", exc)
        except Exception as exc:
            return _error("minion V2 workflow resume failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="submit_human_decision",
        description=(
            "Submit Accept/Edit/Reject for the exact architecture revision card. The decision token, actor, channel, revision, "
            "and manifest SHA are atomically checked and consumed once. Use this when an inline card expired as well as for button handling."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "decision_token": {"type": "string"},
                "decision": {"type": "string", "enum": ["accept", "edit", "reject", "clarify"]},
                "edit_instruction": {"type": "string"},
                "clarification_response": {"type": "string"},
            },
            "required": ["decision_token", "decision"],
        },
    )
    def submit_human_decision(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            payload = self.service.submit_human_decision(
                {**dict(call.args or {}), "actor": actor, "source_channel": channel}
            )
            self._wake()
            return _result("minion V2 human decision accepted", payload)
        except ValueError as exc:
            return _invalid("minion V2 human decision stale or invalid", exc)
        except Exception as exc:
            return _error("minion V2 human decision failed", exc)

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="control_workflow",
        description="Request asynchronous pause or cancel for a V2 workflow. Child aggregates stop at safe points before the workflow settles.",
        args_schema={
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
                "command": {"type": "string", "enum": ["pause", "cancel"]},
                "reason": {"type": "string"},
            },
            "required": ["workflow_id", "command"],
        },
    )
    def control_workflow(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, channel = self._actor_and_channel(call)
            payload = self.service.control_workflow(
                workflow_id=str(call.args.get("workflow_id") or ""),
                command=str(call.args.get("command") or ""),
                actor=actor,
                source_channel=channel,
                reason=str(call.args.get("reason") or ""),
            )
            self._wake()
            return _result("minion V2 workflow control requested", payload)
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
            "properties": {"workflow_id": {"type": "string"}, "reason": {"type": "string"}},
            "required": ["workflow_id"],
        },
    )
    def archive_workflow(self, call: CapabilityCall) -> CapabilityResult:
        try:
            actor, _channel = self._actor_and_channel(call)
            payload = self.service.archive_workflow(
                workflow_id=str(call.args.get("workflow_id") or ""),
                actor=actor,
                reason=str(call.args.get("reason") or ""),
            )
            return _result("minion V2 workflow archived", payload)
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


def _result(title: str, payload: dict[str, Any]) -> CapabilityResult:
    return CapabilityResult(
        status=RuntimeStatus.OK,
        text=title,
        structured=payload,
        llm_text=render_titled_structured_for_llm(title, payload),
    )


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
