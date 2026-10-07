"""Pause/triage cleanup preserves a pending cohort without applying it."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, SubmissionInvariantError
from pal.bunshin.graph_executor import GraphExecution
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.semantic_orchestration.dependency_repair_facts import current_attempt_id, frozen_node, role_for_incarnation
from pal.bunshin.semantic_orchestration.role_cleanup import RoleCleanup
from pal.bunshin.semantic_orchestration.role_leases import RoleLeases
from pal.bunshin.semantic_orchestration.workspace_safety import _raise_if_workspace_held
from pal.bunshin.workspace_resources import WorkspaceLockRegistry


async def retire_repair_control(*, node: AggregateSnapshot, execution: GraphExecution,
        repository: BunshinV2Repository, artifacts: ContentAddressedArtifactStore,
        cleanup: RoleCleanup, leases: RoleLeases, workspace_locks: WorkspaceLockRegistry,
        confirm: bool) -> None:
    pending = execution.dependency_repairs.pending
    if pending is None:
        raise SubmissionInvariantError("pending repair control lost its graph authority")
    members = [item for item in pending.frontier.values() if item.aggregate_id == node.aggregate_id]
    if len(members) != 1:
        raise SubmissionInvariantError("pending repair control requires one exact frozen incarnation")
    incarnation = members[0]
    frozen_ref = dict(node.payload.get("dependency_repair_incarnation_ref") or {})
    original = frozen_node(artifacts, frozen_ref)
    if original.aggregate_id != node.aggregate_id:
        raise SubmissionInvariantError("pending repair control changed its original aggregate")
    role = role_for_incarnation(repository, incarnation)
    if role:
        incarnation = incarnation.bind(replace(incarnation, role_assignment_id=str(role["assignment_id"]),
                                                attempt_id=incarnation.attempt_id or current_attempt_id(role)))
    assignment = await cleanup.retire_incarnation(effect_key=incarnation.effect_key,
        invocation_id=incarnation.lease_owner, lease_resource_key=incarnation.lease_resource,
        fencing_token=incarnation.fencing_token, assignment_id=incarnation.role_assignment_id,
        attempt_id=incarnation.attempt_id)
    role = role_for_incarnation(repository, incarnation)
    if role:
        assignment = str(role["assignment_id"])
    repository.role_cancellation.cancel_role_assignments(workflow_id=node.workflow_id,
        aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id,
        assignment_ids=(assignment,) if assignment else (), reason="pending repair paused for explicit control")
    workspace = Path(str(original.payload.get("workspace_path") or ""))
    await cleanup.release_managed_lsp_workspace(workspace)
    workspace_locks.release(node.aggregate_id)
    workspace_locks.release(f"verification:{node.aggregate_id}")
    _raise_if_workspace_held(workspace, "pending repair control still has a workspace holder")
    leases.release_business_lease(resource_key=incarnation.lease_resource,
        owner_id=incarnation.lease_owner, fencing_token=incarnation.fencing_token)
    if incarnation.snapshot_fencing_token:
        leases.release_business_lease(resource_key=incarnation.snapshot_lease_resource,
            owner_id=incarnation.snapshot_lease_owner, fencing_token=incarnation.snapshot_fencing_token)
    # No captured report or graph closure is asserted here. The preserved
    # workspace and any settled raw receipt are captured after explicit resume.
    if confirm:
        current = repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
        if current is None:
            raise SubmissionInvariantError("pending repair control aggregate disappeared")
        action = "PAUSE_CONFIRMED" if current.state == "PAUSE_REQUESTED" else (
            "CANCEL_CONFIRMED" if current.state == "CANCEL_REQUESTED" and current.payload.get("cancel_target") == "STALE" else "")
        if action:
            repository.transitions.dispatch(ActionEnvelope(action_type=action, workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id,
                actor="bunshin-v2-manager", expected_version=current.version,
                idempotency_key=f"dependency-repair:control:{incarnation.key}:{current.version}:{action}"))
