from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from enum import StrEnum
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
from pal.minion.v2.machines import all_transition_specs


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


STATE_ENUMS = {
    AggregateType.TASK: TaskState,
    AggregateType.WORKFLOW: WorkflowState,
    AggregateType.ARCHITECTURE_REVISION: ArchitectureRevisionState,
    AggregateType.EXECUTION_EPOCH: ExecutionEpochState,
    AggregateType.DAG_NODE_RUN: DagNodeRunState,
    AggregateType.STANDALONE_REVIEW: StandaloneReviewState,
}


STATE_CLASSIFICATIONS: Mapping[AggregateType, Mapping[str, StateClass]] = {
    AggregateType.TASK: {
        TaskState.ACTIVE.value: StateClass.CATALOG,
        TaskState.ARCHIVED.value: StateClass.TERMINAL,
    },
    AggregateType.WORKFLOW: {
        WorkflowState.CREATED.value: StateClass.OUTBOX_WAIT,
        WorkflowState.ACTIVE.value: StateClass.CHILD_WAIT,
        WorkflowState.PAUSE_REQUESTED.value: StateClass.CHILD_WAIT,
        WorkflowState.PAUSED.value: StateClass.PAUSED,
        WorkflowState.CANCEL_REQUESTED.value: StateClass.CHILD_WAIT,
        WorkflowState.COMPLETED.value: StateClass.TERMINAL,
        WorkflowState.REJECTED.value: StateClass.TERMINAL,
        WorkflowState.CANCELLED.value: StateClass.TERMINAL,
        WorkflowState.TRIAGE_REQUIRED.value: StateClass.OPERATOR_WAIT,
    },
    AggregateType.ARCHITECTURE_REVISION: {
        ArchitectureRevisionState.ARCHITECT_QUEUED.value: StateClass.WORKER_LIVENESS,
        ArchitectureRevisionState.ARCHITECT_RUNNING.value: StateClass.WORKER_LIVENESS,
        ArchitectureRevisionState.ARCHITECT_QUIESCING.value: StateClass.WORKER_LIVENESS,
        ArchitectureRevisionState.ARCHITECT_SNAPSHOTTING.value: StateClass.WORKER_LIVENESS,
        ArchitectureRevisionState.REVIEW_QUEUED.value: StateClass.WORKER_LIVENESS,
        ArchitectureRevisionState.REVIEWING.value: StateClass.WORKER_LIVENESS,
        ArchitectureRevisionState.HUMAN_REVIEW.value: StateClass.HUMAN_WAIT,
        ArchitectureRevisionState.CLARIFICATION_PENDING.value: StateClass.HUMAN_WAIT,
        ArchitectureRevisionState.SUPERSEDED.value: StateClass.TERMINAL,
        ArchitectureRevisionState.ACCEPTED.value: StateClass.TERMINAL,
        ArchitectureRevisionState.REJECTED.value: StateClass.TERMINAL,
        ArchitectureRevisionState.PAUSE_REQUESTED.value: StateClass.WORKER_LIVENESS,
        ArchitectureRevisionState.PAUSED.value: StateClass.PAUSED,
        ArchitectureRevisionState.CANCEL_REQUESTED.value: StateClass.WORKER_LIVENESS,
        ArchitectureRevisionState.CANCELLED.value: StateClass.TERMINAL,
        ArchitectureRevisionState.TRIAGE_REQUIRED.value: StateClass.OPERATOR_WAIT,
    },
    AggregateType.EXECUTION_EPOCH: {
        ExecutionEpochState.NOT_STARTED.value: StateClass.OUTBOX_WAIT,
        ExecutionEpochState.STARTING.value: StateClass.OUTBOX_WAIT,
        ExecutionEpochState.RUNNING.value: StateClass.CHILD_WAIT,
        ExecutionEpochState.REPLAN_COLLECTING.value: StateClass.CHILD_WAIT,
        ExecutionEpochState.PAUSE_REQUESTED.value: StateClass.CHILD_WAIT,
        ExecutionEpochState.PAUSED.value: StateClass.PAUSED,
        ExecutionEpochState.REPLAN_REQUIRED.value: StateClass.CHILD_WAIT,
        ExecutionEpochState.SUPERSEDED.value: StateClass.TERMINAL,
        ExecutionEpochState.FINALIZING.value: StateClass.OUTBOX_WAIT,
        ExecutionEpochState.COMPLETED.value: StateClass.TERMINAL,
        ExecutionEpochState.CANCEL_REQUESTED.value: StateClass.CHILD_WAIT,
        ExecutionEpochState.CANCELLED.value: StateClass.TERMINAL,
        ExecutionEpochState.TRIAGE_REQUIRED.value: StateClass.OPERATOR_WAIT,
    },
    AggregateType.DAG_NODE_RUN: {
        DagNodeRunState.BLOCKED_BY_DEPS.value: StateClass.DEPENDENCY_WAIT,
        DagNodeRunState.QUEUED.value: StateClass.WORKER_LIVENESS,
        DagNodeRunState.PRODUCING.value: StateClass.WORKER_LIVENESS,
        DagNodeRunState.QUIESCING.value: StateClass.WORKER_LIVENESS,
        DagNodeRunState.SNAPSHOTTING.value: StateClass.WORKER_LIVENESS,
        DagNodeRunState.REVIEW_QUEUED.value: StateClass.WORKER_LIVENESS,
        DagNodeRunState.REVIEWING.value: StateClass.WORKER_LIVENESS,
        DagNodeRunState.REPAIR_QUEUED.value: StateClass.WORKER_LIVENESS,
        DagNodeRunState.REPAIRING.value: StateClass.WORKER_LIVENESS,
        DagNodeRunState.VERIFY_PREPARING.value: StateClass.WORKER_LIVENESS,
        DagNodeRunState.VERIFYING.value: StateClass.WORKER_LIVENESS,
        DagNodeRunState.ACCEPTED.value: StateClass.TERMINAL,
        DagNodeRunState.STALE.value: StateClass.DEPENDENCY_WAIT,
        DagNodeRunState.PAUSE_REQUESTED.value: StateClass.WORKER_LIVENESS,
        DagNodeRunState.PAUSED.value: StateClass.PAUSED,
        DagNodeRunState.CANCEL_REQUESTED.value: StateClass.WORKER_LIVENESS,
        DagNodeRunState.CANCELLED.value: StateClass.TERMINAL,
        DagNodeRunState.TRIAGE_REQUIRED.value: StateClass.OPERATOR_WAIT,
    },
    AggregateType.STANDALONE_REVIEW: {
        StandaloneReviewState.RECEIVED.value: StateClass.WORKER_LIVENESS,
        StandaloneReviewState.REVIEW_QUEUED.value: StateClass.WORKER_LIVENESS,
        StandaloneReviewState.REVIEWING.value: StateClass.WORKER_LIVENESS,
        StandaloneReviewState.REPORT_READY.value: StateClass.WORKER_LIVENESS,
        StandaloneReviewState.PAUSE_REQUESTED.value: StateClass.WORKER_LIVENESS,
        StandaloneReviewState.PAUSED.value: StateClass.PAUSED,
        StandaloneReviewState.CANCEL_REQUESTED.value: StateClass.WORKER_LIVENESS,
        StandaloneReviewState.CANCELLED.value: StateClass.TERMINAL,
        StandaloneReviewState.COMPLETED.value: StateClass.TERMINAL,
        StandaloneReviewState.TRIAGE_REQUIRED.value: StateClass.OPERATOR_WAIT,
    },
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
    for spec in all_transition_specs():
        aggregate = spec.aggregate_type.value
        source = str(spec.source_state) if spec.source_state is not None else "<create>"
        if callable(spec.target_state):
            dynamic.append((aggregate, source, spec.action_type))
        else:
            static.append((aggregate, source, spec.action_type, str(spec.target_state)))
    return {
        "states": state_pairs(),
        "classes": {
            state_class.value: state_pairs(state_class)
            for state_class in StateClass
        },
        "static_transitions": tuple(sorted(static)),
        "dynamic_transitions": tuple(sorted(dynamic)),
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
    ]
    for state_class in StateClass:
        sections.extend(
            [
                "",
                f"{class_names[state_class]} == {_tla_set(topology['classes'][state_class.value])}",
            ]
        )
    sections.extend(
        [
            "",
            f"StaticTransitions == {_tla_set(topology['static_transitions'])}",
            "",
            f"DynamicTransitions == {_tla_set(topology['dynamic_transitions'])}",
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
            "VARIABLE checked",
            "",
            "Init == checked = TRUE",
            "Next == UNCHANGED checked",
            "Spec == Init /\\ [][Next]_<<checked>>",
            "",
            "TypeOK == checked \\in BOOLEAN",
            "",
            "ClassificationComplete == ClassifiedStates = ConcreteStates",
            "",
            "ClassificationDisjoint ==",
            "    \\A left, right \\in StateClasses : left # right => left \\cap right = {}",
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
            "RecoverableStatesHaveTriage ==",
            "    \\A state \\in RecoverableStates :",
            "        \\E transition \\in StaticTransitions :",
            "            /\\ transition[1] = state[1]",
            "            /\\ transition[2] = state[2]",
            "            /\\ transition[3] = \"ENTER_TRIAGE\"",
            "",
            "TriageHasResolution ==",
            "    \\A state \\in OperatorWaitStates :",
            "        \\E transition \\in DynamicTransitions \\cup",
            "                {<<item[1], item[2], item[3]>> : item \\in StaticTransitions} :",
            "            /\\ transition[1] = state[1]",
            "            /\\ transition[2] = state[2]",
            "            /\\ transition[3] = \"RESOLVE_TRIAGE\"",
            "",
            "TriageCanRecordLaterFailure ==",
            "    \\A state \\in OperatorWaitStates :",
            "        \\E transition \\in StaticTransitions :",
            "            /\\ transition[1] = state[1]",
            "            /\\ transition[2] = state[2]",
            "            /\\ transition[3] = \"ENTER_TRIAGE\"",
            "            /\\ transition[4] = state[2]",
            "",
            "PausedStatesHaveResume ==",
            "    \\A state \\in PausedStates :",
            "        \\E transition \\in DynamicTransitions \\cup",
            "                {<<item[1], item[2], item[3]>> : item \\in StaticTransitions} :",
            "            /\\ transition[1] = state[1]",
            "            /\\ transition[2] = state[2]",
            "            /\\ transition[3] = \"RESUME\"",
            "",
            "=============================================================================",
            "",
        ]
    )
    return "\n".join(sections)


def write_implementation_topology(path: Path) -> None:
    path.write_text(render_implementation_topology(), encoding="utf-8")
