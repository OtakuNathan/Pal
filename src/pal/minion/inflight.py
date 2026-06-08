from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable, TypeVar


T = TypeVar("T")


@dataclass
class InflightTracker:
    _keys: set[str] = field(default_factory=set)

    def claim(self, key: str) -> bool:
        normalized = str(key or "").strip()
        if not normalized or normalized in self._keys:
            return False
        self._keys.add(normalized)
        return True

    def release(self, key: str) -> None:
        self._keys.discard(str(key or "").strip())

    def contains(self, key: str) -> bool:
        return str(key or "").strip() in self._keys

    def create_task(
        self,
        key: str,
        factory: Callable[[], Awaitable[T]],
        *,
        name: str,
        loop: asyncio.AbstractEventLoop | None = None,
    ) -> asyncio.Task[T] | None:
        normalized = str(key or "").strip()
        if not self.claim(normalized):
            return None
        target_loop = loop or asyncio.get_running_loop()

        async def run_claimed() -> T:
            try:
                return await factory()
            finally:
                self.release(normalized)

        return target_loop.create_task(run_claimed(), name=name)
