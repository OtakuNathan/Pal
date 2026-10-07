from __future__ import annotations
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
from pal.bunshin.semantic_orchestration.review_results import _append_ref
from pal.bunshin.semantic_orchestration.workspace_safety import _git_output
from pal.bunshin.semantic_orchestration.workspace_safety import _raise_if_workspace_held
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.adapters import ARTIFACT_BUNDLE_ADAPTER, SOFTWARE_GIT_ADAPTER, ArtifactBundleAdapter, artifact_tree_fingerprint
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateType, LeaseConflict, StaleFencingToken, SubmissionInvariantError
from pal.bunshin.candidate_snapshots import CandidateSnapshotService
from pal.bunshin.workspace_resources import WorkspaceLockRegistry
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.role_cleanup import RoleCleanup
from pal.bunshin.semantic_orchestration.role_leases import RoleLeases
from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts


@dataclass
class ImplementationSnapshot:
    effect_reads: EffectReads
    role_cleanup: RoleCleanup
    role_leases: RoleLeases
    workflow_facts: WorkflowFacts
    artifacts: ContentAddressedArtifactStore
    repository: BunshinRepository
    runtime_root: Path
    workspace_locks: WorkspaceLockRegistry

    async def quiesce_node(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self.effect_reads.effect_snapshot(effect)
        node = await self.role_leases.ensure_node_effect_lease(
            node,
            action_type="REBIND_QUIESCER",
            activation=RoleActivation(
                OrchestrationRole.IMPLEMENTATION,
                RoleMode.REPAIR if node.payload.get("repair_bill_ref") else RoleMode.PRODUCE,
            ),
        )
        invocation_id = str(node.payload.get("active_worker_id") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        self.repository.leases.assert_fencing_token(lease_resource, invocation_id, fencing_token)
        lease = self.repository.leases.read_lease(lease_resource)
        if lease is None:
            raise RuntimeError("writer lease disappeared before quiescing")
        await self.role_cleanup.close_owned_process(
            invocation_id,
        )
        workspace = Path(str(node.payload.get("workspace_path") or ""))
        await self.role_cleanup.release_managed_lsp_workspace(workspace)
        _raise_if_workspace_held(workspace, "a live process still holds the candidate workspace")
        self.workspace_locks.release(node.aggregate_id)
        lock_path = self.workspace_locks.acquire(node.aggregate_id, workspace)
        try:
            _raise_if_workspace_held(
                workspace,
                "a process reached the candidate workspace during quiescing",
                manager_snapshot_lock=lock_path,
            )
            fingerprint = self.workflow_facts.workspace_fingerprint(node, workspace)
        except BaseException:
            self.workspace_locks.release(node.aggregate_id)
            raise
        current = self.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
        self.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="QUIESCE_COMPLETED",
                workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node.aggregate_id,
                actor="bunshin-v2-manager",
                expected_version=current.version,
                idempotency_key=f"effect:{effect['effect_key']}:quiesced",
                payload={
                    "fencing_token": fencing_token,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": fingerprint,
                    "workspace_lock_path": str(lock_path),
                },
            )
        )
        return {}

    async def snapshot_implementation_result(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self.effect_reads.effect_snapshot(effect)
        node = await self.role_leases.ensure_node_effect_lease(
            node,
            action_type="REBIND_SNAPSHOTTER",
            activation=RoleActivation(
                OrchestrationRole.IMPLEMENTATION,
                RoleMode.REPAIR if node.payload.get("repair_bill_ref") else RoleMode.PRODUCE,
            ),
        )
        invocation_id = str(node.payload.get("active_worker_id") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        contract_ref = _ref_from_mapping(node.payload.get("unit_contract_ref"))
        contract = self.artifacts.read_json(contract_ref)
        adapter = self.workflow_facts.execution_adapter(node)
        if adapter == SOFTWARE_GIT_ADAPTER:
            worktree = Path(str(node.payload.get("workspace_path") or ""))
            # The Module branch is the handoff truth.  Coder and Verifier
            # share it, so its current HEAD may contain durable verifier
            # corpus commits or a preserved predecessor candidate that are
            # intentionally outside the Coder's write scope.  The sandbox
            # forbids the Coder from moving HEAD; therefore HEAD at snapshot
            # time is the exact assignment baseline and only the working-tree
            # delta belongs to this Coder turn.
            base_sha = _git_output(worktree, "rev-parse", "HEAD")
            candidate_ref, candidate_digest = CandidateSnapshotService(
                self.repository,
                self.artifacts,
                self.workspace_locks,
            ).create_candidate(
                node_run_id=node.aggregate_id,
                worker_id=invocation_id,
                lease_resource_key=lease_resource,
                fencing_token=fencing_token,
                worktree=worktree,
                expected_workspace_fingerprint=str(node.payload.get("workspace_fingerprint") or ""),
                reference_only_paths=[str(item) for item in list(contract.get("reference_only_paths") or [])],
                path_policy=dict(node.payload.get("path_policy") or {}),
                base_sha=base_sha,
                candidate_baseline_sha=str(node.payload.get("base_sha") or ""),
                unit_contract_hash=contract_ref.sha256,
                dependency_output_hashes=dict(node.payload.get("dependency_output_hashes") or {}),
                environment_fingerprint=str(node.payload.get("environment_fingerprint") or "default"),
                repair_bill_ref=dict(node.payload.get("repair_bill_ref") or {}),
            )
        elif adapter == ARTIFACT_BUNDLE_ADAPTER:
            try:
                self.repository.leases.assert_fencing_token(lease_resource, invocation_id, fencing_token)
                workspace = Path(str(node.payload.get("workspace_path") or ""))
                before = artifact_tree_fingerprint(workspace)
                if before != str(node.payload.get("workspace_fingerprint") or ""):
                    raise RuntimeError("artifact workspace changed after quiescing")
                candidate_ref, candidate_digest = ArtifactBundleAdapter(
                    self.runtime_root,
                    self.artifacts,
                ).snapshot_candidate(
                    workspace=workspace,
                    reference_only_paths=[str(item) for item in list(contract.get("reference_only_paths") or [])],
                    unit_contract_hash=contract_ref.sha256,
                    dependency_output_hashes=dict(node.payload.get("dependency_output_hashes") or {}),
                    environment_fingerprint=str(node.payload.get("environment_fingerprint") or "default"),
                    parent_candidate_digest=str(node.payload.get("candidate_digest") or ""),
                    repair_bill_ref=dict(node.payload.get("repair_bill_ref") or {}),
                )
                if artifact_tree_fingerprint(workspace) != before:
                    raise RuntimeError("artifact workspace changed while snapshotting")
            finally:
                self.workspace_locks.release(node.aggregate_id)
        else:
            raise ValueError(f"unsupported candidate adapter: {adapter}")
        try:
            with self.repository.transaction() as connection:
                current = (connection or self.repository).snapshots.read_snapshot(
                    AggregateType.DAG_NODE_RUN,
                    node.aggregate_id,
                )
                if current is None:
                    raise SubmissionInvariantError(
                        "candidate node disappeared before atomic publication"
                    )
                (connection or self.repository).transitions.dispatch(
                    ActionEnvelope(
                        action_type="CANDIDATE_SNAPSHOTTED",
                        workflow_id=node.workflow_id,
                        aggregate_type=AggregateType.DAG_NODE_RUN,
                        aggregate_id=node.aggregate_id,
                        actor="bunshin-v2-manager",
                        expected_version=current.version,
                        idempotency_key=f"candidate:{candidate_ref.sha256}",
                        payload={
                            "candidate_ref": candidate_ref.to_dict(),
                            "candidate_digest": candidate_digest,
                            "workspace_fingerprint": str(node.payload.get("workspace_fingerprint") or ""),
                            "historical_repair_bill_refs": _append_ref(
                                node.payload.get("historical_repair_bill_refs"),
                                node.payload.get("repair_bill_ref"),
                            ),
                        },
                    ),
                )
                WorkflowCoordinator(self.repository).producer_submitted(
                    workflow_id=node.workflow_id,
                    node_name=str(
                        node.payload.get("module_name")
                        or node.payload.get("unit_id")
                        or ""
                    ),
                    product_ref=candidate_ref.sha256,
                    unit_of_work=connection,
                )
        finally:
            try:
                self.repository.leases.release_lease(
                    lease_resource,
                    invocation_id,
                    fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
        return {"result_artifact_ref": candidate_ref.to_dict()}
