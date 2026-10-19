"""Resident scheduling reducer; every transition is synchronous and owner-local.

The event pump may keep running while a turn awaits I/O. Only a matching
teardown receipt releases its slot. Barriers are orthogonal to turn phase:
quiescence can drain an owner without making the resident available again.
Payload queues and asynchronous effects belong to the core, not this reducer.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum


class CoreTurnPhase(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    FINALIZING = "finalizing"


class ResidentTurnKind(str, Enum):
    CHAT = "chat"
    PROACTIVE = "proactive"
    BACKGROUND = "background"


class SchedulingEvent(str, Enum):
    ADMIT = "admit"
    PROGRAM_FINISHED = "program_finished"
    CANCEL = "cancel"
    TASK_FINISHED = "task_finished"


# This table drives production transitions and the generated TLA+ relation.
PHASE_TRANSITIONS = {
    (CoreTurnPhase.IDLE, SchedulingEvent.ADMIT): CoreTurnPhase.RUNNING,
    (CoreTurnPhase.RUNNING, SchedulingEvent.PROGRAM_FINISHED): CoreTurnPhase.FINALIZING,
    (CoreTurnPhase.RUNNING, SchedulingEvent.CANCEL): CoreTurnPhase.FINALIZING,
    (CoreTurnPhase.FINALIZING, SchedulingEvent.PROGRAM_FINISHED): CoreTurnPhase.FINALIZING,
    (CoreTurnPhase.FINALIZING, SchedulingEvent.CANCEL): CoreTurnPhase.FINALIZING,
    (CoreTurnPhase.FINALIZING, SchedulingEvent.TASK_FINISHED): CoreTurnPhase.IDLE,
}


@dataclass(frozen=True)
class TurnLease:
    turn_id: str
    kind: ResidentTurnKind
    generation: int

    def __post_init__(self) -> None:
        if not self.turn_id or self.generation < 1:
            raise ValueError("turn lease requires an identity and a positive generation")
        object.__setattr__(self, "kind", ResidentTurnKind(self.kind))


@dataclass(frozen=True)
class SchedulingState:
    phase: CoreTurnPhase = CoreTurnPhase.IDLE
    owner: TurnLease | None = None
    generation: int = 0
    barriers: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        object.__setattr__(self, "phase", CoreTurnPhase(self.phase))
        if (self.phase == CoreTurnPhase.IDLE) != (self.owner is None):
            raise ValueError("resident phase and owner disagree")
        if self.owner is not None and self.owner.generation != self.generation:
            raise ValueError("resident owner generation is stale")


@dataclass(frozen=True)
class SchedulingDecision:
    state: SchedulingState
    accepted: bool
    reason: str = ""


def admission_reason(
    state: SchedulingState, kind: ResidentTurnKind, *,
    pending_users: bool = False, other_work: bool = False,
) -> str:
    if state.barriers:
        return "barrier"
    if state.phase != CoreTurnPhase.IDLE:
        return state.phase.value
    if other_work:
        return "draining"
    if kind != ResidentTurnKind.CHAT and pending_users:
        return "user_priority"
    return ""


def reduce_scheduling(
    state: SchedulingState, event: SchedulingEvent, *,
    turn_id: str = "", kind: ResidentTurnKind = ResidentTurnKind.CHAT,
    lease: TurnLease | None = None, pending_users: bool = False,
    other_work: bool = False,
) -> SchedulingDecision:
    event, kind = SchedulingEvent(event), ResidentTurnKind(kind)
    target = PHASE_TRANSITIONS.get((state.phase, event))
    if event == SchedulingEvent.ADMIT:
        reason = admission_reason(state, kind, pending_users=pending_users, other_work=other_work)
        if reason:
            return SchedulingDecision(state, False, reason)
        if not turn_id:
            raise ValueError("resident admission requires a turn id")
        assert target is not None
        generation = state.generation + 1
        return SchedulingDecision(replace(
            state, phase=target, generation=generation,
            owner=TurnLease(turn_id, kind, generation),
        ), True)
    if lease is None or lease != state.owner:
        return SchedulingDecision(state, False, "stale_owner")
    if target is None:
        return SchedulingDecision(state, False, "illegal_transition")
    return SchedulingDecision(replace(
        state, phase=target,
        owner=None if event == SchedulingEvent.TASK_FINISHED else state.owner,
    ), True)


@dataclass
class CoreScheduler:
    """Single owner of the reducer snapshot; never awaits or starts tasks."""

    state: SchedulingState = field(default_factory=SchedulingState)

    def transition(self, event: SchedulingEvent, **kwargs) -> SchedulingDecision:
        decision = reduce_scheduling(self.state, event, **kwargs)
        if decision.accepted:
            self.state = decision.state
        return decision

    def set_barrier(self, name: str, enabled: bool) -> None:
        barriers = self.state.barriers
        updated = barriers | {name} if enabled else barriers - {name}
        self.state = replace(self.state, barriers=frozenset(updated))

    def snapshot(self) -> dict:
        owner = self.state.owner
        return {
            "phase": self.state.phase.value,
            "turn_id": owner.turn_id if owner else "",
            "turn_kind": owner.kind.value if owner else "",
            "generation": self.state.generation,
            "barriers": sorted(self.state.barriers),
        }


def render_scheduling_topology() -> str:
    """Generate the finite implementation relation checked by the TLC suite."""
    rows = ",\n".join(
        f'    <<"{source.value}", "{event.value}", "{target.value}">>'
        for (source, event), target in PHASE_TRANSITIONS.items()
    )
    return f'''--------------------- MODULE ResidentSchedulingTopology ---------------------
EXTENDS TLC
\\* Generated by pal.core.scheduling; do not edit by hand.
States == {{"idle", "running", "finalizing"}}
Transitions == {{
{rows}
}}
VARIABLE phase
Init == phase = "idle"
Next == \\E t \\in Transitions: /\\ phase = t[1] /\\ phase' = t[3]
TypeOK == phase \\in States
NoDirectRunningRelease == \\A t \\in Transitions:
    t[1] = "running" => t[3] # "idle"
OnlyTeardownReleases == \\A t \\in Transitions:
    t[3] = "idle" => t[2] = "task_finished"
Spec == Init /\\ [][Next]_phase
=============================================================================
'''
