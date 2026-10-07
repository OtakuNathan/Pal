from __future__ import annotations
from pal.bunshin.semantic_orchestration.workspace_safety import _raise_if_workspace_held
from pal.bunshin.semantic_orchestration.workspace_safety import _lease_is_live
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.sessions import architecture_reviewer_session_id, architect_session_id_for_revision
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.semantic_orchestration.architecture_review import ArchitectureReview
from pal.bunshin.semantic_orchestration.architecture_snapshot import ArchitectureSnapshot
from pal.bunshin.semantic_orchestration.architecture_stage import ArchitectureStage
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.human_review import HumanReview
from pal.bunshin.semantic_orchestration.node_admission import NodeAdmission
from pal.bunshin.semantic_orchestration.review_delivery import ReviewDelivery
from pal.bunshin.semantic_orchestration.role_cleanup import RoleCleanup


@dataclass
class AggregateControl:
    architecture_review: ArchitectureReview
    architecture_snapshot: ArchitectureSnapshot
    architecture_stage: ArchitectureStage
    effect_reads: EffectReads
    human_review: HumanReview
    node_admission: NodeAdmission
    review_delivery: ReviewDelivery
    role_cleanup: RoleCleanup
    repository: BunshinRepository

    async def stop_aggregate_worker(
        self,
        effect: Mapping[str, Any],
        *,
        cancel: bool,
        confirm: bool = True,
    ) -> Mapping[str, Any]:
        snapshot = self.effect_reads.effect_snapshot(effect)
        invocation_id = str(snapshot.payload.get("active_worker_id") or "")
        lease_resource = str(snapshot.payload.get("lease_resource_key") or "")
        await self.role_cleanup.close_owned_process(
            invocation_id,
        )
        architecture_workspace_text = str(snapshot.payload.get("architecture_workspace_path") or "")
        if architecture_workspace_text:
            architecture_workspace = Path(architecture_workspace_text)
            await self.role_cleanup.release_managed_lsp_workspace(architecture_workspace)
            _raise_if_workspace_held(
                architecture_workspace,
                "aggregate worker still holds the architecture worktree",
            )
        self.repository.role_cancellation.cancel_role_assignments(
            workflow_id=snapshot.workflow_id,
            aggregate_type=snapshot.aggregate_type,
            aggregate_id=snapshot.aggregate_id,
            reason="aggregate cancelled" if cancel else "aggregate paused",
        )
        if confirm:
            current = self.repository.snapshots.read_snapshot(snapshot.aggregate_type, snapshot.aggregate_id)
            action_type = "CANCEL_CONFIRMED" if cancel else "PAUSE_CONFIRMED"
            with self.repository.transaction() as connection:
                (connection or self.repository).transitions.dispatch(
                    ActionEnvelope(
                        action_type=action_type,
                        workflow_id=snapshot.workflow_id,
                        aggregate_type=snapshot.aggregate_type,
                        aggregate_id=snapshot.aggregate_id,
                        actor="bunshin-v2-manager",
                        expected_version=current.version,
                        idempotency_key=f"effect:{effect['effect_key']}:stopped",
                    ),
                )
                if snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
                    WorkflowCoordinator(self.repository).confirm_plan_control(
                        workflow_id=snapshot.workflow_id,
                        cancel=cancel,
                        unit_of_work=connection,
                    )
        fencing_token = int(snapshot.payload.get("fencing_token") or 0)
        if lease_resource and invocation_id and fencing_token:
            try:
                self.repository.leases.release_lease(lease_resource, invocation_id, fencing_token)
            except Exception:
                pass
        if cancel and snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
            for session_id in (
                architect_session_id_for_revision(
                    snapshot.workflow_id,
                    snapshot.aggregate_id,
                    snapshot.payload,
                ),
                architecture_reviewer_session_id(
                    snapshot.workflow_id,
                    snapshot.aggregate_id,
                    snapshot.payload,
                ),
            ):
                self.repository.role_sessions.complete_role_session(
                    session_id,
                    status="cancelled",
                )
        elif cancel and snapshot.aggregate_type == AggregateType.WORKFLOW:
            self.repository.role_maintenance.complete_workflow_role_sessions(
                snapshot.workflow_id,
                status="cancelled",
            )
        return {}

    async def resume_aggregate(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        snapshot = self.effect_reads.effect_snapshot(effect)
        if snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
            if snapshot.state == "PAUSE_REQUESTED":
                return await self.stop_aggregate_worker(effect, cancel=False)
            if snapshot.state == "CANCEL_REQUESTED":
                return await self.stop_aggregate_worker(effect, cancel=True)
            stage_by_state = {
                "ARCHITECT_QUEUED": "architect",
                "ARCHITECT_RUNNING": "architect",
            }
            stage = stage_by_state.get(snapshot.state)
            if stage:
                if snapshot.state.endswith("_RUNNING"):
                    lease_resource = f"architecture:{snapshot.aggregate_id}:{stage}"
                    lease = self.repository.leases.read_lease(lease_resource)
                    if lease and str(lease.get("owner_id") or "") and _lease_is_live(lease):
                        return {"status": "already_running", "active_worker_id": str(lease["owner_id"])}
                resumed_effect = {**dict(effect), "payload": {**dict(effect.get("payload") or {}), "stage": stage}}
                return await self.architecture_stage.run_architecture_stage(resumed_effect)
            if snapshot.state in {"REVIEW_QUEUED", "REVIEWING"}:
                return await self.architecture_review.run_architecture_review(effect)
            if snapshot.state == "ARCHITECT_QUIESCING":
                return await self.architecture_snapshot.quiesce_architect_role(effect)
            if snapshot.state == "ARCHITECT_SNAPSHOTTING":
                return await self.architecture_snapshot.snapshot_architect_result(effect)
            if snapshot.state == "HUMAN_REVIEW":
                return await self.human_review.publish_human_architecture_review(effect)
        if snapshot.aggregate_type == AggregateType.STANDALONE_REVIEW:
            if snapshot.state == "RECEIVED":
                self.repository.transitions.dispatch(
                    ActionEnvelope(
                        action_type="QUEUE_REVIEW",
                        workflow_id=snapshot.workflow_id,
                        aggregate_type=AggregateType.STANDALONE_REVIEW,
                        aggregate_id=snapshot.aggregate_id,
                        actor="bunshin-v2-recovery",
                        expected_version=snapshot.version,
                        idempotency_key=f"effect:{effect['effect_key']}:queue-review",
                    )
                )
                return {}
            if snapshot.state == "PAUSE_REQUESTED":
                return await self.stop_aggregate_worker(effect, cancel=False)
            if snapshot.state == "CANCEL_REQUESTED":
                return await self.stop_aggregate_worker(effect, cancel=True)
            if snapshot.state == "REVIEW_QUEUED":
                return self.node_admission.admit_standalone_review(effect)
            if snapshot.state == "REPORT_READY":
                return await self.review_delivery.publish_standalone_report(effect)
        return {}
