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
        if not spec.panel_button:
            continue
        action_key = str(spec.interaction_action_key or "").strip() or "control.command.run"
        action_args = {"command_name": spec.name} if action_key == "control.command.run" else {}
        rows.append(
            (
                InteractionButtonSpec(
                    label=str(spec.panel_label or spec.name),
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
