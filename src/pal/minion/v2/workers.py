from __future__ import annotations

import asyncio
import contextlib
import base64
import json
import hashlib
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
    ArchitectureReviewResult,
    ResearchMode,
    architecture_manifest_child_refs,
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
from pal.minion.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.minion.v2.contract_builder import (
    ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES,
    CONTRACT_SKETCH_BUILDER_CAPABILITIES,
    EVIDENCE_BUILDER_CAPABILITIES,
    REQUIREMENTS_BUILDER_CAPABILITIES,
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
from pal.minion.v2.integration import IntegrationOwnershipDefect, IntegrationService
from pal.minion.v2.verification import (
    DefectKind,
    UnknownPolicy,
    VerificationCaseKind,
    VerificationCaseRunner,
    VerificationCaseSpec,
    VerificationService,
    VerificationStatus,
    aggregate_verification_status,
)
from pal.shared import MinionInvocationPack


HumanReviewPublisher = Callable[[Mapping[str, Any]], Awaitable[None]]
WorkerEventPublisher = Callable[[Mapping[str, Any]], Awaitable[None]]
BrokerRunRegistrar = Callable[[str, str, MinionInvocationPack, asyncio.subprocess.Process], None]
BrokerRunUnregistrar = Callable[[str], None]


_ARCHITECTURE_STAGE_CONFIG = {
    "requirements": ("requirements", "START_REQUIREMENTS"),
    "research": ("research", "START_RESEARCH"),
    "planning": ("planner", "START_PLANNING"),
}

SEMANTIC_EFFECT_TYPES = frozenset(
    {
        "enqueue_architecture_stage",
        "enqueue_architecture_review",
        "publish_human_architecture_review",
        "request_human_clarification",
        "reconcile_architecture_revision",
        "enqueue_producer",
        "spawn_producer_worker",
        "enqueue_node_review",
        "spawn_verifier_worker",
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
        }[action_type]
        if node.state == target_state and node.payload.get("active_worker_id"):
            return {"provider_request_id": str(node.payload.get("active_worker_id"))}
        cycle = int(node.payload.get("candidate_cycle") or 0) + (1 if role in {"producer", "repair"} else 0)
        invocation_id = f"inv_{hashlib.sha256(f'{node.aggregate_id}:{role}:{cycle}'.encode()).hexdigest()[:24]}"
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
            references["repair_bill"] = _ref_from_mapping(repair_ref)
        instruction = (
            "Repair only the defects in the bound RepairBill. Regress the reviewer reproducer first, add the relevant regression "
            "test to the project, and make the smallest contract-preserving change. Do not revisit unrelated code."
            if repair
            else "Implement the bound UnitWorkView. Start from its approved evidence and contract, write focused tests first, and stay inside owned_area."
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
            },
            prepare_workspace=False,
        )
        report = _primary_json_output(terminal)
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
            turn_index=1,
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
                },
            )
        )
        return {"provider_request_id": invocation_id, "result_artifact_ref": report_ref.to_dict()}

    async def _quiesce_node(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
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
                    owned_area=[str(item) for item in list(contract.get("owned_area") or [])],
                    reference_only_paths=[str(item) for item in list(contract.get("reference_only_paths") or [])],
                    base_sha=base_sha,
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
                        owned_area=[str(item) for item in list(contract.get("owned_area") or [])],
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

    async def _run_verifier(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        invocation_id = str(node.payload.get("active_worker_id") or "")
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        candidate_ref = _ref_from_mapping(node.payload.get("candidate_ref"))
        candidate_digest = str(node.payload.get("candidate_digest") or "")
        adapter = self._execution_adapter(node)
        if adapter == SOFTWARE_GIT_ADAPTER:
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
        if str(node.payload.get("node_kind") or "") == "integration":
            view_ref = self.service.artifacts.put_json(
                {
                    "schema_version": "1",
                    "node_run_id": node.aggregate_id,
                    "node_kind": "integration",
                    "integration_contract": self.service.artifacts.read_json(dict(node.payload["unit_contract_ref"])),
                    "dependency_node_ids": list(node.payload.get("dependency_node_ids") or []),
                    "accepted_dependency_candidate_digests": list(node.payload.get("accepted_dependency_candidate_digests") or []),
                    "verification_obligations": ["full build", "full test", "cross-unit lifecycle and interface adversarial probes"],
                },
                artifact_type="IntegrationWorkViewArtifact",
                child_refs=((str(dict(node.payload["unit_contract_ref"])["sha256"]), "integration_contract"),),
            )
        else:
            view_ref = UnitWorkViewBuilder(self.service.architecture).build(
                node,
                dependency_outputs=dict(node.payload.get("dependency_outputs") or {}),
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
                "Generate and run adversarial verification for the bound candidate. Historical RepairBills come first. "
                "Write reproducible case commands; the manager will rerun them and owns the verdict."
            ),
            reference_refs={"unit_work_view": view_ref, "candidate": candidate_ref},
            workspace_override={
                "kind": "existing_repo",
                "repo_path": str(review_workspace),
                "project_name": str(node.payload.get("unit_id") or "unit"),
            },
            prepare_workspace=False,
        )
        plan = _primary_json_output(terminal)
        case_specs = [_verification_case_spec(item) for item in list(plan.get("cases") or [])]
        findings = _verification_findings(plan, case_specs)
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
        fingerprint = ""
        defect_kind = _defect_kind(plan, node)
        dependency_node_id = str(plan.get("dependency_node_id") or "")
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
                candidate_digest=str(node.payload.get("candidate_digest") or ""),
                verification_ref=report_ref,
                defect_kind=defect_kind,
                severity=str(finding.get("severity") or plan.get("severity") or "major"),
                contract_refs=tuple(finding.get("contract_refs") or first_failure.contract_refs),
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
                affected_refs=list(finding.get("affected_refs") or []),
                finding_id=str(finding.get("finding_id") or ""),
            )
        unknown_policy = _unknown_policy(plan)
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
            candidate_tree_hash=str(node.payload.get("candidate_digest") or ""),
            defect_kind=defect_kind,
            dependency_node_id=dependency_node_id,
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
        return {}

    async def _resume_aggregate(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        snapshot = self._effect_snapshot(effect)
        if snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
            stage_by_state = {
                "REQUIREMENTS_QUEUED": "requirements",
                "REQUIREMENTS_RUNNING": "requirements",
                "RESEARCH_QUEUED": "research",
                "RESEARCH_RUNNING": "research",
                "PLANNING_QUEUED": "planning",
                "PLANNING_RUNNING": "planning",
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
        if snapshot.aggregate_type == AggregateType.STANDALONE_REVIEW:
            if snapshot.state == "REVIEW_QUEUED":
                return self._admit_standalone_review(effect)
            if snapshot.state == "REPORT_READY":
                return await self._publish_standalone_report(effect)
        return {}

    def _resume_node(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        if node.state == "QUEUED":
            return self._admit_node_worker(effect, action_type="START_PRODUCING", role="producer")
        if node.state == "REVIEW_QUEUED":
            return self._admit_node_worker(effect, action_type="START_REVIEW", role="reviewer")
        if node.state == "REPAIR_QUEUED":
            return self._admit_node_worker(effect, action_type="START_REPAIR", role="repair")
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
            raise ValueError("final publish requires an ACCEPTED integration node")
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

    async def _run_standalone_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        review = self._effect_snapshot(effect)
        invocation_id = str(review.payload.get("active_worker_id") or "")
        lease_resource = str(review.payload.get("lease_resource_key") or "")
        fencing_token = int(review.payload.get("fencing_token") or 0)
        request_ref = _ref_from_mapping(review.payload.get("review_request_ref"))
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, review.workflow_id)
        request = workflow_request_from_snapshot(self.service, workflow)
        workspace = dict(request.get("workspace") or {})
        repo_path = str(workspace.get("repo_path") or workspace.get("cwd") or self.service.runtime_root)
        review_repo, review_scratch, base_sha = _prepare_standalone_review_workspace(
            self.service.runtime_root,
            review.aggregate_id,
            Path(repo_path),
        )
        terminal, prompt_ref, terminal_ref = await self._run_profile(
            effect=effect,
            snapshot=review,
            invocation_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            profile=self._profile_for_role_or(review.workflow_id, "reviewer", fallback="verifier"),
            role_override="reviewer",
            instruction="Perform the requested standalone review. Report evidence-grounded findings and do not modify the target. Repair is a separate explicit workflow.",
            reference_refs={"review_request": request_ref},
            workspace_override={"kind": "existing_repo", "repo_path": str(review_repo), "project_name": "standalone-review"},
            prepare_workspace=False,
        )
        plan = _primary_json_output(terminal)
        case_specs = [_verification_case_spec(item) for item in list(plan.get("cases") or [])]
        findings = _standalone_review_findings(plan, case_specs)
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
        )
        status = _standalone_review_status(plan, results)
        report_ref = self.service.artifacts.put_json(
            {
                "schema_version": "1",
                "review_id": review.aggregate_id,
                "status": status.value,
                "cases": [item.to_dict() for item in results],
                "findings": findings,
                "reviewer_summary": str(plan.get("reviewer_summary") or ""),
                "scope": plan.get("scope") or {},
                "reviewed_surfaces": list(plan.get("reviewed_surfaces") or []),
                "commands_or_lsp_evidence": list(plan.get("commands_or_lsp_evidence") or []),
                "test_gaps": list(plan.get("test_gaps") or []),
                "unreviewed_surfaces": list(plan.get("unreviewed_surfaces") or []),
                "residual_risk": list(plan.get("residual_risk") or []),
                "test_workspace_ref": test_workspace_ref.to_dict(),
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

    async def _publish_standalone_report(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        review = self._effect_snapshot(effect)
        report_ref = dict(review.payload.get("verification_artifact_ref") or {})
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, review.workflow_id)
        workflow_request = workflow_request_from_snapshot(self.service, workflow)
        if self.publish_human_review is not None:
            await self.publish_human_review(
                {
                    "workflow_id": review.workflow_id,
                    "standalone_review_id": review.aggregate_id,
                    "report_ref": report_ref,
                    "route": dict(workflow.payload.get("control_route") or {}),
                    "summary": "Minion V2 standalone review completed.",
                }
            )
        current = self.repository.read_snapshot(AggregateType.STANDALONE_REVIEW, review.aggregate_id)
        action_type = "ACKNOWLEDGE_REPORT"
        payload: dict[str, Any] = {}
        report = self.service.artifacts.read_json(report_ref)
        if (
            str(workflow_request.get("operation") or "") == "review_and_repair"
            and str(report.get("status") or "") == VerificationStatus.FAIL
        ):
            manifest_ref = self._compile_review_repair_manifest(review, workflow_request, report_ref)
            action_type = "HANDOFF_REPAIR"
            payload["architecture_manifest_ref"] = manifest_ref.to_dict()
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

    def _compile_review_repair_manifest(
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
        evidence_items = []
        for index, case in enumerate(list(report.get("cases") or [])):
            evidence_items.append(
                {
                    "evidence_id": f"RR-E-{index + 1}",
                    "source_kind": "verification",
                    "location": f"verification:{review.aggregate_id}:{case.get('case_id', index)}",
                    "summary": str(case.get("summary") or case.get("status") or "review evidence"),
                    "supports_requirement_ids": requirement_ids,
                    "content_sha256": str(report_ref.get("sha256") or ""),
                }
            )
        if not evidence_items:
            evidence_items.append(
                {
                    "evidence_id": "RR-E-1",
                    "source_kind": "review",
                    "location": f"review:{review.aggregate_id}",
                    "summary": "Standalone review report",
                    "supports_requirement_ids": requirement_ids,
                    "content_sha256": str(report_ref.get("sha256") or ""),
                }
            )
        evidence_ref = self.service.architecture.publish_evidence_catalog(
            {"evidence": evidence_items},
            requirements_ref=requirements_ref,
            research_mode=ResearchMode.LOCAL_ONLY,
        )
        unit_contract = {
            **seed,
            "unit_id": str(seed.get("unit_id") or "review_repair"),
            "requirement_ids": requirement_ids,
            "evidence_ids": [item["evidence_id"] for item in evidence_items],
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
                "evidence_catalog_ref": evidence_ref.to_dict(),
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
        stage = str(dict(effect.get("payload") or {}).get("stage") or "")
        if stage not in _ARCHITECTURE_STAGE_CONFIG:
            raise ValueError(f"unknown architecture stage: {stage}")
        role, start_action = _ARCHITECTURE_STAGE_CONFIG[stage]
        running_state = {
            "requirements": "REQUIREMENTS_RUNNING",
            "research": "RESEARCH_RUNNING",
            "planning": "PLANNING_RUNNING",
        }[stage]
        rebind_action = {
            "requirements": "REBIND_REQUIREMENTS",
            "research": "REBIND_RESEARCH",
            "planning": "REBIND_PLANNING",
        }[stage]
        revision = self._effect_snapshot(effect)
        profile = self._profile_for_role(revision.workflow_id, role)
        if self._architecture_worker_suppressed(revision, running_state=running_state, start_action=start_action):
            return {"status": "superseded"}
        invocation_id = f"inv_{str(effect['effect_id']).removeprefix('eff_')}"
        lease_resource = f"architecture:{revision.aggregate_id}:{stage}"
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
            if stage == "research" and str(revision.payload.get("research_mode") or "") == ResearchMode.NONE:
                workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
                request = workflow_request_from_snapshot(self.service, workflow)
                result_ref = self._accept_architecture_stage_output(
                    stage,
                    revision,
                    {"evidence": list(request.get("approved_evidence") or [])},
                )
                return {"provider_request_id": invocation_id, "result_artifact_ref": result_ref.to_dict()}
            if stage == "planning":
                try:
                    self.service.architecture.validate_evidence_coverage(
                        requirements_ref=_ref_from_mapping(revision.payload.get("requirements_ref")),
                        evidence_ref=_ref_from_mapping(revision.payload.get("evidence_catalog_ref")),
                    )
                except (TypeError, ValueError) as exc:
                    finding_ref = self.service.artifacts.put_json(
                        {
                            "finding_kind": ArchitectureFindingKind.EVIDENCE_GAP.value,
                            "summary": str(exc),
                            "requirements_ref": revision.payload.get("requirements_ref"),
                            "evidence_catalog_ref": revision.payload.get("evidence_catalog_ref"),
                        },
                        artifact_type="ArchitectureFindingArtifact",
                    )
                    current = self.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
                    self.repository.dispatch(
                        ActionEnvelope(
                            action_type="EVIDENCE_GAP",
                            workflow_id=revision.workflow_id,
                            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                            aggregate_id=revision.aggregate_id,
                            actor="minion-v2-planning-preflight",
                            expected_version=current.version,
                            idempotency_key=f"planning-evidence-gap:{revision.aggregate_id}:{finding_ref.sha256}",
                            payload={"finding_artifact_ref": finding_ref.to_dict()},
                        )
                    )
                    return {"result_artifact_ref": finding_ref.to_dict()}
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
                workspace_override={"kind": "artifact_only"} if stage == "requirements" else None,
                prepare_workspace=stage != "requirements",
            )
            output = _primary_json_output(terminal)
            result_ref = self._accept_architecture_stage_output(stage, revision, output)
            self.repository.record_worker_turn(
                invocation_id=invocation_id,
                fencing_token=lease.fencing_token,
                turn_index=1,
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

    async def _run_architecture_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = self._effect_snapshot(effect)
        if self._architecture_worker_suppressed(
            revision,
            running_state="REVIEWING",
            start_action="START_ARCHITECTURE_REVIEW",
        ):
            return {"status": "superseded"}
        manifest_ref = _ref_from_mapping(revision.payload.get("architecture_manifest_ref"))
        invocation_id = f"inv_{str(effect['effect_id']).removeprefix('eff_')}"
        lease_resource = f"architecture:{revision.aggregate_id}:review"
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
                "Review the bound ArchitectureContractArtifact and its attached fragments by tracing only its requirements, evidence, contracts, topology, "
                "ownership, lifecycle, state, invariants, complexity, and integration claims. The manager's mechanical validation "
                "already passed. Find semantic omissions or contradictions; do not redesign it."
            )
            manifest = self.service.artifacts.read_json(manifest_ref)
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

    async def _publish_human_architecture_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = self._effect_snapshot(effect)
        manifest_ref = _ref_from_mapping(revision.payload.get("architecture_manifest_ref"))
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            raise ValueError("architecture revision has no workflow")
        actor = str(workflow.payload.get("owner") or "pal")
        channel = str(workflow.payload.get("active_channel") or "local")
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
        refs: dict[str, ArtifactRef] = {"workflow_request": request_ref}
        base_manifest_value = revision.payload.get("base_architecture_manifest_ref")
        if not base_manifest_value and revision.payload.get("finding_artifact_ref"):
            base_manifest_value = revision.payload.get("architecture_manifest_ref")
        if base_manifest_value:
            base_manifest_ref = _ref_from_mapping(base_manifest_value)
            refs["base_architecture_manifest"] = base_manifest_ref
            base_manifest = self.service.artifacts.read_json(base_manifest_ref)
            for field_name, value in base_manifest.items():
                if field_name.endswith("_ref") and isinstance(value, Mapping) and value.get("sha256"):
                    refs[f"base_{field_name.removesuffix('_ref')}"] = _ref_from_mapping(value)
                elif field_name.endswith("_refs") and isinstance(value, list):
                    for index, item in enumerate(value):
                        if isinstance(item, Mapping) and item.get("sha256"):
                            refs[f"base_{field_name.removesuffix('_refs')}_{index}"] = _ref_from_mapping(item)
        if revision.payload.get("edit_instruction_ref"):
            refs["edit_instruction"] = _ref_from_mapping(revision.payload.get("edit_instruction_ref"))
        finding_value = revision.payload.get("finding_artifact_ref") or revision.payload.get("replan_finding_ref")
        if finding_value:
            refs["revision_finding"] = _ref_from_mapping(finding_value)
        if stage == "research" and revision.payload.get("evidence_catalog_ref") and (
            finding_value or revision.payload.get("edit_instruction_ref")
        ):
            refs["base_evidence_catalog"] = _ref_from_mapping(revision.payload.get("evidence_catalog_ref"))
        if stage == "requirements":
            instruction = (
                "Normalize the WorkflowRequestArtifact into stable atomic requirements. Preserve source scope and non-goals. "
                "Do not research or design architecture."
            )
            if "edit_instruction" in refs:
                instruction += (
                    " This is a revision: apply only the bound edit instruction, preserve all unrelated accepted requirements, "
                    "and do not broaden scope."
                )
            if "revision_finding" in refs:
                instruction += " Correct only the bound requirements finding and preserve every unaffected requirement verbatim."
            if revision.payload.get("clarification_response_ref"):
                refs["previous_requirements"] = _ref_from_mapping(revision.payload.get("requirements_ref"))
                refs["clarification_response"] = _ref_from_mapping(revision.payload.get("clarification_response_ref"))
                instruction += (
                    " Resolve only the bound clarification against the previous RequirementsArtifact, preserve stable IDs and "
                    "all unrelated requirements, then clear answered open clarifications."
                )
        elif stage == "research":
            requirements_ref = _ref_from_mapping(revision.payload.get("requirements_ref"))
            refs["requirements"] = requirements_ref
            instruction = (
                f"Build the Evidence Catalog for the bound RequirementsArtifact. research_mode={revision.payload.get('research_mode', 'local_only')}. "
                "Use declared local reference roots as truth sources and collect only enough precise evidence for implementation."
            )
            if "base_evidence_catalog" in refs:
                instruction += (
                    " This is a revision: retain every unaffected base evidence entry byte-for-byte and change only entries required "
                    "by the bound edit or finding. Do not repeat research already supported by approved evidence."
                )
        else:
            refs["requirements"] = _ref_from_mapping(revision.payload.get("requirements_ref"))
            refs["evidence_catalog"] = _ref_from_mapping(revision.payload.get("evidence_catalog_ref"))
            instruction = (
                "Create the architecture contract bundle from the bound requirements and evidence. Define module topology and contracts, "
                "ownership, lifecycle/state/invariants, structured complexity, and integration. Do not create milestones or test matrices."
            )
            if "base_architecture_manifest" in refs:
                instruction += (
                    " Revise the base contract surgically: apply only the bound edit or typed finding, reproduce every unaffected "
                    "fragment's JSON content exactly, and preserve module IDs, topology, contracts, and prose outside that scope."
                )
        if stage == "research":
            for index, reference in enumerate(list(request.get("references") or [])):
                if not isinstance(reference, Mapping):
                    continue
                path = str(reference.get("path") or "").strip()
                if path and Path(path).expanduser().exists():
                    name = str(reference.get("name") or f"user_reference_{index + 1}").strip()
                    refs[f"user_{name}"] = _path_pseudo_ref(path, name)
        return instruction, refs

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
        builder_stages = {
            "requirements": "requirements",
            "research": "evidence",
            "planner": "contract",
            "architecture_reviewer": "architecture_review",
        }
        if role in builder_stages:
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
                }
            )
        workspace["reference_paths"] = references
        profile_group, profile_name = profile.rsplit(".", 1)
        pack = MinionInvocationPack(
            invocation_id=invocation_id,
            goal=instruction,
            instruction=instruction,
            acceptance_criteria=["Write the exact primary JSON artifact required by the profile output contract."],
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
        registry = MinionProfileRegistry(runtime_root=self.service.runtime_root)
        pack = registry.resolve_pack(pack)
        pack = apply_v2_role_capability_policy(pack, role=role)
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
        if role == "planner" and "base_architecture_manifest" in bound_reference_refs:
            seed_contract_builder_draft(
                pack.workspace,
                self._base_contract_builder_payload(bound_reference_refs),
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
            error_tail = stderr.decode("utf-8", errors="replace")[-4000:]
            self.repository.finish_worker_invocation(
                invocation_id=invocation_id,
                fencing_token=fencing_token,
                status="failed",
            )
            raise RuntimeError(worker_error or error_tail or f"V2 worker exited {process.returncode}")
        terminal = next((item for item in reversed(events) if str(item.get("event_kind") or "") == "terminal"), None)
        if terminal is None:
            self.repository.finish_worker_invocation(
                invocation_id=invocation_id,
                fencing_token=fencing_token,
                status="failed",
            )
            raise RuntimeError("V2 semantic worker ended without terminal event")
        terminal_payload = dict(terminal.get("payload") or {})
        if str(terminal_payload.get("status") or "") != "completed":
            self.repository.finish_worker_invocation(
                invocation_id=invocation_id,
                fencing_token=fencing_token,
                status="failed",
            )
            raise RuntimeError(str(terminal_payload.get("summary") or "V2 semantic worker failed"))
        terminal_payload["v2_timing"] = _worker_event_timing(events)
        terminal = {**terminal, "payload": terminal_payload}
        terminal_ref = self.service.artifacts.put_json(
            terminal,
            artifact_type="WorkerTerminalArtifact",
            child_refs=((prompt_ref.sha256, "prompt_pack"),),
        )
        self.repository.finish_worker_invocation(
            invocation_id=invocation_id,
            fencing_token=fencing_token,
            status="completed",
        )
        return terminal, prompt_ref, terminal_ref

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

    def _accept_architecture_stage_output(
        self,
        stage: str,
        revision: AggregateSnapshot,
        output: Mapping[str, Any],
    ) -> ArtifactRef:
        if stage == "requirements":
            ref = self.service.architecture.publish_requirements(
                output,
                provenance={"architecture_revision_id": revision.aggregate_id, "stage": stage},
            )
            clarifications = list(output.get("open_clarifications") or [])
            blocking_clarifications = [
                item
                for item in clarifications
                if not isinstance(item, Mapping) or bool(item.get("blocking", True))
            ]
            if blocking_clarifications:
                result_ref = self.service.artifacts.put_json(
                    {"questions": blocking_clarifications, "requirements_ref": ref.to_dict()},
                    artifact_type="ClarificationRequestArtifact",
                    child_refs=((ref.sha256, "requirements"),),
                )
                action_type = "CLARIFICATION_REQUIRED"
                payload = {
                    "requirements_ref": ref.to_dict(),
                    "clarification_ref": result_ref.to_dict(),
                }
            else:
                result_ref = ref
                action_type = "REQUIREMENTS_COMPLETED"
                payload = {"requirements_ref": ref.to_dict()}
        elif stage == "research":
            requirements_ref = _ref_from_mapping(revision.payload.get("requirements_ref"))
            ref = self.service.architecture.publish_evidence_catalog(
                output,
                requirements_ref=requirements_ref,
                research_mode=ResearchMode(str(revision.payload.get("research_mode") or "local_only")),
                provenance={"architecture_revision_id": revision.aggregate_id, "stage": stage},
            )
            action_type = "RESEARCH_COMPLETED"
            payload = {"evidence_catalog_ref": ref.to_dict()}
            result_ref = ref
        else:
            ref = self._publish_planning_bundle(revision, output)
            action_type = "PLANNING_COMPLETED"
            payload = {"architecture_manifest_ref": ref.to_dict()}
            result_ref = ref
        current = self.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
        self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=revision.workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision.aggregate_id,
                actor="minion-v2-worker",
                expected_version=current.version,
                idempotency_key=f"stage-output:{revision.aggregate_id}:{stage}:{result_ref.sha256}",
                payload=payload,
            )
        )
        return result_ref

    def _publish_planning_bundle(self, revision: AggregateSnapshot, output: Mapping[str, Any]) -> ArtifactRef:
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
        provenance = {"architecture_revision_id": revision.aggregate_id, "stage": "planning"}
        constraints = self.service.architecture.publish_fragment(output["global_constraints"], artifact_type="GlobalConstraintsArtifact", provenance=provenance)
        decisions = self.service.architecture.publish_fragment(output["design_decisions"], artifact_type="DesignDecisionsArtifact", provenance=provenance)
        gates = self.service.architecture.publish_fragment(output["gate_checks"], artifact_type="ArchitectureGateChecksArtifact", provenance=provenance)
        units = [self.service.architecture.publish_unit_contract(item, provenance=provenance) for item in list(output["units"] or [])]
        cross = [
            self.service.architecture.publish_fragment(item, artifact_type="CrossUnitContractArtifact", provenance=provenance)
            for item in list(output["cross_unit_contracts"] or [])
        ]
        topology = self.service.architecture.publish_fragment(output["topology"], artifact_type="TopologyArtifact", provenance=provenance)
        integration = self.service.architecture.publish_fragment(output["integration_contract"], artifact_type="IntegrationContractArtifact", provenance=provenance)
        assumptions = self.service.architecture.publish_fragment(output["assumption_ledger"], artifact_type="AssumptionLedgerArtifact", provenance=provenance)
        risks = self.service.architecture.publish_fragment(output["risk_ledger"], artifact_type="RiskLedgerArtifact", provenance=provenance)
        return self.service.architecture.publish_manifest(
            {
                "requirements_ref": dict(revision.payload["requirements_ref"]),
                "evidence_catalog_ref": dict(revision.payload["evidence_catalog_ref"]),
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
                ArchitectureFindingKind.REQUIREMENTS_DEFECT: "REQUIREMENTS_DEFECT",
                ArchitectureFindingKind.EVIDENCE_GAP: "EVIDENCE_GAP",
                ArchitectureFindingKind.CONTRACT_DEFECT: "CONTRACT_DEFECT",
                ArchitectureFindingKind.ARCHITECTURE_DEFECT: "ARCHITECTURE_DEFECT",
            }[finding.finding_kind]
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
    ) -> ArtifactRef:
        patch_bytes = b""
        if execution_adapter == SOFTWARE_GIT_ADAPTER:
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
        "requirements": set(REQUIREMENTS_BUILDER_CAPABILITIES),
        "research": set(EVIDENCE_BUILDER_CAPABILITIES),
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
            )
        )
    if verdict == "PASS" and findings:
        raise ValueError("PASS architecture review cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL architecture review requires typed findings")
    return ArchitectureReviewResult(verdict=verdict, findings=tuple(findings))


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


