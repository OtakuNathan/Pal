from __future__ import annotations

from dataclasses import dataclass, field

from pal.core.mailbox import Mailbox
from pal.shared import (
    CheckpointEvent,
    EventKind,
    MinionProgressEvent,
    MinionTerminalEvent,
    TaskContextPack,
    WorkerProgressEvent,
    WorkerTerminalEvent,
)
from pal.tasking.contracts import TaskingServicePort


@dataclass(frozen=True)
class QueuedMinionEvent:
    event_name: str
    payload: MinionProgressEvent | MinionTerminalEvent | CheckpointEvent


@dataclass
class TaskingService(TaskingServicePort):
    issued_work_orders: list[str] = field(default_factory=list)
    minion_mailbox: Mailbox[QueuedMinionEvent] = field(default_factory=Mailbox)

    def build_context_pack(self, work_order_id: str, goal: str) -> TaskContextPack:
        self.issued_work_orders.append(work_order_id)
        return TaskContextPack(work_order_id=work_order_id, goal=goal)

    def enqueue_minion_progress(self, event: MinionProgressEvent | MinionTerminalEvent | CheckpointEvent) -> None:
        if isinstance(event, MinionProgressEvent):
            event_name = EventKind.MINION_PROGRESS
        elif isinstance(event, MinionTerminalEvent):
            event_name = EventKind.MINION_TERMINAL
        else:
            event_name = EventKind.MINION_CHECKPOINT
        self.minion_mailbox.put(QueuedMinionEvent(event_name=event_name, payload=event))

    @property
    def pending_minion_events(self) -> tuple[QueuedMinionEvent, ...]:
        return self.minion_mailbox.peek_all()

    @property
    def worker_mailbox(self):
        return self.minion_mailbox

    @property
    def pending_worker_events(self):
        return self.pending_minion_events

    def enqueue_worker_progress(self, event: WorkerProgressEvent | WorkerTerminalEvent | CheckpointEvent) -> None:
        self.enqueue_minion_progress(event)
