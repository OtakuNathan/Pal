from __future__ import annotations

from typing import Protocol

from pal.shared import CheckpointEvent, MinionProgressEvent, MinionTerminalEvent, TaskContextPack, WorkerProgressEvent, WorkerTerminalEvent


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


WorkerManagerPort = MinionManagerPort
