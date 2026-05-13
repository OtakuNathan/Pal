from __future__ import annotations

from dataclasses import dataclass

from pal.core.events import EventSource
from pal.foundation import EventEnvelope
from pal.service.service import ServiceManager
from pal.shared import EventKind, SourceKind


@dataclass
class ServiceEventSource(EventSource):
    manager: ServiceManager
    source_id: str = f"{SourceKind.PROACTIVE}.triggers"

    def prepare(self, context) -> bool:
        _ = context
        self.manager.enqueue_due_triggers()
        return self.manager.trigger_mailbox.has_pending()

    def drain(self, context) -> list[EventEnvelope]:
        _ = context
        events = [
            EventEnvelope(
                event_kind=EventKind.PROACTIVE_TRIGGER,
                source_kind=SourceKind.PROACTIVE,
                payload=trigger,
            )
            for trigger in self.manager.trigger_mailbox.drain()
        ]
        return events
