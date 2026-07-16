from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from pal.minion.v2.contracts import (
    ActionEnvelope,
    AggregateType,
    ArchitectureRevisionState,
    DagNodeRunState,
    DomainEventDraft,
    EffectDraft,
    ExecutionEpochState,
    StandaloneReviewState,
    TaskState,
    TransitionGuardError,
    TransitionSpec,
    WorkflowState,
)
from pal.minion.v2.engine import TransitionEngine


# These states are not allowed to sit durably without an outbox effect, a
# worker assignment, or a live fenced lease.  Recovery uses the same table as
# the transition layer so worker liveness does not become a second, hidden
# state machine.
LIVENESS_REQUIRED_STATES: Mapping[AggregateType, frozenset[str]] = {
    AggregateType.ARCHITECTURE_REVISION: frozenset(
        {
            ArchitectureRevisionState.ARCHITECT_QUEUED.value,
            ArchitectureRevisionState.ARCHITECT_RUNNING.value,
            ArchitectureRevisionState.ARCHITECT_QUIESCING.value,
            ArchitectureRevisionState.ARCHITECT_SNAPSHOTTING.value,
            ArchitectureRevisionState.REVIEW_QUEUED.value,
            ArchitectureRevisionState.REVIEWING.value,
            ArchitectureRevisionState.PAUSE_REQUESTED.value,
        }
    ),
    AggregateType.DAG_NODE_RUN: frozenset(
        {
            DagNodeRunState.QUEUED.value,
            DagNodeRunState.PRODUCING.value,
            DagNodeRunState.QUIESCING.value,
            DagNodeRunState.SNAPSHOTTING.value,
            DagNodeRunState.REVIEW_QUEUED.value,
            DagNodeRunState.REVIEWING.value,
            DagNodeRunState.REPAIR_QUEUED.value,
            DagNodeRunState.REPAIRING.value,
            DagNodeRunState.VERIFY_PREPARING.value,
            DagNodeRunState.VERIFYING.value,
            DagNodeRunState.PAUSE_REQUESTED.value,
        }
    ),
    AggregateType.STANDALONE_REVIEW: frozenset(
        {
            StandaloneReviewState.RECEIVED.value,
            StandaloneReviewState.REVIEW_QUEUED.value,
            StandaloneReviewState.REVIEWING.value,
            StandaloneReviewState.REPORT_READY.value,
            StandaloneReviewState.PAUSE_REQUESTED.value,
        }
    ),
}


def _no_guard(_payload: Mapping[str, Any], _action: ActionEnvelope) -> None:
    return None


def _merge_payload(payload: Mapping[str, Any], action: ActionEnvelope) -> Mapping[str, Any]:
    updated = dict(payload)
    updated.update(dict(action.payload))
    updated["last_action_type"] = action.action_type
    updated["last_actor"] = action.actor
    return updated


def _required(*fields: str):
    def guard(_payload: Mapping[str, Any], action: ActionEnvelope) -> None:
        missing = [field for field in fields if action.payload.get(field) in (None, "", [], {})]
        if missing:
            raise TransitionGuardError(f"{action.action_type} requires: {', '.join(missing)}")

    return guard


def _all(*guards):
    def guard(payload: Mapping[str, Any], action: ActionEnvelope) -> None:
        for item in guards:
            item(payload, action)

    return guard


def _default_events(
    _payload: Mapping[str, Any],
    action: ActionEnvelope,
    target_state: str,
) -> tuple[DomainEventDraft, ...]:
    return (
        DomainEventDraft(
            event_type=f"{action.aggregate_type.value}.{action.action_type.lower()}",
            payload={"target_state": target_state, "action_payload": dict(action.payload)},
        ),
    )


def _effect(effect_type: str, **fixed_payload: Any):
    def builder(
        _payload: Mapping[str, Any],
        action: ActionEnvelope,
        _target_state: str,
    ) -> tuple[EffectDraft, ...]:
        payload = dict(fixed_payload)
        payload.update(
            {
                "workflow_id": action.workflow_id,
                "aggregate_type": action.aggregate_type.value,
                "aggregate_id": action.aggregate_id,
            }
        )
        return (EffectDraft(effect_type=effect_type, payload=payload),)

    return builder


def _effects(*effect_types: str):
    def builder(
        _payload: Mapping[str, Any],
        action: ActionEnvelope,
        _target_state: str,
    ) -> tuple[EffectDraft, ...]:
        shared = {
            "workflow_id": action.workflow_id,
            "aggregate_type": action.aggregate_type.value,
            "aggregate_id": action.aggregate_id,
        }
        return tuple(EffectDraft(effect_type=effect_type, payload=shared) for effect_type in effect_types)

    return builder


def _combined_effects(*builders):
    def builder(
        payload: Mapping[str, Any],
        action: ActionEnvelope,
        target_state: str,
    ) -> tuple[EffectDraft, ...]:
        return tuple(
            effect
            for effect_builder in builders
            for effect in effect_builder(payload, action, target_state)
        )

    return builder


def _no_effects(
    _payload: Mapping[str, Any],
    _action: ActionEnvelope,
    _target_state: str,
) -> tuple[EffectDraft, ...]:
    return ()


def _spec(
    aggregate_type: AggregateType,
    source_state: str | None,
    action_type: str,
    target_state,
    *,
    guard=_no_guard,
    reducer=_merge_payload,
    effects=_no_effects,
) -> TransitionSpec:
    return TransitionSpec(
        aggregate_type=aggregate_type,
        source_state=source_state,
        action_type=action_type,
        target_state=target_state,
        reducer=reducer,
        guard=guard,
        event_builder=_default_events,
        effect_builder=effects,
    )


def _pause_reducer(resume_state: str):
    def reducer(payload: Mapping[str, Any], action: ActionEnvelope) -> Mapping[str, Any]:
        updated = dict(_merge_payload(payload, action))
        updated["resume_state"] = resume_state
        return updated

    return reducer


def _triage_reducer(resume_state: str):
    def reducer(payload: Mapping[str, Any], action: ActionEnvelope) -> Mapping[str, Any]:
        updated = dict(_merge_payload(payload, action))
        updated["triage_resume_state"] = resume_state
        return updated

    return reducer


def _resume_cleanup_reducer(payload: Mapping[str, Any], action: ActionEnvelope) -> Mapping[str, Any]:
    updated = dict(_merge_payload(payload, action))
    for field in ("blocker", "active_worker_id", "fencing_token", "lease_resource_key"):
        updated.pop(field, None)
    return updated


def _worker_finished_reducer(payload: Mapping[str, Any], action: ActionEnvelope) -> Mapping[str, Any]:
    updated = dict(_merge_payload(payload, action))
    for field in ("active_worker_id", "fencing_token", "lease_resource_key"):
        updated.pop(field, None)
    return updated


