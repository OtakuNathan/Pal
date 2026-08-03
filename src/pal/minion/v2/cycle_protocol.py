from __future__ import annotations

from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any, Final, Mapping


class CycleKind(StrEnum):
    PLAN = "plan"
    NODE = "node"


class CycleSlot(StrEnum):
    PRODUCER = "producer"
    CHECKER = "checker"


class AssignmentKind(StrEnum):
    INITIAL = "initial"
    REVISION = "revision"
    REPAIR = "repair"
    RECHECK = "recheck"
    RESUME = "resume"


class ProduceCheckState(StrEnum):
    PRODUCER_READY = "PRODUCER_READY"
    PRODUCING = "PRODUCING"
    CHECKER_READY = "CHECKER_READY"
    CHECKING = "CHECKING"
    REPAIR_READY = "REPAIR_READY"
    ACCEPTED = "ACCEPTED"
    WAITING_EXTERNAL = "WAITING_EXTERNAL"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    TRIAGE_REQUIRED = "TRIAGE_REQUIRED"


class PlanCycleState(StrEnum):
    PRODUCER_READY = ProduceCheckState.PRODUCER_READY
    PRODUCING = ProduceCheckState.PRODUCING
    CHECKER_READY = ProduceCheckState.CHECKER_READY
    CHECKING = ProduceCheckState.CHECKING
    REPAIR_READY = ProduceCheckState.REPAIR_READY
    HUMAN_REVIEW = "HUMAN_REVIEW"
    ACCEPTED = ProduceCheckState.ACCEPTED
    REJECTED = "REJECTED"
    WAITING_EXTERNAL = ProduceCheckState.WAITING_EXTERNAL
    PAUSE_REQUESTED = ProduceCheckState.PAUSE_REQUESTED
    PAUSED = ProduceCheckState.PAUSED
    CANCEL_REQUESTED = ProduceCheckState.CANCEL_REQUESTED
    CANCELLED = ProduceCheckState.CANCELLED
    TRIAGE_REQUIRED = ProduceCheckState.TRIAGE_REQUIRED


class NodeCycleState(StrEnum):
    BLOCKED = "BLOCKED"
    PRODUCER_READY = ProduceCheckState.PRODUCER_READY
    PRODUCING = ProduceCheckState.PRODUCING
    CHECKER_READY = ProduceCheckState.CHECKER_READY
    CHECKING = ProduceCheckState.CHECKING
    REPAIR_READY = ProduceCheckState.REPAIR_READY
    ACCEPTED = ProduceCheckState.ACCEPTED
    STALE = "STALE"
    WAITING_EXTERNAL = ProduceCheckState.WAITING_EXTERNAL
    PAUSE_REQUESTED = ProduceCheckState.PAUSE_REQUESTED
    PAUSED = ProduceCheckState.PAUSED
    CANCEL_REQUESTED = ProduceCheckState.CANCEL_REQUESTED
    CANCELLED = ProduceCheckState.CANCELLED
    TRIAGE_REQUIRED = ProduceCheckState.TRIAGE_REQUIRED


class CycleAction(StrEnum):
    UNBLOCK = "unblock"
    START_PRODUCER = "start_producer"
    PRODUCER_SUBMITTED = "producer_submitted"
    PRODUCER_REJECTED = "producer_rejected"
    START_CHECKER = "start_checker"
    CHECKER_ACCEPTED = "checker_accepted"
    CHECKER_REJECTED = "checker_rejected"
    CHECKER_RETRY = "checker_retry"
    REQUEST_HUMAN_REVIEW = "request_human_review"
    HUMAN_ACCEPTED = "human_accepted"
    HUMAN_REJECTED = "human_rejected"
    HUMAN_EDITED = "human_edited"
    MARK_STALE = "mark_stale"
    WAIT_EXTERNAL = "wait_external"
    EXTERNAL_RESUMED = "external_resumed"
    REQUEST_PAUSE = "request_pause"
    PAUSED = "paused"
    RESUME = "resume"
    REQUEST_CANCEL = "request_cancel"
    CANCELLED = "cancelled"
    REQUIRE_TRIAGE = "require_triage"
    RESOLVE_TRIAGE = "resolve_triage"


class CycleTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class CycleAssignment:
    slot: CycleSlot
    kind: AssignmentKind
    generation: int
    input_fingerprint: str

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("assignment generation must be positive")
        if not self.input_fingerprint:
            raise ValueError("assignment input_fingerprint is required")


@dataclass(frozen=True)
class CycleVerdict:
    accepted: bool
    generation: int
    finding_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.generation < 1:
            raise ValueError("verdict generation must be positive")
        if self.accepted and self.finding_refs:
            raise ValueError("an accepted verdict cannot contain findings")
        if not self.accepted and not self.finding_refs:
            raise ValueError("a rejected verdict requires findings")


_COMMON_TRANSITIONS: Final[Mapping[tuple[str, CycleAction], str]] = {
    ("PRODUCER_READY", CycleAction.START_PRODUCER): "PRODUCING",
    ("REPAIR_READY", CycleAction.START_PRODUCER): "PRODUCING",
    ("PRODUCING", CycleAction.PRODUCER_SUBMITTED): "CHECKER_READY",
    ("PRODUCING", CycleAction.PRODUCER_REJECTED): "REPAIR_READY",
    ("CHECKER_READY", CycleAction.START_CHECKER): "CHECKING",
    ("CHECKING", CycleAction.CHECKER_REJECTED): "REPAIR_READY",
    ("CHECKING", CycleAction.CHECKER_RETRY): "CHECKER_READY",
    ("CHECKING", CycleAction.CHECKER_ACCEPTED): "ACCEPTED",
}


_QUIESCENT = frozenset(
    {
        "PRODUCER_READY",
        "CHECKER_READY",
        "REPAIR_READY",
        "ACCEPTED",
        "BLOCKED",
        "STALE",
        "WAITING_EXTERNAL",
        "PAUSED",
        "CANCELLED",
        "REJECTED",
        "TRIAGE_REQUIRED",
        "HUMAN_REVIEW",
    }
)


@dataclass(frozen=True)
class PlanCycle:
    cycle_id: str
    generation: int = 1
    state: PlanCycleState = PlanCycleState.PRODUCER_READY
    active_assignment: CycleAssignment | None = None
    product_ref: str = ""
    accepted_product_ref: str = ""
    last_verdict: CycleVerdict | None = None
    resume_state: PlanCycleState | None = None

    def __post_init__(self) -> None:
        _validate_cycle_generation(self)

    @property
    def kind(self) -> CycleKind:
        return CycleKind.PLAN

    @property
    def is_running(self) -> bool:
        return self.state in {
            PlanCycleState.PRODUCING,
            PlanCycleState.CHECKING,
        }

    @property
    def is_quiescent(self) -> bool:
        return self.state.value in _QUIESCENT

    def transition(
        self,
        action: CycleAction,
        *,
        assignment: CycleAssignment | None = None,
        product_ref: str = "",
        verdict: CycleVerdict | None = None,
    ) -> "PlanCycle":
        return _transition_plan(
            self,
            action,
            assignment=assignment,
            product_ref=product_ref,
            verdict=verdict,
        )


@dataclass(frozen=True)
class NodeCycle:
    cycle_id: str
    node_name: str
    generation: int = 1
    state: NodeCycleState = NodeCycleState.BLOCKED
    active_assignment: CycleAssignment | None = None
    product_ref: str = ""
    accepted_product_ref: str = ""
    last_verdict: CycleVerdict | None = None
    resume_state: NodeCycleState | None = None

    def __post_init__(self) -> None:
        if not self.node_name:
            raise ValueError("node cycle requires node_name")
        _validate_cycle_generation(self)

    @property
    def kind(self) -> CycleKind:
        return CycleKind.NODE

    @property
    def is_running(self) -> bool:
        return self.state in {
            NodeCycleState.PRODUCING,
            NodeCycleState.CHECKING,
        }

    @property
    def is_quiescent(self) -> bool:
        return self.state.value in _QUIESCENT

    def transition(
        self,
        action: CycleAction,
        *,
        assignment: CycleAssignment | None = None,
        product_ref: str = "",
        verdict: CycleVerdict | None = None,
    ) -> "NodeCycle":
        return _transition_node(
            self,
            action,
            assignment=assignment,
            product_ref=product_ref,
            verdict=verdict,
        )


