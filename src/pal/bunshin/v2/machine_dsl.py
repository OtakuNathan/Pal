from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Mapping

from pal.bunshin.v2.contracts import ActionEnvelope, AggregateType, TransitionSpec
from pal.bunshin.v2.role_contracts import RoleActivation


TargetResolver = Callable[[Mapping[str, Any], ActionEnvelope], str]


class StateClass(StrEnum):
    CATALOG = "catalog"
    TERMINAL = "terminal"
    WORKER_LIVENESS = "worker_liveness"
    HUMAN_WAIT = "human_wait"
    OPERATOR_WAIT = "operator_wait"
    PAUSED = "paused"
    DEPENDENCY_WAIT = "dependency_wait"
    CHILD_WAIT = "child_wait"
    OUTBOX_WAIT = "outbox_wait"


class ControlIntent(StrEnum):
    PAUSE = "pause"
    CANCEL = "cancel"


class ControlDisposition(StrEnum):
    REQUEST = "request"
    WAIT = "wait"
    SETTLED = "settled"


class ReconciliationKind(StrEnum):
    """How startup recovery owns a state after process-local work is lost."""

    ADMIT_ROLE = "admit_role"
    RESUME_ROLE = "resume_role"
    RECONCILE_STATE = "reconcile_state"
    CONTROL_ROLE = "control_role"


@dataclass(frozen=True)
class StateRuntimeSpec:
    """Finite role ownership and recovery behavior for one durable state."""

    activations: frozenset[RoleActivation]
    reconciliation: ReconciliationKind

    def __post_init__(self) -> None:
        if not self.activations:
            raise ValueError("a runtime state must declare at least one role activation")


@dataclass(frozen=True)
class DynamicTarget:
    """Executable target resolver with a finite formal target declaration."""

    name: str
    target_states: frozenset[str]
    resolver: TargetResolver

    def __call__(
        self,
        payload: Mapping[str, Any],
        action: ActionEnvelope,
    ) -> str:
        return self.resolver(payload, action)


def target_resolver(*target_states: str, name: str = ""):
    """Bind executable target logic to the finite relation exported to TLA+."""

    declared = frozenset(str(state) for state in target_states)
    if not declared:
        raise ValueError("a dynamic target resolver must declare at least one target state")

    def decorate(resolver: TargetResolver) -> DynamicTarget:
        return DynamicTarget(
            name=str(name or getattr(resolver, "__name__", "dynamic_target")),
            target_states=declared,
            resolver=resolver,
        )

    return decorate


def transition_target_states(transition: TransitionSpec) -> frozenset[str]:
    target = transition.target_state
    if not callable(target):
        return frozenset({str(target)})
    return frozenset(str(state) for state in getattr(target, "target_states", ()))


