from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

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
    from pal.llm.models import LLMEndpointModel


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
        mode = think_status.get("effort_update_mode")
        if mode:
            timing = ("Changes take effect after the next successful compact of a live prefix."
                      if mode == "compact" else "Effort changes append at new user turns; mode changes wait for compact.")
            text = f"# Thinking level\nModel: {markdown_literal(endpoint_id)}\nRequested: **{markdown_literal(current)}**\n\n{timing}"
        else:
            text = f"# Thinking level\nModel: {markdown_literal(endpoint_id)}\nCurrent: **{markdown_literal(current)}**\n\nSelect a level for new turns."
    else:
        text = f"# Thinking level\nEndpoint {markdown_literal(endpoint_id)} does not expose configurable thinking levels."
    if banner:
        text = f"{markdown_literal(banner)}\n\n{text}"
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
        label = "Requested think level" if think_status.get("effort_update_mode") else "Think level"
        return delivery_for_reply(
            None,
            f"{label} for {endpoint_id}: {current}\nAvailable levels: {available}",
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

def model_switch_delivery(request: PendingControlRequest, think_status: dict[str, Any]) -> ControlDelivery:
    target = request.payload["target_id"]
    token = request.request_id
    if request.payload["stage"] == "confirm":
        text = (
            f"# Switch to {markdown_literal(target)}?\n\n"
            "The current reasoning/replay is incompatible or the target context is too small. "
            "Pal must Compact on the current model before switching. Compact summarizes history "
            "and may lose details or reduce subsequent task performance. The new model's cache "
            "must be rebuilt; initial requests may be slower or cost more.\n"
            f"\nContinue: /model confirm {markdown_literal(token)}"
        )
        buttons = [(InteractionButtonSpec("Continue to thinking level", "control.model.confirm", {"request_id": token}),)]
    else:
        text = f"# Select thinking level for {markdown_literal(target)}\n\nThe active model has not changed."
        if request.payload.get("compact_confirmed"):
            text += "\nCompact will run on the current model after you select a thinking level."
        else:
            text += "\nThe current history is compatible; Compact is not required."
        buttons = []
        for choice in think_status.get("choices", ()):
            level = choice["id"]
            buttons.append((InteractionButtonSpec(
                str(choice.get("label") or level), "control.model.think",
                {"request_id": token, "think_level": level},
            ),))
            text += f"\n/model think {markdown_literal(token)} {markdown_literal(level)}"
    text += f"\n\nCancel: /model cancel {markdown_literal(token)}"
    buttons.append((InteractionButtonSpec("Back to models", "control.model.cancel", {"request_id": token}),))
    spec = InteractionMessageSpec(
        interaction_id=control_panel_interaction_id(request.route),
        interaction_kind="control_panel", route=request.route,
        text=text, buttons=tuple(buttons), expires_at=request.expires_at.isoformat(),
        revision=f"{token}:{request.payload['stage']}",
    )
    return delivery_for_interaction(request.route, "interactive_update", spec)

def build_model_panel_interaction(
    route: ControlRoute,
    endpoints: Sequence[LLMEndpointModel],
    active_endpoint_id: str | None,
    *,
    banner: str | None = None,
) -> InteractionMessageSpec:
    active = str(active_endpoint_id or "").strip()
    text = render_model_status_text(endpoints, active)
    if banner:
        text = f"{markdown_literal(banner)}\n\n{text}"
    rows: list[tuple[InteractionButtonSpec, ...]] = []
    for endpoint in endpoints:
        endpoint_id = endpoint.endpoint_id
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
    endpoints: Sequence[LLMEndpointModel],
    active_endpoint_id: str | None,
) -> ControlDelivery:
    if route is None:
        return delivery_for_reply(None, render_model_status_text(endpoints, active_endpoint_id))
    return delivery_for_interaction(
        route,
        "interactive_update",
        build_model_panel_interaction(route, endpoints, active_endpoint_id),
    )


def render_model_status_text(endpoints: Sequence[LLMEndpointModel], active_endpoint_id: str | None) -> str:
    active = str(active_endpoint_id or "").strip()
    if not endpoints:
        return "# Model\nNo enabled LLM model endpoints are available."
    lines = [
        "# Model",
        f"Active: **{markdown_literal(active or '-')}**",
        "",
        "Select an endpoint for new turns.",
        "Switching requires an idle conversation. Incompatible reasoning requires confirmed Compact first.",
    ]
    groups: dict[str, list[LLMEndpointModel]] = {}
    for endpoint in endpoints:
        groups.setdefault(endpoint.provider or "Other", []).append(endpoint)
    for provider, models in groups.items():
        lines.extend(("", f"**{markdown_literal(provider)}**"))
        for endpoint in models:
            label = _format_model_button_label(endpoint, "")
            if endpoint.endpoint_id == active:
                label = f"**{markdown_literal(label)} (active)**"
            else:
                label = markdown_literal(label)
            details: list[str] = []
            if (endpoint.model_id and endpoint.model_id not in {endpoint.endpoint_id, endpoint.display_name}
                    and endpoint.endpoint_id != f"{endpoint.provider}-{endpoint.model_id}"):
                details.append(f"model={endpoint.model_id}")
            if endpoint.wire_shape:
                details.append(endpoint.wire_shape)
            if endpoint.context_window:
                details.append(f"context={endpoint.context_window:,}")
            if endpoint.max_output_tokens:
                details.append(f"max_output={endpoint.max_output_tokens:,}")
            suffix = f" — {markdown_literal(', '.join(details))}" if details else ""
            lines.append(f"• {label}{suffix}")
    return "\n".join(lines)

def _format_model_button_label(endpoint: LLMEndpointModel, active_endpoint_id: str) -> str:
    endpoint_id = endpoint.endpoint_id
    display_name = endpoint.display_name
    model_id = endpoint.model_id
    label = display_name or model_id or endpoint_id
    if label != endpoint_id:
        label = f"{label} ({endpoint_id})"
    if endpoint_id == active_endpoint_id:
        return f"> {label}"
    return label