def plan_cycle_from_mapping(value: Mapping[str, Any]) -> PlanCycle:
    payload = dict(value)
    return PlanCycle(
        cycle_id=str(payload.get("cycle_id") or ""),
        generation=int(payload.get("generation") or 0),
        state=PlanCycleState(str(payload.get("state") or "")),
        active_assignment=_assignment_from_mapping(
            payload.get("active_assignment")
        ),
        product_ref=str(payload.get("product_ref") or ""),
        accepted_product_ref=str(
            payload.get("accepted_product_ref") or ""
        ),
        last_verdict=_verdict_from_mapping(payload.get("last_verdict")),
        resume_state=(
            PlanCycleState(str(payload["resume_state"]))
            if payload.get("resume_state")
            else None
        ),
    )


def node_cycle_from_mapping(value: Mapping[str, Any]) -> NodeCycle:
    payload = dict(value)
    return NodeCycle(
        cycle_id=str(payload.get("cycle_id") or ""),
        node_name=str(payload.get("node_name") or ""),
        generation=int(payload.get("generation") or 0),
        state=NodeCycleState(str(payload.get("state") or "")),
        active_assignment=_assignment_from_mapping(
            payload.get("active_assignment")
        ),
        product_ref=str(payload.get("product_ref") or ""),
        accepted_product_ref=str(
            payload.get("accepted_product_ref") or ""
        ),
        last_verdict=_verdict_from_mapping(payload.get("last_verdict")),
        resume_state=(
            NodeCycleState(str(payload["resume_state"]))
            if payload.get("resume_state")
            else None
        ),
    )


def _assignment_from_mapping(value: Any) -> CycleAssignment | None:
    if not isinstance(value, Mapping):
        return None
    return CycleAssignment(
        slot=CycleSlot(str(value.get("slot") or "")),
        kind=AssignmentKind(str(value.get("kind") or "")),
        generation=int(value.get("generation") or 0),
        input_fingerprint=str(value.get("input_fingerprint") or ""),
    )


def _validate_cycle_generation(cycle: PlanCycle | NodeCycle) -> None:
    if not cycle.cycle_id or cycle.generation < 1:
        raise ValueError("cycle identity and positive generation are required")
    if (
        cycle.active_assignment is not None
        and cycle.active_assignment.generation != cycle.generation
    ):
        raise ValueError("active assignment belongs to another cycle generation")
    if (
        cycle.last_verdict is not None
        and cycle.last_verdict.generation != cycle.generation
    ):
        raise ValueError("last verdict belongs to another cycle generation")


def _verdict_from_mapping(value: Any) -> CycleVerdict | None:
    if not isinstance(value, Mapping):
        return None
    return CycleVerdict(
        accepted=bool(value.get("accepted")),
        generation=int(value.get("generation") or 0),
        finding_refs=tuple(
            str(item) for item in list(value.get("finding_refs") or [])
        ),
    )


def _start_assignment(
    cycle: PlanCycle | NodeCycle,
    assignment: CycleAssignment | None,
    expected_slot: CycleSlot,
) -> CycleAssignment:
    if assignment is None:
        raise CycleTransitionError("starting a role requires an assignment")
    if assignment.slot != expected_slot:
        raise CycleTransitionError(
            f"expected {expected_slot.value} assignment, got {assignment.slot.value}"
        )
    if assignment.generation != cycle.generation:
        raise CycleTransitionError(
            "assignment generation does not match the cycle generation"
        )
    return assignment


def _validate_submission(
    cycle: PlanCycle | NodeCycle,
    product_ref: str,
) -> None:
    if not product_ref:
        raise CycleTransitionError("producer submission requires a product ref")
    assignment = cycle.active_assignment
    if assignment is None or assignment.slot != CycleSlot.PRODUCER:
        raise CycleTransitionError("producer submission has no active producer")


def _validate_verdict(
    cycle: PlanCycle | NodeCycle,
    verdict: CycleVerdict | None,
    *,
    accepted: bool,
) -> CycleVerdict:
    if verdict is None:
        raise CycleTransitionError("checker completion requires a verdict")
    if verdict.accepted != accepted:
        raise CycleTransitionError("checker action and verdict disagree")
    if verdict.generation != cycle.generation:
        raise CycleTransitionError(
            "checker verdict was produced for a different graph generation"
        )
    assignment = cycle.active_assignment
    if assignment is None or assignment.slot != CycleSlot.CHECKER:
        raise CycleTransitionError("checker completion has no active checker")
    return verdict


