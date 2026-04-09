from __future__ import annotations

from typing import Awaitable, Protocol

from pal.foundation import EventEnvelope


class EventSource(Protocol):
    source_id: str

    def prepare(self, context: "MainContext") -> bool:
        ...

    def poll_timeout_ms(self, context: "MainContext") -> int | None:
        ...

    def drain(self, context: "MainContext") -> list[EventEnvelope]:
        ...


class EventHandler(Protocol):
    def can_handle(self, event_kind: str) -> bool:
        ...

    def handle(self, event: EventEnvelope, context: "MainContext") -> list[EventEnvelope] | Awaitable[list[EventEnvelope] | None] | None:
        ...
