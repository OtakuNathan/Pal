from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Generic, TypeVar


T = TypeVar("T")
_MISSING = object()


@dataclass(frozen=True)
class TTLEntry(Generic[T]):
    key: str
    value: T
    created_at_monotonic: float
    expires_at_monotonic: float | None = None

    def is_expired(self, now: float) -> bool:
        return self.expires_at_monotonic is not None and self.expires_at_monotonic <= now


@dataclass
class BoundedTTLBuffer(Generic[T]):
    """Small newest-first in-memory buffer with key de-dupe, capacity, and optional TTL."""

    capacity: int = 10
    ttl_seconds: float | None = None
    clock: Callable[[], float] = time.monotonic
    _entries: list[TTLEntry[T]] = field(default_factory=list)

    def upsert(self, key: str, value: T, *, ttl_seconds: float | None = None) -> None:
        normalized_key = str(key or "").strip()
        if not normalized_key:
            raise ValueError("TTL buffer key must not be empty")
        now = float(self.clock())
        self.prune(now=now)
        resolved_ttl = self.ttl_seconds if ttl_seconds is None else ttl_seconds
        expires_at = None if resolved_ttl is None else now + max(float(resolved_ttl), 0.0)
        self._entries = [entry for entry in self._entries if entry.key != normalized_key]
        self._entries.insert(
            0,
            TTLEntry(
                key=normalized_key,
                value=value,
                created_at_monotonic=now,
                expires_at_monotonic=expires_at,
            ),
        )
        del self._entries[self._capacity():]

    def values(self, *, limit: int | None = None) -> list[T]:
        self.prune()
        resolved_limit = self._capacity() if limit is None else max(0, min(int(limit), self._capacity()))
        return [entry.value for entry in self._entries[:resolved_limit]]

    def items(self, *, limit: int | None = None) -> list[tuple[str, T]]:
        self.prune()
        resolved_limit = self._capacity() if limit is None else max(0, min(int(limit), self._capacity()))
        return [(entry.key, entry.value) for entry in self._entries[:resolved_limit]]

    def get(self, key: str, default: object = None) -> T | object:
        normalized_key = str(key or "").strip()
        self.prune()
        for entry in self._entries:
            if entry.key == normalized_key:
                return entry.value
        return default

    def pop(self, key: str, default: object = _MISSING) -> T | object:
        normalized_key = str(key or "").strip()
        self.prune()
        for index, entry in enumerate(self._entries):
            if entry.key == normalized_key:
                del self._entries[index]
                return entry.value
        if default is _MISSING:
            raise KeyError(normalized_key)
        return default

    def prune(self, *, now: float | None = None) -> int:
        current = float(self.clock() if now is None else now)
        before = len(self._entries)
        self._entries = [entry for entry in self._entries if not entry.is_expired(current)]
        return before - len(self._entries)

    def clear(self) -> None:
        self._entries.clear()

    def __len__(self) -> int:
        self.prune()
        return len(self._entries)

    def _capacity(self) -> int:
        return max(1, int(self.capacity or 1))