def _transition_plan(
    cycle: PlanCycle,
    action: CycleAction,
    *,
    assignment: CycleAssignment | None,
    product_ref: str,
    verdict: CycleVerdict | None,
) -> PlanCycle:
    state = cycle.state.value
    if action == CycleAction.REQUEST_HUMAN_REVIEW and state == "ACCEPTED":
        return replace(cycle, state=PlanCycleState.HUMAN_REVIEW)
    if action == CycleAction.HUMAN_ACCEPTED and state == "HUMAN_REVIEW":
        return replace(cycle, state=PlanCycleState.ACCEPTED)
    if action == CycleAction.HUMAN_REJECTED and state == "HUMAN_REVIEW":
        return replace(cycle, state=PlanCycleState.REJECTED)
    if action == CycleAction.HUMAN_EDITED and state == "HUMAN_REVIEW":
        return replace(
            cycle,
            generation=cycle.generation + 1,
            state=PlanCycleState.REPAIR_READY,
            active_assignment=None,
            product_ref="",
            accepted_product_ref="",
            last_verdict=None,
        )
    target = _common_target(state, action)
    if target is None:
        return _common_control_transition(cycle, action)
    if action == CycleAction.START_PRODUCER:
        assignment = _start_assignment(cycle, assignment, CycleSlot.PRODUCER)
        return replace(
            cycle,
            state=PlanCycleState(target),
            active_assignment=assignment,
        )
    if action == CycleAction.PRODUCER_SUBMITTED:
        _validate_submission(cycle, product_ref)
        return replace(
            cycle,
            state=PlanCycleState(target),
            active_assignment=None,
            product_ref=product_ref,
        )
    if action == CycleAction.PRODUCER_REJECTED:
        return replace(
            cycle,
            state=PlanCycleState(target),
            active_assignment=None,
        )
    if action == CycleAction.START_CHECKER:
        assignment = _start_assignment(cycle, assignment, CycleSlot.CHECKER)
        return replace(
            cycle,
            state=PlanCycleState(target),
            active_assignment=assignment,
        )
    if action in {
        CycleAction.CHECKER_ACCEPTED,
        CycleAction.CHECKER_REJECTED,
        CycleAction.CHECKER_RETRY,
    }:
        verdict = _validate_verdict(
            cycle,
            verdict,
            accepted=action == CycleAction.CHECKER_ACCEPTED,
        )
        return replace(
            cycle,
            state=PlanCycleState(target),
            active_assignment=None,
            last_verdict=verdict,
            accepted_product_ref=(
                cycle.product_ref
                if action == CycleAction.CHECKER_ACCEPTED
                else cycle.accepted_product_ref
            ),
        )
    return replace(cycle, state=PlanCycleState(target))


def _transition_node(
    cycle: NodeCycle,
    action: CycleAction,
    *,
    assignment: CycleAssignment | None,
    product_ref: str,
    verdict: CycleVerdict | None,
) -> NodeCycle:
    state = cycle.state.value
    if action == CycleAction.UNBLOCK and state == "BLOCKED":
        return replace(cycle, state=NodeCycleState.PRODUCER_READY)
    if action == CycleAction.MARK_STALE and state == "ACCEPTED":
        return replace(
            cycle,
            state=NodeCycleState.STALE,
            active_assignment=None,
            last_verdict=None,
        )
    if action == CycleAction.UNBLOCK and state == "STALE":
        return replace(cycle, state=NodeCycleState.PRODUCER_READY)
    target = _common_target(state, action)
    if target is None:
        return _common_control_transition(cycle, action)
    if action == CycleAction.START_PRODUCER:
        assignment = _start_assignment(cycle, assignment, CycleSlot.PRODUCER)
        return replace(
            cycle,
            state=NodeCycleState(target),
            active_assignment=assignment,
        )
    if action == CycleAction.PRODUCER_SUBMITTED:
        _validate_submission(cycle, product_ref)
        return replace(
            cycle,
            state=NodeCycleState(target),
            active_assignment=None,
            product_ref=product_ref,
        )
    if action == CycleAction.PRODUCER_REJECTED:
        return replace(
            cycle,
            state=NodeCycleState(target),
            active_assignment=None,
        )
    if action == CycleAction.START_CHECKER:
        assignment = _start_assignment(cycle, assignment, CycleSlot.CHECKER)
        return replace(
            cycle,
            state=NodeCycleState(target),
            active_assignment=assignment,
        )
    if action in {
        CycleAction.CHECKER_ACCEPTED,
        CycleAction.CHECKER_REJECTED,
        CycleAction.CHECKER_RETRY,
    }:
        verdict = _validate_verdict(
            cycle,
            verdict,
            accepted=action == CycleAction.CHECKER_ACCEPTED,
        )
        return replace(
            cycle,
            state=NodeCycleState(target),
            active_assignment=None,
            last_verdict=verdict,
            accepted_product_ref=(
                cycle.product_ref
                if action == CycleAction.CHECKER_ACCEPTED
                else cycle.accepted_product_ref
            ),
        )
    return replace(cycle, state=NodeCycleState(target))


