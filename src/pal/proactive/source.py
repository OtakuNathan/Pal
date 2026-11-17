from __future__ import annotations

from dataclasses import dataclass

from pal.control.runtime_contracts import CORE_CONTROL
from pal.shared.timers import TimerScopePort, TimerHandlePort

from pal.core.events import EventSource
from pal.foundation import EventEnvelope
from pal.proactive.runtime import ProactiveManager
from pal.shared import EventKind, SourceKind


@dataclass
class ProactiveEventSource(EventSource):
    manager: ProactiveManager
    source_id: str = f"{SourceKind.PROACTIVE}.triggers"
    paused_for_memory: bool = False
    timers: TimerScopePort | None = None
    timer: TimerHandlePort | None = None
    timer_due: str | None = None

    def prepare(self, context) -> bool:
        host = context.port_registry.optional(CORE_CONTROL)
        if host is not None and host.snapshot().memory_maintenance:
            self.paused_for_memory = True
            self.cancel_timer()
            return False
        self.manager.enqueue_due_triggers()
        self.arm_timer()
        if host is not None and not host.can_start_background_turn():
            return False
        return self.manager.trigger_mailbox.has_pending()

    def cancel_timer(self):
        if self.timer is not None:
            self.timer.cancel()
        self.timer = None
        self.timer_due = None

    def arm_timer(self):
        due = min((value for key, value in self.manager.schedule_engine.next_due_by_proactive_id.items()
                   if value and self.manager.registered[key].enabled), default=None)
        if due == self.timer_due and self.timer is not None and self.timer.pending:
            return
        self.cancel_timer()
        delay = self.manager.seconds_until_next_due()
        if self.timers is not None and delay is not None:
            self.timer_due = due
            async def wake():
                self.timer = None
                self.manager._notify_ready()
            self.timer = self.timers.schedule(delay, wake)

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
