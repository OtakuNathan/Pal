"""LLM-owned command and interaction parsing."""
from __future__ import annotations

from pal.control.contracts import (
    ControlAction,
    ControlCommandInvocation,
    ControlCommandSpec,
    ControlInteractionSpec,
    InteractionResult,
)


def _handle_think(invocation: ControlCommandInvocation) -> ControlAction:
    if not invocation.argv:
        return ControlAction(action_kind="show_think", target_scope="runtime", route=invocation.route)
    requested = invocation.argv[0].strip().lower()
    if not requested:
        return ControlAction(
            action_kind="invalid_command", target_scope="control", route=invocation.route,
            notes="Think level is required.",
        )
    return ControlAction(
        action_kind="set_think", target_scope="runtime", route=invocation.route,
        args={"think_level": requested},
    )


def _handle_model(invocation: ControlCommandInvocation) -> ControlAction:
    if not invocation.argv:
        return ControlAction(action_kind="show_model", target_scope="runtime", route=invocation.route)
    operation = invocation.argv[0].lower()
    if operation in {"confirm", "think", "cancel"} and len(invocation.argv) > 1:
        expected = 3 if operation == "think" else 2
        if len(invocation.argv) != expected:
            return ControlAction(
                action_kind="invalid_command", target_scope="control", route=invocation.route,
                notes="Use /model confirm|cancel <request_id> or /model think <request_id> <level>.",
            )
        return ControlAction(
            action_kind="model_switch_step", target_scope="runtime", route=invocation.route,
            args={"operation": operation, "request_id": invocation.argv[1],
                  "think_level": invocation.argv[2] if operation == "think" else ""},
        )
    endpoint_id = invocation.argv[0].strip()
    if not endpoint_id:
        return ControlAction(
            action_kind="invalid_command", target_scope="control", route=invocation.route,
            notes="Use /model <endpoint_id>.",
        )
    return ControlAction(
        action_kind="set_model", target_scope="runtime", route=invocation.route,
        args={"endpoint_id": endpoint_id},
    )


def _handle_refresh_llm_endpoint(invocation: ControlCommandInvocation) -> ControlAction:
    return ControlAction(action_kind="refresh_llm_endpoint", target_scope="runtime", route=invocation.route)


def command_specs() -> tuple[ControlCommandSpec, ...]:
    return (
        ControlCommandSpec(
            name="think", handler=_handle_think,
            description="Show or update the think level for future turns.", usage="/think [level]",
            show_in_panel=True, panel_button=True, panel_label="Think",
            interaction_action_key="control.think.open",
        ),
        ControlCommandSpec(
            name="model", handler=_handle_model,
            description="Show or update the active LLM model for future turns.", usage="/model [endpoint_id]",
            show_in_panel=True, panel_button=True, panel_label="Model",
            interaction_action_key="control.model.open",
        ),
        ControlCommandSpec(
            name="refresh_llm_endpoint", handler=_handle_refresh_llm_endpoint,
            aliases=("refresh_llm_endpoints", "llm_refresh"),
            description="Refresh LLM endpoint topology from the local database for future turns.",
            usage="/refresh_llm_endpoint", show_in_panel=True, panel_button=True, panel_label="Refresh LLM",
        ),
    )


def parse_interaction(result: InteractionResult) -> ControlAction | None:
    key = result.action_key
    if key == "control.think.open":
        return ControlAction(action_kind="show_think", target_scope="runtime", route=result.route)
    if key == "control.model.open":
        return ControlAction(action_kind="show_model", target_scope="runtime", route=result.route)
    if key == "control.think.set":
        requested = str(result.action_args.get("think_level") or "").strip().lower()
        if not requested:
            return ControlAction(
                action_kind="invalid_command", target_scope="control", route=result.route,
                notes="Think level is required.",
            )
        return ControlAction(
            action_kind="set_think", target_scope="runtime", route=result.route,
            args={"think_level": requested},
        )
    if key in {"control.model.confirm", "control.model.think", "control.model.cancel"}:
        return ControlAction(
            action_kind="model_switch_step", target_scope="runtime", route=result.route,
            args={"operation": key.rsplit(".", 1)[-1],
                  "request_id": str(result.action_args.get("request_id") or ""),
                  "think_level": str(result.action_args.get("think_level") or "")},
        )
    if key == "control.model.set":
        endpoint_id = str(result.action_args.get("endpoint_id") or "").strip()
        if not endpoint_id:
            return ControlAction(
                action_kind="invalid_command", target_scope="control", route=result.route,
                notes="Model endpoint id is required.",
            )
        return ControlAction(
            action_kind="set_model", target_scope="runtime", route=result.route,
            args={"endpoint_id": endpoint_id},
        )
    return None


def interaction_specs() -> tuple[ControlInteractionSpec, ...]:
    return tuple(ControlInteractionSpec(key, parse_interaction) for key in (
        "control.think.open", "control.model.open", "control.think.set",
        "control.model.confirm", "control.model.think", "control.model.cancel", "control.model.set",
    ))
