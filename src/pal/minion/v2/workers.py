from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import signal
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from pal.minion.profiles import MinionProfileRegistry
from pal.minion.ipc import python_subprocess_env
from pal.minion.sandbox import build_sandboxed_runner_invocation, with_minion_sandbox_metadata
from pal.minion.turns import sanitize_runner_session_pack
from pal.minion.v2.architecture import (
    ArchitectureFindingKind,
    ArchitectureRevisionTarget,
    ArchitectureReviewResult,
    ResearchMode,
    architecture_manifest_child_refs,
    contract_revision_changes,
    normalize_revision_targets,
)
from pal.minion.v2.adapters import (
    ARTIFACT_BUNDLE_ADAPTER,
    SOFTWARE_GIT_ADAPTER,
    ArtifactBundleAdapter,
    artifact_tree_fingerprint,
    prepare_v2_role_workspace,
    prepare_v2_workspace_environment,
)
from pal.minion.lsp_prewarm import prewarm_workspace_lsp
from pal.minion.v2.artifacts import ArtifactRef
from pal.minion.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, LeaseConflict, StaleFencingToken
from pal.minion.v2.contract_builder import (
    ARCHITECT_BUILDER_CAPABILITIES,
    ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES,
    CONTRACT_SKETCH_BUILDER_CAPABILITIES,
    REQUIREMENTS_BUILDER_CAPABILITIES,
    REVISION_CONTRACT_BUILDER_CAPABILITIES,
    is_contract_builder_capability,
    seed_contract_builder_draft,
)
from pal.minion.v2.execution import (
    CandidateSnapshotService,
    UnitWorkViewBuilder,
    WorkspaceLockRegistry,
    provision_verification_worktree,
    terminate_process_group,
    workspace_content_fingerprint,
    workspace_has_live_processes,
)
from pal.minion.v2.service import MinionV2WorkflowService, workflow_request_from_snapshot
from pal.minion.v2.sessions import architect_session_id, coder_session_id
from pal.minion.v2.skeleton import (
    ARCHITECTURE_SKELETON_ARTIFACT,
    ArchitectureWorkspace,
    SemanticReferenceError,
    SkeletonReviewFinding,
    SkeletonReviewResult,
    compile_skeleton_markdown,
    requirements_semantic_view,
    review_architecture_skeleton,
)
from pal.minion.v2.integration import (
    CandidateUnionConflict,
    CandidateUnionService,
    IntegrationOwnershipDefect,
    IntegrationService,
)
from pal.minion.v2.verification import (
    DefectKind,
    UnknownPolicy,
    VerificationCaseKind,
    VerificationCaseRunner,
    VerificationCaseSpec,
    VerificationService,
    VerificationStatus,
    aggregate_verification_status,
    repair_bill_semantic_view,
    semantic_finding_payload,
)
from pal.shared import MinionInvocationPack


HumanReviewPublisher = Callable[[Mapping[str, Any]], Awaitable[None]]
WorkerEventPublisher = Callable[[Mapping[str, Any]], Awaitable[None]]
BrokerRunRegistrar = Callable[[str, str, MinionInvocationPack, asyncio.subprocess.Process], None]
BrokerRunUnregistrar = Callable[[str], None]


_ARCHITECTURE_STAGE_CONFIG = {
    "architect": ("architect", "START_ARCHITECT"),
}

SEMANTIC_EFFECT_TYPES = frozenset(
    {
        "enqueue_architecture_stage",
        "enqueue_architecture_review",
        "quiesce_architect",
        "snapshot_architecture",
        "publish_human_architecture_review",
        "request_human_clarification",
        "reconcile_architecture_revision",
        "enqueue_producer",
        "spawn_producer_worker",
        "enqueue_node_review",
        "spawn_verifier_worker",
        "enqueue_scenario_verifier",
        "spawn_scenario_verifier",
        "enqueue_repair",
        "spawn_repair_worker",
        "quiesce_worker",
        "snapshot_candidate",
        "pause_node_worker",
        "cancel_node_worker",
        "pause_aggregate_work",
        "cancel_aggregate_work",
        "resume_aggregate_work",
        "resume_node_work",
        "reconcile_node_run",
        "reconcile_standalone_review",
        "publish_final_deliverable",
        "enqueue_standalone_review",
        "publish_review_report",
    }
)


