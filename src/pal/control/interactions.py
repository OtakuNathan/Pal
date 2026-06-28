from __future__ import annotations

import hashlib
import json
from typing import Any
from uuid import uuid4

from pal.control.contracts import (
    ControlAction,
    ControlDelivery,
    ControlDeliveryKind,
    ControlRoute,
    InteractionButtonSpec,
    InteractionMessageSpec,
)

INTERACTIVE_DELIVERY_KINDS: set[str] = {
    "interactive_open",
    "interactive_update",
    "interactive_resolve",
    "interactive_expire",
}
_MINION_APPROVAL_ARG_LIMIT = 320
_TELEGRAM_MESSAGE_TEXT_LIMIT = 4096
_INTERACTION_MESSAGE_SAFE_LIMIT = min(3900, _TELEGRAM_MESSAGE_TEXT_LIMIT)
_MINION_APPROVAL_TEXT_LIMIT = _INTERACTION_MESSAGE_SAFE_LIMIT
_MINION_MEMORY_CANDIDATE_PREVIEW_LIMIT = 1000
_MINION_QUESTION_BUTTON_LABEL_LIMIT = 48
_MINION_QUESTION_EVIDENCE_ITEM_LIMIT = 900


def delivery_for_reply(
    route: ControlRoute | None,
    text: str,
    *,
    payload: dict[str, Any] | None = None,
) -> ControlDelivery:
    return ControlDelivery(
        delivery_kind="reply",
        route=route,
        text=str(text or ""),
        payload=dict(payload or {}),
    )


def delivery_for_interaction(
    route: ControlRoute | None,
    delivery_kind: ControlDeliveryKind,
    interaction: InteractionMessageSpec,
    *,
    text: str | None = None,
    payload: dict[str, Any] | None = None,
) -> ControlDelivery:
    if delivery_kind not in INTERACTIVE_DELIVERY_KINDS:
        raise ValueError(f"unsupported interaction delivery kind: {delivery_kind}")
    return ControlDelivery(
        delivery_kind=delivery_kind,
        route=route or interaction.route,
        text=str(text if text is not None else interaction.text),
        interaction=interaction,
        payload=dict(payload or {}),
    )


def delivery_for_endpoint_status(
    endpoint_id: str,
    status_kind: str,
    *,
    payload: dict[str, Any] | None = None,
) -> ControlDelivery:
    return ControlDelivery(
        delivery_kind="endpoint_status",
        endpoint_id=str(endpoint_id or "").strip() or None,
        payload={
            "status_kind": str(status_kind or "").strip(),
            "payload": dict(payload or {}),
        },
    )


def control_panel_interaction_id(route: ControlRoute) -> str:
    scope = str(route.control_scope_key or route.endpoint_id or "control_panel")
    digest = hashlib.sha1(scope.encode("utf-8")).hexdigest()[:12]
    return f"ctl_panel_{digest}"


def build_control_panel_interaction(
    control_plane: Any,
    route: ControlRoute,
    *,
    banner: str | None = None,
) -> InteractionMessageSpec:
    rows: list[tuple[InteractionButtonSpec, ...]] = []
    for spec in control_plane.list_panel_commands():
        if not getattr(spec, "panel_button", False):
            continue
        action_key = str(getattr(spec, "interaction_action_key", "") or "").strip() or "control.command.run"
        action_args = {"command_name": spec.name} if action_key == "control.command.run" else {}
        rows.append(
            (
                InteractionButtonSpec(
                    label=str(getattr(spec, "panel_label", "") or spec.name),
                    action_key=action_key,
                    action_args=action_args,
                ),
            )
        )
    text = str(control_plane.render_panel_text())
    if banner:
        text = f"{banner}\n\n{text}"
    return InteractionMessageSpec(
        interaction_id=control_panel_interaction_id(route),
        interaction_kind="control_panel",
        route=route,
        text=text,
        buttons=tuple(rows),
        expires_at=None,
    )


def control_panel_delivery(control_plane: Any, route: ControlRoute | None) -> ControlDelivery:
    if route is None:
        return delivery_for_reply(None, str(control_plane.render_panel_text()))
    return delivery_for_interaction(
        route,
        "interactive_update",
        build_control_panel_interaction(control_plane, route),
    )


def build_think_panel_interaction(
    route: ControlRoute,
    think_level: str,
    *,
    banner: str | None = None,
) -> InteractionMessageSpec:
    current = str(think_level or "balanced")

    def label(level: str) -> str:
        return f"> {level}" if level == current else level

    text = f"Think level: {current}\nSelect a level for new turns."
    if banner:
        text = f"{banner}\n\n{text}"
    return InteractionMessageSpec(
        interaction_id=control_panel_interaction_id(route),
        interaction_kind="control_panel",
        route=route,
        text=text,
        buttons=(
            (
                InteractionButtonSpec(label=label("off"), action_key="control.think.set", action_args={"think_level": "off"}),
                InteractionButtonSpec(label=label("minimal"), action_key="control.think.set", action_args={"think_level": "minimal"}),
                InteractionButtonSpec(label=label("low"), action_key="control.think.set", action_args={"think_level": "low"}),
            ),
            (
                InteractionButtonSpec(label=label("balanced"), action_key="control.think.set", action_args={"think_level": "balanced"}),
                InteractionButtonSpec(label=label("deep"), action_key="control.think.set", action_args={"think_level": "deep"}),
                InteractionButtonSpec(label=label("xhigh"), action_key="control.think.set", action_args={"think_level": "xhigh"}),
            ),
            (InteractionButtonSpec(label="Back", action_key="control.panel.back"),),
        ),
        expires_at=None,
    )


