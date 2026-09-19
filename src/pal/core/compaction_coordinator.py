"""Compaction admission tickets: the single owner token gating a logical scope.

One ticket per logical scope at a time (I05). The ticket is the scope's
blocking reason while a compaction transaction runs; it is deliberately
NOT a second global quiesce boolean (see FAILURE_MODES "is_compacting").

All mutating calls must run while the caller holds the scope's channel
turn transition lock; claim/release/cancel verify ticket identity so a
late ``finally`` from a previous owner can never release a successor's
gate (F07) and no ``finally`` clears unrelated quiesce owners (Q13).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Mapping
from uuid import uuid4


class CompactionPhase(str, Enum):
    CLAIMED = "claimed"
    GENERATING = "generating"
    READY = "ready"
    COMMITTED = "committed"
    RELEASED = "released"


class CompactionTrigger(str, Enum):
    MANUAL = "manual"
    MANUAL_HOT = "manual_hot"
    AUTO = "auto"


@dataclass(frozen=True)
class CompactionTicket:
    scope: str
    op_id: str = field(default_factory=lambda: uuid4().hex)
    trigger: CompactionTrigger = CompactionTrigger.MANUAL
    phase: CompactionPhase = CompactionPhase.CLAIMED
    cancelled: bool = False
    cancel_reason: str = ""
    claimed_at_monotonic: float = field(default_factory=time.monotonic)
    deadline_seconds: float = 180.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "scope", str(self.scope or "").strip())
        if not self.scope:
            raise ValueError("compaction ticket requires a scope")
        object.__setattr__(self, "op_id", str(self.op_id or "").strip() or uuid4().hex)
        object.__setattr__(self, "trigger", CompactionTrigger(self.trigger))
        object.__setattr__(self, "phase", CompactionPhase(self.phase))
        object.__setattr__(self, "cancelled", bool(self.cancelled))
        object.__setattr__(self, "cancel_reason", str(self.cancel_reason or ""))
        deadline = float(self.deadline_seconds)
        if deadline <= 0:
            raise ValueError("compaction ticket deadline must be positive")
        object.__setattr__(self, "deadline_seconds", deadline)

    @property
    def expired(self) -> bool:
        return (time.monotonic() - self.claimed_at_monotonic) > self.deadline_seconds

    def with_phase(self, phase: CompactionPhase) -> "CompactionTicket":
        return replace(self, phase=CompactionPhase(phase))

    def with_cancel(self, reason: str) -> "CompactionTicket":
        return replace(self, cancelled=True, cancel_reason=str(reason or ""))


def compaction_gate_active(state: Any) -> bool:
    """True when the host state holds a live compaction ticket (any scope).

    Read-only helper safe to call from any code path that received the
    runtime state; hosts without the field behave as ungated.
    """
    tickets = getattr(state, "compaction_tickets", None)
    if not isinstance(tickets, Mapping):
        return False
    return any(ticket.phase != CompactionPhase.RELEASED for ticket in tickets.values())


class CompactionGate:
    """Claim/cancel/release boundary for one runtime host's compaction tickets.

    The host stores tickets on ``state.compaction_tickets`` so snapshots and
    debug tooling observe the same authority the runtime uses. Callers must
    hold the channel turn transition lock for claim/cancel/release; the gate
    asserts that when a lock object is supplied.
    """

    def __init__(self, state: Any, *, transition_lock: Any | None = None) -> None:
        self._state = state
        self._lock = transition_lock

    # ── inspection ───────────────────────────────────────────────────

    def ticket_for(self, scope: str) -> CompactionTicket | None:
        tickets = getattr(self._state, "compaction_tickets", None)
        if not isinstance(tickets, dict):
            return None
        return tickets.get(str(scope))

    def is_active(self, scope: str) -> bool:
        ticket = self.ticket_for(scope)
        return ticket is not None and ticket.phase != CompactionPhase.RELEASED

    def is_cancelled(self, scope: str, op_id: str) -> bool:
        ticket = self.ticket_for(scope)
        return (
            ticket is not None
            and ticket.op_id == str(op_id)
            and bool(ticket.cancelled)
        )

    # ── mutation (transition lock held) ───────────────────────────────

    def _require_lock(self) -> None:
        if self._lock is None:
            return
        locked = getattr(self._lock, "locked", None)
        if callable(locked) and not locked():
            raise RuntimeError(
                "compaction gate mutation requires the channel turn transition lock"
            )

    def _tickets(self) -> dict[str, CompactionTicket]:
        tickets = getattr(self._state, "compaction_tickets", None)
        if not isinstance(tickets, dict):
            raise RuntimeError("runtime state does not expose compaction_tickets")
        return tickets

    def claim(
        self,
        scope: str,
        *,
        trigger: CompactionTrigger | str,
        deadline_seconds: float = 180.0,
    ) -> CompactionTicket | None:
        """Claim the scope's single admission slot; None when already held."""
        self._require_lock()
        tickets = self._tickets()
        scope_key = str(scope)
        current = tickets.get(scope_key)
        if current is not None and current.phase != CompactionPhase.RELEASED:
            return None
        ticket = CompactionTicket(
            scope=scope_key,
            trigger=CompactionTrigger(trigger),
            deadline_seconds=deadline_seconds,
        )
        tickets[scope_key] = ticket
        return ticket

    def advance(self, ticket: CompactionTicket, phase: CompactionPhase) -> CompactionTicket | None:
        """Move the holder's phase forward; None when the ticket is no longer current."""
        self._require_lock()
        current = self._tickets().get(ticket.scope)
        if current is None or current.op_id != ticket.op_id:
            return None
        updated = current.with_phase(phase)
        self._tickets()[ticket.scope] = updated
        return updated

    def cancel(self, scope: str, *, reason: str) -> CompactionTicket | None:
        """Revoke a ticket's commit eligibility; the holder keeps the gate
        until it releases, so a successor cannot claim mid-teardown."""
        self._require_lock()
        current = self._tickets().get(str(scope))
        if current is None or current.phase == CompactionPhase.RELEASED:
            return None
        updated = current.with_cancel(reason)
        self._tickets()[str(scope)] = updated
        return updated

    def cancel_all(self, *, reason: str) -> tuple[str, ...]:
        self._require_lock()
        cancelled: list[str] = []
        for scope, current in list(self._tickets().items()):
            if current.phase != CompactionPhase.RELEASED and not current.cancelled:
                self._tickets()[scope] = current.with_cancel(reason)
                cancelled.append(scope)
        return tuple(cancelled)

    def release(self, ticket: CompactionTicket) -> bool:
        """Remove the ticket if it is still the current holder (identity-checked)."""
        self._require_lock()
        scope_key = ticket.scope
        current = self._tickets().get(scope_key)
        if current is None or current.op_id != ticket.op_id:
            return False
        self._tickets()[scope_key] = current.with_phase(CompactionPhase.RELEASED)
        del self._tickets()[scope_key]
        return True


__all__ = [
    "CompactionGate",
    "CompactionPhase",
    "CompactionTicket",
    "CompactionTrigger",
    "compaction_gate_active",
]
