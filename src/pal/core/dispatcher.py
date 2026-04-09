from __future__ import annotations

import inspect
from dataclasses import dataclass

from pal.core.main_context import MainContext
from pal.foundation import EventEnvelope


@dataclass
class EventDispatcher:
    def dispatch(self, event: EventEnvelope, context: MainContext) -> list[EventEnvelope]:
        derived: list[EventEnvelope] = []
        for handler in context.event_handler_registry.matching(event.event_kind):
            produced = handler.handle(event, context) or []
            derived.extend(produced)
        return derived

    async def dispatch_async(self, event: EventEnvelope, context: MainContext) -> list[EventEnvelope]:
        derived: list[EventEnvelope] = []
        for handler in context.event_handler_registry.matching(event.event_kind):
            produced = handler.handle(event, context)
            if inspect.isawaitable(produced):
                produced = await produced
            produced = produced or []
            derived.extend(produced)
        return derived
