from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pal.minion.v2.contracts import (
    AggregateType,
    ArchitectureRevisionState,
    DagNodeRunState,
    ExecutionEpochState,
    StandaloneReviewState,
    TaskState,
    WorkflowState,
)
from pal.minion.v2.machine_dsl import ControlDisposition, ControlIntent, StateClass
from pal.minion.v2.machines import all_machine_specs


STATE_ENUMS = {
    AggregateType.TASK: TaskState,
    AggregateType.WORKFLOW: WorkflowState,
    AggregateType.ARCHITECTURE_REVISION: ArchitectureRevisionState,
    AggregateType.EXECUTION_EPOCH: ExecutionEpochState,
    AggregateType.DAG_NODE_RUN: DagNodeRunState,
    AggregateType.STANDALONE_REVIEW: StandaloneReviewState,
}


STATE_CLASSIFICATIONS: Mapping[AggregateType, Mapping[str, StateClass]] = {
    machine.aggregate_type: dict(machine.state_classes)
    for machine in all_machine_specs()
}


def state_pairs(state_class: StateClass | None = None) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (aggregate_type.value, state)
            for aggregate_type, states in STATE_CLASSIFICATIONS.items()
            for state, classification in states.items()
            if state_class is None or classification == state_class
        )
    )


def transition_topology() -> dict[str, Any]:
    static: list[tuple[str, str, str, str]] = []
    dynamic: list[tuple[str, str, str]] = []
    resolved: list[tuple[str, str, str, str]] = []
    control: dict[str, dict[str, tuple[tuple[str, str], ...]]] = {}
    role_ownership: list[tuple[str, str, str, str, str]] = []
    for machine in all_machine_specs():
        aggregate = machine.aggregate_type.value
        role_ownership.extend(
            (
                aggregate,
                state,
                activation.role.value,
                activation.mode.value,
                runtime.reconciliation.value,
            )
            for state, runtime in machine.runtime_states.items()
            for activation in runtime.activations
        )
        for spec in machine.transitions:
            source = str(spec.source_state) if spec.source_state is not None else "<create>"
            if callable(spec.target_state):
                dynamic.append((aggregate, source, spec.action_type))
            else:
                static.append((aggregate, source, spec.action_type, str(spec.target_state)))
            resolved.extend(
                (aggregate, source, spec.action_type, target)
                for target in machine.transition_targets(spec)
            )
        for intent in machine.control_policies:
            intent_control = control.setdefault(
                intent.value,
                {
                    disposition.value: tuple()
                    for disposition in ControlDisposition
                },
            )
            for disposition in ControlDisposition:
                existing = set(intent_control[disposition.value])
                existing.update(
                    (aggregate, state)
                    for state in machine.control_states(intent, disposition)
                )
                intent_control[disposition.value] = tuple(sorted(existing))
    return {
        "states": state_pairs(),
        "initial_states": tuple(
            sorted(
                (machine.aggregate_type.value, state)
                for machine in all_machine_specs()
                for state in machine.initial_states
            )
        ),
        "classes": {
            state_class.value: state_pairs(state_class)
            for state_class in StateClass
        },
        "static_transitions": tuple(sorted(static)),
        "dynamic_transitions": tuple(sorted(dynamic)),
        "resolved_transitions": tuple(sorted(resolved)),
        "role_ownership": tuple(sorted(role_ownership)),
        "control": control,
    }


