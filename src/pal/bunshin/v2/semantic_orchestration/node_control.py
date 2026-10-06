from __future__ import annotations
from pal.bunshin.v2.semantic_orchestration.workspace_safety import _raise_if_workspace_held
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, TYPE_CHECKING

if TYPE_CHECKING:
    from pal.bunshin.v2.semantic_orchestration.dependency_repair_runtime import DependencyRepairRuntime
from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.contracts import ActionEnvelope, AggregateType, SubmissionInvariantError
from pal.bunshin.v2.workspace_resources import WorkspaceLockRegistry
from pal.bunshin.v2.workflow_runtime import WorkflowCoordinator
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.v2.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.v2.semantic_orchestration.implementation_snapshot import ImplementationSnapshot
from pal.bunshin.v2.semantic_orchestration.node_admission import NodeAdmission
from pal.bunshin.v2.semantic_orchestration.role_cleanup import RoleCleanup
from pal.bunshin.v2.semantic_orchestration.verification_snapshot import VerificationSnapshot


@dataclass
class NodeControl:
    effect_reads: EffectReads
    implementation_snapshot: ImplementationSnapshot
    node_admission: NodeAdmission
    role_cleanup: RoleCleanup
    verification_snapshot: VerificationSnapshot
    artifacts: ContentAddressedArtifactStore
    repository: BunshinV2Repository
    workspace_locks: WorkspaceLockRegistry
    dependency_repairs: DependencyRepairRuntime | None = None

    async def stop_node_worker(
        self,
        effect: Mapping[str, Any],
        *,
        cancel: bool,
        confirm: bool = True,
    ) -> Mapping[str, Any]:
        node = self.effect_reads.effect_snapshot(effect)
        if self.dependency_repairs is not None and self.dependency_repairs.owns(node):
            if node.state in {"PAUSE_REQUESTED", "PAUSED", "TRIAGE_REQUIRED"} or self.dependency_repairs._control_blocked(
                node.workflow_id, self.dependency_repairs.execution(node.workflow_id)
            ):
                return await self.dependency_repairs.control_cleanup(node, confirm=confirm)
            return await self.dependency_repairs.reconcile(effect)
        invocation_id = str(node.payload.get("active_worker_id") or "")
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        await self.role_cleanup.close_owned_process(
            invocation_id,
        )
        worktree_text = str(node.payload.get("workspace_path") or "")
        if worktree_text:
            workspace = Path(worktree_text)
            await self.role_cleanup.release_managed_lsp_workspace(workspace)
            _raise_if_workspace_held(workspace, "node worker still holds its worktree")
        pending_verification_value = dict(
            node.payload.get("pending_verification_ref") or {}
        )
        if pending_verification_value.get("sha256"):
            pending_verification = dict(
                self.artifacts.read_json(pending_verification_value)
            )
            review_workspace_text = str(
                pending_verification.get("review_workspace") or ""
            )
            if review_workspace_text:
                review_workspace = Path(review_workspace_text)
                await self.role_cleanup.release_managed_lsp_workspace(review_workspace)
                _raise_if_workspace_held(
                    review_workspace,
                    "node verifier still holds its canonical Module worktree",
                )
        self.workspace_locks.release(node.aggregate_id)
        self.workspace_locks.release(f"verification:{node.aggregate_id}")
        current = self.repository.snapshots.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            node.aggregate_id,
        )
        if current is None:
            raise SubmissionInvariantError("controlled node disappeared before worker retirement")
        cancel_target = str(current.payload.get("cancel_target") or "CANCELLED")
        terminal_cancel = bool(cancel and cancel_target == "CANCELLED")
        self.repository.role_cancellation.cancel_role_assignments(
            workflow_id=node.workflow_id,
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id=node.aggregate_id,
            reason=(
                "node cancelled"
                if terminal_cancel
                else "node frozen for stale dependency"
                if cancel
                else "node paused"
            ),
        )
        if confirm:
            action_type = "CANCEL_CONFIRMED" if cancel or current.state == "CANCEL_REQUESTED" else "PAUSE_CONFIRMED"
            with self.repository.transaction() as connection:
                (connection or self.repository).transitions.dispatch(
                    ActionEnvelope(
                        action_type=action_type,
                        workflow_id=node.workflow_id,
                        aggregate_type=AggregateType.DAG_NODE_RUN,
                        aggregate_id=node.aggregate_id,
                        actor="bunshin-v2-manager",
                        expected_version=current.version,
                        idempotency_key=f"effect:{effect['effect_key']}:stopped",
                    ),
                )
                WorkflowCoordinator(self.repository).confirm_node_control(
                    workflow_id=node.workflow_id,
                    node_name=str(
                        node.payload.get("module_name")
                        or node.payload.get("unit_id")
                        or ""
                    ),
                    cancel=action_type == "CANCEL_CONFIRMED",
                    unit_of_work=connection,
                )
        fencing_token = int(node.payload.get("fencing_token") or 0)
        if lease_resource and invocation_id and fencing_token:
            try:
                self.repository.leases.release_lease(lease_resource, invocation_id, fencing_token)
            except Exception:
                pass
        return {}

    async def reconcile_node(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self.effect_reads.effect_snapshot(effect)
        if self.dependency_repairs is not None and self.dependency_repairs.owns(node):
            return await self.dependency_repairs.reconcile(effect)
        if node.state == "PAUSE_REQUESTED":
            return await self.stop_node_worker(effect, cancel=False)
        if node.state == "CANCEL_REQUESTED":
            return await self.stop_node_worker(effect, cancel=True)
        return await self.resume_node(effect)

    async def resume_node(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self.effect_reads.effect_snapshot(effect)
        if self.dependency_repairs is not None and self.dependency_repairs.owns(node):
            return await self.dependency_repairs.reconcile(effect)
        if node.state == "QUEUED":
            return self.node_admission.admit_node_worker(
                effect,
                action_type="START_PRODUCING",
                activation=RoleActivation(
                    OrchestrationRole.IMPLEMENTATION,
                    RoleMode.PRODUCE,
                ),
            )
        if node.state == "REVIEW_QUEUED":
            return self.node_admission.admit_node_worker(
                effect,
                action_type="START_REVIEW",
                activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
            )
        if node.state == "REPAIR_QUEUED":
            return self.node_admission.admit_node_worker(
                effect,
                action_type="START_REPAIR",
                activation=RoleActivation(
                    OrchestrationRole.IMPLEMENTATION,
                    RoleMode.REPAIR,
                ),
            )
        if node.state == "QUIESCING":
            return await self.implementation_snapshot.quiesce_node(effect)
        if node.state == "SNAPSHOTTING":
            return await self.implementation_snapshot.snapshot_implementation_result(effect)
        if node.state == "REVIEW_QUIESCING":
            return await self.verification_snapshot.quiesce_verifier_role(effect)
        if node.state == "REVIEW_SNAPSHOTTING":
            return self.verification_snapshot.snapshot_semantic_verification(effect)
        return {}