def think_panel_delivery(route: ControlRoute | None, think_level: str) -> ControlDelivery:
    if route is None:
        return delivery_for_reply(None, f"Current think level: {think_level}")
    return delivery_for_interaction(
        route,
        "interactive_update",
        build_think_panel_interaction(route, think_level),
    )


def render_log_status_text(enabled: bool) -> str:
    status = "on" if enabled else "off"
    return (
        f"Prompt log: {status}\n"
        "Use /log start or /log end. Changes apply to new turns only."
    )


def build_log_panel_interaction(
    route: ControlRoute,
    enabled: bool,
    *,
    banner: str | None = None,
) -> InteractionMessageSpec:
    start_label = "> Start logging" if enabled else "Start logging"
    end_label = "Stop logging" if enabled else "> Stop logging"
    text = render_log_status_text(enabled)
    if banner:
        text = f"{banner}\n\n{text}"
    return InteractionMessageSpec(
        interaction_id=control_panel_interaction_id(route),
        interaction_kind="control_panel",
        route=route,
        text=text,
        buttons=(
            (InteractionButtonSpec(label=start_label, action_key="control.log.start"),),
            (InteractionButtonSpec(label=end_label, action_key="control.log.end"),),
            (InteractionButtonSpec(label="Back", action_key="control.panel.back"),),
        ),
        expires_at=None,
    )


def log_panel_delivery(route: ControlRoute | None, enabled: bool) -> ControlDelivery:
    if route is None:
        return delivery_for_reply(None, render_log_status_text(enabled))
    return delivery_for_interaction(
        route,
        "interactive_update",
        build_log_panel_interaction(route, enabled),
    )


def build_reset_confirm_interaction(request: Any) -> InteractionMessageSpec:
    return InteractionMessageSpec(
        interaction_id=str(request.request_id),
        interaction_kind="reset_confirm",
        route=request.route,
        text=reset_confirm_text(),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Confirm Reset",
                    action_key="control.reset.confirm",
                    action_args={"request_id": str(request.request_id)},
                ),
            ),
            (
                InteractionButtonSpec(
                    label="Cancel",
                    action_key="control.reset.cancel",
                    action_args={"request_id": str(request.request_id)},
                ),
            ),
        ),
        expires_at=request.expires_at,
    )


def reset_confirm_text() -> str:
    return (
        "Reset working memory for this scope?\n"
        "This clears L1, L2, and conversation-facing projection only.\n"
        "Durable L3 memory stays intact."
    )


def reset_confirm_fallback_text(request: Any) -> str:
    return f"{reset_confirm_text()}\nConfirm with /reset confirm {request.request_id}"


def reset_confirm_delivery(request: Any, *, already_opened: bool) -> ControlDelivery:
    return delivery_for_interaction(
        request.route,
        "interactive_update" if already_opened else "interactive_open",
        build_reset_confirm_interaction(request),
        text=reset_confirm_fallback_text(request),
    )


def build_terminal_interaction(
    *,
    interaction_id: str,
    interaction_kind: str,
    route: ControlRoute,
    text: str,
) -> InteractionMessageSpec:
    return InteractionMessageSpec(
        interaction_id=str(interaction_id or f"interaction_{uuid4().hex[:8]}"),
        interaction_kind=str(interaction_kind or "interaction"),
        route=route,
        text=str(text or ""),
        buttons=(),
        expires_at=None,
    )


def is_interaction_action(action: ControlAction) -> bool:
    return str(action.args.get("interaction_origin") or "").strip() == "button"


def terminal_delivery_for_action(
    action: ControlAction,
    text: str,
    *,
    delivery_kind: ControlDeliveryKind = "interactive_resolve",
) -> ControlDelivery:
    route = action.route
    if route is None or not is_interaction_action(action):
        return delivery_for_reply(route, text)
    interaction_id = str(
        action.args.get("interaction_id")
        or action.args.get("request_id")
        or action.target_id
        or ""
    ).strip()
    if not interaction_id:
        interaction_id = control_panel_interaction_id(route)
    interaction_kind = str(action.args.get("interaction_kind") or "").strip() or "control_panel"
    return delivery_for_interaction(
        route,
        delivery_kind,
        build_terminal_interaction(
            interaction_id=interaction_id,
            interaction_kind=interaction_kind,
            route=route,
            text=text,
        ),
    )


def terminal_delivery_for_interaction(
    route: ControlRoute,
    *,
    interaction_id: str,
    interaction_kind: str,
    text: str,
    delivery_kind: ControlDeliveryKind = "interactive_resolve",
) -> ControlDelivery:
    return delivery_for_interaction(
        route,
        delivery_kind,
        build_terminal_interaction(
            interaction_id=interaction_id,
            interaction_kind=interaction_kind,
            route=route,
            text=text,
        ),
    )