@dataclass
class MinionV2SemanticWorker:
    service: MinionV2WorkflowService
    publish_human_review: HumanReviewPublisher | None = None
    publish_worker_event: WorkerEventPublisher | None = None
    register_broker_run: BrokerRunRegistrar | None = None
    unregister_broker_run: BrokerRunUnregistrar | None = None
    _processes: dict[str, asyncio.subprocess.Process] = field(default_factory=dict, init=False)
    _run_to_invocation: dict[str, str] = field(default_factory=dict, init=False)
    _worktree_locks: WorkspaceLockRegistry = field(default_factory=WorkspaceLockRegistry, init=False)
    _revoked_tokens: set[tuple[str, int]] = field(default_factory=set, init=False)

    @property
    def repository(self):
        return self.service.repository

    async def execute_semantic_effect(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        effect_type = str(effect.get("effect_type") or "")
        if effect_type == "enqueue_architecture_stage":
            return await self._run_architecture_stage(effect)
        if effect_type == "enqueue_architecture_review":
            return await self._run_architecture_review(effect)
        if effect_type == "quiesce_architect":
            return await self._quiesce_architect(effect)
        if effect_type == "snapshot_architecture":
            return await self._snapshot_architecture(effect)
        if effect_type == "publish_human_architecture_review":
            return await self._publish_human_architecture_review(effect)
        if effect_type == "request_human_clarification":
            return await self._publish_human_clarification(effect)
        if effect_type == "reconcile_architecture_revision":
            return await self._resume_aggregate(effect)
        if effect_type == "enqueue_producer":
            return self._admit_node_worker(effect, action_type="START_PRODUCING", role="producer")
        if effect_type == "spawn_producer_worker":
            return await self._run_producer(effect, repair=False)
        if effect_type == "enqueue_node_review":
            return self._admit_node_worker(effect, action_type="START_REVIEW", role="reviewer")
        if effect_type == "spawn_verifier_worker":
            if str(effect.get("aggregate_type") or "") == AggregateType.STANDALONE_REVIEW.value:
                return await self._run_standalone_review(effect)
            return await self._run_verifier(effect)
        if effect_type == "enqueue_scenario_verifier":
            return self._admit_node_worker(
                effect,
                action_type="START_SCENARIO_VERIFICATION",
                role="scenario_verifier",
            )
        if effect_type == "spawn_scenario_verifier":
            return await self._run_verifier(effect, scenario_mode=True)
        if effect_type == "enqueue_repair":
            return self._admit_node_worker(effect, action_type="START_REPAIR", role="repair")
        if effect_type == "spawn_repair_worker":
            return await self._run_producer(effect, repair=True)
        if effect_type == "quiesce_worker":
            return await self._quiesce_node(effect)
        if effect_type == "snapshot_candidate":
            return await self._snapshot_candidate(effect)
        if effect_type in {"pause_node_worker", "cancel_node_worker"}:
            return await self._stop_node_worker(effect, cancel=effect_type == "cancel_node_worker")
        if effect_type in {"pause_aggregate_work", "cancel_aggregate_work"}:
            return await self._stop_aggregate_worker(effect, cancel=effect_type == "cancel_aggregate_work")
        if effect_type == "resume_aggregate_work":
            return await self._resume_aggregate(effect)
        if effect_type == "resume_node_work":
            return self._resume_node(effect)
        if effect_type == "reconcile_node_run":
            return self._resume_node(effect)
        if effect_type == "reconcile_standalone_review":
            return await self._resume_aggregate(effect)
        if effect_type == "publish_final_deliverable":
            return await self._publish_final_deliverable(effect)
        if effect_type == "enqueue_standalone_review":
            return self._admit_standalone_review(effect)
        if effect_type == "publish_review_report":
            return await self._publish_standalone_report(effect)
        raise RuntimeError(f"V2 semantic effect is not implemented yet: {effect_type}")

    def _admit_node_worker(
        self,
        effect: Mapping[str, Any],
        *,
        action_type: str,
        role: str,
    ) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        target_state = {
            "START_PRODUCING": "PRODUCING",
            "START_REVIEW": "REVIEWING",
            "START_REPAIR": "REPAIRING",
            "START_SCENARIO_VERIFICATION": "VERIFYING",
        }[action_type]
        if node.state == target_state and node.payload.get("active_worker_id"):
            return {"provider_request_id": str(node.payload.get("active_worker_id"))}
        cycle = int(node.payload.get("candidate_cycle") or 0) + (1 if role in {"producer", "repair"} else 0)
        invocation_id = (
            coder_session_id(node.aggregate_id)
            if role in {"producer", "repair"}
            else f"inv_{hashlib.sha256(f'{node.aggregate_id}:{role}:{cycle}'.encode()).hexdigest()[:24]}"
        )
        lease_resource = f"node:{node.aggregate_id}:{'writer' if role in {'producer', 'repair'} else 'review'}"
        lease = self.repository.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={"workflow_id": node.workflow_id, "node_run_id": node.aggregate_id, "role": role},
        )
        payload = {
            "fencing_token": lease.fencing_token,
            "active_worker_id": invocation_id,
            "lease_resource_key": lease_resource,
            "worker_role": role,
        }
        if role in {"producer", "repair"}:
            payload["candidate_cycle"] = cycle
        self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node.aggregate_id,
                actor="minion-v2-scheduler",
                expected_version=node.version,
                idempotency_key=f"effect:{effect['effect_key']}:admit",
                payload=payload,
            )
        )
        return {"provider_request_id": invocation_id}

    async def _ensure_node_effect_lease(
        self,
        node: AggregateSnapshot,
        *,
        action_type: str,
        role: str,
    ) -> AggregateSnapshot:
        invocation_id = str(node.payload.get("active_worker_id") or "")
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        if invocation_id and lease_resource and fencing_token:
            try:
                self.repository.assert_fencing_token(lease_resource, invocation_id, fencing_token)
                return node
            except StaleFencingToken:
                pass

        writer_role = role in {"producer", "repair"}
        if writer_role:
            invocation_id = coder_session_id(node.aggregate_id)
            lease_resource = f"node:{node.aggregate_id}:writer"
        else:
            cycle = int(node.payload.get("candidate_cycle") or 0)
            invocation_id = invocation_id or f"inv_{hashlib.sha256(f'{node.aggregate_id}:reviewer:{cycle}'.encode()).hexdigest()[:24]}"
            lease_resource = f"node:{node.aggregate_id}:review"

        previous = self.repository.read_lease(lease_resource)
        if previous is not None and str(previous.get("owner_id") or "") and _lease_is_live(previous):
            raise LeaseConflict(f"node effect lease is active under {previous.get('owner_id')}")
        process_group = int(dict((previous or {}).get("metadata") or {}).get("process_group_id") or 0)
        if process_group and not await terminate_process_group(process_group, timeout_seconds=5.0):
            raise RuntimeError("expired node worker process group could not be reaped before rebind")
        workspace = Path(str(node.payload.get("workspace_path") or ""))
        if writer_role and workspace_has_live_processes(workspace):
            raise RuntimeError("expired node worker still holds its worktree")

        lease = self.repository.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": node.workflow_id,
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": node.aggregate_id,
                "node_run_id": node.aggregate_id,
                "role": role,
                "workspace_path": str(workspace),
            },
        )
        rebound = self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node.aggregate_id,
                actor="minion-v2-recovery",
                expected_version=node.version,
                idempotency_key=f"rebind:{node.aggregate_id}:{action_type}:{lease.fencing_token}",
                payload={
                    "fencing_token": lease.fencing_token,
                    "active_worker_id": invocation_id,
                    "lease_resource_key": lease_resource,
                    "worker_role": role,
                },
            )
        ).snapshot
        if rebound.state == "SNAPSHOTTING" and not self._worktree_locks.is_held(rebound.aggregate_id):
            self._worktree_locks.acquire(rebound.aggregate_id, workspace)
            fingerprint = self._workspace_fingerprint(rebound, workspace)
            if fingerprint != str(rebound.payload.get("workspace_fingerprint") or ""):
                self._worktree_locks.release(rebound.aggregate_id)
                raise RuntimeError("candidate worktree changed while snapshot worker was unavailable")
        return rebound

    def _admit_standalone_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        review = self._effect_snapshot(effect)
        invocation_id = f"inv_{hashlib.sha256(f'{review.aggregate_id}:review'.encode()).hexdigest()[:24]}"
        lease_resource = f"standalone-review:{review.aggregate_id}"
        lease = self.repository.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={"workflow_id": review.workflow_id, "review_id": review.aggregate_id},
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="START_REVIEW",
                workflow_id=review.workflow_id,
                aggregate_type=AggregateType.STANDALONE_REVIEW,
                aggregate_id=review.aggregate_id,
                actor="minion-v2-scheduler",
                expected_version=review.version,
                idempotency_key=f"effect:{effect['effect_key']}:start",
                payload={
                    "fencing_token": lease.fencing_token,
                    "active_worker_id": invocation_id,
                    "lease_resource_key": lease_resource,
                },
            )
        )
        return {"provider_request_id": invocation_id}

    async def _run_producer(self, effect: Mapping[str, Any], *, repair: bool) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        node = await self._ensure_node_effect_lease(
            node,
            action_type="REBIND_REPAIRER" if repair else "REBIND_PRODUCER",
            role="repair" if repair else "producer",
        )
        if str(node.payload.get("node_kind") or "") == "integration" and not repair:
            return await self._run_integration(effect, node)
        invocation_id = str(node.payload.get("active_worker_id") or "")
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        self.repository.assert_fencing_token(lease_resource, invocation_id, fencing_token)
        self._write_node_journal(
            node,
            owner_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            updates={
                "current_micro_plan": (
                    ["reproduce RepairBill", "apply minimal repair", "run focused regression"]
                    if repair
                    else ["inspect UnitWorkView", "write focused tests", "implement contract", "run verification"]
                ),
                "last_safe_point": "worker_started",
            },
        )
        dependency_outputs = {
            str(key): value for key, value in dict(node.payload.get("dependency_outputs") or {}).items()
        }
        view_ref = UnitWorkViewBuilder(self.service.architecture).build(
            node,
            dependency_outputs=dependency_outputs,
        )
        references = {"unit_work_view": view_ref}
        repair_ref = node.payload.get("repair_bill_ref")
        if isinstance(repair_ref, Mapping) and repair_ref.get("sha256"):
            semantic_repair_ref = self.service.artifacts.put_json(
                repair_bill_semantic_view(self.service.artifacts, repair_ref),
                artifact_type="RepairBillSemanticViewArtifact",
                provenance={"owner": "manager", "audience": "coder"},
                child_refs=((str(repair_ref["sha256"]), "repair_bill"),),
            )
            references["repair_bill"] = semantic_repair_ref
        instruction = (
            "Repair only the defects in the bound RepairBill. Regress the reviewer reproducer first, add the relevant regression "
            "test to the project, and make the smallest contract-preserving change. Do not revisit unrelated code."
            if repair
            else "Implement the bound UnitWorkView. Start from its approved evidence and contract, write focused tests first, and stay inside owned_area."
        )
        if self._is_skeleton_manifest(node.payload.get("architecture_manifest_ref")):
            instruction = (
                "Repair only the bound RepairBill against the accepted code skeleton; regress its reproducer first and change only owned implementation/test paths."
                if repair
                else "Implement the bound ModuleWorkView from the accepted code skeleton. Keep frozen contracts unchanged, write focused tests first, and change only owned implementation/test paths."
            )
        terminal, prompt_ref, terminal_ref = await self._run_profile(
            effect=effect,
            snapshot=node,
            invocation_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            profile=self._profile_for_role(node.workflow_id, "repair" if repair else "producer"),
            role_override="repair" if repair else "producer",
            instruction=instruction,
            reference_refs=references,
            workspace_override={
                "kind": "existing_repo",
                "repo_path": str(node.payload.get("workspace_path") or ""),
                "project_name": str(node.payload.get("unit_id") or "unit"),
                "write_path_scopes": [
                    *list(dict(node.payload.get("path_policy") or {}).get("implementation_scopes") or []),
                    *list(dict(node.payload.get("path_policy") or {}).get("test_scopes") or []),
                ],
                "require_os_path_enforcement": self._is_skeleton_manifest(
                    node.payload.get("architecture_manifest_ref")
                ),
            },
            prepare_workspace=False,
        )
        report = _primary_json_output(terminal)
        if self._is_skeleton_manifest(node.payload.get("architecture_manifest_ref")):
            _validate_skeleton_coder_report(
                report,
                expected_module=str(node.payload.get("module_name") or node.payload.get("unit_id") or ""),
                work_view=self.service.artifacts.read_json(view_ref),
            )
            _reject_manager_identity_fields(report, owner="Coder output")
        status = str(report.get("status") or "candidate_ready").strip().lower()
        report_ref = self.service.artifacts.put_json(
            report,
            artifact_type="ProducerReportArtifact",
            child_refs=((view_ref.sha256, "unit_work_view"),),
        )
        self._write_node_journal(
            node,
            owner_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            updates={
                "current_micro_plan": list(report.get("current_micro_plan") or []),
                "completed_checklist": list(report.get("completed_checklist") or []),
                "files_inspected": list(report.get("files_inspected") or []),
                "files_changed": list(report.get("files_changed") or []),
                "tests_run": list(report.get("tests_run") or []),
                "open_questions": list(report.get("open_questions") or []),
                "known_failures": list(report.get("known_failures") or []),
                "last_safe_point": "producer_report_persisted",
            },
        )
        self.repository.record_worker_turn(
            invocation_id=invocation_id,
            fencing_token=fencing_token,
            turn_index=_worker_session_turn_index(terminal),
            llm_request_ref=prompt_ref.to_dict(),
            llm_response_ref=terminal_ref.to_dict(),
            tool_summary_ref=report_ref.to_dict(),
            **_recorded_worker_metrics(terminal),
        )
        current = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
        if status in {"architecture_defect", "module_split_request"}:
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="PRODUCER_ARCHITECTURE_DEFECT",
                    workflow_id=node.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor=invocation_id,
                    expected_version=current.version,
                    idempotency_key=f"producer-defect:{node.aggregate_id}:{report_ref.sha256}",
                    payload={"finding_artifact_ref": report_ref.to_dict()},
                )
            )
            self.repository.complete_worker_session(coder_session_id(node.aggregate_id))
            self.repository.release_lease(lease_resource, invocation_id, fencing_token)
            return {"provider_request_id": invocation_id, "result_artifact_ref": report_ref.to_dict()}
        if status != "candidate_ready":
            raise ValueError(f"producer report has unsupported status: {status}")
        self.repository.dispatch(
            ActionEnvelope(
                action_type="SUBMIT_CANDIDATE",
                workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node.aggregate_id,
                actor=invocation_id,
                expected_version=current.version,
                idempotency_key=f"producer-submit:{node.aggregate_id}:{report_ref.sha256}",
                payload={
                    "fencing_token": fencing_token,
                    "producer_report_ref": report_ref.to_dict(),
                    "unit_work_view_ref": view_ref.to_dict(),
                },
            )
        )
        return {"provider_request_id": invocation_id, "result_artifact_ref": report_ref.to_dict()}

    async def _quiesce_node(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        node = await self._ensure_node_effect_lease(
            node,
            action_type="REBIND_QUIESCER",
            role="producer",
        )
        invocation_id = str(node.payload.get("active_worker_id") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        self.repository.assert_fencing_token(lease_resource, invocation_id, fencing_token)
        self._revoked_tokens.add((invocation_id, fencing_token))
        lease = self.repository.read_lease(lease_resource)
        if lease is None:
            raise RuntimeError("writer lease disappeared before quiescing")
        metadata = dict(lease.get("metadata") or {})
        process = self._processes.get(invocation_id)
        process_group = int(metadata.get("process_group_id") or (process.pid if process is not None else 0))
        if process_group > 0 and not await terminate_process_group(process_group, timeout_seconds=5.0):
            raise RuntimeError("worker process group did not quiesce")
        workspace = Path(str(node.payload.get("workspace_path") or ""))
        if workspace_has_live_processes(workspace):
            raise RuntimeError("a live process still holds the candidate workspace")
        lock_path = self._worktree_locks.acquire(node.aggregate_id, workspace)
        try:
            if workspace_has_live_processes(workspace):
                raise RuntimeError("a process reached the candidate workspace during quiescing")
            fingerprint = self._workspace_fingerprint(node, workspace)
        except BaseException:
            self._worktree_locks.release(node.aggregate_id)
            raise
        current = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
        self.repository.dispatch(
            ActionEnvelope(
                action_type="QUIESCE_COMPLETED",
                workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node.aggregate_id,
                actor="minion-v2-manager",
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

    async def _snapshot_candidate(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        node = await self._ensure_node_effect_lease(
            node,
            action_type="REBIND_SNAPSHOTTER",
            role="producer",
        )
        invocation_id = str(node.payload.get("active_worker_id") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        if str(node.payload.get("node_kind") or "") == "integration":
            candidate_ref = _ref_from_mapping(node.payload.get("pending_integration_candidate_ref"))
            candidate_digest = str(node.payload.get("pending_integration_candidate_digest") or "")
            if not candidate_digest or not self._worktree_locks.is_held(node.aggregate_id):
                raise RuntimeError("integration candidate is not quiesced for snapshot")
            release_integration_lock = True
        else:
            release_integration_lock = False
            contract_ref = _ref_from_mapping(node.payload.get("unit_contract_ref"))
            contract = self.service.artifacts.read_json(contract_ref)
            adapter = self._execution_adapter(node)
            if adapter == SOFTWARE_GIT_ADAPTER:
                base_sha = str(node.payload.get("candidate_digest") or node.payload.get("base_sha") or "")
                candidate_ref, candidate_digest = CandidateSnapshotService(
                    self.repository,
                    self.service.artifacts,
                    self._worktree_locks,
                ).create_candidate(
                    node_run_id=node.aggregate_id,
                    worker_id=invocation_id,
                    lease_resource_key=lease_resource,
                    fencing_token=fencing_token,
                    worktree=Path(str(node.payload.get("workspace_path") or "")),
                    expected_workspace_fingerprint=str(node.payload.get("workspace_fingerprint") or ""),
                    reference_only_paths=[str(item) for item in list(contract.get("reference_only_paths") or [])],
                    path_policy=dict(node.payload.get("path_policy") or {}),
                    base_sha=base_sha,
                    candidate_baseline_sha=str(node.payload.get("base_sha") or ""),
                    unit_contract_hash=contract_ref.sha256,
                    dependency_output_hashes=dict(node.payload.get("dependency_output_hashes") or {}),
                    environment_fingerprint=str(node.payload.get("environment_fingerprint") or "default"),
                    parent_candidate_digest=str(node.payload.get("candidate_digest") or ""),
                    repair_bill_ref=dict(node.payload.get("repair_bill_ref") or {}),
                )
            elif adapter == ARTIFACT_BUNDLE_ADAPTER:
                try:
                    self.repository.assert_fencing_token(lease_resource, invocation_id, fencing_token)
                    workspace = Path(str(node.payload.get("workspace_path") or ""))
                    before = artifact_tree_fingerprint(workspace)
                    if before != str(node.payload.get("workspace_fingerprint") or ""):
                        raise RuntimeError("artifact workspace changed after quiescing")
                    candidate_ref, candidate_digest = ArtifactBundleAdapter(
                        self.service.runtime_root,
                        self.service.artifacts,
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
                    self._worktree_locks.release(node.aggregate_id)
            else:
                raise ValueError(f"unsupported candidate adapter: {adapter}")
        current = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CANDIDATE_SNAPSHOTTED",
                workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node.aggregate_id,
                actor="minion-v2-manager",
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
            )
        )
        if release_integration_lock:
            self._worktree_locks.release(node.aggregate_id)
        self.repository.release_lease(lease_resource, invocation_id, fencing_token)
        return {"result_artifact_ref": candidate_ref.to_dict()}

    async def _run_verifier(
        self,
        effect: Mapping[str, Any],
        *,
        scenario_mode: bool = False,
    ) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        scenario_mode = scenario_mode or str(node.payload.get("node_kind") or "") == "verification"
        node = await self._ensure_node_effect_lease(
            node,
            action_type="REBIND_SCENARIO_VERIFIER" if scenario_mode else "REBIND_REVIEWER",
            role="reviewer",
        )
        invocation_id = str(node.payload.get("active_worker_id") or "")
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        candidate_ref = _ref_from_mapping(
            node.payload.get("unit_contract_ref") if scenario_mode else node.payload.get("candidate_ref")
        )
        candidate_digest = str(
            node.payload.get("scenario_fingerprint") if scenario_mode else node.payload.get("candidate_digest") or ""
        )
        adapter = self._execution_adapter(node)
        if scenario_mode and adapter == SOFTWARE_GIT_ADAPTER:
            review_workspace = Path(str(node.payload.get("workspace_path") or ""))
            if not review_workspace.is_dir():
                raise ValueError("verification scenario workspace is unavailable")
            review_scratch = (
                self.service.runtime_root
                / "data"
                / "minion"
                / "v2"
                / "verification-scratch"
                / _safe_component(node.aggregate_id)
            )
            review_scratch.mkdir(parents=True, exist_ok=True)
        elif adapter == SOFTWARE_GIT_ADAPTER:
            review_workspace, review_scratch = provision_verification_worktree(
                self.service.runtime_root,
                node=node,
                candidate_digest=candidate_digest,
            )
        elif adapter == ARTIFACT_BUNDLE_ADAPTER:
            review_workspace, review_scratch = ArtifactBundleAdapter(
                self.service.runtime_root,
                self.service.artifacts,
            ).prepare_verification_workspace(
                review_id=f"{node.aggregate_id}:{candidate_digest}",
                candidate_ref=candidate_ref.to_dict(),
            )
        else:
            raise ValueError(f"unsupported verification adapter: {adapter}")
        if scenario_mode:
            view_ref = _ref_from_mapping(node.payload.get("scenario_work_view_ref"))
        elif str(node.payload.get("node_kind") or "") == "integration":
            integration_contract = self.service.artifacts.read_json(
                dict(node.payload["unit_contract_ref"])
            )
            dependency_modules: list[str] = []
            for dependency_id in list(node.payload.get("dependency_node_ids") or []):
                dependency = self.repository.read_snapshot(
                    AggregateType.DAG_NODE_RUN, str(dependency_id)
                )
                if dependency is None:
                    raise ValueError("integration verifier cannot resolve a dependency module")
                dependency_modules.append(
                    str(dependency.payload.get("module_name") or dependency.payload.get("unit_id") or "")
                )
            view_ref = self.service.artifacts.put_json(
                {
                    "schema_version": "1",
                    "module_name": "integration",
                    "integration_contract": integration_contract,
                    "accepted_dependency_modules": dependency_modules,
                    "verification_obligations": ["full build", "full test", "cross-unit lifecycle and interface adversarial probes"],
                },
                artifact_type="IntegrationWorkViewArtifact",
                child_refs=((str(dict(node.payload["unit_contract_ref"])["sha256"]), "integration_contract"),),
            )
        else:
            view_value = node.payload.get("unit_work_view_ref")
            if not isinstance(view_value, Mapping) or not view_value.get("sha256"):
                raise ValueError("verifier requires the exact UnitWorkView used by coder")
            view_ref = _ref_from_mapping(view_value)
            if self._is_skeleton_manifest(node.payload.get("architecture_manifest_ref")):
                view_ref = UnitWorkViewBuilder(self.service.architecture).build(
                    node,
                    dependency_outputs=dict(node.payload.get("dependency_outputs") or {}),
                )
        candidate = dict(self.service.artifacts.read_json(candidate_ref))
        candidate_view_ref = self.service.artifacts.put_json(
            {
                "module_name": str(node.payload.get("module_name") or node.payload.get("unit_id") or ""),
                "node_kind": str(node.payload.get("node_kind") or "unit"),
                "changed_paths": [str(item) for item in list(candidate.get("changed_paths") or [])],
                "candidate_cycle": int(node.payload.get("candidate_cycle") or 0),
                "instruction": (
                    "Verify the exact accepted-module scenario assembled in the bound read-only worktree."
                    if scenario_mode
                    else "Inspect the immutable candidate with Git show/diff in the bound review worktree."
                ),
            },
            artifact_type="VerificationScenarioSemanticViewArtifact" if scenario_mode else "CandidateSemanticViewArtifact",
            provenance={"owner": "manager", "audience": "verifier"},
            child_refs=((candidate_ref.sha256, "candidate"),),
        )
        terminal, prompt_ref, terminal_ref = await self._run_profile(
            effect=effect,
            snapshot=node,
            invocation_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            profile=self._profile_for_role(node.workflow_id, "verifier"),
            role_override="verifier",
            instruction=(
                "Generate and run adversarial verification for the bound real usage scenario. Prove only the Requirements claimed by this exact module combination, entrypoint, and environment. Write reproducible case commands; the manager reruns them and owns the verdict."
                if scenario_mode
                else "Generate and run adversarial verification for the bound candidate. Historical RepairBills come first. Write reproducible case commands; the manager will rerun them and owns the verdict."
            ),
            reference_refs={"module_work_view": view_ref, "candidate_diff": candidate_view_ref},
            workspace_override={
                "kind": "existing_repo",
                "repo_path": str(review_workspace),
                "project_name": str(node.payload.get("unit_id") or "unit"),
            },
            prepare_workspace=False,
        )
        plan = _primary_json_output(terminal)
        _validate_semantic_verification_plan_shape(plan, standalone=False)
        _reject_manager_identity_fields(plan, owner="Verifier output")
        case_specs = _verification_case_specs(plan.get("cases"))
        findings = _verification_findings(plan, case_specs)
        _validate_verifier_requirement_refs(
            work_view=self.service.artifacts.read_json(view_ref),
            cases=case_specs,
            findings=findings,
        )
        verification_policy = self._workflow_policy(node.workflow_id, "verification")
        _validate_verification_policy(plan, case_specs, verification_policy, node)
        case_timeout = float(verification_policy.get("case_timeout_seconds") or 300)
        runner = VerificationCaseRunner(self.service.artifacts)
        case_results = [
            runner.run(
                case,
                cwd=review_workspace,
                environment={"PAL_MINION_REVIEW_SCRATCH": str(review_scratch)},
                timeout_seconds=case_timeout,
            )
            for case in case_specs
        ]
        findings = _confirmed_verification_findings(findings, case_specs, case_results)
        verification = VerificationService(self.repository, self.service.artifacts)
        test_workspace_ref = self._publish_verification_workspace(
            review_worktree=review_workspace,
            review_scratch=review_scratch,
            candidate_digest=candidate_digest,
            execution_adapter=adapter,
            include_candidate_patch=not scenario_mode,
        )
        report_ref, status = verification.publish_report(
            node=node,
            candidate_ref=candidate_ref.to_dict(),
            case_results=case_results,
            reviewer_summary=str(plan.get("reviewer_summary") or ""),
            findings=findings,
            test_workspace_ref=test_workspace_ref.to_dict(),
        )
        current = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
        repair_ref = None
        requirement_patch_ref = None
        revised_requirements_ref = None
        fingerprint = ""
        defect_kind = _defect_kind(plan, node)
        dependency_node_id = _resolve_dependency_node_id(
            self.repository,
            node,
            dependency_module=str(plan.get("dependency_module") or ""),
            required=defect_kind == DefectKind.DEPENDENCY,
        )
        module_node_id = _resolve_dependency_node_id(
            self.repository,
            node,
            dependency_module=str(plan.get("affected_module") or ""),
            required=scenario_mode and defect_kind == DefectKind.MODULE and status == VerificationStatus.FAIL,
        )
        if status in {VerificationStatus.FAIL, VerificationStatus.UNKNOWN}:
            first_failure = next(
                (item for item in case_results if item.status in {VerificationStatus.FAIL, VerificationStatus.UNKNOWN}),
                case_results[0],
            )
            finding = _finding_for_case(findings, first_failure.case_id)
            case_ref = self.service.artifacts.put_json(
                first_failure.to_dict(),
                artifact_type="VerificationReproducerArtifact",
                child_refs=(
                    (str(first_failure.stdout_ref["sha256"]), "stdout"),
                    (str(first_failure.stderr_ref["sha256"]), "stderr"),
                ),
            )
            repair_ref, fingerprint = verification.publish_repair_bill(
                node=current,
                candidate_digest=candidate_digest,
                verification_ref=report_ref,
                defect_kind=defect_kind,
                severity=str(finding.get("severity") or plan.get("severity") or "major"),
                minimal_reproducer_ref=case_ref.to_dict(),
                test_artifact_ref=test_workspace_ref.to_dict(),
                expected={"exit_codes": list(case_specs[case_results.index(first_failure)].expected_exit_codes)},
                actual={"exit_code": first_failure.exit_code, "status": first_failure.status.value},
                suggested_repair_boundary=list(
                    finding.get("suggested_repair_boundary")
                    or plan.get("suggested_repair_boundary")
                    or []
                ),
                finding_section=str(finding.get("finding_section") or "implementation"),
                finding_summary=str(finding.get("summary") or ""),
                failure_reason=str(finding.get("failure_reason") or first_failure.summary),
                case_name=str(finding.get("case_name") or first_failure.case_name),
                requirements=list(finding.get("requirements") or first_failure.requirements),
                locations=list(finding.get("locations") or first_failure.locations),
                invariants=list(finding.get("invariants") or first_failure.invariants),
            )
            requirement_patch = plan.get("requirement_patch")
            if requirement_patch:
                if status != VerificationStatus.FAIL or defect_kind not in {
                    DefectKind.CONTRACT,
                    DefectKind.ARCHITECTURE,
                }:
                    raise ValueError(
                        "RequirementPatch is allowed only for a reproduced contract or architecture defect"
                    )
                manifest = self.service.artifacts.read_json(
                    dict(node.payload.get("architecture_manifest_ref") or {})
                )
                base_requirements_ref = _ref_from_mapping(manifest.get("requirements_ref"))
                requirement_patch_ref, revised_requirements_ref = (
                    self.service.architecture.publish_requirement_patch(
                        base_requirements_ref=base_requirements_ref,
                        proposal=dict(requirement_patch),
                        source={
                            "role": "verifier",
                            "stage": (
                                "scenario_verification" if scenario_mode else "module_verification"
                            ),
                            "case": str(finding.get("case_name") or first_failure.case_name),
                            "finding_summary": str(finding.get("summary") or first_failure.summary),
                        },
                        source_artifact_ref=repair_ref,
                        provenance={
                            "owner": "manager",
                            "source_role": "verifier",
                            "source_stage": (
                                "scenario_verification" if scenario_mode else "module_verification"
                            ),
                        },
                    )
                )
        unknown_policy = _manager_unknown_policy(node)
        if unknown_policy.human_waiver_ref:
            manifest_ref = _ref_from_mapping(node.payload.get("architecture_manifest_ref"))
            manifest = self.service.artifacts.read_json(manifest_ref)
            fragment_hashes = {name: digest for name, digest in architecture_manifest_child_refs(manifest)}
            if not self.service.architecture.validate_human_waiver(
                unknown_policy.human_waiver_ref,
                manifest_ref=manifest_ref,
                fragment_hashes=fragment_hashes,
            ):
                unknown_policy = UnknownPolicy(
                    architecture_allows_platform_unknown=unknown_policy.architecture_allows_platform_unknown,
                    assumption_ref=unknown_policy.assumption_ref,
                    hard_or_core_semantics=unknown_policy.hard_or_core_semantics,
                    human_waiver_ref=None,
                )
        verification.submit_verdict(
            node=current,
            verification_ref=report_ref,
            status=status,
            actor=invocation_id,
            unknown_policy=unknown_policy,
            repair_bill_ref=repair_ref,
            finding_fingerprint_value=fingerprint,
            candidate_tree_hash=candidate_digest,
            defect_kind=defect_kind,
            dependency_node_id=dependency_node_id,
            module_node_id=module_node_id,
            scenario_fingerprint=str(node.payload.get("scenario_fingerprint") or ""),
            requirement_patch_ref=requirement_patch_ref,
            revised_requirements_ref=revised_requirements_ref,
        )
        self.repository.record_worker_turn(
            invocation_id=invocation_id,
            fencing_token=fencing_token,
            turn_index=1,
            llm_request_ref=prompt_ref.to_dict(),
            llm_response_ref=terminal_ref.to_dict(),
            tool_summary_ref=report_ref.to_dict(),
            **_recorded_worker_metrics(terminal),
        )
        self.repository.release_lease(lease_resource, invocation_id, fencing_token)
        return {"provider_request_id": invocation_id, "result_artifact_ref": report_ref.to_dict()}

    async def _run_integration(
        self,
        effect: Mapping[str, Any],
        node: AggregateSnapshot,
    ) -> Mapping[str, Any]:
        snapshots = self.repository.list_workflow_snapshots(node.workflow_id)
        node_by_id = {
            item.aggregate_id: item
            for item in snapshots
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == str(node.payload.get("epoch_id") or "")
        }
        ordered_candidates = []
        for dependency_id in list(node.payload.get("dependency_node_ids") or []):
            dependency = node_by_id[str(dependency_id)]
            if dependency.state != "ACCEPTED":
                raise ValueError(f"integration dependency is not accepted: {dependency_id}")
            ordered_candidates.append(
                {
                    "node_run_id": dependency.aggregate_id,
                    "candidate_digest": str(dependency.payload.get("candidate_digest") or ""),
                    "candidate_ref": dict(dependency.payload.get("candidate_ref") or {}),
                }
            )
        manifest_ref = _ref_from_mapping(node.payload.get("architecture_manifest_ref"))
        try:
            adapter = self._execution_adapter(node)
            if adapter == SOFTWARE_GIT_ADAPTER:
                candidate_ref, candidate_digest = IntegrationService(self.service.artifacts).integrate_candidates(
                    integration_worktree=Path(str(node.payload.get("workspace_path") or "")),
                    ordered_candidates=ordered_candidates,
                    architecture_manifest_sha=manifest_ref.sha256,
                )
            elif adapter == ARTIFACT_BUNDLE_ADAPTER:
                candidate_ref, candidate_digest = ArtifactBundleAdapter(
                    self.service.runtime_root,
                    self.service.artifacts,
                ).integrate_candidates(
                    integration_workspace=Path(str(node.payload.get("workspace_path") or "")),
                    ordered_candidates=ordered_candidates,
                    architecture_manifest_sha=manifest_ref.sha256,
                )
            else:
                raise ValueError(f"unsupported integration adapter: {adapter}")
        except (IntegrationOwnershipDefect, ValueError) as exc:
            finding_ref = self.service.artifacts.put_json(
                {
                    "defect_kind": "architecture_defect",
                    "reason": "exclusive ownership contracts produced an integration conflict",
                    "detail": str(exc),
                    "dependency_node_ids": list(node.payload.get("dependency_node_ids") or []),
                },
                artifact_type="IntegrationOwnershipDefectArtifact",
                child_refs=((manifest_ref.sha256, "architecture_manifest"),),
            )
            current = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="PRODUCER_ARCHITECTURE_DEFECT",
                    workflow_id=node.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="minion-v2-integration",
                    expected_version=current.version,
                    idempotency_key=f"integration-ownership:{finding_ref.sha256}",
                    payload={"finding_artifact_ref": finding_ref.to_dict()},
                )
            )
            lease_resource = str(node.payload.get("lease_resource_key") or "")
            invocation_id = str(node.payload.get("active_worker_id") or "")
            fencing_token = int(node.payload.get("fencing_token") or 0)
            self.repository.release_lease(lease_resource, invocation_id, fencing_token)
            return {"result_artifact_ref": finding_ref.to_dict()}
        current = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
        self.repository.dispatch(
            ActionEnvelope(
                action_type="SUBMIT_CANDIDATE",
                workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node.aggregate_id,
                actor="minion-v2-integration",
                expected_version=current.version,
                idempotency_key=f"integration-submit:{candidate_ref.sha256}",
                payload={
                    "fencing_token": int(node.payload.get("fencing_token") or 0),
                    "pending_integration_candidate_ref": candidate_ref.to_dict(),
                    "pending_integration_candidate_digest": candidate_digest,
                },
            )
        )
        return {"result_artifact_ref": candidate_ref.to_dict()}

    async def _stop_node_worker(self, effect: Mapping[str, Any], *, cancel: bool) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        invocation_id = str(node.payload.get("active_worker_id") or "")
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        lease = self.repository.read_lease(lease_resource) if lease_resource else None
        process = self._processes.get(invocation_id)
        process_group = int(dict((lease or {}).get("metadata") or {}).get("process_group_id") or (process.pid if process else 0))
        if process_group and not await terminate_process_group(process_group, timeout_seconds=5.0):
            raise RuntimeError("node worker process group did not stop")
        worktree_text = str(node.payload.get("workspace_path") or "")
        if worktree_text and workspace_has_live_processes(Path(worktree_text)):
            raise RuntimeError("node worker still holds its worktree")
        current = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
        action_type = "CANCEL_CONFIRMED" if cancel or current.state == "CANCEL_REQUESTED" else "PAUSE_CONFIRMED"
        self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node.aggregate_id,
                actor="minion-v2-manager",
                expected_version=current.version,
                idempotency_key=f"effect:{effect['effect_key']}:stopped",
            )
        )
        fencing_token = int(node.payload.get("fencing_token") or 0)
        if lease_resource and invocation_id and fencing_token:
            try:
                self.repository.release_lease(lease_resource, invocation_id, fencing_token)
            except Exception:
                pass
        if cancel:
            self.repository.complete_worker_session(
                coder_session_id(node.aggregate_id),
                status="cancelled",
            )
        return {}

    async def _stop_aggregate_worker(self, effect: Mapping[str, Any], *, cancel: bool) -> Mapping[str, Any]:
        snapshot = self._effect_snapshot(effect)
        invocation_id = str(snapshot.payload.get("active_worker_id") or "")
        lease_resource = str(snapshot.payload.get("lease_resource_key") or "")
        lease = self.repository.read_lease(lease_resource) if lease_resource else None
        process = self._processes.get(invocation_id)
        process_group = int(dict((lease or {}).get("metadata") or {}).get("process_group_id") or (process.pid if process else 0))
        if process_group and not await terminate_process_group(process_group, timeout_seconds=5.0):
            raise RuntimeError("aggregate worker process group did not stop")
        current = self.repository.read_snapshot(snapshot.aggregate_type, snapshot.aggregate_id)
        action_type = "CANCEL_CONFIRMED" if cancel else "PAUSE_CONFIRMED"
        self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=snapshot.workflow_id,
                aggregate_type=snapshot.aggregate_type,
                aggregate_id=snapshot.aggregate_id,
                actor="minion-v2-manager",
                expected_version=current.version,
                idempotency_key=f"effect:{effect['effect_key']}:stopped",
            )
        )
        fencing_token = int(snapshot.payload.get("fencing_token") or 0)
        if lease_resource and invocation_id and fencing_token:
            try:
                self.repository.release_lease(lease_resource, invocation_id, fencing_token)
            except Exception:
                pass
        if cancel and snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
            self.repository.complete_worker_session(
                architect_session_id(snapshot.workflow_id),
                status="cancelled",
            )
        return {}

    async def _resume_aggregate(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        snapshot = self._effect_snapshot(effect)
        if snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
            stage_by_state = {
                "ARCHITECT_QUEUED": "architect",
                "ARCHITECT_RUNNING": "architect",
            }
            stage = stage_by_state.get(snapshot.state)
            if stage:
                if snapshot.state.endswith("_RUNNING"):
                    lease_resource = f"architecture:{snapshot.aggregate_id}:{stage}"
                    lease = self.repository.read_lease(lease_resource)
                    if lease and str(lease.get("owner_id") or "") and _lease_is_live(lease):
                        return {"status": "already_running", "active_worker_id": str(lease["owner_id"])}
                resumed_effect = {**dict(effect), "payload": {**dict(effect.get("payload") or {}), "stage": stage}}
                return await self._run_architecture_stage(resumed_effect)
            if snapshot.state in {"REVIEW_QUEUED", "REVIEWING"}:
                return await self._run_architecture_review(effect)
            if snapshot.state == "ARCHITECT_QUIESCING":
                return await self._quiesce_architect(effect)
            if snapshot.state == "ARCHITECT_SNAPSHOTTING":
                return await self._snapshot_architecture(effect)
        if snapshot.aggregate_type == AggregateType.STANDALONE_REVIEW:
            if snapshot.state == "REVIEW_QUEUED":
                return self._admit_standalone_review(effect)
            if snapshot.state == "REPORT_READY":
                return await self._publish_standalone_report(effect)
        return {}

    def _resume_node(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        if node.state == "QUEUED":
            if str(node.payload.get("node_kind") or "") == "verification":
                return self._admit_node_worker(
                    effect,
                    action_type="START_SCENARIO_VERIFICATION",
                    role="scenario_verifier",
                )
            return self._admit_node_worker(effect, action_type="START_PRODUCING", role="producer")
        if node.state == "REVIEW_QUEUED":
            return self._admit_node_worker(effect, action_type="START_REVIEW", role="reviewer")
        if node.state == "REPAIR_QUEUED":
            return self._admit_node_worker(effect, action_type="START_REPAIR", role="repair")
        if node.state == "VERIFY_PREPARING":
            current = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="RETRY_VERIFICATION_PREPARATION",
                    workflow_id=node.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="minion-v2-recovery",
                    expected_version=current.version,
                    idempotency_key=f"effect:{effect['effect_key']}:retry-verification-preparation",
                )
            )
        return {}

    @staticmethod
    def _execution_adapter(node: AggregateSnapshot) -> str:
        return str(node.payload.get("execution_adapter") or SOFTWARE_GIT_ADAPTER)

    def _workspace_fingerprint(self, node: AggregateSnapshot, workspace: Path) -> str:
        if self._execution_adapter(node) == ARTIFACT_BUNDLE_ADAPTER:
            return artifact_tree_fingerprint(workspace)
        return workspace_content_fingerprint(workspace)

    async def _publish_final_deliverable(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        epoch = self._effect_snapshot(effect)
        snapshots = self.repository.list_workflow_snapshots(epoch.workflow_id)
        epoch_nodes = [
            item
            for item in snapshots
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
        ]
        integration = next(
            (
                item
                for item in snapshots
                if item.aggregate_type == AggregateType.DAG_NODE_RUN
                and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
                and str(item.payload.get("node_kind") or "") == "integration"
                and item.state == "ACCEPTED"
            ),
            None,
        )
        if integration is None:
            return await self._publish_skeleton_candidate_union(effect, epoch, epoch_nodes)
        verification_ref = _ref_from_mapping(integration.payload.get("verification_artifact_ref"))
        adapter = self._execution_adapter(integration)
        if adapter == SOFTWARE_GIT_ADAPTER:
            deliverable_ref = IntegrationService(self.service.artifacts).publish_final_deliverable(
                repository=Path(str(integration.payload.get("workspace_path") or "")),
                integration_candidate_digest=str(integration.payload.get("candidate_digest") or ""),
                branch_name=f"pal/v2/{epoch.workflow_id}",
                verification_ref=verification_ref,
            )
        elif adapter == ARTIFACT_BUNDLE_ADAPTER:
            deliverable_ref = ArtifactBundleAdapter(
                self.service.runtime_root,
                self.service.artifacts,
            ).publish_deliverable(
                workflow_id=epoch.workflow_id,
                candidate_ref=dict(integration.payload.get("candidate_ref") or {}),
                verification_ref=verification_ref,
            )
        else:
            raise ValueError(f"unsupported publisher adapter: {adapter}")
        current = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch.aggregate_id)
        self.repository.dispatch(
            ActionEnvelope(
                action_type="FINAL_DELIVERABLE_PUBLISHED",
                workflow_id=epoch.workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id=epoch.aggregate_id,
                actor="minion-v2-manager",
                expected_version=current.version,
                idempotency_key=f"publish:{deliverable_ref.sha256}",
                payload={"published_deliverable_ref": deliverable_ref.to_dict()},
            )
        )
        return {"result_artifact_ref": deliverable_ref.to_dict()}

    async def _publish_skeleton_candidate_union(
        self,
        effect: Mapping[str, Any],
        epoch: AggregateSnapshot,
        nodes: list[AggregateSnapshot],
    ) -> Mapping[str, Any]:
        implementation = [item for item in nodes if str(item.payload.get("node_kind") or "unit") == "unit"]
        verification = [item for item in nodes if str(item.payload.get("node_kind") or "") == "verification"]
        if not implementation or not verification or any(item.state != "ACCEPTED" for item in nodes):
            raise ValueError("final candidate union requires all implementation and verification nodes ACCEPTED")
        verification_refs: list[dict[str, Any]] = []
        scenario_fingerprints: dict[str, str] = {}
        for node in verification:
            ref = dict(node.payload.get("verification_artifact_ref") or {})
            report = dict(self.service.artifacts.read_json(ref))
            expected = str(node.payload.get("scenario_fingerprint") or "")
            if not expected or str(report.get("scenario_fingerprint") or "") != expected:
                raise ValueError(f"Verification Node {node.payload.get('module_name')} has stale scenario evidence")
            verification_refs.append(ref)
            scenario_fingerprints[str(node.payload.get("module_name") or node.aggregate_id)] = expected
        ordered = _topological_implementation_nodes(implementation)
        common_git_dir = Path(str(ordered[0].payload.get("common_git_dir") or ""))
        skeleton_sha = str(epoch.payload.get("skeleton_commit_sha") or "")
        if not common_git_dir.is_dir() or not skeleton_sha:
            raise ValueError("candidate union requires the accepted skeleton Git repository")
        publish_worktree = common_git_dir.parent / "worktrees" / "__publish__"
        branch = f"v2/{_safe_component(epoch.aggregate_id)}/publish"
        if not publish_worktree.exists():
            publish_worktree.parent.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                [
                    "git",
                    f"--git-dir={common_git_dir}",
                    "worktree",
                    "add",
                    "-b",
                    branch,
                    str(publish_worktree),
                    skeleton_sha,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr or completed.stdout or "failed to create publish worktree")
        else:
            subprocess.run(["git", "-C", str(publish_worktree), "reset", "--hard", skeleton_sha], check=True)
            subprocess.run(["git", "-C", str(publish_worktree), "clean", "-fd"], check=True)
        candidates = [
            {
                "module_name": str(node.payload.get("module_name") or node.payload.get("unit_id") or ""),
                "candidate_digest": str(node.payload.get("candidate_digest") or ""),
                "candidate_ref": dict(node.payload.get("candidate_ref") or {}),
            }
            for node in ordered
        ]
        service = CandidateUnionService(self.service.artifacts)
        try:
            union_ref, commit_sha = service.compose(
                publish_worktree=publish_worktree,
                ordered_candidates=candidates,
                architecture_skeleton_ref=dict(epoch.payload.get("architecture_manifest_ref") or {}),
            )
        except CandidateUnionConflict as exc:
            finding_ref = self.service.artifacts.put_json(
                {
                    "finding_kind": "architecture_defect",
                    "summary": str(exc),
                    "affected_modules": [item["module_name"] for item in candidates],
                    "source": "final_candidate_union",
                },
                artifact_type="ArchitectureFindingArtifact",
            )
            current = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch.aggregate_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="REPLAN_REQUESTED",
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch.aggregate_id,
                    actor="minion-v2-manager",
                    expected_version=current.version,
                    idempotency_key=f"union-conflict:{finding_ref.sha256}",
                    payload={"finding_artifact_ref": finding_ref.to_dict()},
                )
            )
            return {"result_artifact_ref": finding_ref.to_dict()}
        deliverable_ref = service.publish(
            repository=publish_worktree,
            union_ref=union_ref,
            commit_sha=commit_sha,
            branch_name=f"pal/v2/{epoch.workflow_id}",
            verification_refs=verification_refs,
            scenario_fingerprints=scenario_fingerprints,
        )
        current = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch.aggregate_id)
        self.repository.dispatch(
            ActionEnvelope(
                action_type="FINAL_DELIVERABLE_PUBLISHED",
                workflow_id=epoch.workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id=epoch.aggregate_id,
                actor="minion-v2-manager",
                expected_version=current.version,
                idempotency_key=f"publish:{deliverable_ref.sha256}",
                payload={"published_deliverable_ref": deliverable_ref.to_dict()},
            )
        )
        return {"result_artifact_ref": deliverable_ref.to_dict()}

    async def _run_standalone_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        review = self._effect_snapshot(effect)
        review = await self._ensure_standalone_review_lease(review)
        invocation_id = str(review.payload.get("active_worker_id") or "")
        lease_resource = str(review.payload.get("lease_resource_key") or "")
        fencing_token = int(review.payload.get("fencing_token") or 0)
        request_ref = _ref_from_mapping(review.payload.get("review_request_ref"))
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, review.workflow_id)
        request = workflow_request_from_snapshot(self.service, workflow)
        request_record = self.repository.read_artifact_record(request_ref.sha256)
        skeleton_review = bool(
            request_record
            and str(request_record.get("artifact_type") or "") == ARCHITECTURE_SKELETON_ARTIFACT
        )
        if skeleton_review:
            skeleton = self.service.artifacts.read_json(request_ref)
            requirements_ref = _ref_from_mapping(skeleton.get("requirements_ref"))
            requirements = requirements_semantic_view(
                self.service.artifacts.read_json(requirements_ref)
            )
            review_repo = self.service.skeleton.provision_review_worktree(
                artifact=skeleton,
                review_name=f"standalone-{review.aggregate_id}",
            )
            review_scratch = (
                self.service.runtime_root
                / "data"
                / "minion"
                / "v2"
                / "standalone-reviews"
                / _safe_component(review.aggregate_id)
                / "review-scratch"
            )
            review_scratch.mkdir(parents=True, exist_ok=True)
            base_sha = str(skeleton.get("skeleton_commit_sha") or "")
            submission = dict(skeleton.get("submission") or {})
            review_view_ref = self.service.artifacts.put_json(
                {
                    "review_goal": str(request.get("goal") or "Review the accepted software architecture and implementation."),
                    "requirements": requirements,
                    "modules": dict(submission.get("modules") or {}),
                    "integration": dict(submission.get("integration") or {}),
                },
                artifact_type="StandaloneSkeletonReviewViewArtifact",
                provenance={"owner": "manager", "audience": "standalone_reviewer"},
                child_refs=((request_ref.sha256, "architecture_skeleton"),),
            )
            reviewer_inputs = {"review_request": review_view_ref}
        else:
            workspace = dict(request.get("workspace") or {})
            repo_path = str(workspace.get("repo_path") or workspace.get("cwd") or self.service.runtime_root)
            review_repo, review_scratch, base_sha = _prepare_standalone_review_workspace(
                self.service.runtime_root,
                review.aggregate_id,
                Path(repo_path),
            )
            reviewer_inputs = {"review_request": request_ref}
        terminal, prompt_ref, terminal_ref = await self._run_profile(
            effect=effect,
            snapshot=review,
            invocation_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            profile=self._profile_for_role_or(review.workflow_id, "reviewer", fallback="verifier"),
            role_override="reviewer",
            instruction="Perform the requested standalone review. Report evidence-grounded findings and do not modify the target. Repair is a separate explicit workflow.",
            reference_refs=reviewer_inputs,
            workspace_override={"kind": "existing_repo", "repo_path": str(review_repo), "project_name": "standalone-review"},
            prepare_workspace=False,
        )
        plan = _primary_json_output(terminal)
        _validate_semantic_verification_plan_shape(plan, standalone=True)
        _reject_manager_identity_fields(plan, owner="Standalone Reviewer output")
        case_specs = _verification_case_specs(plan.get("cases"))
        findings = _standalone_review_findings(plan, case_specs)
        if skeleton_review:
            _validate_verifier_requirement_refs(
                work_view=self.service.artifacts.read_json(review_view_ref),
                cases=case_specs,
                findings=findings,
            )
        verification_policy = self._workflow_policy(review.workflow_id, "verification")
        case_timeout = float(verification_policy.get("case_timeout_seconds") or 300)
        results = [
            VerificationCaseRunner(self.service.artifacts).run(
                item,
                cwd=review_repo,
                environment={"PAL_MINION_REVIEW_SCRATCH": str(review_scratch)},
                timeout_seconds=case_timeout,
            )
            for item in case_specs
        ]
        test_workspace_ref = self._publish_verification_workspace(
            review_worktree=review_repo,
            review_scratch=review_scratch,
            candidate_digest=base_sha,
            execution_adapter=SOFTWARE_GIT_ADAPTER,
        )
        status = _standalone_review_status(plan, results)
        report_ref = self.service.artifacts.put_json(
            {
                "schema_version": "1",
                "status": status.value,
                "cases": [item.to_dict() for item in results],
                "findings": [semantic_finding_payload(item) for item in findings],
                "reviewer_summary": str(plan.get("reviewer_summary") or ""),
                "scope": plan.get("scope") or {},
                "reviewed_surfaces": list(plan.get("reviewed_surfaces") or []),
                "commands_or_lsp_evidence": list(plan.get("commands_or_lsp_evidence") or []),
                "test_gaps": list(plan.get("test_gaps") or []),
                "unreviewed_surfaces": list(plan.get("unreviewed_surfaces") or []),
                "residual_risk": list(plan.get("residual_risk") or []),
            },
            artifact_type="StandaloneReviewReportArtifact",
            child_refs=(
                (request_ref.sha256, "review_request"),
                (test_workspace_ref.sha256, "test_workspace"),
            ),
        )
        self.repository.record_worker_turn(
            invocation_id=invocation_id,
            fencing_token=fencing_token,
            turn_index=1,
            llm_request_ref=prompt_ref.to_dict(),
            llm_response_ref=terminal_ref.to_dict(),
            tool_summary_ref=report_ref.to_dict(),
            **_recorded_worker_metrics(terminal),
        )
        current = self.repository.read_snapshot(AggregateType.STANDALONE_REVIEW, review.aggregate_id)
        self.repository.dispatch(
            ActionEnvelope(
                action_type="REPORT_PRODUCED",
                workflow_id=review.workflow_id,
                aggregate_type=AggregateType.STANDALONE_REVIEW,
                aggregate_id=review.aggregate_id,
                actor=invocation_id,
                expected_version=current.version,
                idempotency_key=f"standalone-report:{report_ref.sha256}",
                payload={"verification_artifact_ref": report_ref.to_dict()},
            )
        )
        self.repository.release_lease(lease_resource, invocation_id, fencing_token)
        return {"result_artifact_ref": report_ref.to_dict()}

    async def _ensure_standalone_review_lease(self, review: AggregateSnapshot) -> AggregateSnapshot:
        invocation_id = str(review.payload.get("active_worker_id") or "")
        lease_resource = str(review.payload.get("lease_resource_key") or f"standalone-review:{review.aggregate_id}")
        fencing_token = int(review.payload.get("fencing_token") or 0)
        if invocation_id and fencing_token:
            try:
                self.repository.assert_fencing_token(lease_resource, invocation_id, fencing_token)
                return review
            except StaleFencingToken:
                pass
        invocation_id = invocation_id or f"inv_{hashlib.sha256(f'{review.aggregate_id}:review'.encode()).hexdigest()[:24]}"
        previous = self.repository.read_lease(lease_resource)
        if previous is not None and str(previous.get("owner_id") or "") and _lease_is_live(previous):
            raise LeaseConflict(f"standalone review lease is active under {previous.get('owner_id')}")
        process_group = int(dict((previous or {}).get("metadata") or {}).get("process_group_id") or 0)
        if process_group and not await terminate_process_group(process_group, timeout_seconds=5.0):
            raise RuntimeError("expired standalone reviewer process group could not be reaped before rebind")
        lease = self.repository.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": review.workflow_id,
                "aggregate_type": AggregateType.STANDALONE_REVIEW.value,
                "aggregate_id": review.aggregate_id,
                "role": "reviewer",
            },
        )
        return self.repository.dispatch(
            ActionEnvelope(
                action_type="REBIND_REVIEWER",
                workflow_id=review.workflow_id,
                aggregate_type=AggregateType.STANDALONE_REVIEW,
                aggregate_id=review.aggregate_id,
                actor="minion-v2-recovery",
                expected_version=review.version,
                idempotency_key=f"rebind:{review.aggregate_id}:reviewer:{lease.fencing_token}",
                payload={
                    "fencing_token": lease.fencing_token,
                    "active_worker_id": invocation_id,
                    "lease_resource_key": lease_resource,
                },
            )
        ).snapshot

    async def _publish_standalone_report(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        review = self._effect_snapshot(effect)
        report_ref = dict(review.payload.get("verification_artifact_ref") or {})
        report = self.service.artifacts.read_json(report_ref)
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, review.workflow_id)
        workflow_request = workflow_request_from_snapshot(self.service, workflow)
        if self.publish_human_review is not None:
            await self.publish_human_review(
                {
                    "workflow_id": review.workflow_id,
                    "standalone_review_id": review.aggregate_id,
                    "report_ref": report_ref,
                    "route": dict(workflow.payload.get("control_route") or {}),
                    "summary": _compile_standalone_review_markdown(report),
                }
            )
        current = self.repository.read_snapshot(AggregateType.STANDALONE_REVIEW, review.aggregate_id)
        action_type = "ACKNOWLEDGE_REPORT"
        payload: dict[str, Any] = {}
        if (
            str(workflow_request.get("operation") or "") == "review_and_repair"
            and str(report.get("status") or "") == VerificationStatus.FAIL
        ):
            if self._uses_git_skeleton(review.workflow_id):
                manifest_ref = _ref_from_mapping(review.payload.get("review_request_ref"))
                record = self.repository.read_artifact_record(manifest_ref.sha256)
                if record is None or str(record.get("artifact_type") or "") != ARCHITECTURE_SKELETON_ARTIFACT:
                    raise ValueError(
                        "software review_and_repair requires an ArchitectureSkeletonArtifact"
                    )
                repair_bill_ref = self._publish_standalone_repair_bill(
                    report_ref=report_ref,
                    manifest_ref=manifest_ref,
                )
            else:
                manifest_ref = self._compile_contract_review_repair_manifest(
                    review, workflow_request, report_ref
                )
                repair_bill_ref = None
            action_type = "HANDOFF_REPAIR"
            payload["architecture_manifest_ref"] = manifest_ref.to_dict()
            if repair_bill_ref is not None:
                payload["repair_bill_ref"] = repair_bill_ref.to_dict()
        self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=review.workflow_id,
                aggregate_type=AggregateType.STANDALONE_REVIEW,
                aggregate_id=review.aggregate_id,
                actor="minion-v2-manager",
                expected_version=current.version,
                idempotency_key=f"effect:{effect['effect_key']}:ack",
                payload=payload,
            )
        )
        return {"result_artifact_ref": report_ref}

    def _publish_standalone_repair_bill(
        self,
        *,
        report_ref: Mapping[str, Any],
        manifest_ref: ArtifactRef,
    ) -> ArtifactRef:
        report = dict(self.service.artifacts.read_json(report_ref))
        findings = [dict(item or {}) for item in list(report.get("findings") or [])]
        if not findings:
            raise ValueError("review_and_repair FAIL requires at least one semantic finding")
        artifact = dict(self.service.artifacts.read_json(manifest_ref))
        modules = dict(dict(artifact.get("submission") or {}).get("modules") or {})
        if len(modules) != 1:
            raise ValueError("review_and_repair requires exactly one bounded module")
        module_name = next(iter(modules))
        finding = findings[0]
        payload = {
            "schema_version": "1",
            "module_name": module_name,
            "defect_kind": DefectKind.MODULE.value,
            "severity": str(finding.get("severity") or "major"),
            "finding_section": str(finding.get("finding_section") or "implementation"),
            "finding_summary": str(finding.get("summary") or "Standalone review failed."),
            "failure_reason": str(finding.get("failure_reason") or ""),
            "case_name": str(finding.get("case") or ""),
            "requirements": [dict(item) for item in list(finding.get("requirements") or [])],
            "locations": [dict(item) for item in list(finding.get("locations") or [])],
            "invariants": [str(item) for item in list(finding.get("invariants") or [])],
            "expected": "The accepted skeleton contract and Requirements are satisfied.",
            "actual": str(finding.get("failure_reason") or finding.get("summary") or "Review failed."),
            "suggested_repair_boundary": [
                str(item) for item in list(finding.get("suggested_repair_boundary") or [])
            ],
            "regression_test_obligation": {
                "instruction": "Reproduce this standalone finding before repair and preserve the probe as a regression."
            },
        }
        return self.service.artifacts.put_json(
            payload,
            artifact_type="RepairBillArtifact",
            provenance={"owner": "manager", "source": "standalone_review"},
            child_refs=((str(report_ref.get("sha256") or ""), "standalone_review"),),
        )

    def _compile_contract_review_repair_manifest(
        self,
        review: AggregateSnapshot,
        workflow_request: Mapping[str, Any],
        report_ref: Mapping[str, Any],
    ) -> ArtifactRef:
        review_request_ref = _ref_from_mapping(review.payload.get("review_request_ref"))
        review_request = self.service.artifacts.read_json(review_request_ref)
        seed = dict(review_request.get("unit_contract_seed") or {})
        if not seed:
            raise ValueError("review_and_repair requires ReviewRequest.unit_contract_seed; reviewer may not invent the repair contract")
        report = self.service.artifacts.read_json(report_ref)
        requirements_source = list(review_request.get("requirements") or [])
        if not requirements_source:
            requirements_source = [
                {
                    "requirement_id": f"RR-{index + 1}",
                    "statement": str(item.get("summary") or item),
                    "strength": "hard",
                    "source_refs": [f"review:{review.aggregate_id}"],
                }
                for index, item in enumerate(list(report.get("findings") or []))
            ]
        requirements_ref = self.service.architecture.publish_requirements({"requirements": requirements_source})
        requirement_ids = [
            str(item["requirement_id"])
            for item in self.service.artifacts.read_json(requirements_ref)["requirements"]
        ]
        unit_contract = {
            **seed,
            "unit_id": str(seed.get("unit_id") or "review_repair"),
            "requirement_ids": requirement_ids,
        }
        module_ref = self.service.architecture.publish_unit_contract(unit_contract)
        constraints = self.service.architecture.publish_fragment(
            list(workflow_request.get("constraints") or []),
            artifact_type="GlobalConstraintsArtifact",
        )
        decisions = self.service.architecture.publish_fragment([], artifact_type="DesignDecisionsArtifact")
        gates = self.service.architecture.publish_fragment([], artifact_type="ArchitectureGateChecksArtifact")
        topology = self.service.architecture.publish_fragment(
            {"depends_on": {unit_contract["unit_id"]: []}},
            artifact_type="TopologyArtifact",
        )
        integration = self.service.architecture.publish_fragment(
            {"node_kind": "integration", "depends_on": [unit_contract["unit_id"]]},
            artifact_type="IntegrationContractArtifact",
        )
        assumptions = self.service.architecture.publish_fragment({"assumptions": []}, artifact_type="AssumptionLedgerArtifact")
        risks = self.service.architecture.publish_fragment({"risks": []}, artifact_type="RiskLedgerArtifact")
        return self.service.architecture.publish_manifest(
            {
                "requirements_ref": requirements_ref.to_dict(),
                "global_constraints_ref": constraints.to_dict(),
                "design_decisions_ref": decisions.to_dict(),
                "gate_checks_ref": gates.to_dict(),
                "unit_contract_refs": [module_ref.to_dict()],
                "cross_unit_contract_refs": [],
                "topology_ref": topology.to_dict(),
                "integration_contract_ref": integration.to_dict(),
                "assumption_ledger_ref": assumptions.to_dict(),
                "risk_ledger_ref": risks.to_dict(),
            },
            provenance={"compiled_from_review_request": review_request_ref.sha256, "review_report": report_ref.get("sha256")},
        )

    async def _run_architecture_stage(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        initial_revision = self._effect_snapshot(effect)
        if self._uses_git_skeleton(initial_revision.workflow_id):
            return await self._run_skeleton_architecture_stage(effect, initial_revision)
        stage = str(dict(effect.get("payload") or {}).get("stage") or "")
        if stage not in _ARCHITECTURE_STAGE_CONFIG:
            raise ValueError(f"unknown architecture stage: {stage}")
        role, start_action = _ARCHITECTURE_STAGE_CONFIG[stage]
        running_state = "ARCHITECT_RUNNING"
        rebind_action = "REBIND_ARCHITECT"
        revision = self._effect_snapshot(effect)
        profile = self._profile_for_role(revision.workflow_id, role)
        if self._architecture_worker_suppressed(revision, running_state=running_state, start_action=start_action):
            return {"status": "superseded"}
        invocation_id = architect_session_id(revision.workflow_id)
        lease_resource = f"architecture:{revision.aggregate_id}:{stage}"
        if revision.state == running_state:
            active_lease = self.repository.read_lease(lease_resource)
            if active_lease and str(active_lease.get("owner_id") or "") and _lease_is_live(active_lease):
                return {"status": "already_running", "active_worker_id": str(active_lease["owner_id"])}
        lease = self.repository.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={"workflow_id": revision.workflow_id, "aggregate_id": revision.aggregate_id, "stage": stage},
        )
        try:
            if start_action in self.repository.engine.legal_actions(AggregateType.ARCHITECTURE_REVISION, revision.state):
                revision = self.repository.dispatch(
                    ActionEnvelope(
                        action_type=start_action,
                        workflow_id=revision.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision.aggregate_id,
                        actor=invocation_id,
                        expected_version=revision.version,
                        idempotency_key=f"effect:{effect['effect_key']}:start",
                        payload={
                            "fencing_token": lease.fencing_token,
                            "active_worker_id": invocation_id,
                            "lease_resource_key": lease_resource,
                        },
                    )
                ).snapshot
            elif revision.state == running_state:
                revision = self.repository.dispatch(
                    ActionEnvelope(
                        action_type=rebind_action,
                        workflow_id=revision.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision.aggregate_id,
                        actor=invocation_id,
                        expected_version=revision.version,
                        idempotency_key=f"effect:{effect['effect_key']}:rebind:{lease.fencing_token}",
                        payload={
                            "fencing_token": lease.fencing_token,
                            "active_worker_id": invocation_id,
                            "lease_resource_key": lease_resource,
                        },
                    )
                ).snapshot
            prompt, reference_refs = self._architecture_stage_prompt(stage, revision)
            terminal, prompt_ref, terminal_ref = await self._run_profile(
                effect=effect,
                snapshot=revision,
                invocation_id=invocation_id,
                lease_resource=lease_resource,
                fencing_token=lease.fencing_token,
                profile=profile,
                role_override=role,
                instruction=prompt,
                reference_refs=reference_refs,
                workspace_override=None,
                prepare_workspace=True,
            )
            contract = _named_json_output(terminal, "architecture_bundle.json")
            requirements_ref = _ref_from_mapping(revision.payload.get("requirements_ref"))
            revision_base_manifest_ref = self._revision_input_base_manifest_ref(revision)
            result_ref = self._publish_planning_bundle(
                revision,
                contract,
                requirements_ref=requirements_ref,
                base_manifest_ref=revision_base_manifest_ref,
            )
            current = self.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="ARCHITECT_COMPLETED",
                    workflow_id=revision.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision.aggregate_id,
                    actor="minion-v2-architect",
                    expected_version=current.version,
                    idempotency_key=f"architect-output:{revision.aggregate_id}:{result_ref.sha256}",
                    payload={
                        "requirements_ref": requirements_ref.to_dict(),
                        "architecture_manifest_ref": result_ref.to_dict(),
                        **(
                            {"revision_base_manifest_ref": revision_base_manifest_ref.to_dict()}
                            if revision_base_manifest_ref is not None
                            else {}
                        ),
                    },
                )
            )
            self.repository.record_worker_turn(
                invocation_id=invocation_id,
                fencing_token=lease.fencing_token,
                turn_index=_worker_session_turn_index(terminal),
                llm_request_ref=prompt_ref.to_dict(),
                llm_response_ref=terminal_ref.to_dict(),
                tool_summary_ref=result_ref.to_dict(),
                **_recorded_worker_metrics(terminal),
            )
            return {"provider_request_id": invocation_id, "result_artifact_ref": result_ref.to_dict()}
        finally:
            try:
                self.repository.release_lease(lease_resource, invocation_id, lease.fencing_token)
            except Exception:
                pass

    async def _run_skeleton_architecture_stage(
        self,
        effect: Mapping[str, Any],
        revision: AggregateSnapshot,
    ) -> Mapping[str, Any]:
        if self._architecture_worker_suppressed(
            revision,
            running_state="ARCHITECT_RUNNING",
            start_action="START_ARCHITECT",
        ):
            return {"status": "superseded"}
        invocation_id = architect_session_id(revision.workflow_id)
        lease_resource = f"architecture:{revision.aggregate_id}:writer"
        if revision.state == "ARCHITECT_RUNNING":
            active_lease = self.repository.read_lease(lease_resource)
            if active_lease and str(active_lease.get("owner_id") or "") and _lease_is_live(active_lease):
                return {"status": "already_running", "active_worker_id": str(active_lease["owner_id"])}
        requirements_ref = _ref_from_mapping(revision.payload.get("requirements_ref"))
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            raise ValueError("architecture revision has no workflow")
        request = workflow_request_from_snapshot(self.service, workflow)
        base_manifest_ref = self._revision_input_base_manifest_ref(revision)
        base_artifact: Mapping[str, Any] | None = None
        if base_manifest_ref is not None:
            record = self.repository.read_artifact_record(base_manifest_ref.sha256)
            if record is None or str(record.get("artifact_type") or "") != ARCHITECTURE_SKELETON_ARTIFACT:
                raise ValueError("SWE architecture revision requires an ArchitectureSkeletonArtifact baseline")
            base_artifact = self.service.artifacts.read_json(base_manifest_ref)
        architecture_workspace = self.service.skeleton.provision_architecture_workspace(
            workflow_id=revision.workflow_id,
            revision_name=revision.aggregate_id,
            workspace=dict(request.get("workspace") or {}),
            requirements_ref=requirements_ref,
            base_artifact=base_artifact,
        )
        lease = self.repository.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": revision.workflow_id,
                "aggregate_id": revision.aggregate_id,
                "stage": "architect",
                "workspace_path": str(architecture_workspace.worktree),
            },
        )
        try:
            start_action = (
                "START_ARCHITECT"
                if "START_ARCHITECT"
                in self.repository.engine.legal_actions(AggregateType.ARCHITECTURE_REVISION, revision.state)
                else "REBIND_ARCHITECT"
            )
            revision = self.repository.dispatch(
                ActionEnvelope(
                    action_type=start_action,
                    workflow_id=revision.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision.aggregate_id,
                    actor=invocation_id,
                    expected_version=revision.version,
                    idempotency_key=f"effect:{effect['effect_key']}:{start_action.lower()}:{lease.fencing_token}",
                    payload={
                        "fencing_token": lease.fencing_token,
                        "active_worker_id": invocation_id,
                        "lease_resource_key": lease_resource,
                        "architecture_workspace_path": str(architecture_workspace.worktree),
                        "architecture_common_git_dir": str(architecture_workspace.common_git_dir),
                        "architecture_base_sha": architecture_workspace.base_sha,
                        "architecture_base_tree_sha": architecture_workspace.base_tree_sha,
                        "workspace_snapshot_ref": architecture_workspace.workspace_snapshot_ref.to_dict(),
                    },
                )
            ).snapshot
            requirements_view_ref = self.service.artifacts.put_json(
                requirements_semantic_view(self.service.artifacts.read_json(requirements_ref)),
                artifact_type="RequirementsSemanticViewArtifact",
                provenance={"owner": "manager", "audience": "architect"},
                child_refs=((requirements_ref.sha256, "requirements"),),
            )
            references: dict[str, ArtifactRef] = {"requirements": requirements_view_ref}
            finding_value = revision.payload.get("finding_artifact_ref") or revision.payload.get("replan_finding_ref")
            if finding_value:
                references["revision_finding"] = _ref_from_mapping(finding_value)
            if revision.payload.get("edit_instruction_ref"):
                references["edit_instruction"] = _ref_from_mapping(revision.payload["edit_instruction_ref"])
            for index, raw_reference in enumerate(list(request.get("references") or [])):
                reference = dict(raw_reference or {})
                path = str(reference.get("path") or "").strip()
                if path and Path(path).expanduser().exists():
                    name = str(reference.get("name") or f"user_reference_{index + 1}").strip()
                    references[f"user_{name}"] = _path_pseudo_ref(path, name)
            instruction = (
                "Design the requested software architecture in the bound writable worktree. Requirements is the immutable product truth. "
                "Write contract-level code skeletons, a Construction DAG, directional contract-consumption references, and real scenario-specific Verification Nodes. "
                "A universal integration/join is forbidden unless a real product entrypoint requires that exact combination. "
                "Do not implement behavior, algorithms, mapping tables, SDK call sequences, or complete tests."
            )
            if base_manifest_ref is not None:
                instruction += (
                    " This is a revision based on the existing skeleton. Modify only locations named by revision_finding or the explicit edit instruction; "
                    "preserve every unrelated declaration, contract, path scope, and dependency."
                )
            terminal, prompt_ref, terminal_ref = await self._run_profile(
                effect=effect,
                snapshot=revision,
                invocation_id=invocation_id,
                lease_resource=lease_resource,
                fencing_token=lease.fencing_token,
                profile=self._profile_for_role(revision.workflow_id, "architect"),
                role_override="architect",
                instruction=instruction,
                reference_refs=references,
                workspace_override={
                    "kind": "existing_repo",
                    "repo_path": str(architecture_workspace.worktree),
                    "project_name": "architecture-skeleton",
                    "architecture_skeleton_mode": True,
                },
                prepare_workspace=False,
            )
            submission = _named_json_output(terminal, "architecture_submission.json")
            submission_ref = self.service.artifacts.put_json(
                submission,
                artifact_type="ArchitectureSkeletonSubmissionIntentArtifact",
                provenance={"role": "architect"},
                child_refs=((requirements_ref.sha256, "requirements"),),
            )
            self.repository.record_worker_turn(
                invocation_id=invocation_id,
                fencing_token=lease.fencing_token,
                turn_index=_worker_session_turn_index(terminal),
                llm_request_ref=prompt_ref.to_dict(),
                llm_response_ref=terminal_ref.to_dict(),
                tool_summary_ref=submission_ref.to_dict(),
                **_recorded_worker_metrics(terminal),
            )
            current = self.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="ARCHITECT_SUBMITTED",
                    workflow_id=revision.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision.aggregate_id,
                    actor=invocation_id,
                    expected_version=current.version,
                    idempotency_key=f"architect-submit:{revision.aggregate_id}:{submission_ref.sha256}",
                    payload={
                        "requirements_ref": requirements_ref.to_dict(),
                        "pending_architecture_submission_ref": submission_ref.to_dict(),
                        "fencing_token": lease.fencing_token,
                        "architecture_workspace_path": str(architecture_workspace.worktree),
                        "architecture_common_git_dir": str(architecture_workspace.common_git_dir),
                        "architecture_base_sha": architecture_workspace.base_sha,
                        "architecture_base_tree_sha": architecture_workspace.base_tree_sha,
                        "workspace_snapshot_ref": architecture_workspace.workspace_snapshot_ref.to_dict(),
                        **(
                            {"revision_base_manifest_ref": base_manifest_ref.to_dict()}
                            if base_manifest_ref is not None
                            else {}
                        ),
                    },
                )
            )
            return {"provider_request_id": invocation_id, "result_artifact_ref": submission_ref.to_dict()}
        finally:
            try:
                self.repository.release_lease(lease_resource, invocation_id, lease.fencing_token)
            except Exception:
                pass

    async def _ensure_architecture_effect_lease(
        self,
        revision: AggregateSnapshot,
        *,
        action_type: str,
    ) -> AggregateSnapshot:
        invocation_id = architect_session_id(revision.workflow_id)
        lease_resource = f"architecture:{revision.aggregate_id}:writer"
        fencing_token = int(revision.payload.get("fencing_token") or 0)
        active_worker = str(revision.payload.get("active_worker_id") or invocation_id)
        if fencing_token:
            try:
                self.repository.assert_fencing_token(lease_resource, active_worker, fencing_token)
                if revision.state == "ARCHITECT_SNAPSHOTTING" and not self._worktree_locks.is_held(
                    revision.aggregate_id
                ):
                    workspace = Path(str(revision.payload.get("architecture_workspace_path") or ""))
                    if workspace_has_live_processes(workspace):
                        raise RuntimeError("a live process still holds the architecture worktree")
                    expected = str(revision.payload.get("workspace_fingerprint") or "")
                    current = workspace_content_fingerprint(workspace)
                    if not expected or current != expected:
                        raise RuntimeError("architecture worktree changed while snapshot worker was unavailable")
                    self._worktree_locks.acquire(revision.aggregate_id, workspace)
                return revision
            except StaleFencingToken:
                pass
        previous = self.repository.read_lease(lease_resource)
        if previous is not None and str(previous.get("owner_id") or "") and _lease_is_live(previous):
            raise LeaseConflict(f"architecture effect lease is active under {previous.get('owner_id')}")
        metadata = dict((previous or {}).get("metadata") or {})
        process_group = int(metadata.get("process_group_id") or 0)
        if process_group and not await terminate_process_group(process_group, timeout_seconds=5.0):
            raise RuntimeError("expired architect process group could not be reaped")
        workspace = Path(str(revision.payload.get("architecture_workspace_path") or ""))
        if workspace_has_live_processes(workspace):
            raise RuntimeError("expired architect still holds the architecture worktree")
        lease = self.repository.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": revision.workflow_id,
                "aggregate_id": revision.aggregate_id,
                "stage": "architecture_snapshot",
                "workspace_path": str(workspace),
            },
        )
        rebound = self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=revision.workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision.aggregate_id,
                actor="minion-v2-recovery",
                expected_version=revision.version,
                idempotency_key=f"architecture-rebind:{revision.aggregate_id}:{action_type}:{lease.fencing_token}",
                payload={
                    "fencing_token": lease.fencing_token,
                    "active_worker_id": invocation_id,
                    "lease_resource_key": lease_resource,
                },
            )
        ).snapshot
        if rebound.state == "ARCHITECT_SNAPSHOTTING" and not self._worktree_locks.is_held(revision.aggregate_id):
            expected = str(rebound.payload.get("workspace_fingerprint") or "")
            current = workspace_content_fingerprint(workspace)
            if not expected or current != expected:
                raise RuntimeError("architecture worktree changed while snapshot worker was unavailable")
            self._worktree_locks.acquire(revision.aggregate_id, workspace)
        return rebound

    async def _quiesce_architect(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = await self._ensure_architecture_effect_lease(
            self._effect_snapshot(effect),
            action_type="REBIND_ARCHITECT_QUIESCER",
        )
        invocation_id = str(revision.payload.get("active_worker_id") or "")
        fencing_token = int(revision.payload.get("fencing_token") or 0)
        lease_resource = str(revision.payload.get("lease_resource_key") or "")
        self.repository.assert_fencing_token(lease_resource, invocation_id, fencing_token)
        self._revoked_tokens.add((invocation_id, fencing_token))
        lease = self.repository.read_lease(lease_resource)
        metadata = dict((lease or {}).get("metadata") or {})
        process = self._processes.get(invocation_id)
        process_group = int(metadata.get("process_group_id") or (process.pid if process else 0))
        if process_group and not await terminate_process_group(process_group, timeout_seconds=5.0):
            raise RuntimeError("architect process group did not quiesce")
        workspace = Path(str(revision.payload.get("architecture_workspace_path") or ""))
        if workspace_has_live_processes(workspace):
            raise RuntimeError("a live process still holds the architecture worktree")
        lock_path = self._worktree_locks.acquire(revision.aggregate_id, workspace)
        try:
            if workspace_has_live_processes(workspace):
                raise RuntimeError("a process reached the architecture worktree during quiescing")
            fingerprint = workspace_content_fingerprint(workspace)
        except BaseException:
            self._worktree_locks.release(revision.aggregate_id)
            raise
        current = self.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
        self.repository.dispatch(
            ActionEnvelope(
                action_type="ARCHITECT_QUIESCED",
                workflow_id=revision.workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision.aggregate_id,
                actor="minion-v2-manager",
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

    async def _snapshot_architecture(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = await self._ensure_architecture_effect_lease(
            self._effect_snapshot(effect),
            action_type="REBIND_ARCHITECT_SNAPSHOTTER",
        )
        invocation_id = str(revision.payload.get("active_worker_id") or "")
        fencing_token = int(revision.payload.get("fencing_token") or 0)
        lease_resource = str(revision.payload.get("lease_resource_key") or "")
        self.repository.assert_fencing_token(lease_resource, invocation_id, fencing_token)
        if not self._worktree_locks.is_held(revision.aggregate_id):
            raise RuntimeError("architecture snapshot requires the quiescer's exclusive worktree lock")
        workspace_path = Path(str(revision.payload.get("architecture_workspace_path") or ""))
        before = workspace_content_fingerprint(workspace_path)
        if before != str(revision.payload.get("workspace_fingerprint") or ""):
            raise RuntimeError("architecture worktree changed after quiescing")
        workspace_snapshot_ref = _ref_from_mapping(revision.payload.get("workspace_snapshot_ref"))
        workspace_snapshot = self.service.artifacts.read_json(workspace_snapshot_ref)
        architecture_workspace = ArchitectureWorkspace(
            worktree=workspace_path,
            common_git_dir=Path(str(revision.payload.get("architecture_common_git_dir") or "")),
            base_sha=str(revision.payload.get("architecture_base_sha") or ""),
            base_tree_sha=str(revision.payload.get("architecture_base_tree_sha") or ""),
            original_head=str(workspace_snapshot.get("original_head") or ""),
            source_fingerprint=str(workspace_snapshot.get("source_fingerprint") or ""),
            workspace_snapshot_ref=workspace_snapshot_ref,
        )
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            raise ValueError("architecture revision has no workflow")
        request = workflow_request_from_snapshot(self.service, workflow)
        reference_roots = {
            str(item.get("name") or f"reference_{index + 1}"): Path(str(item.get("path") or "")).expanduser()
            for index, item in enumerate(list(request.get("references") or []))
            if str(dict(item or {}).get("path") or "").strip()
        }
        submission_ref = _ref_from_mapping(revision.payload.get("pending_architecture_submission_ref"))
        submission = self.service.artifacts.read_json(submission_ref)
        requirements_ref = _ref_from_mapping(revision.payload.get("requirements_ref"))
        try:
            manifest_ref = self.service.skeleton.snapshot_architecture(
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
        except ValueError as exc:
            finding_payload: dict[str, Any] = {
                "finding_kind": "contract_defect",
                "summary": str(exc),
                "source": "stable_architecture_preflight",
                "repair_instruction": (
                    "Correct only the rejected semantic DAG, reference, contract skeleton, or path declaration; "
                    "preserve unrelated accepted architecture content."
                ),
            }
            if isinstance(exc, SemanticReferenceError):
                finding_payload["semantic_reference_error"] = exc.to_dict()
            finding_ref = self.service.artifacts.put_json(
                finding_payload,
                artifact_type="ArchitectureFindingArtifact",
                child_refs=((submission_ref.sha256, "rejected_submission"),),
            )
            current = self.repository.read_snapshot(
                AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id
            )
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="ARCHITECTURE_SNAPSHOT_REJECTED",
                    workflow_id=revision.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision.aggregate_id,
                    actor="minion-v2-manager",
                    expected_version=current.version,
                    idempotency_key=(
                        f"architecture-snapshot-rejected:{revision.aggregate_id}:{finding_ref.sha256}"
                    ),
                    payload={"finding_artifact_ref": finding_ref.to_dict()},
                )
            )
            self._worktree_locks.release(revision.aggregate_id)
            self.repository.release_lease(lease_resource, invocation_id, fencing_token)
            return {"result_artifact_ref": finding_ref.to_dict(), "status": "rejected"}
        if workspace_content_fingerprint(workspace_path) != before:
            raise RuntimeError("architecture worktree content changed while the Manager created its commit")
        current = self.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
        self.repository.dispatch(
            ActionEnvelope(
                action_type="ARCHITECTURE_SNAPSHOTTED",
                workflow_id=revision.workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision.aggregate_id,
                actor="minion-v2-manager",
                expected_version=current.version,
                idempotency_key=f"architecture-snapshot:{revision.aggregate_id}:{manifest_ref.sha256}",
                payload={
                    "requirements_ref": requirements_ref.to_dict(),
                    "architecture_manifest_ref": manifest_ref.to_dict(),
                    "workspace_fingerprint": before,
                },
            )
        )
        self._worktree_locks.release(revision.aggregate_id)
        self.repository.release_lease(lease_resource, invocation_id, fencing_token)
        return {"result_artifact_ref": manifest_ref.to_dict()}

    async def _run_architecture_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = self._effect_snapshot(effect)
        if self._architecture_worker_suppressed(
            revision,
            running_state="REVIEWING",
            start_action="START_ARCHITECTURE_REVIEW",
        ):
            return {"status": "superseded"}
        manifest_ref = _ref_from_mapping(revision.payload.get("architecture_manifest_ref"))
        manifest_record = self.repository.read_artifact_record(manifest_ref.sha256)
        if manifest_record and str(manifest_record.get("artifact_type") or "") == ARCHITECTURE_SKELETON_ARTIFACT:
            return await self._run_skeleton_architecture_review(effect, revision, manifest_ref)
        manifest_payload = self.service.artifacts.read_json(manifest_ref)
        if dict(manifest_payload.get("requirements_ref") or {}) != dict(revision.payload.get("requirements_ref") or {}):
            raise ValueError("architecture reviewer requirements ref differs from the architect input")
        invocation_id = f"inv_{str(effect['effect_id']).removeprefix('eff_')}"
        lease_resource = f"architecture:{revision.aggregate_id}:review"
        if revision.state == "REVIEWING":
            active_lease = self.repository.read_lease(lease_resource)
            if active_lease and str(active_lease.get("owner_id") or "") and _lease_is_live(active_lease):
                return {"status": "already_running", "active_worker_id": str(active_lease["owner_id"])}
        lease = self.repository.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={"workflow_id": revision.workflow_id, "aggregate_id": revision.aggregate_id, "stage": "review"},
        )
        try:
            if "START_ARCHITECTURE_REVIEW" in self.repository.engine.legal_actions(AggregateType.ARCHITECTURE_REVISION, revision.state):
                revision = self.repository.dispatch(
                    ActionEnvelope(
                        action_type="START_ARCHITECTURE_REVIEW",
                        workflow_id=revision.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision.aggregate_id,
                        actor=invocation_id,
                        expected_version=revision.version,
                        idempotency_key=f"effect:{effect['effect_key']}:start",
                        payload={
                            "fencing_token": lease.fencing_token,
                            "active_worker_id": invocation_id,
                            "lease_resource_key": lease_resource,
                        },
                    )
                ).snapshot
            elif revision.state == "REVIEWING":
                revision = self.repository.dispatch(
                    ActionEnvelope(
                        action_type="REBIND_ARCHITECTURE_REVIEW",
                        workflow_id=revision.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision.aggregate_id,
                        actor=invocation_id,
                        expected_version=revision.version,
                        idempotency_key=f"effect:{effect['effect_key']}:rebind:{lease.fencing_token}",
                        payload={
                            "fencing_token": lease.fencing_token,
                            "active_worker_id": invocation_id,
                            "lease_resource_key": lease_resource,
                        },
                    )
                ).snapshot
            mechanical = self.service.architecture.review_manifest(manifest_ref)
            if mechanical.verdict != "PASS":
                review_ref = self.service.artifacts.put_json(
                    mechanical.to_dict(),
                    artifact_type="ArchitectureReviewArtifact",
                    child_refs=((manifest_ref.sha256, "architecture_manifest"),),
                )
                self._dispatch_architecture_review_result(revision, mechanical, review_ref)
                return {"result_artifact_ref": review_ref.to_dict()}
            prompt = (
                "Review the bound ArchitectureContractArtifact and its attached fragments by tracing only its requirements, contracts, topology, "
                "ownership, lifecycle, state, invariants, complexity, and integration claims. The manager's mechanical validation "
                "already passed. Find semantic omissions or contradictions; do not redesign it."
            )
            manifest = self.service.artifacts.read_json(manifest_ref)
            revision_base_value = revision.payload.get("revision_base_manifest_ref")
            if revision_base_value:
                revision_scope = self._publish_architecture_revision_review_scope(
                    revision,
                    base_manifest_ref=_ref_from_mapping(revision_base_value),
                    current_manifest_ref=manifest_ref,
                )
                review_refs = {"revision_review_scope": revision_scope}
                finding_value = revision.payload.get("finding_artifact_ref") or revision.payload.get("replan_finding_ref")
                if finding_value:
                    review_refs["revision_finding"] = _ref_from_mapping(finding_value)
                prompt += (
                    " This is a scoped revision review. Read revision_review_scope and revision_finding only; do not reread every unchanged fragment. "
                    "The manager has already compared all fragment references. Check that the marked repair resolves the finding and that its declared "
                    "transitive contract effects are coherent. Every FAIL finding must mark precise semantic revision_targets."
                )
            else:
                review_refs = {"architecture_manifest": manifest_ref}
                for relation, digest in architecture_manifest_child_refs(manifest):
                    artifact = self.service.repository.read_artifact_record(digest)
                    if artifact is not None:
                        review_refs[relation] = ArtifactRef.from_mapping(artifact)
            terminal, prompt_ref, terminal_ref = await self._run_profile(
                effect=effect,
                snapshot=revision,
                invocation_id=invocation_id,
                lease_resource=lease_resource,
                fencing_token=lease.fencing_token,
                profile=self._profile_for_role(revision.workflow_id, "architecture_reviewer"),
                role_override="architecture_reviewer",
                instruction=prompt,
                reference_refs=review_refs,
            )
            raw = _primary_json_output(terminal)
            semantic = _parse_architecture_review(raw)
            review_ref = self.service.artifacts.put_json(
                semantic.to_dict(),
                artifact_type="ArchitectureReviewArtifact",
                child_refs=((manifest_ref.sha256, "architecture_manifest"),),
            )
            self._dispatch_architecture_review_result(revision, semantic, review_ref)
            self.repository.record_worker_turn(
                invocation_id=invocation_id,
                fencing_token=lease.fencing_token,
                turn_index=1,
                llm_request_ref=prompt_ref.to_dict(),
                llm_response_ref=terminal_ref.to_dict(),
                tool_summary_ref=review_ref.to_dict(),
                **_recorded_worker_metrics(terminal),
            )
            return {"provider_request_id": invocation_id, "result_artifact_ref": review_ref.to_dict()}
        finally:
            try:
                self.repository.release_lease(lease_resource, invocation_id, lease.fencing_token)
            except Exception:
                pass

    async def _run_skeleton_architecture_review(
        self,
        effect: Mapping[str, Any],
        revision: AggregateSnapshot,
        manifest_ref: ArtifactRef,
    ) -> Mapping[str, Any]:
        artifact = self.service.artifacts.read_json(manifest_ref)
        if dict(artifact.get("requirements_ref") or {}) != dict(revision.payload.get("requirements_ref") or {}):
            raise ValueError("architecture reviewer requirements differ from the Architect input")
        invocation_id = f"inv_{str(effect['effect_id']).removeprefix('eff_')}"
        lease_resource = f"architecture:{revision.aggregate_id}:review"
        if revision.state == "REVIEWING":
            active = self.repository.read_lease(lease_resource)
            if active and str(active.get("owner_id") or "") and _lease_is_live(active):
                return {"status": "already_running", "active_worker_id": str(active["owner_id"])}
        lease = self.repository.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={"workflow_id": revision.workflow_id, "aggregate_id": revision.aggregate_id, "stage": "review"},
        )
        try:
            action_type = (
                "START_ARCHITECTURE_REVIEW"
                if "START_ARCHITECTURE_REVIEW"
                in self.repository.engine.legal_actions(AggregateType.ARCHITECTURE_REVISION, revision.state)
                else "REBIND_ARCHITECTURE_REVIEW"
            )
            revision = self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=revision.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision.aggregate_id,
                    actor=invocation_id,
                    expected_version=revision.version,
                    idempotency_key=f"effect:{effect['effect_key']}:{action_type.lower()}:{lease.fencing_token}",
                    payload={
                        "fencing_token": lease.fencing_token,
                        "active_worker_id": invocation_id,
                        "lease_resource_key": lease_resource,
                    },
                )
            ).snapshot
            requirements_ref = _ref_from_mapping(artifact.get("requirements_ref"))
            requirements_payload = self.service.artifacts.read_json(requirements_ref)
            review_worktree = self.service.skeleton.provision_review_worktree(
                artifact=artifact,
                review_name=f"{revision.aggregate_id}-{manifest_ref.sha256[:12]}",
            )
            mechanical = review_architecture_skeleton(
                artifact,
                worktree=review_worktree,
                requirements_payload=requirements_payload,
            )
            if mechanical.verdict != "PASS":
                review_ref = self.service.artifacts.put_json(
                    mechanical.to_dict(),
                    artifact_type="ArchitectureReviewArtifact",
                    child_refs=((manifest_ref.sha256, "architecture_skeleton"),),
                )
                self._dispatch_architecture_review_result(revision, mechanical, review_ref)
                return {"result_artifact_ref": review_ref.to_dict()}
            requirements_view_ref = self.service.artifacts.put_json(
                requirements_semantic_view(requirements_payload),
                artifact_type="RequirementsSemanticViewArtifact",
                provenance={"owner": "manager", "audience": "architecture_reviewer"},
                child_refs=((requirements_ref.sha256, "requirements"),),
            )
            review_view = {
                "modules": dict(dict(artifact.get("submission") or {}).get("modules") or {}),
                "integration": dict(dict(artifact.get("submission") or {}).get("integration") or {}),
                "changed_paths": list(artifact.get("changed_paths") or []),
            }
            review_view_ref = self.service.artifacts.put_json(
                review_view,
                artifact_type="ArchitectureSkeletonReviewViewArtifact",
                child_refs=((manifest_ref.sha256, "architecture_skeleton"),),
            )
            diff_text = subprocess.check_output(
                [
                    "git",
                    "-C",
                    str(review_worktree),
                    "diff",
                    "--find-renames",
                    str(artifact.get("base_commit_sha") or ""),
                    str(artifact.get("skeleton_commit_sha") or ""),
                    "--",
                ],
                text=True,
            )
            diff_ref = self.service.artifacts.put_bytes(
                diff_text.encode("utf-8"),
                artifact_type="ArchitectureSkeletonDiffArtifact",
                media_type="text/x-diff",
                child_refs=((manifest_ref.sha256, "architecture_skeleton"),),
            )
            references: dict[str, ArtifactRef] = {
                "requirements": requirements_view_ref,
                "architecture_index": review_view_ref,
                "architecture_diff": diff_ref,
            }
            finding_value = revision.payload.get("finding_artifact_ref") or revision.payload.get("replan_finding_ref")
            if finding_value:
                references["prior_finding"] = _ref_from_mapping(finding_value)
            terminal, prompt_ref, terminal_ref = await self._run_profile(
                effect=effect,
                snapshot=revision,
                invocation_id=invocation_id,
                lease_resource=lease_resource,
                fencing_token=lease.fencing_token,
                profile=self._profile_for_role(revision.workflow_id, "architecture_reviewer"),
                role_override="architecture_reviewer",
                instruction=(
                    "Review the candidate code skeleton against the exact same immutable Requirements received by the Architect. "
                    "Inspect the semantic DAG, code declarations/comments, diff, ownership, lifecycle, state, invariants, dependencies, and end-to-end contract. "
                    "Report all material architecture defects in one pass without designing implementation details."
                ),
                reference_refs=references,
                workspace_override={
                    "kind": "existing_repo",
                    "repo_path": str(review_worktree),
                    "project_name": "architecture-review",
                    "architecture_skeleton_mode": True,
                },
                prepare_workspace=False,
            )
            semantic = _parse_skeleton_review(_named_json_output(terminal, "architecture_review.json"))
            known_modules = set(review_view["modules"])
            for finding in semantic.findings:
                unknown_modules = set(finding.affected_modules) - known_modules
                if unknown_modules:
                    raise ValueError(
                        "architecture review finding references unknown modules: " + ", ".join(sorted(unknown_modules))
                    )
            review_ref = self.service.artifacts.put_json(
                semantic.to_dict(),
                artifact_type="ArchitectureReviewArtifact",
                child_refs=((manifest_ref.sha256, "architecture_skeleton"),),
            )
            self._dispatch_architecture_review_result(revision, semantic, review_ref)
            self.repository.record_worker_turn(
                invocation_id=invocation_id,
                fencing_token=lease.fencing_token,
                turn_index=1,
                llm_request_ref=prompt_ref.to_dict(),
                llm_response_ref=terminal_ref.to_dict(),
                tool_summary_ref=review_ref.to_dict(),
                **_recorded_worker_metrics(terminal),
            )
            return {"provider_request_id": invocation_id, "result_artifact_ref": review_ref.to_dict()}
        finally:
            try:
                self.repository.release_lease(lease_resource, invocation_id, lease.fencing_token)
            except Exception:
                pass

    def _publish_architecture_revision_review_scope(
        self,
        revision: AggregateSnapshot,
        *,
        base_manifest_ref: ArtifactRef,
        current_manifest_ref: ArtifactRef,
    ) -> ArtifactRef:
        base_payload = self._base_contract_builder_payload_from_manifest(base_manifest_ref)
        current_payload = self._base_contract_builder_payload_from_manifest(current_manifest_ref)
        changes = contract_revision_changes(base_payload, current_payload)
        requirements = self.service.artifacts.read_json(revision.payload["requirements_ref"])
        scope = {
            "schema_version": "1",
            "base_manifest_sha": base_manifest_ref.sha256,
            "current_manifest_sha": current_manifest_ref.sha256,
            "changed_targets": [item.to_dict() for item in changes],
            "changes": [
                {
                    "target": item.to_dict(),
                    "before": self._revision_scope_value(base_payload, requirements, item),
                    "after": self._revision_scope_value(current_payload, requirements, item),
                }
                for item in changes
            ],
        }
        return self.service.artifacts.put_json(
            scope,
            artifact_type="ArchitectureRevisionReviewScopeArtifact",
            provenance={"architecture_revision_id": revision.aggregate_id},
            child_refs=(
                (base_manifest_ref.sha256, "base_manifest"),
                (current_manifest_ref.sha256, "current_manifest"),
            ),
        )

    async def _publish_human_architecture_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = self._effect_snapshot(effect)
        manifest_ref = _ref_from_mapping(revision.payload.get("architecture_manifest_ref"))
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            raise ValueError("architecture revision has no workflow")
        actor = str(workflow.payload.get("owner") or "pal")
        channel = str(workflow.payload.get("active_channel") or "local")
        record = self.repository.read_artifact_record(manifest_ref.sha256)
        if record and str(record.get("artifact_type") or "") == ARCHITECTURE_SKELETON_ARTIFACT:
            artifact = self.service.artifacts.read_json(manifest_ref)
            requirements = self.service.artifacts.read_json(dict(artifact.get("requirements_ref") or {}))
            decision_token = self.repository.issue_human_decision_token(
                workflow_id=revision.workflow_id,
                architecture_revision_id=revision.aggregate_id,
                manifest_sha=manifest_ref.sha256,
                actor_id=actor,
                active_channel_id=channel,
            )
            payload = {
                "workflow_id": revision.workflow_id,
                "architecture_revision_id": revision.aggregate_id,
                "manifest_sha": manifest_ref.sha256,
                "actor_id": actor,
                "active_channel_id": channel,
                "decision_token": decision_token,
                "markdown": compile_skeleton_markdown(artifact, requirements_payload=requirements),
                "actions": ["accept", "edit", "reject"],
                "route": dict(workflow.payload.get("control_route") or {}),
            }
        else:
            card = self.service.architecture.create_human_review_card(
                workflow_id=revision.workflow_id,
                architecture_revision_id=revision.aggregate_id,
                manifest_ref=manifest_ref,
                actor_id=actor,
                active_channel_id=channel,
            )
            payload = {
                "workflow_id": card.workflow_id,
                "architecture_revision_id": card.architecture_revision_id,
                "manifest_sha": card.manifest_sha,
                "actor_id": card.actor_id,
                "active_channel_id": card.active_channel_id,
                "decision_token": card.decision_token,
                "markdown": card.markdown,
                "actions": list(card.actions),
                "route": dict(workflow.payload.get("control_route") or {}),
            }
        card_ref = self.service.artifacts.put_json(
            payload,
            artifact_type="HumanReviewCardArtifact",
            child_refs=((manifest_ref.sha256, "architecture_manifest"),),
        )
        if self.publish_human_review is not None:
            await self.publish_human_review({**payload, "card_ref": card_ref.to_dict()})
        return {"result_artifact_ref": card_ref.to_dict()}

    async def _publish_human_clarification(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = self._effect_snapshot(effect)
        clarification_ref = _ref_from_mapping(revision.payload.get("clarification_ref"))
        clarification = self.service.artifacts.read_json(clarification_ref)
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            raise ValueError("clarification has no workflow")
        actor = str(workflow.payload.get("owner") or "pal")
        channel = str(workflow.payload.get("active_channel") or "local")
        token = self.repository.issue_human_decision_token(
            workflow_id=revision.workflow_id,
            architecture_revision_id=revision.aggregate_id,
            manifest_sha=clarification_ref.sha256,
            actor_id=actor,
            active_channel_id=channel,
        )
        questions = list(clarification.get("questions") or [])
        payload = {
            "workflow_id": revision.workflow_id,
            "architecture_revision_id": revision.aggregate_id,
            "manifest_sha": clarification_ref.sha256,
            "actor_id": actor,
            "active_channel_id": channel,
            "decision_token": token,
            "clarification_pending": True,
            "questions": questions,
            "markdown": "Architecture requirements need clarification:\n\n"
            + "\n".join(
                f"- {_clarification_question_text(item)}"
                for item in questions
            ),
            "route": dict(workflow.payload.get("control_route") or {}),
        }
        card_ref = self.service.artifacts.put_json(
            payload,
            artifact_type="HumanClarificationCardArtifact",
            child_refs=((clarification_ref.sha256, "clarification_request"),),
        )
        if self.publish_human_review is not None:
            await self.publish_human_review({**payload, "card_ref": card_ref.to_dict()})
        return {"result_artifact_ref": card_ref.to_dict()}

    def _architecture_stage_prompt(
        self,
        stage: str,
        revision: AggregateSnapshot,
    ) -> tuple[str, dict[str, ArtifactRef]]:
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            raise ValueError("architecture revision has no workflow")
        request = workflow_request_from_snapshot(self.service, workflow)
        request_ref = _ref_from_mapping(revision.payload.get("request_ref") or workflow.payload.get("request_ref"))
        finding_value = revision.payload.get("finding_artifact_ref") or revision.payload.get("replan_finding_ref")
        base_manifest_ref = self._revision_input_base_manifest_ref(revision)
        refs: dict[str, ArtifactRef]
        scoped_revision = base_manifest_ref is not None and finding_value is not None
        if base_manifest_ref is None:
            refs = {
                "workflow_request": request_ref,
                "requirements": _ref_from_mapping(revision.payload.get("requirements_ref")),
            }
        elif scoped_revision:
            refs = {
                "revision_scope": self._publish_architecture_revision_scope(
                    revision,
                    base_manifest_ref=base_manifest_ref,
                    finding_value=finding_value,
                )
            }
        else:
            refs = {"edit_instruction": _ref_from_mapping(revision.payload.get("edit_instruction_ref"))} if revision.payload.get("edit_instruction_ref") else {}
        if revision.payload.get("edit_instruction_ref"):
            refs["edit_instruction"] = _ref_from_mapping(revision.payload.get("edit_instruction_ref"))
        if finding_value:
            refs["revision_finding"] = _ref_from_mapping(finding_value)
        instruction = (
            "Produce an implementation DAG for the immutable bound RequirementsArtifact; do not rewrite its requirements. Inspect local files and "
            "user references only to understand feasibility and architectural boundaries; do not build an evidence catalog or research private "
            "implementation details. Design high-level units, directional "
            "contracts, dataflow, ownership, lifecycle/state/invariants, work-start dependencies, and end-to-end integration. Existing user-provided "
            "module boundaries are authoritative inputs but not presumed complete: add necessary foundation, bridge, or integration units. Do not design "
            "private helpers, algorithms, SDK call sequences, milestones, implementation checklists, evidence catalogs, or test matrices. Submit only "
            "the architecture contract through the bound builder."
        )
        if scoped_revision:
            instruction += (
                " This is a scoped revision: use op_minion_input_read only for revision_finding, revision_scope, and an edit instruction when present; "
                "do not reread the repository, workflow request, full requirements, or base manifest. The manager has preseeded the complete base "
                "contract privately. First call op_minion_contract_revision_read, then change only its semantic targets with the bound CRUD tools. "
                "Unrelated semantic drift is rejected mechanically; untouched fragments retain their existing artifact references."
            )
        elif base_manifest_ref is not None:
            instruction += (
                " This is a human-authored revision. The manager has preseeded the base contract; apply the bound edit instruction without "
                "rediscovering the architecture, and preserve all unrelated semantics."
            )
        for index, reference in enumerate(list(request.get("references") or []) if base_manifest_ref is None else []):
            if not isinstance(reference, Mapping):
                continue
            path = str(reference.get("path") or "").strip()
            if path and Path(path).expanduser().exists():
                name = str(reference.get("name") or f"user_reference_{index + 1}").strip()
                refs[f"user_{name}"] = _path_pseudo_ref(path, name)
        return instruction, refs

    @staticmethod
    def _revision_input_base_manifest_ref(revision: AggregateSnapshot) -> ArtifactRef | None:
        """Return the immediate manifest a revision repairs, never an older ancestor."""

        current = revision.payload.get("architecture_manifest_ref")
        finding = revision.payload.get("finding_artifact_ref") or revision.payload.get("replan_finding_ref")
        if current and finding:
            return _ref_from_mapping(current)
        base = revision.payload.get("base_architecture_manifest_ref")
        return _ref_from_mapping(base) if base else None

    def _publish_architecture_revision_scope(
        self,
        revision: AggregateSnapshot,
        *,
        base_manifest_ref: ArtifactRef,
        finding_value: Any,
    ) -> ArtifactRef:
        """Bind only the semantic chapters a reviewer marked for repair.

        The complete base remains in the manager-owned builder draft. This
        artifact is deliberately small and human-readable so the architect can
        reason in stable section/id/field terms rather than artifact handles.
        """

        finding_ref = _ref_from_mapping(finding_value) if finding_value else None
        finding_payload = self.service.artifacts.read_json(finding_ref) if finding_ref else {}
        raw_targets: list[Any] = []
        for finding in list(dict(finding_payload).get("findings") or []):
            raw_targets.extend(list(dict(finding or {}).get("revision_targets") or []))
        # Human edit instructions are intentionally not guessed into a broad
        # writable scope. They retain the existing full-revision path until the
        # foreground can issue semantic edit targets.
        targets = normalize_revision_targets(raw_targets)
        if not targets:
            raise ValueError("architecture revision finding requires semantic revision_targets")
        base_payload = self._base_contract_builder_payload_from_manifest(base_manifest_ref)
        requirements_payload = self.service.artifacts.read_json(revision.payload["requirements_ref"])
        context: list[dict[str, Any]] = []
        for target in targets:
            context.append(
                {
                    "access": "write",
                    "target": target.to_dict(),
                    "value": self._revision_scope_value(base_payload, requirements_payload, target),
                }
            )
        selected_unit_ids = {item.target_id for item in targets if item.section == "unit"}
        selected_cross_ids = {item.target_id for item in targets if item.section == "cross_unit_contract"}
        for item in list(base_payload.get("cross_unit_contracts") or []):
            contract = dict(item or {})
            contract_id = str(contract.get("id") or "")
            if contract_id and contract_id not in selected_cross_ids and {
                str(contract.get("producer") or ""),
                str(contract.get("consumer") or ""),
            } & selected_unit_ids:
                context.append(
                    {
                        "access": "read_only_context",
                        "target": {
                            "section": "cross_unit_contract",
                            "id": contract_id,
                            "fields": [],
                            "operation": "update",
                        },
                        "value": contract,
                    }
                )
        scope = {
            "schema_version": "1",
            "base_manifest_sha": base_manifest_ref.sha256,
            "finding_sha": finding_ref.sha256 if finding_ref else "",
            "write_targets": [item.to_dict() for item in targets],
            "context": context,
        }
        child_refs = [(base_manifest_ref.sha256, "base_manifest")]
        if finding_ref is not None:
            child_refs.append((finding_ref.sha256, "review_finding"))
        return self.service.artifacts.put_json(
            scope,
            artifact_type="ArchitectureRevisionScopeArtifact",
            provenance={"architecture_revision_id": revision.aggregate_id},
            child_refs=tuple(child_refs),
        )

    @staticmethod
    def _revision_scope_value(
        payload: Mapping[str, Any],
        requirements: Mapping[str, Any],
        target: ArchitectureRevisionTarget,
    ) -> Any:
        collection_sections = {
            "constraint": ("global_constraints", "id"),
            "design_decision": ("design_decisions", "id"),
            "gate_check": ("gate_checks", "id"),
            "unit": ("units", "unit_id"),
            "cross_unit_contract": ("cross_unit_contracts", "id"),
            "requirements": ("requirements", "requirement_id"),
        }
        if target.section in collection_sections:
            field_name, id_field = collection_sections[target.section]
            source = requirements if target.section == "requirements" else payload
            return next(
                (
                    dict(item or {})
                    for item in list(source.get(field_name) or [])
                    if str(dict(item or {}).get(id_field) or "") == target.target_id
                ),
                None,
            )
        if target.section == "topology":
            depends_on = dict(dict(payload.get("topology") or {}).get("depends_on") or {})
            return {"unit_id": target.target_id, "depends_on": list(depends_on.get(target.target_id) or [])}
        if target.section == "integration_contract":
            return dict(payload.get("integration_contract") or {})
        if target.section == "assumption_ledger":
            return dict(payload.get("assumption_ledger") or {})
        if target.section == "risk_ledger":
            return dict(payload.get("risk_ledger") or {})
        return None

    async def _run_profile(
        self,
        *,
        effect: Mapping[str, Any],
        snapshot: AggregateSnapshot,
        invocation_id: str,
        lease_resource: str,
        fencing_token: int,
        profile: str,
        role_override: str,
        instruction: str,
        reference_refs: Mapping[str, ArtifactRef],
        workspace_override: Mapping[str, Any] | None = None,
        prepare_workspace: bool = True,
    ) -> tuple[dict[str, Any], ArtifactRef, ArtifactRef]:
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, snapshot.workflow_id)
        if workflow is None:
            raise ValueError("worker workflow does not exist")
        request = workflow_request_from_snapshot(self.service, workflow)
        binding_ref = dict(workflow.payload.get("family_binding_ref") or {})
        binding = dict(self.service.artifacts.read_json(binding_ref)) if binding_ref else {}
        family_policies = dict(binding.get("policies") or {})
        llm_policy = dict(family_policies.get("llm") or {})
        workspace_policy = dict(family_policies.get("workspace") or {})
        workspace = dict(workspace_override or request.get("workspace") or {})
        if not workspace:
            workspace = {"kind": "new_project", "project_name": f"workflow-{snapshot.workflow_id}"}
        role = str(role_override or "").strip() or self._role_for_profile(snapshot.workflow_id, profile)
        skeleton_mode = bool(workspace.get("architecture_skeleton_mode"))
        builder_stages = {
            "architect": "architect_planning",
            "requirements": "requirements",
            "research": "evidence",
            "planner": "contract",
            "architecture_reviewer": "architecture_review",
        }
        if role in builder_stages and not skeleton_mode:
            workspace["contract_builder_stage"] = builder_stages[role]
        bound_reference_refs = dict(reference_refs)
        if role != "requirements" and bool(workspace_policy.get("prepare", False)):
            workspace, preparation = prepare_v2_workspace_environment(workspace)
            if bool(workspace_policy.get("prewarm_lsp", False)) and dict(workspace.get("lsp_setup") or {}).get("servers"):
                preparation["lsp_prewarm"] = prewarm_workspace_lsp(
                    runtime_root=self.service.runtime_root,
                    workspace=workspace,
                )
            preparation_ref = self.service.artifacts.put_json(
                preparation,
                artifact_type="WorkspacePreparationArtifact",
                provenance={"family_id": str(binding.get("family_id") or ""), "role": role},
            )
            bound_reference_refs["workspace_preparation"] = preparation_ref
        if role in {"verifier", "reviewer"}:
            verification_policy_ref = self.service.artifacts.put_json(
                dict(family_policies.get("verification") or {}),
                artifact_type="VerificationPolicyArtifact",
                provenance={"family_id": str(binding.get("family_id") or ""), "role": role},
            )
            bound_reference_refs["verification_policy"] = verification_policy_ref
        references: list[dict[str, Any]] = []
        for name, ref in bound_reference_refs.items():
            if ref.artifact_type == "LocalPathReference":
                path = str(ref.media_type)
            else:
                record = self.repository.read_artifact_record(ref.sha256)
                if record is None:
                    raise ValueError(f"worker input artifact is unavailable: {name}")
                path = str(record["storage_path"])
            references.append(
                {
                    "name": name,
                    "path": path,
                    "description": f"V2 immutable input {name}",
                    "truth_source": True,
                    "required": True,
                    "bound_input": ref.artifact_type != "LocalPathReference",
                }
            )
        workspace["reference_paths"] = references
        profile_group, profile_name = profile.rsplit(".", 1)
        if skeleton_mode and role == "architect":
            invocation_acceptance = [
                "Write the contract-level code skeleton in the bound architecture worktree.",
                "Submit the complete semantic module DAG and path policy exactly once through op_minion_architecture_submit.",
            ]
        elif skeleton_mode and role == "architecture_reviewer":
            invocation_acceptance = [
                "Review the bound Requirements, skeleton diff, code contracts, and semantic DAG.",
                "Submit one PASS or FAIL through op_minion_skeleton_review_submit.",
            ]
        else:
            invocation_acceptance = ["Write the exact primary JSON artifact required by the profile output contract."]
        pack = MinionInvocationPack(
            invocation_id=invocation_id,
            goal=instruction,
            instruction=instruction,
            acceptance_criteria=invocation_acceptance,
            workspace=workspace,
            profile_group=profile_group,
            profile_name=profile_name,
            minion_profile=profile,
            metadata={
                "minion_v2": {
                    "workflow_id": snapshot.workflow_id,
                    "aggregate_type": snapshot.aggregate_type.value,
                    "aggregate_id": snapshot.aggregate_id,
                    "effect_id": effect["effect_id"],
                    "invocation_id": invocation_id,
                    "lease_resource": lease_resource,
                    "fencing_token": fencing_token,
                    "role": role,
                },
                **(
                    {
                        "agent_session": {
                            "session_id": invocation_id,
                            "response_key": str(effect.get("effect_key") or effect.get("effect_id") or ""),
                            "fencing_token": int(fencing_token),
                        }
                    }
                    if role in {"architect", "producer", "repair"}
                    else {}
                ),
                "requirements_brief": {
                    "references": references,
                    "research_mode": snapshot.payload.get("research_mode", "local_only"),
                },
                "allow_text_only_completion": role not in builder_stages,
                **(
                    {"temperature": llm_policy["temperature"]}
                    if "temperature" in llm_policy
                    else {}
                ),
                **(
                    {"llm_round_timeout_seconds": llm_policy["llm_round_timeout_seconds"]}
                    if "llm_round_timeout_seconds" in llm_policy
                    else {}
                ),
            },
        )
        base_manifest_ref = self._revision_input_base_manifest_ref(snapshot) if role == "architect" else None
        revision_scope: Mapping[str, Any] | None = None
        if role == "architect" and "revision_scope" in bound_reference_refs:
            revision_scope = self.service.artifacts.read_json(bound_reference_refs["revision_scope"])
        registry = MinionProfileRegistry(runtime_root=self.service.runtime_root)
        pack = registry.resolve_pack(pack)
        pack = apply_v2_role_capability_policy(pack, role=role)
        if role == "architect" and revision_scope is not None:
            pack = apply_v2_revision_scope_capability_policy(pack)
        pack = apply_v2_research_capability_policy(
            pack,
            research_mode=str(snapshot.payload.get("research_mode") or "local_only"),
        )
        run_id = f"run_{invocation_id.removeprefix('inv_')[:16]}"
        if prepare_workspace:
            pack = prepare_v2_role_workspace(self.service.runtime_root, pack, run_id=run_id)
        else:
            invocation_dir = self.service.runtime_root / "data" / "minion" / "v2" / "invocations" / invocation_id
            bound_workspace = dict(pack.workspace)
            bound_workspace.update(
                {
                    "run_dir": str(invocation_dir),
                    "artifact_dir": str(invocation_dir / "artifacts"),
                    "artifact_stage_dir": str(invocation_dir / "artifact-stage"),
                    "log_dir": str(invocation_dir / "logs"),
                    "review_scratch_dir": str(invocation_dir / "review-scratch"),
                }
            )
            for key in ("artifact_dir", "artifact_stage_dir", "log_dir", "review_scratch_dir"):
                Path(str(bound_workspace[key])).mkdir(parents=True, exist_ok=True)
            pack = MinionInvocationPack.from_dict({**pack.to_dict(), "workspace": bound_workspace})
        if role == "architect" and base_manifest_ref is not None and not skeleton_mode:
            seed_contract_builder_draft(
                pack.workspace,
                self._base_contract_builder_payload_from_manifest(base_manifest_ref),
                revision_scope=revision_scope,
            )
        pack = sanitize_runner_session_pack(pack)
        pack = with_minion_sandbox_metadata(self.service.runtime_root, pack, run_id=run_id)
        prompt_ref = self.service.artifacts.put_json(
            pack.to_dict(),
            artifact_type="WorkerPromptPackArtifact",
            child_refs=tuple(
                (ref.sha256, name)
                for name, ref in bound_reference_refs.items()
                if ref.artifact_type != "LocalPathReference"
            ),
        )
        self.repository.record_worker_invocation(
            invocation_id=invocation_id,
            workflow_id=snapshot.workflow_id,
            aggregate_type=snapshot.aggregate_type,
            aggregate_id=snapshot.aggregate_id,
            lease_resource_key=lease_resource,
            fencing_token=fencing_token,
            role=profile_name,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        invocation_dir = self.service.runtime_root / "data" / "minion" / "v2" / "invocations" / invocation_id
        invocation_dir.mkdir(parents=True, exist_ok=True)
        pack_path = invocation_dir / "pack.json"
        pack_path.write_text(json.dumps(pack.to_dict(), ensure_ascii=False, sort_keys=True), encoding="utf-8")
        argv = [
            sys.executable,
            "-m",
            "pal.minion.v2.worker_main",
            "--runtime-root",
            str(self.service.runtime_root),
            "--pack-json",
            str(pack_path),
            "--minion-id",
            invocation_id,
            "--run-id",
            run_id,
        ]
        argv, env = build_sandboxed_runner_invocation(
            runtime_root=self.service.runtime_root,
            pack=pack,
            argv=argv,
            env=python_subprocess_env(),
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        self.repository.update_lease_metadata(
            lease_resource,
            invocation_id,
            fencing_token,
            {
                "workflow_id": snapshot.workflow_id,
                "aggregate_type": snapshot.aggregate_type.value,
                "aggregate_id": snapshot.aggregate_id,
                "process_group_id": process.pid,
                "workspace_path": str(pack.workspace.get("repo_path") or ""),
                "run_id": run_id,
            },
        )
        self._processes[invocation_id] = process
        self._run_to_invocation[run_id] = invocation_id
        if self.register_broker_run is not None:
            self.register_broker_run(run_id, invocation_id, pack, process)
        lease_heartbeat = asyncio.create_task(
            self._lease_heartbeat(lease_resource, invocation_id, fencing_token),
            name=f"minion-v2-lease-{invocation_id}",
        )
        try:
            stderr_task = asyncio.create_task(process.stderr.read())
            events: list[dict[str, Any]] = []
            worker_error = ""
            while True:
                raw_line = await process.stdout.readline()
                if not raw_line:
                    break
                try:
                    item = json.loads(raw_line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    continue
                if str(item.get("kind") or "") == "event" and isinstance(item.get("event"), dict):
                    event = dict(item["event"])
                    events.append(event)
                    if self.publish_worker_event is not None:
                        await self.publish_worker_event(event)
                elif str(item.get("kind") or "") == "worker_error":
                    worker_error = str(item.get("error") or "")
            await process.wait()
            stderr = await stderr_task
        finally:
            lease_heartbeat.cancel()
            try:
                await lease_heartbeat
            except asyncio.CancelledError:
                pass
            if process.returncode is None:
                await terminate_process_group(process.pid, timeout_seconds=2.0)
                with contextlib.suppress(Exception):
                    await process.wait()
            self._processes.pop(invocation_id, None)
            self._run_to_invocation.pop(run_id, None)
            if self.unregister_broker_run is not None:
                self.unregister_broker_run(run_id)
        if process.returncode != 0:
            error_tail = _meaningful_stderr_tail(stderr.decode("utf-8", errors="replace"))
            continuation_ref = self._publish_agent_session_checkpoint(invocation_id, fencing_token)
            if continuation_ref is not None and role in {"architect", "producer", "repair"}:
                self.repository.suspend_worker_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    continuation_ref=continuation_ref.to_dict(),
                    status="interrupted",
                )
            else:
                self.repository.finish_worker_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    status="failed",
                )
            details = worker_error or error_tail or "worker emitted no structured error"
            raise RuntimeError(f"V2 worker exited {process.returncode}: {details}")
        terminal = next((item for item in reversed(events) if str(item.get("event_kind") or "") == "terminal"), None)
        if terminal is None:
            continuation_ref = self._publish_agent_session_checkpoint(invocation_id, fencing_token)
            if continuation_ref is not None and role in {"architect", "producer", "repair"}:
                self.repository.suspend_worker_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    continuation_ref=continuation_ref.to_dict(),
                    status="interrupted",
                )
            else:
                self.repository.finish_worker_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    status="failed",
                )
            raise RuntimeError("V2 semantic worker ended without terminal event")
        terminal_payload = dict(terminal.get("payload") or {})
        if str(terminal_payload.get("status") or "") != "completed":
            continuation_ref = self._publish_agent_session_checkpoint(invocation_id, fencing_token)
            if continuation_ref is not None and role in {"architect", "producer", "repair"}:
                self.repository.suspend_worker_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    continuation_ref=continuation_ref.to_dict(),
                    status="interrupted",
                )
            else:
                self.repository.finish_worker_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    status="failed",
                )
            raise RuntimeError(str(terminal_payload.get("summary") or "V2 semantic worker failed"))
        continuation_ref = self._publish_agent_session_checkpoint(invocation_id, fencing_token)
        terminal_payload["v2_timing"] = _worker_event_timing(events)
        if continuation_ref is not None:
            continuation_payload = self.service.artifacts.read_json(continuation_ref)
            terminal_payload["session_turn_index"] = int(continuation_payload.get("llm_round_count") or 0)
        terminal = {**terminal, "payload": terminal_payload}
        terminal_ref = self.service.artifacts.put_json(
            terminal,
            artifact_type="WorkerTerminalArtifact",
            child_refs=(
                (prompt_ref.sha256, "prompt_pack"),
                *(
                    ((continuation_ref.sha256, "agent_session_continuation"),)
                    if continuation_ref is not None
                    else ()
                ),
            ),
        )
        if role in {"architect", "producer", "repair"}:
            if continuation_ref is None:
                raise RuntimeError("resumable worker completed without a durable agent-session checkpoint")
            self.repository.suspend_worker_invocation(
                invocation_id=invocation_id,
                fencing_token=fencing_token,
                continuation_ref=continuation_ref.to_dict(),
            )
        else:
            self.repository.finish_worker_invocation(
                invocation_id=invocation_id,
                fencing_token=fencing_token,
                status="completed",
            )
        return terminal, prompt_ref, terminal_ref

    def _publish_agent_session_checkpoint(
        self,
        invocation_id: str,
        fencing_token: int,
    ) -> ArtifactRef | None:
        invocation_dir = self.service.runtime_root / "data" / "minion" / "v2" / "invocations" / invocation_id
        candidates: list[tuple[int, Path]] = []
        for path in invocation_dir.glob("session-continuation-*.json"):
            suffix = path.stem.removeprefix("session-continuation-")
            if suffix.isdigit() and int(suffix) <= int(fencing_token):
                candidates.append((int(suffix), path))
        for token, path in sorted(candidates, reverse=True):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or str(payload.get("session_id") or "") != invocation_id:
                continue
            if int(payload.get("fencing_token") or 0) != token:
                continue
            return self.service.artifacts.put_json(
                payload,
                artifact_type="AgentSessionContinuationArtifact",
                provenance={"invocation_id": invocation_id, "fencing_token": token},
            )
        return None

    def _profile_for_role(self, workflow_id: str, role: str) -> str:
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            raise ValueError(f"workflow not found while resolving role {role}: {workflow_id}")
        binding_ref = dict(workflow.payload.get("family_binding_ref") or {})
        if not binding_ref:
            raise ValueError(f"workflow has no FamilyBindingArtifact: {workflow_id}")
        binding = dict(self.service.artifacts.read_json(binding_ref))
        profile = str(dict(binding.get("roles") or {}).get(role) or "").strip()
        if not profile:
            raise ValueError(f"family {binding.get('family_id')} does not bind role {role}")
        return profile

    def _uses_git_skeleton(self, workflow_id: str) -> bool:
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            return False
        binding_ref = dict(getattr(workflow, "payload", {}).get("family_binding_ref") or {})
        if not binding_ref:
            return False
        binding = dict(self.service.artifacts.read_json(binding_ref))
        return str(dict(binding.get("builders") or {}).get("contract") or "") == "skeleton_git.v2"

    def _is_skeleton_manifest(self, value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        record = self.repository.read_artifact_record(str(value.get("sha256") or ""))
        return bool(record and str(record.get("artifact_type") or "") == ARCHITECTURE_SKELETON_ARTIFACT)

    def _profile_for_role_or(self, workflow_id: str, role: str, *, fallback: str) -> str:
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            raise ValueError(f"workflow not found while resolving role {role}: {workflow_id}")
        binding = dict(self.service.artifacts.read_json(dict(workflow.payload.get("family_binding_ref") or {})))
        roles = dict(binding.get("roles") or {})
        profile = str(roles.get(role) or roles.get(fallback) or "").strip()
        if not profile:
            raise ValueError(f"family {binding.get('family_id')} binds neither {role} nor {fallback}")
        return profile

    def _workflow_policy(self, workflow_id: str, name: str) -> dict[str, Any]:
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            return {}
        binding_ref = dict(workflow.payload.get("family_binding_ref") or {})
        if not binding_ref:
            return {}
        binding = dict(self.service.artifacts.read_json(binding_ref))
        return dict(dict(binding.get("policies") or {}).get(name) or {})

    def _role_for_profile(self, workflow_id: str, profile: str) -> str:
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            raise ValueError(f"workflow not found while resolving profile role: {workflow_id}")
        binding = dict(self.service.artifacts.read_json(dict(workflow.payload.get("family_binding_ref") or {})))
        matches = [role for role, profile_id in dict(binding.get("roles") or {}).items() if str(profile_id) == profile]
        if not matches:
            raise ValueError(f"profile {profile} is not bound by workflow family {binding.get('family_id')}")
        return matches[0]

    async def send_worker_control(self, run_id: str, message: Mapping[str, Any]) -> bool:
        invocation_id = self._run_to_invocation.get(str(run_id))
        process = self._processes.get(invocation_id or "")
        if process is None or process.returncode is not None or process.stdin is None:
            return False
        process.stdin.write((json.dumps(dict(message), ensure_ascii=False) + "\n").encode("utf-8"))
        await process.stdin.drain()
        return True

    async def _lease_heartbeat(self, resource_key: str, owner_id: str, fencing_token: int) -> None:
        while True:
            await asyncio.sleep(30)
            self.repository.renew_lease(
                resource_key,
                owner_id,
                fencing_token,
                ttl_seconds=120,
            )

    def _publish_planning_bundle(
        self,
        revision: AggregateSnapshot,
        output: Mapping[str, Any],
        *,
        requirements_ref: ArtifactRef | None = None,
        base_manifest_ref: ArtifactRef | None = None,
    ) -> ArtifactRef:
        required_keys = {
            "global_constraints",
            "design_decisions",
            "gate_checks",
            "units",
            "cross_unit_contracts",
            "topology",
            "integration_contract",
            "assumption_ledger",
            "risk_ledger",
        }
        missing = sorted(required_keys - set(output))
        if missing:
            raise ValueError(f"architecture bundle missing keys: {', '.join(missing)}")
        provenance = {
            "architecture_revision_id": revision.aggregate_id,
            "stage": "planning",
            **({"base_manifest_sha": base_manifest_ref.sha256} if base_manifest_ref is not None else {}),
        }
        base_manifest = self.service.artifacts.read_json(base_manifest_ref) if base_manifest_ref is not None else None
        base_payload = (
            self._base_contract_builder_payload_from_manifest(base_manifest_ref)
            if base_manifest_ref is not None
            else None
        )

        def reuse_single(field_name: str, output_key: str, artifact_type: str) -> ArtifactRef:
            if base_manifest is not None and base_payload is not None and output[output_key] == base_payload[output_key]:
                return _ref_from_mapping(base_manifest[field_name])
            return self.service.architecture.publish_fragment(output[output_key], artifact_type=artifact_type, provenance=provenance)

        constraints = reuse_single("global_constraints_ref", "global_constraints", "GlobalConstraintsArtifact")
        decisions = reuse_single("design_decisions_ref", "design_decisions", "DesignDecisionsArtifact")
        gates = reuse_single("gate_checks_ref", "gate_checks", "ArchitectureGateChecksArtifact")

        base_units: dict[str, tuple[dict[str, Any], ArtifactRef]] = {}
        base_cross: dict[str, tuple[dict[str, Any], ArtifactRef]] = {}
        if base_manifest is not None and base_payload is not None:
            base_units = {
                str(item.get("unit_id") or ""): (dict(item), _ref_from_mapping(ref))
                for item, ref in zip(base_payload["units"], list(base_manifest.get("unit_contract_refs") or []), strict=True)
            }
            base_cross = {
                str(item.get("id") or ""): (dict(item), _ref_from_mapping(ref))
                for item, ref in zip(base_payload["cross_unit_contracts"], list(base_manifest.get("cross_unit_contract_refs") or []), strict=True)
            }
        units = []
        for item in list(output["units"] or []):
            value = dict(item or {})
            existing = base_units.get(str(value.get("unit_id") or ""))
            units.append(existing[1] if existing is not None and existing[0] == value else self.service.architecture.publish_unit_contract(value, provenance=provenance))
        cross = []
        for item in list(output["cross_unit_contracts"] or []):
            value = dict(item or {})
            existing = base_cross.get(str(value.get("id") or ""))
            cross.append(existing[1] if existing is not None and existing[0] == value else self.service.architecture.publish_fragment(value, artifact_type="CrossUnitContractArtifact", provenance=provenance))

        topology = reuse_single("topology_ref", "topology", "TopologyArtifact")
        integration = reuse_single("integration_contract_ref", "integration_contract", "IntegrationContractArtifact")
        assumptions = reuse_single("assumption_ledger_ref", "assumption_ledger", "AssumptionLedgerArtifact")
        risks = reuse_single("risk_ledger_ref", "risk_ledger", "RiskLedgerArtifact")
        return self.service.architecture.publish_manifest(
            {
                "requirements_ref": (requirements_ref.to_dict() if requirements_ref else dict(revision.payload["requirements_ref"])),
                "global_constraints_ref": constraints.to_dict(),
                "design_decisions_ref": decisions.to_dict(),
                "gate_checks_ref": gates.to_dict(),
                "unit_contract_refs": [item.to_dict() for item in units],
                "cross_unit_contract_refs": [item.to_dict() for item in cross],
                "topology_ref": topology.to_dict(),
                "integration_contract_ref": integration.to_dict(),
                "assumption_ledger_ref": assumptions.to_dict(),
                "risk_ledger_ref": risks.to_dict(),
            },
            provenance=provenance,
        )

    def _dispatch_architecture_review_result(
        self,
        revision: AggregateSnapshot,
        review: ArchitectureReviewResult,
        review_ref: ArtifactRef,
    ) -> None:
        current = self.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
        if current is None:
            raise ValueError("architecture revision disappeared during review")
        if review.verdict == "PASS":
            action_type = "ARCHITECTURE_REVIEW_PASSED"
            payload = {
                "review_artifact_ref": review_ref.to_dict(),
                "architecture_manifest_ref": current.payload["architecture_manifest_ref"],
            }
        else:
            finding = review.findings[0]
            action_type = {
                "requirements_defect": "REQUIREMENTS_DEFECT",
                "contract_defect": "CONTRACT_DEFECT",
                "architecture_defect": "ARCHITECTURE_DEFECT",
            }[str(finding.finding_kind)]
            payload = {"finding_artifact_ref": review_ref.to_dict(), "findings": [item.to_dict() for item in review.findings]}
        self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=current.workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=current.aggregate_id,
                actor="minion-v2-architecture-reviewer",
                expected_version=current.version,
                idempotency_key=f"architecture-review:{current.aggregate_id}:{review_ref.sha256}",
                payload=payload,
            )
        )

    def _effect_snapshot(self, effect: Mapping[str, Any]) -> AggregateSnapshot:
        aggregate_type = AggregateType(str(effect["aggregate_type"]))
        snapshot = self.repository.read_snapshot(aggregate_type, str(effect["aggregate_id"]))
        if snapshot is None:
            raise ValueError("semantic effect aggregate no longer exists")
        return snapshot

    def _base_contract_builder_payload_from_manifest(self, manifest_ref: ArtifactRef) -> dict[str, Any]:
        manifest = self.service.artifacts.read_json(manifest_ref)

        def read_ref(field_name: str) -> Any:
            return self.service.artifacts.read_json(dict(manifest[field_name]))

        return {
            "global_constraints": read_ref("global_constraints_ref"),
            "design_decisions": read_ref("design_decisions_ref"),
            "gate_checks": read_ref("gate_checks_ref"),
            "units": [
                self.service.artifacts.read_json(dict(item))
                for item in list(manifest.get("unit_contract_refs") or [])
            ],
            "cross_unit_contracts": [
                self.service.artifacts.read_json(dict(item))
                for item in list(manifest.get("cross_unit_contract_refs") or [])
            ],
            "topology": read_ref("topology_ref"),
            "integration_contract": read_ref("integration_contract_ref"),
            "assumption_ledger": read_ref("assumption_ledger_ref"),
            "risk_ledger": read_ref("risk_ledger_ref"),
        }

    def _base_contract_builder_payload(self, refs: Mapping[str, ArtifactRef]) -> dict[str, Any]:
        def read(name: str) -> Any:
            ref = refs.get(name)
            if ref is None:
                raise ValueError(f"base architecture revision is missing {name}")
            return self.service.artifacts.read_json(ref)

        def numbered(prefix: str) -> list[dict[str, Any]]:
            names = sorted(
                (name for name in refs if name.startswith(prefix)),
                key=lambda name: int(name.removeprefix(prefix)),
            )
            return [dict(read(name)) for name in names]

        return {
            "global_constraints": read("base_global_constraints"),
            "design_decisions": read("base_design_decisions"),
            "gate_checks": read("base_gate_checks"),
            "units": numbered("base_unit_contract_"),
            "cross_unit_contracts": numbered("base_cross_unit_contract_"),
            "topology": read("base_topology"),
            "integration_contract": read("base_integration_contract"),
            "assumption_ledger": read("base_assumption_ledger"),
            "risk_ledger": read("base_risk_ledger"),
        }

    def _architecture_worker_suppressed(
        self,
        revision: AggregateSnapshot,
        *,
        running_state: str,
        start_action: str,
    ) -> bool:
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            return True
        if workflow.state in {"PAUSE_REQUESTED", "PAUSED", "CANCEL_REQUESTED", "CANCELLED"}:
            return True
        legal = self.repository.engine.legal_actions(AggregateType.ARCHITECTURE_REVISION, revision.state)
        return revision.state != running_state and start_action not in legal

    def _write_node_journal(
        self,
        node: AggregateSnapshot,
        *,
        owner_id: str,
        lease_resource: str,
        fencing_token: int,
        updates: Mapping[str, Any],
    ) -> None:
        current = self.repository.read_node_journal(node.aggregate_id) or {}
        journal = dict(current.get("journal") or {})
        journal.update(dict(updates))
        self.repository.update_node_journal(
            node_run_id=node.aggregate_id,
            workflow_id=node.workflow_id,
            lease_resource_key=lease_resource,
            owner_id=owner_id,
            fencing_token=fencing_token,
            expected_generation=int(current.get("generation") or 0),
            journal=journal,
        )

    def _publish_verification_workspace(
        self,
        *,
        review_worktree: Path,
        review_scratch: Path,
        candidate_digest: str,
        execution_adapter: str,
        include_candidate_patch: bool = True,
    ) -> ArtifactRef:
        patch_bytes = b""
        if execution_adapter == SOFTWARE_GIT_ADAPTER and include_candidate_patch:
            patch_bytes = subprocess.run(
                ["git", "-C", str(review_worktree), "diff", "--binary", candidate_digest, "--"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            ).stdout
        files: list[dict[str, Any]] = []
        total_bytes = 0
        for root in (review_scratch,):
            for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
                raw = path.read_bytes()
                total_bytes += len(raw)
                if total_bytes > 5 * 1024 * 1024:
                    raise ValueError("verification scratch artifact exceeds 5 MiB")
                files.append(
                    {
                        "path": str(path.relative_to(root)),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "content_base64": base64.b64encode(raw).decode("ascii"),
                    }
                )
        untracked = (
            subprocess.run(
                ["git", "-C", str(review_worktree), "ls-files", "--others", "--exclude-standard", "-z"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            ).stdout.split(b"\0")
            if execution_adapter == SOFTWARE_GIT_ADAPTER
            else []
        )
        for raw_path in (item for item in untracked if item):
            relative = raw_path.decode("utf-8", errors="surrogateescape")
            path = review_worktree / relative
            if not path.is_file() or path.is_symlink():
                continue
            raw = path.read_bytes()
            total_bytes += len(raw)
            if total_bytes > 5 * 1024 * 1024:
                raise ValueError("verification test artifact exceeds 5 MiB")
            files.append(
                {
                    "path": f"worktree/{relative}",
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "content_base64": base64.b64encode(raw).decode("ascii"),
                }
            )
        return self.service.artifacts.put_json(
            {
                "schema_version": "1",
                "candidate_digest": candidate_digest,
                "workspace_patch_base64": base64.b64encode(patch_bytes).decode("ascii"),
                "files": files,
            },
            artifact_type="VerificationTestWorkspaceArtifact",
        )


def _primary_json_output(terminal: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(terminal.get("payload") or {})
    artifacts = [dict(item) for item in list(payload.get("artifacts") or []) if isinstance(item, Mapping)]
    primary = dict(payload.get("primary_artifact") or {})
    if not primary:
        primary = next((item for item in artifacts if str(item.get("role") or "") == "primary"), {})
    path = Path(str(primary.get("path") or ""))
    if not path.is_file():
        raise ValueError("semantic worker did not produce a readable primary artifact")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("semantic worker primary artifact must be a JSON object")
    return value


def _named_json_output(terminal: Mapping[str, Any], filename: str) -> dict[str, Any]:
    payload = dict(terminal.get("payload") or {})
    artifacts = [dict(item) for item in list(payload.get("artifacts") or []) if isinstance(item, Mapping)]
    artifact = next(
        (
            item
            for item in artifacts
            if Path(str(item.get("path") or item.get("relative_path") or "")).name == filename
        ),
        None,
    )
    if artifact is None:
        raise ValueError(f"semantic worker did not produce {filename}")
    path = Path(str(artifact.get("path") or ""))
    if not path.is_file():
        raise ValueError(f"semantic worker produced unreadable {filename}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"semantic worker output {filename} must be a JSON object")
    return value


def _meaningful_stderr_tail(stderr: str, *, limit: int = 4000) -> str:
    lines = str(stderr or "").splitlines()
    filtered = [
        line
        for line in lines
        if "SyntaxWarning:" not in line
        and "site-packages/jieba/" not in line
        and not line.lstrip().startswith(("re_han_default =", "re_skip_default =", "re_skip ="))
    ]
    return "\n".join(filtered)[-limit:]


def _worker_event_timing(events: list[Mapping[str, Any]]) -> dict[str, int]:
    llm_started: dict[str, datetime] = {}
    tool_started: dict[str, datetime] = {}
    llm_seconds = 0.0
    tool_seconds = 0.0
    timestamps: list[datetime] = []
    for event in events:
        created_at = _event_datetime(event.get("created_at"))
        if created_at is None:
            continue
        timestamps.append(created_at)
        if str(event.get("event_kind") or "") != "progress":
            continue
        payload = dict(event.get("payload") or {})
        phase = str(payload.get("phase") or "")
        if phase == "llm_round_started":
            llm_started[str(payload.get("round") or len(llm_started) + 1)] = created_at
        elif phase == "llm_round_completed":
            started = llm_started.pop(str(payload.get("round") or ""), None)
            if started is not None:
                llm_seconds += max(0.0, (created_at - started).total_seconds())
        elif phase == "tool_call_started":
            key = f"{payload.get('round')}:{payload.get('tool_call_index')}"
            tool_started[key] = created_at
        elif phase in {"tool_call_completed", "tool_call_failed"}:
            key = f"{payload.get('round')}:{payload.get('tool_call_index')}"
            started = tool_started.pop(key, None)
            if started is not None:
                tool_seconds += max(0.0, (created_at - started).total_seconds())
    wall_seconds = max(0.0, (max(timestamps) - min(timestamps)).total_seconds()) if timestamps else 0.0
    return {
        "llm_time_ms": int(llm_seconds * 1000),
        "tool_time_ms": int(tool_seconds * 1000),
        "worker_time_ms": int(wall_seconds * 1000),
    }


def _recorded_worker_metrics(terminal: Mapping[str, Any]) -> dict[str, int]:
    timing = dict(dict(terminal.get("payload") or {}).get("v2_timing") or {})
    return {
        "latency_ms": max(0, int(timing.get("llm_time_ms") or 0)),
        "tool_latency_ms": max(0, int(timing.get("tool_time_ms") or 0)),
        "wall_latency_ms": max(0, int(timing.get("worker_time_ms") or 0)),
    }


def _worker_session_turn_index(terminal: Mapping[str, Any]) -> int:
    payload = dict(terminal.get("payload") or {})
    return max(1, int(payload.get("session_turn_index") or 1))


def _event_datetime(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def apply_v2_research_capability_policy(pack: MinionInvocationPack, *, research_mode: str) -> MinionInvocationPack:
    mode = ResearchMode(str(research_mode or ResearchMode.LOCAL_ONLY))
    if mode == ResearchMode.EXTERNAL_ALLOWED:
        return pack
    denied = {"op_web_search", "op_web_read"}
    return MinionInvocationPack.from_dict(
        {
            **pack.to_dict(),
            "allowed_capabilities": [
                capability for capability in pack.allowed_capabilities if capability not in denied
            ],
        }
    )


def apply_v2_role_capability_policy(pack: MinionInvocationPack, *, role: str) -> MinionInvocationPack:
    stage_capabilities = {
        "architect": set(ARCHITECT_BUILDER_CAPABILITIES),
        "requirements": set(REQUIREMENTS_BUILDER_CAPABILITIES),
        "planner": set(CONTRACT_SKETCH_BUILDER_CAPABILITIES),
        "architecture_reviewer": set(ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES),
    }
    allowed_for_stage = stage_capabilities.get(str(role))
    if allowed_for_stage is None:
        return pack
    forbidden_writes = {
        "op_minion_artifact_write",
        "op_minion_artifact_edit",
        "op_file_write",
        "op_file_edit",
        "op_path_delete",
    }
    capabilities = [
        capability
        for capability in pack.allowed_capabilities
        if capability not in forbidden_writes
        and (not is_contract_builder_capability(capability) or capability in allowed_for_stage)
    ]
    return MinionInvocationPack.from_dict({**pack.to_dict(), "allowed_capabilities": capabilities})


def apply_v2_revision_scope_capability_policy(pack: MinionInvocationPack) -> MinionInvocationPack:
    """Hide broad draft inspection from a repair architect.

    The builder contains the complete base privately so it can validate and
    compile a new manifest. The model receives only semantic scope reads and
    CRUD operations; the runtime rejects any diff outside that scope.
    """

    allowed_builder = {
        "op_minion_contract_revision_read",
        "op_minion_contract_validate",
        "op_minion_contract_add_gate_checks_batch",
        "op_minion_contract_delete_gate_checks_batch",
        "op_minion_contract_add_constraints_batch",
        "op_minion_contract_delete_constraints_batch",
        "op_minion_contract_add_design_decisions_batch",
        "op_minion_contract_delete_design_decisions_batch",
        "op_minion_contract_add_unit_outlines_batch",
        "op_minion_contract_replace_unit_outlines_batch",
        "op_minion_contract_delete_units_batch",
        "op_minion_contract_add_unit_acceptance_batch",
        "op_minion_contract_add_cross_unit_contracts_batch",
        "op_minion_contract_delete_cross_unit_contracts_batch",
        "op_minion_contract_set_integration",
        "op_minion_contract_submit_sketch",
    }
    capabilities = [
        capability
        for capability in pack.allowed_capabilities
        if not is_contract_builder_capability(capability) or capability in allowed_builder
    ]
    for capability in REVISION_CONTRACT_BUILDER_CAPABILITIES:
        if capability not in capabilities:
            capabilities.append(capability)
    return MinionInvocationPack.from_dict({**pack.to_dict(), "allowed_capabilities": capabilities})


def _parse_architecture_review(payload: Mapping[str, Any]) -> ArchitectureReviewResult:
    verdict = str(payload.get("verdict") or "").strip().upper()
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("architecture review verdict must be PASS or FAIL")
    from pal.minion.v2.architecture import ArchitectureFinding

    findings = []
    for item in list(payload.get("findings") or []):
        finding_kind = ArchitectureFindingKind(str(dict(item).get("finding_kind") or ""))
        findings.append(
            ArchitectureFinding(
                finding_kind=finding_kind,
                summary=str(dict(item).get("summary") or "").strip(),
                refs=tuple(str(ref) for ref in list(dict(item).get("refs") or [])),
                severity=str(dict(item).get("severity") or "error"),
                revision_targets=normalize_revision_targets(dict(item).get("revision_targets") or []),
            )
        )
    if verdict == "PASS" and findings:
        raise ValueError("PASS architecture review cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL architecture review requires typed findings")
    return ArchitectureReviewResult(verdict=verdict, findings=tuple(findings))


def _parse_skeleton_review(payload: Mapping[str, Any]) -> SkeletonReviewResult:
    verdict = str(payload.get("verdict") or "").strip().upper()
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("architecture skeleton review verdict must be PASS or FAIL")
    findings: list[SkeletonReviewFinding] = []
    for raw in list(payload.get("findings") or []):
        finding = dict(raw or {})
        finding_kind = str(finding.get("finding_kind") or "").strip()
        if finding_kind not in {"requirements_defect", "contract_defect", "architecture_defect"}:
            raise ValueError(f"invalid architecture skeleton finding kind: {finding_kind}")
        summary = str(finding.get("summary") or "").strip()
        if not summary:
            raise ValueError("architecture skeleton findings require a summary")
        findings.append(
            SkeletonReviewFinding(
                finding_kind=finding_kind,
                summary=summary,
                severity=str(finding.get("severity") or "error").strip(),
                affected_modules=tuple(str(item) for item in list(finding.get("affected_modules") or [])),
                requirements=tuple(dict(item or {}) for item in list(finding.get("requirements") or [])),
                locations=tuple(dict(item or {}) for item in list(finding.get("locations") or [])),
            )
        )
    if verdict == "PASS" and findings:
        raise ValueError("PASS architecture review cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL architecture review requires findings")
    return SkeletonReviewResult(verdict=verdict, findings=tuple(findings))


def _ref_from_mapping(value: Any) -> ArtifactRef:
    if not isinstance(value, Mapping):
        raise ValueError("artifact ref is required")
    return ArtifactRef.from_mapping(value)


def _path_pseudo_ref(path: str, name: str) -> ArtifactRef:
    import hashlib

    digest = hashlib.sha256(str(Path(path).expanduser().resolve()).encode("utf-8")).hexdigest()
    return ArtifactRef(
        sha256=digest,
        artifact_type="LocalPathReference",
        schema_version="1",
        media_type=str(Path(path).expanduser().resolve()),
        byte_size=0,
        durable=True,
    )


def _append_ref(existing: Any, value: Any) -> list[dict[str, Any]]:
    result = [dict(item) for item in list(existing or []) if isinstance(item, Mapping)]
    if isinstance(value, Mapping) and value.get("sha256"):
        digest = str(value.get("sha256"))
        if all(str(item.get("sha256") or "") != digest for item in result):
            result.append(dict(value))
    return result


def _verification_case_specs(value: Any) -> list[VerificationCaseSpec]:
    cases = [_verification_case_spec(item) for item in list(value or [])]
    names = [item.case_name for item in cases]
    if len(set(names)) != len(names):
        raise ValueError("verification case names must be unique semantic names")
    return cases


def _verification_case_spec(value: Any) -> VerificationCaseSpec:
    if not isinstance(value, Mapping):
        raise ValueError("verification case must be an object")
    name = str(value.get("name") or "").strip()
    if not name:
        raise ValueError("verification case requires a semantic name")
    command = tuple(str(item) for item in list(value.get("command") or []) if str(item))
    if not command:
        raise ValueError(f"verification case {name!r} requires a command argv")
    requirements = _semantic_requirement_refs(value.get("requirements"), owner=f"case {name!r}")
    locations = _semantic_locations(value.get("locations"), owner=f"case {name!r}")
    invariants = tuple(str(item).strip() for item in list(value.get("invariants") or []) if str(item).strip())
    if not (requirements or locations or invariants):
        raise ValueError(
            f"verification case {name!r} requires Requirement text, a source location, or an invariant"
        )
    case_kind = VerificationCaseKind(str(value.get("case_kind") or ""))
    expected_exit_codes = tuple(int(item) for item in list(value.get("expected_exit_codes") or [0]))
    case_key = hashlib.sha256(
        json.dumps(
            {
                "name": name,
                "case_kind": case_kind.value,
                "command": command,
                "expected_exit_codes": expected_exit_codes,
                "requirements": requirements,
                "locations": locations,
                "invariants": invariants,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return VerificationCaseSpec(
        case_id=f"case_{case_key[:20]}",
        case_name=name,
        case_kind=case_kind,
        command=command,
        expected_exit_codes=expected_exit_codes,
        requirements=requirements,
        locations=locations,
        invariants=invariants,
        description=str(value.get("description") or ""),
    )


_FINDING_SECTIONS = frozenset(
    {
        "ownership",
        "lifecycle",
        "state_machine",
        "invariant",
        "interface",
        "compatibility",
        "delivery",
        "implementation",
    }
)


def _verification_findings(
    plan: Mapping[str, Any],
    cases: list[VerificationCaseSpec],
) -> list[dict[str, Any]]:
    cases_by_name = {item.case_name: item for item in cases}
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in list(plan.get("findings") or []):
        if not isinstance(raw, Mapping):
            raise ValueError("verification finding must be an object")
        item = dict(raw)
        case_name = str(item.get("case") or "").strip()
        section = str(item.get("finding_section") or "implementation").strip()
        if not case_name or case_name not in cases_by_name:
            raise ValueError("verification finding must reference a declared semantic case name")
        if section not in _FINDING_SECTIONS:
            raise ValueError(f"verification finding for {case_name!r} has invalid finding_section: {section}")
        summary = str(item.get("summary") or "").strip()
        failure_reason = str(item.get("failure_reason") or "").strip()
        if not summary or not failure_reason:
            raise ValueError(f"verification finding for {case_name!r} requires summary and failure_reason")
        case = cases_by_name[case_name]
        finding = {
            "case_id": case.case_id,
            "case_name": case_name,
            "finding_section": section,
            "summary": summary,
            "failure_reason": failure_reason,
            "requirements": list(
                _semantic_requirement_refs(item.get("requirements"), owner=f"finding for {case_name!r}")
                or case.requirements
            ),
            "locations": list(
                _semantic_locations(item.get("locations"), owner=f"finding for {case_name!r}")
                or case.locations
            ),
            "invariants": [
                str(value).strip()
                for value in list(item.get("invariants") or case.invariants)
                if str(value).strip()
            ],
            "severity": str(item.get("severity") or "major"),
            "suggested_repair_boundary": [
                str(value) for value in list(item.get("suggested_repair_boundary") or [])
            ],
        }
        if not (finding["requirements"] or finding["locations"] or finding["invariants"]):
            raise ValueError(
                f"verification finding for {case_name!r} requires Requirement text, a source location, or an invariant"
            )
        finding_key = hashlib.sha256(
            json.dumps(finding, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if finding_key in seen:
            raise ValueError(f"duplicate semantic verification finding for {case_name!r}")
        findings.append(finding)
        seen.add(finding_key)
    return findings


def _finding_for_case(findings: list[dict[str, Any]], case_id: str) -> dict[str, Any]:
    return next((item for item in findings if str(item.get("case_id") or "") == case_id), {})


def _confirmed_verification_findings(
    findings: list[dict[str, Any]],
    cases: list[VerificationCaseSpec],
    results: list[Any],
) -> list[dict[str, Any]]:
    blocking = {
        item.case_id: item
        for item in results
        if item.status in {VerificationStatus.FAIL, VerificationStatus.UNKNOWN}
    }
    confirmed = [item for item in findings if str(item.get("case_id") or "") in blocking]
    described = {str(item.get("case_id") or "") for item in confirmed}
    specs = {item.case_id: item for item in cases}
    for case_id, result in blocking.items():
        if case_id in described:
            continue
        spec = specs[case_id]
        confirmed.append(
            {
                "case_id": case_id,
                "case_name": spec.case_name,
                "finding_section": "implementation",
                "summary": spec.description or f"Verification case {spec.case_name!r} did not pass",
                "failure_reason": result.summary,
                "requirements": [dict(item) for item in spec.requirements],
                "locations": [dict(item) for item in spec.locations],
                "invariants": list(spec.invariants),
                "severity": "major",
                "suggested_repair_boundary": [],
            }
        )
    return confirmed


def _semantic_requirement_refs(value: Any, *, owner: str) -> tuple[dict[str, str], ...]:
    result: list[dict[str, str]] = []
    for raw in list(value or []):
        item = dict(raw or {})
        section = str(item.get("section") or "").strip()
        requirement = str(item.get("requirement") or "").strip()
        if not section or not requirement:
            raise ValueError(f"{owner} Requirement references require section and original requirement text")
        result.append({"section": section, "requirement": requirement})
    return tuple(result)


def _semantic_locations(value: Any, *, owner: str) -> tuple[dict[str, str], ...]:
    result: list[dict[str, str]] = []
    for raw in list(value or []):
        item = dict(raw or {})
        path = str(item.get("path") or "").strip()
        if not path:
            raise ValueError(f"{owner} source locations require path")
        result.append(
            {
                "path": path,
                **({"symbol": str(item.get("symbol") or "").strip()} if item.get("symbol") else {}),
                **({"section": str(item.get("section") or "").strip()} if item.get("section") else {}),
            }
        )
    return tuple(result)


def _validate_semantic_verification_plan_shape(
    value: Mapping[str, Any],
    *,
    standalone: bool,
) -> None:
    verifier_fields = {
        "cases",
        "findings",
        "defect_kind",
        "dependency_module",
        "affected_module",
        "requirement_patch",
        "severity",
        "suggested_repair_boundary",
        "policy_exceptions",
        "reviewer_summary",
    }
    standalone_fields = {
        "verdict",
        "scope",
        "reviewed_surfaces",
        "cases",
        "findings",
        "commands_or_lsp_evidence",
        "test_gaps",
        "unreviewed_surfaces",
        "residual_risk",
        "reviewer_summary",
    }
    unknown = set(value) - (standalone_fields if standalone else verifier_fields)
    if unknown:
        raise ValueError("verification output contains unsupported fields: " + ", ".join(sorted(unknown)))
    requirement_patch = value.get("requirement_patch")
    if requirement_patch is not None:
        if not isinstance(requirement_patch, Mapping):
            raise ValueError("requirement_patch must be an object")
        allowed_patch_fields = {
            "patch_kind",
            "section",
            "requirement",
            "strength",
            "reason",
            "affected_modules",
            "affected_contracts",
        }
        extra = set(requirement_patch) - allowed_patch_fields
        if extra:
            raise ValueError(
                "requirement_patch contains Manager-owned or unsupported fields: "
                + ", ".join(sorted(extra))
            )
    case_fields = {
        "name",
        "case_kind",
        "command",
        "expected_exit_codes",
        "requirements",
        "locations",
        "invariants",
        "description",
    }
    finding_fields = {
        "case",
        "finding_section",
        "summary",
        "failure_reason",
        "requirements",
        "locations",
        "invariants",
        "severity",
        "suggested_repair_boundary",
        "evidence",
    }
    for index, raw in enumerate(list(value.get("cases") or [])):
        if not isinstance(raw, Mapping):
            raise ValueError(f"verification case {index} must be an object")
        extra = set(raw) - case_fields
        if extra:
            raise ValueError(
                f"verification case {index} contains unsupported fields: " + ", ".join(sorted(extra))
            )
    for index, raw in enumerate(list(value.get("findings") or [])):
        if not isinstance(raw, Mapping):
            raise ValueError(f"verification finding {index} must be an object")
        extra = set(raw) - finding_fields
        if extra:
            raise ValueError(
                f"verification finding {index} contains unsupported fields: " + ", ".join(sorted(extra))
            )


def _validate_skeleton_coder_report(
    value: Mapping[str, Any],
    *,
    expected_module: str,
    work_view: Mapping[str, Any],
) -> None:
    allowed = {
        "current_micro_plan",
        "completed_checklist",
        "files_inspected",
        "files_changed",
        "tests_run",
        "open_questions",
        "known_failures",
        "status",
        "summary",
        "affected_module",
        "locations",
        "requirements",
    }
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("Coder output contains unsupported fields: " + ", ".join(sorted(unknown)))
    status = str(value.get("status") or "").strip()
    if status not in {"candidate_ready", "architecture_defect", "module_split_request"}:
        raise ValueError("Coder output status must be candidate_ready, architecture_defect, or module_split_request")
    if status in {"architecture_defect", "module_split_request"}:
        if not str(value.get("summary") or "").strip():
            raise ValueError(f"Coder output status {status} requires summary")
        affected_module = str(value.get("affected_module") or "").strip()
        if not affected_module:
            raise ValueError(f"Coder output status {status} requires affected_module")
        if affected_module != str(expected_module or "").strip():
            raise ValueError(
                f"Coder output may report a defect only for its bound module {expected_module!r}"
            )
        locations = _semantic_locations(value.get("locations"), owner=f"Coder {status}")
        requirements = _semantic_requirement_refs(value.get("requirements"), owner=f"Coder {status}")
        if not (locations or requirements):
            raise ValueError(
                f"Coder output status {status} requires Requirement text or a source location"
            )
        referenced_requirements = {
            (str(item.get("section") or ""), str(item.get("requirement") or ""))
            for item in requirements
        }
        unknown_requirements = sorted(referenced_requirements - _work_view_requirement_refs(work_view))
        if unknown_requirements:
            rendered = "; ".join(f"{section}: {requirement}" for section, requirement in unknown_requirements)
            raise ValueError("Coder referenced Requirement text outside its ModuleWorkView: " + rendered)


def _reject_manager_identity_fields(value: Any, *, owner: str, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = str(raw_key)
            lowered = key.casefold()
            if (
                lowered.endswith("_id")
                or lowered.endswith("_ref")
                or lowered.endswith("_sha")
                or "sha256" in lowered
                or lowered in {"handle", "json_pointer", "artifact"}
            ):
                raise ValueError(f"{owner} contains Manager-owned identity field at {path}.{key}")
            _reject_manager_identity_fields(item, owner=owner, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_manager_identity_fields(item, owner=owner, path=f"{path}[{index}]")


def _resolve_dependency_node_id(
    repository: MinionV2Repository,
    node: AggregateSnapshot,
    *,
    dependency_module: str,
    required: bool,
) -> str:
    name = str(dependency_module or "").strip()
    if not required and not name:
        return ""
    if not name:
        raise ValueError("dependency_defect requires dependency_module")
    matches: list[str] = []
    for dependency_id in list(node.payload.get("dependency_node_ids") or []):
        dependency = repository.read_snapshot(AggregateType.DAG_NODE_RUN, str(dependency_id))
        if dependency is None:
            continue
        module_name = str(dependency.payload.get("module_name") or dependency.payload.get("unit_id") or "")
        if module_name == name:
            matches.append(dependency.aggregate_id)
    if len(matches) != 1:
        raise ValueError(f"dependency_module {name!r} does not name exactly one direct dependency")
    return matches[0]


def _validate_verifier_requirement_refs(
    *,
    work_view: Mapping[str, Any],
    cases: list[VerificationCaseSpec],
    findings: list[Mapping[str, Any]],
) -> None:
    allowed = _work_view_requirement_refs(work_view)
    referenced = {
        (str(item.get("section") or ""), str(item.get("requirement") or ""))
        for case in cases
        for item in case.requirements
    }
    referenced.update(
        (str(item.get("section") or ""), str(item.get("requirement") or ""))
        for finding in findings
        for item in list(finding.get("requirements") or [])
    )
    unknown = sorted(referenced - allowed)
    if unknown:
        rendered = "; ".join(f"{section}: {requirement}" for section, requirement in unknown)
        raise ValueError("verifier referenced Requirement text outside its ModuleWorkView: " + rendered)


def _work_view_requirement_refs(work_view: Mapping[str, Any]) -> set[tuple[str, str]]:
    raw_requirements = work_view.get("requirements")
    if isinstance(raw_requirements, Mapping):
        requirements = dict(raw_requirements)
        allowed = {
            (str(section), str(requirement))
            for section, values in dict(requirements.get("sections") or {}).items()
            for requirement in list(values or [])
        }
    else:
        allowed = {
            (
                str(dict(item or {}).get("section") or "Requirements"),
                str(dict(item or {}).get("statement") or dict(item or {}).get("requirement") or ""),
            )
            for item in list(raw_requirements or [])
        }
    integration = dict(work_view.get("integration_contract") or {})
    allowed.update(
        (str(item.get("section") or ""), str(item.get("requirement") or ""))
        for item in list(integration.get("covers") or [])
    )
    return allowed


def _validate_verification_policy(
    plan: Mapping[str, Any],
    cases: list[VerificationCaseSpec],
    policy: Mapping[str, Any],
    node: AggregateSnapshot,
) -> None:
    kinds = {item.case_kind for item in cases}
    exceptions = dict(plan.get("policy_exceptions") or {})
    obligations = (
        ("require_focused_tests", {VerificationCaseKind.UNIT, VerificationCaseKind.CONTRACT_ADVERSARIAL}, "focused_tests"),
        ("require_warning_clean", {VerificationCaseKind.COMPILE}, "warning_clean"),
        ("require_public_surface_dogfood", {VerificationCaseKind.CONSUMER_PROBE}, "public_surface_dogfood"),
    )
    for policy_key, accepted_kinds, exception_key in obligations:
        if not bool(policy.get(policy_key, False)) or kinds.intersection(accepted_kinds):
            continue
        if not str(exceptions.get(exception_key) or "").strip():
            raise ValueError(f"VerificationPolicy requires {exception_key} evidence or a concrete policy_exceptions reason")
    if (
        bool(policy.get("require_historical_regressions", False))
        and node.payload.get("historical_repair_bill_refs")
        and VerificationCaseKind.HISTORICAL_REGRESSION not in kinds
    ):
        raise ValueError("VerificationPolicy requires historical RepairBill regressions first")
    if str(policy.get("lsp_policy") or "") == "when_available" and VerificationCaseKind.LSP not in kinds:
        if not str(exceptions.get("lsp") or "").strip():
            raise ValueError("VerificationPolicy requires LSP evidence or policy_exceptions.lsp")


def _standalone_review_findings(
    plan: Mapping[str, Any],
    cases: list[VerificationCaseSpec],
) -> list[dict[str, Any]]:
    cases_by_name = {item.case_name: item for item in cases}
    findings: list[dict[str, Any]] = []
    for raw in list(plan.get("findings") or []):
        item = dict(raw or {})
        section = str(item.get("finding_section") or "implementation").strip()
        if section not in _FINDING_SECTIONS:
            raise ValueError(f"standalone finding has invalid finding_section: {section}")
        case_name = str(item.get("case") or "").strip()
        if case_name and case_name not in cases_by_name:
            raise ValueError(f"standalone finding references unknown case name: {case_name}")
        summary = str(item.get("summary") or "").strip()
        failure_reason = str(item.get("failure_reason") or "").strip()
        if not summary or not failure_reason:
            raise ValueError("standalone finding requires summary and failure_reason")
        case = cases_by_name.get(case_name)
        findings.append(
            {
                "case_id": case.case_id if case is not None else "",
                "case_name": case_name,
                "finding_section": section,
                "summary": summary,
                "failure_reason": failure_reason,
                "requirements": list(
                    _semantic_requirement_refs(item.get("requirements"), owner="standalone finding")
                    or (case.requirements if case is not None else ())
                ),
                "locations": list(
                    _semantic_locations(item.get("locations"), owner="standalone finding")
                    or (case.locations if case is not None else ())
                ),
                "invariants": [
                    str(value).strip()
                    for value in list(item.get("invariants") or (case.invariants if case is not None else ()))
                    if str(value).strip()
                ],
                "severity": str(item.get("severity") or "major"),
                "evidence": list(item.get("evidence") or []),
                "suggested_repair_boundary": [
                    str(value) for value in list(item.get("suggested_repair_boundary") or [])
                ],
            }
        )
        finding = findings[-1]
        if not (
            finding["requirements"]
            or finding["locations"]
            or finding["invariants"]
            or finding["evidence"]
        ):
            raise ValueError(
                "standalone finding requires Requirement text, a source location, an invariant, or concrete evidence"
            )
    return findings


def _standalone_review_status(
    plan: Mapping[str, Any],
    results: list[Any],
) -> VerificationStatus:
    if any(item.status == VerificationStatus.FAIL for item in results):
        return VerificationStatus.FAIL
    if any(item.status == VerificationStatus.UNKNOWN for item in results):
        return VerificationStatus.UNKNOWN
    verdict = str(plan.get("verdict") or "").strip().lower()
    if verdict == "approved":
        return VerificationStatus.PASS
    if verdict == "changes_requested":
        if not list(plan.get("findings") or []) and not any(
            item.status == VerificationStatus.FAIL for item in results
        ):
            raise ValueError("standalone changes_requested verdict requires a finding or failed case")
        return VerificationStatus.FAIL
    if verdict == "blocked":
        return VerificationStatus.UNKNOWN
    raise ValueError("standalone review verdict must be approved, changes_requested, or blocked")


def _compile_standalone_review_markdown(report: Mapping[str, Any]) -> str:
    """Render the semantic review result without leaking Manager-owned identities."""

    status = str(report.get("status") or VerificationStatus.UNKNOWN)
    lines = ["# Standalone Review", "", f"**Status:** {status}"]
    summary = str(report.get("reviewer_summary") or "").strip()
    if summary:
        lines.extend(("", summary))

    findings = [dict(item or {}) for item in list(report.get("findings") or [])]
    lines.extend(("", "## Findings"))
    if not findings:
        lines.append("- No findings.")
    for index, finding in enumerate(findings, start=1):
        severity = str(finding.get("severity") or "major").upper()
        section = str(finding.get("finding_section") or "implementation")
        finding_summary = str(finding.get("summary") or "Finding").strip()
        lines.extend(("", f"### {index}. [{severity}] {finding_summary}", f"- Area: {section}"))
        case_name = str(finding.get("case") or "").strip()
        if case_name:
            lines.append(f"- Case: {case_name}")
        reason = str(finding.get("failure_reason") or "").strip()
        if reason:
            lines.append(f"- Reason: {reason}")
        for requirement in list(finding.get("requirements") or []):
            item = dict(requirement or {})
            lines.append(
                "- Requirement: "
                f"{str(item.get('section') or 'Requirements')} - "
                f"{str(item.get('requirement') or '')}"
            )
        for location in list(finding.get("locations") or []):
            item = dict(location or {})
            label = str(item.get("path") or "")
            if item.get("symbol"):
                label += f"::{str(item['symbol'])}"
            if item.get("section"):
                label += f" ({str(item['section'])})"
            lines.append(f"- Location: {label}")
        for invariant in list(finding.get("invariants") or []):
            lines.append(f"- Invariant: {str(invariant)}")

    cases = [dict(item or {}) for item in list(report.get("cases") or [])]
    lines.extend(("", "## Verification Cases"))
    if not cases:
        lines.append("- No executable cases were required.")
    for case in cases:
        name = str(case.get("name") or "unnamed case")
        case_status = str(case.get("status") or VerificationStatus.UNKNOWN)
        command = json.dumps(list(case.get("command") or []), ensure_ascii=False)
        lines.append(f"- **{name}**: {case_status}; command `{command}`")

    for heading, key in (
        ("Test Gaps", "test_gaps"),
        ("Unreviewed Surfaces", "unreviewed_surfaces"),
        ("Residual Risk", "residual_risk"),
    ):
        values = [str(item) for item in list(report.get(key) or []) if str(item).strip()]
        if not values:
            continue
        lines.extend(("", f"## {heading}", *(f"- {item}" for item in values)))
    return "\n".join(lines).strip() + "\n"


def _defect_kind(plan: Mapping[str, Any], node: AggregateSnapshot) -> DefectKind:
    raw = str(plan.get("defect_kind") or "").strip()
    if raw:
        return DefectKind(raw)
    if str(node.payload.get("node_kind") or "") == "integration":
        return DefectKind.INTEGRATION
    return DefectKind.MODULE


def _manager_unknown_policy(node: AggregateSnapshot) -> UnknownPolicy:
    raw = dict(node.payload.get("unknown_policy") or {})
    return UnknownPolicy(
        architecture_allows_platform_unknown=bool(raw.get("architecture_allows_platform_unknown")),
        assumption_ref=dict(raw.get("assumption_ref") or {}) or None,
        hard_or_core_semantics=bool(raw.get("hard_or_core_semantics", True)),
        human_waiver_ref=dict(raw.get("human_waiver_ref") or {}) or None,
    )


def _prepare_standalone_review_workspace(
    runtime_root: Path,
    review_id: str,
    source: Path,
) -> tuple[Path, Path, str]:
    source = source.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"standalone review source does not exist: {source}")
    root = Path(runtime_root) / "data" / "minion" / "v2" / "standalone-reviews" / _safe_component(review_id)
    review_repo = root / "worktree"
    scratch = root / "scratch"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    git_probe = subprocess.run(
        ["git", "-C", str(source), "rev-parse", "HEAD"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if git_probe.returncode != 0:
        raise ValueError("standalone software review requires a Git repository")
    base_sha = git_probe.stdout.strip()
    clone = subprocess.run(
        ["git", "clone", "--no-hardlinks", "--quiet", str(source), str(review_repo)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if clone.returncode != 0:
        raise RuntimeError(clone.stderr or clone.stdout or "failed to clone standalone review workspace")
    subprocess.run(["git", "-C", str(review_repo), "checkout", "--detach", "--quiet", base_sha], check=True)
    scratch.mkdir(parents=True, exist_ok=True)
    return review_repo, scratch, base_sha


def _safe_component(value: str) -> str:
    return "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in value)


def _topological_implementation_nodes(nodes: list[AggregateSnapshot]) -> list[AggregateSnapshot]:
    by_id = {item.aggregate_id: item for item in nodes}
    pending = {
        item.aggregate_id: {
            str(dependency)
            for dependency in list(item.payload.get("dependency_node_ids") or [])
            if str(dependency) in by_id
        }
        for item in nodes
    }
    ordered: list[AggregateSnapshot] = []
    while pending:
        ready = sorted(node_id for node_id, dependencies in pending.items() if not dependencies)
        if not ready:
            raise ValueError("implementation Candidate graph contains a cycle during final union")
        for node_id in ready:
            ordered.append(by_id[node_id])
            pending.pop(node_id)
        for dependencies in pending.values():
            dependencies.difference_update(ready)
    return ordered


def _lease_is_live(lease: Mapping[str, Any]) -> bool:
    raw = str(lease.get("expires_at") or "")
    if not raw:
        return False
    try:
        expires_at = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    return expires_at > datetime.now(timezone.utc)


def _clarification_question_text(value: Any) -> str:
    if not isinstance(value, Mapping):
        return str(value)
    item = dict(value)
    return str(item.get("question") or item.get("clarification") or item.get("topic") or "Clarification required")
