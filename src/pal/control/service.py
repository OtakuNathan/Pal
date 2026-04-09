from __future__ import annotations

from dataclasses import dataclass

from pal.control.contracts import ControlAction, ControlEvent, ControlPlanePort
from pal.shared import EventKind


@dataclass
class ControlPlane(ControlPlanePort):
    def parse_event(self, event: ControlEvent) -> ControlAction | None:
        if event.event_kind == EventKind.SLASH_COMMAND:
            command = str(event.payload.get("command") or "").strip().lower()
            if command in {"/pause", "/resume", "/cancel", "/approve"}:
                return ControlAction(
                    action_kind=command.lstrip("/"),
                    target_scope="pal",
                )
        if event.event_kind == EventKind.APPROVAL_REQUEST:
            return ControlAction(
                action_kind="approve",
                target_scope="approval",
                target_id=str(event.payload.get("approval_id") or "") or None,
                requires_user_confirmation=True,
            )
        return None
