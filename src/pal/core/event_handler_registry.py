from __future__ import annotations

from dataclasses import dataclass, field

from pal.core.events import EventHandler


@dataclass
class EventHandlerRegistry:
    handlers: dict[str, list[EventHandler]] = field(default_factory=dict)
    by_module: dict[str, list[tuple[str, EventHandler]]] = field(default_factory=dict)

    def register(self, event_kind: str, handler: EventHandler, *, module_id: str | None = None) -> None:
        bucket = self.handlers.setdefault(event_kind, [])
        if all(item is not handler for item in bucket):
            bucket.append(handler)
        if module_id:
            module_bucket = self.by_module.setdefault(module_id, [])
            if all(kind != event_kind or item is not handler for kind, item in module_bucket):
                module_bucket.append((event_kind, handler))

    def matching(self, event_kind: str) -> list[EventHandler]:
        matched: list[EventHandler] = []
        for handler in self.handlers.get(event_kind, []):
            if handler.can_handle(event_kind):
                matched.append(handler)
        return matched

    def detach_module(self, module_id: str) -> list[str]:
        items = list(self.by_module.pop(module_id, []))
        detached: list[str] = []
        for event_kind, handler in items:
            bucket = self.handlers.get(event_kind)
            if not bucket:
                continue
            self.handlers[event_kind] = [item for item in bucket if item is not handler]
            if not self.handlers[event_kind]:
                self.handlers.pop(event_kind, None)
            detached.append(event_kind)
        return detached
