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

from pal.minion.profiles import resolve_pinned_minion_pack
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
from pal.lsp.ipc import LspManagerClient
from pal.minion.v2.artifacts import ArtifactRef
from pal.minion.v2.contracts import (
    ActionEnvelope,
    AggregateSnapshot,
    AggregateType,
    AggregateVersionConflict,
    DeferredEffectError,
    LeaseConflict,
    PermanentEffectError,
    StaleFencingToken,
    SubmissionInvariantError,
)
from pal.minion.v2.contract_builder import (
    ARCHITECT_BUILDER_CAPABILITIES,
    ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES,
    CONTRACT_SKETCH_BUILDER_CAPABILITIES,
    REQUIREMENTS_BUILDER_CAPABILITIES,
    seed_contract_builder_draft,
)
from pal.minion.v2.candidate_builder import (
    CANDIDATE_BUILDER_CAPABILITIES,
    validate_candidate_submission,
)
from pal.minion.v2.execution import (
    CandidateSnapshotService,
    UnitWorkViewBuilder,
    WorkspaceLockRegistry,
    provision_verification_worktree,
    format_workspace_process_holders,
    terminate_process_group,
    workspace_content_fingerprint,
    workspace_process_holders,
)
from pal.minion.v2.service import MinionV2WorkflowService, workflow_request_from_snapshot
from pal.minion.v2.sessions import (
    architect_session_id_for_revision,
    coder_session_id,
    node_role_generation,
    verifier_session_id,
)
from pal.minion.v2.skeleton import (
    ARCHITECTURE_REPAIR_BASELINE_ARTIFACT,
    ARCHITECTURE_SKELETON_ARTIFACT,
    ArchitectureWorkspace,
    SemanticReferenceError,
    SkeletonReviewFinding,
    SkeletonReviewResult,
    architecture_revision_path_states,
    architecture_revision_scope,
    compile_skeleton_markdown,
    requirements_semantic_view,
    review_architecture_skeleton,
)
from pal.minion.v2.skeleton_builder import (
    ARCHITECTURE_SKELETON_CAPABILITIES,
    SKELETON_REVIEW_CAPABILITIES,
    compile_architecture_review_invocation_tool_contract,
)
from pal.minion.v2.integration import (
    CandidateUnionConflict,
    CandidateUnionService,
    IntegrationOwnershipDefect,
    IntegrationService,
)
from pal.minion.v2.paths import (
    invocation_root,
    resolve_project_git_layout,
    standalone_review_root,
    verification_scratch_root,
)
from pal.minion.v2.projections import PlanRevisionProjectionStore
from pal.minion.v2.replan import (
    ARCHITECTURE_FINDING_BATCH_VIEW_ARTIFACT,
    architecture_finding_semantic_view,
    architecture_revision_finding_value,
    compile_architecture_finding_markdown,
)
from pal.minion.v2.verification import (
    DefectKind,
    UnknownPolicy,
    VerificationCaseKind,
    VerificationCaseResult,
    VerificationCaseSpec,
    VerificationService,
    VerificationStatus,
    aggregate_verification_status,
    repair_bill_semantic_view,
    semantic_finding_payload,
)
from pal.minion.v2.verification_builder import (
    STANDALONE_REVIEW_BUILDER_CAPABILITIES,
    VERIFICATION_BUILDER_CAPABILITIES,
    VERIFICATION_TOOL_CAPABILITIES,
    compile_verification_invocation_tool_contract,
    dominant_verification_defect_kind,
    effective_verification_policy,
    validate_semantic_verification_plan_shape as _validate_semantic_verification_plan_shape,
)
from pal.minion.v2.submission_drafts import (
    AUTHORING_CONTRACT_VERSION,
    SubmissionDraftStore,
    authoring_input_fingerprint,
)
from pal.minion.v2.semantic_evidence import recorded_cases
from pal.minion.v2.worker_gateway import WORKER_GATEWAY_TOKEN_ENV
from pal.minion.v2.worker_protocol import (
    WorkerAssignmentRequest,
    WorkerAssignmentState,
    stable_hash,
)
from pal.shared import MinionInvocationPack


HumanReviewPublisher = Callable[[Mapping[str, Any]], Awaitable[None]]
WorkerEventPublisher = Callable[[Mapping[str, Any]], Awaitable[None]]
BrokerRunRegistrar = Callable[[str, str, MinionInvocationPack, asyncio.subprocess.Process], None]
BrokerRunUnregistrar = Callable[[str], None]


_ARCHITECTURE_STAGE_CONFIG = {
    "architect": ("architect", "START_ARCHITECT"),
}


def _worker_submission_kind(role: str, *, skeleton_mode: bool) -> str:
    if role == "architect":
        return "architecture" if skeleton_mode else "contract"
    return {
        "architecture_reviewer": "architecture_review",
        "producer": "candidate",
        "repair": "candidate",
        "verifier": "verification",
        "scenario_verifier": "verification",
        "reviewer": "standalone_review",
        "requirements": "requirements",
        "planner": "contract",
        "research": "contract",
    }.get(role, "contract")


def _architecture_submit_idempotency_key(
    architecture_revision_id: str,
    source_version: int,
    submission_sha: str,
) -> str:
    return (
        f"architect-submit:{architecture_revision_id}:"
        f"v{int(source_version)}:{submission_sha}"
    )