def build_control_catalog_payload(control_plane: Any) -> dict[str, Any]:
    commands: list[dict[str, str]] = []
    for spec in control_plane.list_panel_commands():
        command = str(spec.name or "").strip().lower()
        description = str(spec.description or "").strip()
        if not command or not description:
            continue
        commands.append({"command": command, "description": description})
    return {"commands": commands}


def control_catalog_delivery(control_plane: Any, endpoint_id: str) -> ControlDelivery:
    return delivery_for_endpoint_status(
        endpoint_id,
        "control_catalog",
        payload=build_control_catalog_payload(control_plane),
    )


def minion_approval_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery:
    return delivery_for_interaction(
        route,
        "interactive_open",
        build_minion_approval_interaction(payload, route),
    )


def build_minion_approval_interaction(payload: dict[str, Any], route: ControlRoute) -> InteractionMessageSpec:
    approval_id = str(payload.get("approval_id") or f"approval_{uuid4().hex[:8]}")
    action_payload = {**payload, "approval_id": approval_id}
    return InteractionMessageSpec(
        interaction_id=approval_id,
        interaction_kind="approval_request",
        route=route,
        text=render_minion_approval_text(action_payload),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Accept",
                    action_key="control.action.dispatch",
                    action_args=_minion_approval_action_payload(action_payload, "accept"),
                ),
                InteractionButtonSpec(
                    label="Accept All",
                    action_key="control.action.dispatch",
                    action_args=_minion_approval_action_payload(action_payload, "accept_all"),
                ),
            ),
            (
                InteractionButtonSpec(
                    label="Reject",
                    action_key="control.action.dispatch",
                    action_args=_minion_approval_action_payload(action_payload, "reject"),
                ),
            ),
        ),
    )


def render_minion_approval_text(payload: dict[str, Any]) -> str:
    args_summary = payload.get("args_summary")
    if not isinstance(args_summary, dict):
        args_summary = {}
    lines = [
        "Minion approval request",
        "",
        f"Title: {payload.get('title') or 'High-risk operation'}",
        f"Run: {payload.get('run_id') or '-'}",
        f"Work order: {payload.get('work_order_id') or '-'}",
        f"Requested action: {payload.get('requested_action') or payload.get('target') or '-'}",
        f"Risk: {payload.get('risk') or 'high'}",
    ]
    impact = str(payload.get("impact") or "").strip()
    if impact:
        lines.append(f"Impact: {impact}")
    target = str(payload.get("target") or "").strip()
    if target:
        lines.append(f"Target: {target}")
    rendered_args = _render_minion_approval_args(args_summary)
    if rendered_args:
        lines.append("Args:")
        lines.extend(rendered_args)
    return _truncate_text("\n".join(lines), _MINION_APPROVAL_TEXT_LIMIT)


def _minion_approval_action_payload(payload: dict[str, Any], decision: str) -> dict[str, Any]:
    approval_id = str(payload.get("approval_id") or "")
    return _minion_interaction_action_payload(
        action_kind="minion_approval_decision",
        target_id=approval_id,
        args={
            "approval_id": approval_id,
            "run_id": str(payload.get("run_id") or ""),
            "minion_id": str(payload.get("minion_id") or ""),
            "decision": decision,
        },
    )


def _minion_interaction_action_payload(*, action_kind: str, target_id: str, args: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_kind": action_kind,
        "target_scope": "minion",
        "target_id": str(target_id or ""),
        "args": dict(args),
    }


