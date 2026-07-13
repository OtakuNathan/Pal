from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Mapping
from uuid import uuid4

from pal.foundation import utc_now


class AggregateType(StrEnum):
    TASK = "task"
    WORKFLOW = "workflow"
    ARCHITECTURE_REVISION = "architecture_revision"
    EXECUTION_EPOCH = "execution_epoch"
    DAG_NODE_RUN = "dag_node_run"
    STANDALONE_REVIEW = "standalone_review"


class TaskState(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class WorkflowState(StrEnum):
    CREATED = "CREATED"
    ACTIVE = "ACTIVE"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    COMPLETED = "COMPLETED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    TRIAGE_REQUIRED = "TRIAGE_REQUIRED"


class ArchitectureRevisionState(StrEnum):
    ARCHITECT_QUEUED = "ARCHITECT_QUEUED"
    ARCHITECT_RUNNING = "ARCHITECT_RUNNING"
    ARCHITECT_QUIESCING = "ARCHITECT_QUIESCING"
    ARCHITECT_SNAPSHOTTING = "ARCHITECT_SNAPSHOTTING"
    REVIEW_QUEUED = "REVIEW_QUEUED"
    REVIEWING = "REVIEWING"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    CLARIFICATION_PENDING = "CLARIFICATION_PENDING"
    REVISION_PENDING = "REVISION_PENDING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    TRIAGE_REQUIRED = "TRIAGE_REQUIRED"


class ExecutionEpochState(StrEnum):
    NOT_STARTED = "NOT_STARTED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    REPLAN_REQUIRED = "REPLAN_REQUIRED"
    FINALIZING = "FINALIZING"
    COMPLETED = "COMPLETED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    TRIAGE_REQUIRED = "TRIAGE_REQUIRED"


class DagNodeRunState(StrEnum):
    BLOCKED_BY_DEPS = "BLOCKED_BY_DEPS"
    QUEUED = "QUEUED"
    PRODUCING = "PRODUCING"
    QUIESCING = "QUIESCING"
    SNAPSHOTTING = "SNAPSHOTTING"
    REVIEW_QUEUED = "REVIEW_QUEUED"
    REVIEWING = "REVIEWING"
    REPAIR_QUEUED = "REPAIR_QUEUED"
    REPAIRING = "REPAIRING"
    VERIFY_PREPARING = "VERIFY_PREPARING"
    VERIFYING = "VERIFYING"
    ACCEPTED = "ACCEPTED"
    STALE = "STALE"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    TRIAGE_REQUIRED = "TRIAGE_REQUIRED"


class StandaloneReviewState(StrEnum):
    RECEIVED = "RECEIVED"
    REVIEW_QUEUED = "REVIEW_QUEUED"
    REVIEWING = "REVIEWING"
    REPORT_READY = "REPORT_READY"
    PAUSE_REQUESTED = "PAUSE_REQUESTED"
    PAUSED = "PAUSED"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"
    TRIAGE_REQUIRED = "TRIAGE_REQUIRED"


@dataclass(frozen=True)
class ActionEnvelope:
    action_type: str
    workflow_id: str
    aggregate_type: AggregateType
    aggregate_id: str
    actor: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    source_channel: str = ""
    expected_version: int | None = None
    idempotency_key: str = ""
    correlation_id: str = ""
    causation_id: str = ""
    action_id: str = field(default_factory=lambda: f"act_{uuid4().hex}")
    created_at: str = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        required = {
            "action_type": self.action_type,
            "aggregate_id": self.aggregate_id,
            "actor": self.actor,
        }
        if self.aggregate_type != AggregateType.TASK:
            required["workflow_id"] = self.workflow_id
        missing = [name for name, value in required.items() if not str(value or "").strip()]
        if missing:
            raise ValueError(f"action envelope missing required fields: {', '.join(missing)}")
        if self.expected_version is not None and self.expected_version < 0:
            raise ValueError("expected_version must be non-negative")

    @property
    def dedup_key(self) -> str:
        return str(self.idempotency_key or self.action_id)


@dataclass(frozen=True)
class AggregateSnapshot:
    aggregate_type: AggregateType
    aggregate_id: str
    workflow_id: str
    state: str
    version: int
    payload: Mapping[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DomainEventDraft:
    event_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DomainEvent:
    event_id: str
    workflow_id: str
    aggregate_type: AggregateType
    aggregate_id: str
    aggregate_version: int
    event_type: str
    payload: Mapping[str, Any]
    action_id: str
    correlation_id: str
    causation_id: str
    created_at: str


@dataclass(frozen=True)
class EffectDraft:
    effect_type: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    max_attempts: int = 8


Guard = Callable[[Mapping[str, Any], ActionEnvelope], None]
Reducer = Callable[[Mapping[str, Any], ActionEnvelope], Mapping[str, Any]]
TargetResolver = Callable[[Mapping[str, Any], ActionEnvelope], str]
EventBuilder = Callable[[Mapping[str, Any], ActionEnvelope, str], tuple[DomainEventDraft, ...]]
EffectBuilder = Callable[[Mapping[str, Any], ActionEnvelope, str], tuple[EffectDraft, ...]]


@dataclass(frozen=True)
class TransitionSpec:
    aggregate_type: AggregateType
    source_state: str | None
    action_type: str
    target_state: str | TargetResolver
    reducer: Reducer
    guard: Guard
    event_builder: EventBuilder
    effect_builder: EffectBuilder


@dataclass(frozen=True)
class TransitionOutcome:
    snapshot: AggregateSnapshot
    events: tuple[DomainEventDraft, ...]
    effects: tuple[EffectDraft, ...]


class TransitionError(ValueError):
    pass


class UnknownTransitionError(TransitionError):
    pass


class TransitionGuardError(TransitionError):
    pass


class AggregateVersionConflict(TransitionError):
    pass


class AggregateNotFound(TransitionError):
    pass


@dataclass(frozen=True)
class DispatchResult:
    snapshot: AggregateSnapshot
    events: tuple[DomainEvent, ...]
    outbox_effect_ids: tuple[str, ...]
    duplicate: bool = False


@dataclass(frozen=True)
class LeaseGrant:
    resource_key: str
    owner_id: str
    fencing_token: int
    acquired_at: str
    expires_at: str


class LeaseConflict(RuntimeError):
    pass


class StaleFencingToken(RuntimeError):
    pass
