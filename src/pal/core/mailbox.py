from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Generic, TypeVar


T = TypeVar("T")


@dataclass
class Mailbox(Generic[T]):
    items: deque[T] = field(default_factory=deque)

    def put(self, item: T) -> None:
        # Mailbox is the stable internal boundary between subsystem producers
        # and the main loop. Producers push; the loop drains.
        self.items.append(item)

    def has_pending(self) -> bool:
        return bool(self.items)

    def drain(self) -> list[T]:
        # Sources consume mailboxes in batches so one poll can advance more than
        # one event without exposing producer-specific queues upstream.
        drained = list(self.items)
        self.items.clear()
        return drained

    def peek_all(self) -> tuple[T, ...]:
        return tuple(self.items)
