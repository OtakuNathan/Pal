from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Mapping

from pal.minion.v2.role_contracts import RoleActivation


class RoleSessionState(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class RoleSessionAction(StrEnum):
    ACTIVATE = "activate"
    SUSPEND = "suspend"
    COMPLETE = "complete"
    CANCEL = "cancel"


class RoleAssignmentState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    RETRY_QUEUED = "retry_queued"
    RESULT_RECORDED = "result_recorded"
    SETTLED = "settled"
    CANCELLED = "cancelled"


class RoleAssignmentAction(StrEnum):
    CLAIM = "claim"
    START = "start"
    QUEUE_RETRY = "queue_retry"
    RECORD_RESULT = "record_result"
    SETTLE = "settle"
    CANCEL = "cancel"


class RoleAttemptState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    FAILED = "failed"
    LOST = "lost"
    CANCELLED = "cancelled"


ACTIVE_ASSIGNMENT_STATES = frozenset(
    {
        RoleAssignmentState.CLAIMED,
        RoleAssignmentState.RUNNING,
        RoleAssignmentState.RESULT_RECORDED,
    }
)


_SESSION_TRANSITIONS = {
    (RoleSessionState.ACTIVE, RoleSessionAction.ACTIVATE): RoleSessionState.ACTIVE,
    (RoleSessionState.ACTIVE, RoleSessionAction.SUSPEND): RoleSessionState.SUSPENDED,
    (RoleSessionState.SUSPENDED, RoleSessionAction.ACTIVATE): RoleSessionState.ACTIVE,
    (RoleSessionState.SUSPENDED, RoleSessionAction.SUSPEND): RoleSessionState.SUSPENDED,
    (RoleSessionState.ACTIVE, RoleSessionAction.COMPLETE): RoleSessionState.COMPLETED,
    (RoleSessionState.SUSPENDED, RoleSessionAction.COMPLETE): RoleSessionState.COMPLETED,
    (RoleSessionState.COMPLETED, RoleSessionAction.COMPLETE): RoleSessionState.COMPLETED,
    (RoleSessionState.ACTIVE, RoleSessionAction.CANCEL): RoleSessionState.CANCELLED,
    (RoleSessionState.SUSPENDED, RoleSessionAction.CANCEL): RoleSessionState.CANCELLED,
    (RoleSessionState.CANCELLED, RoleSessionAction.CANCEL): RoleSessionState.CANCELLED,
}


def role_session_target(
    state: RoleSessionState | str,
    action: RoleSessionAction | str,
) -> RoleSessionState:
    """Resolve the only legal durable logical-session transition."""

    source = RoleSessionState(str(state))
    operation = RoleSessionAction(str(action))
    try:
        return _SESSION_TRANSITIONS[(source, operation)]
    except KeyError as exc:
        raise ValueError(
            f"illegal role session transition: {source.value} + {operation.value}"
        ) from exc


_ASSIGNMENT_TRANSITIONS = {
    (RoleAssignmentState.QUEUED, RoleAssignmentAction.CLAIM): RoleAssignmentState.CLAIMED,
    (RoleAssignmentState.RETRY_QUEUED, RoleAssignmentAction.CLAIM): RoleAssignmentState.CLAIMED,
    (RoleAssignmentState.CLAIMED, RoleAssignmentAction.START): RoleAssignmentState.RUNNING,
    (RoleAssignmentState.CLAIMED, RoleAssignmentAction.QUEUE_RETRY): RoleAssignmentState.RETRY_QUEUED,
    (RoleAssignmentState.RUNNING, RoleAssignmentAction.QUEUE_RETRY): RoleAssignmentState.RETRY_QUEUED,
    (RoleAssignmentState.CLAIMED, RoleAssignmentAction.RECORD_RESULT): RoleAssignmentState.RESULT_RECORDED,
    (RoleAssignmentState.RUNNING, RoleAssignmentAction.RECORD_RESULT): RoleAssignmentState.RESULT_RECORDED,
    (RoleAssignmentState.RETRY_QUEUED, RoleAssignmentAction.RECORD_RESULT): RoleAssignmentState.RESULT_RECORDED,
    (RoleAssignmentState.RESULT_RECORDED, RoleAssignmentAction.RECORD_RESULT): RoleAssignmentState.RESULT_RECORDED,
    (RoleAssignmentState.RESULT_RECORDED, RoleAssignmentAction.SETTLE): RoleAssignmentState.SETTLED,
    (RoleAssignmentState.SETTLED, RoleAssignmentAction.SETTLE): RoleAssignmentState.SETTLED,
}

for _state in RoleAssignmentState:
    if _state not in {
        RoleAssignmentState.RESULT_RECORDED,
        RoleAssignmentState.SETTLED,
        RoleAssignmentState.CANCELLED,
    }:
        _ASSIGNMENT_TRANSITIONS[(_state, RoleAssignmentAction.CANCEL)] = (
            RoleAssignmentState.CANCELLED
        )
_ASSIGNMENT_TRANSITIONS[
    (RoleAssignmentState.CANCELLED, RoleAssignmentAction.CANCEL)
] = RoleAssignmentState.CANCELLED


def role_assignment_target(
    state: RoleAssignmentState | str,
    action: RoleAssignmentAction | str,
) -> RoleAssignmentState:
    """Resolve the only legal durable assignment transition.

    Role assignments model one role activation and its Manager receipt. They
    never own module triage, pause, repair, or acceptance; those remain actions
    on the parent aggregate.
    """

    source = RoleAssignmentState(str(state))
    operation = RoleAssignmentAction(str(action))
    try:
        return _ASSIGNMENT_TRANSITIONS[(source, operation)]
    except KeyError as exc:
        raise ValueError(
            f"illegal role assignment transition: {source.value} + {operation.value}"
        ) from exc


@dataclass(frozen=True)
class RoleAssignmentRequest:
    assignment_key: str
    session_id: str
    workflow_id: str
    aggregate_type: str
    aggregate_id: str
    role: str
    mode: str
    role_profile_id: str
    family_binding_sha: str
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
            "mode": self.mode,
            "role_profile_id": self.role_profile_id,
            "family_binding_sha": self.family_binding_sha,
            "input_fingerprint": self.input_fingerprint,
            "submission_kind": self.submission_kind,
        }
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(
                "role assignment request missing fields: " + ", ".join(missing)
            )
        RoleActivation.from_values(self.role, self.mode)
        if "." not in self.role_profile_id:
            raise ValueError("role assignment role_profile_id must be canonical")
        names = tuple(str(item).strip() for item in self.required_inputs)
        if any(not item for item in names) or len(set(names)) != len(names):
            raise ValueError("role assignment required inputs must be unique non-empty names")
        unknown = sorted(set(names) - {str(item) for item in self.input_refs})
        if unknown:
            raise ValueError(
                "role assignment required inputs have no bound artifact: "
                + ", ".join(unknown)
            )
        if not str(dict(self.execution_spec or {}).get("effect_type") or "").strip():
            raise ValueError("role assignment execution spec requires effect_type")

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
            "mode": self.mode,
            "role_profile_id": self.role_profile_id,
            "family_binding_sha": self.family_binding_sha,
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
class RoleSubmissionReceipt:
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
        raise ValueError("role attempt index must be positive")
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
