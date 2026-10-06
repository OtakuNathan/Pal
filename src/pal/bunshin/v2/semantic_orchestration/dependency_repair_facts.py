"""Immutable incarnation capture for the dependency-repair state machine."""
from __future__ import annotations

from typing import Any, Mapping

from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.contracts import AggregateSnapshot, AggregateType, SubmissionInvariantError
from pal.bunshin.v2.dependency_repair_protocol import RepairIncarnation
from pal.bunshin.v2.graph_executor import GraphExecution
from pal.bunshin.v2.repository import BunshinV2Repository


def node_name(node: AggregateSnapshot) -> str:
    return str(node.payload.get("module_name") or node.payload.get("unit_id") or "")


def current_attempt_id(role: Mapping[str, Any]) -> str:
    # retry_queued retains the previous failed attempt pointer; that pointer
    # does not own a recovered task still preparing its next claim.
    return str(role.get("active_attempt_id") or "") if role.get("state") in {
        "claimed", "running", "result_recorded", "settled",
    } else ""


def role_for_incarnation(repository: BunshinV2Repository, incarnation: RepairIncarnation) -> dict[str, Any] | None:
    if incarnation.role_assignment_id:
        return repository.role_assignments.read_role_assignment(incarnation.role_assignment_id)
    node = repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, incarnation.aggregate_id)
    if node is None:
        raise SubmissionInvariantError("dependency repair aggregate disappeared")
    matches = [row for row in repository.role_assignments.list_role_assignments(workflow_id=node.workflow_id)
               if str(row.get("aggregate_id") or "") == incarnation.aggregate_id
               and str(dict(row.get("execution_spec") or {}).get("effect_key") or "") == incarnation.effect_key]
    open_rows = [row for row in matches if row.get("state") not in {"cancelled", "settled", "failed"}]
    if len(open_rows) > 1:
        raise SubmissionInvariantError("dependency repair found competing role assignments for one incarnation")
    return open_rows[0] if open_rows else (matches[-1] if matches else None)


def freeze_incarnation(repository: BunshinV2Repository, artifacts: ContentAddressedArtifactStore,
                       execution: GraphExecution, node: AggregateSnapshot) -> tuple[RepairIncarnation, ArtifactRef]:
    cycle = execution.cycles[node_name(node)]
    assignment = cycle.active_assignment
    if assignment is None:
        raise SubmissionInvariantError("dependency repair cannot infer an admitted incarnation without its graph assignment")
    pending_ref = dict(node.payload.get("pending_verification_ref") or {})
    pending = (dict(artifacts.read_json(pending_ref)) if pending_ref.get("sha256")
               and node.state in {"REVIEW_QUIESCING", "REVIEW_SNAPSHOTTING"} else {})
    role_id = str(pending.get("role_assignment_id") or "")
    role = repository.role_assignments.read_role_assignment(role_id) if role_id else None
    effect = repository.queries.read_admitted_role_effect(node.aggregate_id, assignment.input_fingerprint)
    if role is None and effect:
        matches = [row for row in repository.role_assignments.list_role_assignments(workflow_id=node.workflow_id)
                   if str(row.get("aggregate_id") or "") == node.aggregate_id
                   and str(dict(row.get("execution_spec") or {}).get("effect_key") or "") == effect["effect_key"]]
        role = matches[-1] if matches else None
    spec = dict((role or {}).get("execution_spec") or {})
    effect_key = str(spec.get("effect_key") or effect.get("effect_key") or "")
    effect_id = str(spec.get("effect_id") or effect.get("effect_id") or effect_key)
    causal = dict(dict(effect.get("payload") or {}).get("_causal_context") or {})
    attempt_id = current_attempt_id(role or {})
    attempt_binding = repository.role_attempts.read_role_attempt_business_lease(attempt_id) if attempt_id else None
    business = dict((attempt_binding or {}).get("business_lease") or {})
    submitted = (str(pending.get("lease_resource_key") or ""), str(pending.get("invocation_id") or ""), int(pending.get("fencing_token") or 0))
    current = (str(node.payload.get("lease_resource_key") or ""), str(node.payload.get("active_worker_id") or ""), int(node.payload.get("fencing_token") or 0))
    actual = (str(business.get("resource_key") or ""), str(business.get("owner_id") or ""), int(business.get("fencing_token") or 0))
    if business and all(submitted) and actual != submitted:
        raise SubmissionInvariantError("pending submission disagrees with its actual admitted attempt lease")
    # Actual claim evidence governs recovered attempts. A Pending freezes the
    # submitted lease; only the later snapshot tuple may differ. Before claim,
    # the transaction-fenced current aggregate owns setup, not an old attempt.
    resource, owner, token = actual if business else submitted if all(submitted) else current
    if not effect_key or not resource or not owner or not token:
        raise SubmissionInvariantError("dependency repair has no exact task and business-fence binding")
    rebound = bool(all(current) and current != (resource, owner, token))
    incarnation = RepairIncarnation(
        node_name=node_name(node), cycle_id=cycle.cycle_id, generation=cycle.generation,
        input_fingerprint=assignment.input_fingerprint, slot=assignment.slot.value,
        aggregate_id=node.aggregate_id, effect_id=effect_id, effect_key=effect_key,
        role_assignment_id=str((role or {}).get("assignment_id") or ""),
        attempt_id=attempt_id,
        lease_resource=resource, lease_owner=owner, fencing_token=token,
        snapshot_lease_resource=current[0] if rebound else "",
        snapshot_lease_owner=current[1] if rebound else "",
        snapshot_fencing_token=current[2] if rebound else 0,
    )
    ref = artifacts.put_json({
        "schema_version": "1", "incarnation": incarnation.to_dict(),
        "workflow_id": node.workflow_id, "aggregate_id": node.aggregate_id,
        "state": node.state, "version": node.version, "payload": dict(node.payload),
        "pending_verification_ref": pending_ref if pending else {},
        "attempt_business_lease_ref": dict((attempt_binding or {}).get("artifact_ref") or {}),
    }, artifact_type="DependencyRepairIncarnationArtifact", child_refs=(
        ((str(attempt_binding["artifact_ref"]["sha256"]), "actual_attempt_business_lease"),)
        if attempt_binding else ()
    ))
    return incarnation, ref


def frozen_node(artifacts: ContentAddressedArtifactStore, reference: Mapping[str, Any]) -> AggregateSnapshot:
    frozen = dict(artifacts.read_json(reference))
    return AggregateSnapshot(aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=str(frozen["aggregate_id"]),
                             workflow_id=str(frozen["workflow_id"]), state=str(frozen["state"]),
                             version=int(frozen["version"]), payload=dict(frozen["payload"]), created_at="", updated_at="")


def append_refs(existing: Any, *references: Mapping[str, Any]) -> list[dict[str, Any]]:
    result = {str(ref["sha256"]): dict(ref) for ref in list(existing or []) if isinstance(ref, Mapping) and ref.get("sha256")}
    for ref in references:
        if ref.get("sha256"):
            result[str(ref["sha256"])] = dict(ref)
    return list(result.values())
