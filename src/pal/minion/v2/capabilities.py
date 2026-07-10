from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from pal.execution.contracts import CapabilityCall, CapabilityResult
from pal.minion.v2.service import MinionV2WorkflowService
from pal.shared import (
    INTROSPECTION_NAMESPACE,
    OPERATION_NAMESPACE,
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

    def __post_init__(self) -> None:
        self.service = MinionV2WorkflowService(Path(self.runtime_root))

    @capability_action(
        namespace=OPERATION_NAMESPACE,
        scope="minion",
        action_name="start_workflow",
        description=(
            "Start one durable Minion V2 workflow. Use new_requirement for a user request; execute_trusted only for an "
            "internally trusted ArchitectureContractArtifact; review_then_execute for an external V2 architecture that must "
            "be reviewed first; standalone_review for review-only; review_and_repair for a bounded review/repair loop. "
            "This is the only normal workflow creation entrypoint."
        ),
        args_schema={
            "type": "object",
            "properties": {
                "workflow_id": {"type": "string"},
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
                "workspace": {"type": "object"},
                "references": {"type": "array"},
                "research_mode": {"type": "string", "enum": ["none", "local_only", "external_allowed"], "default": "local_only"},
                "artifact_ref": {"type": "object"},
            },
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
        description="Resume a deliberately paused V2 workflow through its only legal state transition. It does not retry checkpoints.",
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
        if self.wake_manager is not None:
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
