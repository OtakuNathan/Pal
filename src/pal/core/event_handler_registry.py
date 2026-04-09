from __future__ import annotations

from dataclasses import dataclass, field

from pal.core.events import EventHandler


@dataclass
class EventHandlerRegistry:
    handlers: dict[str, list[EventHandler]] = field(default_factory=dict)

    def register(self, event_kind: str, handler: EventHandler) -> None:
        bucket = self.handlers.setdefault(event_kind, [])
        if handler not in bucket:
            bucket.append(handler)

    def matching(self, event_kind: str) -> list[EventHandler]:
        matched: list[EventHandler] = []
        for handler in self.handlers.get(event_kind, []):
            if handler.can_handle(event_kind):
                matched.append(handler)
        return matched