def _new_node_role_generation_reducer(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> Mapping[str, Any]:
    updated = dict(_worker_finished_reducer(payload, action))
    updated["role_session_generation"] = int(
        payload.get("role_session_generation") or 0
    ) + 1
    return updated


def _replan_finding_reducer(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> Mapping[str, Any]:
    updated = dict(payload)
    finding_ref = dict(action.payload.get("finding_artifact_ref") or {})
    digest = str(finding_ref.get("sha256") or "")
    pending = [dict(item or {}) for item in list(payload.get("pending_replan_findings") or [])]
    entry = {
        "finding_artifact_ref": finding_ref,
        "finding_fingerprint": str(action.payload.get("finding_fingerprint") or ""),
        "source_node": str(action.payload.get("source_node") or ""),
        **(
            {"requirement_patch_ref": dict(action.payload["requirement_patch_ref"])}
            if action.payload.get("requirement_patch_ref")
            else {}
        ),
        **(
            {"revised_requirements_ref": dict(action.payload["revised_requirements_ref"])}
            if action.payload.get("revised_requirements_ref")
            else {}
        ),
    }
    existing_index = next(
        (
            index
            for index, item in enumerate(pending)
            if str(dict(item.get("finding_artifact_ref") or {}).get("sha256") or "") == digest
        ),
        None,
    )
    if existing_index is None:
        pending.append(entry)
    else:
        existing = dict(pending[existing_index])
        existing.update({key: value for key, value in entry.items() if value not in (None, "", [], {})})
        pending[existing_index] = existing
    updated["pending_replan_findings"] = sorted(
        pending,
        key=lambda item: str(dict(item.get("finding_artifact_ref") or {}).get("sha256") or ""),
    )
    if str(action.payload.get("source_node") or ""):
        sources = set(str(item) for item in list(payload.get("replan_source_nodes") or []))
        sources.add(str(action.payload["source_node"]))
        updated["replan_source_nodes"] = sorted(sources)
    updated["last_action_type"] = action.action_type
    updated["last_actor"] = action.actor
    return updated


def _replan_batch_ready_reducer(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> Mapping[str, Any]:
    updated = dict(_merge_payload(payload, action))
    updated["replan_generation"] = int(payload.get("replan_generation") or 0) + 1
    updated.pop("pending_replan_findings", None)
    updated.pop("replan_source_nodes", None)
    return updated


def _replacement_epoch_started_reducer(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> Mapping[str, Any]:
    updated = dict(_merge_payload(payload, action))
    updated["replacement_execution_epoch_id"] = str(action.payload.get("replacement_execution_epoch_id") or "")
    return updated


def _reopen_replan_collection_reducer(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> Mapping[str, Any]:
    updated = dict(payload)
    updated["pending_replan_findings"] = [
        dict(item or {}) for item in list(action.payload.get("finding_entries") or [])
    ]
    for field in (
        "replan_finding_batch_ref",
        "replan_finding_fingerprints",
        "active_replan_revision_id",
        "failure_artifact_ref",
        "blocker",
    ):
        updated.pop(field, None)
    updated["last_action_type"] = action.action_type
    updated["last_actor"] = action.actor
    return updated


def _architecture_review_reopened_reducer(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> Mapping[str, Any]:
    updated = dict(_worker_finished_reducer(payload, action))
    for field in ("review_artifact_ref", "human_review_card_ref"):
        updated.pop(field, None)
    return updated


def _architecture_snapshotted_reducer(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> Mapping[str, Any]:
    updated = dict(_merge_payload(payload, action))
    updated.pop("architecture_repair_baseline_ref", None)
    return updated


def _worker_started_reducer(payload: Mapping[str, Any], action: ActionEnvelope) -> Mapping[str, Any]:
    updated = dict(_merge_payload(payload, action))
    for field in ("blocker", "failure_artifact_ref"):
        updated.pop(field, None)
    return updated


def _architecture_worker_started_reducer(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> Mapping[str, Any]:
    updated = dict(_worker_started_reducer(payload, action))
    for field in (
        "process_group_reaped",
        "exclusive_workspace_lock",
        "workspace_fingerprint",
        "workspace_lock_path",
        "pending_architecture_submission_ref",
    ):
        updated.pop(field, None)
    return updated


def _architecture_resume_cleanup_reducer(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> Mapping[str, Any]:
    updated = dict(_resume_cleanup_reducer(payload, action))
    if action.action_type == "RESOLVE_TRIAGE":
        updated["architect_session_generation"] = int(
            payload.get("architect_session_generation") or 0
        ) + 1
    updated.pop("failure_artifact_ref", None)
    source_field = "triage_resume_state" if action.action_type == "RESOLVE_TRIAGE" else "resume_state"
    source = str(payload.get(source_field) or "")
    if source != "ARCHITECT_SNAPSHOTTING":
        for field in (
            "process_group_reaped",
            "exclusive_workspace_lock",
            "workspace_fingerprint",
            "workspace_lock_path",
        ):
            updated.pop(field, None)
    return updated


def _cancel_reducer(cancel_target: str):
    def reducer(payload: Mapping[str, Any], action: ActionEnvelope) -> Mapping[str, Any]:
        updated = dict(_merge_payload(payload, action))
        updated["cancel_target"] = cancel_target
        return updated

    return reducer


def _resume_target(allowed_states: frozenset[str], field: str = "resume_state"):
    def resolve(payload: Mapping[str, Any], _action: ActionEnvelope) -> str:
        target = str(payload.get(field) or "")
        if target not in allowed_states:
            raise TransitionGuardError(f"invalid {field}: {target or '<empty>'}")
        return target

    return resolve


def _mapped_resume_target(mapping: Mapping[str, str], field: str = "resume_state"):
    def resolve(payload: Mapping[str, Any], _action: ActionEnvelope) -> str:
        source = str(payload.get(field) or "")
        target = mapping.get(source)
        if target is None:
            raise TransitionGuardError(f"invalid {field}: {source or '<empty>'}")
        return target

    return resolve


def _mapped_triage_resume_target(mapping: Mapping[str, str]):
    def resolve(payload: Mapping[str, Any], _action: ActionEnvelope) -> str:
        source = str(payload.get("triage_resume_state") or "")
        if source == "PAUSE_REQUESTED":
            source = str(payload.get("resume_state") or "")
        target = mapping.get(source)
        if target is None:
            raise TransitionGuardError(f"invalid triage resume state: {source or '<empty>'}")
        return target

    return resolve


def _ready_dependencies(payload: Mapping[str, Any], action: ActionEnvelope) -> None:
    dependencies = {str(item) for item in list(payload.get("dependency_node_ids") or [])}
    accepted = {
        str(item)
        for item in list(action.payload.get("accepted_dependency_node_ids", payload.get("accepted_dependency_node_ids")) or [])
    }
    if dependencies - accepted:
        raise TransitionGuardError("node dependencies are not all ACCEPTED")
    if bool(action.payload.get("epoch_frozen", payload.get("epoch_frozen"))):
        raise TransitionGuardError("execution epoch is frozen")


def _node_kind(expected: str):
    def guard(payload: Mapping[str, Any], _action: ActionEnvelope) -> None:
        actual = str(payload.get("node_kind") or "unit")
        if actual != expected:
            raise TransitionGuardError(f"action requires node_kind={expected}, found {actual}")

    return guard


def _node_kind_in(*expected: str):
    allowed = frozenset(expected)

    def guard(payload: Mapping[str, Any], _action: ActionEnvelope) -> None:
        actual = str(payload.get("node_kind") or "unit")
        if actual not in allowed:
            expected_text = ", ".join(sorted(allowed))
            raise TransitionGuardError(
                f"action requires node_kind in {{{expected_text}}}, found {actual}"
            )

    return guard


def _lease_guard(_payload: Mapping[str, Any], action: ActionEnvelope) -> None:
    token = action.payload.get("fencing_token")
    if not isinstance(token, int) or token <= 0:
        raise TransitionGuardError(f"{action.action_type} requires a positive fencing_token")


def _worker_failure_guard(_payload: Mapping[str, Any], action: ActionEnvelope) -> None:
    _required("failure_artifact_ref", "blocker")(_payload, action)
    blocker = action.payload.get("blocker")
    if not isinstance(blocker, Mapping):
        raise TransitionGuardError("WORKER_FAILED blocker must be a structured mapping")
    if not str(blocker.get("kind") or "").strip() or not str(
        blocker.get("summary") or ""
    ).strip():
        raise TransitionGuardError("WORKER_FAILED blocker requires kind and summary")


def _quiesce_guard(_payload: Mapping[str, Any], action: ActionEnvelope) -> None:
    _lease_guard(_payload, action)
    if action.payload.get("process_group_reaped") is not True:
        raise TransitionGuardError("worker process group has not been reaped")
    if action.payload.get("exclusive_workspace_lock") is not True:
        raise TransitionGuardError("exclusive worktree lock is required")
    if not str(action.payload.get("workspace_fingerprint") or ""):
        raise TransitionGuardError("worktree fingerprint is required")


def _candidate_guard(payload: Mapping[str, Any], action: ActionEnvelope) -> None:
    _required("candidate_ref", "candidate_digest", "workspace_fingerprint")(payload, action)
    before = str(payload.get("workspace_fingerprint") or "")
    after = str(action.payload.get("workspace_fingerprint") or "")
    if before and before != after:
        raise TransitionGuardError("worktree changed while candidate snapshot was created")


def _allowed_unknown_guard(_payload: Mapping[str, Any], action: ActionEnvelope) -> None:
    if action.payload.get("policy_allows_unknown") is not True:
        raise TransitionGuardError("UNKNOWN is not allowed by policy")
    if action.payload.get("assumption_ref") in (None, "", {}):
        raise TransitionGuardError("UNKNOWN requires an Assumption Ledger reference")
    if action.payload.get("hard_or_core_semantics") is True and not action.payload.get("human_waiver_ref"):
        raise TransitionGuardError("hard/core UNKNOWN requires a HumanWaiverArtifact")


def _workflow_transitions() -> list[TransitionSpec]:
    kind = AggregateType.WORKFLOW
    S = WorkflowState
    active = {S.CREATED, S.ACTIVE}
    transitions = [
        _spec(kind, None, "CREATE_WORKFLOW", S.CREATED, effects=_effect("submit_action", action_type="START_WORKFLOW")),
        _spec(kind, S.CREATED, "START_WORKFLOW", S.ACTIVE, effects=_effect("route_workflow")),
        _spec(kind, S.ACTIVE, "LINK_ARCHITECTURE_REVISION", S.ACTIVE, guard=_required("architecture_revision_id")),
        _spec(kind, S.ACTIVE, "LINK_EXECUTION_EPOCH", S.ACTIVE, guard=_required("execution_epoch_id")),
        _spec(kind, S.ACTIVE, "LINK_STANDALONE_REVIEW", S.ACTIVE, guard=_required("standalone_review_id")),
        _spec(kind, S.ACTIVE, "REBIND_CHANNEL", S.ACTIVE, guard=_required("active_channel", "control_route")),
        _spec(kind, S.ACTIVE, "MARK_COMPLETED", S.COMPLETED, guard=_required("result_artifact_ref")),
        _spec(kind, S.ACTIVE, "REJECT_WORKFLOW", S.REJECTED),
        _spec(kind, S.PAUSE_REQUESTED, "CHILDREN_PAUSED", S.PAUSED),
        _spec(
            kind,
            S.PAUSED,
            "RESUME",
            S.ACTIVE,
            reducer=_resume_cleanup_reducer,
            effects=_effect("propagate_resume"),
        ),
        _spec(kind, S.CANCEL_REQUESTED, "CHILDREN_CANCELLED", S.CANCELLED),
    ]
    for state in active:
        transitions.append(
            _spec(
                kind,
                state,
                "REQUEST_PAUSE",
                S.PAUSE_REQUESTED,
                reducer=_pause_reducer(str(state)),
                effects=_effect("propagate_pause"),
            )
        )
    for state in {S.CREATED, S.ACTIVE, S.PAUSE_REQUESTED, S.PAUSED, S.TRIAGE_REQUIRED}:
        transitions.append(
            _spec(kind, state, "REQUEST_CANCEL", S.CANCEL_REQUESTED, effects=_effect("propagate_cancel"))
        )
    for state in {S.CREATED, S.ACTIVE, S.PAUSE_REQUESTED}:
        transitions.append(
            _spec(kind, state, "ENTER_TRIAGE", S.TRIAGE_REQUIRED, reducer=_triage_reducer(str(state)))
        )
    transitions.append(
        _spec(
            kind,
            S.TRIAGE_REQUIRED,
            "RESOLVE_TRIAGE",
            S.ACTIVE,
            reducer=_resume_cleanup_reducer,
            effects=_effect("reconcile_workflow"),
        )
    )
    for state in {S.COMPLETED, S.REJECTED, S.CANCELLED}:
        transitions.append(_spec(kind, state, "ARCHIVE", state, reducer=_merge_payload))
    return transitions


def _task_transitions() -> list[TransitionSpec]:
    kind = AggregateType.TASK
    S = TaskState
    return [
        _spec(
            kind,
            None,
            "CREATE_TASK",
            S.ACTIVE,
            guard=_required("family_id", "task_revision_ref"),
        ),
        _spec(
            kind,
            S.ACTIVE,
            "UPDATE_TASK_CONTEXT",
            S.ACTIVE,
            guard=_required("task_revision_ref"),
        ),
        _spec(kind, S.ACTIVE, "ARCHIVE_TASK", S.ARCHIVED),
    ]


def _architecture_transitions() -> list[TransitionSpec]:
    kind = AggregateType.ARCHITECTURE_REVISION
    S = ArchitectureRevisionState
    transitions = [
        _spec(kind, None, "CREATE_ARCHITECTURE_REVISION", S.ARCHITECT_QUEUED, effects=_effect("enqueue_architecture_stage", stage="architect")),
        _spec(kind, None, "IMPORT_ARCHITECTURE_REVISION", S.REVIEW_QUEUED, guard=_required("architecture_manifest_ref"), effects=_effect("enqueue_architecture_review")),
        _spec(
            kind,
            S.ARCHITECT_QUEUED,
            "START_ARCHITECT",
            S.ARCHITECT_RUNNING,
            guard=_lease_guard,
            reducer=_architecture_worker_started_reducer,
        ),
        _spec(
            kind,
            S.ARCHITECT_RUNNING,
            "REBIND_ARCHITECT",
            S.ARCHITECT_RUNNING,
            guard=_lease_guard,
            reducer=_architecture_worker_started_reducer,
        ),
        _spec(
            kind,
            S.ARCHITECT_RUNNING,
            "ARCHITECT_SUBMITTED",
            S.ARCHITECT_QUIESCING,
            guard=_all(_required("requirements_ref", "pending_architecture_submission_ref", "architecture_workspace_path"), _lease_guard),
            effects=_effect("quiesce_architect"),
        ),
        _spec(kind, S.ARCHITECT_QUIESCING, "REBIND_ARCHITECT_QUIESCER", S.ARCHITECT_QUIESCING, guard=_lease_guard),
        _spec(
            kind,
            S.ARCHITECT_QUIESCING,
            "ARCHITECT_QUIESCED",
            S.ARCHITECT_SNAPSHOTTING,
            guard=_quiesce_guard,
            effects=_effect("snapshot_architecture"),
        ),
        _spec(kind, S.ARCHITECT_SNAPSHOTTING, "REBIND_ARCHITECT_SNAPSHOTTER", S.ARCHITECT_SNAPSHOTTING, guard=_lease_guard),
        _spec(
            kind,
            S.ARCHITECT_SNAPSHOTTING,
            "ARCHITECTURE_SNAPSHOTTED",
            S.REVIEW_QUEUED,
            guard=_required("requirements_ref", "architecture_manifest_ref"),
            reducer=_architecture_snapshotted_reducer,
            effects=_effect("enqueue_architecture_review"),
        ),
        _spec(
            kind,
            S.ARCHITECT_SNAPSHOTTING,
            "ARCHITECTURE_SNAPSHOT_REJECTED",
            S.ARCHITECT_QUEUED,
            guard=_required("finding_artifact_ref", "architecture_repair_baseline_ref"),
            effects=_effect("enqueue_architecture_stage", stage="architect"),
        ),
        _spec(kind, S.ARCHITECT_RUNNING, "ARCHITECT_COMPLETED", S.REVIEW_QUEUED, guard=_required("requirements_ref", "architecture_manifest_ref"), effects=_effect("enqueue_architecture_review")),
        _spec(kind, S.ARCHITECT_RUNNING, "CLARIFICATION_REQUIRED", S.CLARIFICATION_PENDING, guard=_required("clarification_ref"), effects=_effect("request_human_clarification")),
        _spec(kind, S.CLARIFICATION_PENDING, "CLARIFICATION_PROVIDED", S.ARCHITECT_QUEUED, guard=_required("decision_token", "clarification_ref", "clarification_response_ref"), effects=_effect("enqueue_architecture_stage", stage="architect")),
        _spec(kind, S.REVIEW_QUEUED, "START_ARCHITECTURE_REVIEW", S.REVIEWING, guard=_lease_guard),
        _spec(kind, S.REVIEWING, "REBIND_ARCHITECTURE_REVIEW", S.REVIEWING, guard=_lease_guard),
        _spec(
            kind,
            S.REVIEWING,
            "ARCHITECTURE_REVIEW_PASSED",
            S.HUMAN_REVIEW,
            guard=_required("review_artifact_ref", "architecture_manifest_ref"),
            reducer=_worker_finished_reducer,
            effects=_effect("publish_human_architecture_review"),
        ),
        _spec(
            kind,
            S.HUMAN_REVIEW,
            "HUMAN_REVIEW_PUBLISHED",
            S.HUMAN_REVIEW,
            guard=_required("human_review_card_ref"),
        ),
        _spec(
            kind,
            S.HUMAN_REVIEW,
            "REOPEN_ARCHITECTURE_REVIEW",
            S.REVIEW_QUEUED,
            guard=_required("reason"),
            reducer=_architecture_review_reopened_reducer,
            effects=_effect("enqueue_architecture_review"),
        ),
        _spec(
            kind,
            S.REVIEWING,
            "REQUIREMENTS_DEFECT",
            S.ARCHITECT_QUEUED,
            guard=_required("finding_artifact_ref"),
            reducer=_worker_finished_reducer,
            effects=_effect("enqueue_architecture_stage", stage="architect"),
        ),
        _spec(
            kind,
            S.REVIEWING,
            "CONTRACT_DEFECT",
            S.ARCHITECT_QUEUED,
            guard=_required("finding_artifact_ref"),
            reducer=_worker_finished_reducer,
            effects=_effect("enqueue_architecture_stage", stage="architect"),
        ),
        _spec(
            kind,
            S.REVIEWING,
            "ARCHITECTURE_DEFECT",
            S.ARCHITECT_QUEUED,
            guard=_required("finding_artifact_ref"),
            reducer=_worker_finished_reducer,
            effects=_effect("enqueue_architecture_stage", stage="architect"),
        ),
        _spec(
            kind,
            S.HUMAN_REVIEW,
            "HUMAN_ACCEPT",
            S.ACCEPTED,
            guard=_required("decision_token", "architecture_manifest_ref"),
            effects=_combined_effects(
                _effect("materialize_plan_revision", status="accepted"),
                _effect("submit_action", action_type="START_EXECUTION"),
            ),
        ),
        _spec(
            kind,
            S.HUMAN_REVIEW,
            "HUMAN_EDIT",
            S.SUPERSEDED,
            guard=_required("decision_token", "edit_instruction_ref"),
            effects=_combined_effects(
                _effect("materialize_plan_revision", status="revision_requested"),
                _effect("create_architecture_revision"),
            ),
        ),
        _spec(
            kind,
            S.HUMAN_REVIEW,
            "HUMAN_REJECT",
            S.REJECTED,
            guard=_required("decision_token"),
            effects=_combined_effects(
                _effect("materialize_plan_revision", status="rejected"),
                _effect("submit_workflow_rejection"),
            ),
        ),
        _spec(kind, S.PAUSE_REQUESTED, "PAUSE_CONFIRMED", S.PAUSED),
        _spec(kind, S.CANCEL_REQUESTED, "CANCEL_CONFIRMED", S.CANCELLED),
    ]
    pausable = {
        S.ARCHITECT_QUEUED,
        S.ARCHITECT_RUNNING,
        S.ARCHITECT_QUIESCING,
        S.ARCHITECT_SNAPSHOTTING,
        S.REVIEW_QUEUED,
        S.REVIEWING,
        S.HUMAN_REVIEW,
        S.CLARIFICATION_PENDING,
    }
    for state in pausable:
        transitions.append(_spec(kind, state, "REQUEST_PAUSE", S.PAUSE_REQUESTED, reducer=_pause_reducer(str(state)), effects=_effect("pause_aggregate_work")))
    architecture_resume = {
        str(S.ARCHITECT_QUEUED): str(S.ARCHITECT_QUEUED),
        str(S.ARCHITECT_RUNNING): str(S.ARCHITECT_QUEUED),
        str(S.ARCHITECT_QUIESCING): str(S.ARCHITECT_QUIESCING),
        str(S.ARCHITECT_SNAPSHOTTING): str(S.ARCHITECT_SNAPSHOTTING),
        str(S.REVIEW_QUEUED): str(S.REVIEW_QUEUED),
        str(S.REVIEWING): str(S.REVIEW_QUEUED),
        str(S.HUMAN_REVIEW): str(S.HUMAN_REVIEW),
        str(S.CLARIFICATION_PENDING): str(S.CLARIFICATION_PENDING),
    }
    transitions.append(
        _spec(
            kind,
            S.PAUSED,
            "RESUME",
            _mapped_resume_target(architecture_resume),
            reducer=_architecture_resume_cleanup_reducer,
            effects=_effect("resume_aggregate_work"),
        )
    )
    cancellable = pausable | {S.PAUSE_REQUESTED, S.PAUSED, S.TRIAGE_REQUIRED}
    for state in cancellable:
        transitions.append(_spec(kind, state, "REQUEST_CANCEL", S.CANCEL_REQUESTED, effects=_effect("cancel_aggregate_work")))
    triageable = pausable | {S.PAUSE_REQUESTED}
    for state in triageable:
        transitions.append(_spec(kind, state, "ENTER_TRIAGE", S.TRIAGE_REQUIRED, reducer=_triage_reducer(str(state))))
    for state in {S.ARCHITECT_RUNNING, S.REVIEWING}:
        transitions.append(
            _spec(
                kind,
                state,
                "WORKER_FAILED",
                S.TRIAGE_REQUIRED,
                guard=_worker_failure_guard,
                reducer=_triage_reducer(str(state)),
            )
        )
    transitions.append(
        _spec(
            kind,
            S.TRIAGE_REQUIRED,
            "RESOLVE_TRIAGE",
            _mapped_triage_resume_target(architecture_resume),
            reducer=_architecture_resume_cleanup_reducer,
            effects=_effect("reconcile_architecture_revision"),
        )
    )
    return transitions


def _execution_transitions() -> list[TransitionSpec]:
    kind = AggregateType.EXECUTION_EPOCH
    S = ExecutionEpochState
    transitions = [
        _spec(kind, None, "CREATE_EXECUTION_EPOCH", S.NOT_STARTED, guard=_required("architecture_manifest_ref", "topology_ref")),
        _spec(kind, S.NOT_STARTED, "START_EXECUTION", S.STARTING),
        _spec(kind, S.STARTING, "NODES_COMPILED", S.RUNNING, guard=_required("node_ids"), effects=_effect("schedule_ready_nodes")),
        _spec(kind, S.RUNNING, "SCHEDULE_TICK", S.RUNNING, effects=_effect("schedule_ready_nodes")),
        _spec(kind, S.RUNNING, "ALL_UNIT_NODES_ACCEPTED", S.RUNNING, effects=_effect("queue_integration_node")),
        _spec(kind, S.RUNNING, "INTEGRATION_ACCEPTED", S.FINALIZING, guard=_required("integration_candidate_ref"), effects=_effect("publish_final_deliverable")),
        _spec(
            kind,
            S.RUNNING,
            "ALL_REQUIRED_NODES_ACCEPTED",
            S.FINALIZING,
            guard=_required("accepted_candidate_refs", "verification_artifact_refs"),
            effects=_effect("publish_final_deliverable"),
        ),
        _spec(kind, S.FINALIZING, "FINAL_DELIVERABLE_PUBLISHED", S.COMPLETED, guard=_required("published_deliverable_ref"), effects=_effect("submit_workflow_completion")),
        _spec(
            kind,
            S.RUNNING,
            "REGISTER_REPLAN_FINDING",
            S.REPLAN_COLLECTING,
            guard=_required("finding_artifact_ref"),
            reducer=_replan_finding_reducer,
            effects=_effect("freeze_epoch_for_replan"),
        ),
        _spec(
            kind,
            S.FINALIZING,
            "REGISTER_REPLAN_FINDING",
            S.REPLAN_COLLECTING,
            guard=_required("finding_artifact_ref"),
            reducer=_replan_finding_reducer,
            effects=_effect("freeze_epoch_for_replan"),
        ),
        _spec(
            kind,
            S.REPLAN_COLLECTING,
            "REGISTER_REPLAN_FINDING",
            S.REPLAN_COLLECTING,
            guard=_required("finding_artifact_ref"),
            reducer=_replan_finding_reducer,
            effects=_effect("reconcile_execution_epoch"),
        ),
        _spec(
            kind,
            S.REPLAN_COLLECTING,
            "RECONCILE_REPLAN_COLLECTION",
            S.REPLAN_COLLECTING,
            effects=_effect("reconcile_execution_epoch"),
        ),
        _spec(
            kind,
            S.REPLAN_COLLECTING,
            "REPLAN_BATCH_READY",
            S.REPLAN_REQUIRED,
            guard=_required("replan_finding_batch_ref", "replan_finding_fingerprints"),
            reducer=_replan_batch_ready_reducer,
            effects=_effect("create_replan_revision"),
        ),
        _spec(
            kind,
            S.REPLAN_REQUIRED,
            "REPLAN_REVISION_LINKED",
            S.REPLAN_REQUIRED,
            guard=_required("active_replan_revision_id"),
        ),
        _spec(
            kind,
            S.REPLAN_REQUIRED,
            "REOPEN_REPLAN_COLLECTION",
            S.REPLAN_COLLECTING,
            guard=_required("finding_entries"),
            reducer=_reopen_replan_collection_reducer,
            effects=_effect("freeze_epoch_for_replan"),
        ),
        _spec(
            kind,
            S.REPLAN_REQUIRED,
            "REPLACEMENT_EPOCH_STARTED",
            S.SUPERSEDED,
            guard=_required("replacement_execution_epoch_id"),
            reducer=_replacement_epoch_started_reducer,
        ),
        _spec(kind, S.PAUSE_REQUESTED, "NODES_PAUSED", S.PAUSED),
        _spec(
            kind,
            S.PAUSED,
            "RESUME",
            _mapped_resume_target(
                {
                    str(S.STARTING): str(S.STARTING),
                    str(S.RUNNING): str(S.RUNNING),
                    str(S.REPLAN_COLLECTING): str(S.REPLAN_COLLECTING),
                    str(S.REPLAN_REQUIRED): str(S.REPLAN_REQUIRED),
                    str(S.FINALIZING): str(S.FINALIZING),
                }
            ),
            reducer=_resume_cleanup_reducer,
            effects=_effect("resume_epoch_nodes"),
        ),
        _spec(kind, S.CANCEL_REQUESTED, "NODES_CANCELLED", S.CANCELLED),
    ]
    for state in {
        S.STARTING,
        S.RUNNING,
        S.REPLAN_COLLECTING,
        S.REPLAN_REQUIRED,
        S.FINALIZING,
    }:
        transitions.append(_spec(kind, state, "REQUEST_PAUSE", S.PAUSE_REQUESTED, reducer=_pause_reducer(str(state)), effects=_effect("pause_epoch_nodes")))
    for state in {S.NOT_STARTED, S.STARTING, S.RUNNING, S.REPLAN_COLLECTING, S.PAUSE_REQUESTED, S.PAUSED, S.REPLAN_REQUIRED, S.FINALIZING, S.TRIAGE_REQUIRED}:
        transitions.append(_spec(kind, state, "REQUEST_CANCEL", S.CANCEL_REQUESTED, effects=_effect("cancel_epoch_nodes")))
    for state in {S.STARTING, S.RUNNING, S.REPLAN_COLLECTING, S.PAUSE_REQUESTED, S.REPLAN_REQUIRED, S.FINALIZING}:
        transitions.append(_spec(kind, state, "ENTER_TRIAGE", S.TRIAGE_REQUIRED, reducer=_triage_reducer(str(state))))
    transitions.append(
        _spec(
            kind,
            S.TRIAGE_REQUIRED,
            "RESOLVE_TRIAGE",
            _mapped_triage_resume_target(
                {
                    str(S.STARTING): str(S.STARTING),
                    str(S.RUNNING): str(S.RUNNING),
                    str(S.REPLAN_COLLECTING): str(S.REPLAN_COLLECTING),
                    str(S.REPLAN_REQUIRED): str(S.REPLAN_REQUIRED),
                    str(S.FINALIZING): str(S.FINALIZING),
                },
            ),
            reducer=_resume_cleanup_reducer,
            effects=_effect("reconcile_execution_epoch"),
        )
    )
    return transitions


def _node_transitions() -> list[TransitionSpec]:
    kind = AggregateType.DAG_NODE_RUN
    S = DagNodeRunState
    transitions = [
        _spec(kind, None, "CREATE_NODE_RUN", S.BLOCKED_BY_DEPS, guard=_required("unit_contract_ref", "epoch_id")),
        _spec(
            kind,
            S.BLOCKED_BY_DEPS,
            "REUSE_ACCEPTED_CANDIDATE",
            S.ACCEPTED,
            guard=_all(
                _node_kind("unit"),
                _required("candidate_ref", "candidate_digest", "verification_artifact_ref", "reuse_fingerprint"),
                _ready_dependencies,
            ),
            effects=_effects("notify_node_accepted", "publish_accepted_memory_candidate"),
        ),
        _spec(kind, S.BLOCKED_BY_DEPS, "DEPENDENCIES_ACCEPTED", S.QUEUED, guard=_all(_node_kind("unit"), _ready_dependencies), effects=_effect("enqueue_producer")),
        _spec(
            kind,
            S.BLOCKED_BY_DEPS,
            "LEGACY_INTEGRATION_DEPENDENCIES_ACCEPTED",
            S.QUEUED,
            guard=_all(_node_kind("integration"), _ready_dependencies),
            effects=_effect("enqueue_producer"),
        ),
        _spec(
            kind,
            S.BLOCKED_BY_DEPS,
            "VERIFICATION_DEPENDENCIES_ACCEPTED",
            S.VERIFY_PREPARING,
            guard=_all(_node_kind("verification"), _ready_dependencies),
            effects=_effect("prepare_verification_scenario"),
        ),
        _spec(
            kind,
            S.QUEUED,
            "START_PRODUCING",
            S.PRODUCING,
            guard=_all(_node_kind_in("unit", "integration"), _lease_guard),
            effects=_effect("spawn_producer_worker"),
            reducer=_worker_started_reducer,
        ),
        _spec(kind, S.PRODUCING, "REBIND_PRODUCER", S.PRODUCING, guard=_lease_guard, reducer=_worker_started_reducer),
        _spec(kind, S.PRODUCING, "SUBMIT_CANDIDATE", S.QUIESCING, guard=_lease_guard, effects=_effect("quiesce_worker")),
        _spec(kind, S.REPAIRING, "SUBMIT_CANDIDATE", S.QUIESCING, guard=_lease_guard, effects=_effect("quiesce_worker")),
        _spec(kind, S.PRODUCING, "PRODUCER_ARCHITECTURE_DEFECT", S.STALE, guard=_required("finding_artifact_ref"), effects=_effect("request_epoch_replan"), reducer=_worker_finished_reducer),
        _spec(kind, S.REPAIRING, "PRODUCER_ARCHITECTURE_DEFECT", S.STALE, guard=_required("finding_artifact_ref"), effects=_effect("request_epoch_replan"), reducer=_worker_finished_reducer),
        _spec(kind, S.QUIESCING, "QUIESCE_COMPLETED", S.SNAPSHOTTING, guard=_quiesce_guard, effects=_effect("snapshot_candidate")),
        _spec(kind, S.QUIESCING, "REBIND_QUIESCER", S.QUIESCING, guard=_lease_guard, reducer=_worker_started_reducer),
        _spec(kind, S.QUIESCING, "QUIESCE_FAILED", S.TRIAGE_REQUIRED, guard=_required("failure_artifact_ref")),
        _spec(kind, S.SNAPSHOTTING, "CANDIDATE_SNAPSHOTTED", S.REVIEW_QUEUED, guard=_candidate_guard, effects=_effect("enqueue_node_review")),
        _spec(kind, S.SNAPSHOTTING, "REBIND_SNAPSHOTTER", S.SNAPSHOTTING, guard=_lease_guard, reducer=_worker_started_reducer),
        _spec(kind, S.SNAPSHOTTING, "SNAPSHOT_FAILED", S.TRIAGE_REQUIRED, guard=_required("failure_artifact_ref")),
        _spec(kind, S.REVIEW_QUEUED, "START_REVIEW", S.REVIEWING, guard=_lease_guard, effects=_effect("spawn_verifier_worker"), reducer=_worker_started_reducer),
        _spec(kind, S.REVIEWING, "REBIND_REVIEWER", S.REVIEWING, guard=_lease_guard, reducer=_worker_started_reducer),
        _spec(kind, S.REVIEWING, "REVIEW_PASSED", S.ACCEPTED, guard=_required("verification_artifact_ref"), effects=_effects("notify_node_accepted", "publish_accepted_memory_candidate"), reducer=_worker_finished_reducer),
        _spec(kind, S.REVIEWING, "REVIEW_UNKNOWN_ALLOWED", S.ACCEPTED, guard=_all(_required("verification_artifact_ref"), _allowed_unknown_guard), effects=_effects("notify_node_accepted", "publish_accepted_memory_candidate"), reducer=_worker_finished_reducer),
        _spec(kind, S.REVIEWING, "REVIEW_FAILED", S.REPAIR_QUEUED, guard=_required("verification_artifact_ref", "repair_bill_ref", "finding_fingerprint"), effects=_effect("enqueue_repair"), reducer=_worker_finished_reducer),
        _spec(kind, S.REVIEWING, "DEPENDENCY_DEFECT", S.STALE, guard=_required("repair_bill_ref", "dependency_node_id"), effects=_effect("reopen_dependency_and_stale_descendants"), reducer=_worker_finished_reducer),
        _spec(kind, S.REVIEWING, "CONTRACT_DEFECT", S.STALE, guard=_required("repair_bill_ref"), effects=_effect("request_epoch_replan"), reducer=_worker_finished_reducer),
        _spec(kind, S.REVIEWING, "ARCHITECTURE_DEFECT", S.STALE, guard=_required("repair_bill_ref"), effects=_effect("request_epoch_replan"), reducer=_worker_finished_reducer),
        _spec(kind, S.REPAIR_QUEUED, "START_REPAIR", S.REPAIRING, guard=_lease_guard, effects=_effect("spawn_repair_worker"), reducer=_worker_started_reducer),
        _spec(kind, S.REPAIRING, "REBIND_REPAIRER", S.REPAIRING, guard=_lease_guard, reducer=_worker_started_reducer),
        _spec(
            kind,
            S.VERIFY_PREPARING,
            "VERIFICATION_PREPARED",
            S.QUEUED,
            guard=_required(
                "scenario_fingerprint",
                "scenario_candidate_union_ref",
                "scenario_commit_sha",
                "verification_workspace_fingerprint",
            ),
            effects=_effect("enqueue_scenario_verifier"),
        ),
        _spec(kind, S.VERIFY_PREPARING, "REBIND_VERIFICATION_PREPARER", S.VERIFY_PREPARING),
        _spec(
            kind,
            S.VERIFY_PREPARING,
            "RETRY_VERIFICATION_PREPARATION",
            S.VERIFY_PREPARING,
            effects=_effect("prepare_verification_scenario"),
        ),
        _spec(
            kind,
            S.QUEUED,
            "START_SCENARIO_VERIFICATION",
            S.VERIFYING,
            guard=_all(_node_kind("verification"), _lease_guard),
            effects=_effect("spawn_scenario_verifier"),
            reducer=_worker_started_reducer,
        ),
        _spec(kind, S.VERIFYING, "REBIND_SCENARIO_VERIFIER", S.VERIFYING, guard=_lease_guard, reducer=_worker_started_reducer),
        _spec(kind, S.VERIFYING, "VERIFICATION_PASSED", S.ACCEPTED, guard=_required("verification_artifact_ref", "scenario_fingerprint"), effects=_effect("notify_node_accepted"), reducer=_worker_finished_reducer),
        _spec(kind, S.VERIFYING, "VERIFICATION_UNKNOWN_ALLOWED", S.ACCEPTED, guard=_all(_required("verification_artifact_ref", "scenario_fingerprint"), _allowed_unknown_guard), effects=_effect("notify_node_accepted"), reducer=_worker_finished_reducer),
        _spec(kind, S.VERIFYING, "MODULE_DEFECT", S.STALE, guard=_required("repair_bill_ref", "module_node_id"), effects=_effect("reopen_dependency_and_stale_descendants"), reducer=_worker_finished_reducer),
        _spec(kind, S.VERIFYING, "DEPENDENCY_DEFECT", S.STALE, guard=_required("repair_bill_ref", "dependency_node_id"), effects=_effect("reopen_dependency_and_stale_descendants"), reducer=_worker_finished_reducer),
        _spec(kind, S.VERIFYING, "CONTRACT_DEFECT", S.STALE, guard=_required("repair_bill_ref"), effects=_effect("request_epoch_replan"), reducer=_worker_finished_reducer),
        _spec(kind, S.VERIFYING, "ARCHITECTURE_DEFECT", S.STALE, guard=_required("repair_bill_ref"), effects=_effect("request_epoch_replan"), reducer=_worker_finished_reducer),
        _spec(kind, S.STALE, "REQUEUE_STALE", S.QUEUED, guard=_all(_node_kind("unit"), _required("unit_contract_ref", "dependency_fingerprint"), _ready_dependencies), effects=_effect("enqueue_producer")),
        _spec(
            kind,
            S.STALE,
            "REQUEUE_LEGACY_INTEGRATION_STALE",
            S.QUEUED,
            guard=_all(
                _node_kind("integration"),
                _required("unit_contract_ref", "dependency_fingerprint"),
                _ready_dependencies,
            ),
            effects=_effect("enqueue_producer"),
        ),
        _spec(
            kind,
            S.STALE,
            "REQUEUE_VERIFICATION_STALE",
            S.VERIFY_PREPARING,
            guard=_all(_node_kind("verification"), _required("unit_contract_ref", "dependency_fingerprint"), _ready_dependencies),
            effects=_effect("prepare_verification_scenario"),
        ),
        _spec(kind, S.PAUSE_REQUESTED, "PAUSE_CONFIRMED", S.PAUSED),
        _spec(
            kind,
            S.CANCEL_REQUESTED,
            "CANCEL_CONFIRMED",
            _resume_target(frozenset({str(S.CANCELLED), str(S.STALE)}), "cancel_target"),
        ),
        _spec(
            kind,
            S.ACCEPTED,
            "REOPEN_DEPENDENCY",
            S.REPAIR_QUEUED,
            guard=_required("repair_bill_ref"),
            effects=_effect("enqueue_repair"),
            reducer=_new_node_role_generation_reducer,
        ),
        _spec(kind, S.ACCEPTED, "MEMORY_CANDIDATE_PUBLISHED", S.ACCEPTED, guard=_required("memory_candidate_ref")),
    ]
    pausable = {S.QUEUED, S.PRODUCING, S.REVIEW_QUEUED, S.REVIEWING, S.REPAIR_QUEUED, S.REPAIRING, S.VERIFY_PREPARING, S.VERIFYING}
    resumable = frozenset(str(state) for state in pausable)
    for state in pausable:
        transitions.append(_spec(kind, state, "REQUEST_PAUSE", S.PAUSE_REQUESTED, reducer=_pause_reducer(str(state)), effects=_effect("pause_node_worker")))
    node_resume = {
        str(S.QUEUED): str(S.QUEUED),
        str(S.PRODUCING): str(S.QUEUED),
        str(S.REVIEW_QUEUED): str(S.REVIEW_QUEUED),
        str(S.REVIEWING): str(S.REVIEW_QUEUED),
        str(S.REPAIR_QUEUED): str(S.REPAIR_QUEUED),
        str(S.REPAIRING): str(S.REPAIR_QUEUED),
        str(S.QUIESCING): str(S.QUEUED),
        str(S.SNAPSHOTTING): str(S.QUEUED),
        str(S.VERIFY_PREPARING): str(S.VERIFY_PREPARING),
        str(S.VERIFYING): str(S.VERIFY_PREPARING),
    }
    transitions.append(
        _spec(
            kind,
            S.PAUSED,
            "RESUME",
            _mapped_resume_target(node_resume),
            reducer=_resume_cleanup_reducer,
            effects=_effect("resume_node_work"),
        )
    )
    cancellable = pausable | {S.BLOCKED_BY_DEPS, S.QUIESCING, S.SNAPSHOTTING, S.STALE, S.PAUSE_REQUESTED, S.PAUSED, S.TRIAGE_REQUIRED}
    for state in cancellable:
        transitions.append(
            _spec(
                kind,
                state,
                "REQUEST_CANCEL",
                S.CANCEL_REQUESTED,
                reducer=_cancel_reducer(str(S.CANCELLED)),
                effects=_effect("cancel_node_worker"),
            )
        )
    directly_staleable = {S.BLOCKED_BY_DEPS, S.QUEUED, S.REVIEW_QUEUED, S.REPAIR_QUEUED, S.ACCEPTED, S.CANCELLED}
    for state in directly_staleable:
        transitions.append(
            _spec(
                kind,
                state,
                "MARK_STALE",
                S.STALE,
                guard=_required("stale_reason_ref"),
                effects=_effect("suspend_stale_node_assignments"),
                reducer=(
                    _new_node_role_generation_reducer
                    if state in {S.ACCEPTED, S.CANCELLED}
                    else _merge_payload
                ),
            )
        )
    for state in {S.PRODUCING, S.QUIESCING, S.SNAPSHOTTING, S.REVIEWING, S.REPAIRING, S.VERIFY_PREPARING, S.VERIFYING, S.PAUSE_REQUESTED, S.PAUSED}:
        transitions.append(
            _spec(
                kind,
                state,
                "REQUEST_STALE",
                S.CANCEL_REQUESTED,
                guard=_required("stale_reason_ref"),
                reducer=_cancel_reducer(str(S.STALE)),
                effects=_effect("cancel_node_worker"),
            )
        )
    triageable = pausable | {S.QUIESCING, S.SNAPSHOTTING, S.PAUSE_REQUESTED}
    for state in triageable:
        transitions.append(_spec(kind, state, "ENTER_TRIAGE", S.TRIAGE_REQUIRED, reducer=_triage_reducer(str(state))))
    for state in {S.PRODUCING, S.REVIEWING, S.REPAIRING, S.VERIFYING}:
        transitions.append(
            _spec(
                kind,
                state,
                "WORKER_FAILED",
                S.TRIAGE_REQUIRED,
                guard=_worker_failure_guard,
                reducer=_triage_reducer(str(state)),
            )
        )
    transitions.append(
        _spec(
            kind,
            S.TRIAGE_REQUIRED,
            "RESOLVE_TRIAGE",
            _mapped_triage_resume_target(node_resume),
            reducer=_resume_cleanup_reducer,
            effects=_effect("reconcile_node_run"),
        )
    )
    return transitions


def _standalone_review_transitions() -> list[TransitionSpec]:
    kind = AggregateType.STANDALONE_REVIEW
    S = StandaloneReviewState
    transitions = [
        _spec(kind, None, "CREATE_STANDALONE_REVIEW", S.RECEIVED, guard=_required("review_request_ref")),
        _spec(kind, S.RECEIVED, "QUEUE_REVIEW", S.REVIEW_QUEUED, effects=_effect("enqueue_standalone_review")),
        _spec(kind, S.REVIEW_QUEUED, "START_REVIEW", S.REVIEWING, guard=_lease_guard, effects=_effect("spawn_verifier_worker"), reducer=_worker_started_reducer),
        _spec(kind, S.REVIEWING, "REBIND_REVIEWER", S.REVIEWING, guard=_lease_guard, reducer=_worker_started_reducer),
        _spec(kind, S.REVIEWING, "REPORT_PRODUCED", S.REPORT_READY, guard=_required("verification_artifact_ref"), effects=_effect("publish_review_report"), reducer=_worker_finished_reducer),
        _spec(kind, S.REPORT_READY, "ACKNOWLEDGE_REPORT", S.COMPLETED, effects=_effect("submit_standalone_completion")),
        _spec(kind, S.REPORT_READY, "HANDOFF_REPAIR", S.COMPLETED, guard=_required("architecture_manifest_ref"), effects=_effect("start_review_repair_execution")),
        _spec(kind, S.PAUSE_REQUESTED, "PAUSE_CONFIRMED", S.PAUSED),
        _spec(kind, S.CANCEL_REQUESTED, "CANCEL_CONFIRMED", S.CANCELLED),
    ]
    pausable = {S.RECEIVED, S.REVIEW_QUEUED, S.REVIEWING, S.REPORT_READY}
    resumable = frozenset(str(state) for state in pausable)
    for state in pausable:
        transitions.append(_spec(kind, state, "REQUEST_PAUSE", S.PAUSE_REQUESTED, reducer=_pause_reducer(str(state)), effects=_effect("pause_aggregate_work")))
    transitions.append(
        _spec(
            kind,
            S.PAUSED,
            "RESUME",
            _resume_target(resumable),
            reducer=_resume_cleanup_reducer,
            effects=_effect("resume_aggregate_work"),
        )
    )
    for state in pausable | {S.PAUSE_REQUESTED, S.PAUSED, S.TRIAGE_REQUIRED}:
        transitions.append(_spec(kind, state, "REQUEST_CANCEL", S.CANCEL_REQUESTED, effects=_effect("cancel_aggregate_work")))
    for state in pausable | {S.PAUSE_REQUESTED}:
        transitions.append(_spec(kind, state, "ENTER_TRIAGE", S.TRIAGE_REQUIRED, reducer=_triage_reducer(str(state))))
    transitions.append(
        _spec(
            kind,
            S.REVIEWING,
            "WORKER_FAILED",
            S.TRIAGE_REQUIRED,
            guard=_worker_failure_guard,
            reducer=_triage_reducer(str(S.REVIEWING)),
        )
    )
    standalone_resume = {
        str(S.RECEIVED): str(S.RECEIVED),
        str(S.REVIEW_QUEUED): str(S.REVIEW_QUEUED),
        str(S.REVIEWING): str(S.REVIEW_QUEUED),
        str(S.REPORT_READY): str(S.REPORT_READY),
    }
    transitions.append(
        _spec(
            kind,
            S.TRIAGE_REQUIRED,
            "RESOLVE_TRIAGE",
            _mapped_triage_resume_target(standalone_resume),
            reducer=_resume_cleanup_reducer,
            effects=_effect("reconcile_standalone_review"),
        )
    )
    return transitions


def all_transition_specs() -> tuple[TransitionSpec, ...]:
    groups: Iterable[list[TransitionSpec]] = (
        _task_transitions(),
        _workflow_transitions(),
        _architecture_transitions(),
        _execution_transitions(),
        _node_transitions(),
        _standalone_review_transitions(),
    )
    return tuple(item for group in groups for item in group)


def build_default_transition_engine() -> TransitionEngine:
    return TransitionEngine(all_transition_specs())
