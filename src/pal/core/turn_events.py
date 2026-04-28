from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


TurnEvent = dict[str, Any]

TURN_START = "turn.start"
TURN_END = "turn.end"
TURN_TOOL_CALL_BEFORE = "turn.tool_call_before"
TURN_TOOL_CALL_AFTER = "turn.tool_call_after"

ALL_TURN_TOPICS = frozenset({TURN_START, TURN_END, TURN_TOOL_CALL_BEFORE, TURN_TOOL_CALL_AFTER})


@dataclass
class TurnEventBus:
    _subscribers: dict[str, list[Callable[[str, TurnEvent], None]]] = field(default_factory=dict)

    def subscribe(self, topic: str, handler: Callable[[str, TurnEvent], None]) -> None:
        bucket = self._subscribers.setdefault(topic, [])
        if handler not in bucket:
            bucket.append(handler)

    def unsubscribe(self, topic: str, handler: Callable[[str, TurnEvent], None]) -> None:
        bucket = self._subscribers.get(topic)
        if bucket is None:
            return
        while handler in bucket:
            bucket.remove(handler)

    def emit(self, topic: str, event: TurnEvent) -> None:
        for handler in list(self._subscribers.get(topic, ())):
            handler(topic, event)

    def subscribers_for(self, topic: str) -> tuple[Callable[[str, TurnEvent], None], ...]:
        return tuple(self._subscribers.get(topic, ()))
