from __future__ import annotations

from dataclasses import dataclass

from pal.core.events import EventHandler
from pal.foundation import EventEnvelope
from pal.service.service import ServiceManager, ServiceRunner
from pal.shared import EventKind, ServiceTriggerEvent


@dataclass
class ServiceTriggerHandler(EventHandler):
    manager: ServiceManager
    runner: ServiceRunner | None = None

    def can_handle(self, event_kind: str) -> bool:
        return event_kind == EventKind.SERVICE_TRIGGER

    async def handle(self, event: EventEnvelope, context) -> list[EventEnvelope] | None:
        if not isinstance(event.payload, ServiceTriggerEvent):
            return []
        definition = self.manager.registered.get(event.payload.service_id)
        if definition is None:
            return []
        service_run_id = self.runner.begin_run(event.payload) if self.runner is not None else None
        core = context.require_port("core:core")
        try:
            outcome = await core.process_service_trigger_async(event.payload, definition)
        except Exception as exc:
            if self.runner is not None:
                self.runner.fail_run(service_run_id, error_text=str(exc))
            raise
        if self.runner is not None:
            self.runner.complete_run(service_run_id, turn_id=outcome.turn_id, final_reply=_settled_output_text(outcome))
        self.manager.mark_run_completed(event.payload.service_id)
        return []


def _settled_output_text(outcome) -> str:
    replies = tuple(str(item).strip() for item in getattr(outcome, "reply_texts", ()) if str(item).strip())
    if replies:
        return "\n\n".join(replies)
    return str(getattr(outcome, "final_reply", "") or "")
