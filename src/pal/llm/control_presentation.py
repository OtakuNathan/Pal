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

def render_model_status_text(endpoints: Sequence[LLMEndpointModel], active_endpoint_id: str | None) -> str:
    active = str(active_endpoint_id or "").strip()
    if not endpoints:
        return "No enabled LLM model endpoints are available."
    lines = [
        f"Model: {active or '-'}",
        "Select an endpoint for new turns.",
        "Switching requires an idle conversation. Incompatible reasoning requires confirmed Compact first.",
        "",
        "Models:",
    ]
    for endpoint in endpoints:
        endpoint_id = endpoint.endpoint_id
        marker = ">" if endpoint_id == active else " "
        lines.append(f"{marker} {_format_model_endpoint(endpoint)}")
    return "\n".join(lines).rstrip()

def model_switch_delivery(request: PendingControlRequest, think_status: dict[str, Any]) -> ControlDelivery:
    target = request.payload["target_id"]
    token = request.request_id
    if request.payload["stage"] == "confirm":
        text = (
            f"Switch to {target}?\n"
            "The current reasoning/replay is incompatible or the target context is too small. "
            "Pal must Compact on the current model before switching. Compact summarizes history "
            "and may lose details or reduce subsequent task performance. The new model's cache "
            "must be rebuilt; initial requests may be slower or cost more.\n"
            f"Continue: /model confirm {token}"
        )
        buttons = [(InteractionButtonSpec("Continue to thinking level", "control.model.confirm", {"request_id": token}),)]
    else:
        text = f"Select thinking level for {target}. The active model has not changed."
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
            text += f"\n/model think {token} {level}"
    text += f"\nCancel: /model cancel {token}"
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
        text = f"{banner}\n\n{text}"
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

def _format_model_endpoint(endpoint: LLMEndpointModel) -> str:
    endpoint_id = endpoint.endpoint_id
    model_id = endpoint.model_id
    display_name = endpoint.display_name
    provider = endpoint.provider
    wire_shape = endpoint.wire_shape
    context_window = endpoint.context_window
    max_output_tokens = endpoint.max_output_tokens
    label = endpoint_id
    if display_name and display_name != endpoint_id:
        label = f"{display_name} ({endpoint_id})"
    details: list[str] = []
    if model_id and model_id not in {endpoint_id, display_name}:
        details.append(f"model={model_id}")
    if provider or wire_shape:
        details.append("/".join(item for item in (provider, wire_shape) if item))
    if context_window:
        details.append(f"context={context_window}")
    if max_output_tokens:
        details.append(f"max_output={max_output_tokens}")
    if not details:
        return label
    return f"{label} [{', '.join(details)}]"

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
