from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from threading import RLock

from pal.core.events import EventSource
from pal.shared.registration import OwnerRelation, OwnershipEvent, owner_relation, reduce_ownership


@dataclass(frozen=True)
class EventSourceRegistration:
    module_id: str
    source: EventSource


@dataclass
class EventSourceRegistry:
    _entries: dict[str, EventSourceRegistration] = field(default_factory=dict, repr=False)
    _lock: RLock = field(default_factory=RLock, repr=False)

    @property
    def sources(self) -> dict[str, EventSource]:
        with self._lock:
            return {name: entry.source for name, entry in self._entries.items()}

    def attach(self, module_id: str, source: EventSource) -> EventSourceRegistration:
        name = source.source_id
        if not module_id or not name or name != name.strip():
            raise ValueError("Event sources require a module and a nonempty stable source_id")
        with self._lock:
            current = self._entries.get(name)
            relation = owner_relation(current.source if current else None, source)
            try:
                reduce_ownership(relation, OwnershipEvent.CLAIM)
            except ValueError:
                raise ValueError(f"event source already registered: {name} (owner={current.module_id if current else ''})") from None
            if any(entry.source is source for entry in self._entries.values()):
                raise ValueError("Event source instance is already registered")
            registration = EventSourceRegistration(module_id, source)
            self._entries[name] = registration
            return registration

    def iter_sources(self) -> tuple[EventSource, ...]:
        with self._lock:
            return tuple(entry.source for entry in self._entries.values())

    def detach_module(self, module_id: str, *, expected_sources: Sequence[EventSource]) -> list[str]:
        with self._lock:
            removed = []
            for name, entry in tuple(self._entries.items()):
                if entry.module_id != module_id:
                    continue
                expected = next((source for source in expected_sources if source is entry.source), None)
                relation = owner_relation(entry.source, expected)
                target = reduce_ownership(relation, OwnershipEvent.RELEASE)
                if relation is OwnerRelation.SELF and target is OwnerRelation.VACANT:
                    self._entries.pop(name)
                    removed.append(name)
            return removed
