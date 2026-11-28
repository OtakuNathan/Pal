from __future__ import annotations

from typing import TYPE_CHECKING

from pal.control.contracts import (
    ControlDelivery,
    ControlRoute,
    InteractionButtonSpec,
    InteractionMessageSpec,
)

from pal.control.interactions import (
    control_panel_interaction_id,
    delivery_for_reply,
    delivery_for_interaction,
)
from pal.control.presentation import markdown_literal

if TYPE_CHECKING:
    from pal.core.contracts import PendingControlRequest


def render_log_status_text(enabled: bool) -> str:
    status = "on" if enabled else "off"
    return (
        f"Debug log: {status}\n"
        "Use /log start or /log end. Changes apply to new turns and Bunshin role runs only."
    )

def build_log_panel_interaction(
    route: ControlRoute,
    enabled: bool,
    *,
    banner: str | None = None,
) -> InteractionMessageSpec:
    start_label = "> Start logging" if enabled else "Start logging"
    end_label = "Stop logging" if enabled else "> Stop logging"
    text = (
        f"**Debug log:** **{'on' if enabled else 'off'}**\n\n"
        "Use /log start or /log end. Changes apply to new turns and Bunshin role runs only."
    )
    if banner:
        text = f"{markdown_literal(banner)}\n\n{text}"
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

def build_reset_confirm_interaction(request: PendingControlRequest) -> InteractionMessageSpec:
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
        expires_at=request.expires_at.isoformat(),
    )

def reset_confirm_text() -> str:
    return (
        "Reset working memory for this scope?\n"
        "This clears L1, L2, and conversation-facing projection only.\n"
        "Durable L3 memory stays intact."
    )

def reset_confirm_fallback_text(request: PendingControlRequest) -> str:
    return f"{reset_confirm_text()}\nConfirm with /reset confirm {request.request_id}"

def reset_confirm_delivery(request: PendingControlRequest, *, already_opened: bool) -> ControlDelivery:
    return delivery_for_interaction(
        request.route,
        "interactive_update" if already_opened else "interactive_open",
        build_reset_confirm_interaction(request),
        text=reset_confirm_fallback_text(request),
    )