@dataclass(frozen=True)
class ControlPolicy:
    request_action: str
    settled_states: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class MachineSpec:
    """Declarative runtime/formal state-machine source of truth."""

    aggregate_type: AggregateType
    state_classes: Mapping[str, StateClass]
    transitions: tuple[TransitionSpec, ...]
    control_policies: Mapping[ControlIntent, ControlPolicy] = field(default_factory=dict)
    runtime_states: Mapping[str, StateRuntimeSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        states = set(self.state_classes)
        unknown_runtime_states = set(self.runtime_states) - states
        if unknown_runtime_states:
            raise ValueError(
                f"{self.aggregate_type.value} runtime metadata references unknown states: "
                + ", ".join(sorted(unknown_runtime_states))
            )
        required_runtime_states = {
            state
            for state, state_class in self.state_classes.items()
            if state_class == StateClass.WORKER_LIVENESS
        }
        missing_runtime_states = required_runtime_states - set(self.runtime_states)
        extra_runtime_states = set(self.runtime_states) - required_runtime_states
        if missing_runtime_states or extra_runtime_states:
            details: list[str] = []
            if missing_runtime_states:
                details.append("missing " + ", ".join(sorted(missing_runtime_states)))
            if extra_runtime_states:
                details.append("non-liveness " + ", ".join(sorted(extra_runtime_states)))
            raise ValueError(
                f"{self.aggregate_type.value} runtime state metadata must exactly cover "
                "worker-liveness states: " + "; ".join(details)
            )
        keys: set[tuple[str | None, str]] = set()
        for transition in self.transitions:
            if transition.aggregate_type != self.aggregate_type:
                raise ValueError(
                    f"{self.aggregate_type.value} machine contains a transition for "
                    f"{transition.aggregate_type.value}"
                )
            key = (transition.source_state, transition.action_type)
            if key in keys:
                raise ValueError(
                    f"duplicate transition in {self.aggregate_type.value}: {key}"
                )
            keys.add(key)
            if transition.source_state is not None and str(transition.source_state) not in states:
                raise ValueError(
                    f"unknown source state in {self.aggregate_type.value}: "
                    f"{transition.source_state}"
                )
            targets = transition_target_states(transition)
            if callable(transition.target_state) and not targets:
                raise ValueError(
                    f"dynamic transition in {self.aggregate_type.value} has no formal targets: "
                    f"{transition.source_state} + {transition.action_type}"
                )
            unknown_targets = targets - states
            if unknown_targets:
                raise ValueError(
                    f"unknown target states in {self.aggregate_type.value}: "
                    + ", ".join(sorted(unknown_targets))
                )
        for intent, policy in self.control_policies.items():
            unknown = set(policy.settled_states) - states
            if unknown:
                raise ValueError(
                    f"{self.aggregate_type.value} {intent.value} policy references unknown states: "
                    + ", ".join(sorted(unknown))
                )
            request_states = self.control_request_states(intent)
            overlap = request_states & set(policy.settled_states)
            if overlap:
                raise ValueError(
                    f"{self.aggregate_type.value} {intent.value} states cannot both request and settle: "
                    + ", ".join(sorted(overlap))
                )
        initial = set(self.initial_states)
        if not initial:
            raise ValueError(
                f"{self.aggregate_type.value} machine has no creation transition"
            )
        reachable = set(initial)
        pending = list(initial)
        while pending:
            source = pending.pop()
            for transition in self.transitions:
                if str(transition.source_state) != source:
                    continue
                for target in transition_target_states(transition):
                    if target in reachable:
                        continue
                    reachable.add(target)
                    pending.append(target)
        unreachable = states - reachable
        if unreachable:
            raise ValueError(
                f"{self.aggregate_type.value} machine contains unreachable states: "
                + ", ".join(sorted(unreachable))
            )

    @property
    def states(self) -> frozenset[str]:
        return frozenset(self.state_classes)

    @property
    def initial_states(self) -> frozenset[str]:
        return frozenset(
            target
            for transition in self.transitions
            if transition.source_state is None
            for target in transition_target_states(transition)
        )

    def legal_actions(self, state: str | None) -> frozenset[str]:
        return frozenset(
            transition.action_type
            for transition in self.transitions
            if transition.source_state == state
        )

    def transition_targets(self, transition: TransitionSpec) -> frozenset[str]:
        return transition_target_states(transition)

    def control_request_states(self, intent: ControlIntent) -> frozenset[str]:
        policy = self.control_policies.get(intent)
        if policy is None:
            return frozenset()
        return frozenset(
            str(transition.source_state)
            for transition in self.transitions
            if transition.source_state is not None
            and transition.action_type == policy.request_action
        )

    def control_disposition(
        self,
        intent: ControlIntent,
        state: str,
    ) -> ControlDisposition:
        policy = self.control_policies.get(intent)
        if policy is None:
            raise ValueError(
                f"{self.aggregate_type.value} does not participate in {intent.value} control"
            )
        if state in policy.settled_states:
            return ControlDisposition.SETTLED
        if policy.request_action in self.legal_actions(state):
            return ControlDisposition.REQUEST
        return ControlDisposition.WAIT

    def control_states(
        self,
        intent: ControlIntent,
        disposition: ControlDisposition,
    ) -> frozenset[str]:
        return frozenset(
            state
            for state in self.states
            if self.control_disposition(intent, state) == disposition
        )

    def runtime_for_state(self, state: str) -> StateRuntimeSpec | None:
        return self.runtime_states.get(str(state))

    def states_for_activation(self, activation: RoleActivation) -> frozenset[str]:
        return frozenset(
            state
            for state, runtime in self.runtime_states.items()
            if activation in runtime.activations
        )
