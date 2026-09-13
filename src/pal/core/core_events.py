from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable
from collections.abc import Iterable
from copy import deepcopy
from queue import Empty, Full, Queue
from threading import RLock


CoreEvent = dict[str, Any]
TurnEvent = CoreEvent

TURN_START = "turn.start"
TURN_END = "turn.end"
TURN_TOOL_CALL_BEFORE = "turn.tool_call_before"
TURN_TOOL_CALL_AFTER = "turn.tool_call_after"

TURN_TOOL_CALL_FAILED = "turn.tool_call_failed"
MEMORY_SLEEP = "memory.sleep"
FAILURE_STARTED = "failure.started"
FAILURE_FINISHED = "failure.finished"
SAFE_MODE_STARTED = "failure.safe_mode_started"
SAFE_MODE_FINISHED = "failure.safe_mode_finished"
RUNTIME_SNAPSHOT = "runtime.snapshot"

ALL_CORE_TOPICS = frozenset({TURN_START, TURN_END, TURN_TOOL_CALL_BEFORE,
    TURN_TOOL_CALL_AFTER, TURN_TOOL_CALL_FAILED, MEMORY_SLEEP, FAILURE_STARTED,
    FAILURE_FINISHED, SAFE_MODE_STARTED, SAFE_MODE_FINISHED})

ALL_TURN_TOPICS = frozenset({TURN_START, TURN_END, TURN_TOOL_CALL_BEFORE, TURN_TOOL_CALL_AFTER})


@dataclass
class CoreEventBus:
    _subscribers: dict[str, list[Callable[[str, TurnEvent], None]]] = field(default_factory=dict)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)

    _lock: Any = field(default_factory=RLock, repr=False)
    _state: dict[str, Any] = field(default_factory=lambda: {
        "sleeping": False, "failures": [], "safe_modes": [], "turns": [],
    })

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def open_subscription(
        self, topics: Iterable[str] = ALL_CORE_TOPICS, *, max_pending: int = 128,
        active: bool = True,
    ) -> CoreEventSubscription:
        return CoreEventSubscription(self, frozenset(topics), max_pending, active=active)

    def subscribe(self, topic: str, handler: Callable[[str, TurnEvent], None]) -> None:
        with self._lock:
            bucket = self._subscribers.setdefault(topic, [])
            if handler not in bucket:
                bucket.append(handler)

    def unsubscribe(self, topic: str, handler: Callable[[str, TurnEvent], None]) -> None:
        with self._lock:
            bucket = self._subscribers.get(topic)
            if bucket is None:
                return
            while handler in bucket:
                bucket.remove(handler)

    def emit(self, topic: str, event: TurnEvent) -> None:
        with self._lock:
            if topic == MEMORY_SLEEP:
                self._state["sleeping"] = bool(event["sleeping"])
            for start, end, key, identity in (
                (TURN_START, TURN_END, "turns", "turn_id"),
                (FAILURE_STARTED, FAILURE_FINISHED, "failures", "failure_id"),
                (SAFE_MODE_STARTED, SAFE_MODE_FINISHED, "safe_modes", "failure_id"),
            ):
                if topic in {start, end}:
                    self._state[key] = [item for item in self._state[key]
                                        if item.get(identity) != event.get(identity)]
                    if topic == start:
                        self._state[key].append(deepcopy(event))
            handlers = list(self._subscribers.get(topic, ()))
            for handler in handlers:
                try:
                    handler(topic, deepcopy(event))
                except Exception as exc:
                    del self.diagnostics[:-127]
                    self.diagnostics.append(
                        {
                            "kind": "turn_event_subscriber_failed",
                            "topic": topic,
                            "handler": _handler_name(handler),
                            "error": f"{exc.__class__.__name__}: {exc}",
                        }
                    )

    def subscribers_for(self, topic: str) -> tuple[Callable[[str, TurnEvent], None], ...]:
        with self._lock:
            return tuple(self._subscribers.get(topic, ()))


def _handler_name(handler: Callable[[str, TurnEvent], None]) -> str:
    qualified = getattr(handler, "__qualname__", None)
    module = getattr(handler, "__module__", None)
    if qualified and module:
        return f"{module}.{qualified}"
    return repr(handler)


# Compatibility for existing turn-only integrations; this is the same bus.
TurnEventBus = CoreEventBus


class CoreEventSubscription:
    """Bounded nonblocking mailbox. Consumers own their worker/I/O lifecycle."""

    def __init__(
        self, bus: CoreEventBus, topics: frozenset[str], max_pending: int, *, active: bool = True,
    ) -> None:
        if max_pending < 2:
            raise ValueError("max_pending must be at least 2")
        self.bus, self.topics = bus, topics
        self.queue = Queue(maxsize=max_pending)
        self.closed = False
        self.dropped = 0
        self.active = False
        if active:
            self.activate()

    def activate(self) -> None:
        with self.bus._lock:
            if self.closed or self.active:
                return
            self.active = True
            for topic in self.topics:
                self.bus.subscribe(topic, self._receive)
            self.queue.put_nowait((RUNTIME_SNAPSHOT, self.bus.snapshot()))

    def _receive(self, topic: str, event: CoreEvent) -> None:
        with self.bus._lock:
            if self.closed:
                return
            try:
                self.queue.put_nowait((topic, event))
            except Full:
                # Ephemeral observations are lossy. Resync state instead of
                # allowing a slow display to block Core or pin stale state.
                while True:
                    try:
                        self.queue.get_nowait()
                        self.dropped += 1
                    except Empty:
                        break
                self.queue.put_nowait((RUNTIME_SNAPSHOT, self.bus.snapshot()))
                self.queue.put_nowait((topic, event))

    def get(self, timeout: float | None = None) -> tuple[str, CoreEvent]:
        return self.queue.get(timeout=timeout)

    def close(self) -> None:
        with self.bus._lock:
            self.closed = True
            for topic in self.topics:
                self.bus.unsubscribe(topic, self._receive)
            while True:
                try:
                    self.queue.get_nowait()
                except Empty:
                    break
