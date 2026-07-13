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
        action_name="start_workflow",
        description=(
            "Start one durable Minion workflow and bind it to the current actor/channel. Supply the user-confirmed goal, natural-language Requirement "
            "sections, family, workspace, and truth-source references. The Manager creates or reuses the internal Task and all identities. Use "
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
                "sections": {
                    "type": "object",
                    "description": "Immutable Requirement text grouped by natural-language section.",
                    "additionalProperties": {"type": "array", "items": {"type": "string"}},
                },
                "requirements": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "section": {"type": "string"},
                            "statement": {"type": "string"},
                            "strength": {"type": "string", "enum": ["hard", "soft"]},
                        },
                        "required": ["section", "statement"],
                        "additionalProperties": False,
                    },
                },
                "strengths": {"type": "object"},
                "constraints": {"type": "array"},
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
            "Resume a deliberately paused V2 workflow or resolve recoverable TRIAGE_REQUIRED child aggregates through their "
            "declared RESOLVE_TRIAGE transitions. This never skips a gate or fabricates a checkpoint."
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
                "edit_instruction": {"type": "string"},
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