def _render_minion_approval_args(args_summary: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for index, (key, value) in enumerate(sorted(args_summary.items())):
        if index >= 8:
            lines.append(f"- ... {len(args_summary) - index} more args")
            break
        rendered = _render_arg_value(value)
        lines.append(f"- {key}: {_truncate_text(rendered, _MINION_APPROVAL_ARG_LIMIT)}")
    return lines


def _render_arg_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return str(value)
    return " ".join(str(value or "").split())


def _truncate_text(text: str, limit: int) -> str:
    value = str(text or "")
    if len(value) <= limit:
        return value
    return f"{value[: max(0, limit - 24)].rstrip()}... [truncated]"


def minion_lesson_approval_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    interaction = build_minion_lesson_approval_interaction(payload, route)
    if interaction is None:
        return None
    return delivery_for_interaction(route, "interactive_open", interaction)


def memory_candidate_approval_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    interaction = build_memory_candidate_approval_interaction(payload, route)
    if interaction is None:
        return None
    return delivery_for_interaction(route, "interactive_open", interaction)


def minion_module_continue_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    interaction = build_minion_module_continue_interaction(payload, route)
    if interaction is None:
        return None
    return delivery_for_interaction(route, "interactive_open", interaction)


def minion_plan_acceptance_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    interaction = build_minion_plan_acceptance_interaction(payload, route)
    if interaction is None:
        return None
    return delivery_for_interaction(route, "interactive_open", interaction)


def minion_requirements_review_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    interaction = build_minion_requirements_review_interaction(payload, route)
    if interaction is None:
        return None
    return delivery_for_interaction(route, "interactive_open", interaction)


def build_minion_requirements_review_interaction(payload: dict[str, Any], route: ControlRoute) -> InteractionMessageSpec | None:
    work_order_id = str(payload.get("work_order_id") or "").strip()
    artifact = payload.get("requirements_artifact") or payload.get("primary_artifact")
    if not isinstance(artifact, dict):
        artifact = {}
    target_id = work_order_id or str(artifact.get("relative_path") or artifact.get("path") or "").strip()
    if not target_id:
        return None
    interaction_id = f"minion_requirements_accept_{target_id}"
    summary = str(payload.get("summary") or "").strip()
    relative_path = str(artifact.get("relative_path") or "").strip()
    artifact_path = str(artifact.get("path") or "").strip()
    architecture_mode = str(payload.get("architecture_mode") or "").strip()
    lines = [
        "Minion requirements draft is ready.",
        "",
        f"Work order: {work_order_id or '-'}",
    ]
    if relative_path or artifact_path:
        lines.append(f"Artifact: {relative_path or artifact_path}")
    if architecture_mode:
        lines.append(f"Architecture mode: {architecture_mode}")
    if summary:
        lines.extend(["", "Summary:", _truncate_text(summary, 900)])
    lines.append("")
    lines.append("Accept these requirements and dispatch the architect?")
    base_args = {
        "work_order_id": work_order_id,
        "requirements_artifact": dict(artifact),
    }
    return InteractionMessageSpec(
        interaction_id=interaction_id,
        interaction_kind="minion_requirements_review",
        route=route,
        text="\n".join(lines),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Accept Requirements",
                    action_key="control.action.dispatch",
                    action_args=_minion_interaction_action_payload(
                        action_kind="minion_requirements_accept",
                        target_id=target_id,
                        args={
                            **base_args,
                            "reason": "human accepted requirements from channel interaction",
                        },
                    ),
                ),
                InteractionButtonSpec(
                    label="Reject Requirements",
                    action_key="control.action.dispatch",
                    action_args=_minion_interaction_action_payload(
                        action_kind="minion_requirements_reject",
                        target_id=target_id,
                        args={
                            **base_args,
                            "reason": "human rejected requirements from channel interaction",
                        },
                    ),
                ),
                InteractionButtonSpec(
                    label="Edit Requirements",
                    action_key="control.action.dispatch",
                    action_args=_minion_interaction_action_payload(
                        action_kind="minion_requirements_edit",
                        target_id=target_id,
                        args={
                            **base_args,
                            "reason": "human requested requirements edits from channel interaction",
                        },
                    ),
                ),
            ),
        ),
    )


def build_minion_plan_acceptance_interaction(payload: dict[str, Any], route: ControlRoute) -> InteractionMessageSpec | None:
    plan_ref = payload.get("plan_ref")
    if not isinstance(plan_ref, dict):
        return None
    work_order_id = str(payload.get("work_order_id") or "").strip()
    plan_id = str(plan_ref.get("plan_id") or "").strip()
    target_id = work_order_id or plan_id
    if not target_id:
        return None
    review_gate_ref = payload.get("review_gate_ref")
    if not isinstance(review_gate_ref, dict):
        review_gate_ref = {}
    interaction_id = f"minion_plan_accept_{target_id}"
    summary = str(payload.get("summary") or "").strip()
    lines = [
        "Minion plan review passed.",
        "",
        f"Work order: {work_order_id or '-'}",
        f"Plan: {plan_id or '-'}",
    ]
    revision = plan_ref.get("plan_revision")
    if revision is not None:
        lines.append(f"Revision: {revision}")
    gate_id = str(review_gate_ref.get("gate_id") or "").strip()
    if gate_id:
        lines.append(f"Review gate: {gate_id}")
    if summary:
        lines.extend(["", "Summary:", _truncate_text(summary, 900)])
    lines.append("")
    lines.append("Accept this reviewed plan for dispatch?")
    return InteractionMessageSpec(
        interaction_id=interaction_id,
        interaction_kind="minion_plan_acceptance",
        route=route,
        text="\n".join(lines),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Accept Plan",
                    action_key="control.action.dispatch",
                    action_args=_minion_interaction_action_payload(
                        action_kind="minion_plan_accept_override",
                        target_id=target_id,
                        args={
                            "work_order_id": work_order_id,
                            "plan_ref": dict(plan_ref),
                            "review_gate_ref": dict(review_gate_ref),
                            "reason": "human accepted reviewer-passed plan from channel interaction",
                        },
                    ),
                ),
                InteractionButtonSpec(
                    label="Reject Plan",
                    action_key="control.action.dispatch",
                    action_args=_minion_interaction_action_payload(
                        action_kind="minion_plan_reject",
                        target_id=target_id,
                        args={
                            "work_order_id": work_order_id,
                            "plan_ref": dict(plan_ref),
                            "review_gate_ref": dict(review_gate_ref),
                            "reason": "human rejected reviewer-passed plan from channel interaction",
                        },
                    ),
                ),
                InteractionButtonSpec(
                    label="Edit Plan",
                    action_key="control.action.dispatch",
                    action_args=_minion_interaction_action_payload(
                        action_kind="minion_plan_edit",
                        target_id=target_id,
                        args={
                            "work_order_id": work_order_id,
                            "plan_ref": dict(plan_ref),
                            "review_gate_ref": dict(review_gate_ref),
                            "reason": "human requested edits for reviewer-passed plan from channel interaction",
                        },
                    ),
                ),
            ),
        ),
    )


