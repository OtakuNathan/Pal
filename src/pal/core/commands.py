"""Core-owned orchestration commands use the same registry as other modules."""
from __future__ import annotations

from typing import TYPE_CHECKING

from pal.control.contracts import (
    ControlAction,
    ControlCommandInvocation,
    ControlCommandSpec,
    ControlInteractionSpec,
    InteractionResult,
)
from pal.core.btw import start_btw
from pal.core.control_action_registry import ControlActionSpec
from pal.execution.tool_facade import LifecycleAccess

if TYPE_CHECKING:
    from pal.core.runtime import PalCore


def _handle_btw(invocation: ControlCommandInvocation) -> ControlAction:
    parts = invocation.raw_text.lstrip().split(maxsplit=1)
    text = parts[1] if len(parts) == 2 else ""
    if not text.strip():
        return ControlAction(
            action_kind="invalid_command", target_scope="control", route=invocation.route,
            notes="Use /btw <question>.",
        )
    return ControlAction(
        action_kind="start_btw", target_scope="runtime", route=invocation.route,
        args={"text": text, "input_id": invocation.origin_event_id},
    )


def _handle_status(invocation: ControlCommandInvocation) -> ControlAction:
    return ControlAction(action_kind="show_status", target_scope="runtime", route=invocation.route)


def _handle_log(invocation: ControlCommandInvocation) -> ControlAction:
    if not invocation.argv:
        return ControlAction(action_kind="show_log", target_scope="runtime", route=invocation.route)
    subcommand = invocation.argv[0].strip().lower()
    if subcommand not in {"start", "end"}:
        return ControlAction(
            action_kind="invalid_command", target_scope="control", route=invocation.route,
            notes="Use /log start or /log end.",
        )
    return ControlAction(
        action_kind="set_log", target_scope="runtime", route=invocation.route,
        args={"prompt_log_enabled": subcommand == "start"},
    )


def _handle_interrupt(invocation: ControlCommandInvocation) -> ControlAction:
    return ControlAction(action_kind="interrupt_turn", target_scope="runtime", route=invocation.route)


def _handle_compact(invocation: ControlCommandInvocation) -> ControlAction:
    return ControlAction(action_kind="compact_memory", target_scope="memory", route=invocation.route)


def _handle_reset(invocation: ControlCommandInvocation) -> ControlAction:
    if not invocation.argv:
        return ControlAction(action_kind="open_reset_confirm", target_scope="memory", route=invocation.route)
    request_id = invocation.argv[1].strip() if len(invocation.argv) > 1 else ""
    if invocation.argv[0].lower() != "confirm" or not request_id:
        return ControlAction(
            action_kind="invalid_command", target_scope="control", route=invocation.route,
            notes="Use /reset or /reset confirm <request_id>.",
        )
    return ControlAction(
        action_kind="reset_memory", target_scope="memory", route=invocation.route,
        args={"request_id": request_id},
    )


def command_specs() -> tuple[ControlCommandSpec, ...]:
    return (
        ControlCommandSpec(
            name="btw", handler=_handle_btw,
            description="Ask a temporary read-only side question without adding to conversation history.",
            usage="/btw <question>", show_in_panel=True,
        ),
        ControlCommandSpec(
            name="status", handler=_handle_status,
            description="Show Pal activity, queued messages, active LLM, and usage statistics.",
            usage="/status", show_in_panel=True, panel_button=True, panel_label="Pal Status",
        ),
        ControlCommandSpec(
            name="interrupt", handler=_handle_interrupt,
            description="Interrupt the current active turn in this scope.", usage="/interrupt",
            show_in_panel=True, panel_button=True, panel_label="Interrupt",
            interaction_action_key="control.interrupt.run",
        ),
        ControlCommandSpec(
            name="log", handler=_handle_log,
            description="Show or update prompt debug logging for future turns.", usage="/log [start|end]",
            show_in_panel=True, panel_button=True, panel_label="Log", interaction_action_key="control.log.open",
        ),
        ControlCommandSpec(
            name="compact", handler=_handle_compact,
            description="Manually trigger memory compaction.", usage="/compact",
            show_in_panel=True, panel_button=True, panel_label="Compact",
            interaction_action_key="control.compact.run",
        ),
        ControlCommandSpec(
            name="reset", handler=_handle_reset,
            description="Open memory reset confirmation for this scope.", usage="/reset",
            show_in_panel=True, panel_button=True, panel_label="Reset Memory",
            interaction_action_key="control.reset.open", confirm_policy="confirm",
        ),
    )


def parse_interaction(result: InteractionResult) -> ControlAction | None:
    key = result.action_key
    if key == "control.log.open":
        return ControlAction(action_kind="show_log", target_scope="runtime", route=result.route)
    if key in {"control.log.start", "control.log.end"}:
        return ControlAction(
            action_kind="set_log", target_scope="runtime", route=result.route,
            args={"prompt_log_enabled": key == "control.log.start"},
        )
    if key == "control.interrupt.run":
        return ControlAction(action_kind="interrupt_turn", target_scope="runtime", route=result.route)
    if key == "control.compact.run":
        if result.action_args:
            return ControlAction(
                action_kind="invalid_command", target_scope="control", route=result.route,
                notes="This compact button is no longer valid. Open /control again.",
            )
        return ControlAction(
            action_kind="compact_memory", target_scope="memory", route=result.route,
        )
    if key == "control.reset.open":
        return ControlAction(action_kind="open_reset_confirm", target_scope="memory", route=result.route)
    if key in {"control.reset.confirm", "control.reset.cancel"}:
        return ControlAction(
            action_kind="reset_memory" if key == "control.reset.confirm" else "cancel_reset_confirm",
            target_scope="memory", route=result.route,
            args={"request_id": str(result.action_args.get("request_id") or result.interaction_id).strip()},
        )
    return None


def interaction_specs() -> tuple[ControlInteractionSpec, ...]:
    return tuple(ControlInteractionSpec(key, parse_interaction) for key in (
        "control.log.open", "control.log.start", "control.log.end", "control.interrupt.run",
        "control.compact.run", "control.reset.open", "control.reset.confirm", "control.reset.cancel",
    ))


def action_handlers(core: PalCore) -> dict[str, ControlActionSpec]:
    async def btw(action: ControlAction) -> None:
        await start_btw(core, action)

    return {
        "start_btw": ControlActionSpec(btw),
        "show_status": ControlActionSpec(core._handle_show_status_async, allow_during_maintenance=True),
        "show_log": ControlActionSpec(core._handle_show_log_async),
        "set_log": ControlActionSpec(core._handle_set_log_async),
        "interrupt_turn": ControlActionSpec(core._handle_interrupt_turn_async,
                                           lifecycle_access=LifecycleAccess.OWNER_MANAGED),
        "open_reset_confirm": ControlActionSpec(core._handle_open_reset_confirm_async),
        "cancel_reset_confirm": ControlActionSpec(core._handle_cancel_reset_confirm_async),
        "reset_memory": ControlActionSpec(core._handle_reset_memory_async,
                                         lifecycle_access=LifecycleAccess.OWNER_MANAGED),
        "compact_memory": ControlActionSpec(core._handle_compact_memory_async),
        "route_reply": ControlActionSpec(core._handle_route_reply_async, allow_during_maintenance=True),
        "invoke_capability": ControlActionSpec(core._handle_invoke_capability_async,
                                              lifecycle_access=LifecycleAccess.OWNER_MANAGED),
    }