def transition_topology_digest() -> str:
    encoded = json.dumps(
        transition_topology(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _tla_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=True)


def _tla_tuple(values: tuple[str, ...]) -> str:
    return "<<" + ", ".join(_tla_string(value) for value in values) + ">>"


def _tla_set(values: tuple[tuple[str, ...], ...]) -> str:
    if not values:
        return "{}"
    lines = ["{"]
    for index, value in enumerate(values):
        suffix = "," if index + 1 < len(values) else ""
        lines.append(f"    {_tla_tuple(value)}{suffix}")
    lines.append("}")
    return "\n".join(lines)


def render_implementation_topology() -> str:
    topology = transition_topology()
    class_names = {
        state_class: "".join(part.title() for part in state_class.value.split("_")) + "States"
        for state_class in StateClass
    }
    sections = [
        "--------------------- MODULE ImplementationTopology ---------------------",
        "EXTENDS FiniteSets, TLC",
        "",
        "\\* Generated by pal.minion.v2.formal; do not edit by hand.",
        f"TransitionTopologyDigest == {_tla_string(transition_topology_digest())}",
        "",
        f"ConcreteStates == {_tla_set(topology['states'])}",
        "",
        f"InitialStates == {_tla_set(topology['initial_states'])}",
    ]
    for state_class in StateClass:
        sections.extend(
            [
                "",
                f"{class_names[state_class]} == {_tla_set(topology['classes'][state_class.value])}",
            ]
        )
    for intent in ControlIntent:
        control = dict(topology["control"].get(intent.value) or {})
        prefix = intent.value.title()
        controlled = tuple(
            sorted(
                {
                    item
                    for disposition in ControlDisposition
                    for item in tuple(control.get(disposition.value) or ())
                }
            )
        )
        sections.extend(
            [
                "",
                f"{prefix}ControlledStates == {_tla_set(controlled)}",
            ]
        )
        for disposition in ControlDisposition:
            sections.extend(
                [
                    "",
                    f"{prefix}{disposition.value.title()}States == "
                    f"{_tla_set(tuple(control.get(disposition.value) or ()))}",
                ]
            )
    sections.extend(
        [
            "",
            f"StaticTransitions == {_tla_set(topology['static_transitions'])}",
            "",
            f"DynamicTransitions == {_tla_set(topology['dynamic_transitions'])}",
            "",
            f"ResolvedTransitions == {_tla_set(topology['resolved_transitions'])}",
            "",
            f"RoleOwnership == {_tla_set(topology['role_ownership'])}",
            "",
            "StateClasses == {",
            "    " + ", ".join(class_names.values()),
            "}",
            "",
            "ClassifiedStates == UNION StateClasses",
            "",
            "RecoverableStates ==",
            "    WorkerLivenessStates \\cup HumanWaitStates \\cup",
            "    ChildWaitStates \\cup OutboxWaitStates",
            "",
            "VARIABLE currentState",
            "",
            "Init == currentState \\in InitialStates",
            "Next ==",
            "    \\/ \\E transition \\in ResolvedTransitions :",
            "        /\\ transition[2] # \"<create>\"",
            "        /\\ currentState = <<transition[1], transition[2]>>",
            "        /\\ currentState' = <<transition[1], transition[4]>>",
            "    \\/ /\\ currentState \\in TerminalStates",
            "       /\\ UNCHANGED currentState",
            "Spec == Init /\\ [][Next]_<<currentState>>",
            "",
            "TypeOK == currentState \\in ConcreteStates",
            "",
            "ClassificationComplete == ClassifiedStates = ConcreteStates",
            "",
            "ClassificationDisjoint ==",
            "    \\A left, right \\in StateClasses : left # right => left \\cap right = {}",
            "",
            "InitialStatesWellFormed == InitialStates \\subseteq ConcreteStates",
            "",
            "StaticTransitionsWellFormed ==",
            "    \\A transition \\in StaticTransitions :",
            "        /\\ (transition[2] = \"<create>\" \\/ <<transition[1], transition[2]>> \\in ConcreteStates)",
            "        /\\ <<transition[1], transition[4]>> \\in ConcreteStates",
            "",
            "DynamicTransitionsWellFormed ==",
            "    \\A transition \\in DynamicTransitions :",
            "        transition[2] # \"<create>\" /\\ <<transition[1], transition[2]>> \\in ConcreteStates",
            "",
            "ResolvedTransitionsWellFormed ==",
            "    \\A transition \\in ResolvedTransitions :",
            "        /\\ (transition[2] = \"<create>\" \\/ <<transition[1], transition[2]>> \\in ConcreteStates)",
            "        /\\ <<transition[1], transition[4]>> \\in ConcreteStates",
            "",
            "DynamicTransitionsResolved ==",
            "    \\A dynamic \\in DynamicTransitions :",
            "        \\E transition \\in ResolvedTransitions :",
            "            /\\ transition[1] = dynamic[1]",
            "            /\\ transition[2] = dynamic[2]",
            "            /\\ transition[3] = dynamic[3]",
            "",
            "RecoverableStatesHaveTriage ==",
            "    \\A state \\in RecoverableStates :",
            "        \\E transition \\in ResolvedTransitions :",
            "            /\\ transition[1] = state[1]",
            "            /\\ transition[2] = state[2]",
            "            /\\ transition[3] = \"ENTER_TRIAGE\"",
            "",
            "TriageHasResolution ==",
            "    \\A state \\in OperatorWaitStates :",
            "        \\E transition \\in ResolvedTransitions :",
            "            /\\ transition[1] = state[1]",
            "            /\\ transition[2] = state[2]",
            "            /\\ transition[3] = \"RESOLVE_TRIAGE\"",
            "",
            "TriageCanRecordLaterFailure ==",
            "    \\A state \\in OperatorWaitStates :",
            "        \\E transition \\in ResolvedTransitions :",
            "            /\\ transition[1] = state[1]",
            "            /\\ transition[2] = state[2]",
            "            /\\ transition[3] = \"ENTER_TRIAGE\"",
            "            /\\ transition[4] = state[2]",
            "",
            "PausedStatesHaveResume ==",
            "    \\A state \\in PausedStates :",
            "        \\E transition \\in ResolvedTransitions :",
            "            /\\ transition[1] = state[1]",
            "            /\\ transition[2] = state[2]",
            "            /\\ transition[3] = \"RESUME\"",
            "",
            "PauseControlPartition ==",
            "    /\\ PauseControlledStates =",
            "        PauseRequestStates \\cup PauseWaitStates \\cup PauseSettledStates",
            "    /\\ PauseRequestStates \\cap PauseWaitStates = {}",
            "    /\\ PauseRequestStates \\cap PauseSettledStates = {}",
            "    /\\ PauseWaitStates \\cap PauseSettledStates = {}",
            "",
            "CancelControlPartition ==",
            "    /\\ CancelControlledStates =",
            "        CancelRequestStates \\cup CancelWaitStates \\cup CancelSettledStates",
            "    /\\ CancelRequestStates \\cap CancelWaitStates = {}",
            "    /\\ CancelRequestStates \\cap CancelSettledStates = {}",
            "    /\\ CancelWaitStates \\cap CancelSettledStates = {}",
            "",
            "PauseRequestStatesHaveTransition ==",
            "    \\A state \\in PauseRequestStates :",
            "        \\E transition \\in ResolvedTransitions :",
            "            /\\ transition[1] = state[1]",
            "            /\\ transition[2] = state[2]",
            "            /\\ transition[3] = \"REQUEST_PAUSE\"",
            "",
            "CancelRequestStatesHaveTransition ==",
            "    \\A state \\in CancelRequestStates :",
            "        \\E transition \\in ResolvedTransitions :",
            "            /\\ transition[1] = state[1]",
            "            /\\ transition[2] = state[2]",
            "            /\\ transition[3] = \"REQUEST_CANCEL\"",
            "",
            "=============================================================================",
            "",
        ]
    )
    return "\n".join(sections)


def write_implementation_topology(path: Path) -> None:
    path.write_text(render_implementation_topology(), encoding="utf-8")