def build_minion_module_continue_interaction(payload: dict[str, Any], route: ControlRoute) -> InteractionMessageSpec | None:
    if not bool(payload.get("has_next_module")):
        return None
    work_order_id = str(payload.get("parent_work_order_id") or payload.get("work_order_id") or "").strip()
    if not work_order_id:
        return None
    module_id = str(payload.get("module_id") or "").strip()
    next_module_id = str(payload.get("next_module_id") or "").strip()
    milestone_index = payload.get("parent_milestone_index")
    interaction_id = f"minion_continue_{work_order_id}"
    lines = [
        "Minion milestone completed.",
        "",
        f"Work order: {work_order_id}",
    ]
    if module_id:
        lines.append(f"Completed module: {module_id}")
    if milestone_index is not None:
        lines.append(f"Completed milestone: {milestone_index}")
    summary = str(payload.get("summary") or "").strip()
    if summary:
        lines.extend(["", "Summary:", _truncate_text(summary, 900)])
    if next_module_id:
        lines.extend(["", f"Next module: {next_module_id}"])
    lines.append("")
    lines.append("Continue to the next milestone?")

    def action_args(action_kind: str, *, reason: str = "") -> dict[str, Any]:
        return {
            "action_kind": action_kind,
            "target_scope": "minion",
            "target_id": work_order_id,
            "args": {
                "work_order_id": work_order_id,
                "module_id": module_id,
                "next_module_id": next_module_id,
                "parent_milestone_index": milestone_index,
                "reason": reason,
            },
        }

    return InteractionMessageSpec(
        interaction_id=interaction_id,
        interaction_kind="minion_module_continue",
        route=route,
        text="\n".join(lines),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Continue",
                    action_key="control.action.dispatch",
                    action_args=action_args("minion_plan_continue"),
                ),
                InteractionButtonSpec(
                    label="Pause",
                    action_key="control.action.dispatch",
                    action_args=action_args("minion_plan_pause", reason="user paused at module milestone boundary"),
                ),
            ),
            (
                InteractionButtonSpec(
                    label="Finish",
                    action_key="control.action.dispatch",
                    action_args=action_args("minion_plan_finish", reason="user finished at module milestone boundary"),
                ),
            ),
        ),
    )


def build_minion_lesson_approval_interaction(
    payload: dict[str, Any],
    route: ControlRoute,
) -> InteractionMessageSpec | None:
    task_lessons = _string_list(payload.get("task_lessons"))
    system_lessons = _string_list(payload.get("system_lessons"))
    if not task_lessons and not system_lessons:
        return None
    run_id = str(payload.get("run_id") or "")
    work_order_id = str(payload.get("work_order_id") or "")
    interaction_id = f"minion_lesson_{run_id or uuid4().hex[:12]}"
    lines = [
        "Minion proposed reusable lessons.",
        "",
        "Absorb these into Pal memory?",
    ]
    if task_lessons:
        lines.append("")
        lines.append("Task lessons:")
        lines.extend(f"- {lesson}" for lesson in task_lessons)
    if system_lessons:
        lines.append("")
        lines.append("System lessons:")
        lines.extend(f"- {lesson}" for lesson in system_lessons)
    base_args = {
        "work_order_id": work_order_id,
        "run_id": run_id,
        "minion_id": str(payload.get("minion_id") or ""),
        "task_lessons": task_lessons,
        "system_lessons": system_lessons,
    }

    def action_args(decision: str) -> dict[str, Any]:
        return {
            "action_kind": "minion_lesson_decision",
            "target_scope": "minion",
            "target_id": work_order_id,
            "args": {**base_args, "decision": decision},
        }

    return InteractionMessageSpec(
        interaction_id=interaction_id,
        interaction_kind="minion_lesson_approval",
        route=route,
        text="\n".join(lines),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Accept",
                    action_key="control.action.dispatch",
                    action_args=action_args("accept"),
                ),
                InteractionButtonSpec(
                    label="Reject",
                    action_key="control.action.dispatch",
                    action_args=action_args("reject"),
                ),
                InteractionButtonSpec(
                    label="Edit",
                    action_key="control.action.dispatch",
                    action_args=action_args("edit"),
                ),
            ),
        ),
    )


def build_memory_candidate_approval_interaction(
    payload: dict[str, Any],
    route: ControlRoute,
) -> InteractionMessageSpec | None:
    memory_candidates = _dict_list(payload.get("memory_candidates"))
    if not memory_candidates:
        return None
    source_kind = str(payload.get("source_kind") or "pal").strip() or "pal"
    source_ref = str(payload.get("source_ref") or payload.get("work_order_id") or payload.get("run_id") or "").strip()
    source_label = str(payload.get("source_label") or "").strip()
    candidate_batch_id = str(payload.get("candidate_batch_id") or source_ref or uuid4().hex[:12]).strip()
    interaction_id = f"memory_candidate_{candidate_batch_id}"
    source_name = "Minion" if source_kind == "minion" else "Pal"
    lines = [
        f"{source_name} proposed durable memory candidates.",
        "",
        "Save these into Pal memory?",
    ]
    if source_label:
        lines.extend(["", f"Source: {source_label}"])
    elif source_ref:
        lines.extend(["", f"Source: {source_kind}:{source_ref}"])
    lines.append("")
    lines.append("Memory candidates:")
    lines.extend(f"- {_memory_candidate_preview(candidate)}" for candidate in memory_candidates)
    base_args = {
        "source_kind": source_kind,
        "source_ref": source_ref,
        "source_label": source_label,
        "memory_candidates": memory_candidates,
    }

    def action_args(decision: str) -> dict[str, Any]:
        return {
            "action_kind": "memory_candidate_decision",
            "target_scope": "memory",
            "target_id": candidate_batch_id,
            "args": {**base_args, "decision": decision},
        }

    return InteractionMessageSpec(
        interaction_id=interaction_id,
        interaction_kind="memory_candidate_approval",
        route=route,
        text="\n".join(lines),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Accept",
                    action_key="control.action.dispatch",
                    action_args=action_args("accept"),
                ),
                InteractionButtonSpec(
                    label="Reject",
                    action_key="control.action.dispatch",
                    action_args=action_args("reject"),
                ),
                InteractionButtonSpec(
                    label="Edit",
                    action_key="control.action.dispatch",
                    action_args=action_args("edit"),
                ),
            ),
        ),
    )


