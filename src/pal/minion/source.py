from __future__ import annotations

from dataclasses import dataclass

from pal.core.events import EventSource
from pal.foundation import EventEnvelope
from pal.minion.service import TaskingService


@dataclass
class MinionEventSource(EventSource):
    service: TaskingService
    source_id: str = "tasking.minion"

    def prepare(self, context) -> bool:
        _ = context
        return self.service.minion_mailbox.has_pending()

    def poll_timeout_ms(self, context) -> int | None:
        _ = context
        return 0 if self.service.minion_mailbox.has_pending() else None

    def drain(self, context) -> list[EventEnvelope]:
        _ = context
        events = [
            EventEnvelope(
                event_kind=item.event_name,
                source_kind="minion",
                payload=item.payload,
            )
            for item in self.service.minion_mailbox.drain()
        ]
        return events
