from __future__ import annotations

from dataclasses import dataclass, field

from pal.core.events import EventSource


@dataclass
class EventSourceRegistry:
    sources: dict[str, EventSource] = field(default_factory=dict)
    by_module: dict[str, list[str]] = field(default_factory=dict)

    def attach(self, module_id: str, source: EventSource) -> None:
        self.sources[source.source_id] = source
        bucket = self.by_module.setdefault(module_id, [])
        if source.source_id not in bucket:
            bucket.append(source.source_id)

    def iter_sources(self) -> tuple[EventSource, ...]:
        return tuple(self.sources.values())

    def detach_module(self, module_id: str) -> list[str]:
        names = list(self.by_module.pop(module_id, []))
        for name in names:
            self.sources.pop(name, None)
        return names
