"""Control owns its panel and protocol error replies."""
from __future__ import annotations

from typing import TYPE_CHECKING

from pal.control.contracts import (
    ControlAction,
    ControlCommandInvocation,
    ControlCommandSpec,
    ControlInteractionSpec,
    InteractionResult,
)
from pal.control.interactions import control_panel_delivery
from pal.control.runtime_contracts import CORE_CONTROL
from pal.core.control_action_registry import ControlActionSpec

if TYPE_CHECKING:
    from pal.control.service import ControlPlane
    from pal.core.main_context import MainContext


def _handle_control(invocation: ControlCommandInvocation) -> ControlAction:
    return ControlAction(action_kind="show_panel", target_scope="control", route=invocation.route)


def command_specs() -> tuple[ControlCommandSpec, ...]:
    return (ControlCommandSpec(
        name="control", handler=_handle_control,
        description="Show the control panel and command help.", usage="/control",
        show_in_panel=True, panel_label="Control",
    ),)


def parse_interaction(result: InteractionResult) -> ControlAction | None:
    if result.action_key in {"control.panel.show", "control.panel.back"}:
        return ControlAction(action_kind="show_panel", target_scope="control", route=result.route)
    return None


def interaction_specs() -> tuple[ControlInteractionSpec, ...]:
    return tuple(ControlInteractionSpec(key, parse_interaction) for key in (
        "control.panel.show", "control.panel.back",
    ))


class ControlCommands:
    def __init__(self, context: MainContext, plane: ControlPlane) -> None:
        self._core = context.port_registry.observe(CORE_CONTROL)
        self._plane = plane

    async def show_panel(self, action: ControlAction) -> None:
        if action.route is not None:
            await self._core.current.publish_catalog(endpoint_id=action.route.endpoint_id)
        await self._core.current.deliver(control_panel_delivery(self._plane, action.route))

    async def publish_catalog(self, action: ControlAction) -> None:
        await self._core.current.publish_catalog(endpoint_id=action.args.get("endpoint_id"))

    async def invalid(self, action: ControlAction) -> None:
        await self._core.current.reply(action, action.notes or "Invalid command.")

    async def unknown(self, action: ControlAction) -> None:
        name = str(action.args.get("command_name") or "").strip()
        await self._core.current.reply(action, f"Unknown command: /{name}" if name else "Unknown command.")

    def handlers(self) -> dict[str, ControlActionSpec]:
        return {
            "show_panel": ControlActionSpec(self.show_panel, allow_during_maintenance=True),
            "publish_control_catalog": ControlActionSpec(self.publish_catalog, allow_during_maintenance=True),
            "invalid_command": ControlActionSpec(self.invalid),
            "unknown_command": ControlActionSpec(self.unknown),
        }