def _verification_case_spec(value: Any) -> VerificationCaseSpec:
    if not isinstance(value, Mapping):
        raise ValueError("verification case must be an object")
    command = tuple(str(item) for item in list(value.get("command") or []) if str(item))
    if not command:
        raise ValueError("verification case requires a command argv")
    return VerificationCaseSpec(
        case_id=str(value.get("case_id") or "").strip(),
        case_kind=VerificationCaseKind(str(value.get("case_kind") or "")),
        command=command,
        expected_exit_codes=tuple(int(item) for item in list(value.get("expected_exit_codes") or [0])),
        contract_refs=tuple(str(item) for item in list(value.get("contract_refs") or [])),
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
    case_ids = {item.case_id for item in cases if item.case_id}
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(list(plan.get("findings") or []), start=1):
        if not isinstance(raw, Mapping):
            raise ValueError("verification finding must be an object")
        item = dict(raw)
        finding_id = str(item.get("finding_id") or f"F-{index}").strip()
        case_id = str(item.get("case_id") or "").strip()
        section = str(item.get("finding_section") or "implementation").strip()
        if not finding_id or finding_id in seen:
            raise ValueError(f"invalid or duplicate verification finding_id: {finding_id or '<empty>'}")
        if not case_id or case_id not in case_ids:
            raise ValueError(f"verification finding {finding_id} must reference a declared case_id")
        if section not in _FINDING_SECTIONS:
            raise ValueError(f"verification finding {finding_id} has invalid finding_section: {section}")
        summary = str(item.get("summary") or "").strip()
        failure_reason = str(item.get("failure_reason") or "").strip()
        if not summary or not failure_reason:
            raise ValueError(f"verification finding {finding_id} requires summary and failure_reason")
        findings.append(
            {
                "finding_id": finding_id,
                "case_id": case_id,
                "finding_section": section,
                "summary": summary,
                "failure_reason": failure_reason,
                "affected_refs": [str(value) for value in list(item.get("affected_refs") or [])],
                "contract_refs": [str(value) for value in list(item.get("contract_refs") or [])],
                "severity": str(item.get("severity") or "major"),
                "suggested_repair_boundary": [
                    str(value) for value in list(item.get("suggested_repair_boundary") or [])
                ],
            }
        )
        seen.add(finding_id)
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
                "finding_id": f"F-{len(confirmed) + 1}",
                "case_id": case_id,
                "finding_section": "implementation",
                "summary": spec.description or f"Verification case {case_id} did not pass",
                "failure_reason": result.summary,
                "affected_refs": [],
                "contract_refs": list(spec.contract_refs),
                "severity": "major",
                "suggested_repair_boundary": [],
            }
        )
    return confirmed


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
    case_ids = {item.case_id for item in cases if item.case_id}
    findings: list[dict[str, Any]] = []
    for index, raw in enumerate(list(plan.get("findings") or []), start=1):
        item = dict(raw or {})
        section = str(item.get("finding_section") or "implementation").strip()
        if section not in _FINDING_SECTIONS:
            raise ValueError(f"standalone finding has invalid finding_section: {section}")
        case_id = str(item.get("case_id") or "").strip()
        if case_id and case_id not in case_ids:
            raise ValueError(f"standalone finding references unknown case_id: {case_id}")
        summary = str(item.get("summary") or "").strip()
        failure_reason = str(item.get("failure_reason") or "").strip()
        if not summary or not failure_reason:
            raise ValueError("standalone finding requires summary and failure_reason")
        findings.append(
            {
                "finding_id": str(item.get("finding_id") or f"F-{index}"),
                "case_id": case_id,
                "finding_section": section,
                "summary": summary,
                "failure_reason": failure_reason,
                "affected_refs": [str(value) for value in list(item.get("affected_refs") or [])],
                "contract_refs": [str(value) for value in list(item.get("contract_refs") or [])],
                "severity": str(item.get("severity") or "major"),
                "evidence": list(item.get("evidence") or []),
            }
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
        return VerificationStatus.FAIL
    if verdict == "blocked":
        return VerificationStatus.UNKNOWN
    raise ValueError("standalone review verdict must be approved, changes_requested, or blocked")


def _defect_kind(plan: Mapping[str, Any], node: AggregateSnapshot) -> DefectKind:
    raw = str(plan.get("defect_kind") or "").strip()
    if raw:
        return DefectKind(raw)
    if str(node.payload.get("node_kind") or "") == "integration":
        return DefectKind.INTEGRATION
    return DefectKind.MODULE


def _unknown_policy(plan: Mapping[str, Any]) -> UnknownPolicy:
    raw = dict(plan.get("unknown_policy") or {})
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
