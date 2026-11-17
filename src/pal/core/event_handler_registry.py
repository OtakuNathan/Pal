from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pal.core.events import EventHandler

if TYPE_CHECKING:
    from pal.core.main_context import MainContext
    from pal.core.module_registry import ModuleHandle


@dataclass(eq=False)
class EventHandlerRegistration:
    handler: EventHandler
    owner: ModuleHandle | None
    live: bool = True

    def is_current(self, context: MainContext) -> bool:
        return self.live and (self.owner is None or context.module_registry.is_current(self.owner))


@dataclass
class EventHandlerRegistry:
    handlers: dict[str, list[EventHandlerRegistration]] = field(default_factory=dict)
    by_module: dict[str, list[tuple[str, EventHandler]]] = field(default_factory=dict)

    def register(self, event_kind: str, handler: EventHandler, *, owner: ModuleHandle | None,
                 module_id: str | None = None) -> None:
        bucket = self.handlers.setdefault(event_kind, [])
        for index, entry in enumerate(bucket):
            if entry.handler is handler:
                if entry.owner is not owner or not entry.live:
                    entry.live = False
                    bucket[index] = EventHandlerRegistration(handler, owner)
                break
        else:
            bucket.append(EventHandlerRegistration(handler, owner))
        if module_id:
            module_bucket = self.by_module.setdefault(module_id, [])
            if all(kind != event_kind or item is not handler for kind, item in module_bucket):
                module_bucket.append((event_kind, handler))

    def matching(self, event_kind: str) -> list[EventHandlerRegistration]:
        return list(self.handlers.get(event_kind, []))

    def detach_module(self, module_id: str, *, expected_owner: ModuleHandle | None = None) -> list[str]:
        items = list(self.by_module.get(module_id, []))
        detached: list[str] = []
        retained: list[tuple[str, EventHandler]] = []
        for event_kind, handler in items:
            bucket = self.handlers.get(event_kind)
            if not bucket:
                continue
            removed = []
            for entry in bucket:
                if entry.handler is handler and (expected_owner is None or entry.owner is expected_owner):
                    entry.live = False
                    removed.append(entry)
            if not removed:
                retained.append((event_kind, handler))
                continue
            self.handlers[event_kind] = [item for item in bucket if item not in removed]
            if not self.handlers[event_kind]:
                self.handlers.pop(event_kind, None)
            detached.append(event_kind)
        if retained:
            self.by_module[module_id] = retained
        else:
            self.by_module.pop(module_id, None)
        return detached
