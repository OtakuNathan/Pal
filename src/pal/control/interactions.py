from __future__ import annotations

import hashlib
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
    think_status: dict[str, Any],
    *,
    banner: str | None = None,
    back_to_models: bool = False,
) -> InteractionMessageSpec:
    current = str(think_status.get("current") or "")
    endpoint_id = str(think_status.get("endpoint_id") or "-")
    choices = list(think_status.get("choices") or [])

    def label(choice_id: str, display: str) -> str:
        return f"> {display}" if choice_id == current else display

    if choices:
        text = f"Think level for {endpoint_id}: {current}\nSelect a level for new turns."
    else:
        text = f"Endpoint {endpoint_id} does not expose configurable thinking levels."
    if banner:
        text = f"{banner}\n\n{text}"
    choice_buttons = [
        InteractionButtonSpec(
            label=label(
                str(choice.get("id") or ""),
                str(choice.get("label") or choice.get("id") or ""),
            ),
            action_key="control.think.set",
            action_args={"think_level": str(choice.get("id") or "")},
        )
        for choice in choices
        if str(choice.get("id") or "").strip()
    ]
    rows = [
        tuple(choice_buttons[index : index + 3])
        for index in range(0, len(choice_buttons), 3)
    ]
    rows.append(
        (
            InteractionButtonSpec(
                label="Back to models" if back_to_models else "Back",
                action_key="control.model.open" if back_to_models else "control.panel.back",
            ),
        )
    )
    return InteractionMessageSpec(
        interaction_id=control_panel_interaction_id(route),
        interaction_kind="control_panel",
        route=route,
        text=text,
        buttons=tuple(rows),
        expires_at=None,
    )


def think_panel_delivery(
    route: ControlRoute | None,
    think_status: dict[str, Any],
    *,
    banner: str | None = None,
    back_to_models: bool = False,
) -> ControlDelivery:
    endpoint_id = str(think_status.get("endpoint_id") or "-")
    current = str(think_status.get("current") or "")
    choices = list(think_status.get("choices") or [])
    if route is None:
        if not choices:
            return delivery_for_reply(None, f"Endpoint {endpoint_id} does not expose configurable thinking levels.")
        available = ", ".join(str(choice.get("id") or "") for choice in choices)
        return delivery_for_reply(
            None,
            f"Think level for {endpoint_id}: {current}\nAvailable levels: {available}",
        )
    return delivery_for_interaction(
        route,
        "interactive_update",
        build_think_panel_interaction(
            route,
            think_status,
            banner=banner,
            back_to_models=back_to_models,
        ),
    )


def render_model_status_text(endpoints: list[Any] | tuple[Any, ...], active_endpoint_id: str | None) -> str:
    active = str(active_endpoint_id or "").strip()
    endpoint_items = list(endpoints or [])
    if not endpoint_items:
        return "No enabled LLM model endpoints are available."
    lines = [
        f"Model: {active or '-'}",
        "Select an endpoint for new turns.",
        "",
        "Models:",
    ]
    for endpoint in endpoint_items:
        endpoint_id = _endpoint_field(endpoint, "endpoint_id")
        marker = ">" if endpoint_id == active else " "
        lines.append(f"{marker} {_format_model_endpoint(endpoint)}")
    return "\n".join(lines).rstrip()


def build_model_panel_interaction(
    route: ControlRoute,
    endpoints: list[Any] | tuple[Any, ...],
    active_endpoint_id: str | None,
    *,
    banner: str | None = None,
) -> InteractionMessageSpec:
    active = str(active_endpoint_id or "").strip()
    endpoint_items = list(endpoints or [])
    text = render_model_status_text(endpoint_items, active)
    if banner:
        text = f"{banner}\n\n{text}"
    rows: list[tuple[InteractionButtonSpec, ...]] = []
    for endpoint in endpoint_items:
        endpoint_id = _endpoint_field(endpoint, "endpoint_id")
        if not endpoint_id:
            continue
        label = _format_model_button_label(endpoint, active)
        rows.append(
            (
                InteractionButtonSpec(
                    label=label,
                    action_key="control.model.set",
                    action_args={"endpoint_id": endpoint_id},
                ),
            )
        )
    rows.append((InteractionButtonSpec(label="Back", action_key="control.panel.back"),))
    return InteractionMessageSpec(
        interaction_id=control_panel_interaction_id(route),
        interaction_kind="control_panel",
        route=route,
        text=text,
        buttons=tuple(rows),
        expires_at=None,
    )


def model_panel_delivery(
    route: ControlRoute | None,
    endpoints: list[Any] | tuple[Any, ...],
    active_endpoint_id: str | None,
) -> ControlDelivery:
    if route is None:
        return delivery_for_reply(None, render_model_status_text(endpoints, active_endpoint_id))
    return delivery_for_interaction(
        route,
        "interactive_update",
        build_model_panel_interaction(route, endpoints, active_endpoint_id),
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


def _endpoint_field(endpoint: Any, field_name: str) -> str:
    if isinstance(endpoint, dict):
        value = endpoint.get(field_name)
    else:
        value = getattr(endpoint, field_name, None)
    return str(value or "").strip()


def _format_model_endpoint(endpoint: Any) -> str:
    endpoint_id = _endpoint_field(endpoint, "endpoint_id")
    model_id = _endpoint_field(endpoint, "model_id")
    display_name = _endpoint_field(endpoint, "display_name")
    provider = _endpoint_field(endpoint, "provider")
    api_mode = _endpoint_field(endpoint, "api_mode")
    context_window = _endpoint_field(endpoint, "context_window")
    max_output_tokens = _endpoint_field(endpoint, "max_output_tokens")
    label = endpoint_id
    if display_name and display_name != endpoint_id:
        label = f"{display_name} ({endpoint_id})"
    details: list[str] = []
    if model_id and model_id not in {endpoint_id, display_name}:
        details.append(f"model={model_id}")
    if provider or api_mode:
        details.append("/".join(item for item in (provider, api_mode) if item))
    if context_window:
        details.append(f"context={context_window}")
    if max_output_tokens:
        details.append(f"max_output={max_output_tokens}")
    if not details:
        return label
    return f"{label} [{', '.join(details)}]"


def _format_model_button_label(endpoint: Any, active_endpoint_id: str) -> str:
    endpoint_id = _endpoint_field(endpoint, "endpoint_id")
    display_name = _endpoint_field(endpoint, "display_name")
    model_id = _endpoint_field(endpoint, "model_id")
    label = display_name or model_id or endpoint_id
    if label != endpoint_id:
        label = f"{label} ({endpoint_id})"
    if endpoint_id == active_endpoint_id:
        return f"> {label}"
    return label