def minion_question_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    interaction = build_minion_question_interaction(payload, route)
    if interaction is None:
        return None
    return delivery_for_interaction(route, "interactive_open", interaction)


def minion_question_update_delivery(payload: dict[str, Any], route: ControlRoute) -> ControlDelivery | None:
    interaction = build_minion_question_interaction(payload, route)
    if interaction is None:
        return None
    return delivery_for_interaction(route, "interactive_update", interaction)


def minion_question_resolve_delivery(payload: dict[str, Any], route: ControlRoute, text: str) -> ControlDelivery | None:
    session = minion_question_session(payload)
    interaction_id = str(session.get("interaction_id") or "").strip()
    if not interaction_id:
        return None
    interaction = InteractionMessageSpec(
        interaction_id=interaction_id,
        interaction_kind="minion_question",
        route=route,
        text=_truncate_text(text, _INTERACTION_MESSAGE_SAFE_LIMIT),
        buttons=(),
    )
    return delivery_for_interaction(route, "interactive_resolve", interaction)


def build_minion_question_interaction(payload: dict[str, Any], route: ControlRoute) -> InteractionMessageSpec | None:
    session = minion_question_session(payload)
    questions = _dict_list(session.get("questions"))[:3]
    if not questions:
        return None
    if minion_question_ready(session) and bool(session.get("review")):
        return _build_minion_question_review_interaction(session, route)
    current_index = _clamp_int(session.get("current_index"), minimum=0, maximum=len(questions) - 1)
    current_question = dict(questions[current_index])
    answers = _answer_map(session.get("answers"))
    required_ids = _required_question_ids(questions)
    answered_required = sum(1 for question_id in required_ids if question_id in answers)
    question_id = _question_id(current_question, current_index)
    selected_answer = answers.get(question_id, {})
    lines = [
        "Planner needs input.",
        "",
        f"Question {current_index + 1}/{len(questions)}",
    ]
    question_text = str(current_question.get("question") or "").strip()
    if question_text:
        lines.append(question_text)
    why_needed = str(current_question.get("why_needed") or "").strip()
    if why_needed:
        lines.extend(["", f"Why: {why_needed}"])
    evidence = _question_evidence_preview(current_question.get("evidence"))
    if evidence:
        lines.append("")
        lines.append("Evidence:")
        lines.extend(f"- {item}" for item in evidence)
    options = _dict_list(current_question.get("options"))[:3]
    selected_option_id = str(selected_answer.get("selected_option_id") or "").strip()
    if options:
        lines.append("")
        lines.append("Options:")
        for option_index, option in enumerate(options, start=1):
            option_id = _option_id(option, option_index)
            full_label = _option_label(option, option_index)
            description = str(option.get("description") or option.get("summary") or "").strip()
            option_text = f"{full_label}: {description}" if description and description != full_label else full_label
            marker = ">" if option_id == selected_option_id else " "
            lines.append(f"{marker} {option_index}. {option_text}")
    lines.append("")
    lines.append(f"Answered: {answered_required}/{len(required_ids)}")

    rows: list[tuple[InteractionButtonSpec, ...]] = []
    option_buttons: list[InteractionButtonSpec] = []
    for option_index, option in enumerate(options, start=1):
        option_id = _option_id(option, option_index)
        full_label = _option_label(option, option_index)
        selected = option_id == selected_option_id
        option_buttons.append(
            InteractionButtonSpec(
                label=_question_option_button_label(full_label, selected=selected),
                action_key="control.action.dispatch",
                action_args=_minion_question_action_payload(
                    session,
                    action_kind="minion_question_select",
                    current_index=current_index,
                    extra={
                        "question_id": question_id,
                        "selected_option_id": option_id,
                        "answer": full_label,
                    },
                ),
            )
        )
    if option_buttons:
        rows.append(tuple(option_buttons))
    nav_buttons: list[InteractionButtonSpec] = []
    if current_index > 0:
        nav_buttons.append(
            InteractionButtonSpec(
                label="Back",
                action_key="control.action.dispatch",
                action_args=_minion_question_action_payload(
                    session,
                    action_kind="minion_question_nav",
                    current_index=current_index,
                    extra={"target_index": current_index - 1},
                ),
            )
        )
    if current_index < len(questions) - 1:
        nav_buttons.append(
            InteractionButtonSpec(
                label="Next",
                action_key="control.action.dispatch",
                action_args=_minion_question_action_payload(
                    session,
                    action_kind="minion_question_nav",
                    current_index=current_index,
                    extra={"target_index": current_index + 1},
                ),
            )
        )
    if nav_buttons:
        rows.append(tuple(nav_buttons))
    if not rows:
        return None
    return InteractionMessageSpec(
        interaction_id=str(session.get("interaction_id") or ""),
        interaction_kind="minion_question",
        route=route,
        text=_truncate_text("\n".join(lines), _INTERACTION_MESSAGE_SAFE_LIMIT),
        buttons=tuple(rows),
    )


