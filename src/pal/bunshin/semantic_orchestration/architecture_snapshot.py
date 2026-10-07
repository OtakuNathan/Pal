from __future__ import annotations
from pal.bunshin.artifacts import ArtifactRef
import pal.bunshin.execution_values as _dependency_execution_values
from pal.bunshin.semantic_orchestration.workflow_requests import WorkflowRequests
from pal.bunshin.semantic_orchestration.architecture_instructions import _stable_architecture_preflight_finding
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
from pal.bunshin.semantic_orchestration.workspace_safety import _raise_if_workspace_held
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, LeaseConflict, StaleFencingToken, SubmissionInvariantError
from pal.bunshin.execution_values import workspace_content_fingerprint
from pal.bunshin.workspace_resources import WorkspaceLockRegistry
from pal.bunshin.skeleton import ARCHITECTURE_REPAIR_BASELINE_ARTIFACT, ArchitectureWorkspace, architecture_revision_path_states
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.role_cleanup import RoleCleanup
from pal.bunshin.semantic_orchestration.role_leases import RoleLeases
from pal.bunshin.skeleton import GitBackedSkeletonService


@dataclass
class ArchitectureSnapshot:
    effect_reads: EffectReads
    role_cleanup: RoleCleanup
    role_leases: RoleLeases
    artifacts: ContentAddressedArtifactStore
    repository: BunshinRepository
    requests: WorkflowRequests
    skeleton: GitBackedSkeletonService
    workspace_locks: WorkspaceLockRegistry

    async def quiesce_architect_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = await self.role_leases.ensure_architecture_effect_lease(
            self.effect_reads.effect_snapshot(effect),
            action_type="REBIND_ARCHITECT_QUIESCER",
        )
        invocation_id = str(revision.payload.get("active_worker_id") or "")
        fencing_token = int(revision.payload.get("fencing_token") or 0)
        lease_resource = str(revision.payload.get("lease_resource_key") or "")
        self.repository.leases.assert_fencing_token(lease_resource, invocation_id, fencing_token)
        await self.role_cleanup.close_owned_process(
            invocation_id,
        )
        workspace = Path(str(revision.payload.get("architecture_workspace_path") or ""))
        await self.role_cleanup.release_managed_lsp_workspace(workspace)
        _raise_if_workspace_held(workspace, "a live process still holds the architecture worktree")
        self.workspace_locks.release(revision.aggregate_id)
        lock_path = self.workspace_locks.acquire(revision.aggregate_id, workspace)
        try:
            _raise_if_workspace_held(
                workspace,
                "a process reached the architecture worktree during quiescing",
                manager_snapshot_lock=lock_path,
            )
            fingerprint = _dependency_execution_values.workspace_content_fingerprint(workspace)
        except BaseException:
            self.workspace_locks.release(revision.aggregate_id)
            raise
        current = self.repository.snapshots.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
        self.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="ARCHITECT_QUIESCED",
                workflow_id=revision.workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision.aggregate_id,
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

    async def snapshot_architect_result(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = await self.role_leases.ensure_architecture_effect_lease(
            self.effect_reads.effect_snapshot(effect),
            action_type="REBIND_ARCHITECT_SNAPSHOTTER",
        )
        invocation_id = str(revision.payload.get("active_worker_id") or "")
        fencing_token = int(revision.payload.get("fencing_token") or 0)
        lease_resource = str(revision.payload.get("lease_resource_key") or "")
        self.repository.leases.assert_fencing_token(lease_resource, invocation_id, fencing_token)
        if not self.workspace_locks.is_held(revision.aggregate_id):
            raise RuntimeError("architecture snapshot requires the quiescer's exclusive worktree lock")
        workspace_path = Path(str(revision.payload.get("architecture_workspace_path") or ""))
        before = _dependency_execution_values.workspace_content_fingerprint(workspace_path)
        if before != str(revision.payload.get("workspace_fingerprint") or ""):
            raise RuntimeError("architecture worktree changed after quiescing")
        architecture_workspace, contract_intent, reference_roots, requirements_ref, submission, submission_ref = self.read_architecture_snapshot_input(revision, workspace_path)
        try:
            skeleton_ref = self.skeleton.snapshot_architect_result(
                workflow_name=revision.workflow_id,
                revision_name=revision.aggregate_id,
                architecture_workspace=architecture_workspace,
                submission=submission,
                requirements_ref=requirements_ref,
                reference_roots=reference_roots,
                evidence_catalog_ref=(
                    _ref_from_mapping(revision.payload.get("evidence_catalog_ref"))
                    if revision.payload.get("evidence_catalog_ref")
                    else None
                ),
            )
            skeleton_artifact = dict(
                self.artifacts.read_json(skeleton_ref)
            )
            manifest_ref = self.publish_architecture_manifest(contract_intent, requirements_ref, revision, skeleton_artifact, skeleton_ref, submission_ref)
        except ValueError as exc:
            finding_payload = _stable_architecture_preflight_finding(
                exc,
                contract_intent=contract_intent,
                submission=submission,
            )
            finding_ref = self.artifacts.put_json(
                finding_payload,
                artifact_type="ArchitectureFindingArtifact",
                child_refs=((submission_ref.sha256, "rejected_submission"),),
            )
            repair_baseline_ref = self.artifacts.put_json(
                {
                    "submission": dict(submission),
                    "path_states": architecture_revision_path_states(
                        workspace_path,
                        architecture_workspace.base_sha,
                    ),
                    "workspace_fingerprint": before,
                },
                artifact_type=ARCHITECTURE_REPAIR_BASELINE_ARTIFACT,
                provenance={
                    "workflow_id": revision.workflow_id,
                    "architecture_revision_id": revision.aggregate_id,
                },
                child_refs=(
                    (submission_ref.sha256, "rejected_submission"),
                    (finding_ref.sha256, "snapshot_finding"),
                ),
            )
            try:
                with self.repository.transaction() as connection:
                    current = (connection or self.repository).snapshots.read_snapshot(
                        AggregateType.ARCHITECTURE_REVISION,
                        revision.aggregate_id,
                    )
                    if current is None:
                        raise SubmissionInvariantError(
                            "architecture revision disappeared before rejection"
                        )
                    (connection or self.repository).transitions.dispatch(
                        ActionEnvelope(
                            action_type="ARCHITECTURE_SNAPSHOT_REJECTED",
                            workflow_id=revision.workflow_id,
                            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                            aggregate_id=revision.aggregate_id,
                            actor="bunshin-v2-manager",
                            expected_version=current.version,
                            idempotency_key=(
                                f"architecture-snapshot-rejected:{revision.aggregate_id}:{finding_ref.sha256}"
                            ),
                            payload={
                                "finding_artifact_ref": finding_ref.to_dict(),
                                "architecture_repair_baseline_ref": repair_baseline_ref.to_dict(),
                            },
                        ),
                    )
                    WorkflowCoordinator(self.repository).reject_plan_product(
                        workflow_id=revision.workflow_id,
                        unit_of_work=connection,
                    )
            finally:
                self.workspace_locks.release(revision.aggregate_id)
                try:
                    self.repository.leases.release_lease(
                        lease_resource,
                        invocation_id,
                        fencing_token,
                    )
                except (LeaseConflict, StaleFencingToken):
                    pass
            return {"result_artifact_ref": finding_ref.to_dict(), "status": "rejected"}
        if _dependency_execution_values.workspace_content_fingerprint(workspace_path) != before:
            raise RuntimeError("architecture worktree content changed while the Manager created its commit")
        manifest_payload = self.artifacts.read_json(manifest_ref)
        effective_requirements_ref = _ref_from_mapping(manifest_payload.get("requirements_ref"))
        try:
            self.commit_architecture_snapshot(before, effective_requirements_ref, manifest_ref, revision)
        finally:
            self.workspace_locks.release(revision.aggregate_id)
            try:
                self.repository.leases.release_lease(
                    lease_resource,
                    invocation_id,
                    fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
        return {"result_artifact_ref": manifest_ref.to_dict()}

    def commit_architecture_snapshot(self, before: Any, effective_requirements_ref: Any, manifest_ref: ArtifactRef, revision: AggregateSnapshot) -> None:
        with self.repository.transaction() as connection:
            current = (connection or self.repository).snapshots.read_snapshot(
                AggregateType.ARCHITECTURE_REVISION,
                revision.aggregate_id,
            )
            if current is None:
                raise SubmissionInvariantError(
                    "architecture revision disappeared before publication"
                )
            (connection or self.repository).transitions.dispatch(
                ActionEnvelope(
                    action_type="ARCHITECTURE_SNAPSHOTTED",
                    workflow_id=revision.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision.aggregate_id,
                    actor="bunshin-v2-manager",
                    expected_version=current.version,
                    idempotency_key=f"architecture-snapshot:{revision.aggregate_id}:{manifest_ref.sha256}",
                    payload={
                        "requirements_ref": effective_requirements_ref.to_dict(),
                        "architecture_manifest_ref": manifest_ref.to_dict(),
                        "workspace_fingerprint": before,
                    },
                ),
            )
            WorkflowCoordinator(self.repository).submit_plan_product(
                workflow_id=revision.workflow_id,
                product_ref=manifest_ref.sha256,
                unit_of_work=connection,
            )

    def publish_architecture_manifest(
        self, contract_intent: Any, requirements_ref: Any, revision: AggregateSnapshot, skeleton_artifact: Any,
        skeleton_ref: Any, submission_ref: ArtifactRef,
    ) -> ArtifactRef:
        manifest_ref = self.artifacts.put_json(
            {
                "schema_version": "2",
                "contract_schema": str(
                    contract_intent.get("contract_schema") or ""
                ),
                "contract": dict(contract_intent.get("contract") or {}),
                "graph_ir": dict(contract_intent.get("graph_ir") or {}),
                "graph_source_map_ref": dict(
                    contract_intent.get("graph_source_map_ref") or {}
                ),
                "requirements_ref": requirements_ref.to_dict(),
                "repository_layout": dict(
                    skeleton_artifact.get("repository_layout") or {}
                ),
                "skeleton_commit_sha": str(
                    skeleton_artifact.get("skeleton_commit_sha") or ""
                ),
                "skeleton_tree_sha": str(
                    skeleton_artifact.get("skeleton_tree_sha") or ""
                ),
                "skeleton_bundle_ref": dict(
                    skeleton_artifact.get("skeleton_bundle_ref") or {}
                ),
                "git_bundle_ref": dict(
                    skeleton_artifact.get("git_bundle_ref") or {}
                ),
                "workspace_snapshot_ref": dict(
                    skeleton_artifact.get("workspace_snapshot_ref") or {}
                ),
                "base_commit_sha": str(
                    skeleton_artifact.get("base_commit_sha") or ""
                ),
                "base_tree_sha": str(
                    skeleton_artifact.get("base_tree_sha") or ""
                ),
                "contract_file_hashes": dict(
                    skeleton_artifact.get("contract_file_hashes") or {}
                ),
                "changed_paths": list(
                    skeleton_artifact.get("changed_paths") or []
                ),
                "original_workspace_head": str(
                    skeleton_artifact.get("original_workspace_head") or ""
                ),
                "source_fingerprint": str(
                    skeleton_artifact.get("source_fingerprint") or ""
                ),
            },
            artifact_type="ContractArtifact",
            provenance={
                "architecture_revision_id": revision.aggregate_id,
                "role": "architect",
                "contract_schema": str(
                    contract_intent.get("contract_schema") or ""
                ),
            },
            child_refs=(
                (submission_ref.sha256, "contract_submission"),
                (skeleton_ref.sha256, "repository_snapshot"),
                (requirements_ref.sha256, "requirements"),
            )
            + (
                (
                    (
                        str(
                            dict(
                                contract_intent.get(
                                    "graph_source_map_ref"
                                )
                                or {}
                            ).get("sha256")
                            or ""
                        ),
                        "graph_source_map",
                    ),
                )
                if dict(
                    contract_intent.get("graph_source_map_ref") or {}
                ).get("sha256")
                else ()
            ),
        )
        return manifest_ref

    def read_architecture_snapshot_input(self, revision: AggregateSnapshot, workspace_path: Any) -> tuple[Any, Any, Any, Any, Any, ArtifactRef]:
        workspace_snapshot_ref = _ref_from_mapping(revision.payload.get("workspace_snapshot_ref"))
        workspace_snapshot = self.artifacts.read_json(workspace_snapshot_ref)
        architecture_workspace = ArchitectureWorkspace(
            worktree=workspace_path,
            common_git_dir=Path(str(revision.payload.get("architecture_common_git_dir") or "")),
            base_sha=str(revision.payload.get("architecture_base_sha") or ""),
            base_tree_sha=str(revision.payload.get("architecture_base_tree_sha") or ""),
            original_head=str(workspace_snapshot.get("original_head") or ""),
            source_fingerprint=str(workspace_snapshot.get("source_fingerprint") or ""),
            workspace_snapshot_ref=workspace_snapshot_ref,
            project_name=str(
                dict(revision.payload.get("architecture_repository_layout") or {}).get("project_name") or ""
            ),
            project_key=str(
                dict(revision.payload.get("architecture_repository_layout") or {}).get("project_key") or ""
            ),
            workflow_name=str(
                dict(revision.payload.get("architecture_repository_layout") or {}).get("workflow_name") or ""
            ),
            workflow_key=str(
                dict(revision.payload.get("architecture_repository_layout") or {}).get("workflow_key") or ""
            ),
            workflow_branch=str(
                dict(revision.payload.get("architecture_repository_layout") or {}).get("workflow_branch") or ""
            ),
            architecture_branch=str(revision.payload.get("architecture_branch") or ""),
        )
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            raise ValueError("architecture revision has no workflow")
        request = self.requests.read(workflow)
        reference_roots = {
            str(item.get("name") or f"reference_{index + 1}"): Path(str(item.get("path") or "")).expanduser()
            for index, item in enumerate(list(request.get("references") or []))
            if str(dict(item or {}).get("path") or "").strip()
        }
        submission_ref = _ref_from_mapping(revision.payload.get("pending_architecture_submission_ref"))
        contract_intent = dict(
            self.artifacts.read_json(submission_ref)
        )
        submission = dict(
            contract_intent.get("compiled_skeleton_submission") or {}
        )
        if not submission:
            raise ValueError(
                "contract submission has no compiled software skeleton projection"
            )
        requirements_ref = _ref_from_mapping(revision.payload.get("requirements_ref"))
        return architecture_workspace, contract_intent, reference_roots, requirements_ref, submission, submission_ref
