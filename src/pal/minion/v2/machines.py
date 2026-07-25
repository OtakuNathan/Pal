from __future__ import annotations

from collections.abc import Iterable, Mapping
from functools import lru_cache
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
from pal.minion.v2.machine_dsl import (
    ControlIntent,
    ControlPolicy,
    MachineSpec,
    ReconciliationKind,
    StateClass,
    StateRuntimeSpec,
    target_resolver,
)
from pal.minion.v2.role_contracts import OrchestrationRole, RoleActivation, RoleMode


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
    for field in (
        "blocker",
        "active_worker_id",
        "fencing_token",
        "lease_resource_key",
    ):
        updated.pop(field, None)
    return updated


def _workflow_cancel_reducer(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> Mapping[str, Any]:
    updated = dict(_merge_payload(payload, action))
    updated.pop("restart_execution_request", None)
    updated.pop("restart_cancel_requested", None)
    return updated


def _restart_cancel_reducer(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> Mapping[str, Any]:
    updated = dict(_merge_payload(payload, action))
    updated["restart_cancel_requested"] = True
    return updated


def _worker_finished_reducer(payload: Mapping[str, Any], action: ActionEnvelope) -> Mapping[str, Any]:
    updated = dict(_merge_payload(payload, action))
    for field in ("active_worker_id", "fencing_token", "lease_resource_key"):
        updated.pop(field, None)
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
    updated["architecture_review_generation"] = (
        int(payload.get("architecture_review_generation") or 0) + 1
    )
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


def _task_revision_appended_reducer(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> Mapping[str, Any]:
    updated = dict(_merge_payload(payload, action))
    updated["last_task_revision"] = dict(action.payload["task_revision"])
    updated["last_task_revision_ref"] = dict(action.payload["requirements_ref"])
    updated["last_task_revision_digest"] = str(action.payload["task_revision_digest"])
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


def _node_resume_cleanup_reducer(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> Mapping[str, Any]:
    updated = dict(_resume_cleanup_reducer(payload, action))
    updated.pop("failure_artifact_ref", None)
    source = str(payload.get("triage_resume_state") or "")
    if source not in {
        "SNAPSHOTTING",
        "REVIEW_SNAPSHOTTING",
        "VERIFY_SNAPSHOTTING",
    }:
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
    @target_resolver(*allowed_states, name=f"{field}_target")
    def resolve(payload: Mapping[str, Any], _action: ActionEnvelope) -> str:
        target = str(payload.get(field) or "")
        if target not in allowed_states:
            raise TransitionGuardError(f"invalid {field}: {target or '<empty>'}")
        return target

    return resolve


def _mapped_resume_target(mapping: Mapping[str, str], field: str = "resume_state"):
    @target_resolver(*mapping.values(), name=f"mapped_{field}_target")
    def resolve(payload: Mapping[str, Any], _action: ActionEnvelope) -> str:
        source = str(payload.get(field) or "")
        target = mapping.get(source)
        if target is None:
            raise TransitionGuardError(f"invalid {field}: {source or '<empty>'}")
        return target

    return resolve


def _mapped_triage_resume_target(mapping: Mapping[str, str]):
    @target_resolver(*mapping.values(), name="triage_resume_target")
    def resolve(payload: Mapping[str, Any], _action: ActionEnvelope) -> str:
        source = str(payload.get("triage_resume_state") or "")
        target = mapping.get(source)
        if target is None:
            raise TransitionGuardError(f"invalid triage resume state: {source or '<empty>'}")
        return target

    return resolve


@target_resolver(
    WorkflowState.CANCELLED,
    WorkflowState.RESTARTING,
    name="workflow_cancel_or_restart_target",
)
def _workflow_cancel_or_restart_target(
    payload: Mapping[str, Any],
    _action: ActionEnvelope,
) -> str:
    return (
        str(WorkflowState.RESTARTING)
        if payload.get("restart_execution_request")
        else str(WorkflowState.CANCELLED)
    )


def _workflow_cancelled_effects(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
    target_state: str,
) -> tuple[EffectDraft, ...]:
    if target_state != str(WorkflowState.RESTARTING):
        return ()
    return _effect("start_replacement_workflow_from_architecture")(
        payload,
        action,
        target_state,
    )


def _workflow_triage_resume_effects(
    payload: Mapping[str, Any],
    action: ActionEnvelope,
    target_state: str,
) -> tuple[EffectDraft, ...]:
    if target_state == str(WorkflowState.RESTARTING):
        return _effect("start_replacement_workflow_from_architecture")(
            payload,
            action,
            target_state,
        )
    return _effect("reconcile_workflow")(payload, action, target_state)


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


def _task_revision_append_guard(
    _payload: Mapping[str, Any],
    action: ActionEnvelope,
) -> None:
    _required(
        "requirements_ref",
        "task_revision",
        "task_revision_digest",
        "task_revision_sequence",
    )(
        _payload,
        action,
    )
    if not isinstance(action.payload.get("task_revision"), Mapping):
        raise TransitionGuardError("task revision must contain structured communication")


def _role_failure_guard(_payload: Mapping[str, Any], action: ActionEnvelope) -> None:
    _required("failure_artifact_ref", "blocker")(_payload, action)
    blocker = action.payload.get("blocker")
    if not isinstance(blocker, Mapping):
        raise TransitionGuardError("ROLE_FAILED blocker must be a structured mapping")
    if not str(blocker.get("kind") or "").strip() or not str(
        blocker.get("summary") or ""
    ).strip():
        raise TransitionGuardError("ROLE_FAILED blocker requires kind and summary")


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
        _spec(
            kind,
            S.CANCEL_REQUESTED,
            "CHILDREN_CANCELLED",
            _workflow_cancel_or_restart_target,
            effects=_workflow_cancelled_effects,
        ),
        _spec(
            kind,
            S.RESTARTING,
            "REPLACEMENT_WORKFLOW_STARTED",
            S.CANCELLED,
            guard=_required("replacement_workflow_id"),
        ),
        _spec(
            kind,
            S.RESTARTING,
            "REPLACEMENT_WORKFLOW_ABORTED",
            S.CANCELLED,
        ),
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
            _spec(
                kind,
                state,
                "REQUEST_CANCEL",
                S.CANCEL_REQUESTED,
                reducer=_workflow_cancel_reducer,
                effects=_effect("propagate_cancel"),
            )
        )
    transitions.append(
        _spec(
            kind,
            S.CANCEL_REQUESTED,
            "REQUEST_CANCEL",
            S.CANCEL_REQUESTED,
            reducer=_workflow_cancel_reducer,
        )
    )
    transitions.append(
        _spec(
            kind,
            S.RESTARTING,
            "REQUEST_CANCEL",
            S.RESTARTING,
            reducer=_restart_cancel_reducer,
        )
    )
    for state in {S.ACTIVE, S.PAUSE_REQUESTED, S.PAUSED, S.TRIAGE_REQUIRED}:
        transitions.append(
            _spec(
                kind,
                state,
                "REQUEST_EXECUTION_RESTART",
                S.CANCEL_REQUESTED,
                guard=_required("restart_execution_request"),
                effects=_effect("propagate_cancel"),
            )
        )
    workflow_triage_states = {
        S.CREATED,
        S.ACTIVE,
        S.PAUSE_REQUESTED,
        S.CANCEL_REQUESTED,
        S.RESTARTING,
    }
    for state in workflow_triage_states:
        transitions.append(
            _spec(
                kind,
                state,
                "ENTER_TRIAGE",
                S.TRIAGE_REQUIRED,
                reducer=_triage_reducer(str(state)),
                effects=(
                    _effect("freeze_workflow_children")
                    if state == S.ACTIVE
                    else _no_effects
                ),
            )
        )
    transitions.append(
        _spec(kind, S.TRIAGE_REQUIRED, "ENTER_TRIAGE", S.TRIAGE_REQUIRED)
    )
    transitions.append(
        _spec(
            kind,
            S.TRIAGE_REQUIRED,
            "RESOLVE_TRIAGE",
            _mapped_triage_resume_target(
                {
                    str(S.CREATED): str(S.CREATED),
                    str(S.ACTIVE): str(S.ACTIVE),
                    str(S.PAUSE_REQUESTED): str(S.PAUSE_REQUESTED),
                    str(S.CANCEL_REQUESTED): str(S.CANCEL_REQUESTED),
                    str(S.RESTARTING): str(S.RESTARTING),
                }
            ),
            reducer=_resume_cleanup_reducer,
            effects=_workflow_triage_resume_effects,
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
            guard=_required(
                "primary_profile_id",
                "family_id",
                "family_binding_ref",
                "task_revision_ref",
            ),
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
        _spec(kind, None, "CREATE_ARCHITECTURE_REVISION", S.ARCHITECT_QUEUED, effects=_effect("admit_architect_role")),
        _spec(kind, None, "IMPORT_ARCHITECTURE_REVISION", S.REVIEW_QUEUED, guard=_required("architecture_manifest_ref"), effects=_effect("run_reviewer_role", role_mode="architecture")),
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
            "DATA_ARCHITECT_COMPLETED",
            S.REVIEW_QUEUED,
            guard=_required("requirements_ref", "architecture_manifest_ref"),
            effects=_effect("run_reviewer_role", role_mode="architecture"),
        ),
        _spec(
            kind,
            S.ARCHITECT_RUNNING,
            "TASK_REVISION_APPENDED",
            S.ARCHITECT_RUNNING,
            guard=_task_revision_append_guard,
            reducer=_task_revision_appended_reducer,
        ),
        _spec(
            kind,
            S.ARCHITECT_RUNNING,
            "ARCHITECT_SUBMITTED",
            S.ARCHITECT_QUIESCING,
            guard=_all(
                _required(
                    "requirements_ref",
                    "pending_architecture_submission_ref",
                    "architecture_workspace_path",
                ),
                _lease_guard,
            ),
            effects=_effect("quiesce_architect_role"),
        ),
        _spec(kind, S.ARCHITECT_QUIESCING, "REBIND_ARCHITECT_QUIESCER", S.ARCHITECT_QUIESCING, guard=_lease_guard),
        _spec(
            kind,
            S.ARCHITECT_QUIESCING,
            "ARCHITECT_QUIESCED",
            S.ARCHITECT_SNAPSHOTTING,
            guard=_quiesce_guard,
            effects=_effect("snapshot_architect_result"),
        ),
        _spec(kind, S.ARCHITECT_SNAPSHOTTING, "REBIND_ARCHITECT_SNAPSHOTTER", S.ARCHITECT_SNAPSHOTTING, guard=_lease_guard),
        _spec(
            kind,
            S.ARCHITECT_SNAPSHOTTING,
            "ARCHITECTURE_SNAPSHOTTED",
            S.REVIEW_QUEUED,
            guard=_required("requirements_ref", "architecture_manifest_ref"),
            reducer=_architecture_snapshotted_reducer,
            effects=_effect("run_reviewer_role", role_mode="architecture"),
        ),
        _spec(
            kind,
            S.ARCHITECT_SNAPSHOTTING,
            "ARCHITECTURE_SNAPSHOT_REJECTED",
            S.ARCHITECT_QUEUED,
            guard=_required("finding_artifact_ref", "architecture_repair_baseline_ref"),
            effects=_effect("admit_architect_role"),
        ),
        _spec(kind, S.REVIEW_QUEUED, "START_ARCHITECTURE_REVIEW", S.REVIEWING, guard=_lease_guard),
        _spec(kind, S.REVIEWING, "REBIND_ARCHITECTURE_REVIEW", S.REVIEWING, guard=_lease_guard),
        _spec(
            kind,
            S.REVIEWING,
            "ARCHITECTURE_REVIEW_PASSED",
            S.HUMAN_REVIEW,
            guard=_required("review_artifact_ref", "architecture_manifest_ref"),
            reducer=_worker_finished_reducer,
            effects=_effect("publish_architecture_review_request"),
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
            effects=_effect("run_reviewer_role", role_mode="architecture"),
        ),
        _spec(
            kind,
            S.REVIEWING,
            "ARCHITECTURE_REVIEW_FAILED",
            S.ARCHITECT_QUEUED,
            guard=_required("finding_artifact_ref"),
            reducer=_worker_finished_reducer,
            effects=_effect("admit_architect_role"),
        ),
        _spec(
            kind,
            S.REVIEWING,
            "REQUIREMENTS_DEFECT",
            S.ARCHITECT_QUEUED,
            guard=_required("finding_artifact_ref"),
            reducer=_worker_finished_reducer,
            effects=_effect("admit_architect_role"),
        ),
        _spec(
            kind,
            S.REVIEWING,
            "CONTRACT_DEFECT",
            S.ARCHITECT_QUEUED,
            guard=_required("finding_artifact_ref"),
            reducer=_worker_finished_reducer,
            effects=_effect("admit_architect_role"),
        ),
        _spec(
            kind,
            S.REVIEWING,
            "ARCHITECTURE_DEFECT",
            S.ARCHITECT_QUEUED,
            guard=_required("finding_artifact_ref"),
            reducer=_worker_finished_reducer,
            effects=_effect("admit_architect_role"),
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
    }
    for state in pausable:
        transitions.append(_spec(kind, state, "REQUEST_PAUSE", S.PAUSE_REQUESTED, reducer=_pause_reducer(str(state)), effects=_effect("pause_role")))
    architecture_resume = {
        str(S.ARCHITECT_QUEUED): str(S.ARCHITECT_QUEUED),
        str(S.ARCHITECT_RUNNING): str(S.ARCHITECT_QUEUED),
        str(S.ARCHITECT_QUIESCING): str(S.ARCHITECT_QUIESCING),
        str(S.ARCHITECT_SNAPSHOTTING): str(S.ARCHITECT_SNAPSHOTTING),
        str(S.REVIEW_QUEUED): str(S.REVIEW_QUEUED),
        str(S.REVIEWING): str(S.REVIEW_QUEUED),
        str(S.HUMAN_REVIEW): str(S.HUMAN_REVIEW),
        str(S.PAUSE_REQUESTED): str(S.PAUSE_REQUESTED),
        str(S.CANCEL_REQUESTED): str(S.CANCEL_REQUESTED),
    }
    transitions.append(
        _spec(
            kind,
            S.PAUSED,
            "RESUME",
            _mapped_resume_target(architecture_resume),
            reducer=_architecture_resume_cleanup_reducer,
            effects=_effect("resume_semantic_state"),
        )
    )
    cancellable = pausable | {S.PAUSE_REQUESTED, S.PAUSED, S.TRIAGE_REQUIRED}
    for state in cancellable:
        transitions.append(_spec(kind, state, "REQUEST_CANCEL", S.CANCEL_REQUESTED, effects=_effect("cancel_role")))
    triageable = pausable | {S.PAUSE_REQUESTED, S.CANCEL_REQUESTED}
    for state in triageable:
        transitions.append(
            _spec(
                kind,
                state,
                "ENTER_TRIAGE",
                S.TRIAGE_REQUIRED,
                reducer=_triage_reducer(str(state)),
                effects=(
                    _effect("quiesce_role_for_triage")
                    if state not in {S.PAUSE_REQUESTED, S.CANCEL_REQUESTED}
                    else _no_effects
                ),
            )
        )
    transitions.append(
        _spec(kind, S.TRIAGE_REQUIRED, "ENTER_TRIAGE", S.TRIAGE_REQUIRED)
    )
    for state in {S.ARCHITECT_RUNNING, S.REVIEWING}:
        transitions.append(
            _spec(
                kind,
                state,
                "ROLE_FAILED",
                S.TRIAGE_REQUIRED,
                guard=_role_failure_guard,
                reducer=_triage_reducer(str(state)),
                effects=_effect("quiesce_role_for_triage"),
            )
        )
    transitions.append(
        _spec(
            kind,
            S.TRIAGE_REQUIRED,
            "RESOLVE_TRIAGE",
            _mapped_triage_resume_target(architecture_resume),
            reducer=_architecture_resume_cleanup_reducer,
            effects=_effect("reconcile_semantic_state"),
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
                    str(S.NOT_STARTED): str(S.NOT_STARTED),
                    str(S.STARTING): str(S.STARTING),
                    str(S.RUNNING): str(S.RUNNING),
                    str(S.REPLAN_COLLECTING): str(S.REPLAN_COLLECTING),
                    str(S.REPLAN_REQUIRED): str(S.REPLAN_REQUIRED),
                    str(S.FINALIZING): str(S.FINALIZING),
                    str(S.PAUSE_REQUESTED): str(S.PAUSE_REQUESTED),
                    str(S.CANCEL_REQUESTED): str(S.CANCEL_REQUESTED),
                }
            ),
            reducer=_resume_cleanup_reducer,
            effects=_effect("reconcile_execution_epoch"),
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
    execution_triage_states = {
        S.NOT_STARTED,
        S.STARTING,
        S.RUNNING,
        S.REPLAN_COLLECTING,
        S.PAUSE_REQUESTED,
        S.REPLAN_REQUIRED,
        S.FINALIZING,
        S.CANCEL_REQUESTED,
    }
    for state in execution_triage_states:
        transitions.append(
            _spec(
                kind,
                state,
                "ENTER_TRIAGE",
                S.TRIAGE_REQUIRED,
                reducer=_triage_reducer(str(state)),
                effects=(
                    _effect("freeze_epoch_nodes")
                    if state not in {S.NOT_STARTED, S.PAUSE_REQUESTED, S.CANCEL_REQUESTED}
                    else _no_effects
                ),
            )
        )
    transitions.append(
        _spec(kind, S.TRIAGE_REQUIRED, "ENTER_TRIAGE", S.TRIAGE_REQUIRED)
    )
    transitions.append(
        _spec(
            kind,
            S.TRIAGE_REQUIRED,
            "RESOLVE_TRIAGE",
            _mapped_triage_resume_target(
                {
                    str(S.NOT_STARTED): str(S.NOT_STARTED),
                    str(S.STARTING): str(S.STARTING),
                    str(S.RUNNING): str(S.RUNNING),
                    str(S.REPLAN_COLLECTING): str(S.REPLAN_COLLECTING),
                    str(S.REPLAN_REQUIRED): str(S.REPLAN_REQUIRED),
                    str(S.FINALIZING): str(S.FINALIZING),
                    str(S.PAUSE_REQUESTED): str(S.PAUSE_REQUESTED),
                    str(S.CANCEL_REQUESTED): str(S.CANCEL_REQUESTED),
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
        _spec(kind, S.BLOCKED_BY_DEPS, "DEPENDENCIES_ACCEPTED", S.QUEUED, guard=_all(_node_kind("unit"), _ready_dependencies), effects=_effect("admit_implementation_role", role_mode="produce")),
        _spec(
            kind,
            S.BLOCKED_BY_DEPS,
            "LEGACY_INTEGRATION_DEPENDENCIES_ACCEPTED",
            S.QUEUED,
            guard=_all(_node_kind("integration"), _ready_dependencies),
            effects=_effect("admit_implementation_role", role_mode="produce"),
        ),
        _spec(
            kind,
            S.BLOCKED_BY_DEPS,
            "VERIFICATION_DEPENDENCIES_ACCEPTED",
            S.VERIFY_PREPARING,
            guard=_all(_node_kind("system_verification"), _ready_dependencies),
            effects=_effect("prepare_system_verification"),
        ),
        _spec(
            kind,
            S.QUEUED,
            "START_PRODUCING",
            S.PRODUCING,
            guard=_all(_node_kind_in("unit", "integration"), _lease_guard),
            effects=_effect("run_implementation_role", role_mode="produce"),
            reducer=_worker_started_reducer,
        ),
        _spec(kind, S.PRODUCING, "REBIND_PRODUCER", S.PRODUCING, guard=_lease_guard, reducer=_worker_started_reducer),
        _spec(kind, S.PRODUCING, "SUBMIT_CANDIDATE", S.QUIESCING, guard=_lease_guard, effects=_effect("quiesce_implementation_role", role_mode="produce")),
        _spec(kind, S.REPAIRING, "SUBMIT_CANDIDATE", S.QUIESCING, guard=_lease_guard, effects=_effect("quiesce_implementation_role", role_mode="repair")),
        _spec(kind, S.PRODUCING, "PRODUCER_ARCHITECTURE_DEFECT", S.STALE, guard=_required("finding_artifact_ref"), effects=_effect("request_epoch_replan"), reducer=_worker_finished_reducer),
        _spec(kind, S.REPAIRING, "PRODUCER_ARCHITECTURE_DEFECT", S.STALE, guard=_required("finding_artifact_ref"), effects=_effect("request_epoch_replan"), reducer=_worker_finished_reducer),
        _spec(kind, S.QUIESCING, "QUIESCE_COMPLETED", S.SNAPSHOTTING, guard=_quiesce_guard, effects=_effect("snapshot_implementation_result")),
        _spec(kind, S.QUIESCING, "REBIND_QUIESCER", S.QUIESCING, guard=_lease_guard, reducer=_worker_started_reducer),
        _spec(
            kind,
            S.QUIESCING,
            "QUIESCE_FAILED",
            S.TRIAGE_REQUIRED,
            guard=_required("failure_artifact_ref"),
            reducer=_triage_reducer(str(S.QUIESCING)),
            effects=_effect("quiesce_role_for_triage"),
        ),
        _spec(kind, S.SNAPSHOTTING, "CANDIDATE_SNAPSHOTTED", S.REVIEW_QUEUED, guard=_candidate_guard, effects=_effect("admit_verifier_role", role_mode="module")),
        _spec(kind, S.SNAPSHOTTING, "REBIND_SNAPSHOTTER", S.SNAPSHOTTING, guard=_lease_guard, reducer=_worker_started_reducer),
        _spec(
            kind,
            S.SNAPSHOTTING,
            "SNAPSHOT_FAILED",
            S.TRIAGE_REQUIRED,
            guard=_required("failure_artifact_ref"),
            reducer=_triage_reducer(str(S.SNAPSHOTTING)),
            effects=_effect("quiesce_role_for_triage"),
        ),
        _spec(kind, S.REVIEW_QUEUED, "START_REVIEW", S.REVIEWING, guard=_lease_guard, effects=_effect("run_verifier_role", role_mode="module"), reducer=_worker_started_reducer),
        _spec(kind, S.REVIEWING, "REBIND_REVIEWER", S.REVIEWING, guard=_lease_guard, reducer=_worker_started_reducer),
        _spec(
            kind,
            S.REVIEWING,
            "SUBMIT_SEMANTIC_VERIFICATION",
            S.REVIEW_QUIESCING,
            guard=_required("pending_verification_ref"),
            effects=_effect("quiesce_verifier_role", role_mode="module"),
        ),
        _spec(
            kind,
            S.REVIEW_QUIESCING,
            "VERIFIER_QUIESCED",
            S.REVIEW_SNAPSHOTTING,
            guard=_quiesce_guard,
            effects=_effect("snapshot_verifier_result", role_mode="module"),
        ),
        _spec(kind, S.REPAIR_QUEUED, "START_REPAIR", S.REPAIRING, guard=_lease_guard, effects=_effect("run_implementation_role", role_mode="repair"), reducer=_worker_started_reducer),
        _spec(kind, S.REPAIRING, "REBIND_REPAIRER", S.REPAIRING, guard=_lease_guard, reducer=_worker_started_reducer),
        _spec(
            kind,
            S.VERIFY_PREPARING,
            "VERIFICATION_PREPARED",
            S.QUEUED,
            guard=_required(
                "system_fingerprint",
                "system_candidate_union_ref",
                "system_commit_sha",
                "verification_workspace_fingerprint",
            ),
            effects=_effect("admit_verifier_role", role_mode="system"),
        ),
        _spec(kind, S.VERIFY_PREPARING, "REBIND_VERIFICATION_PREPARER", S.VERIFY_PREPARING),
        _spec(
            kind,
            S.VERIFY_PREPARING,
            "RETRY_VERIFICATION_PREPARATION",
            S.VERIFY_PREPARING,
            effects=_effect("prepare_system_verification"),
        ),
        _spec(
            kind,
            S.QUEUED,
            "START_SYSTEM_VERIFICATION",
            S.VERIFYING,
            guard=_all(_node_kind("system_verification"), _lease_guard),
            effects=_effect("run_verifier_role", role_mode="system"),
            reducer=_worker_started_reducer,
        ),
        _spec(kind, S.VERIFYING, "REBIND_SYSTEM_VERIFIER", S.VERIFYING, guard=_lease_guard, reducer=_worker_started_reducer),
        _spec(
            kind,
            S.VERIFYING,
            "SUBMIT_SEMANTIC_VERIFICATION",
            S.VERIFY_QUIESCING,
            guard=_required("pending_verification_ref"),
            effects=_effect("quiesce_verifier_role", role_mode="system"),
        ),
        _spec(
            kind,
            S.VERIFY_QUIESCING,
            "VERIFIER_QUIESCED",
            S.VERIFY_SNAPSHOTTING,
            guard=_quiesce_guard,
            effects=_effect("snapshot_verifier_result", role_mode="system"),
        ),
        _spec(kind, S.REVIEW_SNAPSHOTTING, "REVIEW_PASSED", S.ACCEPTED, guard=_required("verification_artifact_ref"), effects=_effects("notify_node_accepted", "publish_accepted_memory_candidate"), reducer=_worker_finished_reducer),
        _spec(kind, S.REVIEW_SNAPSHOTTING, "REVIEW_UNKNOWN_ALLOWED", S.ACCEPTED, guard=_all(_required("verification_artifact_ref"), _allowed_unknown_guard), effects=_effects("notify_node_accepted", "publish_accepted_memory_candidate"), reducer=_worker_finished_reducer),
        _spec(kind, S.REVIEW_SNAPSHOTTING, "REVIEW_FAILED", S.REPAIR_QUEUED, guard=_required("verification_artifact_ref", "repair_bill_ref", "finding_fingerprint"), effects=_effect("admit_implementation_role", role_mode="repair"), reducer=_worker_finished_reducer),
        _spec(kind, S.REVIEW_SNAPSHOTTING, "DEPENDENCY_DEFECT", S.STALE, guard=_required("repair_bill_ref", "repair_target_node_id"), effects=_effect("reopen_dependency_and_stale_descendants"), reducer=_worker_finished_reducer),
        _spec(kind, S.REVIEW_SNAPSHOTTING, "CONTRACT_DEFECT", S.STALE, guard=_required("repair_bill_ref"), effects=_effect("request_epoch_replan"), reducer=_worker_finished_reducer),
        _spec(kind, S.REVIEW_SNAPSHOTTING, "ARCHITECTURE_DEFECT", S.STALE, guard=_required("repair_bill_ref"), effects=_effect("request_epoch_replan"), reducer=_worker_finished_reducer),
        _spec(kind, S.VERIFY_SNAPSHOTTING, "VERIFICATION_PASSED", S.ACCEPTED, guard=_required("verification_artifact_ref", "system_fingerprint"), effects=_effect("notify_node_accepted"), reducer=_worker_finished_reducer),
        _spec(kind, S.VERIFY_SNAPSHOTTING, "VERIFICATION_UNKNOWN_ALLOWED", S.ACCEPTED, guard=_all(_required("verification_artifact_ref", "system_fingerprint"), _allowed_unknown_guard), effects=_effect("notify_node_accepted"), reducer=_worker_finished_reducer),
        _spec(kind, S.VERIFY_SNAPSHOTTING, "MODULE_DEFECT", S.STALE, guard=_required("repair_bill_ref", "repair_target_node_id"), effects=_effect("reopen_dependency_and_stale_descendants"), reducer=_worker_finished_reducer),
        _spec(kind, S.VERIFY_SNAPSHOTTING, "DEPENDENCY_DEFECT", S.STALE, guard=_required("repair_bill_ref", "repair_target_node_id"), effects=_effect("reopen_dependency_and_stale_descendants"), reducer=_worker_finished_reducer),
        _spec(kind, S.VERIFY_SNAPSHOTTING, "VERIFICATION_DEFECT", S.STALE, guard=_required("repair_bill_ref", "repair_target_node_id"), effects=_effect("reopen_verifier_and_stale_descendants"), reducer=_worker_finished_reducer),
        _spec(kind, S.VERIFY_SNAPSHOTTING, "CONTRACT_DEFECT", S.STALE, guard=_required("repair_bill_ref"), effects=_effect("request_epoch_replan"), reducer=_worker_finished_reducer),
        _spec(kind, S.VERIFY_SNAPSHOTTING, "ARCHITECTURE_DEFECT", S.STALE, guard=_required("repair_bill_ref"), effects=_effect("request_epoch_replan"), reducer=_worker_finished_reducer),
        _spec(kind, S.STALE, "REQUEUE_STALE", S.QUEUED, guard=_all(_node_kind("unit"), _required("unit_contract_ref", "dependency_fingerprint"), _ready_dependencies), effects=_effect("admit_implementation_role", role_mode="produce")),
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
            effects=_effect("admit_implementation_role", role_mode="produce"),
        ),
        _spec(
            kind,
            S.STALE,
            "REQUEUE_VERIFICATION_STALE",
            S.VERIFY_PREPARING,
            guard=_all(_node_kind("system_verification"), _required("unit_contract_ref", "dependency_fingerprint"), _ready_dependencies),
            effects=_effect("prepare_system_verification"),
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
            effects=_effect("admit_implementation_role", role_mode="repair"),
            reducer=_merge_payload,
        ),
        _spec(
            kind,
            S.ACCEPTED,
            "REOPEN_VERIFICATION",
            S.REVIEW_QUEUED,
            guard=_required("repair_bill_ref"),
            effects=_effect("admit_verifier_role", role_mode="module"),
            reducer=_merge_payload,
        ),
        _spec(kind, S.ACCEPTED, "MEMORY_CANDIDATE_PUBLISHED", S.ACCEPTED, guard=_required("memory_candidate_ref")),
    ]
    pausable = {
        S.BLOCKED_BY_DEPS,
        S.QUEUED,
        S.PRODUCING,
        S.REVIEW_QUEUED,
        S.REVIEWING,
        S.REVIEW_QUIESCING,
        S.REVIEW_SNAPSHOTTING,
        S.REPAIR_QUEUED,
        S.REPAIRING,
        S.VERIFY_PREPARING,
        S.VERIFYING,
        S.VERIFY_QUIESCING,
        S.VERIFY_SNAPSHOTTING,
    }
    resumable = frozenset(str(state) for state in pausable)
    for state in pausable:
        transitions.append(_spec(kind, state, "REQUEST_PAUSE", S.PAUSE_REQUESTED, reducer=_pause_reducer(str(state)), effects=_effect("pause_role")))
    node_resume = {
        str(S.BLOCKED_BY_DEPS): str(S.BLOCKED_BY_DEPS),
        str(S.QUEUED): str(S.QUEUED),
        str(S.PRODUCING): str(S.QUEUED),
        str(S.REVIEW_QUEUED): str(S.REVIEW_QUEUED),
        str(S.REVIEWING): str(S.REVIEW_QUEUED),
        str(S.REVIEW_QUIESCING): str(S.REVIEW_QUIESCING),
        str(S.REVIEW_SNAPSHOTTING): str(S.REVIEW_SNAPSHOTTING),
        str(S.REPAIR_QUEUED): str(S.REPAIR_QUEUED),
        str(S.REPAIRING): str(S.REPAIR_QUEUED),
        str(S.QUIESCING): str(S.QUIESCING),
        str(S.SNAPSHOTTING): str(S.SNAPSHOTTING),
        str(S.VERIFY_PREPARING): str(S.VERIFY_PREPARING),
        str(S.VERIFYING): str(S.VERIFY_PREPARING),
        str(S.VERIFY_QUIESCING): str(S.VERIFY_QUIESCING),
        str(S.VERIFY_SNAPSHOTTING): str(S.VERIFY_SNAPSHOTTING),
        str(S.PAUSE_REQUESTED): str(S.PAUSE_REQUESTED),
        str(S.CANCEL_REQUESTED): str(S.CANCEL_REQUESTED),
    }
    transitions.append(
        _spec(
            kind,
            S.PAUSED,
            "RESUME",
            _mapped_resume_target(node_resume),
            reducer=_resume_cleanup_reducer,
            effects=_effect("resume_semantic_state"),
        )
    )
    cancellable = pausable | {S.BLOCKED_BY_DEPS, S.QUIESCING, S.SNAPSHOTTING, S.REVIEW_QUIESCING, S.REVIEW_SNAPSHOTTING, S.VERIFY_QUIESCING, S.VERIFY_SNAPSHOTTING, S.STALE, S.PAUSE_REQUESTED, S.PAUSED, S.TRIAGE_REQUIRED}
    for state in cancellable:
        transitions.append(
            _spec(
                kind,
                state,
                "REQUEST_CANCEL",
                S.CANCEL_REQUESTED,
                reducer=_cancel_reducer(str(S.CANCELLED)),
                effects=_effect("cancel_role"),
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
                reducer=_merge_payload,
            )
        )
    for state in {S.PRODUCING, S.QUIESCING, S.SNAPSHOTTING, S.REVIEWING, S.REVIEW_QUIESCING, S.REVIEW_SNAPSHOTTING, S.REPAIRING, S.VERIFY_PREPARING, S.VERIFYING, S.VERIFY_QUIESCING, S.VERIFY_SNAPSHOTTING, S.PAUSE_REQUESTED, S.PAUSED}:
        transitions.append(
            _spec(
                kind,
                state,
                "REQUEST_STALE",
                S.CANCEL_REQUESTED,
                guard=_required("stale_reason_ref"),
                reducer=_cancel_reducer(str(S.STALE)),
                effects=_effect("cancel_role"),
            )
        )
    triageable = pausable | {
        S.QUIESCING,
        S.SNAPSHOTTING,
        S.REVIEW_QUIESCING,
        S.REVIEW_SNAPSHOTTING,
        S.VERIFY_QUIESCING,
        S.VERIFY_SNAPSHOTTING,
        S.PAUSE_REQUESTED,
        S.CANCEL_REQUESTED,
    }
    for state in triageable:
        transitions.append(
            _spec(
                kind,
                state,
                "ENTER_TRIAGE",
                S.TRIAGE_REQUIRED,
                reducer=_triage_reducer(str(state)),
                effects=(
                    _effect("quiesce_role_for_triage")
                    if state not in {S.PAUSE_REQUESTED, S.CANCEL_REQUESTED}
                    else _no_effects
                ),
            )
        )
    transitions.append(
        _spec(kind, S.TRIAGE_REQUIRED, "ENTER_TRIAGE", S.TRIAGE_REQUIRED)
    )
    for state in {S.PRODUCING, S.REVIEWING, S.REPAIRING, S.VERIFYING}:
        transitions.append(
            _spec(
                kind,
                state,
                "ROLE_FAILED",
                S.TRIAGE_REQUIRED,
                guard=_role_failure_guard,
                reducer=_triage_reducer(str(state)),
                effects=_effect("quiesce_role_for_triage"),
            )
        )
    transitions.append(
        _spec(
            kind,
            S.TRIAGE_REQUIRED,
            "RESOLVE_TRIAGE",
            _mapped_triage_resume_target(node_resume),
            reducer=_node_resume_cleanup_reducer,
            effects=_effect("reconcile_semantic_state"),
        )
    )
    return transitions


def _standalone_review_transitions() -> list[TransitionSpec]:
    kind = AggregateType.STANDALONE_REVIEW
    S = StandaloneReviewState
    transitions = [
        _spec(kind, None, "CREATE_STANDALONE_REVIEW", S.RECEIVED, guard=_required("review_request_ref")),
        _spec(kind, S.RECEIVED, "QUEUE_REVIEW", S.REVIEW_QUEUED, effects=_effect("admit_reviewer_role", role_mode="standalone")),
        _spec(kind, S.REVIEW_QUEUED, "START_REVIEW", S.REVIEWING, guard=_lease_guard, effects=_effect("run_reviewer_role", role_mode="standalone"), reducer=_worker_started_reducer),
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
        transitions.append(_spec(kind, state, "REQUEST_PAUSE", S.PAUSE_REQUESTED, reducer=_pause_reducer(str(state)), effects=_effect("pause_role")))
    transitions.append(
        _spec(
            kind,
            S.PAUSED,
            "RESUME",
            _resume_target(resumable),
            reducer=_resume_cleanup_reducer,
            effects=_effect("resume_semantic_state"),
        )
    )
    for state in pausable | {S.PAUSE_REQUESTED, S.PAUSED, S.TRIAGE_REQUIRED}:
        transitions.append(_spec(kind, state, "REQUEST_CANCEL", S.CANCEL_REQUESTED, effects=_effect("cancel_role")))
    for state in pausable | {S.PAUSE_REQUESTED, S.CANCEL_REQUESTED}:
        transitions.append(
            _spec(
                kind,
                state,
                "ENTER_TRIAGE",
                S.TRIAGE_REQUIRED,
                reducer=_triage_reducer(str(state)),
                effects=(
                    _effect("quiesce_role_for_triage")
                    if state not in {S.PAUSE_REQUESTED, S.CANCEL_REQUESTED}
                    else _no_effects
                ),
            )
        )
    transitions.append(
        _spec(kind, S.TRIAGE_REQUIRED, "ENTER_TRIAGE", S.TRIAGE_REQUIRED)
    )
    transitions.append(
        _spec(
            kind,
            S.REVIEWING,
            "ROLE_FAILED",
            S.TRIAGE_REQUIRED,
            guard=_role_failure_guard,
            reducer=_triage_reducer(str(S.REVIEWING)),
            effects=_effect("quiesce_role_for_triage"),
        )
    )
    standalone_resume = {
        str(S.RECEIVED): str(S.RECEIVED),
        str(S.REVIEW_QUEUED): str(S.REVIEW_QUEUED),
        str(S.REVIEWING): str(S.REVIEW_QUEUED),
        str(S.REPORT_READY): str(S.REPORT_READY),
        str(S.PAUSE_REQUESTED): str(S.PAUSE_REQUESTED),
        str(S.CANCEL_REQUESTED): str(S.CANCEL_REQUESTED),
    }
    transitions.append(
        _spec(
            kind,
            S.TRIAGE_REQUIRED,
            "RESOLVE_TRIAGE",
            _mapped_triage_resume_target(standalone_resume),
            reducer=_resume_cleanup_reducer,
            effects=_effect("reconcile_semantic_state"),
        )
    )
    return transitions


def _state_class_map(
    state_enum: type,
    groups: Mapping[StateClass, Iterable[Any]],
) -> Mapping[str, StateClass]:
    result = {
        str(state): state_class
        for state_class, states in groups.items()
        for state in states
    }
    expected = {str(state) for state in state_enum}
    if set(result) != expected:
        missing = sorted(expected - set(result))
        extra = sorted(set(result) - expected)
        raise ValueError(
            f"state classification mismatch for {state_enum.__name__}: "
            f"missing={missing}, extra={extra}"
        )
    return result


def _activation(role: OrchestrationRole, mode: RoleMode) -> RoleActivation:
    return RoleActivation(role, mode)


def _runtime_state_map(
    *entries: tuple[
        Iterable[Any],
        Iterable[RoleActivation],
        ReconciliationKind,
    ],
) -> Mapping[str, StateRuntimeSpec]:
    result: dict[str, StateRuntimeSpec] = {}
    for states, activations, reconciliation in entries:
        runtime = StateRuntimeSpec(frozenset(activations), reconciliation)
        for state in states:
            key = str(state)
            if key in result:
                raise ValueError(f"duplicate runtime state declaration: {key}")
            result[key] = runtime
    return result


@lru_cache(maxsize=1)
def all_machine_specs() -> tuple[MachineSpec, ...]:
    task_classes = _state_class_map(
        TaskState,
        {
            StateClass.CATALOG: {TaskState.ACTIVE},
            StateClass.TERMINAL: {TaskState.ARCHIVED},
        },
    )
    workflow_classes = _state_class_map(
        WorkflowState,
        {
            StateClass.OUTBOX_WAIT: {
                WorkflowState.CREATED,
                WorkflowState.RESTARTING,
            },
            StateClass.CHILD_WAIT: {
                WorkflowState.ACTIVE,
                WorkflowState.PAUSE_REQUESTED,
                WorkflowState.CANCEL_REQUESTED,
            },
            StateClass.PAUSED: {WorkflowState.PAUSED},
            StateClass.TERMINAL: {
                WorkflowState.COMPLETED,
                WorkflowState.REJECTED,
                WorkflowState.CANCELLED,
            },
            StateClass.OPERATOR_WAIT: {WorkflowState.TRIAGE_REQUIRED},
        },
    )
    architecture_classes = _state_class_map(
        ArchitectureRevisionState,
        {
            StateClass.WORKER_LIVENESS: {
                ArchitectureRevisionState.ARCHITECT_QUEUED,
                ArchitectureRevisionState.ARCHITECT_RUNNING,
                ArchitectureRevisionState.ARCHITECT_QUIESCING,
                ArchitectureRevisionState.ARCHITECT_SNAPSHOTTING,
                ArchitectureRevisionState.REVIEW_QUEUED,
                ArchitectureRevisionState.REVIEWING,
                ArchitectureRevisionState.PAUSE_REQUESTED,
                ArchitectureRevisionState.CANCEL_REQUESTED,
            },
            StateClass.HUMAN_WAIT: {
                ArchitectureRevisionState.HUMAN_REVIEW,
            },
            StateClass.PAUSED: {ArchitectureRevisionState.PAUSED},
            StateClass.TERMINAL: {
                ArchitectureRevisionState.SUPERSEDED,
                ArchitectureRevisionState.ACCEPTED,
                ArchitectureRevisionState.REJECTED,
                ArchitectureRevisionState.CANCELLED,
            },
            StateClass.OPERATOR_WAIT: {ArchitectureRevisionState.TRIAGE_REQUIRED},
        },
    )
    execution_classes = _state_class_map(
        ExecutionEpochState,
        {
            StateClass.OUTBOX_WAIT: {
                ExecutionEpochState.NOT_STARTED,
                ExecutionEpochState.STARTING,
                ExecutionEpochState.FINALIZING,
            },
            StateClass.CHILD_WAIT: {
                ExecutionEpochState.RUNNING,
                ExecutionEpochState.REPLAN_COLLECTING,
                ExecutionEpochState.PAUSE_REQUESTED,
                ExecutionEpochState.REPLAN_REQUIRED,
                ExecutionEpochState.CANCEL_REQUESTED,
            },
            StateClass.PAUSED: {ExecutionEpochState.PAUSED},
            StateClass.TERMINAL: {
                ExecutionEpochState.SUPERSEDED,
                ExecutionEpochState.COMPLETED,
                ExecutionEpochState.CANCELLED,
            },
            StateClass.OPERATOR_WAIT: {ExecutionEpochState.TRIAGE_REQUIRED},
        },
    )
    node_classes = _state_class_map(
        DagNodeRunState,
        {
            StateClass.DEPENDENCY_WAIT: {
                DagNodeRunState.BLOCKED_BY_DEPS,
                DagNodeRunState.STALE,
            },
            StateClass.WORKER_LIVENESS: {
                DagNodeRunState.QUEUED,
                DagNodeRunState.PRODUCING,
                DagNodeRunState.QUIESCING,
                DagNodeRunState.SNAPSHOTTING,
                DagNodeRunState.REVIEW_QUEUED,
                DagNodeRunState.REVIEWING,
                DagNodeRunState.REVIEW_QUIESCING,
                DagNodeRunState.REVIEW_SNAPSHOTTING,
                DagNodeRunState.REPAIR_QUEUED,
                DagNodeRunState.REPAIRING,
                DagNodeRunState.VERIFY_PREPARING,
                DagNodeRunState.VERIFYING,
                DagNodeRunState.VERIFY_QUIESCING,
                DagNodeRunState.VERIFY_SNAPSHOTTING,
                DagNodeRunState.PAUSE_REQUESTED,
                DagNodeRunState.CANCEL_REQUESTED,
            },
            StateClass.PAUSED: {DagNodeRunState.PAUSED},
            StateClass.TERMINAL: {
                DagNodeRunState.ACCEPTED,
                DagNodeRunState.CANCELLED,
            },
            StateClass.OPERATOR_WAIT: {DagNodeRunState.TRIAGE_REQUIRED},
        },
    )
    standalone_classes = _state_class_map(
        StandaloneReviewState,
        {
            StateClass.WORKER_LIVENESS: {
                StandaloneReviewState.RECEIVED,
                StandaloneReviewState.REVIEW_QUEUED,
                StandaloneReviewState.REVIEWING,
                StandaloneReviewState.REPORT_READY,
                StandaloneReviewState.PAUSE_REQUESTED,
                StandaloneReviewState.CANCEL_REQUESTED,
            },
            StateClass.PAUSED: {StandaloneReviewState.PAUSED},
            StateClass.TERMINAL: {
                StandaloneReviewState.CANCELLED,
                StandaloneReviewState.COMPLETED,
            },
            StateClass.OPERATOR_WAIT: {StandaloneReviewState.TRIAGE_REQUIRED},
        },
    )
    architect_activations = {
        _activation(OrchestrationRole.ARCHITECT, RoleMode.AUTHOR),
        _activation(OrchestrationRole.ARCHITECT, RoleMode.REVISION),
    }
    architecture_review = {
        _activation(OrchestrationRole.REVIEWER, RoleMode.ARCHITECTURE)
    }
    architecture_runtime = _runtime_state_map(
        (
            {ArchitectureRevisionState.ARCHITECT_QUEUED},
            architect_activations,
            ReconciliationKind.ADMIT_ROLE,
        ),
        (
            {ArchitectureRevisionState.ARCHITECT_RUNNING},
            architect_activations,
            ReconciliationKind.RESUME_ROLE,
        ),
        (
            {
                ArchitectureRevisionState.ARCHITECT_QUIESCING,
                ArchitectureRevisionState.ARCHITECT_SNAPSHOTTING,
            },
            architect_activations,
            ReconciliationKind.RECONCILE_STATE,
        ),
        (
            {ArchitectureRevisionState.REVIEW_QUEUED},
            architecture_review,
            ReconciliationKind.ADMIT_ROLE,
        ),
        (
            {ArchitectureRevisionState.REVIEWING},
            architecture_review,
            ReconciliationKind.RESUME_ROLE,
        ),
        (
            {
                ArchitectureRevisionState.PAUSE_REQUESTED,
                ArchitectureRevisionState.CANCEL_REQUESTED,
            },
            architect_activations | architecture_review,
            ReconciliationKind.CONTROL_ROLE,
        ),
    )
    implementation_produce = {
        _activation(OrchestrationRole.IMPLEMENTATION, RoleMode.PRODUCE)
    }
    implementation_repair = {
        _activation(OrchestrationRole.IMPLEMENTATION, RoleMode.REPAIR)
    }
    verifier_module = {_activation(OrchestrationRole.VERIFIER, RoleMode.MODULE)}
    verifier_system = {_activation(OrchestrationRole.VERIFIER, RoleMode.SYSTEM)}
    all_node_activations = (
        implementation_produce
        | implementation_repair
        | verifier_module
        | verifier_system
    )
    node_runtime = _runtime_state_map(
        (
            {DagNodeRunState.QUEUED},
            implementation_produce,
            ReconciliationKind.ADMIT_ROLE,
        ),
        (
            {DagNodeRunState.PRODUCING},
            implementation_produce,
            ReconciliationKind.RESUME_ROLE,
        ),
        (
            {DagNodeRunState.QUIESCING, DagNodeRunState.SNAPSHOTTING},
            implementation_produce,
            ReconciliationKind.RECONCILE_STATE,
        ),
        (
            {DagNodeRunState.REPAIR_QUEUED},
            implementation_repair,
            ReconciliationKind.ADMIT_ROLE,
        ),
        (
            {DagNodeRunState.REPAIRING},
            implementation_repair,
            ReconciliationKind.RESUME_ROLE,
        ),
        (
            {DagNodeRunState.REVIEW_QUEUED},
            verifier_module,
            ReconciliationKind.ADMIT_ROLE,
        ),
        (
            {DagNodeRunState.REVIEWING},
            verifier_module,
            ReconciliationKind.RESUME_ROLE,
        ),
        (
            {DagNodeRunState.REVIEW_QUIESCING, DagNodeRunState.REVIEW_SNAPSHOTTING},
            verifier_module,
            ReconciliationKind.RECONCILE_STATE,
        ),
        (
            {DagNodeRunState.VERIFY_PREPARING},
            verifier_system,
            ReconciliationKind.ADMIT_ROLE,
        ),
        (
            {DagNodeRunState.VERIFYING},
            verifier_system,
            ReconciliationKind.RESUME_ROLE,
        ),
        (
            {DagNodeRunState.VERIFY_QUIESCING, DagNodeRunState.VERIFY_SNAPSHOTTING},
            verifier_system,
            ReconciliationKind.RECONCILE_STATE,
        ),
        (
            {DagNodeRunState.PAUSE_REQUESTED, DagNodeRunState.CANCEL_REQUESTED},
            all_node_activations,
            ReconciliationKind.CONTROL_ROLE,
        ),
    )
    standalone_review = {
        _activation(OrchestrationRole.REVIEWER, RoleMode.STANDALONE)
    }
    standalone_runtime = _runtime_state_map(
        (
            {StandaloneReviewState.RECEIVED, StandaloneReviewState.REVIEW_QUEUED},
            standalone_review,
            ReconciliationKind.ADMIT_ROLE,
        ),
        (
            {StandaloneReviewState.REVIEWING},
            standalone_review,
            ReconciliationKind.RESUME_ROLE,
        ),
        (
            {StandaloneReviewState.REPORT_READY},
            standalone_review,
            ReconciliationKind.RECONCILE_STATE,
        ),
        (
            {
                StandaloneReviewState.PAUSE_REQUESTED,
                StandaloneReviewState.CANCEL_REQUESTED,
            },
            standalone_review,
            ReconciliationKind.CONTROL_ROLE,
        ),
    )

    return (
        MachineSpec(AggregateType.TASK, task_classes, tuple(_task_transitions())),
        MachineSpec(
            AggregateType.WORKFLOW,
            workflow_classes,
            tuple(_workflow_transitions()),
            {
                ControlIntent.PAUSE: ControlPolicy(
                    "REQUEST_PAUSE",
                    frozenset(
                        {
                            WorkflowState.PAUSED,
                            WorkflowState.COMPLETED,
                            WorkflowState.REJECTED,
                            WorkflowState.CANCELLED,
                            WorkflowState.TRIAGE_REQUIRED,
                        }
                    ),
                ),
                ControlIntent.CANCEL: ControlPolicy(
                    "REQUEST_CANCEL",
                    frozenset(
                        {
                            WorkflowState.COMPLETED,
                            WorkflowState.REJECTED,
                            WorkflowState.CANCELLED,
                        }
                    ),
                ),
            },
        ),
        MachineSpec(
            AggregateType.ARCHITECTURE_REVISION,
            architecture_classes,
            tuple(_architecture_transitions()),
            {
                ControlIntent.PAUSE: ControlPolicy(
                    "REQUEST_PAUSE",
                    frozenset(
                        {
                            ArchitectureRevisionState.PAUSED,
                            ArchitectureRevisionState.ACCEPTED,
                            ArchitectureRevisionState.REJECTED,
                            ArchitectureRevisionState.SUPERSEDED,
                            ArchitectureRevisionState.CANCELLED,
                            ArchitectureRevisionState.TRIAGE_REQUIRED,
                        }
                    ),
                ),
                ControlIntent.CANCEL: ControlPolicy(
                    "REQUEST_CANCEL",
                    frozenset(
                        {
                            ArchitectureRevisionState.ACCEPTED,
                            ArchitectureRevisionState.REJECTED,
                            ArchitectureRevisionState.SUPERSEDED,
                            ArchitectureRevisionState.CANCELLED,
                        }
                    ),
                ),
            },
            runtime_states=architecture_runtime,
        ),
        MachineSpec(
            AggregateType.EXECUTION_EPOCH,
            execution_classes,
            tuple(_execution_transitions()),
            {
                ControlIntent.PAUSE: ControlPolicy(
                    "REQUEST_PAUSE",
                    frozenset(
                        {
                            ExecutionEpochState.PAUSED,
                            ExecutionEpochState.SUPERSEDED,
                            ExecutionEpochState.COMPLETED,
                            ExecutionEpochState.CANCELLED,
                            ExecutionEpochState.TRIAGE_REQUIRED,
                        }
                    ),
                ),
                ControlIntent.CANCEL: ControlPolicy(
                    "REQUEST_CANCEL",
                    frozenset(
                        {
                            ExecutionEpochState.SUPERSEDED,
                            ExecutionEpochState.COMPLETED,
                            ExecutionEpochState.CANCELLED,
                        }
                    ),
                ),
            },
        ),
        MachineSpec(
            AggregateType.DAG_NODE_RUN,
            node_classes,
            tuple(_node_transitions()),
            {
                ControlIntent.PAUSE: ControlPolicy(
                    "REQUEST_PAUSE",
                    frozenset(
                        {
                            DagNodeRunState.PAUSED,
                            DagNodeRunState.ACCEPTED,
                            DagNodeRunState.STALE,
                            DagNodeRunState.CANCELLED,
                            DagNodeRunState.TRIAGE_REQUIRED,
                        }
                    ),
                ),
                ControlIntent.CANCEL: ControlPolicy(
                    "REQUEST_CANCEL",
                    frozenset(
                        {
                            DagNodeRunState.ACCEPTED,
                            DagNodeRunState.CANCELLED,
                        }
                    ),
                ),
            },
            runtime_states=node_runtime,
        ),
        MachineSpec(
            AggregateType.STANDALONE_REVIEW,
            standalone_classes,
            tuple(_standalone_review_transitions()),
            {
                ControlIntent.PAUSE: ControlPolicy(
                    "REQUEST_PAUSE",
                    frozenset(
                        {
                            StandaloneReviewState.PAUSED,
                            StandaloneReviewState.CANCELLED,
                            StandaloneReviewState.COMPLETED,
                            StandaloneReviewState.TRIAGE_REQUIRED,
                        }
                    ),
                ),
                ControlIntent.CANCEL: ControlPolicy(
                    "REQUEST_CANCEL",
                    frozenset(
                        {
                            StandaloneReviewState.CANCELLED,
                            StandaloneReviewState.COMPLETED,
                        }
                    ),
                ),
            },
            runtime_states=standalone_runtime,
        ),
    )


def machine_spec_for(aggregate_type: AggregateType) -> MachineSpec:
    return next(
        spec for spec in all_machine_specs() if spec.aggregate_type == aggregate_type
    )


def all_transition_specs() -> tuple[TransitionSpec, ...]:
    return tuple(
        transition
        for machine in all_machine_specs()
        for transition in machine.transitions
    )


# Recovery and formal verification consume the same classification as the
# runtime transition engine. This is deliberately derived after every machine
# has been constructed so a new state cannot be added to only one surface.
LIVENESS_REQUIRED_STATES: Mapping[AggregateType, frozenset[str]] = {
    machine.aggregate_type: frozenset(
        state
        for state, state_class in machine.state_classes.items()
        if state_class == StateClass.WORKER_LIVENESS
    )
    for machine in all_machine_specs()
    if StateClass.WORKER_LIVENESS in set(machine.state_classes.values())
}


def build_default_transition_engine() -> TransitionEngine:
    return TransitionEngine(all_transition_specs())