def _common_target(state: str, action: CycleAction) -> str | None:
    return _COMMON_TRANSITIONS.get((state, action))


def _common_control_transition(
    cycle: PlanCycle | NodeCycle,
    action: CycleAction,
) -> PlanCycle | NodeCycle:
    enum_type = type(cycle.state)
    state = cycle.state.value
    if action == CycleAction.WAIT_EXTERNAL and state in {
        "PRODUCING",
        "CHECKING",
    }:
        return replace(
            cycle,
            state=enum_type("WAITING_EXTERNAL"),
            active_assignment=None,
            resume_state=enum_type(
                "PRODUCER_READY" if state == "PRODUCING" else "CHECKER_READY"
            ),
        )
    if action == CycleAction.EXTERNAL_RESUMED and state == "WAITING_EXTERNAL":
        target = cycle.resume_state or enum_type("PRODUCER_READY")
        return replace(cycle, state=target, resume_state=None)
    if action == CycleAction.REQUEST_PAUSE and state not in {
        "CANCELLED",
        "REJECTED",
    }:
        resume_state = (
            enum_type("PRODUCER_READY")
            if state == "PRODUCING"
            else enum_type("CHECKER_READY")
            if state == "CHECKING"
            else cycle.state
        )
        return replace(
            cycle,
            state=enum_type("PAUSE_REQUESTED"),
            resume_state=(
                resume_state
                if state not in {"PAUSE_REQUESTED", "PAUSED"}
                else cycle.resume_state
            ),
        )
    if action == CycleAction.PAUSED and state == "PAUSE_REQUESTED":
        return replace(
            cycle,
            state=enum_type("PAUSED"),
            active_assignment=None,
        )
    if action == CycleAction.RESUME and state == "PAUSED":
        target = cycle.resume_state or enum_type("PRODUCER_READY")
        return replace(cycle, state=target, resume_state=None)
    if action == CycleAction.REQUEST_CANCEL and state not in {
        "CANCELLED",
        "REJECTED",
    }:
        return replace(cycle, state=enum_type("CANCEL_REQUESTED"))
    if action == CycleAction.CANCELLED and state == "CANCEL_REQUESTED":
        return replace(
            cycle,
            state=enum_type("CANCELLED"),
            active_assignment=None,
            resume_state=None,
        )
    if action == CycleAction.REQUIRE_TRIAGE and state not in {
        "CANCELLED",
        "REJECTED",
    }:
        resume_state = (
            enum_type("PRODUCER_READY")
            if state == "PRODUCING"
            else enum_type("CHECKER_READY")
            if state == "CHECKING"
            else cycle.state
        )
        return replace(
            cycle,
            state=enum_type("TRIAGE_REQUIRED"),
            active_assignment=None,
            resume_state=resume_state,
        )
    if action == CycleAction.RESOLVE_TRIAGE and state == "TRIAGE_REQUIRED":
        target = cycle.resume_state or enum_type("PRODUCER_READY")
        return replace(cycle, state=target, resume_state=None)
    raise CycleTransitionError(
        f"{cycle.kind.value} cycle cannot {action.value} from {state}"
    )
