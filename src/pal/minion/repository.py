from __future__ import annotations

from typing import Protocol


class TaskingRepositoryPort(Protocol):
    def save_stub(self) -> None:
        ...
