from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pal.shared import EventKind, TaskContextPack


@dataclass(frozen=True)
class MinionIPCHandlers:
    progress_event_name: str = EventKind.MINION_PROGRESS
    checkpoint_event_name: str = EventKind.MINION_CHECKPOINT
    terminal_event_name: str = EventKind.MINION_TERMINAL


class MinionRuntimePort(Protocol):
    def accept(self, context_pack: TaskContextPack) -> None:
        ...


# Compatibility aliases; old worker names are no longer canonical.
WorkerIPCHandlers = MinionIPCHandlers
WorkerRuntimePort = MinionRuntimePort

