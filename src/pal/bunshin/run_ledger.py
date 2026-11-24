"""Process-local progress for concrete runs. Durable assignment receipts belong to storage."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal, Protocol

from pal.foundation import utc_now
from pal.shared import BunshinInvocationPack


class TerminalRunStatus(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    KILLED = "killed"
    TIMEOUT = "timeout"
    SUSPENDED = "suspended"
    INTERRUPTED = "interrupted"
    BLOCKED = "blocked"


RunStatus = Literal["running", "approval_pending", "clarification_pending", "exiting"] | TerminalRunStatus
_TERMINAL_STATUSES = frozenset(TerminalRunStatus)


@dataclass
class BunshinRunState:
    bunshin_id: str
    run_id: str
    pack: BunshinInvocationPack
    process: "ProcessStatusView | None" = None
    status: RunStatus = "running"
    started_at: str = field(default_factory=utc_now)
    ended_at: str = ""
    last_error: str = ""
    last_event: dict[str, Any] = field(default_factory=dict)
    last_event_at: str = ""
    pending_approval: dict[str, Any] = field(default_factory=dict)
    pending_clarification: dict[str, Any] = field(default_factory=dict)
    pending_terminal_status: TerminalRunStatus | Literal[""] = ""
    process_group_reaped: bool = False
    assignment_id: str = ""
    attempt_id: str = ""
    producer_diagnostic_count: int = field(default=0, init=False, repr=False)

    @property
    def active(self) -> bool:
        return self.status not in _TERMINAL_STATUSES

    @property
    def accepts_interactions(self) -> bool:
        return self.active and not self.process_group_reaped and not self.pending_terminal_status

    def record_event(self, item: dict[str, Any], payload: dict[str, Any]) -> bool:
        kind = item.get("event_kind")
        if kind in {"approval_requested", "clarification_requested"} and not self.accepts_interactions:
            return False
        if kind == "approval_requested":
            self.pending_approval = payload
            self.status = "approval_pending"
        elif kind == "clarification_requested":
            self.pending_clarification = payload
            self.status = "clarification_pending"
        elif kind == "terminal":
            resolved_interactions: list[dict[str, str]] = []
            approval_id = str(self.pending_approval.get("approval_id") or "").strip()
            if approval_id:
                resolved_interactions.append(
                    {
                        "interaction_id": approval_id,
                        "interaction_kind": "bunshin_approval",
                    }
                )
            clarification_id = str(
                self.pending_clarification.get("clarification_id") or ""
            ).strip()
            if clarification_id:
                resolved_interactions.append(
                    {
                        "interaction_id": clarification_id,
                        "interaction_kind": "bunshin_clarification",
                    }
                )
            if resolved_interactions:
                payload["resolved_interactions"] = resolved_interactions
            self.pending_approval = {}
            self.pending_clarification = {}
            # A terminal IPC receipt means the worker has finished its
            # logical work, not that its process owner has finished
            # cleanup. Keep the run active until the owner confirms its
            # direct child has been reaped.
            try:
                terminal_status = TerminalRunStatus(payload.get("status") or "completed")
            except ValueError:
                terminal_status = TerminalRunStatus.FAILED
                self.last_error = f"unrecognized worker terminal status: {payload.get('status')!r}"
                payload["status"] = terminal_status.value
            if self.process_group_reaped:
                self.status = terminal_status
                self.pending_terminal_status = ""
                self.ended_at = self.ended_at or utc_now()
            else:
                self.pending_terminal_status = terminal_status
                self.status = "exiting"
        self.last_event = item
        self.last_event_at = str(item.get("created_at") or utc_now())
        return True

    def mark_reaped(self) -> None:
        if self.process_group_reaped:
            return
        self.process_group_reaped = True
        self.status = self.pending_terminal_status or TerminalRunStatus.FAILED
        self.pending_terminal_status = ""
        self.ended_at = utc_now()

    def acknowledge(self, kind: Literal["approval", "clarification"], interaction_id: str) -> None:
        # Sending control can overlap a new question or a terminal receipt.
        pending = self.pending_approval if kind == "approval" else self.pending_clarification
        if str(pending.get(f"{kind}_id") or "") != interaction_id:
            return
        if kind == "approval":
            self.pending_approval = {}
        else:
            self.pending_clarification = {}
        if self.status == f"{kind}_pending" and self.accepts_interactions:
            self.status = "running"

    def summary(self) -> dict[str, Any]:
        return {
            "bunshin_id": self.bunshin_id,
            "run_id": self.run_id,
            "invocation_id": self.pack.invocation_id,
            "assignment_id": self.assignment_id,
            "attempt_id": self.attempt_id,
            "bunshin_profile": self.pack.bunshin_profile,
            "status": str(self.status),
            "run_active": self.active,
            "pid": self.process.pid if self.active and self.process is not None else None,
            "returncode": self.process.returncode if self.process is not None else None,
            "instruction": self.pack.instruction,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "last_error": self.last_error,
            "last_event_at": self.last_event_at,
            "last_event": dict(self.last_event),
            "pending_approval": dict(self.pending_approval),
            "pending_clarification": dict(self.pending_clarification),
            "pending_terminal_status": str(self.pending_terminal_status),
            "process_group_reaped": self.process_group_reaped,
        }

    def detail(self) -> dict[str, Any]:
        return {**self.summary(), "task_context_pack": self.pack.to_dict()}


class ProcessStatusView(Protocol):
    """Read-only scalar process projection; carries no I/O authority."""

    @property
    def assignment_id(self) -> str: ...

    @property
    def attempt_id(self) -> str: ...

    @property
    def pid(self) -> int: ...

    @property
    def returncode(self) -> int | None: ...


@dataclass
class RunLedger:
    runs: dict[str, BunshinRunState]

    def require_active(self, run_id: str) -> BunshinRunState:
        state = self.runs.get(run_id)
        if state is None:
            raise KeyError(f"unknown bunshin run: {run_id}")
        if not state.active:
            raise RuntimeError(f"bunshin run is terminal: {run_id}")
        return state

    def register(self, run_id: str, bunshin_id: str, pack: BunshinInvocationPack, process: ProcessStatusView) -> None:
        self.runs[run_id] = BunshinRunState(
            bunshin_id=bunshin_id, run_id=run_id, pack=pack, process=process,
            assignment_id=process.assignment_id, attempt_id=process.attempt_id,
        )

    def reap(self, run_id: str) -> None:
        state = self.runs.get(run_id)
        if state is not None:
            state.mark_reaped()

    def active(self) -> list[BunshinRunState]:
        return [run for run in self.runs.values() if run.active]

    def summaries(self) -> list[dict[str, Any]]:
        return [run.summary() for run in sorted(self.runs.values(), key=lambda run: run.started_at)]
