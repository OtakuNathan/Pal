from __future__ import annotations

from dataclasses import dataclass

from pal.core.events import EventSource
from pal.foundation import EventEnvelope
from pal.service.service import ServiceManager
from pal.shared import EventKind, SourceKind


@dataclass
class ServiceEventSource(EventSource):
    manager: ServiceManager
    source_id: str = f"{SourceKind.SERVICE}.triggers"

    def prepare(self, context) -> bool:
        _ = context
        self.manager.enqueue_due_triggers()
        return self.manager.trigger_mailbox.has_pending()

    def poll_timeout_ms(self, context) -> int | None:
        _ = context
        return 0 if self.manager.trigger_mailbox.has_pending() else None

    def drain(self, context) -> list[EventEnvelope]:
        _ = context
        events = [
            EventEnvelope(
                event_kind=EventKind.SERVICE_TRIGGER,
                source_kind=SourceKind.SERVICE,
                payload=trigger,
            )
            for trigger in self.manager.trigger_mailbox.drain()
        ]
        return events