def minion_question_session(payload: dict[str, Any]) -> dict[str, Any]:
    raw = dict(payload or {})
    question_payload = raw.get("ask_user_question")
    if isinstance(question_payload, dict):
        raw = {**raw, **dict(question_payload)}
    run_id = str(raw.get("run_id") or "").strip()
    clarification_id = str(raw.get("clarification_id") or raw.get("approval_id") or "").strip()
    if not clarification_id:
        clarification_id = f"clarify_{run_id or uuid4().hex[:12]}"
    questions = _dict_list(raw.get("questions"))[:3]
    answers = _answer_map(raw.get("answers"))
    return {
        "clarification_id": clarification_id,
        "interaction_id": str(raw.get("interaction_id") or f"minion_question_{clarification_id}"),
        "run_id": run_id,
        "minion_id": str(raw.get("minion_id") or "").strip(),
        "work_order_id": str(raw.get("work_order_id") or "").strip(),
        "task_id": str(raw.get("task_id") or "").strip(),
        "turn_index": raw.get("turn_index", 0),
        "plan_revision": raw.get("plan_revision", 0),
        "plan_draft_id": str(raw.get("plan_draft_id") or "").strip(),
        "questions": questions,
        "answers": answers,
        "current_index": _clamp_int(raw.get("current_index"), minimum=0, maximum=max(len(questions) - 1, 0)),
        "review": bool(raw.get("review")),
    }


def minion_question_session_with_selection(session: dict[str, Any], *, question_id: str, selected_option_id: str, answer: str) -> dict[str, Any]:
    updated = dict(session or {})
    answers = _answer_map(updated.get("answers"))
    normalized_question_id = str(question_id or "").strip()
    if normalized_question_id:
        answers[normalized_question_id] = {
            "question_id": normalized_question_id,
            "selected_option_id": str(selected_option_id or "").strip(),
            "answer": str(answer or "").strip(),
            "run_id": str(updated.get("run_id") or ""),
            "minion_id": str(updated.get("minion_id") or ""),
            "turn_index": updated.get("turn_index", 0),
            "plan_revision": updated.get("plan_revision", 0),
        }
    updated["answers"] = answers
    questions = _dict_list(updated.get("questions"))[:3]
    required_ids = _required_question_ids(questions)
    if all(question_id in answers for question_id in required_ids):
        updated["ready"] = True
        updated["review"] = True
        return updated
    current_index = _clamp_int(updated.get("current_index"), minimum=0, maximum=max(len(questions) - 1, 0))
    for index in range(current_index + 1, len(questions)):
        if _question_id(questions[index], index) in required_ids and _question_id(questions[index], index) not in answers:
            updated["current_index"] = index
            return updated
    for index, question in enumerate(questions):
        if _question_id(question, index) in required_ids and _question_id(question, index) not in answers:
            updated["current_index"] = index
            return updated
    return updated


def minion_question_answers(session: dict[str, Any]) -> list[dict[str, Any]]:
    answers = _answer_map((session or {}).get("answers"))
    questions = _dict_list((session or {}).get("questions"))[:3]
    result: list[dict[str, Any]] = []
    for index, question in enumerate(questions):
        question_id = _question_id(question, index)
        answer = dict(answers.get(question_id) or {})
        if not answer:
            continue
        answer.setdefault("question_id", question_id)
        answer.setdefault("question", str(question.get("question") or "").strip())
        result.append(answer)
    return result


def minion_question_ready(session: dict[str, Any]) -> bool:
    questions = _dict_list((session or {}).get("questions"))[:3]
    answers = _answer_map((session or {}).get("answers"))
    required_ids = _required_question_ids(questions)
    return bool(required_ids) and all(question_id in answers for question_id in required_ids)


def _question_evidence_preview(value: Any) -> list[str]:
    if isinstance(value, str):
        rendered = " ".join(value.split())
        return [_truncate_text(rendered, _MINION_QUESTION_EVIDENCE_ITEM_LIMIT)] if rendered else []
    items = _dict_list(value)
    if not items:
        return []
    rendered_items: list[str] = []
    for item in items:
        file_name = str(item.get("file") or item.get("path") or "").strip()
        finding = str(item.get("finding") or item.get("summary") or item.get("text") or "").strip()
        rendered = f"{file_name}: {finding}" if file_name and finding else file_name or finding
        rendered = " ".join(rendered.split())
        if rendered:
            rendered_items.append(_truncate_text(rendered, _MINION_QUESTION_EVIDENCE_ITEM_LIMIT))
    return rendered_items


