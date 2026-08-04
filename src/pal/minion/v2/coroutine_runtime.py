from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass
class CoroutineRunPermit:
    _semaphore: "CoroutineRunSemaphore"
    run_id: str
    _released: bool = field(default=False, init=False)

    @property
    def released(self) -> bool:
        return self._released

    async def release(self) -> None:
        if self._released:
            return
        await self._semaphore.release(self.run_id)
        self._released = True

    async def __aenter__(self) -> "CoroutineRunPermit":
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        await self.release()


@dataclass
class CoroutineRunSemaphore:
    """Capacity for materialized logical-coroutine incarnations only.

    Durable sessions and READY/SUSPENDED/WAITING states consume no capacity.
    The permit is acquired immediately before process materialization and may
    be released only after checkpoint/completion and process-group reap.
    """

    capacity: int
    _active_run_ids: set[str] = field(default_factory=set, init=False)
    _condition: asyncio.Condition = field(
        default_factory=asyncio.Condition,
        init=False,
    )

    def __post_init__(self) -> None:
        if self.capacity < 1:
            raise ValueError("coroutine run capacity must be positive")

    @property
    def active_count(self) -> int:
        return len(self._active_run_ids)

    @property
    def active_run_ids(self) -> frozenset[str]:
        return frozenset(self._active_run_ids)

    async def acquire(self, run_id: str) -> CoroutineRunPermit:
        if not run_id:
            raise ValueError("run_id is required")
        async with self._condition:
            if run_id in self._active_run_ids:
                raise RuntimeError(f"coroutine run already active: {run_id}")
            await self._condition.wait_for(
                lambda: len(self._active_run_ids) < self.capacity
            )
            self._active_run_ids.add(run_id)
        return CoroutineRunPermit(self, run_id)

    async def try_acquire(self, run_id: str) -> CoroutineRunPermit | None:
        if not run_id:
            raise ValueError("run_id is required")
        async with self._condition:
            if run_id in self._active_run_ids:
                raise RuntimeError(f"coroutine run already active: {run_id}")
            if len(self._active_run_ids) >= self.capacity:
                return None
            self._active_run_ids.add(run_id)
        return CoroutineRunPermit(self, run_id)

    async def release(self, run_id: str) -> None:
        async with self._condition:
            if run_id not in self._active_run_ids:
                raise RuntimeError(f"coroutine run is not active: {run_id}")
            self._active_run_ids.remove(run_id)
            self._condition.notify(1)
