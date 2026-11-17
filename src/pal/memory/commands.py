"""Memory-owned review command parsing."""
from __future__ import annotations

from pal.control.contracts import ControlAction, ControlCommandInvocation, ControlCommandSpec


def _handle_review(invocation: ControlCommandInvocation) -> ControlAction:
    return ControlAction(
        action_kind="memory_review_open", target_scope="memory", route=invocation.route,
        args={"batch_id": invocation.argv[0] if invocation.argv else ""},
    )


def command_specs() -> tuple[ControlCommandSpec, ...]:
    return (ControlCommandSpec(
        name="memory_review", handler=_handle_review,
        description="Review or resume a memory proposal batch.", usage="/memory_review [batch_id]",
        show_in_panel=True, panel_button=True, panel_label="Memory proposals",
    ),)
