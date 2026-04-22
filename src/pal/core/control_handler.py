from __future__ import annotations

from dataclasses import dataclass

from pal.control.contracts import ControlAction
from pal.core.events import EventHandler
from pal.foundation import EventEnvelope
from pal.shared import EventKind


@dataclass
class CoreControlActionHandler(EventHandler):
    core: "PalCore"

    def can_handle(self, event_kind: str) -> bool:
        return event_kind == EventKind.CONTROL_ACTION

    async def handle(self, event: EventEnvelope, context) -> list[EventEnvelope] | None:
        _ = context
        action = event.payload
        if not isinstance(action, ControlAction):
            return []
        await self.core.handle_control_action_async(action)
        return []
