from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import inspect
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
from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
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
    git_changed_paths,
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
    verifier_session_subject,
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
    review_architecture_skeleton,
)
from pal.minion.v2.task_sources import TASK_SOURCE_BUNDLE_ARTIFACT, validate_task_source_bundle
from pal.minion.v2.skeleton_builder import (
    ARCHITECTURE_SKELETON_CAPABILITIES,
    SKELETON_REVIEW_CAPABILITIES,
    compile_architecture_review_invocation_tool_contract,
)
from pal.minion.v2.swe_verification import (
    SWE_VERIFICATION_CAPABILITIES,
    compile_swe_verification_tool_contract,
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
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.machines import machine_spec_for
from pal.minion.v2.review_findings import structured_findings
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
    historical_repair_checklist_items,
    repair_bill_semantic_view,
    repair_checklist_items,
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
from pal.minion.v2.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.minion.v2.semantic_orchestration.architecture import ARCHITECTURE_EFFECT_ROUTES
from pal.minion.v2.semantic_orchestration.contracts import (
    SemanticEffectRoute,
    merge_effect_routes,
)
from pal.minion.v2.semantic_orchestration.implementation import IMPLEMENTATION_EFFECT_ROUTES
from pal.minion.v2.semantic_orchestration.review import REVIEW_EFFECT_ROUTES
from pal.minion.v2.semantic_orchestration.verification import VERIFICATION_EFFECT_ROUTES
from pal.minion.ipc import ROLE_GATEWAY_TOKEN_ENV
from pal.minion.v2.role_gateway import role_submission_artifact_type
from pal.minion.v2.role_protocol import (
    RoleAssignmentRequest,
    RoleAssignmentState,
    stable_hash,
)
from pal.shared import MinionInvocationPack


HumanReviewPublisher = Callable[[Mapping[str, Any]], Awaitable[None]]
WorkerEventPublisher = Callable[[Mapping[str, Any]], Awaitable[None]]
BrokerRunRegistrar = Callable[[str, str, MinionInvocationPack, asyncio.subprocess.Process], None]
BrokerRunUnregistrar = Callable[[str], None]


EPHEMERAL_ROLE_INPUT_NAMES = frozenset({"workspace_preparation"})
_DURABLE_WORKSPACE_WRITER_ROLES = frozenset({"architect", "implementation"})


def _role_input_is_semantic(name: str, *, role: str, mode: str = "") -> bool:
    return (
        str(name) not in EPHEMERAL_ROLE_INPUT_NAMES
        or str(role or "").strip() == OrchestrationRole.VERIFIER.value
        or (
            str(role or "").strip() == OrchestrationRole.REVIEWER.value
            and str(mode or "").strip() == RoleMode.STANDALONE.value
        )
    )


def _semantic_role_input_refs(
    input_refs: Mapping[str, Mapping[str, Any]],
    *,
    role: str = "",
    mode: str = "",
) -> dict[str, dict[str, Any]]:
    return {
        str(name): dict(ref)
        for name, ref in sorted(input_refs.items())
        if _role_input_is_semantic(str(name), role=role, mode=mode)
    }


def _assignment_role_input_refs(
    input_refs: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return the complete input identity for one concrete assignment request."""

    return {
        str(name): dict(ref)
        for name, ref in sorted(input_refs.items())
    }


def _durable_workspace_preparation(
    preparation: Mapping[str, Any],
) -> dict[str, Any]:
    durable = dict(preparation or {})
    raw_lsp = durable.get("lsp_workspace_preparation")
    if isinstance(raw_lsp, Mapping):
        lsp = dict(raw_lsp)
        # These describe this RPC observation, not the prepared environment.
        # Keeping them in a content-addressed worker input breaks outbox replay.
        lsp.pop("prepared_at", None)
        lsp.pop("environment_changed", None)
        durable["lsp_workspace_preparation"] = lsp
    return durable


def _candidate_tree_fingerprint(
    candidate: Mapping[str, Any],
    *,
    fallback: str,
) -> str:
    return str(
        candidate.get("candidate_tree_sha")
        or candidate.get("tree_fingerprint")
        or fallback
    )


def _verifier_reference_refs(
    *,
    artifacts: ContentAddressedArtifactStore,
    node_payload: Mapping[str, Any],
    module_work_view_ref: ArtifactRef,
    candidate_diff_ref: ArtifactRef,
) -> dict[str, ArtifactRef]:
    references = {
        "module_work_view": module_work_view_ref,
        "candidate_diff": candidate_diff_ref,
    }
    architecture_ref_value = node_payload.get("architecture_manifest_ref")
    if not isinstance(architecture_ref_value, Mapping) or not architecture_ref_value.get("sha256"):
        return references
    architecture_manifest = artifacts.read_json(_ref_from_mapping(architecture_ref_value))
    requirements_value = architecture_manifest.get("requirements_ref")
    if not isinstance(requirements_value, Mapping) or not requirements_value.get("sha256"):
        return references
    requirements_source_ref = _ref_from_mapping(requirements_value)
    references["task"] = requirements_source_ref
    return references


def _publish_git_diff_artifact(
    *,
    artifacts: ContentAddressedArtifactStore,
    worktree: Path,
    base: str,
    target: str,
    paths: list[str],
    title: str,
    artifact_type: str,
    child_refs: tuple[tuple[str, str], ...],
) -> ArtifactRef:
    if not base or not target:
        raise ValueError(f"{title} requires complete Git provenance")
    command = [
        "git",
        "-C",
        str(worktree),
        "diff",
        "--find-renames",
        "--no-ext-diff",
        "--no-color",
        base,
        target,
        "--",
        *sorted(dict.fromkeys(str(path) for path in paths if str(path).strip())),
    ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or f"failed to create {title}")
    body = completed.stdout or "(no changes in this bound path set)\n"
    return artifacts.put_bytes(
        (title.rstrip() + "\n\n" + body).encode("utf-8"),
        artifact_type=artifact_type,
        media_type="text/x-diff",
        provenance={"owner": "manager", "audience": "verifier"},
        child_refs=child_refs,
    )


def _module_verifier_git_diff_refs(
    *,
    artifacts: ContentAddressedArtifactStore,
    node_payload: Mapping[str, Any],
    candidate: Mapping[str, Any],
    candidate_ref: ArtifactRef,
    candidate_digest: str,
    review_worktree: Path,
) -> dict[str, ArtifactRef]:
    architecture_value = node_payload.get("architecture_manifest_ref")
    if not isinstance(architecture_value, Mapping) or not architecture_value.get("sha256"):
        raise ValueError("module verifier requires the accepted architecture skeleton")
    architecture_ref = _ref_from_mapping(architecture_value)
    architecture = artifacts.read_json(architecture_ref)
    skeleton_sha = str(architecture.get("skeleton_commit_sha") or "")
    changed_paths = [str(item) for item in list(candidate.get("changed_paths") or [])]
    child_refs = (
        (candidate_ref.sha256, "candidate"),
        (architecture_ref.sha256, "accepted_skeleton"),
    )
    refs = {
        "candidate_diff": _publish_git_diff_artifact(
            artifacts=artifacts,
            worktree=review_worktree,
            base=skeleton_sha,
            target=candidate_digest,
            paths=changed_paths,
            title="Accepted Skeleton -> current Candidate (module-owned paths)",
            artifact_type="ModuleCandidateGitDiffArtifact",
            child_refs=child_refs,
        )
    }
    path_policy = dict(node_payload.get("path_policy") or {})
    if str(path_policy.get("contract_mode") or "file_frozen") == "review_guarded":
        refs["contract_diff"] = _publish_git_diff_artifact(
            artifacts=artifacts,
            worktree=review_worktree,
            base=skeleton_sha,
            target=candidate_digest,
            paths=[str(item) for item in list(path_policy.get("contract_paths") or [])],
            title=(
                "Accepted contract shape -> current Candidate. Reject semantic API, ownership, "
                "lifecycle, state, invariant, error, or compatibility drift."
            ),
            artifact_type="ReviewGuardedContractGitDiffArtifact",
            child_refs=child_refs,
        )
    previous_candidate = str(
        candidate.get("parent_candidate_digest") or candidate.get("previous_head_sha") or ""
    )
    if str(candidate.get("parent_candidate_digest") or "") and previous_candidate != candidate_digest:
        refs["repair_diff"] = _publish_git_diff_artifact(
            artifacts=artifacts,
            worktree=review_worktree,
            base=previous_candidate,
            target=candidate_digest,
            paths=changed_paths,
            title="Previous Candidate -> repaired Candidate (this repair delta)",
            artifact_type="ModuleRepairGitDiffArtifact",
            child_refs=child_refs,
        )
    return refs


def _role_session_scope(
    snapshot: AggregateSnapshot,
    activation: RoleActivation,
) -> tuple[str, str]:
    if activation.role == OrchestrationRole.IMPLEMENTATION:
        return "module_run", str(snapshot.aggregate_id)
    if activation.role == OrchestrationRole.VERIFIER:
        subject = verifier_session_subject(snapshot.payload)
        return ("scenario" if subject.startswith("scenario:") else "candidate"), subject
    return snapshot.aggregate_type.value, str(snapshot.aggregate_id)


def _role_uses_bound_durable_workspace(
    role: str,
    workspace: Mapping[str, Any],
) -> bool:
    if str(role or "").strip() not in _DURABLE_WORKSPACE_WRITER_ROLES:
        return False
    repo_path = str(workspace.get("repo_path") or workspace.get("workspace_path") or "").strip()
    mode = str(dict(workspace.get("workspace_policy") or {}).get("mode") or "").strip().lower()
    return bool(repo_path) and mode != "read_only_repo"


def _prepare_role_workspace_before_environment(
    runtime_root: Path,
    workspace: Mapping[str, Any],
    *,
    role: str,
    invocation_id: str,
    run_id: str,
    fencing_token: int,
    prepare_workspace: bool,
) -> tuple[dict[str, Any], bool]:
    prepared = dict(workspace or {})
    uses_bound_durable_workspace = _role_uses_bound_durable_workspace(role, prepared)
    if not prepare_workspace or uses_bound_durable_workspace:
        return prepared, uses_bound_durable_workspace
    role_pack = prepare_v2_role_workspace(
        runtime_root,
        MinionInvocationPack(
            invocation_id=invocation_id,
            workspace=prepared,
        ),
        run_id=run_id,
        attempt_key=f"fence-{fencing_token}",
    )
    return dict(role_pack.workspace), uses_bound_durable_workspace


def _role_submission_kind(activation: RoleActivation, *, skeleton_mode: bool) -> str:
    if activation.role == OrchestrationRole.ARCHITECT:
        return "architecture" if skeleton_mode else "contract"
    if activation.role == OrchestrationRole.REVIEWER:
        return (
            "architecture_review"
            if activation.mode == RoleMode.ARCHITECTURE
            else "standalone_review"
        )
    if activation.role == OrchestrationRole.IMPLEMENTATION:
        return "candidate"
    if activation.role == OrchestrationRole.VERIFIER:
        return "verification"
    raise ValueError(f"unsupported role activation: {activation.to_dict()}")


def _architecture_submit_idempotency_key(
    architecture_revision_id: str,
    source_version: int,
    submission_sha: str,
) -> str:
    return (
        f"architect-submit:{architecture_revision_id}:"
        f"v{int(source_version)}:{submission_sha}"
    )


def _implementation_action_idempotency_key(
    action: str,
    node_run_id: str,
    candidate_cycle: int,
    report_sha: str,
) -> str:
    return (
        f"producer-{str(action).strip()}:{node_run_id}:"
        f"cycle-{int(candidate_cycle)}:{report_sha}"
    )


CONTROL_EFFECT_ROUTES = {
    "pause_role": SemanticEffectRoute("_pause_role"),
    "cancel_role": SemanticEffectRoute("_cancel_role"),
    "quiesce_role_for_triage": SemanticEffectRoute("_quiesce_role_for_triage"),
    "resume_semantic_state": SemanticEffectRoute("_resume_semantic_state"),
    "reconcile_semantic_state": SemanticEffectRoute("_reconcile_semantic_state"),
}

SEMANTIC_EFFECT_ROUTES = merge_effect_routes(
    ARCHITECTURE_EFFECT_ROUTES,
    REVIEW_EFFECT_ROUTES,
    IMPLEMENTATION_EFFECT_ROUTES,
    VERIFICATION_EFFECT_ROUTES,
    CONTROL_EFFECT_ROUTES,
)
SEMANTIC_EFFECT_TYPES = frozenset(SEMANTIC_EFFECT_ROUTES)


@dataclass
class SemanticOrchestrator:
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

    def _record_role_turn(
        self,
        *,
        terminal: Mapping[str, Any],
        **kwargs: Any,
    ) -> None:
        if bool(dict(terminal.get("payload") or {}).get("durable_receipt_replay")):
            return
        self.repository.record_role_turn(**kwargs)

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
        route = SEMANTIC_EFFECT_ROUTES.get(effect_type)
        if route is None:
            raise RuntimeError(f"semantic effect is not implemented: {effect_type}")
        mode = self._effect_role_mode(effect)
        if mode and route.modes and RoleMode(mode) not in route.modes:
            raise ValueError(f"{effect_type} does not support role mode {mode}")
        handler = getattr(self, route.handler)
        if route.background:
            return await self._launch_background_worker(effect, handler)
        result = handler(effect)
        if inspect.isawaitable(result):
            result = await result
        return dict(result)

    def _admit_implementation_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        mode = RoleMode(self._effect_role_mode(effect))
        if mode == RoleMode.PRODUCE:
            return self._admit_node_worker(
                effect,
                action_type="START_PRODUCING",
                activation=RoleActivation(
                    OrchestrationRole.IMPLEMENTATION,
                    RoleMode.PRODUCE,
                ),
            )
        if mode == RoleMode.REPAIR:
            node = self._effect_snapshot(effect)
            repair_inputs = self._install_verifier_tests_for_repair(node)
            return self._admit_node_worker(
                effect,
                action_type="START_REPAIR",
                activation=RoleActivation(
                    OrchestrationRole.IMPLEMENTATION,
                    RoleMode.REPAIR,
                ),
                extra_payload=repair_inputs,
            )
        raise ValueError(f"unsupported implementation mode: {mode.value}")

    async def _run_implementation_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        mode = RoleMode(self._effect_role_mode(effect))
        return await self._run_implementation(effect, repair=mode == RoleMode.REPAIR)

    def _admit_verifier_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        mode = RoleMode(self._effect_role_mode(effect))
        if mode == RoleMode.MODULE:
            return self._admit_node_worker(
                effect,
                action_type="START_REVIEW",
                activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
            )
        if mode == RoleMode.SCENARIO:
            return self._admit_node_worker(
                effect,
                action_type="START_SCENARIO_VERIFICATION",
                activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.SCENARIO),
            )
        raise ValueError(f"unsupported verifier mode: {mode.value}")

    async def _run_verification_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        mode = RoleMode(self._effect_role_mode(effect))
        return await self._run_verification(effect, scenario_mode=mode == RoleMode.SCENARIO)

    def _admit_reviewer_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if RoleMode(self._effect_role_mode(effect)) != RoleMode.STANDALONE:
            raise ValueError("only standalone review has a separate admission step")
        return self._admit_standalone_review(effect)

    async def _run_reviewer_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        mode = RoleMode(self._effect_role_mode(effect))
        if mode == RoleMode.ARCHITECTURE:
            return await self._run_architecture_review(effect)
        if mode == RoleMode.STANDALONE:
            return await self._run_standalone_review(effect)
        raise ValueError(f"unsupported reviewer mode: {mode.value}")

    async def _pause_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(effect.get("aggregate_type") or "") == AggregateType.DAG_NODE_RUN.value:
            return await self._stop_node_worker(effect, cancel=False)
        return await self._stop_aggregate_worker(effect, cancel=False)

    async def _cancel_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(effect.get("aggregate_type") or "") == AggregateType.DAG_NODE_RUN.value:
            return await self._stop_node_worker(effect, cancel=True)
        return await self._stop_aggregate_worker(effect, cancel=True)

    async def _quiesce_role_for_triage(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(effect.get("aggregate_type") or "") == AggregateType.DAG_NODE_RUN.value:
            return await self._stop_node_worker(effect, cancel=False, confirm=False)
        return await self._stop_aggregate_worker(effect, cancel=False, confirm=False)

    async def _resume_semantic_state(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(effect.get("aggregate_type") or "") == AggregateType.DAG_NODE_RUN.value:
            return await self._resume_node(effect)
        return await self._resume_aggregate(effect)

    async def _reconcile_semantic_state(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(effect.get("aggregate_type") or "") == AggregateType.DAG_NODE_RUN.value:
            return await self._reconcile_node(effect)
        return await self._resume_aggregate(effect)

    @staticmethod
    def _effect_role_mode(effect: Mapping[str, Any]) -> str:
        return str(
            effect.get("role_mode")
            or dict(effect.get("payload") or {}).get("role_mode")
            or ""
        )

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
        raise RuntimeError("role assignment was not durably created within 120 seconds")

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
                assignment = self.repository.read_role_assignment(assignment_id)
                if assignment is not None:
                    disposition = self._role_assignment_disposition(effect, assignment)
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
                    assignment = self.repository.read_role_assignment(assignment_id)
                    if (
                        assignment is not None
                        and assignment["state"]
                        == RoleAssignmentState.RESULT_RECORDED.value
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
                assignment = self.repository.read_role_assignment(assignment_id)
                if assignment is None:
                    raise
                disposition = self._role_assignment_disposition(effect, assignment)
                if disposition:
                    self._release_background_business_lease(effect)
                    return {
                        "provider_request_id": assignment_id,
                        "status": disposition,
                    }
                permanent = isinstance(exc, PermanentEffectError)
                if assignment["state"] in {
                    RoleAssignmentState.CLAIMED.value,
                    RoleAssignmentState.RUNNING.value,
                } and not permanent:
                    assignment = self._queue_active_assignment_retry(
                        assignment,
                        error_kind="worker_supervisor_failure",
                        error_text=f"{exc.__class__.__name__}: {exc}",
                    )
                attempts = self.repository.list_role_attempts(assignment_id)
                if self._stopping:
                    self._release_background_business_lease(effect)
                    return {
                        "provider_request_id": assignment_id,
                        "status": "suspended",
                    }
                if (
                    assignment["state"] in {
                        RoleAssignmentState.QUEUED.value,
                        RoleAssignmentState.RETRY_QUEUED.value,
                        RoleAssignmentState.RESULT_RECORDED.value,
                        RoleAssignmentState.SETTLED.value,
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
                return self._settle_background_role_failure(
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
        attempt = self.repository.read_role_attempt(attempt_id_value)
        if attempt is None:
            return dict(assignment)
        lease_resource = str(attempt.get("lease_resource_key") or "")
        fencing_token = int(attempt.get("fencing_token") or 0)
        updated = self.repository.queue_role_attempt_retry(
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
        assignment = self.repository.read_role_assignment(assignment_id)
        if assignment is None or assignment["state"] not in {
            RoleAssignmentState.CLAIMED.value,
            RoleAssignmentState.RUNNING.value,
        }:
            return
        self._queue_active_assignment_retry(
            assignment,
            error_kind="manager_shutdown",
            error_text="manager stopped before the role assignment settled",
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

    def _role_assignment_disposition(
        self,
        effect: Mapping[str, Any],
        assignment: Mapping[str, Any],
    ) -> str:
        assignment_state = str(assignment.get("state") or "")
        if assignment_state == RoleAssignmentState.CANCELLED.value:
            return "cancelled"
        route = SEMANTIC_EFFECT_ROUTES.get(str(effect.get("effect_type") or ""))
        aggregate_type_value = str(assignment.get("aggregate_type") or "")
        expected_states: set[str] = set()
        if route is not None and route.role is not None:
            modes = route.modes
            effect_mode = self._effect_role_mode(effect)
            if effect_mode:
                modes = frozenset({RoleMode(effect_mode)})
            try:
                machine = machine_spec_for(AggregateType(aggregate_type_value))
            except ValueError:
                machine = None
            if machine is not None:
                for mode in modes:
                    expected_states.update(
                        machine.states_for_activation(RoleActivation(route.role, mode))
                    )
        if expected_states:
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
            if snapshot.state not in expected_states:
                if snapshot.state in {"PAUSE_REQUESTED", "PAUSED"}:
                    return "suspended"
                if snapshot.state in {"CANCEL_REQUESTED", "CANCELLED", "STALE"}:
                    return "cancelled"
                return "settled" if assignment_state == RoleAssignmentState.SETTLED.value else "superseded"
        elif assignment_state == RoleAssignmentState.SETTLED.value:
            return "settled"
        reusable = self._reusable_role_assignment(
            workflow_id=str(assignment.get("workflow_id") or ""),
            aggregate_type=str(assignment.get("aggregate_type") or ""),
            aggregate_id=str(assignment.get("aggregate_id") or ""),
            role=str(assignment.get("role") or ""),
            mode=str(assignment.get("mode") or ""),
            submission_kind=str(assignment.get("submission_kind") or ""),
            input_refs=dict(assignment.get("input_refs") or {}),
            exclude_assignment_id=str(assignment.get("assignment_id") or ""),
        )
        if reusable is not None:
            return "superseded by an equivalent durable submission"
        return ""

    def _reusable_role_assignment(
        self,
        *,
        workflow_id: str,
        aggregate_type: str,
        aggregate_id: str,
        role: str,
        mode: str,
        submission_kind: str,
        input_refs: Mapping[str, Mapping[str, Any]],
        exclude_assignment_id: str = "",
    ) -> dict[str, Any] | None:
        semantic_inputs = _semantic_role_input_refs(input_refs, role=role, mode=mode)
        expected_artifact_type = role_submission_artifact_type(submission_kind)
        if not expected_artifact_type:
            return None
        candidates = self.repository.list_role_assignments(workflow_id=workflow_id)
        for candidate in reversed(candidates):
            if str(candidate.get("assignment_id") or "") == exclude_assignment_id:
                continue
            if str(candidate.get("aggregate_type") or "") != aggregate_type:
                continue
            if str(candidate.get("aggregate_id") or "") != aggregate_id:
                continue
            if str(candidate.get("role") or "") != role:
                continue
            if str(candidate.get("mode") or "") != mode:
                continue
            if str(candidate.get("submission_kind") or "") != submission_kind:
                continue
            if str(candidate.get("state") or "") not in {
                RoleAssignmentState.RESULT_RECORDED.value,
                RoleAssignmentState.SETTLED.value,
            }:
                continue
            artifact_ref = dict(candidate.get("submission_artifact_ref") or {})
            if str(artifact_ref.get("artifact_type") or "") != expected_artifact_type:
                continue
            if (
                _semantic_role_input_refs(
                    dict(candidate.get("input_refs") or {}),
                    role=role,
                    mode=mode,
                )
                == semantic_inputs
            ):
                return dict(candidate)
        return None

    def _role_submission_settlement(
        self,
        effect: Mapping[str, Any],
        *,
        required: bool = True,
    ) -> dict[str, str]:
        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "")
        assignment_id = self._assignment_ids_by_effect.get(effect_key, "")
        if not assignment_id:
            return {}
        assignment = self.repository.read_role_assignment(assignment_id)
        if assignment is None:
            raise SubmissionInvariantError("role assignment disappeared before settlement")
        if assignment["state"] not in {
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        }:
            if not required:
                return {}
            raise SubmissionInvariantError(
                "role business action requires a durable submission receipt"
            )
        payload_hash = str(assignment.get("submission_payload_hash") or "")
        if not payload_hash:
            raise SubmissionInvariantError(
                "role assignment submission receipt has no payload hash"
            )
        return {
            "role_assignment_id": assignment_id,
            "role_submission_payload_hash": payload_hash,
        }

    def _release_background_business_lease(self, effect: Mapping[str, Any]) -> None:
        try:
            snapshot = self._effect_snapshot(effect)
            if snapshot.state in {
                "REVIEW_QUIESCING",
                "REVIEW_SNAPSHOTTING",
                "VERIFY_QUIESCING",
                "VERIFY_SNAPSHOTTING",
            }:
                return
            resource = str(snapshot.payload.get("lease_resource_key") or "")
            owner = str(snapshot.payload.get("active_worker_id") or "")
            token = int(snapshot.payload.get("fencing_token") or 0)
            if resource and owner and token:
                self.repository.release_lease(resource, owner, token)
        except Exception:
            return

    def _settle_background_role_failure(
        self,
        effect: Mapping[str, Any],
        assignment: Mapping[str, Any],
        error: Exception,
        *,
        exhausted: bool,
    ) -> Mapping[str, Any]:
        assignment_id = str(assignment["assignment_id"])
        attempts = self.repository.list_role_attempts(assignment_id)
        error_text = f"{error.__class__.__name__}: {error}"
        failure_payload = {
            "kind": "role_assignment_failed",
            "role": str(assignment.get("role") or ""),
            "attempt_count": len(attempts),
            "exhausted": bool(exhausted),
            "error_kind": (
                "attempt_budget_exhausted"
                if exhausted
                else "permanent_role_failure"
            ),
            "error": error_text,
            "effect_type": str(effect.get("effect_type") or ""),
        }
        failure_ref = self.service.artifacts.put_json(
            failure_payload,
            artifact_type="RoleAssignmentFailureArtifact",
        )
        current_assignment = self.repository.read_role_assignment(assignment_id)
        if current_assignment is None:
            raise SubmissionInvariantError("role assignment disappeared before failure settlement")
        if current_assignment["state"] not in {
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        }:
            if not attempts:
                raise SubmissionInvariantError(
                    "role assignment failed before an attempt was durably claimed"
                )
            self.repository.record_role_failure_result(
                assignment_id=assignment_id,
                attempt_id_value=str(attempts[-1]["attempt_id"]),
                error_kind=str(failure_payload["error_kind"]),
                error_text=error_text,
                failure_artifact_ref=failure_ref.to_dict(),
                payload_hash=stable_hash(failure_payload),
                settlement_action={
                    "action_type": "ROLE_FAILED",
                    "aggregate_type": str(current_assignment["aggregate_type"]),
                    "aggregate_id": str(current_assignment["aggregate_id"]),
                },
            )
            current_assignment = self.repository.read_role_assignment(assignment_id)
        if current_assignment is None:
            raise SubmissionInvariantError("role failure receipt was not durable")
        for _attempt in range(3):
            snapshot = self._effect_snapshot(effect)
            legal = self.repository.engine.legal_actions(
                snapshot.aggregate_type,
                snapshot.state,
            )
            if "ROLE_FAILED" not in legal:
                if snapshot.state == "TRIAGE_REQUIRED":
                    self.repository.settle_role_assignment(
                        assignment_id=assignment_id,
                        submission_payload_hash=str(
                            current_assignment["submission_payload_hash"]
                        ),
                    )
                    return {
                        "provider_request_id": assignment_id,
                        "status": "triage_required",
                    }
                self.repository.cancel_role_assignments(
                    workflow_id=str(current_assignment["workflow_id"]),
                    aggregate_type=str(current_assignment["aggregate_type"]),
                    aggregate_id=str(current_assignment["aggregate_id"]),
                    reason=f"role failure superseded by parent state {snapshot.state}",
                )
                return {
                    "provider_request_id": assignment_id,
                    "status": "superseded",
                }
            try:
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="ROLE_FAILED",
                        workflow_id=snapshot.workflow_id,
                        aggregate_type=snapshot.aggregate_type,
                        aggregate_id=snapshot.aggregate_id,
                        actor="minion-v2-worker-supervisor",
                        expected_version=snapshot.version,
                        idempotency_key=f"worker-failed:{assignment_id}",
                        payload={
                            "failure_artifact_ref": failure_ref.to_dict(),
                            "blocker": {
                                "kind": "role_failure",
                                "summary": error_text,
                                "role": str(current_assignment.get("role") or ""),
                                "attempt_count": len(attempts),
                            },
                        },
                    ),
                    role_assignment_id=assignment_id,
                    role_submission_payload_hash=str(
                        current_assignment["submission_payload_hash"]
                    ),
                )
                return {
                    "provider_request_id": assignment_id,
                    "status": "triage_required",
                }
            except AggregateVersionConflict:
                continue
        raise DeferredEffectError("role failure receipt settlement lost repeated CAS races")

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
            self.repository.list_role_assignments(
            states=(
                RoleAssignmentState.QUEUED.value,
                RoleAssignmentState.CLAIMED.value,
                RoleAssignmentState.RUNNING.value,
                RoleAssignmentState.RETRY_QUEUED.value,
                RoleAssignmentState.RESULT_RECORDED.value,
            )
            ),
            key=lambda item: (
                {
                    RoleAssignmentState.RESULT_RECORDED.value: 0,
                    RoleAssignmentState.RUNNING.value: 1,
                    RoleAssignmentState.CLAIMED.value: 1,
                    RoleAssignmentState.RETRY_QUEUED.value: 2,
                    RoleAssignmentState.QUEUED.value: 3,
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
            if self._role_assignment_attempt_is_live(assignment):
                # Another supervisor still owns the process attempt. This is
                # especially important for reconcile effects, which may run a
                # semantic worker inline rather than through _background_workers.
                # Recovery may take over only after the durable attempt lease
                # expires; otherwise two supervisors can race at submission.
                continue
            disposition = self._role_assignment_disposition(effect, assignment)
            if disposition:
                self.repository.cancel_role_assignments(
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

    def _role_assignment_attempt_is_live(
        self,
        assignment: Mapping[str, Any],
    ) -> bool:
        attempt_id_value = str(assignment.get("active_attempt_id") or "")
        if not attempt_id_value:
            return False
        attempt = self.repository.read_role_attempt(attempt_id_value)
        if attempt is None:
            return False
        resource = str(attempt.get("lease_resource_key") or "")
        token = int(attempt.get("fencing_token") or 0)
        if not resource or token <= 0:
            return False
        lease = self.repository.read_lease(resource)
        return bool(
            lease is not None
            and str(lease.get("owner_id") or "") == attempt_id_value
            and int(lease.get("fencing_token") or 0) == token
            and _lease_is_live(lease)
        )

    def _runner_for_recovered_effect(
        self,
        effect: Mapping[str, Any],
    ) -> Callable[[Mapping[str, Any]], Awaitable[Mapping[str, Any]]] | None:
        route = SEMANTIC_EFFECT_ROUTES.get(str(effect.get("effect_type") or ""))
        if route is None or not route.background:
            return None
        return getattr(self, route.handler)

    def _admit_node_worker(
        self,
        effect: Mapping[str, Any],
        *,
        action_type: str,
        activation: RoleActivation,
        extra_payload: Mapping[str, Any] | None = None,
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
        implementation = activation.role == OrchestrationRole.IMPLEMENTATION
        cycle = int(node.payload.get("candidate_cycle") or 0) + (1 if implementation else 0)
        generation = node_role_generation(node.payload)
        invocation_id = (
            coder_session_id(node.aggregate_id, generation)
            if implementation
            else verifier_session_id(
                node.aggregate_id,
                verifier_session_subject(node.payload),
                generation,
            )
        )
        lease_resource = f"node:{node.aggregate_id}:{'writer' if implementation else 'review'}"
        lease = self.repository.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": node.workflow_id,
                "node_run_id": node.aggregate_id,
                **activation.to_dict(),
            },
        )
        payload = {
            "fencing_token": lease.fencing_token,
            "active_worker_id": invocation_id,
            "lease_resource_key": lease_resource,
            "active_role": activation.role.value,
            "active_role_mode": activation.mode.value,
            **dict(extra_payload or {}),
        }
        if implementation:
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

    def _install_verifier_tests_for_repair(
        self,
        node: AggregateSnapshot,
    ) -> dict[str, Any]:
        repair_ref_value = dict(node.payload.get("repair_bill_ref") or {})
        if not repair_ref_value.get("sha256"):
            return {}
        repair = dict(self.service.artifacts.read_json(repair_ref_value))
        if str(repair.get("artifact_kind") or "") != "semantic_repair_packet":
            return {}
        test_delta_value = dict(repair.get("test_delta_ref") or {})
        if not test_delta_value.get("sha256"):
            raise SubmissionInvariantError(
                "semantic Repair Packet is missing its verifier test delta"
            )
        if dict(node.payload.get("verifier_test_delta_ref") or {}) == test_delta_value:
            return {
                "verifier_test_delta_ref": test_delta_value,
                "verifier_test_paths": list(node.payload.get("verifier_test_paths") or []),
            }
        test_delta_ref = ArtifactRef.from_mapping(test_delta_value)
        test_delta = dict(self.service.artifacts.read_json(test_delta_ref))
        declared_paths = [
            str(item)
            for item in list(
                test_delta.get("changed_paths")
                or repair.get("changed_test_paths")
                or []
            )
            if str(item).strip()
        ]
        test_scopes = [
            dict(item or {})
            for item in list(
                dict(node.payload.get("path_policy") or {}).get("test_scopes") or []
            )
        ]
        external_paths = [
            path
            for path in declared_paths
            if not any(_semantic_path_scope_matches(path, scope) for scope in test_scopes)
        ]
        if external_paths:
            return {
                "verifier_test_delta_ref": test_delta_ref.to_dict(),
                "external_verifier_test_paths": declared_paths,
                "verifier_test_paths": [],
            }
        workspace = Path(str(node.payload.get("workspace_path") or ""))
        candidate_digest = str(node.payload.get("candidate_digest") or "")
        if not workspace.is_dir() or not candidate_digest:
            raise SubmissionInvariantError(
                "repair worktree or candidate baseline is unavailable"
            )
        head = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if head.returncode != 0 or head.stdout.strip() != candidate_digest:
            raise SubmissionInvariantError(
                "repair worktree is not based on the rejected candidate"
            )
        subprocess.run(
            ["git", "-C", str(workspace), "reset", "--hard", candidate_digest],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        file_entries = [
            dict(item)
            for item in list(test_delta.get("files") or [])
            if isinstance(item, Mapping)
            and str(dict(item).get("path") or "").startswith("worktree/")
        ]
        for item in file_entries:
            relative = str(item.get("path") or "").removeprefix("worktree/")
            target = (workspace / relative).resolve()
            if not target.is_relative_to(workspace.resolve()):
                raise SubmissionInvariantError(
                    f"verifier test path escapes the repair worktree: {relative}"
                )
            if target.exists() and target.is_file():
                target.unlink()
        encoded_patch = str(test_delta.get("workspace_patch_base64") or "")
        patch_bytes = base64.b64decode(encoded_patch, validate=True) if encoded_patch else b""
        if patch_bytes:
            applied = subprocess.run(
                ["git", "-C", str(workspace), "apply", "--binary", "-"],
                input=patch_bytes,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if applied.returncode != 0:
                raise SubmissionInvariantError(
                    "failed to install verifier regression patch: "
                    + applied.stderr.decode("utf-8", errors="replace")[-4000:]
                )
        for item in file_entries:
            relative = str(item.get("path") or "").removeprefix("worktree/")
            raw = base64.b64decode(str(item.get("content_base64") or ""), validate=True)
            if hashlib.sha256(raw).hexdigest() != str(item.get("sha256") or ""):
                raise SubmissionInvariantError(
                    f"verifier test artifact hash mismatch: {relative}"
                )
            target = workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(raw)
        changed_paths = _verification_workspace_changed_paths(
            workspace,
            candidate_digest,
        )
        outside = [
            path
            for path in changed_paths
            if not any(_semantic_path_scope_matches(path, scope) for scope in test_scopes)
        ]
        if outside:
            subprocess.run(
                ["git", "-C", str(workspace), "reset", "--hard", candidate_digest],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            raise SubmissionInvariantError(
                "verifier Repair Packet changed paths outside test scopes: "
                + ", ".join(outside)
            )
        if not changed_paths:
            raise SubmissionInvariantError(
                "semantic Repair Packet installed no verifier regression tests"
            )
        return {
            "verifier_test_delta_ref": test_delta_ref.to_dict(),
            "verifier_test_paths": changed_paths,
        }

    def _promote_verifier_tests(
        self,
        *,
        node: AggregateSnapshot,
        review_workspace: Path,
        candidate_ref: ArtifactRef,
        candidate: Mapping[str, Any],
        candidate_digest: str,
        test_delta_ref: ArtifactRef,
        changed_test_paths: list[str],
    ) -> tuple[ArtifactRef, str, dict[str, Any]]:
        if self._execution_adapter(node) != SOFTWARE_GIT_ADAPTER:
            raise SubmissionInvariantError(
                "semantic verifier test promotion currently requires the software Git adapter"
            )
        if not changed_test_paths:
            raise SubmissionInvariantError("PASS has no verifier test delta to promote")
        # Include untracked verifier tests in the Manager-owned tree without
        # granting the verifier Git write authority.
        subprocess.run(
            ["git", "-C", str(review_workspace), "add", "-A", "--"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        tree = subprocess.run(
            ["git", "-C", str(review_workspace), "write-tree"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        baseline_sha = str(candidate.get("base_sha") or "")
        if not baseline_sha:
            raise SubmissionInvariantError(
                "verifier test promotion requires the Candidate baseline SHA"
            )
        promotion_key = hashlib.sha256(
            f"squashed-v2:{candidate_digest}:{test_delta_ref.sha256}:{tree}".encode("utf-8")
        ).hexdigest()
        ref_name = f"refs/pal/verifier-tests/{promotion_key}"
        existing = subprocess.run(
            ["git", "-C", str(review_workspace), "rev-parse", "--verify", ref_name],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        if existing.returncode == 0:
            promoted_digest = existing.stdout.strip()
        else:
            promoted_digest = subprocess.run(
                [
                    "git",
                    "-C",
                    str(review_workspace),
                    "-c",
                    "user.name=Pal Minion Verifier",
                    "-c",
                    "user.email=minion-verifier@localhost",
                    "commit-tree",
                    tree,
                    "-p",
                    baseline_sha,
                    "-m",
                    (
                        f"promote verifier tests for {node.aggregate_id}\n\n"
                        f"Pal-Verification-Key: {promotion_key}"
                    ),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(review_workspace),
                    "update-ref",
                    ref_name,
                    promoted_digest,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
        self._publish_promoted_candidate_to_node_workspace(
            node=node,
            review_workspace=review_workspace,
            source_ref=ref_name,
            candidate_digest=candidate_digest,
            promoted_digest=promoted_digest,
        )
        delta_patch = subprocess.run(
            [
                "git",
                "-C",
                str(review_workspace),
                "diff",
                "--binary",
                baseline_sha,
                promoted_digest,
                "--",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
        promoted = {
            **dict(candidate),
            "candidate_digest": promoted_digest,
            "previous_head_sha": candidate_digest,
            "candidate_tree_sha": tree,
            "delta_patch_sha": hashlib.sha256(delta_patch).hexdigest(),
            "changed_paths": sorted(
                set(str(item) for item in list(candidate.get("changed_paths") or []))
                | set(changed_test_paths)
            ),
            "verifier_test_delta_ref": test_delta_ref.to_dict(),
            "verifier_test_paths": list(changed_test_paths),
            "candidate_key": promotion_key,
        }
        promoted_ref = self.service.artifacts.put_json(
            promoted,
            artifact_type="CandidateSnapshotArtifact",
            provenance={"owner": "manager", "promotion": "verifier_tests"},
            child_refs=(
                (candidate_ref.sha256, "implementation_candidate"),
                (test_delta_ref.sha256, "verifier_test_delta"),
            ),
        )
        return promoted_ref, promoted_digest, promoted

    def _publish_promoted_candidate_to_node_workspace(
        self,
        *,
        node: AggregateSnapshot,
        review_workspace: Path,
        source_ref: str,
        candidate_digest: str,
        promoted_digest: str,
    ) -> None:
        node_workspace_text = str(node.payload.get("workspace_path") or "")
        node_workspace = Path(node_workspace_text) if node_workspace_text else review_workspace
        if not node_workspace.is_dir():
            raise SubmissionInvariantError(
                "verifier test promotion requires the durable node worktree"
            )
        if node_workspace.resolve() == review_workspace.resolve():
            return

        lock_key = f"verification-promotion:{node.aggregate_id}"
        _raise_if_workspace_held(
            node_workspace,
            "a live process still holds the node worktree during verifier test promotion",
        )
        lock_path = self._worktree_locks.acquire(lock_key, node_workspace)
        try:
            _raise_if_workspace_held(
                node_workspace,
                "a process reached the node worktree during verifier test promotion",
                manager_snapshot_lock=lock_path,
            )
            head = subprocess.run(
                ["git", "-C", str(node_workspace), "rev-parse", "HEAD"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
            if head not in {candidate_digest, promoted_digest}:
                raise SubmissionInvariantError(
                    "node worktree moved away from the verified Candidate before test promotion"
                )
            dirty = subprocess.run(
                ["git", "-C", str(node_workspace), "status", "--porcelain"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
            if dirty:
                raise SubmissionInvariantError(
                    "node worktree changed before verifier test promotion"
                )
            fetched = subprocess.run(
                [
                    "git",
                    "-C",
                    str(node_workspace),
                    "fetch",
                    "--no-tags",
                    str(review_workspace),
                    f"+{source_ref}:{source_ref}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if fetched.returncode != 0:
                raise SubmissionInvariantError(
                    "failed to publish verifier tests into the epoch Git repository: "
                    + (fetched.stderr or fetched.stdout).strip()[-4000:]
                )
            imported = subprocess.run(
                ["git", "-C", str(node_workspace), "rev-parse", "--verify", source_ref],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
            if imported != promoted_digest:
                raise SubmissionInvariantError(
                    "published verifier test ref does not match the promoted Candidate"
                )
            subprocess.run(
                ["git", "-C", str(node_workspace), "reset", "--hard", promoted_digest],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
        finally:
            self._worktree_locks.release(lock_key)

    async def _ensure_node_effect_lease(
        self,
        node: AggregateSnapshot,
        *,
        action_type: str,
        activation: RoleActivation,
    ) -> AggregateSnapshot:
        invocation_id = str(node.payload.get("active_worker_id") or "")
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        writer_role = activation.role == OrchestrationRole.IMPLEMENTATION
        generation = node_role_generation(node.payload)
        expected_invocation_id = (
            coder_session_id(node.aggregate_id, generation)
            if writer_role
            else verifier_session_id(
                node.aggregate_id,
                verifier_session_subject(node.payload),
                generation,
            )
        )
        if invocation_id and invocation_id != expected_invocation_id:
            previous = self.repository.read_lease(lease_resource)
            if previous is not None and _lease_is_live(previous):
                raise DeferredEffectError(
                    "node worker is still fenced by an obsolete logical session"
                )
            invocation_id = ""
            fencing_token = 0
        if invocation_id and lease_resource and fencing_token:
            if await self._reuse_or_retire_effect_lease(
                resource_key=lease_resource,
                owner_id=invocation_id,
                fencing_token=fencing_token,
                worker_label=f"node worker {node.aggregate_id}",
            ):
                await self._ensure_node_snapshot_lock(node)
                return node

        if writer_role:
            invocation_id = expected_invocation_id
            lease_resource = f"node:{node.aggregate_id}:writer"
        else:
            invocation_id = invocation_id or expected_invocation_id
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
                "role": activation.role.value,
                "mode": activation.mode.value,
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
                    "active_role": activation.role.value,
                    "active_role_mode": activation.mode.value,
                },
            )
        ).snapshot
        await self._ensure_node_snapshot_lock(rebound)
        return rebound

    async def _ensure_node_snapshot_lock(self, node: AggregateSnapshot) -> None:
        if node.state != "SNAPSHOTTING" or self._worktree_locks.is_held(node.aggregate_id):
            return
        workspace = Path(str(node.payload.get("workspace_path") or ""))
        await self._release_managed_lsp_workspace(workspace)
        _raise_if_workspace_held(
            workspace,
            "a live process still holds the candidate worktree",
        )
        expected = str(node.payload.get("workspace_fingerprint") or "")
        current = self._workspace_fingerprint(node, workspace)
        if not expected or current != expected:
            raise RuntimeError("candidate worktree changed while snapshot worker was unavailable")
        self._worktree_locks.acquire(node.aggregate_id, workspace)

    def _admit_standalone_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        review = self._effect_snapshot(effect)
        invocation_id = f"inv_{hashlib.sha256(f'{review.aggregate_id}:review'.encode()).hexdigest()[:24]}"
        lease_resource = f"standalone-review:{review.aggregate_id}"
        lease = self.repository.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": review.workflow_id,
                "review_id": review.aggregate_id,
                "role": OrchestrationRole.REVIEWER.value,
                "mode": RoleMode.STANDALONE.value,
            },
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
                    "active_role": OrchestrationRole.REVIEWER.value,
                    "active_role_mode": RoleMode.STANDALONE.value,
                },
            )
        )
        return {"provider_request_id": invocation_id}

    async def _run_implementation(self, effect: Mapping[str, Any], *, repair: bool) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        node = await self._ensure_node_effect_lease(
            node,
            action_type="REBIND_REPAIRER" if repair else "REBIND_PRODUCER",
            activation=RoleActivation(
                OrchestrationRole.IMPLEMENTATION,
                RoleMode.REPAIR if repair else RoleMode.PRODUCE,
            ),
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
        architecture_ref = _ref_from_mapping(node.payload.get("architecture_manifest_ref"))
        architecture_payload = dict(self.service.artifacts.read_json(architecture_ref))
        task_source_value = architecture_payload.get("requirements_ref")
        if isinstance(task_source_value, Mapping) and task_source_value.get("sha256"):
            references["task"] = _ref_from_mapping(task_source_value)
        repair_ref = node.payload.get("repair_bill_ref")
        if isinstance(repair_ref, Mapping) and repair_ref.get("sha256"):
            semantic_repair_view = repair_bill_semantic_view(
                self.service.artifacts,
                repair_ref,
            )
            semantic_repair_view["verifier_tests_are_preinstalled"] = bool(
                node.payload.get("verifier_test_paths")
            )
            semantic_repair_ref = self.service.artifacts.put_json(
                semantic_repair_view,
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
            profile=self._profile_for_role(node.workflow_id, "implementation"),
            activation=RoleActivation(
                OrchestrationRole.IMPLEMENTATION,
                RoleMode.REPAIR if repair else RoleMode.PRODUCE,
            ),
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
                "read_only_overlay_paths": (
                    list(node.payload.get("verifier_test_paths") or [])
                    if repair
                    else []
                ),
                "require_os_path_enforcement": self._is_skeleton_manifest(
                    node.payload.get("architecture_manifest_ref")
                ),
            },
            prepare_workspace=True,
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
        self._record_role_turn(
            terminal=terminal,
            invocation_id=invocation_id,
            fencing_token=fencing_token,
            turn_index=_role_session_turn_index(terminal),
            llm_request_ref=prompt_ref.to_dict(),
            llm_response_ref=terminal_ref.to_dict(),
            tool_summary_ref=report_ref.to_dict(),
            **_recorded_role_metrics(terminal),
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
                    idempotency_key=_implementation_action_idempotency_key(
                        "defect",
                        node.aggregate_id,
                        int(node.payload.get("candidate_cycle") or 0),
                        report_ref.sha256,
                    ),
                    payload={"finding_artifact_ref": report_ref.to_dict()},
                ),
                **self._role_submission_settlement(effect),
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
                idempotency_key=_implementation_action_idempotency_key(
                    "submit",
                    node.aggregate_id,
                    int(node.payload.get("candidate_cycle") or 0),
                    report_ref.sha256,
                ),
                payload={
                    "fencing_token": fencing_token,
                    "producer_report_ref": report_ref.to_dict(),
                    "unit_work_view_ref": view_ref.to_dict(),
                },
            ),
            **self._role_submission_settlement(effect),
        )
        return {"provider_request_id": invocation_id, "result_artifact_ref": report_ref.to_dict()}

    async def _quiesce_node(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        node = await self._ensure_node_effect_lease(
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
                manager_snapshot_lock=lock_path,
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

    async def _snapshot_implementation_result(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        node = await self._ensure_node_effect_lease(
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

    async def _run_verification(
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
            activation=RoleActivation(
                OrchestrationRole.VERIFIER,
                RoleMode.SCENARIO if scenario_mode else RoleMode.MODULE,
            ),
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
        skeleton_manifest = self._is_skeleton_manifest(
            node.payload.get("architecture_manifest_ref")
        )
        candidate_view_ref: ArtifactRef | None = None
        if scenario_mode or adapter != SOFTWARE_GIT_ADAPTER or not skeleton_manifest:
            candidate_view_ref = self.service.artifacts.put_json(
                {
                    "module_name": str(node.payload.get("module_name") or node.payload.get("unit_id") or ""),
                    "node_kind": str(node.payload.get("node_kind") or "unit"),
                    "changed_paths": [str(item) for item in list(candidate.get("changed_paths") or [])],
                    "candidate_cycle": int(node.payload.get("candidate_cycle") or 0),
                    "instruction": (
                        "Verify the exact accepted-module scenario assembled in the bound read-only worktree."
                        if scenario_mode
                        else "Inspect the immutable candidate in the bound review workspace."
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
        # The work view limits this run's scope; the complete immutable ledger
        # prevents an upstream summary from narrowing user intent.
        candidate_diff_ref = candidate_view_ref
        git_diff_refs: dict[str, ArtifactRef] = {}
        if not scenario_mode and adapter == SOFTWARE_GIT_ADAPTER and skeleton_manifest:
            git_diff_refs = _module_verifier_git_diff_refs(
                artifacts=self.service.artifacts,
                node_payload=node.payload,
                candidate=candidate,
                candidate_ref=candidate_ref,
                candidate_digest=candidate_digest,
                review_worktree=review_workspace,
            )
            candidate_diff_ref = git_diff_refs.pop("candidate_diff")
        if candidate_diff_ref is None:
            raise ValueError("verifier requires a bound candidate diff or semantic scenario view")
        verifier_references = _verifier_reference_refs(
            artifacts=self.service.artifacts,
            node_payload=node.payload,
            module_work_view_ref=view_ref,
            candidate_diff_ref=candidate_diff_ref,
        )
        verifier_references.update(git_diff_refs)
        terminal, prompt_ref, terminal_ref = await self._run_profile(
            effect=effect,
            snapshot=node,
            invocation_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            profile=self._profile_for_role(node.workflow_id, "verifier"),
            activation=RoleActivation(
                OrchestrationRole.VERIFIER,
                RoleMode.SCENARIO if scenario_mode else RoleMode.MODULE,
            ),
            instruction=(
                "Generate and run adversarial verification for the bound real usage scenario. Read every immutable task-source file directly, preserve its exact qualifications and examples, and prove only the obligations exercised by this exact module combination, entrypoint, and environment. Write real tests only in any bound scenario test scope, then submit one semantic outcome."
                if scenario_mode
                else "Generate and run adversarial verification for the bound candidate. Read the Manager-bound Candidate diff and any Contract/Repair diff before judging the code. Read every immutable task-source file directly without widening this module's scope. For review_guarded contracts, compare the Accepted Skeleton shape with the Candidate and reject semantic contract drift. Run historical verifier regressions first, then add or strengthen adversarial tests in the bound test scopes and submit one semantic outcome."
            ),
            reference_refs=verifier_references,
            workspace_override={
                "kind": "existing_repo",
                "repo_path": str(review_workspace),
                "project_name": str(node.payload.get("unit_id") or "unit"),
                "review_scratch_dir": str(review_scratch),
                "verification_scenario": scenario_mode,
                "verification_scratch_only": (
                    scenario_mode or adapter != SOFTWARE_GIT_ADAPTER
                ),
                "write_path_scopes": list(
                    dict(node.payload.get("path_policy") or {}).get("test_scopes") or []
                ),
                "require_os_path_enforcement": bool(
                    dict(node.payload.get("path_policy") or {}).get("test_scopes")
                ),
            },
            prepare_workspace=True,
        )
        review_workspace, review_scratch = _verification_workspace_from_prompt_pack(
            artifacts=self.service.artifacts,
            prompt_ref=prompt_ref,
        )
        plan = _primary_json_output(terminal)
        if str(plan.get("outcome") or "").strip():
            return self._complete_semantic_verifier(
                effect=effect,
                node=node,
                scenario_mode=scenario_mode,
                invocation_id=invocation_id,
                lease_resource=lease_resource,
                fencing_token=fencing_token,
                candidate_ref=candidate_ref,
                candidate_digest=candidate_digest,
                candidate=candidate,
                review_workspace=review_workspace,
                review_scratch=review_scratch,
                execution_adapter=adapter,
                submission=plan,
                terminal=terminal,
                prompt_ref=prompt_ref,
                terminal_ref=terminal_ref,
            )
        raise SubmissionInvariantError(
            "Verifier must finish with one semantic outcome tool; legacy VerificationPlan submissions are disabled"
        )

    def _complete_semantic_verifier(
        self,
        *,
        effect: Mapping[str, Any],
        node: AggregateSnapshot,
        scenario_mode: bool,
        invocation_id: str,
        lease_resource: str,
        fencing_token: int,
        candidate_ref: ArtifactRef,
        candidate_digest: str,
        candidate: Mapping[str, Any],
        review_workspace: Path,
        review_scratch: Path,
        execution_adapter: str,
        submission: Mapping[str, Any],
        terminal: Mapping[str, Any],
        prompt_ref: ArtifactRef,
        terminal_ref: ArtifactRef,
    ) -> Mapping[str, Any]:
        outcome = str(submission.get("outcome") or "").strip()
        allowed_outcomes = {
            "pass",
            "module_repair",
            "dependency_repairs",
            "contract_revision",
            "architecture_revision",
            "requirements_revision",
            "unknown",
        }
        errors: list[str] = []
        if outcome not in allowed_outcomes:
            errors.append(f"unknown semantic verification outcome: {outcome or '<missing>'}")
        try:
            findings = structured_findings(submission)
        except ValueError as exc:
            findings = []
            errors.append(str(exc))
        reason = str(submission.get("reason") or "").strip()
        if outcome not in {"pass", "unknown"} and not findings:
            errors.append("repair and revision outcomes require structured findings")
        if outcome in {"pass", "unknown"} and findings:
            errors.append(f"{outcome.upper()} requires an empty finding list")
        if outcome == "unknown" and not reason:
            errors.append("UNKNOWN requires an environmental reason")
        scratch_only = scenario_mode or execution_adapter != SOFTWARE_GIT_ADAPTER
        changed_paths = (
            _verification_scratch_paths(review_scratch)
            if scratch_only
            else _verification_workspace_changed_paths(review_workspace, candidate_digest)
        )
        submitted_changed_paths = sorted(
            {
                str(item).replace("\\", "/")
                for item in list(submission.get("changed_test_paths") or [])
                if str(item).strip()
            }
        )
        if changed_paths != submitted_changed_paths:
            errors.append(
                "verifier test paths changed after semantic submission: submitted "
                f"{submitted_changed_paths}, current {changed_paths}"
            )
        test_scopes = [
            dict(item or {})
            for item in list(
                dict(node.payload.get("path_policy") or {}).get("test_scopes") or []
            )
        ]
        outside = [] if scratch_only else [
            path
            for path in changed_paths
            if not any(_semantic_path_scope_matches(path, scope) for scope in test_scopes)
        ]
        if outside:
            errors.append(
                "verifier changed paths outside the bound test scopes: "
                + ", ".join(outside)
            )
        if outcome != "unknown" and not changed_paths:
            errors.append("verification requires a real test delta in the bound test scopes")
        receipts = [
            dict(item)
            for item in list(submission.get("tool_receipts") or [])
            if isinstance(item, Mapping)
        ]
        if not receipts:
            errors.append("verification requires Manager-recorded shell, Git, or LSP evidence")
        if any(
            bool(dict(item.get("structured") or {}).get("read_only_workspace_dirty"))
            for item in receipts
        ):
            errors.append(
                "a verification command modified the audited workspace after its pre-command snapshot"
            )
        last_write = max(
            (index for index, item in enumerate(receipts) if item.get("kind") == "test_write"),
            default=-1,
        )
        final_checks = [
            item
            for index, item in enumerate(receipts)
            if index > last_write and item.get("kind") in {"command", "lsp"}
        ]
        if changed_paths and not final_checks:
            errors.append("run verification again after the final test edit")
        if outcome == "pass" and not any(bool(item.get("ok")) for item in final_checks):
            errors.append("PASS requires a successful final command or LSP receipt")
        if errors:
            raise SubmissionInvariantError(
                "semantic verifier submission failed manager validation:\n- "
                + "\n- ".join(errors)
            )

        settlement = self._role_submission_settlement(effect)
        assignment = self.repository.read_role_assignment(
            settlement["role_assignment_id"]
        )
        if assignment is None:
            raise SubmissionInvariantError("verifier assignment disappeared before quiescing")
        submission_ref = dict(assignment.get("submission_artifact_ref") or {})
        pending_ref = self.service.artifacts.put_json(
            {
                "schema_version": "1",
                "scenario_mode": scenario_mode,
                "submission": dict(submission),
                "candidate_ref": candidate_ref.to_dict(),
                "implementation_candidate_ref": candidate_ref.to_dict(),
                "candidate_digest": candidate_digest,
                "submitted_workspace_fingerprint": workspace_content_fingerprint(
                    review_workspace
                ),
                "review_workspace": str(review_workspace),
                "review_scratch": str(review_scratch),
                "execution_adapter": execution_adapter,
                "invocation_id": invocation_id,
                "lease_resource_key": lease_resource,
                "fencing_token": fencing_token,
                "role_assignment_id": settlement["role_assignment_id"],
                "role_submission_payload_hash": settlement[
                    "role_submission_payload_hash"
                ],
                "submission_ref": submission_ref,
            },
            artifact_type="PendingSemanticVerificationArtifact",
            provenance={"owner": "manager", "source_role": "verifier"},
            child_refs=tuple(
                (str(ref["sha256"]), relation)
                for ref, relation in (
                    (candidate_ref.to_dict(), "candidate"),
                    (submission_ref, "semantic_submission"),
                    (prompt_ref.to_dict(), "prompt_pack"),
                    (terminal_ref.to_dict(), "worker_terminal"),
                )
                if ref.get("sha256")
            ),
        )
        self._record_role_turn(
            terminal=terminal,
            invocation_id=invocation_id,
            fencing_token=fencing_token,
            turn_index=1,
            llm_request_ref=prompt_ref.to_dict(),
            llm_response_ref=terminal_ref.to_dict(),
            tool_summary_ref=pending_ref.to_dict(),
            **_recorded_role_metrics(terminal),
        )
        current = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            node.aggregate_id,
        )
        if current is None:
            raise SubmissionInvariantError("verification node disappeared before quiescing")
        self.repository.dispatch(
            ActionEnvelope(
                action_type="SUBMIT_SEMANTIC_VERIFICATION",
                workflow_id=current.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=current.aggregate_id,
                actor=invocation_id,
                expected_version=current.version,
                idempotency_key=(
                    f"semantic-verification-submit:{current.aggregate_id}:"
                    f"{pending_ref.sha256}"
                ),
                payload={
                    "pending_verification_ref": pending_ref.to_dict(),
                    "role_assignment_id": settlement["role_assignment_id"],
                    "role_submission_payload_hash": settlement[
                        "role_submission_payload_hash"
                    ],
                },
            ),
            **settlement,
        )
        return {
            "provider_request_id": invocation_id,
            "result_artifact_ref": pending_ref.to_dict(),
        }

    async def _quiesce_verifier_role(
        self,
        effect: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        pending_ref = _ref_from_mapping(node.payload.get("pending_verification_ref"))
        pending = dict(self.service.artifacts.read_json(pending_ref))
        invocation_id = str(pending.get("invocation_id") or "")
        lease_resource = str(pending.get("lease_resource_key") or "")
        fencing_token = int(pending.get("fencing_token") or 0)
        try:
            self.repository.assert_fencing_token(
                lease_resource,
                invocation_id,
                fencing_token,
            )
        except (LeaseConflict, StaleFencingToken):
            lease = self.repository.read_lease(lease_resource)
            if lease is not None and _lease_is_live(lease):
                raise
            rebound = self.repository.claim_lease(
                lease_resource,
                invocation_id,
                ttl_seconds=120,
                metadata={
                    "workflow_id": node.workflow_id,
                    "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                    "aggregate_id": node.aggregate_id,
                    "role": "verifier_snapshot",
                    "workspace_path": str(pending.get("review_workspace") or ""),
                },
            )
            fencing_token = rebound.fencing_token
        self._revoked_tokens.add((invocation_id, fencing_token))
        lease = self.repository.read_lease(lease_resource)
        process_group = int(
            dict((lease or {}).get("metadata") or {}).get("process_group_id") or 0
        )
        if process_group and not await terminate_process_group(
            process_group,
            timeout_seconds=5.0,
        ):
            raise RuntimeError("verifier process group did not quiesce")
        review_workspace = Path(str(pending.get("review_workspace") or ""))
        await self._release_managed_lsp_workspace(review_workspace)
        _raise_if_workspace_held(
            review_workspace,
            "a live process still holds the verifier worktree",
        )
        lock_key = f"verification:{node.aggregate_id}"
        self._worktree_locks.release(lock_key)
        lock_path = self._worktree_locks.acquire(lock_key, review_workspace)
        try:
            _raise_if_workspace_held(
                review_workspace,
                "a process reached the verifier worktree during quiescing",
                manager_snapshot_lock=lock_path,
            )
            fingerprint = workspace_content_fingerprint(review_workspace)
            submitted_fingerprint = str(
                pending.get("submitted_workspace_fingerprint") or ""
            )
            if submitted_fingerprint and fingerprint != submitted_fingerprint:
                raise RuntimeError(
                    "verifier worktree changed after semantic submission"
                )
        except BaseException:
            self._worktree_locks.release(lock_key)
            raise
        current = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            node.aggregate_id,
        )
        if current is None:
            self._worktree_locks.release(lock_key)
            raise SubmissionInvariantError("verification node disappeared while quiescing")
        self.repository.dispatch(
            ActionEnvelope(
                action_type="VERIFIER_QUIESCED",
                workflow_id=current.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=current.aggregate_id,
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

    def _snapshot_semantic_verification(
        self,
        effect: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        pending_ref = _ref_from_mapping(node.payload.get("pending_verification_ref"))
        pending = dict(self.service.artifacts.read_json(pending_ref))
        review_workspace = Path(str(pending.get("review_workspace") or ""))
        review_scratch = Path(str(pending.get("review_scratch") or ""))
        candidate_ref = _ref_from_mapping(pending.get("candidate_ref"))
        candidate_digest = str(pending.get("candidate_digest") or "")
        candidate = dict(self.service.artifacts.read_json(candidate_ref))
        submission = dict(pending.get("submission") or {})
        scenario_mode = bool(pending.get("scenario_mode"))
        lock_key = f"verification:{node.aggregate_id}"
        expected_fingerprint = str(node.payload.get("workspace_fingerprint") or "")
        if not self._worktree_locks.is_held(lock_key):
            _raise_if_workspace_held(
                review_workspace,
                "a live process still holds the verifier worktree",
            )
            if workspace_content_fingerprint(review_workspace) != expected_fingerprint:
                raise RuntimeError("verifier worktree changed after quiescing")
            self._worktree_locks.acquire(lock_key, review_workspace)
        try:
            if workspace_content_fingerprint(review_workspace) != expected_fingerprint:
                raise RuntimeError("verifier worktree changed while snapshotting")
            return self._finalize_semantic_verification(
                node=node,
                pending=pending,
                submission=submission,
                candidate_ref=candidate_ref,
                candidate_digest=candidate_digest,
                candidate=candidate,
                review_workspace=review_workspace,
                review_scratch=review_scratch,
                execution_adapter=str(pending.get("execution_adapter") or ""),
                scenario_mode=scenario_mode,
            )
        finally:
            self._worktree_locks.release(lock_key)

    def _finalize_semantic_verification(
        self,
        *,
        node: AggregateSnapshot,
        pending: Mapping[str, Any],
        submission: Mapping[str, Any],
        candidate_ref: ArtifactRef,
        candidate_digest: str,
        candidate: Mapping[str, Any],
        review_workspace: Path,
        review_scratch: Path,
        execution_adapter: str,
        scenario_mode: bool,
    ) -> Mapping[str, Any]:
        invocation_id = str(
            node.payload.get("active_worker_id")
            or pending.get("invocation_id")
            or ""
        )
        lease_resource = str(
            node.payload.get("lease_resource_key")
            or pending.get("lease_resource_key")
            or ""
        )
        fencing_token = int(
            node.payload.get("fencing_token")
            or pending.get("fencing_token")
            or 0
        )
        outcome = str(submission.get("outcome") or "").strip()
        findings = structured_findings(submission)
        reason = str(submission.get("reason") or "").strip()
        scratch_only = scenario_mode or execution_adapter != SOFTWARE_GIT_ADAPTER
        changed_paths = (
            _verification_scratch_paths(review_scratch)
            if scratch_only
            else _verification_workspace_changed_paths(
                review_workspace,
                candidate_digest,
            )
        )
        submitted_changed_paths = sorted(
            {
                str(item).replace("\\", "/")
                for item in list(submission.get("changed_test_paths") or [])
                if str(item).strip()
            }
        )
        if changed_paths != submitted_changed_paths:
            raise SubmissionInvariantError(
                "verifier test paths changed between submission and snapshot"
            )
        test_scopes = [
            dict(item or {})
            for item in list(
                dict(node.payload.get("path_policy") or {}).get("test_scopes") or []
            )
        ]
        outside = [] if scratch_only else [
            path
            for path in changed_paths
            if not any(
                _semantic_path_scope_matches(path, scope) for scope in test_scopes
            )
        ]
        if outside:
            raise SubmissionInvariantError(
                "verifier snapshot contains paths outside the bound test scopes: "
                + ", ".join(outside)
            )
        receipts = [
            dict(item)
            for item in list(submission.get("tool_receipts") or [])
            if isinstance(item, Mapping)
        ]
        test_workspace_ref = self._publish_verification_workspace(
            review_worktree=review_workspace,
            review_scratch=review_scratch,
            candidate_digest=candidate_digest,
            execution_adapter=execution_adapter,
            include_candidate_patch=not scratch_only,
        )
        accepted_candidate_ref = candidate_ref
        accepted_candidate_digest = candidate_digest
        accepted_candidate = dict(candidate)
        if outcome == "pass" and not scratch_only:
            (
                accepted_candidate_ref,
                accepted_candidate_digest,
                accepted_candidate,
            ) = self._promote_verifier_tests(
                node=node,
                review_workspace=review_workspace,
                candidate_ref=candidate_ref,
                candidate=candidate,
                candidate_digest=candidate_digest,
                test_delta_ref=test_workspace_ref,
                changed_test_paths=changed_paths,
            )
        receipts_ref = self.service.artifacts.put_json(
            {
                "schema_version": "1",
                "candidate_digest": candidate_digest,
                "receipts": receipts,
            },
            artifact_type="VerificationToolReceiptSetArtifact",
            provenance={"owner": "manager", "role": "verifier"},
        )
        status = (
            VerificationStatus.PASS
            if outcome == "pass"
            else VerificationStatus.UNKNOWN
            if outcome == "unknown"
            else VerificationStatus.FAIL
        )
        report_ref = self.service.artifacts.put_json(
            {
                "schema_version": "2",
                "module_name": str(
                    node.payload.get("module_name") or node.payload.get("unit_id") or ""
                ),
                "outcome": outcome,
                "status": status.value,
                "findings": findings,
                "unknown_reason": reason,
                "changed_test_paths": changed_paths,
                "candidate_ref": accepted_candidate_ref.to_dict(),
                "implementation_candidate_ref": candidate_ref.to_dict(),
                "test_delta_ref": test_workspace_ref.to_dict(),
                "tool_receipts_ref": receipts_ref.to_dict(),
                **(
                    {"scenario_fingerprint": str(node.payload.get("scenario_fingerprint") or "")}
                    if scenario_mode
                    else {}
                ),
            },
            artifact_type="VerificationArtifact",
            provenance={"owner": "manager", "source_role": "verifier"},
            child_refs=(
                (accepted_candidate_ref.sha256, "candidate"),
                (candidate_ref.sha256, "implementation_candidate"),
                (test_workspace_ref.sha256, "test_delta"),
                (receipts_ref.sha256, "tool_receipts"),
            ),
        )
        defect_kind = {
            "dependency_repairs": DefectKind.DEPENDENCY,
            "contract_revision": DefectKind.CONTRACT,
            "architecture_revision": DefectKind.ARCHITECTURE,
            "requirements_revision": DefectKind.ARCHITECTURE,
        }.get(
            outcome,
            DefectKind.INTEGRATION
            if str(node.payload.get("node_kind") or "") == "integration"
            else DefectKind.MODULE,
        )
        target_modules = [
            str(item).strip()
            for item in list(submission.get("target_modules") or [])
            if str(item).strip()
        ]
        dependency_node_ids = [
            _resolve_dependency_node_id(
                self.repository,
                node,
                dependency_module=module_name,
            )
            for module_name in target_modules
        ]
        module_node_id = ""
        if scenario_mode and outcome == "module_repair":
            raise SubmissionInvariantError(
                "scenario verifier must use dependency repairs with semantic module names"
            )
        if scenario_mode and outcome == "dependency_repairs" and dependency_node_ids:
            module_node_id = dependency_node_ids[0]
            defect_kind = DefectKind.MODULE
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "outcome": outcome,
                    "findings": findings,
                    "test_delta": test_workspace_ref.sha256,
                    "receipt_hashes": [str(item.get("output_sha256") or "") for item in receipts],
                    "candidate_tree": _candidate_tree_fingerprint(
                        accepted_candidate,
                        fallback=accepted_candidate_digest,
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        repair_ref: ArtifactRef | None = None
        if status == VerificationStatus.FAIL:
            repair_ref = self.service.artifacts.put_json(
                {
                    "schema_version": "1",
                    "artifact_kind": "semantic_repair_packet",
                    "module_name": str(
                        node.payload.get("module_name") or node.payload.get("unit_id") or ""
                    ),
                    "route": outcome,
                    "target_modules": target_modules,
                    "findings": findings,
                    "candidate_ref": candidate_ref.to_dict(),
                    "verification_ref": report_ref.to_dict(),
                    "test_delta_ref": test_workspace_ref.to_dict(),
                    "changed_test_paths": changed_paths,
                    "tool_receipts_ref": receipts_ref.to_dict(),
                    "regression_commands": [
                        str(dict(item.get("args") or {}).get("cmd") or "")
                        for item in receipts
                        if item.get("kind") == "command"
                        and str(dict(item.get("args") or {}).get("cmd") or "").strip()
                    ],
                },
                artifact_type="RepairPacketArtifact",
                provenance={"owner": "manager", "source_role": "verifier"},
                child_refs=(
                    (report_ref.sha256, "verification"),
                    (test_workspace_ref.sha256, "test_delta"),
                    (receipts_ref.sha256, "tool_receipts"),
                ),
            )

        current = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            node.aggregate_id,
        )
        if current is None:
            raise SubmissionInvariantError("verification node disappeared before verdict")
        unknown_policy = _manager_unknown_policy(node)
        VerificationService(self.repository, self.service.artifacts).submit_verdict(
            node=current,
            verification_ref=report_ref,
            status=status,
            actor=invocation_id,
            unknown_policy=unknown_policy,
            repair_bill_ref=repair_ref,
            finding_fingerprint_value=fingerprint if repair_ref is not None else "",
            candidate_tree_hash=_candidate_tree_fingerprint(
                accepted_candidate,
                fallback=accepted_candidate_digest,
            ),
            defect_kind=defect_kind,
            dependency_node_id=dependency_node_ids[0] if dependency_node_ids else "",
            dependency_node_ids=dependency_node_ids,
            module_node_id=module_node_id,
            module_node_ids=(dependency_node_ids if module_node_id else ()),
            scenario_fingerprint=str(node.payload.get("scenario_fingerprint") or ""),
            accepted_candidate_ref=(
                accepted_candidate_ref if outcome == "pass" and not scratch_only else None
            ),
            accepted_candidate_digest=(
                accepted_candidate_digest if outcome == "pass" and not scratch_only else ""
            ),
        )
        self.repository.complete_role_session(invocation_id)
        self.repository.release_lease(lease_resource, invocation_id, fencing_token)
        return {
            "provider_request_id": invocation_id,
            "result_artifact_ref": report_ref.to_dict(),
        }

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
        pending_verification_value = dict(
            node.payload.get("pending_verification_ref") or {}
        )
        if pending_verification_value.get("sha256"):
            pending_verification = dict(
                self.service.artifacts.read_json(pending_verification_value)
            )
            review_workspace_text = str(
                pending_verification.get("review_workspace") or ""
            )
            if review_workspace_text:
                review_workspace = Path(review_workspace_text)
                await self._release_managed_lsp_workspace(review_workspace)
                _raise_if_workspace_held(
                    review_workspace,
                    "node verifier still holds its review worktree",
                )
        self._worktree_locks.release(node.aggregate_id)
        self._worktree_locks.release(f"verification:{node.aggregate_id}")
        current = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            node.aggregate_id,
        )
        cancel_target = str(current.payload.get("cancel_target") or "CANCELLED")
        terminal_cancel = bool(cancel and cancel_target == "CANCELLED")
        self.repository.cancel_role_assignments(
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
            for session in self.repository.list_role_sessions(
                workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node.aggregate_id,
            ):
                if str(session.get("status") or "") in {"completed", "cancelled"}:
                    continue
                self.repository.complete_role_session(
                    str(session["session_id"]),
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
        self.repository.cancel_role_assignments(
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
            self.repository.complete_role_session(
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
                return await self._quiesce_architect_role(effect)
            if snapshot.state == "ARCHITECT_SNAPSHOTTING":
                return await self._snapshot_architect_result(effect)
            if snapshot.state == "HUMAN_REVIEW":
                return await self._publish_human_architecture_review(effect)
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
        return await self._resume_node(effect)

    async def _resume_node(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        if node.state == "QUEUED":
            if str(node.payload.get("node_kind") or "") == "verification":
                return self._admit_node_worker(
                    effect,
                    action_type="START_SCENARIO_VERIFICATION",
                    activation=RoleActivation(
                        OrchestrationRole.VERIFIER,
                        RoleMode.SCENARIO,
                    ),
                )
            return self._admit_node_worker(
                effect,
                action_type="START_PRODUCING",
                activation=RoleActivation(
                    OrchestrationRole.IMPLEMENTATION,
                    RoleMode.PRODUCE,
                ),
            )
        if node.state == "REVIEW_QUEUED":
            return self._admit_node_worker(
                effect,
                action_type="START_REVIEW",
                activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
            )
        if node.state == "REPAIR_QUEUED":
            return self._admit_node_worker(
                effect,
                action_type="START_REPAIR",
                activation=RoleActivation(
                    OrchestrationRole.IMPLEMENTATION,
                    RoleMode.REPAIR,
                ),
            )
        if node.state == "QUIESCING":
            return await self._quiesce_node(effect)
        if node.state == "SNAPSHOTTING":
            return await self._snapshot_implementation_result(effect)
        if node.state in {"REVIEW_QUIESCING", "VERIFY_QUIESCING"}:
            return await self._quiesce_verifier_role(effect)
        if node.state in {"REVIEW_SNAPSHOTTING", "VERIFY_SNAPSHOTTING"}:
            return self._snapshot_semantic_verification(effect)
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
        scenarios = [item for item in nodes if str(item.payload.get("node_kind") or "") == "verification"]
        if not implementation or not scenarios or any(item.state != "ACCEPTED" for item in nodes):
            raise ValueError(
                "final candidate union requires every implementation module and end-to-end scenario ACCEPTED"
            )
        verification_refs: list[dict[str, Any]] = []
        scenario_fingerprints: dict[str, str] = {}
        for node in scenarios:
            ref = dict(node.payload.get("verification_artifact_ref") or {})
            if not ref.get("sha256"):
                raise ValueError(
                    f"accepted scenario {node.payload.get('module_name')} has no VerificationArtifact"
                )
            verification_refs.append(ref)
            scenario_name = str(node.payload.get("module_name") or node.aggregate_id)
            scenario_fingerprint = str(node.payload.get("scenario_fingerprint") or "")
            if not scenario_fingerprint:
                raise ValueError(f"accepted scenario {scenario_name} has no scenario fingerprint")
            scenario_fingerprints[scenario_name] = scenario_fingerprint
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
                    "modules": dict(submission.get("modules") or {}),
                    "integration": dict(submission.get("integration") or {}),
                },
                artifact_type="StandaloneSkeletonReviewViewArtifact",
                provenance={"owner": "manager", "audience": "standalone_reviewer"},
                child_refs=((request_ref.sha256, "architecture_skeleton"),),
            )
            reviewer_inputs = {"task": requirements_ref, "review_request": review_view_ref}
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
            profile=self._profile_for_role(review.workflow_id, "reviewer"),
            activation=RoleActivation(OrchestrationRole.REVIEWER, RoleMode.STANDALONE),
            instruction="Perform the requested standalone review. Report evidence-grounded findings and do not modify the target. Repair is a separate explicit workflow.",
            reference_refs=reviewer_inputs,
            workspace_override={
                "kind": "existing_repo",
                "repo_path": str(review_repo),
                "project_name": "standalone-review",
                "review_scratch_dir": str(review_scratch),
            },
            prepare_workspace=True,
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
            mode="standalone",
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
        self._record_role_turn(
            terminal=terminal,
            invocation_id=invocation_id,
            fencing_token=fencing_token,
            turn_index=1,
            llm_request_ref=prompt_ref.to_dict(),
            llm_response_ref=terminal_ref.to_dict(),
            tool_summary_ref=report_ref.to_dict(),
            **_recorded_role_metrics(terminal),
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
            **self._role_submission_settlement(effect),
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
                    "active_role": OrchestrationRole.REVIEWER.value,
                    "active_role_mode": RoleMode.STANDALONE.value,
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
            "schema_version": "2",
            "artifact_kind": "structured_repair_bill",
            "module_name": module_name,
            "route": "module_repair",
            "findings": findings,
            "expected": "The accepted skeleton contract and Requirements are satisfied.",
            "actual": str(finding.get("summary") or "Review failed."),
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
        requirements_ref = _ref_from_mapping(workflow_request.get("requirements_ref"))
        validate_task_source_bundle(self.service.artifacts.read_json(requirements_ref))
        unit_contract = {
            **seed,
            "unit_id": str(seed.get("unit_id") or "review_repair"),
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
        stage = OrchestrationRole.ARCHITECT.value
        start_action = "START_ARCHITECT"
        running_state = "ARCHITECT_RUNNING"
        rebind_action = "REBIND_ARCHITECT"
        revision = self._effect_snapshot(effect)
        activation = RoleActivation(
            OrchestrationRole.ARCHITECT,
            (
                RoleMode.REVISION
                if self._revision_input_base_manifest_ref(revision) is not None
                else RoleMode.AUTHOR
            ),
        )
        profile = self._profile_for_role(revision.workflow_id, OrchestrationRole.ARCHITECT.value)
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
            metadata={
                "workflow_id": revision.workflow_id,
                "aggregate_id": revision.aggregate_id,
                "role": OrchestrationRole.ARCHITECT.value,
                "mode": activation.mode.value,
            },
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
                            "active_role": OrchestrationRole.ARCHITECT.value,
                            "active_role_mode": activation.mode.value,
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
                            "active_role": OrchestrationRole.ARCHITECT.value,
                            "active_role_mode": activation.mode.value,
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
                activation=activation,
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
                    action_type="DATA_ARCHITECT_COMPLETED",
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
                **self._role_submission_settlement(effect),
            )
            self._record_role_turn(
                terminal=terminal,
                invocation_id=invocation_id,
                fencing_token=lease.fencing_token,
                turn_index=_role_session_turn_index(terminal),
                llm_request_ref=prompt_ref.to_dict(),
                llm_response_ref=terminal_ref.to_dict(),
                tool_summary_ref=result_ref.to_dict(),
                **_recorded_role_metrics(terminal),
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
                        "active_role": OrchestrationRole.ARCHITECT.value,
                        "active_role_mode": (
                            RoleMode.REVISION.value
                            if revision_scope is not None
                            else RoleMode.AUTHOR.value
                        ),
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
            references: dict[str, ArtifactRef] = {"task": requirements_ref}
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
                activation=RoleActivation(
                    OrchestrationRole.ARCHITECT,
                    RoleMode.REVISION if revision_scope is not None else RoleMode.AUTHOR,
                ),
                instruction=instruction,
                reference_refs=references,
                workspace_override=workspace_override,
                prepare_workspace=True,
            )
            submission = _named_json_output(terminal, "architecture_submission.json")
            submission_ref = self.service.artifacts.put_json(
                submission,
                artifact_type="ArchitectureSkeletonSubmissionIntentArtifact",
                provenance={"role": "architect"},
                child_refs=((requirements_ref.sha256, "requirements"),),
            )
            self._record_role_turn(
                terminal=terminal,
                invocation_id=invocation_id,
                fencing_token=lease.fencing_token,
                turn_index=_role_session_turn_index(terminal),
                llm_request_ref=prompt_ref.to_dict(),
                llm_response_ref=terminal_ref.to_dict(),
                tool_summary_ref=submission_ref.to_dict(),
                **_recorded_role_metrics(terminal),
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
                **self._role_submission_settlement(effect),
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
                    "active_role": OrchestrationRole.ARCHITECT.value,
                    "active_role_mode": (
                        RoleMode.REVISION.value
                        if self._revision_input_base_manifest_ref(revision) is not None
                        else RoleMode.AUTHOR.value
                    ),
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

    async def _quiesce_architect_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
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
                manager_snapshot_lock=lock_path,
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

    async def _snapshot_architect_result(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
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
            manifest_ref = self.service.skeleton.snapshot_architect_result(
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
        manifest_payload = self.service.artifacts.read_json(manifest_ref)
        effective_requirements_ref = _ref_from_mapping(manifest_payload.get("requirements_ref"))
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
                    "requirements_ref": effective_requirements_ref.to_dict(),
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
            metadata={
                "workflow_id": revision.workflow_id,
                "aggregate_id": revision.aggregate_id,
                "role": OrchestrationRole.REVIEWER.value,
                "mode": RoleMode.ARCHITECTURE.value,
            },
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
                            "active_role": OrchestrationRole.REVIEWER.value,
                            "active_role_mode": RoleMode.ARCHITECTURE.value,
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
                            "active_role": OrchestrationRole.REVIEWER.value,
                            "active_role_mode": RoleMode.ARCHITECTURE.value,
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
                provenance={"owner": "manager", "audience": "reviewer"},
                child_refs=((manifest_ref.sha256, "architecture_manifest"),),
            )
            review_refs: dict[str, ArtifactRef] = {
                "task": requirements_ref,
                "architecture_contract": semantic_contract_ref,
            }
            revision_base_value = revision.payload.get("revision_base_manifest_ref")
            if revision_base_value:
                finding_value = architecture_revision_finding_value(revision.payload)
                if finding_value:
                    review_refs["revision_finding"] = self._publish_architecture_finding_view(
                        finding_value,
                        audience="reviewer",
                    )
                root_batch_value = revision.payload.get("replan_finding_batch_ref")
                if root_batch_value:
                    review_refs["replan_finding_batch"] = self._publish_architecture_finding_view(
                        root_batch_value,
                        audience="reviewer",
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
                profile=self._profile_for_role(revision.workflow_id, "reviewer"),
                activation=RoleActivation(OrchestrationRole.REVIEWER, RoleMode.ARCHITECTURE),
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
            self._record_role_turn(
                terminal=terminal,
                invocation_id=invocation_id,
                fencing_token=lease.fencing_token,
                turn_index=1,
                llm_request_ref=prompt_ref.to_dict(),
                llm_response_ref=terminal_ref.to_dict(),
                tool_summary_ref=review_ref.to_dict(),
                **_recorded_role_metrics(terminal),
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
                        "active_role": OrchestrationRole.REVIEWER.value,
                        "active_role_mode": RoleMode.ARCHITECTURE.value,
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
                "task": requirements_ref,
                "architecture_index": review_view_ref,
                "architecture_diff": diff_ref,
            }
            finding_value = architecture_revision_finding_value(revision.payload)
            if finding_value:
                references["prior_finding"] = self._publish_architecture_finding_view(
                    finding_value,
                    audience="reviewer",
                )
            root_batch_value = revision.payload.get("replan_finding_batch_ref")
            if root_batch_value:
                references["replan_finding_batch"] = self._publish_architecture_finding_view(
                    root_batch_value,
                    audience="reviewer",
                )
            terminal, prompt_ref, terminal_ref = await self._run_profile(
                effect=effect,
                snapshot=revision,
                invocation_id=invocation_id,
                lease_resource=lease_resource,
                fencing_token=lease.fencing_token,
                profile=self._profile_for_role(revision.workflow_id, "reviewer"),
                activation=RoleActivation(OrchestrationRole.REVIEWER, RoleMode.ARCHITECTURE),
                instruction=(
                    "Review the candidate code skeleton against the exact same immutable Requirements received by the Architect. "
                    "Inspect the module DAG, complete skeleton diff, declarations and comments, ownership, lifecycle, state, invariants, dependencies, and end-to-end contract. "
                    "The Manager performs no semantic coverage or contract validation; independently review every hard Requirement and module in the bound scope. "
                    "Record only material defects as findings; do not write positive audit rows. "
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
                prepare_workspace=True,
            )
            try:
                semantic_payload = _named_json_output(terminal, "architecture_review.json")
                semantic = _parse_skeleton_review(semantic_payload)
            except Exception as exc:
                raise SubmissionInvariantError(
                    f"accepted architecture_review_submit failed manager defense-in-depth validation: {exc}"
                ) from exc
            review_ref = self.service.artifacts.put_json(
                {
                    **semantic.to_dict(),
                    "review_scope": dict(semantic_payload.get("review_scope") or {}),
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
            self._record_role_turn(
                terminal=terminal,
                invocation_id=invocation_id,
                fencing_token=lease.fencing_token,
                turn_index=1,
                llm_request_ref=prompt_ref.to_dict(),
                llm_response_ref=terminal_ref.to_dict(),
                tool_summary_ref=review_ref.to_dict(),
                **_recorded_role_metrics(terminal),
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
                    "markdown": compile_skeleton_markdown(
                        artifact,
                        requirements_payload=requirements,
                    ),
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
            markdown_ref = self.service.artifacts.put_bytes(
                str(payload.get("markdown") or "").encode("utf-8"),
                artifact_type="ArchitectureHumanReviewMarkdownArtifact",
                media_type="text/markdown",
                child_refs=((manifest_ref.sha256, "architecture_manifest"),),
            )
            markdown_record = self.repository.read_artifact_record(markdown_ref.sha256)
            attachments: list[dict[str, Any]] = []
            if markdown_record is not None:
                attachments.append(
                    {
                        "path": str(markdown_record["storage_path"]),
                        "file_name": "architecture.md",
                        "mime_type": "text/markdown",
                        "caption": "Architecture skeleton, contract graph, and verification scenarios",
                    }
                )
            manifest_payload = dict(self.service.artifacts.read_json(manifest_ref))
            task_source_value = manifest_payload.get("requirements_ref")
            if isinstance(task_source_value, Mapping) and task_source_value.get("sha256"):
                task_source_ref = _ref_from_mapping(task_source_value)
                task_record = self.repository.read_artifact_record(task_source_ref.sha256)
                if task_record and str(task_record.get("artifact_type") or "") == TASK_SOURCE_BUNDLE_ARTIFACT:
                    for source in self.service.task_sources.source_attachments(task_source_ref):
                        source_ref = _ref_from_mapping(source["artifact_ref"])
                        source_record = self.repository.read_artifact_record(source_ref.sha256)
                        if source_record is None:
                            continue
                        attachments.append(
                            {
                                "path": str(source_record["storage_path"]),
                                "file_name": str(source["name"]).replace("/", "__"),
                                "mime_type": str(source["media_type"]),
                                "caption": f"Immutable task source: {source['name']}",
                            }
                        )
                        card_children.append((source_ref.sha256, "task_source"))
            payload["attachments"] = attachments
            card_children.append((markdown_ref.sha256, "architecture_markdown"))
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
        finding_value = architecture_revision_finding_value(revision.payload)
        base_manifest_ref = self._revision_input_base_manifest_ref(revision)
        refs: dict[str, ArtifactRef]
        scoped_revision = base_manifest_ref is not None and finding_value is not None
        if base_manifest_ref is None:
            refs = {
                "workflow_request": request_ref,
                "task": requirements_ref,
            }
        elif scoped_revision:
            refs = {
                "task": requirements_ref,
                "revision_scope": self._publish_architecture_revision_scope(
                    revision,
                    base_manifest_ref=base_manifest_ref,
                    finding_value=finding_value,
                )
            }
        else:
            refs = {"task": requirements_ref}
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
            "Produce an implementation DAG from every immutable file under reference:task; preserve their exact meaning and do not create normalized Requirement records. Inspect local files and "
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
        targets = normalize_revision_targets(raw_targets)
        base_payload = self._base_contract_builder_payload_from_manifest(base_manifest_ref)
        requirements_payload = self.service.artifacts.read_json(revision.payload["requirements_ref"])
        context: list[dict[str, Any]] = []
        if not targets:
            context.append(
                {
                    "access": "write",
                    "target": {"section": "architecture_contract", "name": "complete_contract"},
                    "value": _semantic_contract_review_view(base_payload, requirements_payload),
                }
            )
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
            "findings": [dict(item) for item in list(dict(finding_payload).get("findings") or [])],
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
            return {}
        return {"write_targets": [target.to_dict() for target in targets]}

    @staticmethod
    def _semantic_revision_target(
        target: ArchitectureRevisionTarget,
        value: Any,
    ) -> dict[str, Any]:
        selected = dict(value or {}) if isinstance(value, Mapping) else {}
        if target.section == "task_source":
            return {
                "section": "task_source",
                "name": target.target_id,
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
        del requirements
        collection_sections = {
            "constraint": ("global_constraints", "id"),
            "design_decision": ("design_decisions", "id"),
            "gate_check": ("gate_checks", "id"),
            "unit": ("units", "unit_id"),
            "cross_unit_contract": ("cross_unit_contracts", "id"),
        }
        if target.section in collection_sections:
            field_name, id_field = collection_sections[target.section]
            source = payload
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
        activation: RoleActivation,
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
        role = activation.role.value
        mode = activation.mode.value
        continuation_capable = activation.role in {
            OrchestrationRole.ARCHITECT,
            OrchestrationRole.IMPLEMENTATION,
            OrchestrationRole.VERIFIER,
        }
        if activation.role == OrchestrationRole.IMPLEMENTATION:
            workspace["manager_owned_submission_paths"] = [
                "coder_report.json",
                "producer_report.json",
            ]
        run_id = f"run_{invocation_id.removeprefix('inv_')[:16]}"
        workspace, uses_bound_durable_workspace = _prepare_role_workspace_before_environment(
            self.service.runtime_root,
            workspace,
            role=role,
            invocation_id=invocation_id,
            run_id=run_id,
            fencing_token=fencing_token,
            prepare_workspace=prepare_workspace,
        )
        skeleton_mode = bool(workspace.get("architecture_skeleton_mode"))
        builder_stage = (
            "architect_planning"
            if activation.role == OrchestrationRole.ARCHITECT
            else (
                "architecture_review"
                if activation
                == RoleActivation(OrchestrationRole.REVIEWER, RoleMode.ARCHITECTURE)
                else ""
            )
        )
        if builder_stage and not skeleton_mode:
            workspace["contract_builder_stage"] = builder_stage
        bound_reference_refs = dict(reference_refs)
        if bool(workspace_policy.get("prepare", False)):
            workspace, preparation = prepare_v2_workspace_environment(
                workspace,
                runtime_root=self.service.runtime_root,
            )
            if bool(workspace_policy.get("prewarm_lsp", False)) and list(workspace.get("languages") or []):
                lsp_preparation = prewarm_workspace_lsp(
                    runtime_root=self.service.runtime_root,
                    workspace=workspace,
                )
                preparation["lsp_workspace_preparation"] = lsp_preparation
                environment_fingerprint = str(
                    lsp_preparation.get("environment_fingerprint") or ""
                ).strip()
                if environment_fingerprint:
                    workspace["lsp_environment_fingerprint"] = environment_fingerprint
            preparation_ref = self.service.artifacts.put_json(
                _durable_workspace_preparation(preparation),
                artifact_type="WorkspacePreparationArtifact",
                provenance={"family_id": str(binding.get("family_id") or ""), "role": role},
            )
            bound_reference_refs["workspace_preparation"] = preparation_ref
        if activation.role == OrchestrationRole.VERIFIER or activation == RoleActivation(
            OrchestrationRole.REVIEWER,
            RoleMode.STANDALONE,
        ):
            view_name = (
                "module_work_view"
                if activation.role == OrchestrationRole.VERIFIER
                else "review_request"
            )
            view_ref = bound_reference_refs.get(view_name)
            view = self.service.artifacts.read_json(view_ref) if view_ref is not None else {}
            effective_policy = effective_verification_policy(
                work_view=view,
                verification_policy=dict(family_policies.get("verification") or {}),
                standalone=activation.mode == RoleMode.STANDALONE,
            )
            verification_policy_ref = self.service.artifacts.put_json(
                effective_policy,
                artifact_type="VerificationPolicyArtifact",
                provenance={"family_id": str(binding.get("family_id") or ""), "role": role},
            )
            bound_reference_refs["verification_policy"] = verification_policy_ref
        references: list[dict[str, Any]] = []
        reference_items = list(bound_reference_refs.items())
        if activation.mode == RoleMode.REPAIR:
            priority = {"repair_bill": 0, "unit_work_view": 1, "workspace_preparation": 2}
            reference_items.sort(key=lambda item: (priority.get(item[0], 3), item[0]))
        for name, ref in reference_items:
            if ref.artifact_type == "LocalPathReference":
                path = str(ref.media_type)
            elif ref.artifact_type == TASK_SOURCE_BUNDLE_ARTIFACT:
                path = str(self.service.task_sources.materialize(ref).root)
            else:
                record = self.repository.read_artifact_record(ref.sha256)
                if record is None:
                    raise ValueError(f"worker input artifact is unavailable: {name}")
                path = str(
                    self.service.task_sources.materialize_artifact(
                        ref,
                        semantic_name=name,
                    )
                )
            references.append(
                {
                    "name": name,
                    "path": path,
                    "description": f"V2 immutable input {name}",
                    "truth_source": True,
                    "required": True,
                    "bound_input": False,
                }
            )
        workspace["reference_paths"] = references
        profile_group, profile_name = profile.rsplit(".", 1)
        if skeleton_mode and activation.role == OrchestrationRole.ARCHITECT:
            invocation_acceptance = [
                "Write the contract-level code skeleton in the bound architecture worktree.",
                "Declare semantic module contract dependencies and real end-to-end scenarios incrementally, then call architecture_submit with no arguments.",
            ]
        elif skeleton_mode and activation == RoleActivation(
            OrchestrationRole.REVIEWER,
            RoleMode.ARCHITECTURE,
        ):
            invocation_acceptance = [
                "Review the exact bound task sources, skeleton diff, code contracts, semantic dependencies, and scenarios.",
                "Inspect the complete Manager-bound scope, then call architecture_review_pass or submit every material defect once through architecture_review_fail.",
            ]
        elif activation.role == OrchestrationRole.VERIFIER:
            invocation_acceptance = [
                "Write and run reproducible adversarial tests only in the bound module test scopes or scenario review scratch.",
                "Call exactly one semantic verification outcome tool; do not construct a VerificationPlan or evidence JSON.",
            ]
        elif activation.role == OrchestrationRole.IMPLEMENTATION:
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
        elif activation.mode == RoleMode.STANDALONE:
            invocation_acceptance = [
                "Review only the bound immutable target and run reproducible read-only probes.",
                "Record surfaces, findings, and conclusion incrementally, then call review_submit with no arguments.",
            ]
        else:
            invocation_acceptance = ["Write the exact primary JSON artifact required by the profile output contract."]
        mandatory_inputs: list[str] = []
        input_fingerprint = authoring_input_fingerprint(
            {
                "role": role,
                "mode": mode,
                "references": _assignment_role_input_refs(
                    {
                        name: ref.to_dict()
                        for name, ref in bound_reference_refs.items()
                    }
                ),
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
                    "mode": mode,
                    "executor_profile_id": profile,
                    "family_binding_sha": str(binding_ref.get("sha256") or ""),
                    "submission_receipt_required": True,
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
                "allow_text_only_completion": False,
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
        base_manifest_ref = (
            self._revision_input_base_manifest_ref(snapshot)
            if activation.role == OrchestrationRole.ARCHITECT
            else None
        )
        revision_scope: Mapping[str, Any] | None = None
        if activation.role == OrchestrationRole.ARCHITECT and "revision_scope" in bound_reference_refs:
            if skeleton_mode:
                revision_scope = dict(workspace.get("architecture_revision_scope") or {}) or None
            else:
                revision_scope = self._internal_architecture_revision_scope(snapshot) or None
        role_binding = dict(dict(binding.get("role_bindings") or {}).get(role) or {})
        pinned_profile = dict(role_binding.get("executor_profile") or {})
        if not pinned_profile:
            raise ValueError(
                f"FamilyBindingArtifact has no pinned executor profile for role {role}"
            )
        pack = resolve_pinned_minion_pack(
            pack,
            profile_payload=pinned_profile,
            family_payload=binding,
        )
        pack = apply_v2_role_capability_policy(pack, activation=activation)
        if activation.role == OrchestrationRole.ARCHITECT and revision_scope is not None:
            pack = apply_v2_revision_scope_capability_policy(pack)
        pack = apply_v2_research_capability_policy(
            pack,
            research_mode=str(snapshot.payload.get("research_mode") or "local_only"),
        )
        if activation == RoleActivation(
            OrchestrationRole.REVIEWER,
            RoleMode.ARCHITECTURE,
        ) and skeleton_mode:
            requirements_ref = bound_reference_refs.get("task")
            architecture_ref = bound_reference_refs.get("architecture_index")
            if requirements_ref is None or architecture_ref is None:
                raise ValueError("Architecture Reviewer requires bound task sources and architecture_index")
            tool_contract = compile_architecture_review_invocation_tool_contract(
                task_sources=self.service.artifacts.read_json(requirements_ref),
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
        if activation.role == OrchestrationRole.VERIFIER:
            view_ref = bound_reference_refs.get("module_work_view")
            if view_ref is not None:
                tool_contract = compile_swe_verification_tool_contract(
                    self.service.artifacts.read_json(view_ref)
                )
                pack_value = pack.to_dict()
                metadata = dict(pack_value.get("metadata") or {})
                minion_v2 = dict(metadata.get("minion_v2") or {})
                minion_v2["swe_verification_tool_contract"] = tool_contract
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
                pack = MinionInvocationPack.from_dict(
                    {
                        **pack_value,
                        "metadata": metadata,
                        "resolved_profile": resolved_profile,
                    }
                )
        elif activation == RoleActivation(OrchestrationRole.REVIEWER, RoleMode.STANDALONE):
            view_ref = bound_reference_refs.get("review_request")
            if view_ref is not None:
                tool_contract = compile_verification_invocation_tool_contract(
                    work_view=self.service.artifacts.read_json(view_ref),
                    verification_policy=dict(family_policies.get("verification") or {}),
                    standalone=True,
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
        if (
            prepare_workspace
            and not uses_bound_durable_workspace
            and not bool(pack.workspace.get("v2_role_workspace"))
        ):
            pack = prepare_v2_role_workspace(
                self.service.runtime_root,
                pack,
                run_id=run_id,
                attempt_key=f"fence-{fencing_token}",
            )
        elif not bool(pack.workspace.get("v2_role_workspace")):
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
        if activation.role == OrchestrationRole.ARCHITECT and base_manifest_ref is not None and not skeleton_mode:
            seed_contract_builder_draft(
                pack.workspace,
                self._base_contract_builder_payload_from_manifest(base_manifest_ref),
                revision_scope=revision_scope,
            )
        submission_kind = _role_submission_kind(activation, skeleton_mode=skeleton_mode)
        durable_input_refs = {
            name: ref.to_dict()
            for name, ref in bound_reference_refs.items()
            if ref.artifact_type != "LocalPathReference"
        }
        reusable_assignment = self._reusable_role_assignment(
            workflow_id=snapshot.workflow_id,
            aggregate_type=snapshot.aggregate_type.value,
            aggregate_id=snapshot.aggregate_id,
            role=role,
            mode=mode,
            submission_kind=submission_kind,
            input_refs=durable_input_refs,
        )
        if reusable_assignment is not None:
            self.repository.cancel_role_assignments(
                workflow_id=snapshot.workflow_id,
                aggregate_type=snapshot.aggregate_type,
                aggregate_id=snapshot.aggregate_id,
                reason=(
                    "superseded by equivalent durable role submission "
                    + str(reusable_assignment["assignment_id"])
                ),
                exclude_assignment_id=str(reusable_assignment["assignment_id"]),
            )
            self._signal_assignment_ready(effect, str(reusable_assignment["assignment_id"]))
            prompt_ref = self._durable_assignment_prompt_ref(reusable_assignment)
            if prompt_ref is None:
                if activation.role == OrchestrationRole.VERIFIER:
                    raise SubmissionInvariantError(
                        "durable verifier receipt lost its original prompt workspace binding"
                    )
                pack = sanitize_runner_session_pack(pack)
                prompt_ref = self.service.artifacts.put_json(
                    pack.to_dict(),
                    artifact_type="RolePromptPackArtifact",
                    child_refs=tuple(
                        (ref.sha256, name)
                        for name, ref in bound_reference_refs.items()
                        if ref.artifact_type != "LocalPathReference"
                    ),
                )
            terminal = self._terminal_from_assignment_receipt(
                reusable_assignment,
                primary_artifact_name=_role_primary_artifact_name(pack),
                summary="Reconciled an equivalent durable role submission receipt.",
            )
            terminal_ref = self.service.artifacts.put_json(
                terminal,
                artifact_type="RoleTerminalArtifact",
                child_refs=(
                    (prompt_ref.sha256, "prompt_pack"),
                    (
                        str(dict(reusable_assignment["submission_artifact_ref"])["sha256"]),
                        "submission_receipt",
                    ),
                ),
            )
            return terminal, prompt_ref, terminal_ref
        session_scope_kind, session_subject_key = _role_session_scope(snapshot, activation)
        self.repository.ensure_role_session(
            session_id=invocation_id,
            workflow_id=snapshot.workflow_id,
            aggregate_type=snapshot.aggregate_type,
            aggregate_id=snapshot.aggregate_id,
            role=role,
            mode=mode,
            executor_profile_id=profile,
            family_binding_sha=str(binding_ref.get("sha256") or ""),
            scope_kind=session_scope_kind,
            subject_key=session_subject_key,
        )
        assignment = self.repository.create_role_assignment(
            RoleAssignmentRequest(
                assignment_key=(
                    f"{str(effect.get('effect_key') or effect.get('effect_id') or '')}:"
                    f"{role}:{mode}:{input_fingerprint}"
                ),
                session_id=invocation_id,
                workflow_id=snapshot.workflow_id,
                aggregate_type=snapshot.aggregate_type.value,
                aggregate_id=snapshot.aggregate_id,
                role=role,
                mode=mode,
                executor_profile_id=profile,
                family_binding_sha=str(binding_ref.get("sha256") or ""),
                input_fingerprint=input_fingerprint,
                required_inputs=(),
                input_refs=durable_input_refs,
                execution_spec={
                    "effect_type": str(effect.get("effect_type") or "run_role"),
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
            RoleAssignmentState.CLAIMED.value,
            RoleAssignmentState.RUNNING.value,
        }:
            active_attempt = self.repository.read_role_attempt(
                str(assignment.get("active_attempt_id") or "")
            )
            active_lease = self.repository.read_lease(
                str(dict(active_attempt or {}).get("lease_resource_key") or "")
            )
            if active_lease is not None and _lease_is_live(active_lease):
                raise DeferredEffectError("role assignment already has a live process attempt")
            if active_attempt is not None:
                assignment = self.repository.queue_role_attempt_retry(
                    assignment_id=str(assignment["assignment_id"]),
                    attempt_id_value=str(active_attempt["attempt_id"]),
                    error_kind="attempt_lease_expired",
                    error_text="role attempt lease expired before submission settlement",
                )

        if assignment["state"] in {
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        }:
            prompt_ref = self._durable_assignment_prompt_ref(assignment)
            if prompt_ref is None:
                if activation.role == OrchestrationRole.VERIFIER:
                    raise SubmissionInvariantError(
                        "durable verifier receipt lost its original prompt workspace binding"
                    )
                pack = sanitize_runner_session_pack(pack)
                prompt_ref = self.service.artifacts.put_json(
                    pack.to_dict(),
                    artifact_type="RolePromptPackArtifact",
                    child_refs=tuple(
                        (ref.sha256, name)
                        for name, ref in bound_reference_refs.items()
                        if ref.artifact_type != "LocalPathReference"
                    ),
                )
            terminal = self._terminal_from_assignment_receipt(
                assignment,
                primary_artifact_name=_role_primary_artifact_name(pack),
                summary="Reconciled the exact durable role submission receipt.",
            )
            terminal_ref = self.service.artifacts.put_json(
                terminal,
                artifact_type="RoleTerminalArtifact",
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
            RoleAssignmentState.QUEUED.value,
            RoleAssignmentState.RETRY_QUEUED.value,
        }:
            raise SubmissionInvariantError(
                f"role assignment cannot start from {assignment['state']}"
            )
        attempt = self.repository.claim_role_assignment(str(assignment["assignment_id"]))
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
                "mode": mode,
            },
        )
        continuation_input_path, continuation_output_path = (
            self._prepare_agent_session_attempt(
                session_id=invocation_id,
                attempt_id=str(attempt["attempt_id"]),
            )
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
            "scope_kind": session_scope_kind,
            "subject_key": session_subject_key,
            "continuation_input_path": str(continuation_input_path or ""),
            "continuation_output_path": str(continuation_output_path),
        }
        pack = MinionInvocationPack.from_dict({**pack_value, "metadata": metadata})
        pack = sanitize_runner_session_pack(pack)
        pack = with_minion_sandbox_metadata(self.service.runtime_root, pack, run_id=run_id)
        prompt_ref = self.service.artifacts.put_json(
            pack.to_dict(),
            artifact_type="RolePromptPackArtifact",
            child_refs=tuple(
                (ref.sha256, name)
                for name, ref in bound_reference_refs.items()
                if ref.artifact_type != "LocalPathReference"
            ),
        )
        self.repository.start_role_attempt(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(attempt["attempt_id"]),
            lease_resource_key=assignment_lease_resource,
            fencing_token=assignment_lease.fencing_token,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        assignment_access_token = self.repository.issue_role_attempt_access_token(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(attempt["attempt_id"]),
            fencing_token=assignment_lease.fencing_token,
        )
        self.repository.record_role_invocation(
            invocation_id=invocation_id,
            workflow_id=snapshot.workflow_id,
            aggregate_type=snapshot.aggregate_type,
            aggregate_id=snapshot.aggregate_id,
            lease_resource_key=lease_resource,
            fencing_token=fencing_token,
            role=role,
            mode=mode,
            executor_profile_id=profile,
            family_binding_sha=str(binding_ref.get("sha256") or ""),
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
        runner_env[ROLE_GATEWAY_TOKEN_ENV] = assignment_access_token
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
        self.repository.update_role_attempt_process_group(
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
        assignment_after_process = self.repository.read_role_assignment(
            str(assignment["assignment_id"])
        )
        has_submission_receipt = bool(
            dict((assignment_after_process or {}).get("submission_artifact_ref") or {})
        )
        if process.returncode != 0 and not has_submission_receipt:
            error_tail = _meaningful_stderr_tail(stderr.decode("utf-8", errors="replace"))
            self.repository.queue_role_attempt_retry(
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
                continuation_output_path,
            )
            if continuation_ref is not None and continuation_capable:
                self.repository.suspend_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    continuation_ref=continuation_ref.to_dict(),
                    status="interrupted",
                )
            else:
                self.repository.finish_role_invocation(
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
                primary_artifact_name=_role_primary_artifact_name(pack),
                summary="Recovered a durable submission after the role process ended.",
            )
        if terminal is None:
            self.repository.queue_role_attempt_retry(
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
                continuation_output_path,
            )
            if continuation_ref is not None and continuation_capable:
                self.repository.suspend_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    continuation_ref=continuation_ref.to_dict(),
                    status="interrupted",
                )
            else:
                self.repository.finish_role_invocation(
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
            self.repository.queue_role_attempt_retry(
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
                continuation_output_path,
            )
            if continuation_ref is None:
                raise RuntimeError(
                    "worker reached a manager-restart safe point without a durable continuation"
                )
            self.repository.suspend_role_invocation(
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
                self.repository.queue_role_attempt_retry(
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
                continuation_output_path,
            )
            if continuation_ref is not None and continuation_capable:
                self.repository.suspend_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    continuation_ref=continuation_ref.to_dict(),
                    status="interrupted",
                )
            else:
                self.repository.finish_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    status="failed",
                )
            if completion_stalled:
                raise PermanentEffectError(summary)
            raise RuntimeError(summary)
        assignment_after_process = self.repository.read_role_assignment(
            str(assignment["assignment_id"])
        )
        if assignment_after_process is None or assignment_after_process["state"] not in {
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        }:
            with contextlib.suppress(Exception):
                self.repository.release_lease(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                )
            continuation_ref = self._publish_agent_session_checkpoint(
                invocation_id,
                assignment_lease.fencing_token,
                continuation_output_path,
            )
            if continuation_ref is not None and continuation_capable:
                self.repository.suspend_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    continuation_ref=continuation_ref.to_dict(),
                    status="interrupted",
                )
            else:
                self.repository.finish_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    status="failed",
                )
            raise SubmissionInvariantError(
                "role executor reported completion before its durable submission receipt"
            )
        with contextlib.suppress(Exception):
            self.repository.release_lease(
                assignment_lease_resource,
                str(attempt["attempt_id"]),
                assignment_lease.fencing_token,
            )
        terminal = self._terminal_from_assignment_receipt(
            assignment_after_process,
            primary_artifact_name=_role_primary_artifact_name(pack),
            summary=str(dict(terminal.get("payload") or {}).get("summary") or "Role submission recorded."),
            original_terminal=terminal,
        )
        terminal_payload = dict(terminal.get("payload") or {})
        continuation_ref = self._publish_agent_session_checkpoint(
            invocation_id,
            assignment_lease.fencing_token,
            continuation_output_path,
        )
        terminal_payload["v2_timing"] = _worker_event_timing(events)
        if continuation_ref is not None:
            continuation_payload = self.service.artifacts.read_json(continuation_ref)
            terminal_payload["session_turn_index"] = int(continuation_payload.get("llm_round_count") or 0)
        terminal = {**terminal, "payload": terminal_payload}
        terminal_ref = self.service.artifacts.put_json(
            terminal,
            artifact_type="RoleTerminalArtifact",
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
        if continuation_capable:
            if continuation_ref is None:
                raise RuntimeError("resumable role completed without a durable agent-session checkpoint")
            self.repository.suspend_role_invocation(
                invocation_id=invocation_id,
                fencing_token=fencing_token,
                continuation_ref=continuation_ref.to_dict(),
            )
        else:
            self.repository.finish_role_invocation(
                invocation_id=invocation_id,
                fencing_token=fencing_token,
                status="completed",
            )
        return terminal, prompt_ref, terminal_ref

    def _durable_assignment_prompt_ref(
        self,
        assignment: Mapping[str, Any],
    ) -> ArtifactRef | None:
        attempt_id = str(assignment.get("active_attempt_id") or "")
        if not attempt_id:
            return None
        attempt = self.repository.read_role_attempt(attempt_id)
        prompt_value = dict((attempt or {}).get("prompt_pack_ref") or {})
        if not prompt_value.get("sha256"):
            return None
        prompt_ref = _ref_from_mapping(prompt_value)
        if self.repository.read_artifact_record(prompt_ref.sha256) is None:
            return None
        return prompt_ref

    def _terminal_from_assignment_receipt(
        self,
        assignment: Mapping[str, Any],
        *,
        primary_artifact_name: str,
        summary: str,
        original_terminal: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        artifact_ref = dict(assignment.get("submission_artifact_ref") or {})
        if not artifact_ref:
            raise SubmissionInvariantError("role assignment has no submission artifact")
        submitted = self.service.artifacts.read_json(artifact_ref)
        if stable_hash(submitted) != str(assignment.get("submission_payload_hash") or ""):
            raise SubmissionInvariantError(
                "role assignment submission payload hash does not match its artifact"
            )
        record = self.repository.read_artifact_record(str(artifact_ref.get("sha256") or ""))
        if record is None:
            raise SubmissionInvariantError("role assignment submission artifact is unavailable")
        primary = {
            "path": str(record["storage_path"]),
            "relative_path": primary_artifact_name,
            "title": "Durable role submission",
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
                "durable_receipt_replay": True,
                "session_turn_index": int(
                    original_payload.get("session_turn_index") or 0
                ),
                "v2_timing": dict(original_payload.get("v2_timing") or {}),
            },
        }

    def _prepare_agent_session_attempt(
        self,
        *,
        session_id: str,
        attempt_id: str,
    ) -> tuple[Path | None, Path]:
        session = self.repository.read_role_session(session_id)
        if session is None:
            raise RuntimeError(f"role session disappeared before process start: {session_id}")
        attempt_dir = (
            invocation_root(self.service.runtime_root)
            / session_id
            / "session-attempts"
            / _safe_component(attempt_id)
        )
        attempt_dir.mkdir(parents=True, exist_ok=True)
        restore_path = attempt_dir / "continuation-input.json"
        checkpoint_path = attempt_dir / "continuation-output.json"
        with contextlib.suppress(FileNotFoundError):
            restore_path.unlink()
        with contextlib.suppress(FileNotFoundError):
            checkpoint_path.unlink()

        continuation_ref = dict(session.get("continuation_ref") or {})
        if not continuation_ref:
            return None, checkpoint_path
        if str(continuation_ref.get("artifact_type") or "") != "AgentSessionContinuationArtifact":
            raise RuntimeError("role session continuation has the wrong artifact type")
        payload = self.service.artifacts.read_json(continuation_ref)
        if not isinstance(payload, Mapping):
            raise RuntimeError("role session continuation is not a JSON object")
        restored = dict(payload)
        if str(restored.get("session_id") or "") != session_id:
            raise RuntimeError("role session continuation has the wrong session identity")
        scope_kind = str(session.get("scope_kind") or "")
        subject_key = str(session.get("subject_key") or "")
        stored_scope = str(restored.get("scope_kind") or "")
        stored_subject = str(restored.get("subject_key") or "")
        if stored_scope and stored_scope != scope_kind:
            raise RuntimeError("role session continuation has the wrong scope")
        if stored_subject and stored_subject != subject_key:
            raise RuntimeError("role session continuation has the wrong subject")
        restored.update(
            {
                "schema_version": "2",
                "scope_kind": scope_kind,
                "subject_key": subject_key,
            }
        )
        temporary = restore_path.parent / f".{restore_path.name}.{os.getpid()}.tmp"
        temporary.write_text(
            json.dumps(restored, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
        os.replace(temporary, restore_path)
        return restore_path, checkpoint_path

    def _publish_agent_session_checkpoint(
        self,
        invocation_id: str,
        fencing_token: int,
        checkpoint_path: Path,
    ) -> ArtifactRef | None:
        if not checkpoint_path.is_file():
            return None
        try:
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("worker checkpoint output is unreadable") from exc
        if not isinstance(payload, dict) or str(payload.get("session_id") or "") != invocation_id:
            raise RuntimeError("worker checkpoint output has the wrong session identity")
        if str(payload.get("schema_version") or "") != "2":
            raise RuntimeError("worker checkpoint output has an unsupported schema version")
        if int(payload.get("fencing_token") or 0) != int(fencing_token):
            raise RuntimeError("worker checkpoint output has a stale fencing token")
        session = self.repository.read_role_session(invocation_id)
        if session is None:
            raise RuntimeError("worker checkpoint output has no durable session")
        if str(payload.get("scope_kind") or "") != str(session.get("scope_kind") or ""):
            raise RuntimeError("worker checkpoint output has the wrong scope")
        if str(payload.get("subject_key") or "") != str(session.get("subject_key") or ""):
            raise RuntimeError("worker checkpoint output has the wrong subject")
        return self.service.artifacts.put_json(
            payload,
            artifact_type="AgentSessionContinuationArtifact",
            provenance={"invocation_id": invocation_id, "fencing_token": int(fencing_token)},
        )

    def _profile_for_role(self, workflow_id: str, role: str) -> str:
        role = OrchestrationRole(str(role)).value
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            raise ValueError(f"workflow not found while resolving role {role}: {workflow_id}")
        binding_ref = dict(workflow.payload.get("family_binding_ref") or {})
        if not binding_ref:
            raise ValueError(f"workflow has no FamilyBindingArtifact: {workflow_id}")
        binding = dict(self.service.artifacts.read_json(binding_ref))
        role_binding = dict(dict(binding.get("role_bindings") or {}).get(role) or {})
        profile = str(
            dict(role_binding.get("executor_profile") or {}).get("canonical_profile_id")
            or dict(role_binding.get("executor_profile") or {}).get("minion_profile")
            or ""
        ).strip()
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

    def _workflow_policy(self, workflow_id: str, name: str) -> dict[str, Any]:
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            return {}
        binding_ref = dict(workflow.payload.get("family_binding_ref") or {})
        if not binding_ref:
            return {}
        binding = dict(self.service.artifacts.read_json(binding_ref))
        return dict(dict(binding.get("policies") or {}).get(name) or {})

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
            action_type = "ARCHITECTURE_REVIEW_FAILED"
            payload = {
                "finding_artifact_ref": review_ref.to_dict(),
                "findings": [item.to_dict() for item in review.findings],
            }
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
            **self._role_submission_settlement(effect or {}),
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
                "changed_paths": _verification_workspace_changed_paths(
                    review_worktree,
                    candidate_digest,
                ),
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
        "Design the requested software architecture in the bound writable worktree. The exact task sources are immutable product truth. "
        "Write contract-level code skeletons, an acyclic semantic Contract Dependency Graph, and one or more meaningful end-to-end scenarios. "
        "All implementation Coders may start from the accepted protocols; contract dependencies describe semantic consumption, not scheduling barriers. "
        "A scenario names the exact implementation modules, real entrypoint, observable behavior, and environment it verifies, but owns no product source. "
        "A universal all-module scenario is forbidden unless a real product entrypoint requires that exact combination. "
        "Do not implement behavior, algorithms, mapping tables, SDK call sequences, or complete tests."
    )
    if has_base_manifest:
        instruction += (
            " This is a revision based on the existing skeleton. Modify only locations named by revision_finding or the explicit edit instruction; "
            "preserve every unrelated declaration, contract, path scope, and dependency. The semantic DAG Draft is already seeded from "
            "the accepted baseline: do not remove, recreate, or restate unchanged modules. A source-only contract "
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


def _skeleton_architecture_review_view(
    artifact: Mapping[str, Any],
) -> dict[str, Any]:
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


def _verification_workspace_from_prompt_pack(
    *,
    artifacts: ContentAddressedArtifactStore,
    prompt_ref: ArtifactRef | Mapping[str, Any],
) -> tuple[Path, Path]:
    """Resolve the isolated workspace actually bound to a verifier process."""

    prompt_pack = artifacts.read_json(prompt_ref)
    workspace = dict(prompt_pack.get("workspace") or {})
    if not bool(workspace.get("v2_role_workspace")):
        raise SubmissionInvariantError(
            "verifier prompt pack is not bound to an isolated role workspace"
        )
    review_workspace = Path(str(workspace.get("repo_path") or ""))
    review_scratch = Path(str(workspace.get("review_scratch_dir") or ""))
    if not review_workspace.is_dir():
        raise SubmissionInvariantError(
            "verifier prompt pack references an unavailable role workspace"
        )
    if not review_scratch.is_dir():
        raise SubmissionInvariantError(
            "verifier prompt pack references an unavailable review scratch directory"
        )
    return review_workspace, review_scratch


def _verification_workspace_changed_paths(
    review_worktree: Path,
    candidate_digest: str,
) -> list[str]:
    """Return the verifier-authored delta relative to the immutable candidate."""

    return git_changed_paths(review_worktree, candidate_digest)


def _verification_scratch_paths(review_scratch: Path) -> list[str]:
    if not review_scratch.is_dir():
        return []
    return [
        f"review_scratch/{path.relative_to(review_scratch).as_posix()}"
        for path in sorted(
            item for item in review_scratch.rglob("*") if item.is_file() and not item.is_symlink()
        )
    ]


def _semantic_path_scope_matches(path: str, scope: Mapping[str, Any]) -> bool:
    normalized = str(path).replace(os.sep, "/").strip("/")
    target = str(scope.get("path") or "").replace(os.sep, "/").strip("/")
    if not target:
        return False
    kind = str(scope.get("kind") or "").strip().lower()
    if kind == "file":
        return normalized == target
    if kind == "directory":
        return normalized == target or normalized.startswith(target + "/")
    return False


def _named_json_output(terminal: Mapping[str, Any], filename: str) -> dict[str, Any]:
    payload = dict(terminal.get("payload") or {})
    artifacts = [dict(item) for item in list(payload.get("artifacts") or []) if isinstance(item, Mapping)]
    artifact = next(
        (
            item
            for item in artifacts
            if filename
            in {
                Path(str(item.get("relative_path") or "")).name,
                Path(str(item.get("path") or "")).name,
            }
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


def _recorded_role_metrics(terminal: Mapping[str, Any]) -> dict[str, int]:
    timing = dict(dict(terminal.get("payload") or {}).get("v2_timing") or {})
    return {
        "latency_ms": max(0, int(timing.get("llm_time_ms") or 0)),
        "tool_latency_ms": max(0, int(timing.get("tool_time_ms") or 0)),
        "wall_latency_ms": max(0, int(timing.get("worker_time_ms") or 0)),
    }


def _role_session_turn_index(terminal: Mapping[str, Any]) -> int:
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


def _role_primary_artifact_name(pack: MinionInvocationPack) -> str:
    output_policy = dict((pack.workspace or {}).get("output_policy") or {})
    if not output_policy:
        output_policy = dict(
            (pack.resolved_profile or {}).get("effective_output_policy") or {}
        )
    primary_artifact = str(output_policy.get("primary_artifact") or "").strip()
    if not primary_artifact:
        raise ValueError("role invocation has no primary output artifact contract")
    return primary_artifact


def apply_v2_role_capability_policy(
    pack: MinionInvocationPack,
    *,
    activation: RoleActivation,
) -> MinionInvocationPack:
    current = set(pack.allowed_capabilities)
    skeleton_authoring = False
    if activation.role == OrchestrationRole.ARCHITECT:
        skeleton_authoring = bool(
            current.intersection(
                set(ARCHITECTURE_SKELETON_CAPABILITIES)
                - {"op_minion_architecture_ask_user"}
            )
        )
        allowed_authoring = (
            set(ARCHITECTURE_SKELETON_CAPABILITIES)
            if skeleton_authoring
            else set(ARCHITECT_BUILDER_CAPABILITIES)
        )
    elif activation == RoleActivation(OrchestrationRole.REVIEWER, RoleMode.ARCHITECTURE):
        allowed_authoring = (
            set(SKELETON_REVIEW_CAPABILITIES)
            if current.intersection(SKELETON_REVIEW_CAPABILITIES)
            else set(ARCHITECTURE_REVIEW_BUILDER_CAPABILITIES)
        )
    else:
        allowed_authoring = {
            OrchestrationRole.VERIFIER: (
                {*SWE_VERIFICATION_CAPABILITIES, "op_minion_verification_scratch_write"}
                if current.intersection(SWE_VERIFICATION_CAPABILITIES)
                else set(VERIFICATION_BUILDER_CAPABILITIES)
            ),
            OrchestrationRole.REVIEWER: set(STANDALONE_REVIEW_BUILDER_CAPABILITIES),
            OrchestrationRole.IMPLEMENTATION: set(CANDIDATE_BUILDER_CAPABILITIES),
        }.get(activation.role)
    if allowed_authoring is None:
        return pack
    current.update(allowed_authoring)
    forbidden_writes = {
        "op_minion_artifact_write",
        "op_minion_artifact_edit",
    }
    if activation.role in {OrchestrationRole.ARCHITECT, OrchestrationRole.REVIEWER}:
        forbidden_writes.update({"op_file_write", "op_file_edit", "op_path_delete"})
    if activation.role == OrchestrationRole.ARCHITECT and current.intersection(
        set(ARCHITECTURE_SKELETON_CAPABILITIES)
        - {"op_minion_architecture_ask_user"}
    ):
        forbidden_writes.difference_update({"op_file_write", "op_file_edit", "op_path_delete"})
    if (
        activation.role == OrchestrationRole.IMPLEMENTATION
        and str(pack.profile_group or "") != "software_engineering"
    ):
        forbidden_writes.difference_update(
            {"op_minion_artifact_write", "op_minion_artifact_edit"}
        )
    capabilities = [
        capability
        for capability in sorted(current)
        if capability not in forbidden_writes
        and (not _is_authoring_capability_name(capability) or capability in allowed_authoring)
    ]
    if activation.role == OrchestrationRole.ARCHITECT:
        primary_artifact = (
            "architecture_submission.json"
            if skeleton_authoring
            else "architecture_bundle.json"
        )
        allowed_output_types = (
            ["ArchitectureSkeletonSubmission"]
            if skeleton_authoring
            else ["ArchitecturePlanningStageOutput"]
        )
    elif activation == RoleActivation(
        OrchestrationRole.REVIEWER,
        RoleMode.ARCHITECTURE,
    ):
        primary_artifact = "architecture_review.json"
        allowed_output_types = ["ArchitectureReviewStageOutput"]
    elif activation.role == OrchestrationRole.REVIEWER:
        primary_artifact = "standalone_review.json"
        allowed_output_types = ["StandaloneReviewReport"]
    elif activation.role == OrchestrationRole.VERIFIER:
        primary_artifact = "verification_submission.json"
        allowed_output_types = ["SemanticVerificationSubmissionArtifact"]
    else:
        software_implementation = str(pack.profile_group or "") == "software_engineering"
        primary_artifact = (
            "coder_report.json" if software_implementation else "producer_report.json"
        )
        allowed_output_types = (
            ["ModuleCoderReport", "ModuleSplitRequest"]
            if software_implementation
            else ["UnitProducerReport", "UnitSplitRequest"]
        )

    pack_value = pack.to_dict()
    workspace = dict(pack_value.get("workspace") or {})
    output_policy = dict(workspace.get("output_policy") or {})
    output_policy.update(
        {
            "primary_artifact": primary_artifact,
            "allowed_output_types": allowed_output_types,
        }
    )
    workspace["output_policy"] = output_policy
    resolved_profile = dict(pack_value.get("resolved_profile") or {})
    resolved_profile["effective_output_policy"] = dict(output_policy)
    return MinionInvocationPack.from_dict(
        {
            **pack_value,
            "allowed_capabilities": capabilities,
            "workspace": workspace,
            "resolved_profile": resolved_profile,
        }
    )


def _is_authoring_capability_name(name: str) -> bool:
    value = str(name or "")
    return value == "op_minion_add_finding" or value.startswith(
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


def _parse_architecture_review(payload: Mapping[str, Any]) -> SkeletonReviewResult:
    verdict = str(payload.get("verdict") or "").strip().upper()
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("architecture review verdict must be PASS or FAIL")
    findings = tuple(
        SkeletonReviewFinding(
            finding_key=str(item["finding_key"]),
            finding_kind=str(item["finding_kind"]),
            priority=str(item["priority"]),
            summary=str(item["summary"]),
            locations=tuple(dict(location) for location in list(item.get("locations") or [])),
        )
        for item in structured_findings(payload)
    )
    if verdict == "PASS" and findings:
        raise ValueError("PASS architecture review cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL architecture review requires typed findings")
    return SkeletonReviewResult(verdict=verdict, findings=findings)


def _parse_skeleton_review(payload: Mapping[str, Any]) -> SkeletonReviewResult:
    verdict = str(payload.get("verdict") or "").strip().upper()
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("architecture skeleton review verdict must be PASS or FAIL")
    raw_findings = structured_findings(payload)
    findings = tuple(
        SkeletonReviewFinding(
            finding_key=str(item["finding_key"]),
            finding_kind=str(item["finding_kind"]),
            priority=str(item["priority"]),
            summary=str(item["summary"]),
            locations=tuple(dict(location) for location in list(item.get("locations") or [])),
        )
        for item in raw_findings
    )
    if verdict == "PASS" and findings:
        raise ValueError("PASS architecture review cannot contain findings")
    if verdict == "FAIL" and not findings:
        raise ValueError("FAIL architecture review requires findings")
    return SkeletonReviewResult(verdict=verdict, findings=findings)


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
    mode: str,
    draft_kind: str,
) -> list[VerificationCaseResult]:
    internal = dict(plan.get("internal_context") or {})
    evidence_invocation_id = str(internal.get("invocation_id") or "").strip()
    if not evidence_invocation_id:
        raise ValueError("recorded verification evidence has no fenced invocation")
    input_fingerprint = str(internal.get("input_fingerprint") or "").strip()
    if not input_fingerprint:
        raise ValueError("recorded verification evidence has no bound input fingerprint")
    draft_key = str(internal.get("draft_key") or "").strip()
    if not draft_key:
        raise ValueError("recorded verification evidence has no Draft binding")
    durable = SubmissionDraftStore(runtime_root).read_submitted(draft_key)
    if (
        durable.workflow_id != workflow_id
        or durable.invocation_id != evidence_invocation_id
        or durable.role != role
        or durable.mode != mode
        or durable.draft_kind != draft_kind
        or durable.input_fingerprint != input_fingerprint
        or durable.fencing_token != int(internal.get("fencing_token") or 0)
    ):
        raise ValueError("recorded verification evidence Draft binding is invalid")
    if evidence_invocation_id != invocation_id:
        repository = MinionV2Repository(runtime_root)
        attempt = repository.read_role_attempt(evidence_invocation_id)
        assignment = (
            repository.read_role_assignment(str(attempt.get("assignment_id") or ""))
            if attempt is not None
            else None
        )
        if (
            attempt is None
            or assignment is None
            or str(assignment.get("session_id") or "") != invocation_id
            or str(assignment.get("workflow_id") or "") != workflow_id
            or str(assignment.get("role") or "") != role
            or str(assignment.get("input_fingerprint") or "") != input_fingerprint
            or str(attempt.get("lease_resource_key") or "")
            != durable.lease_resource_key
            or int(attempt.get("fencing_token") or 0) != durable.fencing_token
        ):
            raise ValueError(
                "recorded verification evidence is not owned by the current logical role session"
            )
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
        if case_id in described or result.status != VerificationStatus.FAIL:
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


def _semantic_contract_review_view(
    contract: Mapping[str, Any],
    requirements: Mapping[str, Any],
) -> dict[str, Any]:
    del requirements

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
    *,
    work_view: Mapping[str, Any],
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
    required_historical = historical_repair_checklist_items(work_view)
    if required_historical:
        historical_status = {
            str(item.get("name") or ""): str(item.get("status") or "")
            for item in list(plan.get("recorded_results") or [])
            if str(dict(item or {}).get("case_kind") or "") == "historical_regression"
        }
        missing = [
            str(item["case"])
            for item in required_historical
            if str(item["case"]) not in historical_status
        ]
        if missing:
            raise ValueError(
                "verification must replay every historical RepairBill case before submit: "
                + ", ".join(missing)
            )
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
    del cases
    return structured_findings(plan)


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
        severity = str(finding.get("priority") or "p1").upper()
        section = str(finding.get("finding_kind") or "finding")
        finding_summary = str(finding.get("summary") or "Finding").strip()
        lines.extend(("", f"### {index}. [{severity}] {finding_summary}", f"- Area: {section}"))
        lines.append(f"- Key: {str(finding.get('finding_key') or '')}")
        for location in list(finding.get("locations") or []):
            item = dict(location or {})
            label = str(item.get("file") or "") + f":{int(item.get('line') or 1)}"
            if item.get("symbol"):
                label += f"::{str(item['symbol'])}"
            lines.append(f"- Location: {label}")

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
            for dependency in list(item.payload.get("contract_dependency_node_ids") or [])
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


def _raise_if_workspace_held(
    workspace: Path,
    message: str,
    *,
    manager_snapshot_lock: Path | None = None,
) -> None:
    holders = workspace_process_holders(workspace)
    if manager_snapshot_lock is not None:
        try:
            lock_path = manager_snapshot_lock.resolve().relative_to(workspace.resolve()).as_posix()
        except ValueError:
            lock_path = ""
        if lock_path:
            holders = tuple(
                holder
                for holder in holders
                if not (
                    holder.pid == os.getpid()
                    and not holder.holds_cwd
                    and bool(holder.read_paths or holder.write_paths or holder.unknown_paths)
                    and all(
                        path == lock_path
                        for path in (
                            *holder.read_paths,
                            *holder.write_paths,
                            *holder.unknown_paths,
                        )
                    )
                )
            )
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
