"""Settle a missing plan checker without a fictitious review verdict."""
from __future__ import annotations
from typing import Any, Mapping
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.workflow_runtime import WorkflowCoordinator


def skip_plan_review(repository: BunshinRepository, artifacts: ContentAddressedArtifactStore,
                     revision: AggregateSnapshot, manifest_ref: ArtifactRef) -> Mapping[str, Any]:
    from pal.bunshin.cycle_protocol import CycleAction
    from pal.bunshin.imported_plan import bind_imported_plan_product
    receipt = artifacts.put_json({"verdict": "not_required", "findings": [],
                                      "architecture_manifest_ref": manifest_ref.to_dict()},
                                     artifact_type="SkippedPlanReviewArtifact",
                                     child_refs=((manifest_ref.sha256, "plan"),))
    with repository.transaction() as connection:
        bind_imported_plan_product(repository=repository, artifacts=artifacts,
                                   revision=revision, unit_of_work=connection)
        coordinator = WorkflowCoordinator(repository)
        cycle = coordinator.ensure_plan_cycle(workflow_id=revision.workflow_id, unit_of_work=connection)
        if cycle.product_ref != manifest_ref.sha256:
            raise ValueError("plan review skip must bind the current plan")
        coordinator.transition_plan(workflow_id=revision.workflow_id, action=CycleAction.SKIP_CHECKER, unit_of_work=connection)
        coordinator.transition_plan(workflow_id=revision.workflow_id, action=CycleAction.REQUEST_HUMAN_REVIEW, unit_of_work=connection)
        connection.transitions.dispatch(ActionEnvelope(
            action_type="SKIP_PLAN_CHECKER", workflow_id=revision.workflow_id,
            aggregate_type=AggregateType.ARCHITECTURE_REVISION, aggregate_id=revision.aggregate_id,
            expected_version=revision.version, actor="bunshin-manager",
            idempotency_key=f"skip-plan-review:{revision.aggregate_id}:{manifest_ref.sha256}",
            payload={"review_artifact_ref": receipt.to_dict(), "architecture_manifest_ref": manifest_ref.to_dict()},
        ))
    return {"status": "review_not_required", "result_artifact_ref": receipt.to_dict()}
