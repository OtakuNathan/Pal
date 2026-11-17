"""One-shot events scheduled on the resident main loop."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

TimerCallback = Callable[[], Awaitable[None]]


class TimerHandlePort(Protocol):
    @property
    def pending(self) -> bool: ...
    def cancel(self) -> None: ...


class TimerScopePort(Protocol):
    def bind_owner(self, is_current: Callable[[], bool]) -> None: ...
    def schedule(self, delay_seconds: float, callback: TimerCallback) -> TimerHandlePort: ...
    def close(self) -> None: ...
