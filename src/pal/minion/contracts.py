from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pal.shared import CheckpointEvent, EventKind, MinionProgressEvent, MinionTerminalEvent, TaskContextPack


@dataclass(frozen=True)
class MinionIPCHandlers:
    progress_event_name: str = EventKind.MINION_PROGRESS
    checkpoint_event_name: str = EventKind.MINION_CHECKPOINT
    terminal_event_name: str = EventKind.MINION_TERMINAL


class MinionRuntimePort(Protocol):
    def accept(self, context_pack: TaskContextPack) -> None:
        ...


class WorkOrderServicePort(Protocol):
    def open_work_order(self, goal: str) -> str:
        ...


class MinionManagerPort(Protocol):
    def spawn(self, context_pack: TaskContextPack) -> None:
        ...


class CheckpointServicePort(Protocol):
    def record(self, event: CheckpointEvent) -> None:
        ...


class LedgerServicePort(Protocol):
    def record(self, event: MinionProgressEvent | MinionTerminalEvent | CheckpointEvent) -> None:
        ...


class TaskingServicePort(Protocol):
    def build_context_pack(self, work_order_id: str, goal: str) -> TaskContextPack:
        ...
