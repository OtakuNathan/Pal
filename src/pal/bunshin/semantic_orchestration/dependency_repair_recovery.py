"""Validate immutable authority for resuming an interrupted repair cohort.

Manual recovery restores a logical cursor, never a new role admission. The
pending frontier, frozen aggregate and captured source report must all still
refer to the same original incarnation.
"""
from __future__ import annotations

from typing import Any, Mapping

from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import AggregateSnapshot, SubmissionInvariantError
from pal.bunshin.dependency_repair_protocol import RepairIncarnation
from pal.bunshin.graph_executor import GraphExecutionState
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.semantic_orchestration.dependency_repair_facts import node_name


def pending_repair_incarnation(
    repository: BunshinV2Repository,
    artifacts: ContentAddressedArtifactStore,
    node: AggregateSnapshot,
) -> RepairIncarnation | None:
    """Return the current exact member only after checking its saved evidence.

    Call inside the public control transaction. A missing capture is legitimate
    for a peer interrupted before collection; a registered report source always
    has its immutable capture and packet in the persisted ledger.
    """
    execution = repository.cycles.read_graph_execution(workflow_id=node.workflow_id)
    if execution is None:
        return None
    if execution.state in {GraphExecutionState.CANCELLED, GraphExecutionState.REPLAN_REQUIRED}:
        raise SubmissionInvariantError("terminal graph control dominates triage recovery")
    cohort = execution.dependency_repairs.pending
    if cohort is None:
        return None
    members = [item for item in cohort.frontier.values() if item.aggregate_id == node.aggregate_id]
    if not members:
        return None
    if len(members) != 1 or execution.state != GraphExecutionState.RUNNING:
        raise SubmissionInvariantError("dependency repair has no unique current recovery incarnation")
    member = members[0]
    if (dict(node.payload.get("blocker") or {}).get("kind") == "no_progress"
            and dict(node.payload.get("verification_artifact_ref") or {}).get("sha256")):
        raise SubmissionInvariantError("dependency repair no-progress verdict requires terminal resolution")
    cycle = execution.cycles[member.node_name]
    if (node_name(node) != member.node_name
            or int(node.payload.get("graph_generation") or 0) != member.generation
            or cycle.cycle_id != member.cycle_id or cycle.generation != member.generation):
        raise SubmissionInvariantError("dependency repair recovery belongs to another graph incarnation")

    def read(reference: Mapping[str, Any], kind: str = "") -> dict[str, Any]:
        record = repository.artifacts.read_artifact_record(str(reference.get("sha256") or ""))
        if record is None or not record.get("durable"):
            raise SubmissionInvariantError("dependency repair recovery has no durable evidence")
        ref = ArtifactRef.from_mapping(record)
        if (kind and ref.artifact_type != kind) or any(
            reference.get(key, value) != value for key, value in ref.to_dict().items()
        ):
            raise SubmissionInvariantError("dependency repair recovery evidence reference changed")
        return dict(artifacts.read_json(ref))

    frozen = read(dict(node.payload.get("dependency_repair_incarnation_ref") or {}),
                  "DependencyRepairIncarnationArtifact")
    original = RepairIncarnation.from_mapping(frozen.get("incarnation") or {})
    if (original.bind(member) != member
            or frozen.get("workflow_id") != node.workflow_id
            or frozen.get("aggregate_id") != node.aggregate_id):
        raise SubmissionInvariantError("dependency repair recovery lost its frozen aggregate binding")
    payload = dict(frozen.get("payload") or {})
    if (str(payload.get("module_name") or payload.get("unit_id") or "") != member.node_name
            or int(payload.get("graph_generation") or 0) != member.generation):
        raise SubmissionInvariantError("dependency repair frozen aggregate belongs to another graph")

    capture_ref = dict(node.payload.get("dependency_repair_capture_ref") or {})
    capture: dict[str, Any] = {}
    if capture_ref:
        if capture_ref not in [dict(ref) for ref in cohort.captures.get(member.key, ())]:
            raise SubmissionInvariantError("dependency repair recovery capture is not a persisted frontier receipt")
        capture = read(capture_ref, "DependencyRepairCaptureArtifact")
        if any(capture.get(key) != value for key, value in {
            "incarnation_key": member.key, "workflow_id": node.workflow_id,
            "node_run_id": node.aggregate_id, "node_name": member.node_name,
            "slot": member.slot, "source_assignment_id": member.role_assignment_id,
            "invocation_id": member.lease_owner, "lease_resource_key": member.lease_resource,
            "fencing_token": member.fencing_token,
        }.items()):
            raise SubmissionInvariantError("dependency repair recovery capture belongs to another incarnation")

    # A later cleanup error may replace the visible blocker. The immutable
    # verdict index still forbids replaying an already terminal receipt.
    for pending in (dict(frozen.get("pending_verification_ref") or {}),
                    dict(capture.get("source_pending_ref") or capture.get("pending_ref") or {})):
        if pending.get("sha256") and repository.queries.read_verification_settlement_ref(
            node.aggregate_id, str(pending["sha256"]),
        ):
            raise SubmissionInvariantError("dependency repair recovery already has a committed verdict")

    original_candidate = (dict(payload.get("candidate_ref") or {}), str(payload.get("candidate_digest") or ""))
    current_candidate = (dict(node.payload.get("candidate_ref") or {}), str(node.payload.get("candidate_digest") or ""))
    captured_candidate = (dict(capture.get("candidate_ref") or {}), str(capture.get("candidate_digest") or ""))
    if current_candidate != original_candidate and (not capture or current_candidate != captured_candidate):
        raise SubmissionInvariantError("dependency repair recovery candidate binding changed")
    if original_candidate[0]:
        read(original_candidate[0])
    if current_candidate[0] and current_candidate != original_candidate:
        read(current_candidate[0])
    if dict(node.payload.get("pending_verification_ref") or {}) != dict(payload.get("pending_verification_ref") or {}):
        raise SubmissionInvariantError("dependency repair recovery pending submission changed")
    if any(node.payload.get(key) != payload.get(key) for key in (
        "role_assignment_id", "role_submission_payload_hash", "verifier_evaluation_generation",
    )):
        raise SubmissionInvariantError("dependency repair recovery role receipt changed")

    for intent in cohort.intents.values():
        if intent.source_node != member.node_name:
            continue
        if (not capture or cycle.product_ref != intent.source_product_ref
                or dict(capture.get("source_candidate_ref") or {}) != dict(intent.candidate_ref)
                or capture.get("source_candidate_digest") != intent.candidate_digest
                or capture.get("source_assignment_id") != intent.source_assignment_id
                or capture.get("source_payload_hash") != intent.submission_payload_hash
                or dict(capture.get("repair_packet_ref") or {}) != dict(intent.packet_ref)
                or dict(capture.get("source_pending_ref") or capture.get("pending_ref") or {}) != dict(intent.pending_verification_ref)
                or dict(intent.packet_ref) not in [dict(ref) for ref in cohort.captures.get(member.key, ())]):
            raise SubmissionInvariantError("dependency repair recovery source receipt changed")
        read(intent.candidate_ref)
        read(intent.packet_ref)
        if intent.pending_verification_ref:
            pending = read(intent.pending_verification_ref)
            if (dict(pending.get("candidate_ref") or {}) != dict(intent.candidate_ref)
                    or pending.get("candidate_digest") != intent.candidate_digest
                    or pending.get("role_assignment_id") != intent.source_assignment_id
                    or pending.get("role_submission_payload_hash") != intent.submission_payload_hash):
                raise SubmissionInvariantError("dependency repair recovery pending receipt changed")
    return member
