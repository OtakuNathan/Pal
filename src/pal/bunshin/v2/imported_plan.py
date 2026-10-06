"""Bind a reviewed external plan without inventing a producer assignment."""
from __future__ import annotations

from typing import Any, Mapping

from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.contract_protocol import CONTRACT_ARTIFACT
from pal.bunshin.v2.contracts import AggregateSnapshot, AggregateType, SubmissionInvariantError
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.storage.queries import QueriesStore
from pal.bunshin.v2.unit_of_work import BunshinUnitOfWork
from pal.bunshin.v2.workflow_runtime import WorkflowCoordinator


def bind_imported_plan_product(*, repository: BunshinV2Repository,
        artifacts: ContentAddressedArtifactStore, revision: AggregateSnapshot,
        unit_of_work: BunshinUnitOfWork) -> bool:
    """Repair only the exact initial import's empty logical plan cursor.

    This is also the legacy recovery guard. Normal authored products never
    obtain import authority merely by reaching REVIEW_QUEUED. The import event,
    request, current revision pointer and product all have to agree. The imported
    artifact and any foreign GraphIR embedded in it remain immutable/uninstalled.
    """
    queries = QueriesStore(unit_of_work.transitions.database, unit_of_work.transitions.projections)
    imported = queries.read_architecture_import_ref(revision.aggregate_id)
    if not imported:
        return False
    current = unit_of_work.snapshots.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
    if current is None or current.workflow_id != revision.workflow_id or current.version != revision.version:
        raise SubmissionInvariantError("plan import no longer owns the current revision")
    product = dict(current.payload.get("architecture_manifest_ref") or {})
    if imported != product:
        # An imported revision can later be repaired by a real Architect. Its
        # normal producer submission, never the old import, then owns review.
        cycle = unit_of_work.cycles.read_plan_cycle(workflow_id=revision.workflow_id)
        if (imported.get("sha256") != product.get("sha256") and cycle is not None
                and cycle.product_ref == str(product.get("sha256") or "")):
            return False
        raise SubmissionInvariantError("imported plan product differs from its immutable creation event")
    if current.state not in {"REVIEW_QUEUED", "REVIEWING"} or int(current.payload.get("revision_number") or 1) != 1:
        raise SubmissionInvariantError("only the current initial imported revision can bind a plan product")
    workflow = unit_of_work.snapshots.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
    if workflow is None or str(workflow.payload.get("architecture_revision_id") or "") != current.aggregate_id:
        raise SubmissionInvariantError("imported revision is not the workflow's current architecture")

    def read(reference: Mapping[str, Any], expected_type: str = "") -> dict[str, Any]:
        record = unit_of_work.transitions.artifacts.read_artifact_record(str(reference.get("sha256") or ""))
        if record is None or not record.get("durable"):
            raise SubmissionInvariantError("plan import requires durable immutable evidence")
        ref = ArtifactRef.from_mapping(record)
        if ((expected_type and ref.artifact_type != expected_type)
                or any(reference.get(key, value) != value for key, value in ref.to_dict().items())):
            raise SubmissionInvariantError("plan import artifact metadata differs from its durable record")
        return dict(artifacts.read_json(ref))

    request = read(dict(workflow.payload.get("request_ref") or {}))
    artifact = read(imported, CONTRACT_ARTIFACT)
    requirements = dict(current.payload.get("requirements_ref") or {})
    if (request.get("operation") != "review_then_execute"
            or dict(request.get("input_artifact_ref") or {}) != imported
            or not requirements
            or dict(request.get("requirements_ref") or {}) != requirements
            or dict(artifact.get("requirements_ref") or {}) != requirements):
        raise SubmissionInvariantError("plan import request, product and pinned requirements disagree")
    read(requirements)
    WorkflowCoordinator(repository).import_plan_product(workflow_id=revision.workflow_id,
        product_ref=str(imported["sha256"]), unit_of_work=unit_of_work)
    return True