def _question_option_button_label(label: str, *, selected: bool) -> str:
    prefix = "> " if selected else ""
    text = " ".join(str(label or "").split()) or "Option"
    limit = max(8, _MINION_QUESTION_BUTTON_LABEL_LIMIT - len(prefix))
    if len(text) > limit:
        text = f"{text[: max(0, limit - 3)].rstrip()}..."
    return f"{prefix}{text}"


def _build_minion_question_review_interaction(session: dict[str, Any], route: ControlRoute) -> InteractionMessageSpec | None:
    questions = _dict_list(session.get("questions"))[:3]
    answers = _answer_map(session.get("answers"))
    if not questions:
        return None
    lines = [
        "Planner needs input.",
        "",
        "Review answers",
    ]
    for index, question in enumerate(questions):
        question_id = _question_id(question, index)
        answer = answers.get(question_id, {})
        question_text = " ".join(str(question.get("question") or f"Question {index + 1}").split())
        answer_text = _question_answer_text(question, answer)
        lines.append("")
        lines.append(f"{index + 1}. {question_text}")
        lines.append(f"> {answer_text or '-'}")
    edit_args = _minion_question_action_payload(
        session,
        action_kind="minion_question_nav",
        current_index=0,
        extra={"target_index": 0, "review": False},
    )
    submit_args = _minion_question_action_payload(
        session,
        action_kind="minion_question_submit",
        current_index=_clamp_int(session.get("current_index"), minimum=0, maximum=max(len(questions) - 1, 0)),
    )
    return InteractionMessageSpec(
        interaction_id=str(session.get("interaction_id") or ""),
        interaction_kind="minion_question",
        route=route,
        text=_truncate_text("\n".join(lines), _INTERACTION_MESSAGE_SAFE_LIMIT),
        buttons=(
            (
                InteractionButtonSpec(
                    label="Edit",
                    action_key="control.action.dispatch",
                    action_args=edit_args,
                ),
                InteractionButtonSpec(
                    label="Submit",
                    action_key="control.action.dispatch",
                    action_args=submit_args,
                ),
            ),
        ),
    )


def _question_answer_text(question: dict[str, Any], answer: dict[str, Any]) -> str:
    explicit = str(answer.get("answer") or "").strip()
    if explicit:
        return explicit
    selected = str(answer.get("selected_option_id") or "").strip()
    for index, option in enumerate(_dict_list(question.get("options"))[:3], start=1):
        if _option_id(option, index) == selected:
            return _option_label(option, index)
    return selected


def _minion_question_action_payload(
    session: dict[str, Any],
    *,
    action_kind: str,
    current_index: int,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    args = {
        "clarification_id": str(session.get("clarification_id") or ""),
        "interaction_id": str(session.get("interaction_id") or ""),
        "work_order_id": str(session.get("work_order_id") or ""),
        "run_id": str(session.get("run_id") or ""),
        "minion_id": str(session.get("minion_id") or ""),
        "turn_index": session.get("turn_index", 0),
        "plan_revision": session.get("plan_revision", 0),
        "questions": _dict_list(session.get("questions"))[:3],
        "answers": _answer_map(session.get("answers")),
        "current_index": int(current_index),
        "review": bool(session.get("review")),
    }
    args.update(dict(extra or {}))
    return _minion_interaction_action_payload(
        action_kind=action_kind,
        target_id=str(session.get("clarification_id") or ""),
        args=args,
    )


def _answer_map(value: Any) -> dict[str, dict[str, Any]]:
    if isinstance(value, dict):
        return {str(key): dict(item) for key, item in value.items() if str(key).strip() and isinstance(item, dict)}
    if isinstance(value, (list, tuple)):
        result: dict[str, dict[str, Any]] = {}
        for item in value:
            if not isinstance(item, dict):
                continue
            question_id = str(item.get("question_id") or "").strip()
            if question_id:
                result[question_id] = dict(item)
        return result
    return {}


def _required_question_ids(questions: list[dict[str, Any]]) -> list[str]:
    return [
        _question_id(question, index)
        for index, question in enumerate(questions)
        if question.get("blocking") is not False
    ]


def _question_id(question: dict[str, Any], index: int) -> str:
    return str(question.get("question_id") or f"q{index + 1}").strip()


def _option_id(option: dict[str, Any], index: int) -> str:
    return str(option.get("id") or option.get("option_id") or f"option_{index}").strip()


def _option_label(option: dict[str, Any], index: int) -> str:
    return str(option.get("label") or _option_id(option, index)).strip() or f"Option {index}"


def _clamp_int(value: Any, *, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, (list, tuple)):
        values = list(value)
    else:
        values = []
    return _dedupe_nonempty([str(item) for item in values])


def _dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _memory_candidate_preview(candidate: dict[str, Any]) -> str:
    title = " ".join(str(candidate.get("title") or "").split())
    summary = " ".join(str(candidate.get("summary") or candidate.get("search_text") or "").split())
    if title and summary and summary != title:
        text = f"{title}: {summary}"
    else:
        text = title or summary or "Untitled memory candidate"
    if len(text) > _MINION_MEMORY_CANDIDATE_PREVIEW_LIMIT:
        return f"{text[: _MINION_MEMORY_CANDIDATE_PREVIEW_LIMIT - 3].rstrip()}..."
    return text


def _dedupe_nonempty(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        item = " ".join(str(value or "").split())
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
