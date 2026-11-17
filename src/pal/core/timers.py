"""Cancellable timers enqueue work; only the main loop executes callbacks."""
from __future__ import annotations

import heapq
import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock

from pal.foundation import EventEnvelope
from pal.shared.timers import TimerCallback

TIMER_EVENT = "runtime.timer"


@dataclass(eq=False)
class TimerHandle:
    scope: TimerScope
    callback: TimerCallback
    _pending: bool = True

    @property
    def pending(self) -> bool:
        return self._pending and not self.scope.closed

    def cancel(self) -> None:
        with self.scope.scheduler.lock:
            self._pending = False
            self.scope.handles.discard(self)
        self.scope.scheduler.wake()

    async def fire(self) -> None:
        with self.scope.scheduler.lock:
            if not self.pending:
                return
            self._pending = False
            self.scope.handles.discard(self)
            is_current = self.scope.owner_is_current
        if self.scope.closed or (is_current is not None and not is_current()):
            return
        await self.callback()


@dataclass(eq=False)
class TimerScope:
    scheduler: TimerScheduler
    closed: bool = False
    handles: set[TimerHandle] = field(default_factory=set)
    owner_is_current: Callable[[], bool] | None = field(default=None, init=False, repr=False)

    def bind_owner(self, is_current: Callable[[], bool]) -> None:
        with self.scheduler.lock:
            if self.closed:
                raise RuntimeError("timer owner is closed")
            if self.owner_is_current is not None:
                raise ValueError("timer scope already has an owner")
            self.owner_is_current = is_current

    def schedule(self, delay_seconds: float, callback: TimerCallback) -> TimerHandle:
        delay = float(delay_seconds)
        if not math.isfinite(delay):
            raise ValueError("timer delay must be finite")
        with self.scheduler.lock:
            if self.closed or self.scheduler.closed:
                raise RuntimeError("timer owner is closed")
            handle = TimerHandle(self, callback)
            self.handles.add(handle)
            self.scheduler.sequence += 1
            heapq.heappush(self.scheduler.queue, (
                self.scheduler.clock() + max(0.0, delay), self.scheduler.sequence, handle))
        self.scheduler.wake()
        return handle

    def close(self) -> None:
        with self.scheduler.lock:
            self.closed = True
            for handle in self.handles:
                handle._pending = False
            self.handles.clear()
            self.scheduler.scopes.discard(self)
        self.scheduler.wake()


@dataclass
class TimerScheduler:
    wake: Callable[[], None]
    clock: Callable[[], float] = time.monotonic
    queue: list[tuple[float, int, TimerHandle]] = field(default_factory=list)
    scopes: set[TimerScope] = field(default_factory=set)
    sequence: int = 0
    closed: bool = False
    lock: RLock = field(default_factory=RLock, repr=False)

    def open_scope(self) -> TimerScope:
        with self.lock:
            if self.closed:
                raise RuntimeError("timer scheduler is closed")
            scope = TimerScope(self)
            self.scopes.add(scope)
            return scope

    def _prune(self) -> None:
        while self.queue and not self.queue[0][2].pending:
            heapq.heappop(self.queue)
        # Repeatedly replacing long timers must not retain cancelled callbacks.
        if len(self.queue) > 64 and sum(handle.pending for _, _, handle in self.queue) * 2 < len(self.queue):
            self.queue[:] = [entry for entry in self.queue if entry[2].pending]
            heapq.heapify(self.queue)

    def next_delay(self) -> float | None:
        with self.lock:
            self._prune()
            return max(0.0, self.queue[0][0] - self.clock()) if self.queue else None

    def drain_due(self) -> list[EventEnvelope]:
        events = []
        with self.lock:
            self._prune()
            now = self.clock()
            while self.queue and self.queue[0][0] <= now:
                _, _, handle = heapq.heappop(self.queue)
                if handle.pending:
                    events.append(EventEnvelope(event_kind=TIMER_EVENT, source_kind="core", payload=handle))
        return events

    def close(self) -> None:
        with self.lock:
            self.closed = True
            scopes = tuple(self.scopes)
        for scope in scopes:
            scope.close()
        with self.lock:
            self.queue.clear()
