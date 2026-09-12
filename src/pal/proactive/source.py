from __future__ import annotations

from dataclasses import dataclass

from pal.core.events import EventSource
from pal.foundation import EventEnvelope
from pal.proactive.runtime import ProactiveManager
from pal.shared import EventKind, SourceKind


@dataclass
class ProactiveEventSource(EventSource):
    manager: ProactiveManager
    source_id: str = f"{SourceKind.PROACTIVE}.triggers"
    paused_for_memory: bool = False

    def prepare(self, context) -> bool:
        core = context.port_registry.get("core:core")
        if core is not None and core.state.memory_maintenance:
            self.paused_for_memory = True
            return False
        self.manager.enqueue_due_triggers()
        return self.manager.trigger_mailbox.has_pending()

    def drain(self, context) -> list[EventEnvelope]:
        _ = context
        triggers = self.manager.trigger_mailbox.drain()
        if self.paused_for_memory:
            triggers = list({trigger.proactive_id: trigger for trigger in triggers}.values())
            self.paused_for_memory = False
        events = [
            EventEnvelope(
                event_kind=EventKind.PROACTIVE_TRIGGER,
                source_kind=SourceKind.PROACTIVE,
                payload=trigger,
            )
            for trigger in triggers
        ]
        return events
