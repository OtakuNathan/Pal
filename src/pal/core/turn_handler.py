from __future__ import annotations

from dataclasses import dataclass

from pal.core.events import EventHandler
from pal.foundation import EventEnvelope
from pal.shared import ChannelEnvelope, EventKind


@dataclass
class TurnEventHandler(EventHandler):
    core: "PalCore"

    def can_handle(self, event_kind: str) -> bool:
        return event_kind == EventKind.USER_MESSAGE

    async def handle(self, event: EventEnvelope, context) -> list[EventEnvelope] | None:
        _ = context
        if not isinstance(event.payload, ChannelEnvelope):
            return []
        await self.core.schedule_channel_turn_async(event.payload)
        return []