SEMANTIC_EFFECT_TYPES = frozenset(
    {
        "enqueue_architecture_stage",
        "enqueue_architecture_review",
        "quiesce_architect",
        "snapshot_architecture",
        "publish_human_architecture_review",
        "materialize_plan_revision",
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
        "quiesce_node_for_triage",
        "pause_aggregate_work",
        "cancel_aggregate_work",
        "quiesce_aggregate_for_triage",
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
    max_parallel_workers: int = 5
    publish_human_review: HumanReviewPublisher | None = None
    publish_worker_event: WorkerEventPublisher | None = None
    register_broker_run: BrokerRunRegistrar | None = None
    unregister_broker_run: BrokerRunUnregistrar | None = None
    _processes: dict[str, asyncio.subprocess.Process] = field(default_factory=dict, init=False)
    _run_to_invocation: dict[str, str] = field(default_factory=dict, init=False)
    _worktree_locks: WorkspaceLockRegistry = field(default_factory=WorkspaceLockRegistry, init=False)
    _revoked_tokens: set[tuple[str, int]] = field(default_factory=set, init=False)
    _background_workers: dict[str, asyncio.Task[Mapping[str, Any]]] = field(
        default_factory=dict,
        init=False,
    )
    _assignment_ready_events: dict[str, asyncio.Event] = field(
        default_factory=dict,
        init=False,
    )
    _assignment_ids_by_effect: dict[str, str] = field(default_factory=dict, init=False)
    _stopping: bool = field(default=False, init=False)

    @property
    def repository(self):
        return self.service.repository

    @property
    def active_background_count(self) -> int:
        return sum(not task.done() for task in self._background_workers.values())

    def request_stop(self) -> None:
        self._stopping = True

    async def stop_background_workers(self, *, timeout_seconds: float = 10.0) -> None:
        self.request_stop()
        tracked = tuple(self._background_workers.items())
        if not tracked:
            return
        tasks = tuple(task for _effect_key, task in tracked)
        _done, pending = await asyncio.wait(
            tasks,
            timeout=max(0.0, float(timeout_seconds)),
        )
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        for effect_key, _task in tracked:
            self._queue_interrupted_assignment_retry(effect_key)

    async def _release_managed_lsp_workspace(self, workspace: Path) -> Mapping[str, Any]:
        try:
            return await LspManagerClient(
                self.service.runtime_root,
                request_timeout_seconds=15.0,
            ).release_workspace(workspace)
        except Exception as exc:
            # The LSP manager is optional. The strict process-holder check that
            # follows still protects the snapshot if a server remains alive.
            return {
                "status": "unavailable",
                "error": f"{exc.__class__.__name__}: {exc}",
            }

    async def execute_semantic_effect(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        effect_type = str(effect.get("effect_type") or "")
        if effect_type == "enqueue_architecture_stage":
            return await self._launch_background_worker(
                effect,
                self._run_architecture_stage,
            )
        if effect_type == "enqueue_architecture_review":
            return await self._launch_background_worker(
                effect,
                self._run_architecture_review,
            )
        if effect_type == "quiesce_architect":
            return await self._quiesce_architect(effect)
        if effect_type == "snapshot_architecture":
            return await self._snapshot_architecture(effect)
        if effect_type == "publish_human_architecture_review":
            return await self._publish_human_architecture_review(effect)
        if effect_type == "materialize_plan_revision":
            return self._materialize_plan_revision_status(effect)
        if effect_type == "request_human_clarification":
            return await self._publish_human_clarification(effect)
        if effect_type == "reconcile_architecture_revision":
            return await self._resume_aggregate(effect)
        if effect_type == "enqueue_producer":
            return self._admit_node_worker(effect, action_type="START_PRODUCING", role="producer")
        if effect_type == "spawn_producer_worker":
            return await self._launch_background_worker(
                effect,
                lambda value: self._run_producer(value, repair=False),
            )
        if effect_type == "enqueue_node_review":
            return self._admit_node_worker(effect, action_type="START_REVIEW", role="reviewer")
        if effect_type == "spawn_verifier_worker":
            if str(effect.get("aggregate_type") or "") == AggregateType.STANDALONE_REVIEW.value:
                return await self._launch_background_worker(
                    effect,
                    self._run_standalone_review,
                )
            return await self._launch_background_worker(effect, self._run_verifier)
        if effect_type == "enqueue_scenario_verifier":
            return self._admit_node_worker(
                effect,
                action_type="START_SCENARIO_VERIFICATION",
                role="scenario_verifier",
            )
        if effect_type == "spawn_scenario_verifier":
            return await self._launch_background_worker(
                effect,
                lambda value: self._run_verifier(value, scenario_mode=True),
            )
        if effect_type == "enqueue_repair":
            return self._admit_node_worker(effect, action_type="START_REPAIR", role="repair")
        if effect_type == "spawn_repair_worker":
            return await self._launch_background_worker(
                effect,
                lambda value: self._run_producer(value, repair=True),
            )
        if effect_type == "quiesce_worker":
            return await self._quiesce_node(effect)
        if effect_type == "snapshot_candidate":
            return await self._snapshot_candidate(effect)
        if effect_type in {"pause_node_worker", "cancel_node_worker"}:
            return await self._stop_node_worker(effect, cancel=effect_type == "cancel_node_worker")
        if effect_type == "quiesce_node_for_triage":
            return await self._stop_node_worker(effect, cancel=False, confirm=False)
        if effect_type in {"pause_aggregate_work", "cancel_aggregate_work"}:
            return await self._stop_aggregate_worker(effect, cancel=effect_type == "cancel_aggregate_work")
        if effect_type == "quiesce_aggregate_for_triage":
            return await self._stop_aggregate_worker(effect, cancel=False, confirm=False)
        if effect_type == "resume_aggregate_work":
            return await self._resume_aggregate(effect)
        if effect_type == "resume_node_work":
            return self._resume_node(effect)
        if effect_type == "reconcile_node_run":
            return await self._reconcile_node(effect)
        if effect_type == "reconcile_standalone_review":
            return await self._resume_aggregate(effect)
        if effect_type == "publish_final_deliverable":
            return await self._publish_final_deliverable(effect)
        if effect_type == "enqueue_standalone_review":
            return self._admit_standalone_review(effect)
        if effect_type == "publish_review_report":
            return await self._publish_standalone_report(effect)
        raise RuntimeError(f"V2 semantic effect is not implemented yet: {effect_type}")

    async def _launch_background_worker(
        self,
        effect: Mapping[str, Any],
        runner: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
    ) -> Mapping[str, Any]:
        if self._stopping:
            raise DeferredEffectError("worker supervisor is stopping")
        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "").strip()
        if not effect_key:
            raise ValueError("background worker effect requires an effect key")
        existing = self._background_workers.get(effect_key)
        if existing is not None and not existing.done():
            return {
                "provider_request_id": self._assignment_ids_by_effect.get(effect_key, effect_key),
                "status": "already_running",
            }
        if self.active_background_count >= max(1, int(self.max_parallel_workers)):
            raise DeferredEffectError("worker supervisor has no available execution slot")
        ready = self._assignment_ready_events.setdefault(effect_key, asyncio.Event())
        task = asyncio.create_task(
            self._background_worker_loop(effect, runner),
            name=f"minion-v2-assignment-{hashlib.sha256(effect_key.encode()).hexdigest()[:12]}",
        )
        self._background_workers[effect_key] = task
        task.add_done_callback(
            lambda completed, key=effect_key: self._background_worker_done(key, completed)
        )
        ready_wait = asyncio.create_task(ready.wait())
        done, _pending = await asyncio.wait(
            {task, ready_wait},
            timeout=120.0,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if ready_wait not in done:
            ready_wait.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ready_wait
        if task in done:
            return dict(task.result())
        if ready.is_set():
            return {
                "provider_request_id": self._assignment_ids_by_effect.get(effect_key, effect_key),
                "status": "assignment_started",
            }
        raise RuntimeError("worker assignment was not durably created within 120 seconds")

    async def _background_worker_loop(
        self,
        effect: Mapping[str, Any],
        runner: Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]],
    ) -> Mapping[str, Any]:
        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "")
        supervisor_failures = 0
        while True:
            if self._stopping:
                assignment_id = self._assignment_ids_by_effect.get(effect_key, "")
                if not assignment_id:
                    raise DeferredEffectError(
                        "worker supervisor stopped before creating a durable assignment"
                    )
                return {
                    "provider_request_id": assignment_id,
                    "status": "suspended",
                }
            assignment_id = self._assignment_ids_by_effect.get(effect_key, "")
            if assignment_id:
                assignment = self.repository.read_worker_assignment(assignment_id)
                if assignment is not None:
                    disposition = self._worker_assignment_disposition(effect, assignment)
                    if disposition:
                        self._release_background_business_lease(effect)
                        return {
                            "provider_request_id": assignment_id,
                            "status": disposition,
                        }
            try:
                result = await runner(effect)
                assignment_id = self._assignment_ids_by_effect.get(effect_key, "")
                if assignment_id:
                    assignment = self.repository.read_worker_assignment(assignment_id)
                    if (
                        assignment is not None
                        and assignment["state"]
                        == WorkerAssignmentState.RESULT_RECORDED.value
                    ):
                        raise SubmissionInvariantError(
                            "worker business action returned without atomically settling "
                            "its durable submission"
                        )
                return result
            except asyncio.CancelledError:
                raise
            except DeferredEffectError:
                assignment_id = self._assignment_ids_by_effect.get(effect_key, "")
                if not assignment_id:
                    raise
                self._release_background_business_lease(effect)
                return {
                    "provider_request_id": assignment_id,
                    "status": "suspended",
                }
            except Exception as exc:
                supervisor_failures += 1
                assignment_id = self._assignment_ids_by_effect.get(effect_key, "")
                if not assignment_id:
                    raise
                assignment = self.repository.read_worker_assignment(assignment_id)
                if assignment is None:
                    raise
                disposition = self._worker_assignment_disposition(effect, assignment)
                if disposition:
                    self._release_background_business_lease(effect)
                    return {
                        "provider_request_id": assignment_id,
                        "status": disposition,
                    }
                if assignment["state"] == WorkerAssignmentState.SETTLED.value:
                    return {
                        "provider_request_id": assignment_id,
                        "status": "settled",
                    }
                permanent = isinstance(exc, PermanentEffectError)
                if assignment["state"] in {
                    WorkerAssignmentState.CLAIMED.value,
                    WorkerAssignmentState.RUNNING.value,
                } and not permanent:
                    assignment = self._queue_active_assignment_retry(
                        assignment,
                        error_kind="worker_supervisor_failure",
                        error_text=f"{exc.__class__.__name__}: {exc}",
                    )
                attempts = self.repository.list_worker_attempts(assignment_id)
                if self._stopping:
                    self._release_background_business_lease(effect)
                    return {
                        "provider_request_id": assignment_id,
                        "status": "suspended",
                    }
                if (
                    assignment["state"] in {
                        WorkerAssignmentState.QUEUED.value,
                        WorkerAssignmentState.RETRY_QUEUED.value,
                        WorkerAssignmentState.RESULT_RECORDED.value,
                    }
                    and max(len(attempts), supervisor_failures) < 3
                    and not permanent
                ):
                    # A recorded submission is already the durable LLM result.
                    # Replay the role wrapper so it can reconcile the same
                    # receipt with its business Action; _run_profile will not
                    # invoke the model again for this assignment state.
                    self._release_background_business_lease(effect)
                    await asyncio.sleep(5.0)
                    continue
                self._release_background_business_lease(effect)
                return self._settle_background_worker_failure(
                    effect,
                    assignment,
                    exc,
                    exhausted=not permanent,
                )

    def _queue_active_assignment_retry(
        self,
        assignment: Mapping[str, Any],
        *,
        error_kind: str,
        error_text: str,
    ) -> dict[str, Any]:
        attempt_id_value = str(assignment.get("active_attempt_id") or "")
        if not attempt_id_value:
            return dict(assignment)
        attempt = self.repository.read_worker_attempt(attempt_id_value)
        if attempt is None:
            return dict(assignment)
        lease_resource = str(attempt.get("lease_resource_key") or "")
        fencing_token = int(attempt.get("fencing_token") or 0)
        updated = self.repository.queue_worker_attempt_retry(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=attempt_id_value,
            error_kind=error_kind,
            error_text=error_text,
        )
        if lease_resource and fencing_token:
            with contextlib.suppress(Exception):
                self.repository.release_lease(
                    lease_resource,
                    attempt_id_value,
                    fencing_token,
                )
        return updated

    def _queue_interrupted_assignment_retry(self, effect_key: str) -> None:
        assignment_id = self._assignment_ids_by_effect.get(effect_key, "")
        if not assignment_id:
            return
        assignment = self.repository.read_worker_assignment(assignment_id)
        if assignment is None or assignment["state"] not in {
            WorkerAssignmentState.CLAIMED.value,
            WorkerAssignmentState.RUNNING.value,
        }:
            return
        self._queue_active_assignment_retry(
            assignment,
            error_kind="manager_shutdown",
            error_text="manager stopped before the worker assignment settled",
        )

    def _background_worker_done(
        self,
        effect_key: str,
        task: asyncio.Task[Mapping[str, Any]],
    ) -> None:
        if not task.cancelled():
            # Retrieve the exception so an early launch failure cannot become
            # an unobserved asyncio task warning after its Outbox waiter exits.
            task.exception()
        if self._background_workers.get(effect_key) is task:
            self._background_workers.pop(effect_key, None)
        self._assignment_ready_events.pop(effect_key, None)

    def _signal_assignment_ready(
        self,
        effect: Mapping[str, Any],
        assignment_id: str,
    ) -> None:
        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "")
        if not effect_key:
            return
        self._assignment_ids_by_effect[effect_key] = str(assignment_id)
        event = self._assignment_ready_events.get(effect_key)
        if event is not None:
            event.set()

    def _worker_assignment_disposition(
        self,
        effect: Mapping[str, Any],
        assignment: Mapping[str, Any],
    ) -> str:
        assignment_state = str(assignment.get("state") or "")
        if assignment_state == WorkerAssignmentState.CANCELLED.value:
            return "cancelled"
        if assignment_state == WorkerAssignmentState.SETTLED.value:
            return "settled"
        expected_states = {
            "enqueue_architecture_stage": {"ARCHITECT_RUNNING"},
            "enqueue_architecture_review": {"REVIEWING"},
            "spawn_producer_worker": {"PRODUCING"},
            "spawn_repair_worker": {"REPAIRING"},
            "spawn_verifier_worker": {"REVIEWING"},
            "spawn_scenario_verifier": {"VERIFYING"},
        }.get(str(effect.get("effect_type") or ""))
        if not expected_states:
            return ""
        try:
            aggregate_type = AggregateType(str(assignment.get("aggregate_type") or ""))
            snapshot = self.repository.read_snapshot(
                aggregate_type,
                str(assignment.get("aggregate_id") or ""),
            )
        except (KeyError, ValueError):
            return "superseded"
        if snapshot is None:
            return "superseded"
        if snapshot.state in expected_states:
            return ""
        if snapshot.state in {"PAUSE_REQUESTED", "PAUSED"}:
            return "suspended"
        if snapshot.state in {"CANCEL_REQUESTED", "CANCELLED", "STALE"}:
            return "cancelled"
        return "superseded"

    def _worker_submission_settlement(
        self,
        effect: Mapping[str, Any],
        *,
        required: bool = True,
    ) -> dict[str, str]:
        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "")
        assignment_id = self._assignment_ids_by_effect.get(effect_key, "")
        if not assignment_id:
            return {}
        assignment = self.repository.read_worker_assignment(assignment_id)
        if assignment is None:
            raise SubmissionInvariantError("worker assignment disappeared before settlement")
        if assignment["state"] not in {
            WorkerAssignmentState.RESULT_RECORDED.value,
            WorkerAssignmentState.SETTLED.value,
        }:
            if not required:
                return {}
            raise SubmissionInvariantError(
                "worker business action requires a durable submission receipt"
            )
        payload_hash = str(assignment.get("submission_payload_hash") or "")
        if not payload_hash:
            raise SubmissionInvariantError(
                "worker assignment submission receipt has no payload hash"
            )
        return {
            "worker_assignment_id": assignment_id,
            "worker_submission_payload_hash": payload_hash,
        }

    def _release_background_business_lease(self, effect: Mapping[str, Any]) -> None:
        try:
            snapshot = self._effect_snapshot(effect)
            resource = str(snapshot.payload.get("lease_resource_key") or "")
            owner = str(snapshot.payload.get("active_worker_id") or "")
            token = int(snapshot.payload.get("fencing_token") or 0)
            if resource and owner and token:
                self.repository.release_lease(resource, owner, token)
        except Exception:
            return

    def _settle_background_worker_failure(
        self,
        effect: Mapping[str, Any],
        assignment: Mapping[str, Any],
        error: Exception,
        *,
        exhausted: bool,
    ) -> Mapping[str, Any]:
        assignment_id = str(assignment["assignment_id"])
        attempts = self.repository.list_worker_attempts(assignment_id)
        error_text = f"{error.__class__.__name__}: {error}"
        failure_payload = {
            "kind": "worker_assignment_failed",
            "role": str(assignment.get("role") or ""),
            "attempt_count": len(attempts),
            "exhausted": bool(exhausted),
            "error_kind": (
                "attempt_budget_exhausted"
                if exhausted
                else "permanent_worker_failure"
            ),
            "error": error_text,
            "effect_type": str(effect.get("effect_type") or ""),
        }
        failure_ref = self.service.artifacts.put_json(
            failure_payload,
            artifact_type="WorkerAssignmentFailureArtifact",
        )
        current_assignment = self.repository.read_worker_assignment(assignment_id)
        if current_assignment is None:
            raise SubmissionInvariantError("worker assignment disappeared before failure settlement")
        if current_assignment["state"] not in {
            WorkerAssignmentState.RESULT_RECORDED.value,
            WorkerAssignmentState.SETTLED.value,
        }:
            if not attempts:
                raise SubmissionInvariantError(
                    "worker assignment failed before an attempt was durably claimed"
                )
            self.repository.record_worker_failure_result(
                assignment_id=assignment_id,
                attempt_id_value=str(attempts[-1]["attempt_id"]),
                error_kind=str(failure_payload["error_kind"]),
                error_text=error_text,
                failure_artifact_ref=failure_ref.to_dict(),
                payload_hash=stable_hash(failure_payload),
                settlement_action={
                    "action_type": "WORKER_FAILED",
                    "aggregate_type": str(current_assignment["aggregate_type"]),
                    "aggregate_id": str(current_assignment["aggregate_id"]),
                },
            )
            current_assignment = self.repository.read_worker_assignment(assignment_id)
        if current_assignment is None:
            raise SubmissionInvariantError("worker failure receipt was not durable")
        if current_assignment["state"] == WorkerAssignmentState.SETTLED.value:
            return {"provider_request_id": assignment_id, "status": "settled"}

        for _attempt in range(3):
            snapshot = self._effect_snapshot(effect)
            legal = self.repository.engine.legal_actions(
                snapshot.aggregate_type,
                snapshot.state,
            )
            if "WORKER_FAILED" not in legal:
                if snapshot.state == "TRIAGE_REQUIRED":
                    self.repository.settle_worker_assignment(
                        assignment_id=assignment_id,
                        submission_payload_hash=str(
                            current_assignment["submission_payload_hash"]
                        ),
                    )
                    return {
                        "provider_request_id": assignment_id,
                        "status": "triage_required",
                    }
                self.repository.cancel_worker_assignments(
                    workflow_id=str(current_assignment["workflow_id"]),
                    aggregate_type=str(current_assignment["aggregate_type"]),
                    aggregate_id=str(current_assignment["aggregate_id"]),
                    reason=f"worker failure superseded by parent state {snapshot.state}",
                )
                return {
                    "provider_request_id": assignment_id,
                    "status": "superseded",
                }
            try:
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="WORKER_FAILED",
                        workflow_id=snapshot.workflow_id,
                        aggregate_type=snapshot.aggregate_type,
                        aggregate_id=snapshot.aggregate_id,
                        actor="minion-v2-worker-supervisor",
                        expected_version=snapshot.version,
                        idempotency_key=f"worker-failed:{assignment_id}",
                        payload={
                            "failure_artifact_ref": failure_ref.to_dict(),
                            "blocker": {
                                "kind": "worker_failure",
                                "summary": error_text,
                                "role": str(current_assignment.get("role") or ""),
                                "attempt_count": len(attempts),
                            },
                        },
                    ),
                    worker_assignment_id=assignment_id,
                    worker_submission_payload_hash=str(
                        current_assignment["submission_payload_hash"]
                    ),
                )
                return {
                    "provider_request_id": assignment_id,
                    "status": "triage_required",
                }
            except AggregateVersionConflict:
                continue
        raise DeferredEffectError("worker failure receipt settlement lost repeated CAS races")

    async def recover_background_assignments(self) -> int:
        if self._stopping:
            return 0
        available = max(
            0,
            max(1, int(self.max_parallel_workers)) - self.active_background_count,
        )
        if available == 0:
            return 0
        recoverable = sorted(
            self.repository.list_worker_assignments(
            states=(
                WorkerAssignmentState.QUEUED.value,
                WorkerAssignmentState.CLAIMED.value,
                WorkerAssignmentState.RUNNING.value,
                WorkerAssignmentState.RETRY_QUEUED.value,
                WorkerAssignmentState.RESULT_RECORDED.value,
            )
            ),
            key=lambda item: (
                {
                    WorkerAssignmentState.RESULT_RECORDED.value: 0,
                    WorkerAssignmentState.RUNNING.value: 1,
                    WorkerAssignmentState.CLAIMED.value: 1,
                    WorkerAssignmentState.RETRY_QUEUED.value: 2,
                    WorkerAssignmentState.QUEUED.value: 3,
                }.get(str(item.get("state") or ""), 9),
                str(item.get("created_at") or ""),
                str(item.get("assignment_id") or ""),
            ),
        )
        started = 0
        for assignment in recoverable:
            if started >= available:
                break
            effect = dict(assignment.get("execution_spec") or {})
            effect_key = str(effect.get("effect_key") or assignment["assignment_key"])
            effect["effect_key"] = effect_key
            effect.setdefault("effect_id", f"assignment:{assignment['assignment_id']}")
            runner = self._runner_for_recovered_effect(effect)
            if runner is None:
                continue
            disposition = self._worker_assignment_disposition(effect, assignment)
            if disposition:
                self.repository.cancel_worker_assignments(
                    workflow_id=str(assignment["workflow_id"]),
                    aggregate_type=str(assignment["aggregate_type"]),
                    aggregate_id=str(assignment["aggregate_id"]),
                    reason=f"assignment recovery suppressed: {disposition}",
                )
                continue
            self._assignment_ids_by_effect[effect_key] = str(assignment["assignment_id"])
            ready = self._assignment_ready_events.setdefault(effect_key, asyncio.Event())
            ready.set()
            if effect_key in self._background_workers and not self._background_workers[effect_key].done():
                continue
            task = asyncio.create_task(
                self._background_worker_loop(effect, runner),
                name=f"minion-v2-recovered-{str(assignment['assignment_id'])[-12:]}",
            )
            self._background_workers[effect_key] = task
            task.add_done_callback(
                lambda completed, key=effect_key: self._background_worker_done(key, completed)
            )
            started += 1
        return started

    def _runner_for_recovered_effect(
        self,
        effect: Mapping[str, Any],
    ) -> Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None:
        effect_type = str(effect.get("effect_type") or "")
        if effect_type == "enqueue_architecture_stage":
            return self._run_architecture_stage
        if effect_type == "enqueue_architecture_review":
            return self._run_architecture_review
        if effect_type == "spawn_producer_worker":
            return lambda value: self._run_producer(value, repair=False)
        if effect_type == "spawn_repair_worker":
            return lambda value: self._run_producer(value, repair=True)
        if effect_type == "spawn_scenario_verifier":
            return lambda value: self._run_verifier(value, scenario_mode=True)
        if effect_type == "spawn_verifier_worker":
            if str(effect.get("aggregate_type") or "") == AggregateType.STANDALONE_REVIEW.value:
                return self._run_standalone_review
            return self._run_verifier
        return None

    def _admit_node_worker(
        self,
        effect: Mapping[str, Any],
        *,
        action_type: str,
        role: str,
    ) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        epoch_id = str(node.payload.get("epoch_id") or "")
        epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
        if epoch is not None and epoch.state in {
            "REPLAN_COLLECTING",
            "REPLAN_REQUIRED",
            "SUPERSEDED",
        }:
            legal = self.repository.engine.legal_actions(AggregateType.DAG_NODE_RUN, node.state)
            finding_ref = dict(epoch.payload.get("replan_finding_batch_ref") or {})
            if not finding_ref:
                pending = list(epoch.payload.get("pending_replan_findings") or [])
                if pending:
                    finding_ref = dict(dict(pending[0] or {}).get("finding_artifact_ref") or {})
            action = "MARK_STALE" if "MARK_STALE" in legal else "REQUEST_STALE" if "REQUEST_STALE" in legal else ""
            if action and finding_ref:
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type=action,
                        workflow_id=node.workflow_id,
                        aggregate_type=AggregateType.DAG_NODE_RUN,
                        aggregate_id=node.aggregate_id,
                        actor="minion-v2-replan",
                        expected_version=node.version,
                        idempotency_key=f"replan-suppress-admission:{node.aggregate_id}:{node.version}",
                        payload={"stale_reason_ref": finding_ref},
                    )
                )
            return {"status": "suppressed_by_replan"}
        target_state = {
            "START_PRODUCING": "PRODUCING",
            "START_REVIEW": "REVIEWING",
            "START_REPAIR": "REPAIRING",
            "START_SCENARIO_VERIFICATION": "VERIFYING",
        }[action_type]
        if node.state == target_state and node.payload.get("active_worker_id"):
            return {"provider_request_id": str(node.payload.get("active_worker_id"))}
        cycle = int(node.payload.get("candidate_cycle") or 0) + (1 if role in {"producer", "repair"} else 0)
        generation = node_role_generation(node.payload)
        invocation_id = (
            coder_session_id(node.aggregate_id, generation)
            if role in {"producer", "repair"}
            else verifier_session_id(node.aggregate_id, generation)
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
            if await self._reuse_or_retire_effect_lease(
                resource_key=lease_resource,
                owner_id=invocation_id,
                fencing_token=fencing_token,
                worker_label=f"node worker {node.aggregate_id}",
            ):
                return node

        writer_role = role in {"producer", "repair"}
        generation = node_role_generation(node.payload)
        if writer_role:
            invocation_id = coder_session_id(node.aggregate_id, generation)
            lease_resource = f"node:{node.aggregate_id}:writer"
        else:
            invocation_id = invocation_id or verifier_session_id(
                node.aggregate_id,
                generation,
            )
            lease_resource = f"node:{node.aggregate_id}:review"

        previous = self.repository.read_lease(lease_resource)
        if previous is not None and str(previous.get("owner_id") or "") and _lease_is_live(previous):
            raise LeaseConflict(f"node effect lease is active under {previous.get('owner_id')}")
        process_group = int(dict((previous or {}).get("metadata") or {}).get("process_group_id") or 0)
        if process_group and not await terminate_process_group(process_group, timeout_seconds=5.0):
            raise RuntimeError("expired node worker process group could not be reaped before rebind")
        workspace = Path(str(node.payload.get("workspace_path") or ""))
        if writer_role:
            await self._release_managed_lsp_workspace(workspace)
            _raise_if_workspace_held(workspace, "expired node worker still holds its worktree")

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
            try:
                _validate_skeleton_coder_report(
                    report,
                    expected_module=str(node.payload.get("module_name") or node.payload.get("unit_id") or ""),
                    work_view=self.service.artifacts.read_json(view_ref),
                )
                _reject_manager_identity_fields(report, owner="Coder output")
            except Exception as exc:
                raise SubmissionInvariantError(
                    f"accepted candidate_submit failed manager defense-in-depth validation: {exc}"
                ) from exc
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
                ),
                **self._worker_submission_settlement(effect),
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
                    "unit_work_view_ref": view_ref.to_dict(),
                },
            ),
            **self._worker_submission_settlement(effect),
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
        await self._release_managed_lsp_workspace(workspace)
        _raise_if_workspace_held(workspace, "a live process still holds the candidate workspace")
        self._worktree_locks.release(node.aggregate_id)
        lock_path = self._worktree_locks.acquire(node.aggregate_id, workspace)
        try:
            _raise_if_workspace_held(
                workspace,
                "a process reached the candidate workspace during quiescing",
            )
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
            review_scratch = verification_scratch_root(self.service.runtime_root) / _safe_component(
                node.aggregate_id
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
        _seed_durable_verification_scratch(
            invocation_root(self.service.runtime_root) / invocation_id / "attempts",
            review_scratch,
        )
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
        verification_policy = effective_verification_policy(
            work_view=self.service.artifacts.read_json(view_ref),
            verification_policy=self._workflow_policy(node.workflow_id, "verification"),
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
                "Generate and run adversarial verification for the bound real usage scenario. Prove only the Requirements claimed by this exact module combination, entrypoint, and environment. Execute each case with the dedicated verification tools; the Manager durably records evidence and owns the verdict."
                if scenario_mode
                else "Generate and run adversarial verification for the bound candidate. Historical RepairBills come first. Execute each case with the dedicated verification tools; the Manager durably records evidence and owns the verdict."
            ),
            reference_refs={
                "module_work_view": view_ref,
                "candidate_diff": candidate_view_ref,
            },
            workspace_override={
                "kind": "existing_repo",
                "repo_path": str(review_workspace),
                "project_name": str(node.payload.get("unit_id") or "unit"),
                "review_scratch_dir": str(review_scratch),
            },
            prepare_workspace=False,
        )
        plan = _primary_json_output(terminal)
        try:
            _validate_semantic_verification_plan_shape(plan, standalone=False)
            _reject_manager_identity_fields(
                {key: value for key, value in plan.items() if key not in {"recorded_results", "internal_context"}},
                owner="Verifier semantic output",
            )
            case_specs = _verification_case_specs(plan.get("cases"))
            findings = _verification_findings(plan, case_specs)
            _validate_verifier_requirement_refs(
                work_view=self.service.artifacts.read_json(view_ref),
                cases=case_specs,
                findings=findings,
            )
            _validate_verification_policy(plan, case_specs, verification_policy, node)
        except Exception as exc:
            raise SubmissionInvariantError(
                f"accepted verification_submit failed manager defense-in-depth validation: {exc}"
            ) from exc
        case_results = _recorded_verification_case_results(
            plan,
            cases=case_specs,
            artifacts=self.service.artifacts,
            runtime_root=self.service.runtime_root,
            workflow_id=node.workflow_id,
            invocation_id=invocation_id,
            lease_resource_key=lease_resource,
            fencing_token=fencing_token,
            role="verifier",
            draft_kind="verification",
        )
        findings = _confirmed_verification_findings(findings, case_specs, case_results)
        routing_status = aggregate_verification_status(item.status for item in case_results)
        routing_findings = _routable_verification_findings(
            findings,
            case_results,
            status=routing_status,
        )
        defect_kind = _defect_kind(plan, node, findings=routing_findings)
        routing_case_ids = {
            str(item.get("case_id") or "") for item in routing_findings
        }
        findings = [
            {
                **item,
                "defect_kind": str(item.get("defect_kind") or defect_kind.value),
                "routing_disposition": (
                    "dominant"
                    if str(item.get("defect_kind") or defect_kind.value) == defect_kind.value
                    and str(item.get("case_id") or "") in routing_case_ids
                    else "deferred"
                ),
            }
            for item in findings
        ]
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
        dependency_node_id, module_node_id = _resolve_verification_defect_targets(
            self.repository,
            node,
            plan=plan,
            status=status,
            defect_kind=defect_kind,
            scenario_mode=scenario_mode,
        )
        if status in {VerificationStatus.FAIL, VerificationStatus.UNKNOWN}:
            blocking_results = {
                item.case_id: item
                for item in case_results
                if item.status == status
            }
            dominant_findings = [
                item
                for item in findings
                if str(item.get("routing_disposition") or "") == "dominant"
                and str(item.get("case_id") or "") in blocking_results
            ]
            if not dominant_findings:
                first_failure = next(iter(blocking_results.values()))
                dominant_findings = [_finding_for_case(findings, first_failure.case_id)]
            dominant_case_ids = {
                str(item.get("case_id") or "") for item in dominant_findings
            }
            dominant_results = [
                item for item in case_results if item.case_id in dominant_case_ids
            ]
            first_failure = dominant_results[0]
            finding = dominant_findings[0]
            result_specs = {item.case_id: item for item in case_specs}
            case_ref = self.service.artifacts.put_json(
                {"cases": [item.to_dict() for item in dominant_results]},
                artifact_type="VerificationReproducerSetArtifact",
                child_refs=tuple(
                    (str(ref["sha256"]), relation)
                    for item in dominant_results
                    for relation, ref in (
                        ("stdout", item.stdout_ref),
                        ("stderr", item.stderr_ref),
                    )
                    if ref.get("sha256")
                ),
            )
            severity_order = {"minor": 0, "major": 1, "blocker": 2}
            severity = max(
                (str(item.get("severity") or "major") for item in dominant_findings),
                key=lambda item: severity_order.get(item, 1),
            )
            repair_boundary = sorted(
                {
                    str(path)
                    for item in dominant_findings
                    for path in list(item.get("suggested_repair_boundary") or [])
                    if str(path).strip()
                }
            )
            repair_ref, fingerprint = verification.publish_repair_bill(
                node=current,
                candidate_digest=candidate_digest,
                verification_ref=report_ref,
                defect_kind=defect_kind,
                severity=severity,
                minimal_reproducer_ref=case_ref.to_dict(),
                test_artifact_ref=test_workspace_ref.to_dict(),
                expected={
                    "cases": [
                        {
                            "name": item.case_name,
                            "exit_codes": list(result_specs[item.case_id].expected_exit_codes),
                        }
                        for item in dominant_results
                    ]
                },
                actual={
                    "cases": [
                        {
                            "name": item.case_name,
                            "exit_code": item.exit_code,
                            "status": item.status.value,
                        }
                        for item in dominant_results
                    ]
                },
                suggested_repair_boundary=repair_boundary,
                finding_section=str(finding.get("finding_section") or "implementation"),
                finding_summary=str(finding.get("summary") or ""),
                failure_reason=str(finding.get("failure_reason") or first_failure.summary),
                case_name=str(finding.get("case_name") or first_failure.case_name),
                requirements=list(finding.get("requirements") or first_failure.requirements),
                locations=list(finding.get("locations") or first_failure.locations),
                invariants=list(finding.get("invariants") or first_failure.invariants),
                findings=dominant_findings,
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
            **self._worker_submission_settlement(effect),
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

    async def _stop_node_worker(
        self,
        effect: Mapping[str, Any],
        *,
        cancel: bool,
        confirm: bool = True,
    ) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        invocation_id = str(node.payload.get("active_worker_id") or "")
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        lease = self.repository.read_lease(lease_resource) if lease_resource else None
        process = self._processes.get(invocation_id)
        process_group = int(dict((lease or {}).get("metadata") or {}).get("process_group_id") or (process.pid if process else 0))
        if process_group and not await terminate_process_group(process_group, timeout_seconds=5.0):
            raise RuntimeError("node worker process group did not stop")
        worktree_text = str(node.payload.get("workspace_path") or "")
        if worktree_text:
            workspace = Path(worktree_text)
            await self._release_managed_lsp_workspace(workspace)
            _raise_if_workspace_held(workspace, "node worker still holds its worktree")
        current = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            node.aggregate_id,
        )
        cancel_target = str(current.payload.get("cancel_target") or "CANCELLED")
        terminal_cancel = bool(cancel and cancel_target == "CANCELLED")
        self.repository.cancel_worker_assignments(
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
        if terminal_cancel:
            self.repository.complete_worker_session(
                coder_session_id(
                    node.aggregate_id,
                    node_role_generation(node.payload),
                ),
                status="cancelled",
            )
            self.repository.complete_worker_session(
                verifier_session_id(
                    node.aggregate_id,
                    node_role_generation(node.payload),
                ),
                status="cancelled",
            )
        return {}

    async def _stop_aggregate_worker(
        self,
        effect: Mapping[str, Any],
        *,
        cancel: bool,
        confirm: bool = True,
    ) -> Mapping[str, Any]:
        snapshot = self._effect_snapshot(effect)
        invocation_id = str(snapshot.payload.get("active_worker_id") or "")
        lease_resource = str(snapshot.payload.get("lease_resource_key") or "")
        lease = self.repository.read_lease(lease_resource) if lease_resource else None
        process = self._processes.get(invocation_id)
        process_group = int(dict((lease or {}).get("metadata") or {}).get("process_group_id") or (process.pid if process else 0))
        if process_group and not await terminate_process_group(process_group, timeout_seconds=5.0):
            raise RuntimeError("aggregate worker process group did not stop")
        architecture_workspace_text = str(snapshot.payload.get("architecture_workspace_path") or "")
        if architecture_workspace_text:
            architecture_workspace = Path(architecture_workspace_text)
            await self._release_managed_lsp_workspace(architecture_workspace)
            _raise_if_workspace_held(
                architecture_workspace,
                "aggregate worker still holds the architecture worktree",
            )
        self.repository.cancel_worker_assignments(
            workflow_id=snapshot.workflow_id,
            aggregate_type=snapshot.aggregate_type,
            aggregate_id=snapshot.aggregate_id,
            reason="aggregate cancelled" if cancel else "aggregate paused",
        )
        if confirm:
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
                architect_session_id_for_revision(
                    snapshot.workflow_id,
                    snapshot.aggregate_id,
                    snapshot.payload,
                ),
                status="cancelled",
            )
        return {}

    async def _resume_aggregate(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        snapshot = self._effect_snapshot(effect)
        if snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
            if snapshot.state == "PAUSE_REQUESTED":
                return await self._stop_aggregate_worker(effect, cancel=False)
            if snapshot.state == "CANCEL_REQUESTED":
                return await self._stop_aggregate_worker(effect, cancel=True)
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
            if snapshot.state == "HUMAN_REVIEW":
                return await self._publish_human_architecture_review(effect)
            if snapshot.state == "CLARIFICATION_PENDING":
                return await self._publish_human_clarification(effect)
        if snapshot.aggregate_type == AggregateType.STANDALONE_REVIEW:
            if snapshot.state == "RECEIVED":
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="QUEUE_REVIEW",
                        workflow_id=snapshot.workflow_id,
                        aggregate_type=AggregateType.STANDALONE_REVIEW,
                        aggregate_id=snapshot.aggregate_id,
                        actor="minion-v2-recovery",
                        expected_version=snapshot.version,
                        idempotency_key=f"effect:{effect['effect_key']}:queue-review",
                    )
                )
                return {}
            if snapshot.state == "PAUSE_REQUESTED":
                return await self._stop_aggregate_worker(effect, cancel=False)
            if snapshot.state == "CANCEL_REQUESTED":
                return await self._stop_aggregate_worker(effect, cancel=True)
            if snapshot.state == "REVIEW_QUEUED":
                return self._admit_standalone_review(effect)
            if snapshot.state == "REPORT_READY":
                return await self._publish_standalone_report(effect)
        return {}

    async def _reconcile_node(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        if node.state == "PAUSE_REQUESTED":
            return await self._stop_node_worker(effect, cancel=False)
        if node.state == "CANCEL_REQUESTED":
            return await self._stop_node_worker(effect, cancel=True)
        return self._resume_node(effect)

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
            workflow_branch = str(integration.payload.get("workflow_branch") or "").strip()
            if not workflow_branch:
                raise ValueError("integration publisher requires the bound workflow branch")
            deliverable_ref = IntegrationService(self.service.artifacts).publish_final_deliverable(
                repository=Path(str(integration.payload.get("workspace_path") or "")),
                integration_candidate_digest=str(integration.payload.get("candidate_digest") or ""),
                branch_name=workflow_branch,
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
        workflow_branch = str(ordered[0].payload.get("workflow_branch") or "").strip()
        workflow_key = str(ordered[0].payload.get("workflow_key") or "").strip()
        if not workflow_branch or not workflow_key:
            raise ValueError("candidate union requires the bound workflow branch and worktree key")
        publish_worktree = common_git_dir.parent / "worktrees" / workflow_key / "publish"
        branch = workflow_branch
        if not publish_worktree.exists():
            publish_worktree.parent.mkdir(parents=True, exist_ok=True)
            completed = subprocess.run(
                [
                    "git",
                    f"--git-dir={common_git_dir}",
                    "worktree",
                    "add",
                    str(publish_worktree),
                    branch,
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
                    action_type="REGISTER_REPLAN_FINDING",
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch.aggregate_id,
                    actor="minion-v2-manager",
                    expected_version=current.version,
                    idempotency_key=f"union-conflict:{finding_ref.sha256}",
                    payload={
                        "finding_artifact_ref": finding_ref.to_dict(),
                        "finding_fingerprint": finding_ref.sha256,
                        "source_node": "final_candidate_union",
                    },
                )
            )
            return {"result_artifact_ref": finding_ref.to_dict()}
        deliverable_ref = service.publish(
            repository=publish_worktree,
            union_ref=union_ref,
            commit_sha=commit_sha,
            branch_name=workflow_branch,
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
        skeleton_review_workspace = None
        if skeleton_review:
            skeleton = self.service.artifacts.read_json(request_ref)
            requirements_ref = _ref_from_mapping(skeleton.get("requirements_ref"))
            requirements = requirements_semantic_view(
                self.service.artifacts.read_json(requirements_ref)
            )
            skeleton_review_workspace = self.service.skeleton.provision_review_worktree(
                artifact=skeleton,
                review_name=f"standalone-{review.aggregate_id}",
            )
            review_repo = skeleton_review_workspace.worktree
            review_scratch = skeleton_review_workspace.root / "review-scratch"
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
        _seed_durable_verification_scratch(
            invocation_root(self.service.runtime_root) / invocation_id / "attempts",
            review_scratch,
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
            reference_refs=reviewer_inputs,
            workspace_override={
                "kind": "existing_repo",
                "repo_path": str(review_repo),
                "project_name": "standalone-review",
                "review_scratch_dir": str(review_scratch),
            },
            prepare_workspace=False,
        )
        plan = _primary_json_output(terminal)
        try:
            _validate_semantic_verification_plan_shape(plan, standalone=True)
            _reject_manager_identity_fields(
                {key: value for key, value in plan.items() if key not in {"recorded_results", "internal_context"}},
                owner="Standalone Reviewer semantic output",
            )
            case_specs = _verification_case_specs(plan.get("cases"))
            findings = _standalone_review_findings(plan, case_specs)
            if skeleton_review:
                _validate_verifier_requirement_refs(
                    work_view=self.service.artifacts.read_json(review_view_ref),
                    cases=case_specs,
                    findings=findings,
                )
        except Exception as exc:
            raise SubmissionInvariantError(
                f"accepted review_submit failed manager defense-in-depth validation: {exc}"
            ) from exc
        results = _recorded_verification_case_results(
            plan,
            cases=case_specs,
            artifacts=self.service.artifacts,
            runtime_root=self.service.runtime_root,
            workflow_id=review.workflow_id,
            invocation_id=invocation_id,
            lease_resource_key=lease_resource,
            fencing_token=fencing_token,
            role="reviewer",
            draft_kind="standalone_review",
        )
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
            ),
            **self._worker_submission_settlement(effect),
        )
        if skeleton_review_workspace is not None:
            skeleton_review_workspace.cleanup()
        self.repository.release_lease(lease_resource, invocation_id, fencing_token)
        return {"result_artifact_ref": report_ref.to_dict()}

    async def _ensure_standalone_review_lease(self, review: AggregateSnapshot) -> AggregateSnapshot:
        invocation_id = str(review.payload.get("active_worker_id") or "")
        lease_resource = str(review.payload.get("lease_resource_key") or f"standalone-review:{review.aggregate_id}")
        fencing_token = int(review.payload.get("fencing_token") or 0)
        if invocation_id and fencing_token:
            if await self._reuse_or_retire_effect_lease(
                resource_key=lease_resource,
                owner_id=invocation_id,
                fencing_token=fencing_token,
                worker_label=f"standalone reviewer {review.aggregate_id}",
            ):
                return review
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
        implementation_modules = {
            name: module
            for name, module in modules.items()
            if str(dict(module or {}).get("module_kind") or "") == "implementation"
        }
        if len(implementation_modules) != 1:
            raise ValueError("review_and_repair requires exactly one bounded module")
        module_name = next(iter(implementation_modules))
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
        invocation_id = architect_session_id_for_revision(
            revision.workflow_id,
            revision.aggregate_id,
            revision.payload,
        )
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
                ),
                **self._worker_submission_settlement(effect),
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
        invocation_id = architect_session_id_for_revision(
            revision.workflow_id,
            revision.aggregate_id,
            revision.payload,
        )
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
        finding_value = architecture_revision_finding_value(revision.payload)
        repair_baseline_value = revision.payload.get("architecture_repair_baseline_ref")
        repair_baseline: Mapping[str, Any] | None = None
        if repair_baseline_value:
            repair_baseline = self.service.artifacts.read_json(
                _ref_from_mapping(repair_baseline_value)
            )
        scope_base_submission: Mapping[str, Any] | None = None
        scope_base_path_states: Mapping[str, str] | None = None
        if repair_baseline is not None:
            scope_base_submission = dict(repair_baseline.get("submission") or {})
            scope_base_path_states = {
                str(path): str(value)
                for path, value in dict(repair_baseline.get("path_states") or {}).items()
            }
        elif base_artifact is not None:
            scope_base_submission = dict(base_artifact.get("submission") or {})
        revision_scope: Mapping[str, Any] | None = None
        if scope_base_submission is not None and finding_value:
            revision_scope = architecture_revision_scope(
                scope_base_submission,
                self.service.artifacts.read_json(_ref_from_mapping(finding_value)),
            )
        architecture_workspace = self.service.skeleton.provision_architecture_workspace(
            workflow_id=revision.workflow_id,
            workflow_name=str(request.get("workflow_name") or request.get("goal") or revision.workflow_id),
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
        handed_off_to_quiescer = False
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
                        "architecture_repository_layout": {
                            "project_name": architecture_workspace.project_name,
                            "project_key": architecture_workspace.project_key,
                            "workflow_name": architecture_workspace.workflow_name,
                            "workflow_key": architecture_workspace.workflow_key,
                            "workflow_branch": architecture_workspace.workflow_branch,
                        },
                        "architecture_branch": architecture_workspace.architecture_branch,
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
            if finding_value:
                references["revision_finding"] = self._publish_architecture_finding_view(
                    finding_value,
                    audience="architect",
                )
            if revision_scope is not None:
                scope_children: list[tuple[str, str]] = []
                if finding_value:
                    scope_children.append(
                        (_ref_from_mapping(finding_value).sha256, "revision_finding")
                    )
                references["revision_scope"] = self.service.artifacts.put_json(
                    {
                        "affected_modules": list(
                            revision_scope.get("affected_modules") or []
                        ),
                        "affected_verification_nodes": list(
                            revision_scope.get("affected_verification_nodes") or []
                        ),
                        "allowed_paths": list(revision_scope.get("allowed_paths") or []),
                        "allow_topology_changes": bool(
                            revision_scope.get("allow_topology_changes")
                        ),
                    },
                    artifact_type="ArchitectureSkeletonRevisionScopeArtifact",
                    provenance={
                        "owner": "manager",
                        "audience": "architect",
                    },
                    child_refs=tuple(scope_children),
                )
            if revision.payload.get("edit_instruction_ref"):
                references["edit_instruction"] = _ref_from_mapping(revision.payload["edit_instruction_ref"])
            for index, raw_reference in enumerate(list(request.get("references") or [])):
                reference = dict(raw_reference or {})
                path = str(reference.get("path") or "").strip()
                if path and Path(path).expanduser().exists():
                    name = str(reference.get("name") or f"user_reference_{index + 1}").strip()
                    references[f"user_{name}"] = _path_pseudo_ref(path, name)
            finding_payload = (
                dict(self.service.artifacts.read_json(_ref_from_mapping(finding_value)))
                if finding_value
                else {}
            )
            instruction = _skeleton_architect_instruction(
                finding=finding_payload,
                has_base_manifest=base_manifest_ref is not None,
                has_revision_scope=revision_scope is not None,
            )
            workspace_override: dict[str, Any] = {
                "kind": "existing_repo",
                "repo_path": str(architecture_workspace.worktree),
                "project_name": architecture_workspace.project_name,
                "architecture_skeleton_mode": True,
                "architecture_base_sha": architecture_workspace.base_sha,
            }
            if scope_base_submission is not None:
                workspace_override.update(
                    {
                        "architecture_revision_base_submission": dict(scope_base_submission),
                        "architecture_revision_base_sha": architecture_workspace.base_sha,
                    }
                )
                if scope_base_path_states is not None:
                    workspace_override["architecture_revision_base_path_states"] = dict(
                        scope_base_path_states
                    )
                if revision_scope is not None:
                    workspace_override["architecture_revision_scope"] = dict(revision_scope)
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
                workspace_override=workspace_override,
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
                    idempotency_key=_architecture_submit_idempotency_key(
                        revision.aggregate_id,
                        current.version,
                        submission_ref.sha256,
                    ),
                    payload={
                        "requirements_ref": requirements_ref.to_dict(),
                        "pending_architecture_submission_ref": submission_ref.to_dict(),
                        "fencing_token": lease.fencing_token,
                        "architecture_workspace_path": str(architecture_workspace.worktree),
                        "architecture_common_git_dir": str(architecture_workspace.common_git_dir),
                        "architecture_base_sha": architecture_workspace.base_sha,
                        "architecture_base_tree_sha": architecture_workspace.base_tree_sha,
                        "architecture_repository_layout": {
                            "project_name": architecture_workspace.project_name,
                            "project_key": architecture_workspace.project_key,
                            "workflow_name": architecture_workspace.workflow_name,
                            "workflow_key": architecture_workspace.workflow_key,
                            "workflow_branch": architecture_workspace.workflow_branch,
                        },
                        "architecture_branch": architecture_workspace.architecture_branch,
                        "workspace_snapshot_ref": architecture_workspace.workspace_snapshot_ref.to_dict(),
                        **(
                            {"revision_base_manifest_ref": base_manifest_ref.to_dict()}
                            if base_manifest_ref is not None
                            else {}
                        ),
                    },
                ),
                **self._worker_submission_settlement(effect),
            )
            # ARCHITECT_SUBMITTED transfers the live writer lease to the
            # quiesce/snapshot effects. Releasing it here makes a normal
            # submission look like expired-worker recovery and races the
            # worker process-group teardown.
            handed_off_to_quiescer = True
            return {"provider_request_id": invocation_id, "result_artifact_ref": submission_ref.to_dict()}
        finally:
            if not handed_off_to_quiescer:
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
        invocation_id = architect_session_id_for_revision(
            revision.workflow_id,
            revision.aggregate_id,
            revision.payload,
        )
        lease_resource = f"architecture:{revision.aggregate_id}:writer"
        fencing_token = int(revision.payload.get("fencing_token") or 0)
        active_worker = str(revision.payload.get("active_worker_id") or invocation_id)
        if fencing_token:
            if await self._reuse_or_retire_effect_lease(
                resource_key=lease_resource,
                owner_id=active_worker,
                fencing_token=fencing_token,
                worker_label=f"architecture worker {revision.aggregate_id}",
            ):
                if revision.state == "ARCHITECT_SNAPSHOTTING" and not self._worktree_locks.is_held(
                    revision.aggregate_id
                ):
                    workspace = Path(str(revision.payload.get("architecture_workspace_path") or ""))
                    await self._release_managed_lsp_workspace(workspace)
                    _raise_if_workspace_held(
                        workspace,
                        "a live process still holds the architecture worktree",
                    )
                    expected = str(revision.payload.get("workspace_fingerprint") or "")
                    current = workspace_content_fingerprint(workspace)
                    if not expected or current != expected:
                        raise RuntimeError("architecture worktree changed while snapshot worker was unavailable")
                    self._worktree_locks.acquire(revision.aggregate_id, workspace)
                return revision
        previous = self.repository.read_lease(lease_resource)
        if previous is not None and str(previous.get("owner_id") or "") and _lease_is_live(previous):
            raise LeaseConflict(f"architecture effect lease is active under {previous.get('owner_id')}")
        metadata = dict((previous or {}).get("metadata") or {})
        process_group = int(metadata.get("process_group_id") or 0)
        if process_group and not await terminate_process_group(process_group, timeout_seconds=5.0):
            raise RuntimeError("expired architect process group could not be reaped")
        workspace = Path(str(revision.payload.get("architecture_workspace_path") or ""))
        await self._release_managed_lsp_workspace(workspace)
        _raise_if_workspace_held(workspace, "expired architect still holds the architecture worktree")
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
        await self._release_managed_lsp_workspace(workspace)
        _raise_if_workspace_held(workspace, "a live process still holds the architecture worktree")
        self._worktree_locks.release(revision.aggregate_id)
        lock_path = self._worktree_locks.acquire(revision.aggregate_id, workspace)
        try:
            _raise_if_workspace_held(
                workspace,
                "a process reached the architecture worktree during quiescing",
            )
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
        revision_base_artifact: Mapping[str, Any] | None = None
        revision_scope: Mapping[str, Any] | None = None
        revision_base_path_states: Mapping[str, str] | None = None
        revision_base_value = revision.payload.get("revision_base_manifest_ref")
        finding_value = architecture_revision_finding_value(revision.payload)
        repair_baseline_value = revision.payload.get("architecture_repair_baseline_ref")
        if repair_baseline_value and finding_value:
            repair_baseline = self.service.artifacts.read_json(
                _ref_from_mapping(repair_baseline_value)
            )
            revision_base_artifact = {
                "submission": dict(repair_baseline.get("submission") or {})
            }
            revision_base_path_states = {
                str(path): str(value)
                for path, value in dict(repair_baseline.get("path_states") or {}).items()
            }
            revision_scope = architecture_revision_scope(
                dict(revision_base_artifact.get("submission") or {}),
                self.service.artifacts.read_json(_ref_from_mapping(finding_value)),
            )
        elif revision_base_value:
            loaded_revision_base = self.service.artifacts.read_json(
                _ref_from_mapping(revision_base_value)
            )
            if finding_value:
                revision_base_artifact = loaded_revision_base
                revision_scope = architecture_revision_scope(
                    dict(revision_base_artifact.get("submission") or {}),
                    self.service.artifacts.read_json(_ref_from_mapping(finding_value)),
                )
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
                revision_base_artifact=revision_base_artifact,
                revision_scope=revision_scope,
                revision_base_path_states=revision_base_path_states,
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
            repair_baseline_ref = self.service.artifacts.put_json(
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
                    payload={
                        "finding_artifact_ref": finding_ref.to_dict(),
                        "architecture_repair_baseline_ref": repair_baseline_ref.to_dict(),
                    },
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
                self._dispatch_architecture_review_result(
                    revision,
                    mechanical,
                    review_ref,
                    effect=effect,
                )
                return {"result_artifact_ref": review_ref.to_dict()}
            prompt = (
                "Review the bound ArchitectureContractArtifact and its attached fragments by tracing only its requirements, contracts, topology, "
                "ownership, lifecycle, state, invariants, complexity, and integration claims. The manager's mechanical validation "
                "already passed. Find semantic omissions or contradictions; do not redesign it."
            )
            manifest = self.service.artifacts.read_json(manifest_ref)
            requirements_ref = _ref_from_mapping(manifest.get("requirements_ref"))
            requirements_payload = self.service.artifacts.read_json(requirements_ref)
            contract_payload = self._base_contract_builder_payload_from_manifest(manifest_ref)
            semantic_contract_ref = self.service.artifacts.put_json(
                _semantic_contract_review_view(contract_payload, requirements_payload),
                artifact_type="ArchitectureContractSemanticViewArtifact",
                provenance={"owner": "manager", "audience": "architecture_reviewer"},
                child_refs=((manifest_ref.sha256, "architecture_manifest"),),
            )
            semantic_requirements_ref = self.service.artifacts.put_json(
                requirements_semantic_view(requirements_payload),
                artifact_type="RequirementsSemanticViewArtifact",
                provenance={"owner": "manager", "audience": "architecture_reviewer"},
                child_refs=((requirements_ref.sha256, "requirements"),),
            )
            review_refs: dict[str, ArtifactRef] = {
                "requirements": semantic_requirements_ref,
                "architecture_contract": semantic_contract_ref,
            }
            revision_base_value = revision.payload.get("revision_base_manifest_ref")
            if revision_base_value:
                finding_value = architecture_revision_finding_value(revision.payload)
                if finding_value:
                    review_refs["revision_finding"] = self._publish_architecture_finding_view(
                        finding_value,
                        audience="architecture_reviewer",
                    )
                root_batch_value = revision.payload.get("replan_finding_batch_ref")
                if root_batch_value:
                    review_refs["replan_finding_batch"] = self._publish_architecture_finding_view(
                        root_batch_value,
                        audience="architecture_reviewer",
                    )
                prompt += (
                    " This is a scoped revision review. Check the repaired semantic target against revision_finding, every item in the original replan_finding_batch, and the compact semantic contract view. "
                    "The manager already compared unchanged fragments. Every FAIL finding names a semantic target; hidden revision identity is Manager-owned."
                )
            workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
            request = workflow_request_from_snapshot(self.service, workflow)
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
                workspace_override={
                    **dict(request.get("workspace") or {}),
                    "contract_review_base_payload": contract_payload,
                    "contract_review_requirements_payload": requirements_payload,
                },
            )
            raw = _primary_json_output(terminal)
            semantic = _parse_architecture_review(raw)
            review_ref = self.service.artifacts.put_json(
                semantic.to_dict(),
                artifact_type="ArchitectureReviewArtifact",
                child_refs=((manifest_ref.sha256, "architecture_manifest"),),
            )
            self._dispatch_architecture_review_result(
                revision,
                semantic,
                review_ref,
                effect=effect,
            )
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
        review_workspace = None
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
            review_workspace = self.service.skeleton.provision_review_worktree(
                artifact=artifact,
                review_name=f"{revision.aggregate_id}-{manifest_ref.sha256[:12]}",
            )
            review_worktree = review_workspace.worktree
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
                self._dispatch_architecture_review_result(
                    revision,
                    mechanical,
                    review_ref,
                    effect=effect,
                )
                return {"result_artifact_ref": review_ref.to_dict()}
            requirements_view_ref = self.service.artifacts.put_json(
                requirements_semantic_view(requirements_payload),
                artifact_type="RequirementsSemanticViewArtifact",
                provenance={"owner": "manager", "audience": "architecture_reviewer"},
                child_refs=((requirements_ref.sha256, "requirements"),),
            )
            review_view = _skeleton_architecture_review_view(artifact)
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
            finding_value = architecture_revision_finding_value(revision.payload)
            if finding_value:
                references["prior_finding"] = self._publish_architecture_finding_view(
                    finding_value,
                    audience="architecture_reviewer",
                )
            root_batch_value = revision.payload.get("replan_finding_batch_ref")
            if root_batch_value:
                references["replan_finding_batch"] = self._publish_architecture_finding_view(
                    root_batch_value,
                    audience="architecture_reviewer",
                )
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
                    "Treat every covers entry as an Architect claim to audit, not as proof that the Requirement is satisfied. "
                    "Record one explicit audit for every hard Requirement, module, and Verification Node before submitting. "
                    "For a replan, explicitly audit every item in replan_finding_batch and do not PASS while any item remains unresolved. "
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
            try:
                semantic_payload = _named_json_output(terminal, "architecture_review.json")
                semantic = _parse_skeleton_review(semantic_payload)
                known_modules = set(review_view["modules"])
                for finding in semantic.findings:
                    unknown_modules = set(finding.affected_modules) - known_modules
                    if unknown_modules:
                        raise ValueError(
                            "architecture review finding references unknown modules: "
                            + ", ".join(sorted(unknown_modules))
                        )
            except Exception as exc:
                raise SubmissionInvariantError(
                    f"accepted architecture_review_submit failed manager defense-in-depth validation: {exc}"
                ) from exc
            review_ref = self.service.artifacts.put_json(
                {
                    **semantic.to_dict(),
                    "audit": dict(semantic_payload.get("audit") or {}),
                },
                artifact_type="ArchitectureReviewArtifact",
                child_refs=((manifest_ref.sha256, "architecture_skeleton"),),
            )
            self._dispatch_architecture_review_result(
                revision,
                semantic,
                review_ref,
                effect=effect,
            )
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
            if review_workspace is not None:
                review_workspace.cleanup()
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
        stored_card = dict(revision.payload.get("human_review_card_ref") or {})
        if stored_card:
            card_ref = _ref_from_mapping(stored_card)
            payload = dict(self.service.artifacts.read_json(card_ref))
        else:
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
            replan_batch_value = revision.payload.get("replan_finding_batch_ref")
            if replan_batch_value:
                replan_payload = dict(
                    self.service.artifacts.read_json(_ref_from_mapping(replan_batch_value))
                )
                payload["markdown"] = (
                    compile_architecture_finding_markdown(replan_payload)
                    + "\n"
                    + str(payload.get("markdown") or "")
                )
            card_children = [(manifest_ref.sha256, "architecture_manifest")]
            if replan_batch_value:
                card_children.append(
                    (_ref_from_mapping(replan_batch_value).sha256, "replan_findings")
                )
            card_ref = self.service.artifacts.put_json(
                payload,
                artifact_type="HumanReviewCardArtifact",
                child_refs=tuple(card_children),
            )
            current = self.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
            if current is None:
                raise ValueError("architecture revision disappeared before human review publication")
            if not current.payload.get("human_review_card_ref"):
                current = self.repository.dispatch(
                    ActionEnvelope(
                        action_type="HUMAN_REVIEW_PUBLISHED",
                        workflow_id=revision.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision.aggregate_id,
                        actor="minion-v2-manager",
                        expected_version=current.version,
                        idempotency_key=f"human-review-published:{effect.get('effect_id') or card_ref.sha256}",
                        payload={"human_review_card_ref": card_ref.to_dict()},
                    )
                ).snapshot
            persisted_ref = dict(current.payload.get("human_review_card_ref") or {})
            if persisted_ref and str(persisted_ref.get("sha256") or "") != card_ref.sha256:
                card_ref = _ref_from_mapping(persisted_ref)
                payload = dict(self.service.artifacts.read_json(card_ref))
        architecture_artifact = self._architecture_artifact_with_runtime_layout(
            revision,
            dict(self.service.artifacts.read_json(manifest_ref)),
        )
        review_value = revision.payload.get("review_artifact_ref")
        review_payload = (
            dict(self.service.artifacts.read_json(_ref_from_mapping(review_value)))
            if isinstance(review_value, Mapping) and review_value.get("sha256")
            else {"verdict": "PASS", "findings": []}
        )
        PlanRevisionProjectionStore(self.service.runtime_root).materialize(
            workflow_id=revision.workflow_id,
            revision_id=revision.aggregate_id,
            architecture_artifact=architecture_artifact,
            markdown=str(payload.get("markdown") or ""),
            review=review_payload,
            status="reviewed_pending_human",
        )
        if self.publish_human_review is not None:
            await self.publish_human_review({**payload, "card_ref": card_ref.to_dict()})
        return {"result_artifact_ref": card_ref.to_dict()}

    def _materialize_plan_revision_status(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = self._effect_snapshot(effect)
        manifest_ref = _ref_from_mapping(revision.payload.get("architecture_manifest_ref"))
        artifact = self._architecture_artifact_with_runtime_layout(
            revision,
            dict(self.service.artifacts.read_json(manifest_ref)),
        )
        root = PlanRevisionProjectionStore(self.service.runtime_root).update_status(
            workflow_id=revision.workflow_id,
            revision_id=revision.aggregate_id,
            architecture_artifact=artifact,
            status=str(effect.get("status") or revision.state.lower()),
        )
        return {"status": str(effect.get("status") or revision.state.lower()), "projection_path": str(root)}

    def _architecture_artifact_with_runtime_layout(
        self,
        revision: AggregateSnapshot,
        artifact: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = dict(artifact)
        if dict(result.get("repository_layout") or {}):
            return result
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            return result
        request = workflow_request_from_snapshot(self.service, workflow)
        layout = resolve_project_git_layout(
            self.service.runtime_root,
            workspace=dict(request.get("workspace") or {}),
            workflow_id=revision.workflow_id,
            workflow_name=str(
                request.get("workflow_name") or request.get("goal") or revision.workflow_id
            ),
        )
        result["repository_layout"] = layout.to_artifact_dict()
        return result

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
        requirements_ref = _ref_from_mapping(revision.payload.get("requirements_ref"))
        requirements_view_ref = self.service.artifacts.put_json(
            requirements_semantic_view(self.service.artifacts.read_json(requirements_ref)),
            artifact_type="RequirementsSemanticViewArtifact",
            provenance={"owner": "manager", "audience": "architect"},
            child_refs=((requirements_ref.sha256, "requirements"),),
        )
        finding_value = architecture_revision_finding_value(revision.payload)
        base_manifest_ref = self._revision_input_base_manifest_ref(revision)
        refs: dict[str, ArtifactRef]
        scoped_revision = base_manifest_ref is not None and finding_value is not None
        if base_manifest_ref is None:
            refs = {
                "workflow_request": request_ref,
                "requirements": requirements_view_ref,
            }
        elif scoped_revision:
            refs = {
                "requirements": requirements_view_ref,
                "revision_scope": self._publish_architecture_revision_scope(
                    revision,
                    base_manifest_ref=base_manifest_ref,
                    finding_value=finding_value,
                )
            }
        else:
            refs = {"requirements": requirements_view_ref}
            if revision.payload.get("edit_instruction_ref"):
                refs["edit_instruction"] = _ref_from_mapping(revision.payload.get("edit_instruction_ref"))
        if revision.payload.get("edit_instruction_ref"):
            refs["edit_instruction"] = _ref_from_mapping(revision.payload.get("edit_instruction_ref"))
        if finding_value and not scoped_revision:
            refs["revision_finding"] = self._publish_architecture_finding_view(
                finding_value,
                audience="architect",
            )
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
                " This is a scoped revision: read revision_scope first and consult the bound immutable Requirements only when exact Requirement text is needed; "
                "do not reread the repository, workflow request, or base manifest. The manager has preseeded the complete base "
                "contract privately. Change only the named semantic targets with the same incremental Contract tools used for initial authoring. "
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
        finding = architecture_revision_finding_value(revision.payload)
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
        """Publish a small semantic repair view while retaining identities internally."""

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
            value = self._revision_scope_value(base_payload, requirements_payload, target)
            context.append(
                {
                    "access": "write",
                    "target": self._semantic_revision_target(target, value),
                    "fields": list(target.fields),
                    "operation": target.operation,
                    "value": _semantic_contract_review_view(
                        {"selected": value}, requirements_payload
                    )["selected"],
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
                            "name": str(contract.get("semantic_name") or ""),
                        },
                        "value": _semantic_contract_review_view(
                            {"selected": contract}, requirements_payload
                        )["selected"],
                    }
                )
        scope = {
            "findings": [
                {
                    "finding_kind": str(dict(item or {}).get("finding_kind") or ""),
                    "summary": str(dict(item or {}).get("summary") or ""),
                    "severity": str(dict(item or {}).get("severity") or "error"),
                }
                for item in list(dict(finding_payload).get("findings") or [])
            ],
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

    def _publish_architecture_finding_view(
        self,
        finding_value: Any,
        *,
        audience: str,
    ) -> ArtifactRef:
        finding_ref = _ref_from_mapping(finding_value)
        payload = dict(self.service.artifacts.read_json(finding_ref))
        return self.service.artifacts.put_json(
            architecture_finding_semantic_view(payload),
            artifact_type=ARCHITECTURE_FINDING_BATCH_VIEW_ARTIFACT,
            provenance={"owner": "manager", "audience": audience},
            child_refs=((finding_ref.sha256, "architecture_findings"),),
        )

    def _internal_architecture_revision_scope(
        self,
        revision: AggregateSnapshot,
    ) -> dict[str, Any]:
        finding_value = architecture_revision_finding_value(revision.payload)
        if not finding_value:
            raise ValueError("scoped architecture revision has no finding")
        finding_payload = self.service.artifacts.read_json(_ref_from_mapping(finding_value))
        raw_targets = [
            target
            for finding in list(dict(finding_payload).get("findings") or [])
            for target in list(dict(finding or {}).get("revision_targets") or [])
        ]
        targets = normalize_revision_targets(raw_targets)
        if not targets:
            raise ValueError("architecture revision finding requires semantic revision_targets")
        return {"write_targets": [target.to_dict() for target in targets]}

    @staticmethod
    def _semantic_revision_target(
        target: ArchitectureRevisionTarget,
        value: Any,
    ) -> dict[str, Any]:
        selected = dict(value or {}) if isinstance(value, Mapping) else {}
        if target.section == "requirements":
            return {
                "section": "requirements",
                "requirement_section": str(selected.get("section") or "Requirements"),
                "requirement": str(selected.get("statement") or ""),
            }
        if target.section in {"unit", "topology"}:
            return {
                "section": target.section,
                "name": str(selected.get("unit_id") or target.target_id),
            }
        if target.section in {
            "constraint",
            "design_decision",
            "gate_check",
            "cross_unit_contract",
        }:
            semantic_name = str(selected.get("semantic_name") or "").strip()
            if not semantic_name:
                raise ValueError(f"{target.section} revision target has no semantic name")
            return {"section": target.section, "name": semantic_name}
        return {"section": target.section, "name": target.section}

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
        if role in {"producer", "repair"}:
            workspace["manager_owned_submission_paths"] = [
                "coder_report.json",
                "producer_report.json",
            ]
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
            view_name = "module_work_view" if role == "verifier" else "review_request"
            view_ref = bound_reference_refs.get(view_name)
            view = self.service.artifacts.read_json(view_ref) if view_ref is not None else {}
            effective_policy = effective_verification_policy(
                work_view=view,
                verification_policy=dict(family_policies.get("verification") or {}),
                standalone=role == "reviewer",
            )
            verification_policy_ref = self.service.artifacts.put_json(
                effective_policy,
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
                "Build the semantic module and verification topology incrementally, then call architecture_submit with no arguments.",
            ]
        elif skeleton_mode and role == "architecture_reviewer":
            invocation_acceptance = [
                "Review the bound Requirements, skeleton diff, code contracts, and semantic DAG.",
                "Record one audit for every hard Requirement, module, and Verification Node; record each material finding, then call architecture_review_submit with no arguments.",
            ]
        elif role == "verifier":
            invocation_acceptance = [
                "Execute and register reproducible adversarial cases with dedicated verification tools, then record semantic findings.",
                "Call verification_submit with no arguments; do not write an output artifact or verdict directly.",
            ]
        elif role in {"producer", "repair"}:
            if self._is_skeleton_manifest(snapshot.payload.get("architecture_manifest_ref")):
                invocation_acceptance = [
                    "Implement or repair only the bound module and run focused developer tests.",
                    "Record checks with dedicated developer tools, then call candidate_submit with no arguments.",
                ]
            else:
                invocation_acceptance = [
                    "Write the contracted product artifact in the bound workspace and run a focused validation.",
                    "Record progress and checks with dedicated developer tools, then call candidate_submit with no arguments; do not write producer_report.json.",
                ]
        elif role == "reviewer":
            invocation_acceptance = [
                "Review only the bound immutable target and run reproducible read-only probes.",
                "Record surfaces, findings, and conclusion incrementally, then call review_submit with no arguments.",
            ]
        else:
            invocation_acceptance = ["Write the exact primary JSON artifact required by the profile output contract."]
        input_fingerprint = authoring_input_fingerprint(
            {
                "role": role,
                "references": {
                    name: ref.to_dict()
                    for name, ref in sorted(bound_reference_refs.items())
                },
                "architecture_revision_base_submission": workspace.get(
                    "architecture_revision_base_submission"
                ),
                "architecture_revision_scope": workspace.get(
                    "architecture_revision_scope"
                ),
            }
        )
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
                    "lease_resource_key": lease_resource,
                    "fencing_token": fencing_token,
                    "role": role,
                    "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
                    "authoring_input_fingerprint": input_fingerprint,
                },
                "agent_session": {
                    "session_id": invocation_id,
                    "response_key": str(effect.get("effect_key") or effect.get("effect_id") or ""),
                    "fencing_token": int(fencing_token),
                },
                "requirements_brief": {
                    "references": references,
                    "research_mode": snapshot.payload.get("research_mode", "local_only"),
                },
                "allow_text_only_completion": role
                not in {*builder_stages, "verifier", "producer", "repair", "reviewer"},
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
            if skeleton_mode:
                revision_scope = dict(workspace.get("architecture_revision_scope") or {}) or None
            else:
                revision_scope = self._internal_architecture_revision_scope(snapshot)
        profile_definitions = dict(binding.get("profile_definitions") or {})
        pinned_profile = dict(profile_definitions.get(role) or {})
        if not pinned_profile:
            roles = dict(binding.get("roles") or {})
            matching_role = next(
                (name for name, profile_id in roles.items() if str(profile_id) == profile),
                "",
            )
            pinned_profile = dict(profile_definitions.get(matching_role) or {})
        if not pinned_profile:
            raise ValueError(f"FamilyBindingArtifact has no pinned profile definition for role {role}")
        pack = resolve_pinned_minion_pack(
            pack,
            profile_payload=pinned_profile,
            family_payload=dict(binding.get("manifest") or {}),
        )
        pack = apply_v2_role_capability_policy(pack, role=role)
        if role == "architect" and revision_scope is not None:
            pack = apply_v2_revision_scope_capability_policy(pack)
        pack = apply_v2_research_capability_policy(
            pack,
            research_mode=str(snapshot.payload.get("research_mode") or "local_only"),
        )
        if role == "architecture_reviewer" and skeleton_mode:
            requirements_ref = bound_reference_refs.get("requirements")
            architecture_ref = bound_reference_refs.get("architecture_index")
            if requirements_ref is None or architecture_ref is None:
                raise ValueError("Architecture Reviewer requires bound requirements and architecture_index")
            tool_contract = compile_architecture_review_invocation_tool_contract(
                requirements=self.service.artifacts.read_json(requirements_ref),
                architecture=self.service.artifacts.read_json(architecture_ref),
            )
            pack_value = pack.to_dict()
            metadata = dict(pack_value.get("metadata") or {})
            minion_v2 = dict(metadata.get("minion_v2") or {})
            minion_v2["architecture_review_tool_contract"] = tool_contract
            metadata["minion_v2"] = minion_v2
            resolved_profile = dict(pack_value.get("resolved_profile") or {})
            description_overrides = dict(
                resolved_profile.get("capability_description_overrides") or {}
            )
            description_overrides.update(
                {
                    str(key): str(value)
                    for key, value in dict(tool_contract.get("description_overrides") or {}).items()
                }
            )
            resolved_profile["capability_description_overrides"] = description_overrides
            pack = MinionInvocationPack.from_dict(
                {
                    **pack_value,
                    "metadata": metadata,
                    "resolved_profile": resolved_profile,
                }
            )
        if role in {"verifier", "reviewer"}:
            view_name = "module_work_view" if role == "verifier" else "review_request"
            view_ref = bound_reference_refs.get(view_name)
            if view_ref is not None:
                tool_contract = compile_verification_invocation_tool_contract(
                    work_view=self.service.artifacts.read_json(view_ref),
                    verification_policy=dict(family_policies.get("verification") or {}),
                    standalone=role == "reviewer",
                )
                pack_value = pack.to_dict()
                metadata = dict(pack_value.get("metadata") or {})
                minion_v2 = dict(metadata.get("minion_v2") or {})
                minion_v2["verification_tool_contract"] = tool_contract
                metadata["minion_v2"] = minion_v2
                resolved_profile = dict(pack_value.get("resolved_profile") or {})
                description_overrides = dict(
                    resolved_profile.get("capability_description_overrides") or {}
                )
                description_overrides.update(
                    {
                        str(key): str(value)
                        for key, value in dict(
                            tool_contract.get("description_overrides") or {}
                        ).items()
                    }
                )
                resolved_profile["capability_description_overrides"] = description_overrides
                allowed_verification_capabilities = {
                    str(item) for item in list(tool_contract.get("allowed_capabilities") or [])
                }
                pack_value["allowed_capabilities"] = [
                    capability
                    for capability in list(pack_value.get("allowed_capabilities") or [])
                    if capability not in VERIFICATION_TOOL_CAPABILITIES
                    or capability in allowed_verification_capabilities
                ]
                pack = MinionInvocationPack.from_dict(
                    {
                        **pack_value,
                        "metadata": metadata,
                        "resolved_profile": resolved_profile,
                    }
                )
        run_id = f"run_{invocation_id.removeprefix('inv_')[:16]}"
        if prepare_workspace:
            pack = prepare_v2_role_workspace(
                self.service.runtime_root,
                pack,
                run_id=run_id,
                attempt_key=f"fence-{fencing_token}",
            )
        else:
            invocation_dir = invocation_root(self.service.runtime_root) / invocation_id
            attempt_dir = invocation_dir / "attempts" / f"fence-{fencing_token}"
            bound_workspace = dict(pack.workspace)
            bound_workspace.update(
                {
                    "run_dir": str(invocation_dir),
                    "artifact_dir": str(attempt_dir / "artifacts"),
                    "artifact_stage_dir": str(attempt_dir / "artifact-stage"),
                    "log_dir": str(attempt_dir / "logs"),
                    "review_scratch_dir": str(
                        bound_workspace.get("review_scratch_dir")
                        or attempt_dir / "review-scratch"
                    ),
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
        submission_kind = _worker_submission_kind(role, skeleton_mode=skeleton_mode)
        durable_input_refs = {
            name: ref.to_dict()
            for name, ref in bound_reference_refs.items()
            if ref.artifact_type != "LocalPathReference"
        }
        self.repository.ensure_worker_session(
            session_id=invocation_id,
            workflow_id=snapshot.workflow_id,
            aggregate_type=snapshot.aggregate_type,
            aggregate_id=snapshot.aggregate_id,
            role=role,
        )
        assignment = self.repository.create_worker_assignment(
            WorkerAssignmentRequest(
                assignment_key=(
                    f"{str(effect.get('effect_key') or effect.get('effect_id') or '')}:"
                    f"{role}:{input_fingerprint}"
                ),
                session_id=invocation_id,
                workflow_id=snapshot.workflow_id,
                aggregate_type=snapshot.aggregate_type.value,
                aggregate_id=snapshot.aggregate_id,
                role=role,
                input_fingerprint=input_fingerprint,
                required_inputs=tuple(sorted(durable_input_refs)),
                input_refs=durable_input_refs,
                execution_spec={
                    "effect_type": str(effect.get("effect_type") or "spawn_worker"),
                    "effect_id": str(effect.get("effect_id") or ""),
                    "effect_key": str(effect.get("effect_key") or ""),
                    "workflow_id": snapshot.workflow_id,
                    "aggregate_type": snapshot.aggregate_type.value,
                    "aggregate_id": snapshot.aggregate_id,
                    "payload": dict(effect.get("payload") or {}),
                },
                submission_kind=submission_kind,
            )
        )
        self._signal_assignment_ready(effect, str(assignment["assignment_id"]))
        if assignment["state"] in {
            WorkerAssignmentState.CLAIMED.value,
            WorkerAssignmentState.RUNNING.value,
        }:
            active_attempt = self.repository.read_worker_attempt(
                str(assignment.get("active_attempt_id") or "")
            )
            active_lease = self.repository.read_lease(
                str(dict(active_attempt or {}).get("lease_resource_key") or "")
            )
            if active_lease is not None and _lease_is_live(active_lease):
                raise DeferredEffectError("worker assignment already has a live process attempt")
            if active_attempt is not None:
                assignment = self.repository.queue_worker_attempt_retry(
                    assignment_id=str(assignment["assignment_id"]),
                    attempt_id_value=str(active_attempt["attempt_id"]),
                    error_kind="attempt_lease_expired",
                    error_text="worker attempt lease expired before submission settlement",
                )

        if assignment["state"] in {
            WorkerAssignmentState.RESULT_RECORDED.value,
            WorkerAssignmentState.SETTLED.value,
        }:
            pack = sanitize_runner_session_pack(pack)
            prompt_ref = self.service.artifacts.put_json(
                pack.to_dict(),
                artifact_type="WorkerPromptPackArtifact",
                child_refs=tuple(
                    (ref.sha256, name)
                    for name, ref in bound_reference_refs.items()
                    if ref.artifact_type != "LocalPathReference"
                ),
            )
            terminal = self._terminal_from_assignment_receipt(
                assignment,
                role=role,
                summary="Reconciled the exact durable worker submission receipt.",
            )
            terminal_ref = self.service.artifacts.put_json(
                terminal,
                artifact_type="WorkerTerminalArtifact",
                child_refs=(
                    (prompt_ref.sha256, "prompt_pack"),
                    (
                        str(dict(assignment["submission_artifact_ref"])["sha256"]),
                        "submission_receipt",
                    ),
                ),
            )
            return terminal, prompt_ref, terminal_ref

        if assignment["state"] not in {
            WorkerAssignmentState.QUEUED.value,
            WorkerAssignmentState.RETRY_QUEUED.value,
        }:
            raise SubmissionInvariantError(
                f"worker assignment cannot start from {assignment['state']}"
            )
        attempt = self.repository.claim_worker_assignment(str(assignment["assignment_id"]))
        assignment_lease_resource = f"assignment:{assignment['assignment_id']}"
        assignment_lease = self.repository.claim_lease(
            assignment_lease_resource,
            str(attempt["attempt_id"]),
            ttl_seconds=120,
            metadata={
                "workflow_id": snapshot.workflow_id,
                "aggregate_type": snapshot.aggregate_type.value,
                "aggregate_id": snapshot.aggregate_id,
                "role": role,
            },
        )
        pack_value = pack.to_dict()
        metadata = dict(pack_value.get("metadata") or {})
        minion_v2 = dict(metadata.get("minion_v2") or {})
        minion_v2.update(
            {
                "invocation_id": str(attempt["attempt_id"]),
                "lease_resource": assignment_lease_resource,
                "lease_resource_key": assignment_lease_resource,
                "fencing_token": assignment_lease.fencing_token,
            }
        )
        metadata["minion_v2"] = minion_v2
        metadata["agent_session"] = {
            "session_id": invocation_id,
            "response_key": str(effect.get("effect_key") or effect.get("effect_id") or ""),
            "fencing_token": assignment_lease.fencing_token,
        }
        pack = MinionInvocationPack.from_dict({**pack_value, "metadata": metadata})
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
        self.repository.start_worker_attempt(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(attempt["attempt_id"]),
            lease_resource_key=assignment_lease_resource,
            fencing_token=assignment_lease.fencing_token,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        assignment_access_token = self.repository.issue_worker_attempt_access_token(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(attempt["attempt_id"]),
            fencing_token=assignment_lease.fencing_token,
        )
        self.repository.record_worker_invocation(
            invocation_id=invocation_id,
            workflow_id=snapshot.workflow_id,
            aggregate_type=snapshot.aggregate_type,
            aggregate_id=snapshot.aggregate_id,
            lease_resource_key=lease_resource,
            fencing_token=fencing_token,
            role=profile_name,
            authoring_contract_version=AUTHORING_CONTRACT_VERSION,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        invocation_dir = invocation_root(self.service.runtime_root) / invocation_id
        invocation_dir.mkdir(parents=True, exist_ok=True)
        attempt_dir = invocation_dir / "attempts" / f"fence-{fencing_token}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        pack_path = attempt_dir / "pack.json"
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
        runner_env = python_subprocess_env()
        runner_env[WORKER_GATEWAY_TOKEN_ENV] = assignment_access_token
        argv, env = build_sandboxed_runner_invocation(
            runtime_root=self.service.runtime_root,
            pack=pack,
            argv=argv,
            env=runner_env,
        )
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        self.repository.update_worker_attempt_process_group(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(attempt["attempt_id"]),
            fencing_token=assignment_lease.fencing_token,
            process_group_id=process.pid,
        )
        self.repository.update_lease_metadata(
            assignment_lease_resource,
            str(attempt["attempt_id"]),
            assignment_lease.fencing_token,
            {
                "workflow_id": snapshot.workflow_id,
                "aggregate_type": snapshot.aggregate_type.value,
                "aggregate_id": snapshot.aggregate_id,
                "role": role,
                "process_group_id": process.pid,
                "workspace_path": str(pack.workspace.get("repo_path") or ""),
                "run_id": run_id,
            },
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
        assignment_heartbeat = asyncio.create_task(
            self._lease_heartbeat(
                assignment_lease_resource,
                str(attempt["attempt_id"]),
                assignment_lease.fencing_token,
            ),
            name=f"minion-v2-assignment-lease-{attempt['attempt_id']}",
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
            assignment_heartbeat.cancel()
            try:
                await lease_heartbeat
            except asyncio.CancelledError:
                pass
            try:
                await assignment_heartbeat
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
        assignment_after_process = self.repository.read_worker_assignment(
            str(assignment["assignment_id"])
        )
        has_submission_receipt = bool(
            dict((assignment_after_process or {}).get("submission_artifact_ref") or {})
        )
        if process.returncode != 0 and not has_submission_receipt:
            error_tail = _meaningful_stderr_tail(stderr.decode("utf-8", errors="replace"))
            self.repository.queue_worker_attempt_retry(
                assignment_id=str(assignment["assignment_id"]),
                attempt_id_value=str(attempt["attempt_id"]),
                error_kind="worker_process_failed",
                error_text=worker_error or error_tail or "worker emitted no structured error",
            )
            with contextlib.suppress(Exception):
                self.repository.release_lease(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                )
            continuation_ref = self._publish_agent_session_checkpoint(
                invocation_id,
                assignment_lease.fencing_token,
            )
            if continuation_ref is not None and role in {"architect", "producer", "repair", "verifier"}:
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
        if terminal is None and has_submission_receipt:
            terminal = self._terminal_from_assignment_receipt(
                dict(assignment_after_process or {}),
                role=role,
                summary="Recovered a durable submission after the worker process ended.",
            )
        if terminal is None:
            self.repository.queue_worker_attempt_retry(
                assignment_id=str(assignment["assignment_id"]),
                attempt_id_value=str(attempt["attempt_id"]),
                error_kind="missing_terminal_and_receipt",
                error_text="worker ended without terminal event or durable submission receipt",
            )
            with contextlib.suppress(Exception):
                self.repository.release_lease(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                )
            continuation_ref = self._publish_agent_session_checkpoint(
                invocation_id,
                assignment_lease.fencing_token,
            )
            if continuation_ref is not None and role in {"architect", "producer", "repair", "verifier"}:
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
        if (
            str(terminal_payload.get("status") or "") == "suspended"
            and bool(terminal_payload.get("manager_restart"))
        ):
            self.repository.queue_worker_attempt_retry(
                assignment_id=str(assignment["assignment_id"]),
                attempt_id_value=str(attempt["attempt_id"]),
                error_kind="manager_restart",
                error_text=str(
                    terminal_payload.get("summary")
                    or "worker deferred for manager restart"
                ),
            )
            with contextlib.suppress(Exception):
                self.repository.release_lease(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                )
            continuation_ref = self._publish_agent_session_checkpoint(
                invocation_id,
                assignment_lease.fencing_token,
            )
            if continuation_ref is None:
                raise RuntimeError(
                    "worker reached a manager-restart safe point without a durable continuation"
                )
            self.repository.suspend_worker_invocation(
                invocation_id=invocation_id,
                fencing_token=fencing_token,
                continuation_ref=continuation_ref.to_dict(),
                status="interrupted",
            )
            raise DeferredEffectError(
                str(terminal_payload.get("summary") or "worker deferred for manager restart")
            )
        if str(terminal_payload.get("status") or "") != "completed":
            summary = str(terminal_payload.get("summary") or "V2 semantic worker failed")
            completion_stalled = (
                str(terminal_payload.get("blocker_kind") or "")
                == "completion_gate_stalled"
            )
            if not completion_stalled:
                self.repository.queue_worker_attempt_retry(
                    assignment_id=str(assignment["assignment_id"]),
                    attempt_id_value=str(attempt["attempt_id"]),
                    error_kind="worker_terminal_failed",
                    error_text=summary,
                )
            with contextlib.suppress(Exception):
                self.repository.release_lease(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                )
            continuation_ref = self._publish_agent_session_checkpoint(
                invocation_id,
                assignment_lease.fencing_token,
            )
            if continuation_ref is not None and role in {"architect", "producer", "repair", "verifier"}:
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
            if completion_stalled:
                raise PermanentEffectError(summary)
            raise RuntimeError(summary)
        assignment_after_process = self.repository.read_worker_assignment(
            str(assignment["assignment_id"])
        )
        if assignment_after_process is None or assignment_after_process["state"] not in {
            WorkerAssignmentState.RESULT_RECORDED.value,
            WorkerAssignmentState.SETTLED.value,
        }:
            with contextlib.suppress(Exception):
                self.repository.release_lease(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                )
            raise SubmissionInvariantError(
                "worker reported completion before its durable submission receipt"
            )
        with contextlib.suppress(Exception):
            self.repository.release_lease(
                assignment_lease_resource,
                str(attempt["attempt_id"]),
                assignment_lease.fencing_token,
            )
        terminal = self._terminal_from_assignment_receipt(
            assignment_after_process,
            role=role,
            summary=str(dict(terminal.get("payload") or {}).get("summary") or "Worker submission recorded."),
            original_terminal=terminal,
        )
        terminal_payload = dict(terminal.get("payload") or {})
        continuation_ref = self._publish_agent_session_checkpoint(
            invocation_id,
            assignment_lease.fencing_token,
        )
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
                (
                    str(dict(assignment_after_process["submission_artifact_ref"])["sha256"]),
                    "submission_receipt",
                ),
                *(
                    ((continuation_ref.sha256, "agent_session_continuation"),)
                    if continuation_ref is not None
                    else ()
                ),
            ),
        )
        if role in {"architect", "producer", "repair", "verifier"}:
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

    def _terminal_from_assignment_receipt(
        self,
        assignment: Mapping[str, Any],
        *,
        role: str,
        summary: str,
        original_terminal: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_ref = dict(assignment.get("submission_artifact_ref") or {})
        if not artifact_ref:
            raise SubmissionInvariantError("worker assignment has no submission artifact")
        submitted = self.service.artifacts.read_json(artifact_ref)
        if stable_hash(submitted) != str(assignment.get("submission_payload_hash") or ""):
            raise SubmissionInvariantError(
                "worker assignment submission payload hash does not match its artifact"
            )
        record = self.repository.read_artifact_record(str(artifact_ref.get("sha256") or ""))
        if record is None:
            raise SubmissionInvariantError("worker assignment submission artifact is unavailable")
        filename = {
            "architect": "architecture_submission.json",
            "architecture_reviewer": "architecture_review.json",
            "producer": "coder_report.json",
            "repair": "coder_report.json",
            "verifier": "verification_plan.json",
            "scenario_verifier": "verification_plan.json",
            "reviewer": "standalone_review.json",
            "requirements": "requirements.json",
        }.get(role, "architecture_bundle.json")
        primary = {
            "path": str(record["storage_path"]),
            "relative_path": filename,
            "title": "Durable worker submission",
            "role": "primary",
            "mime_type": "application/json",
        }
        original_payload = dict(dict(original_terminal or {}).get("payload") or {})
        return {
            "event_kind": "terminal",
            "phase": "completed",
            "payload": {
                **original_payload,
                "status": "completed",
                "summary": str(summary or "Worker submission recorded."),
                "artifacts": [primary],
                "primary_artifact": primary,
                "submission_receipt": artifact_ref,
                "session_turn_index": int(
                    original_payload.get("session_turn_index") or 0
                ),
                "v2_timing": dict(original_payload.get("v2_timing") or {}),
            },
        }

    def _publish_agent_session_checkpoint(
        self,
        invocation_id: str,
        fencing_token: int,
    ) -> ArtifactRef | None:
        invocation_dir = invocation_root(self.service.runtime_root) / invocation_id
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

    async def _reuse_or_retire_effect_lease(
        self,
        *,
        resource_key: str,
        owner_id: str,
        fencing_token: int,
        worker_label: str,
    ) -> bool:
        """Reuse a fresh admission lease, or fence an unmanaged prior worker."""

        try:
            self.repository.assert_fencing_token(resource_key, owner_id, fencing_token)
            process = self._processes.get(owner_id)
            if process is not None and process.returncode is None:
                raise LeaseConflict(f"{worker_label} is already active in this manager")
            lease = self.repository.read_lease(resource_key)
            process_group = int(dict((lease or {}).get("metadata") or {}).get("process_group_id") or 0)
            if process_group <= 0:
                # Admission and process spawn are separate effects. Refresh the
                # lease here so a delayed outbox claim still gets a full TTL
                # before the first heartbeat.
                self.repository.renew_lease(
                    resource_key,
                    owner_id,
                    fencing_token,
                    ttl_seconds=120,
                )
                return True
            if not await terminate_process_group(process_group, timeout_seconds=5.0):
                raise RuntimeError(f"prior {worker_label} process group could not be reaped before rebind")
            self.repository.release_lease(resource_key, owner_id, fencing_token)
            return False
        except StaleFencingToken:
            return False

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
        *,
        effect: Mapping[str, Any] | None = None,
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
            ),
            **self._worker_submission_settlement(effect or {}),
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


def _skeleton_architect_instruction(
    *,
    finding: Mapping[str, Any],
    has_base_manifest: bool,
    has_revision_scope: bool,
) -> str:
    instruction = (
        "Design the requested software architecture in the bound writable worktree. Requirements is the immutable product truth. "
        "Write contract-level code skeletons, a Construction DAG, directional contract-consumption references, and real scenario-specific Verification Nodes. "
        "A universal integration/join is forbidden unless a real product entrypoint requires that exact combination. "
        "Do not implement behavior, algorithms, mapping tables, SDK call sequences, or complete tests."
    )
    if has_base_manifest:
        instruction += (
            " This is a revision based on the existing skeleton. Modify only locations named by revision_finding or the explicit edit instruction; "
            "preserve every unrelated declaration, contract, path scope, and dependency. The semantic DAG Draft is already seeded from "
            "the accepted baseline: do not remove, recreate, or restate unchanged modules or Verification Nodes. A source-only contract "
            "repair may submit the unchanged semantic DAG after editing the scoped skeleton files."
        )
    if finding:
        summary = str(finding.get("summary") or "the bound architecture finding").strip()
        repair = str(finding.get("repair_instruction") or "").strip()
        instruction += (
            " A previous architecture submission was rejected and is not accepted. Read revision_finding before any other work, "
            f"correct this exact defect: {summary}"
            + (f" Repair boundary: {repair}" if repair else "")
            + " Do not report the earlier submit as completion. Call architecture_submit again after the correction."
        )
    elif has_base_manifest:
        instruction += (
            " Use the same incremental architecture tools to change only scoped semantic units, then call architecture_submit with no arguments."
        )
    if has_revision_scope:
        instruction += (
            " Read the bound revision_scope before editing. If one physical reference or contract defect affects multiple named modules, "
            "repair every affected module in the same candidate; do not wait for stable preflight to report the same defect one module at a time. "
            "The Manager will mechanically reject semantic or source changes outside this scope."
        )
    return instruction


def _skeleton_architecture_review_view(artifact: Mapping[str, Any]) -> dict[str, Any]:
    submission = artifact.get("submission")
    if not isinstance(submission, Mapping):
        raise SubmissionInvariantError("architecture skeleton is missing its semantic submission")
    review_view = dict(submission)
    review_view["changed_paths"] = list(artifact.get("changed_paths") or [])
    return review_view


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
    current = set(pack.allowed_capabilities)
    if role == "reviewer" and not current.intersection(STANDALONE_REVIEW_BUILDER_CAPABILITIES):
        return pack
    if role == "architect":
        allowed_authoring = (
            set(ARCHITECTURE_SKELETON_CAPABILITIES)
            if current.intersection(ARCHITECTURE_SKELETON_CAPABILITIES)
            else set(ARCHITECT_BUILDER_CAPABILITIES)
        )
    elif role == "architecture_reviewer":
        allowed_authoring = (
            set(SKELETON_REVIEW_CAPABILITIES)
            if current.intersection(SKELETON_REVIEW_CAPABILITIES)
            else set(ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES)
        )
    else:
        allowed_authoring = {
            "requirements": set(REQUIREMENTS_BUILDER_CAPABILITIES),
            "planner": set(CONTRACT_SKETCH_BUILDER_CAPABILITIES),
            "verifier": set(VERIFICATION_BUILDER_CAPABILITIES),
            "reviewer": set(STANDALONE_REVIEW_BUILDER_CAPABILITIES),
            "producer": set(CANDIDATE_BUILDER_CAPABILITIES),
            "repair": set(CANDIDATE_BUILDER_CAPABILITIES),
        }.get(str(role))
    if allowed_authoring is None:
        return pack
    forbidden_writes = {
        "op_minion_artifact_write",
        "op_minion_artifact_edit",
    }
    if role in {"architect", "architecture_reviewer", "requirements", "planner", "verifier", "reviewer"}:
        forbidden_writes.update({"op_file_write", "op_file_edit", "op_path_delete"})
    if role == "architect" and current.intersection(ARCHITECTURE_SKELETON_CAPABILITIES):
        forbidden_writes.difference_update({"op_file_write", "op_file_edit", "op_path_delete"})
    if role in {"producer", "repair"} and str(pack.profile_group or "") != "software_engineering":
        forbidden_writes.difference_update(
            {"op_minion_artifact_write", "op_minion_artifact_edit"}
        )
    capabilities = [
        capability
        for capability in pack.allowed_capabilities
        if capability not in forbidden_writes
        and (not _is_authoring_capability_name(capability) or capability in allowed_authoring)
    ]
    return MinionInvocationPack.from_dict({**pack.to_dict(), "allowed_capabilities": capabilities})


def _is_authoring_capability_name(name: str) -> bool:
    return str(name or "").startswith(
        (
            "op_minion_requirement",
            "op_minion_requirements",
            "op_minion_contract",
            "op_minion_architecture",
            "op_minion_developer",
            "op_minion_candidate",
            "op_minion_verification",
            "op_minion_review_",
            "op_minion_standalone_review",
        )
    )


def apply_v2_revision_scope_capability_policy(pack: MinionInvocationPack) -> MinionInvocationPack:
    """Revision scope is enforced by the Binder and Draft reducer, not a second tool surface."""

    return pack


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


def _recorded_verification_case_results(
    plan: Mapping[str, Any],
    *,
    cases: list[VerificationCaseSpec],
    artifacts: Any,
    runtime_root: Path,
    workflow_id: str,
    invocation_id: str,
    lease_resource_key: str,
    fencing_token: int,
    role: str,
    draft_kind: str,
) -> list[VerificationCaseResult]:
    internal = dict(plan.get("internal_context") or {})
    if str(internal.get("invocation_id") or "") != invocation_id:
        raise ValueError("recorded verification evidence belongs to another invocation")
    input_fingerprint = str(internal.get("input_fingerprint") or "").strip()
    if not input_fingerprint:
        raise ValueError("recorded verification evidence has no bound input fingerprint")
    draft_key = str(internal.get("draft_key") or "").strip()
    if not draft_key:
        raise ValueError("recorded verification evidence has no Draft binding")
    durable = SubmissionDraftStore(runtime_root).read_submitted(draft_key)
    if (
        durable.workflow_id != workflow_id
        or durable.invocation_id != invocation_id
        or durable.role != role
        or durable.draft_kind != draft_kind
        or durable.input_fingerprint != input_fingerprint
        or durable.fencing_token != int(internal.get("fencing_token") or 0)
    ):
        raise ValueError("recorded verification evidence Draft binding is invalid")
    durable_plan = artifacts.read_json(durable.submission_artifact_ref)
    if json.dumps(durable_plan, ensure_ascii=False, sort_keys=True) != json.dumps(
        dict(plan), ensure_ascii=False, sort_keys=True
    ):
        raise ValueError("verification artifact does not match its durable submission receipt")
    recorded = recorded_cases(durable.payload)
    submitted = [dict(item or {}) for item in list(plan.get("recorded_results") or [])]
    if json.dumps(recorded, ensure_ascii=False, sort_keys=True) != json.dumps(
        submitted,
        ensure_ascii=False,
        sort_keys=True,
    ):
        raise ValueError("verification artifact does not match the fenced durable Draft")
    by_name = {str(item.get("name") or ""): item for item in recorded}
    if set(by_name) != {case.case_name for case in cases}:
        raise ValueError("recorded verification results do not match declared case names")
    results: list[VerificationCaseResult] = []
    for case in cases:
        item = by_name[case.case_name]
        if str(item.get("input_fingerprint") or "") != input_fingerprint:
            raise ValueError(f"case {case.case_name!r} was recorded against different immutable inputs")
        if tuple(str(value) for value in list(item.get("command") or [])) != case.command:
            raise ValueError(f"case {case.case_name!r} command differs from its recorded execution")
        status = VerificationStatus(str(item.get("status") or ""))
        exit_code = item.get("exit_code")
        if status == VerificationStatus.PASS and (
            exit_code is None or int(exit_code) not in case.expected_exit_codes
        ):
            raise ValueError(f"case {case.case_name!r} has an impossible PASS result")
        if status == VerificationStatus.FAIL and (
            exit_code is None or int(exit_code) in case.expected_exit_codes
        ):
            raise ValueError(f"case {case.case_name!r} has an impossible FAIL result")
        stdout_ref = dict(item.get("stdout_ref") or {})
        stderr_ref = dict(item.get("stderr_ref") or {})
        if status != VerificationStatus.UNKNOWN or stdout_ref:
            artifacts.read_bytes(stdout_ref)
        if status != VerificationStatus.UNKNOWN or stderr_ref:
            artifacts.read_bytes(stderr_ref)
        results.append(
            VerificationCaseResult(
                case_id=case.case_id,
                case_name=case.case_name,
                case_kind=case.case_kind,
                status=status,
                command=case.command,
                exit_code=int(exit_code) if exit_code is not None else None,
                stdout_ref=stdout_ref,
                stderr_ref=stderr_ref,
                environment=dict(item.get("environment") or {}),
                summary=str(item.get("summary") or ""),
                requirements=case.requirements,
                locations=case.locations,
                invariants=case.invariants,
            )
        )
    return results


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
        defect_kind = str(item.get("defect_kind") or "").strip()
        if defect_kind and defect_kind not in {value.value for value in DefectKind}:
            raise ValueError(
                f"verification finding for {case_name!r} has invalid defect_kind: {defect_kind}"
            )
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
            **({"defect_kind": defect_kind} if defect_kind else {}),
            **(
                {"target_module": str(item.get("target_module") or "").strip()}
                if str(item.get("target_module") or "").strip()
                else {}
            ),
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


def _validate_skeleton_coder_report(
    value: Mapping[str, Any],
    *,
    expected_module: str,
    work_view: Mapping[str, Any],
) -> None:
    bound_view = dict(work_view)
    bound_module = str(bound_view.get("module_name") or expected_module or "").strip()
    if bound_module != str(expected_module or "").strip():
        raise ValueError(
            f"Coder work view module {bound_module!r} does not match expected module {expected_module!r}"
        )
    bound_view["module_name"] = bound_module
    validate_candidate_submission(value, work_view=bound_view)


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


def _resolve_verification_defect_targets(
    repository: MinionV2Repository,
    node: AggregateSnapshot,
    *,
    plan: Mapping[str, Any],
    status: VerificationStatus,
    defect_kind: DefectKind,
    scenario_mode: bool,
) -> tuple[str, str]:
    """Resolve only the node target required by the selected FAIL route."""

    if status != VerificationStatus.FAIL:
        return "", ""
    if defect_kind == DefectKind.DEPENDENCY:
        return (
            _resolve_dependency_node_id(
                repository,
                node,
                dependency_module=str(plan.get("dependency_module") or ""),
            ),
            "",
        )
    if scenario_mode and defect_kind == DefectKind.MODULE:
        return (
            "",
            _resolve_dependency_node_id(
                repository,
                node,
                dependency_module=str(plan.get("affected_module") or ""),
            ),
        )
    return "", ""


def _resolve_dependency_node_id(
    repository: MinionV2Repository,
    node: AggregateSnapshot,
    *,
    dependency_module: str,
) -> str:
    name = str(dependency_module or "").strip()
    if not name:
        raise ValueError("verification defect route requires a target module")
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


def _semantic_contract_review_view(
    contract: Mapping[str, Any],
    requirements: Mapping[str, Any],
) -> dict[str, Any]:
    requirement_by_id = {
        str(dict(item).get("requirement_id") or ""): {
            "section": str(dict(item).get("section") or "Requirements"),
            "requirement": str(dict(item).get("statement") or ""),
        }
        for item in list(requirements.get("requirements") or [])
    }

    def semantic(value: Any) -> Any:
        if isinstance(value, Mapping):
            result: dict[str, Any] = {}
            for key, item in value.items():
                if key in {"id", "schema_version", "complexity_policy_violations"}:
                    continue
                if key == "unit_id":
                    result["name"] = semantic(item)
                    continue
                if key == "requirement_ids":
                    result["requirements"] = [
                        requirement_by_id[str(requirement_id)]
                        for requirement_id in list(item or [])
                        if str(requirement_id) in requirement_by_id
                    ]
                    continue
                result[str(key)] = semantic(item)
            return result
        if isinstance(value, list):
            return [semantic(item) for item in value]
        return value

    return semantic(contract)


def _validate_verification_policy(
    plan: Mapping[str, Any],
    cases: list[VerificationCaseSpec],
    policy: Mapping[str, Any],
    node: AggregateSnapshot,
) -> None:
    tags = {
        str(tag)
        for item in list(plan.get("recorded_results") or [])
        for tag in list(dict(item or {}).get("obligation_tags") or [])
    }
    exceptions = dict(plan.get("policy_exceptions") or {})
    obligations = (
        ("require_focused_tests", "focused_tests"),
        ("require_warning_clean", "warning_clean"),
        ("require_consumer_probe", "consumer_probe"),
        ("require_public_surface_dogfood", "public_surface_dogfood"),
        ("require_platform_probe", "platform_probe"),
    )
    for policy_key, obligation_tag in obligations:
        if not bool(policy.get(policy_key, False)) or obligation_tag in tags:
            continue
        if not str(exceptions.get(obligation_tag) or "").strip():
            raise ValueError(f"VerificationPolicy requires {obligation_tag} evidence or a concrete UNKNOWN reason")
    if (
        bool(policy.get("require_historical_regressions", False))
        and node.payload.get("historical_repair_bill_refs")
        and "historical_regressions" not in tags
    ):
        raise ValueError("VerificationPolicy requires historical RepairBill regressions first")
    if str(policy.get("lsp_policy") or "") == "when_available" and "lsp" not in tags:
        if not str(exceptions.get("lsp") or "").strip():
            raise ValueError("VerificationPolicy requires LSP evidence or policy_exceptions.lsp")
    allowed_obligations = {
        str(item) for item in list(policy.get("allowed_obligations") or []) if str(item)
    }
    unexpected = tags - allowed_obligations if allowed_obligations else set()
    if unexpected:
        raise ValueError(
            "verification evidence exceeds this node's declared scope: "
            + ", ".join(sorted(unexpected))
        )


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


def _defect_kind(
    plan: Mapping[str, Any],
    node: AggregateSnapshot,
    *,
    findings: list[Mapping[str, Any]] | None = None,
) -> DefectKind:
    dominant = dominant_verification_defect_kind(list(findings or []))
    if dominant:
        return DefectKind(dominant)
    raw = str(plan.get("defect_kind") or "").strip()
    if raw:
        return DefectKind(raw)
    if str(node.payload.get("node_kind") or "") == "integration":
        return DefectKind.INTEGRATION
    return DefectKind.MODULE


def _routable_verification_findings(
    findings: list[Mapping[str, Any]],
    case_results: list[VerificationCaseResult],
    *,
    status: VerificationStatus,
) -> list[dict[str, Any]]:
    if status not in {VerificationStatus.FAIL, VerificationStatus.UNKNOWN}:
        return []
    case_ids = {
        item.case_id
        for item in case_results
        if item.status == status
    }
    return [
        dict(item)
        for item in findings
        if str(item.get("case_id") or "") in case_ids
    ]


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
    root = standalone_review_root(runtime_root) / _safe_component(review_id)
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


def _seed_durable_verification_scratch(attempts_root: Path, durable_scratch: Path) -> None:
    """Recover pre-durable verifier probes once, then keep one candidate-bound scratch."""

    durable_scratch.mkdir(parents=True, exist_ok=True)
    if any(path.is_file() for path in durable_scratch.rglob("*")):
        return
    attempts = sorted(
        (path for path in attempts_root.glob("fence-*") if path.is_dir()),
        key=lambda path: path.stat().st_mtime_ns,
        reverse=True,
    )
    source = next(
        (
            path / "review-scratch"
            for path in attempts
            if (path / "review-scratch").is_dir()
            and any(item.is_file() and not item.is_symlink() for item in (path / "review-scratch").rglob("*"))
        ),
        None,
    )
    if source is None:
        return
    files = [item for item in source.rglob("*") if item.is_file() and not item.is_symlink()]
    if sum(item.stat().st_size for item in files) > 5 * 1024 * 1024:
        raise ValueError("legacy verification scratch exceeds 5 MiB recovery budget")
    for source_file in files:
        destination = durable_scratch / source_file.relative_to(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)


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


def _raise_if_workspace_held(workspace: Path, message: str) -> None:
    holders = workspace_process_holders(workspace)
    if holders:
        raise RuntimeError(f"{message}; {format_workspace_process_holders(holders)}")


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
