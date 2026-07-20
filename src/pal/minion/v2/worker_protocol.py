from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping


class WorkerSessionState(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class WorkerSessionAction(StrEnum):
    ACTIVATE = "activate"
    SUSPEND = "suspend"
    COMPLETE = "complete"
    CANCEL = "cancel"


class WorkerAssignmentState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    RETRY_QUEUED = "retry_queued"
    RESULT_RECORDED = "result_recorded"
    SETTLED = "settled"
    CANCELLED = "cancelled"


class WorkerAssignmentAction(StrEnum):
    CLAIM = "claim"
    START = "start"
    QUEUE_RETRY = "queue_retry"
    RECORD_RESULT = "record_result"
    SETTLE = "settle"
    CANCEL = "cancel"


class WorkerAttemptState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    FAILED = "failed"
    LOST = "lost"
    CANCELLED = "cancelled"


ACTIVE_ASSIGNMENT_STATES = frozenset(
    {
        WorkerAssignmentState.CLAIMED,
        WorkerAssignmentState.RUNNING,
        WorkerAssignmentState.RESULT_RECORDED,
    }
)


_SESSION_TRANSITIONS = {
    (WorkerSessionState.ACTIVE, WorkerSessionAction.ACTIVATE): WorkerSessionState.ACTIVE,
    (WorkerSessionState.ACTIVE, WorkerSessionAction.SUSPEND): WorkerSessionState.SUSPENDED,
    (WorkerSessionState.SUSPENDED, WorkerSessionAction.ACTIVATE): WorkerSessionState.ACTIVE,
    (WorkerSessionState.SUSPENDED, WorkerSessionAction.SUSPEND): WorkerSessionState.SUSPENDED,
    (WorkerSessionState.ACTIVE, WorkerSessionAction.COMPLETE): WorkerSessionState.COMPLETED,
    (WorkerSessionState.SUSPENDED, WorkerSessionAction.COMPLETE): WorkerSessionState.COMPLETED,
    (WorkerSessionState.COMPLETED, WorkerSessionAction.COMPLETE): WorkerSessionState.COMPLETED,
    (WorkerSessionState.ACTIVE, WorkerSessionAction.CANCEL): WorkerSessionState.CANCELLED,
    (WorkerSessionState.SUSPENDED, WorkerSessionAction.CANCEL): WorkerSessionState.CANCELLED,
    (WorkerSessionState.CANCELLED, WorkerSessionAction.CANCEL): WorkerSessionState.CANCELLED,
}


def worker_session_target(
    state: WorkerSessionState | str,
    action: WorkerSessionAction | str,
) -> WorkerSessionState:
    """Resolve the only legal durable logical-session transition."""

    source = WorkerSessionState(str(state))
    operation = WorkerSessionAction(str(action))
    try:
        return _SESSION_TRANSITIONS[(source, operation)]
    except KeyError as exc:
        raise ValueError(
            f"illegal worker session transition: {source.value} + {operation.value}"
        ) from exc


_ASSIGNMENT_TRANSITIONS = {
    (WorkerAssignmentState.QUEUED, WorkerAssignmentAction.CLAIM): WorkerAssignmentState.CLAIMED,
    (WorkerAssignmentState.RETRY_QUEUED, WorkerAssignmentAction.CLAIM): WorkerAssignmentState.CLAIMED,
    (WorkerAssignmentState.CLAIMED, WorkerAssignmentAction.START): WorkerAssignmentState.RUNNING,
    (WorkerAssignmentState.CLAIMED, WorkerAssignmentAction.QUEUE_RETRY): WorkerAssignmentState.RETRY_QUEUED,
    (WorkerAssignmentState.RUNNING, WorkerAssignmentAction.QUEUE_RETRY): WorkerAssignmentState.RETRY_QUEUED,
    (WorkerAssignmentState.CLAIMED, WorkerAssignmentAction.RECORD_RESULT): WorkerAssignmentState.RESULT_RECORDED,
    (WorkerAssignmentState.RUNNING, WorkerAssignmentAction.RECORD_RESULT): WorkerAssignmentState.RESULT_RECORDED,
    (WorkerAssignmentState.RETRY_QUEUED, WorkerAssignmentAction.RECORD_RESULT): WorkerAssignmentState.RESULT_RECORDED,
    (WorkerAssignmentState.RESULT_RECORDED, WorkerAssignmentAction.RECORD_RESULT): WorkerAssignmentState.RESULT_RECORDED,
    (WorkerAssignmentState.RESULT_RECORDED, WorkerAssignmentAction.SETTLE): WorkerAssignmentState.SETTLED,
    (WorkerAssignmentState.SETTLED, WorkerAssignmentAction.SETTLE): WorkerAssignmentState.SETTLED,
}

for _state in WorkerAssignmentState:
    if _state not in {
        WorkerAssignmentState.RESULT_RECORDED,
        WorkerAssignmentState.SETTLED,
        WorkerAssignmentState.CANCELLED,
    }:
        _ASSIGNMENT_TRANSITIONS[(_state, WorkerAssignmentAction.CANCEL)] = (
            WorkerAssignmentState.CANCELLED
        )
_ASSIGNMENT_TRANSITIONS[
    (WorkerAssignmentState.CANCELLED, WorkerAssignmentAction.CANCEL)
] = WorkerAssignmentState.CANCELLED


def worker_assignment_target(
    state: WorkerAssignmentState | str,
    action: WorkerAssignmentAction | str,
) -> WorkerAssignmentState:
    """Resolve the only legal durable assignment transition.

    Worker assignments model one role activation and its Manager receipt. They
    never own module triage, pause, repair, or acceptance; those remain actions
    on the parent aggregate.
    """

    source = WorkerAssignmentState(str(state))
    operation = WorkerAssignmentAction(str(action))
    try:
        return _ASSIGNMENT_TRANSITIONS[(source, operation)]
    except KeyError as exc:
        raise ValueError(
            f"illegal worker assignment transition: {source.value} + {operation.value}"
        ) from exc


def worker_session_role(activation_role: str) -> str:
    role = str(activation_role or "").strip()
    if role in {"producer", "repair"}:
        return "coder"
    if role in {"verifier", "scenario_verifier"}:
        return "verifier"
    return role


@dataclass(frozen=True)
class WorkerAssignmentRequest:
    assignment_key: str
    session_id: str
    workflow_id: str
    aggregate_type: str
    aggregate_id: str
    role: str
    input_fingerprint: str
    required_inputs: tuple[str, ...]
    input_refs: Mapping[str, Mapping[str, Any]]
    execution_spec: Mapping[str, Any]
    submission_kind: str

    def __post_init__(self) -> None:
        required = {
            "assignment_key": self.assignment_key,
            "session_id": self.session_id,
            "workflow_id": self.workflow_id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "role": self.role,
            "input_fingerprint": self.input_fingerprint,
            "submission_kind": self.submission_kind,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(
                "worker assignment request missing fields: " + ", ".join(missing)
            )
        names = tuple(str(item).strip() for item in self.required_inputs)
        if any(not item for item in names) or len(set(names)) != len(names):
            raise ValueError("worker assignment required inputs must be unique non-empty names")
        unknown = sorted(set(names) - {str(item) for item in self.input_refs})
        if unknown:
            raise ValueError(
                "worker assignment required inputs have no bound artifact: "
                + ", ".join(unknown)
            )
        if not str(dict(self.execution_spec or {}).get("effect_type") or "").strip():
            raise ValueError("worker assignment execution spec requires effect_type")

    @property
    def assignment_id(self) -> str:
        return semantic_id("asg", self.assignment_key)

    @property
    def request_hash(self) -> str:
        return stable_hash(self.to_payload())

    def to_payload(self) -> dict[str, Any]:
        return {
            "assignment_key": self.assignment_key,
            "session_id": self.session_id,
            "workflow_id": self.workflow_id,
            "aggregate_type": self.aggregate_type,
            "aggregate_id": self.aggregate_id,
            "role": self.role,
            "input_fingerprint": self.input_fingerprint,
            "required_inputs": sorted(str(item) for item in self.required_inputs),
            "input_refs": {
                str(name): dict(ref)
                for name, ref in sorted(self.input_refs.items())
            },
            "execution_spec": dict(self.execution_spec),
            "submission_kind": self.submission_kind,
        }


@dataclass(frozen=True)
class WorkerSubmissionReceipt:
    assignment_id: str
    artifact_ref: Mapping[str, Any]
    payload_hash: str
    settlement_action: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "artifact_ref": dict(self.artifact_ref),
            "payload_hash": self.payload_hash,
            "settlement_action": dict(self.settlement_action),
        }


def attempt_id(assignment_id: str, attempt_index: int) -> str:
    if int(attempt_index) <= 0:
        raise ValueError("worker attempt index must be positive")
    return semantic_id("att", f"{assignment_id}:{int(attempt_index)}")


def semantic_id(prefix: str, value: str) -> str:
    digest = hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
