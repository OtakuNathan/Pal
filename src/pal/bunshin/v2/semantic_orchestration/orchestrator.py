from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import inspect
import json
import os
import re
import signal
import shutil
import sqlite3
import subprocess
import sys
import yaml
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Mapping

from pal.bunshin.profiles import resolve_pinned_bunshin_pack
from pal.bunshin.checkpoint import (
    AgentSessionCheckpointError,
    LogicalCoroutineCheckpointStore,
    normalize_agent_session_checkpoint,
)
from pal.bunshin.harnesses import (
    HARNESS_LAUNCH_PAL_SANDBOX,
    PAL_HARNESS_ID,
    BunshinHarnessRegistryGeneration,
    BunshinHarnessRegistry,
    BunshinHarnessSpec,
)
from pal.bunshin.ipc import BUNSHIN_RUNTIME_DB_PATH_ENV, python_subprocess_env
from pal.bunshin.sandbox import build_sandboxed_runner_invocation, with_bunshin_sandbox_metadata
from pal.bunshin.tool_guidance import merge_tool_guidance_overrides
from pal.bunshin.turns import sanitize_runner_session_pack
from pal.bunshin.v2.contract_runtime import (
    ResearchMode,
)
from pal.bunshin.v2.adapters import (
    ARTIFACT_BUNDLE_ADAPTER,
    SOFTWARE_GIT_ADAPTER,
    ArtifactBundleAdapter,
    artifact_tree_fingerprint,
    prepare_v2_role_workspace,
    prepare_v2_workspace_environment,
)
from pal.bunshin.lsp_prewarm import prewarm_workspace_lsp
from pal.lsp.ipc import LspManagerClient
from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.contracts import (
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
from pal.bunshin.v2.contract_protocol import (
    CONTRACT_ARTIFACT,
    software_contract_projection,
)
from pal.bunshin.v2.contract_submission import (
    architect_path,
    bind_architect_file,
)
from pal.bunshin.v2.candidate_builder import (
    CANDIDATE_BUILDER_CAPABILITIES,
    validate_candidate_submission,
)
from pal.bunshin.v2.execution import (
    CandidateSnapshotService,
    UnitWorkViewBuilder,
    WorkspaceLockRegistry,
    git_changed_paths,
    provision_module_verification_workspace,
    format_workspace_process_holders,
    terminate_process_group,
    workspace_content_fingerprint,
    workspace_process_holders,
)
from pal.bunshin.v2.service import BunshinV2WorkflowService, workflow_request_from_snapshot
from pal.bunshin.v2.sessions import (
    architecture_cycle_id,
    architecture_reviewer_session_id,
    architect_session_id_for_revision,
    coder_session_id,
    module_name_from_payload,
    module_verifier_session_id,
    node_role_generation,
)
from pal.bunshin.v2.skeleton import (
    ARCHITECTURE_REPAIR_BASELINE_ARTIFACT,
    ArchitectureWorkspace,
    compiled_module_write_scopes,
    SemanticReferenceError,
    SkeletonReviewFinding,
    SkeletonReviewResult,
    architecture_revision_path_states,
    architecture_revision_scope,
    compile_skeleton_markdown,
    review_architecture_skeleton,
)


from pal.bunshin.v2.workspace_paths import (
    ARCHITECT_AUTHORING_RELATIVE_PATH,
    module_developer_test_path,
    module_verification_corpus_path,
)
from pal.bunshin.v2.task_ledger import (
    TASK_LEDGER_ARTIFACT,
)
from pal.bunshin.v2.swe_verification import (
    SWE_VERIFICATION_CAPABILITIES,
    compile_swe_verification_tool_contract,
    semantic_verification_submission_errors,
)
from pal.bunshin.v2.delivery import DeliveryService
from pal.bunshin.v2.paths import (
    invocation_root,
    resolve_project_git_layout,
    standalone_review_root,
    verification_scratch_root,
)
from pal.bunshin.v2.projections import PlanRevisionProjectionStore
from pal.bunshin.v2.process_lifecycle import WorkerProcessOwner
from pal.bunshin.v2.role_runtime import RoleSupervisor
from pal.bunshin.v2.cycle_protocol import AssignmentKind, CycleSlot
from pal.bunshin.v2.workflow_runtime import WorkflowCoordinator
from pal.bunshin.v2.graph_executor import FindingClass
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.machines import machine_spec_for
from pal.bunshin.v2.human_review import (
    HUMAN_REVIEW_RENDER_VERSION,
    human_review_card_is_current,
)
from pal.bunshin.v2.review_findings import structured_advisories, structured_findings
from pal.bunshin.v2.replan import (
    ARCHITECTURE_FINDING_BATCH_VIEW_ARTIFACT,
    architecture_finding_semantic_view,
    architecture_revision_finding_value,
    compile_architecture_finding_markdown,
)
from pal.bunshin.v2.verification import (
    DefectKind,
    UnknownPolicy,
    VerificationCaseKind,
    VerificationCaseResult,
    VerificationCaseSpec,
    VerificationService,
    VerificationStatus,
    aggregate_verification_status,
    historical_repair_checklist_items,
    no_progress_detected,
    repair_bill_semantic_view,
    repair_checklist_items,
    validate_verification_case_order,
)
from pal.bunshin.v2.verification_builder import (
    VERIFICATION_EVIDENCE_CAPABILITIES,
    compile_verification_invocation_tool_contract,
    dominant_verification_defect_kind,
    effective_verification_policy,
)
from pal.bunshin.v2.submission_drafts import (
    AUTHORING_CONTRACT_VERSION,
    SubmissionDraftStore,
    authoring_input_fingerprint,
)
from pal.bunshin.v2.semantic_evidence import recorded_cases
from pal.bunshin.v2.work_items import submission_work_items
from pal.bunshin.v2.role_contracts import (
    OrchestrationRole,
    RoleActivation,
    RoleMode,
    family_execution_adapter,
    role_session_stage_key,
    validate_family_binding_payload,
)
from pal.bunshin.v2.semantic_orchestration.architecture import ARCHITECTURE_EFFECT_ROUTES
from pal.bunshin.v2.semantic_orchestration.contracts import (
    SemanticEffectRoute,
    merge_effect_routes,
)
from pal.bunshin.v2.semantic_orchestration.implementation import IMPLEMENTATION_EFFECT_ROUTES
from pal.bunshin.v2.semantic_orchestration.review import REVIEW_EFFECT_ROUTES
from pal.bunshin.v2.semantic_orchestration.verification import VERIFICATION_EFFECT_ROUTES
from pal.bunshin.ipc import ROLE_GATEWAY_TOKEN_ENV
from pal.bunshin.v2.role_gateway import role_submission_artifact_type
from pal.bunshin.v2.role_protocol import (
    RoleAttemptState,
    RoleAssignmentRequest,
    RoleAssignmentState,
    canonical_role_profile_parts,
    stable_hash,
)
from pal.shared import BunshinInvocationPack


_ROLE_FAILURE_ATTEMPT_LIMIT = 3
_UNCHARGED_ROLE_ATTEMPT_ERROR_KINDS = frozenset(
    {
        "manager_restart",
        "manager_shutdown",
    }
)


def _charged_role_failure_attempt_count(
    attempts: tuple[Mapping[str, Any], ...] | list[Mapping[str, Any]],
) -> int:
    """Count failures, not process shells, against the logical role budget."""

    charged = 0
    for attempt in attempts:
        status = str(attempt.get("status") or "")
        if status == RoleAttemptState.FAILED.value:
            charged += 1
            continue
        if status != RoleAttemptState.LOST.value:
            continue
        if str(attempt.get("error_kind") or "") in _UNCHARGED_ROLE_ATTEMPT_ERROR_KINDS:
            continue
        charged += 1
    return charged


HumanReviewPublisher = Callable[[Mapping[str, Any]], Awaitable[None]]
WorkerEventPublisher = Callable[[Mapping[str, Any]], Awaitable[None]]
WorkflowEventPublisher = Callable[[Mapping[str, Any]], None]
BrokerRunRegistrar = Callable[[str, str, BunshinInvocationPack, asyncio.subprocess.Process], None]
BrokerRunUnregistrar = Callable[[str, bool], None]
SkillInjector = Callable[[str], Mapping[str, str]]


EPHEMERAL_ROLE_INPUT_NAMES = frozenset({"workspace_preparation"})


def _role_input_is_semantic(name: str, *, role: str, mode: str = "") -> bool:
    # Workspace preparation is an attempt-local observation. It may contain
    # paths, scanned-file counts, and optional LSP observations that naturally
    # change when a verifier writes a corpus case or a process is restarted.
    # Those changes must not create a new logical assignment or invalidate the
    # durable role Draft. Evidence records carry their own environment
    # fingerprint when that distinction matters.
    return str(name) not in EPHEMERAL_ROLE_INPUT_NAMES


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
    producer_report_value = node_payload.get("producer_report_ref")
    if isinstance(producer_report_value, Mapping) and producer_report_value.get("sha256"):
        references["coder_report"] = _ref_from_mapping(producer_report_value)
    return references



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
    base_sha = str(
        candidate.get("base_sha")
        or candidate.get("previous_head_sha")
        or ""
    )
    if not base_sha or not candidate_digest:
        raise ValueError("module verifier requires a complete Git review range")
    if _git_output(review_worktree, "rev-parse", "HEAD") != candidate_digest:
        raise ValueError("module verifier worktree is not at the review target")
    review_range = artifacts.put_json(
        {
            "schema_version": "1",
            "base_sha": base_sha,
            "target_sha": candidate_digest,
            "instruction": (
                "Use git log/show/diff in the bound Module worktree. Git is the "
                "only source of truth for changed code and tests."
            ),
        },
        artifact_type="GitReviewRangeArtifact",
        provenance={"owner": "manager", "audience": "verifier"},
        child_refs=(
            (candidate_ref.sha256, "checkpoint"),
            (architecture_ref.sha256, "accepted_skeleton"),
        ),
    )
    return {"candidate_diff": review_range}


def _role_session_scope(
    snapshot: AggregateSnapshot,
    activation: RoleActivation,
) -> tuple[str, str]:
    if snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION and activation.role in {
        OrchestrationRole.ARCHITECT,
        OrchestrationRole.REVIEWER,
    }:
        return (
            "architecture_cycle",
            architecture_cycle_id(snapshot.aggregate_id, snapshot.payload),
        )
    if activation.role == OrchestrationRole.IMPLEMENTATION:
        return "module", module_name_from_payload(snapshot.payload)
    if activation.role == OrchestrationRole.VERIFIER:
        return "module", module_name_from_payload(snapshot.payload)
    return snapshot.aggregate_type.value, str(snapshot.aggregate_id)


def _node_role_session_id(
    node: AggregateSnapshot,
    activation: RoleActivation,
) -> str:
    generation = node_role_generation(node.payload)
    if activation.role == OrchestrationRole.IMPLEMENTATION:
        return coder_session_id(
            node.workflow_id,
            module_name_from_payload(node.payload),
            generation,
        )
    return module_verifier_session_id(
        node.workflow_id,
        module_name_from_payload(node.payload),
        generation,
    )


def _role_mode_profile_payload(
    profile_payload: Mapping[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    """Compile one role-mode profile before it enters the immutable prompt pack."""

    compiled = dict(profile_payload)
    mode_fragments = dict(
        dict(compiled.get("metadata") or {}).get("mode_fragments") or {}
    )
    fragment = dict(mode_fragments.get(str(mode)) or {})
    for name in (
        "identity_fragment",
        "behavior_fragment",
        "output_contract_fragment",
    ):
        value = str(fragment.get(name) or "").strip()
        if value:
            compiled[name] = value
    return compiled


def _role_uses_bound_durable_workspace(
    role: str,
    workspace: Mapping[str, Any],
) -> bool:
    repo_path = str(workspace.get("repo_path") or workspace.get("workspace_path") or "").strip()
    binding = str(workspace.get("workspace_binding") or "").strip().lower()
    if binding not in {"canonical", "ephemeral_artifact"}:
        return False
    return bool(repo_path) and binding == "canonical"


def _workflow_skill_injections(
    request: Mapping[str, Any],
    inject_skill: SkillInjector | None,
) -> list[dict[str, str]]:
    skill_refs = [
        str(item or "").strip()
        for item in list(request.get("skill_refs") or [])
        if str(item or "").strip()
    ]
    if skill_refs and inject_skill is None:
        raise PermanentEffectError("approved skill injection is unavailable in Manager")
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for skill_ref in skill_refs:
        if skill_ref in seen:
            continue
        seen.add(skill_ref)
        try:
            injected = dict(inject_skill(skill_ref)) if inject_skill is not None else {}
        except Exception as exc:
            raise PermanentEffectError(
                f"approved skill injection failed: {skill_ref} ({exc})"
            ) from exc
        skill_id = str(injected.get("skill_id") or skill_ref).strip()
        reminder = str(injected.get("system_reminder") or "").strip()
        if not reminder.startswith("<system-reminder>") or not reminder.endswith(
            "</system-reminder>"
        ):
            raise PermanentEffectError(
                f"approved skill produced no valid system reminder: {skill_ref}"
            )
        result.append(
            {
                "skill_id": skill_id,
                "system_reminder": reminder,
            }
        )
    return result


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
        BunshinInvocationPack(
            invocation_id=invocation_id,
            workspace=prepared,
        ),
        run_id=run_id,
        attempt_key=f"fence-{fencing_token}",
    )
    return dict(role_pack.workspace), uses_bound_durable_workspace


def _bind_role_attempt_sandbox(
    runtime_root: Path,
    pack: BunshinInvocationPack,
    *,
    run_id: str,
    durable_prompt_reused: bool,
) -> BunshinInvocationPack:
    """Finalize one attempt without losing a durable prompt's sandbox binds.

    A persisted prompt already contains Manager-produced host-to-sandbox
    reference bindings and projected ``/pal/references`` paths.  Sanitizing it
    again would discard the bindings while leaving the projected paths behind,
    so a retry would mistake those sandbox paths for host sources.  Fresh packs
    still pass through the normal manager-metadata sanitizer.
    """

    if durable_prompt_reused:
        if not isinstance(dict(pack.metadata or {}).get("sandbox"), dict):
            raise SubmissionInvariantError(
                "durable role prompt lost its sandbox reference binding"
            )
    else:
        pack = sanitize_runner_session_pack(pack)
    return with_bunshin_sandbox_metadata(runtime_root, pack, run_id=run_id)


def _refresh_ephemeral_role_reference_binds(
    durable_pack: BunshinInvocationPack,
    current_pack: BunshinInvocationPack,
) -> BunshinInvocationPack:
    """Point attempt-local durable prompt binds at the current attempt inputs.

    Assignment retries intentionally reuse the original semantic prompt, but
    ``workspace_preparation`` describes one concrete process attempt and is
    excluded from the assignment fingerprint.  Reusing its old host bind makes
    the projected reference advertise a new fence while still exposing the
    previous fence's scratch paths inside the sandbox.
    """

    current_references = {
        str(item.get("name") or ""): dict(item)
        for item in list(dict(current_pack.workspace or {}).get("reference_paths") or [])
        if isinstance(item, Mapping)
        and str(item.get("name") or "") in EPHEMERAL_ROLE_INPUT_NAMES
        and not bool(item.get("bound_input"))
    }
    if not current_references:
        return durable_pack

    value = durable_pack.to_dict()
    metadata = dict(value.get("metadata") or {})
    sandbox = dict(metadata.get("sandbox") or {})
    binds = [
        dict(item)
        for item in list(sandbox.get("reference_binds") or [])
        if isinstance(item, Mapping)
    ]
    refreshed: set[str] = set()
    for bind in binds:
        name = str(bind.get("name") or "")
        reference = current_references.get(name)
        if reference is None:
            continue
        bind.update(
            {
                "source_path": str(reference.get("path") or ""),
                "include": list(reference.get("include") or []),
                "required": bool(reference.get("required", True)),
            }
        )
        refreshed.add(name)
    missing = sorted(set(current_references) - refreshed)
    if missing:
        raise SubmissionInvariantError(
            "durable role prompt lost attempt-local sandbox reference bindings: "
            + ", ".join(missing)
        )
    sandbox["reference_binds"] = binds
    metadata["sandbox"] = sandbox
    return BunshinInvocationPack.from_dict({**value, "metadata": metadata})


def _workspace_tooling_from_work_view(
    work_view: Mapping[str, Any],
) -> dict[str, Any]:
    """Translate explicit accepted language context into workspace tooling."""

    context = dict(work_view.get("context") or {})
    primary_language = _canonical_lsp_language(
        str(context.get("primary_language") or context.get("language") or "")
    )
    languages = [
        language
        for language in (
            _canonical_lsp_language(str(value))
            for value in list(context.get("languages") or [])
        )
        if language
    ]
    if primary_language:
        languages.insert(0, primary_language)
    languages = list(dict.fromkeys(languages))
    tooling: dict[str, Any] = {}
    if primary_language:
        tooling.update(
            {
                "primary_language": primary_language,
                "languages": languages,
            }
        )
    explicit_standard = str(context.get("cpp_standard") or "").strip().lower()
    if re.fullmatch(
        r"(?:c\+\+(?:98|03|11|14|17|20|23|26)|c(?:89|90|99|11|17|18|23))",
        explicit_standard,
    ):
        tooling["cpp_standard"] = explicit_standard
        return tooling
    language = str(context.get("language") or "").strip().lower()
    cpp_match = re.fullmatch(r"c\+\+\s*(98|03|11|14|17|20|23|26)", language)
    if cpp_match:
        tooling["cpp_standard"] = f"c++{cpp_match.group(1)}"
        return tooling
    c_match = re.fullmatch(r"c\s*(89|90|99|11|17|18|23)", language)
    if c_match:
        tooling["cpp_standard"] = f"c{c_match.group(1)}"
    return tooling


def _canonical_lsp_language(value: str) -> str:
    """Map an accepted human language label to Pal's canonical LSP id."""

    normalized = " ".join(str(value or "").strip().lower().split())
    if not normalized:
        return ""
    if re.fullmatch(r"objective[ -]?c\+\+(?:\s*\d+)?", normalized):
        return "objective-cpp"
    if re.fullmatch(r"objective[ -]?c(?:\s*\d+)?", normalized):
        return "objective-c"
    if re.fullmatch(r"c\+\+(?:\s*(?:98|03|11|14|17|20|23|26))?", normalized):
        return "cpp"
    if re.fullmatch(r"c(?:\s*(?:89|90|99|11|17|18|23))?", normalized):
        return "c"
    without_version = re.sub(
        r"\s+(?:v(?:ersion)?\s*)?\d+(?:\.\d+)*(?:\s+(?:lts|edition))?$",
        "",
        normalized,
    )
    aliases = {
        "bash": "shell",
        "c#": "csharp",
        "c-sharp": "csharp",
        "cs": "csharp",
        "csharp": "csharp",
        "cpp": "cpp",
        "cxx": "cpp",
        "css": "css",
        "ecmascript": "javascript",
        "go": "go",
        "golang": "go",
        "html": "html",
        "java": "java",
        "javascript": "javascript",
        "js": "javascript",
        "jsx": "javascript",
        "json": "json",
        "lua": "lua",
        "objective-c": "objective-c",
        "objective-cpp": "objective-cpp",
        "python": "python",
        "py": "python",
        "rs": "rust",
        "rust": "rust",
        "sh": "shell",
        "shell": "shell",
        "shellscript": "shell",
        "typescript": "typescript",
        "ts": "typescript",
        "tsx": "typescript",
        "yaml": "yaml",
        "yml": "yaml",
    }
    return aliases.get(without_version, "")


def _role_submission_kind(activation: RoleActivation, *, contract_authoring: bool) -> str:
    del contract_authoring
    if activation.role == OrchestrationRole.ARCHITECT:
        return "contract"
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


def _select_attempt_harness(
    generation: BunshinHarnessRegistryGeneration,
    *,
    role: str,
    prior_attempts: tuple[Mapping[str, Any], ...] = (),
) -> BunshinHarnessSpec:
    preferred = generation.select(role)
    if preferred.harness_id == PAL_HARNESS_ID:
        return preferred
    failed_preferred = [
        attempt
        for attempt in prior_attempts
        if str(attempt.get("harness_id") or "") == preferred.harness_id
        and str(attempt.get("status") or "")
        in {"failed", "interrupted", "cancelled", "lost"}
    ]
    if len(failed_preferred) < 2:
        return preferred
    return next(
        spec
        for spec in generation.specs
        if spec.harness_id == PAL_HARNESS_ID and spec.supports(role)
    )


def _assignment_input_fingerprint(assignment: Mapping[str, Any]) -> str:
    """Return the immutable authoring fingerprint pinned by an assignment."""

    value = str(assignment.get("input_fingerprint") or "").strip()
    if not value:
        raise SubmissionInvariantError(
            "role assignment has no immutable input fingerprint"
        )
    return value


def _assignment_has_durable_submission(assignment: Mapping[str, Any]) -> bool:
    """Return whether a role result crossed its immutable receipt boundary."""

    return (
        str(assignment.get("state") or "")
        in {
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        }
        and bool(dict(assignment.get("submission_artifact_ref") or {}))
        and bool(str(assignment.get("submission_payload_hash") or "").strip())
    )


def _is_transient_sqlite_lock(error: BaseException) -> bool:
    return isinstance(error, sqlite3.OperationalError) and any(
        marker in str(error).lower()
        for marker in ("database is locked", "database table is locked")
    )


def _contract_submit_idempotency_key(
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
    service: BunshinV2WorkflowService
    max_parallel_workers: int = 5
    runtime_db_path: Path | None = None
    harness_registry: BunshinHarnessRegistry = field(
        default_factory=lambda: BunshinHarnessRegistry(include_pal=True)
    )
    publish_human_review: HumanReviewPublisher | None = None
    publish_worker_event: WorkerEventPublisher | None = None
    publish_workflow_event: WorkflowEventPublisher | None = None
    register_broker_run: BrokerRunRegistrar | None = None
    unregister_broker_run: BrokerRunUnregistrar | None = None
    inject_skill: SkillInjector | None = None
    prompt_log_enabled: bool = False
    _processes: dict[str, asyncio.subprocess.Process] = field(default_factory=dict, init=False)
    _process_owners: dict[str, WorkerProcessOwner] = field(default_factory=dict, init=False)
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
    _role_supervisor: RoleSupervisor = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._role_supervisor = RoleSupervisor(
            max_active_runs=max(1, int(self.max_parallel_workers))
        )

    @property
    def repository(self):
        return self.service.repository

    @property
    def active_background_count(self) -> int:
        """Return live logical tasks for graceful-drain accounting only.

        This is deliberately not execution capacity.  A durable role may be
        materialized, suspended, or waiting for the process semaphore while
        this task remains alive.  ``RoleSupervisor.active_run_count`` is the
        sole capacity projection.
        """

        return sum(not task.done() for task in self._background_workers.values())

    @property
    def active_process_count(self) -> int:
        return self._role_supervisor.active_run_count

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
        if tracked:
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
        # A process owner is removed only after its complete process group has
        # exited and broker accounting has been finalized.  This also retries a
        # cleanup that was interrupted after the leader exited.
        owners = tuple(self._process_owners.values())
        if owners:
            results = await asyncio.gather(
                *(owner.close() for owner in owners),
                return_exceptions=True,
            )
            failures = [
                result
                for result in results
                if isinstance(result, BaseException)
            ]
            if failures:
                raise RuntimeError(
                    "worker supervisor stopped before every process group was reaped"
                ) from failures[0]
        if self._process_owners or self._processes or self._run_to_invocation:
            raise RuntimeError("worker supervisor retained live process accounting")

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
        snapshot = self._effect_snapshot(effect)
        causal = self._effect_causal_context(effect)
        target_state = str(causal.get("target_state") or "")
        causal_version = int(causal.get("aggregate_version") or 0)
        if target_state and snapshot.state != target_state:
            return {
                "status": "superseded",
                "aggregate_state": snapshot.state,
                "causal_target_state": target_state,
            }
        if causal_version and snapshot.version != causal_version:
            causal_owner = str(causal.get("active_worker_id") or "")
            same_owner = bool(
                causal_owner
                and causal_owner
                == str(snapshot.payload.get("active_worker_id") or "")
                and str(causal.get("lease_resource_key") or "")
                == str(snapshot.payload.get("lease_resource_key") or "")
                and int(causal.get("fencing_token") or 0)
                == int(snapshot.payload.get("fencing_token") or 0)
            )
            if not same_owner:
                return {
                    "status": "superseded",
                    "aggregate_state": snapshot.state,
                    "aggregate_version": snapshot.version,
                    "causal_aggregate_version": causal_version,
                }
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

    def _effect_causal_context(
        self,
        effect: Mapping[str, Any],
    ) -> dict[str, Any]:
        embedded = dict(dict(effect.get("payload") or {}).get("_causal_context") or {})
        if embedded:
            return embedded
        return self.repository.read_domain_event_effect_context(
            str(effect.get("event_id") or "")
        )

    def _admit_implementation_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        mode = RoleMode(self._effect_role_mode(effect))
        if (
            mode == RoleMode.PRODUCE
            and self._role_participant_kind(
                self._effect_snapshot(effect).workflow_id,
                OrchestrationRole.IMPLEMENTATION.value,
            )
            == "null"
        ):
            return self._accept_null_execution(effect)
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

    def _accept_null_execution(
        self,
        effect: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Settle an explicitly external/human role without spawning a worker."""

        node = self._effect_snapshot(effect)
        if self._role_participant_kind(
            node.workflow_id,
            OrchestrationRole.VERIFIER.value,
        ) != "null":
            raise ValueError(
                "null implementation requires a null verifier binding"
            )
        contract_ref = _ref_from_mapping(
            node.payload.get("unit_contract_ref")
        )
        role_binding = self._role_binding(
            node.workflow_id,
            OrchestrationRole.IMPLEMENTATION.value,
        )
        receipt = {
            "schema_version": "1",
            "status": "not_applicable",
            "module_name": str(
                node.payload.get("module_name")
                or node.payload.get("unit_id")
                or ""
            ),
            "reason": str(
                role_binding.get("reason")
                or "external_human_execution"
            ),
            "contract_ref": contract_ref.to_dict(),
        }
        workspace = Path(str(node.payload.get("workspace_path") or ""))
        workspace.mkdir(parents=True, exist_ok=True)
        source_payload = dict(
            self.service.artifacts.read_json(
                (
                    node.payload.get("architecture_manifest_ref")
                    if bool(node.payload.get("graph_sink"))
                    else contract_ref
                )
            )
        )
        deliverable_contract = dict(
            source_payload.get("contract") or source_payload
        )
        (workspace / "architect.yaml").write_text(
            yaml.safe_dump(
                deliverable_contract,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        candidate_ref, candidate_digest = ArtifactBundleAdapter(
            self.service.runtime_root,
            self.service.artifacts,
        ).snapshot_candidate(
            workspace=workspace,
            reference_only_paths=(),
            unit_contract_hash=contract_ref.sha256,
            dependency_output_hashes={},
            environment_fingerprint="null-executor.v1",
        )
        verification_ref = self.service.artifacts.put_json(
            {
                **receipt,
                "verification_status": "not_applicable",
            },
            artifact_type="NullVerificationReceiptArtifact",
            provenance={"owner": "manager", "participant": "null"},
            child_refs=((candidate_ref.sha256, "candidate"),),
        )
        graph_contract_hash = str(
            node.payload.get("graph_contract_hash") or ""
        )
        if not graph_contract_hash:
            raise ValueError(
                "null execution node has no GraphIR contract hash"
            )
        module_name = str(
            node.payload.get("module_name")
            or node.payload.get("unit_id")
            or ""
        )
        coordinator = WorkflowCoordinator(self.repository)
        with self.repository.transaction() as connection:
            accepted = self.repository.dispatch(
                ActionEnvelope(
                    action_type="ACCEPT_NULL_EXECUTION",
                    workflow_id=node.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="bunshin-v2-manager",
                    expected_version=node.version,
                    idempotency_key=(
                        f"effect:{effect['effect_key']}:null-execution"
                    ),
                    payload={
                        "candidate_ref": candidate_ref.to_dict(),
                        "candidate_digest": candidate_digest,
                        "verification_artifact_ref": (
                            verification_ref.to_dict()
                        ),
                        "graph_contract_hash": graph_contract_hash,
                        "output_hashes": {},
                        "null_execution": True,
                    },
                ),
                _connection=connection,
            ).snapshot
            coordinator.accept_null_node(
                workflow_id=node.workflow_id,
                node_name=module_name,
                product_ref=candidate_ref.sha256,
                input_fingerprint=str(effect["effect_key"]),
                _connection=connection,
            )
        return {
            "status": "accepted",
            "node_run_id": accepted.aggregate_id,
            "result_artifact_ref": candidate_ref.to_dict(),
        }

    async def _run_implementation_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        mode = RoleMode(self._effect_role_mode(effect))
        return await self._run_implementation(effect, repair=mode == RoleMode.REPAIR)

    def _admit_verifier_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        mode = RoleMode(self._effect_role_mode(effect))
        if mode != RoleMode.MODULE:
            raise ValueError(f"unsupported verifier mode: {mode.value}")
        return self._admit_node_worker(
            effect,
            action_type="START_REVIEW",
            activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
        )

    async def _run_verification_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if RoleMode(self._effect_role_mode(effect)) != RoleMode.MODULE:
            raise ValueError("verifier role requires a module node cycle")
        return await self._run_verification(effect)

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
        ready = self._assignment_ready_events.setdefault(effect_key, asyncio.Event())
        task = asyncio.create_task(
            self._background_worker_loop(effect, runner),
            name=f"bunshin-v2-assignment-{hashlib.sha256(effect_key.encode()).hexdigest()[:12]}",
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
                    if not isinstance(exc, DeferredEffectError):
                        startup_failure = self._settle_background_startup_failure(
                            effect,
                            exc,
                        )
                        if startup_failure is not None:
                            return startup_failure
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
                charged_failures = _charged_role_failure_attempt_count(attempts)
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
                    and max(charged_failures, supervisor_failures)
                    < _ROLE_FAILURE_ATTEMPT_LIMIT
                    and not permanent
                ):
                    # A recorded submission is already the durable LLM result.
                    # Replay the role wrapper so it can reconcile the same
                    # receipt with its business Action; _run_profile will not
                    # invoke the model again for this assignment state.
                    self._release_background_business_lease(effect)
                    await asyncio.sleep(5.0)
                    continue
                if (
                    _assignment_has_durable_submission(assignment)
                    and _is_transient_sqlite_lock(exc)
                ):
                    # The model output is already durable and recovery can
                    # replay it without another LLM turn.  A transient
                    # repository lock after that boundary is reconciliation
                    # debt, not evidence that the role failed.
                    self._release_background_business_lease(effect)
                    return {
                        "provider_request_id": assignment_id,
                        "status": "reconciliation_deferred",
                    }
                self._release_background_business_lease(effect)
                return self._settle_background_role_failure(
                    effect,
                    assignment,
                    exc,
                    exhausted=not permanent,
                )

    def _settle_background_startup_failure(
        self,
        effect: Mapping[str, Any],
        error: Exception,
    ) -> Mapping[str, Any] | None:
        """Triage a role that entered running state before assignment durability.

        Retrying the original outbox effect is unsafe after its business state
        has advanced: the causal guard will correctly supersede the replay, but
        that would otherwise strand the aggregate in a worker-owned state with
        no durable executor.  Once ``ROLE_FAILED`` is legal, record the startup
        failure on the aggregate itself and expose it to ordinary triage.
        """

        route = SEMANTIC_EFFECT_ROUTES.get(str(effect.get("effect_type") or ""))
        role = route.role.value if route is not None and route.role is not None else ""
        error_text = f"{error.__class__.__name__}: {error}"
        failure_payload = {
            "kind": "role_startup_failed",
            "role": role,
            "attempt_count": 1,
            "error_kind": "pre_assignment_failure",
            "error": error_text,
            "effect_type": str(effect.get("effect_type") or ""),
        }
        failure_ref = self.service.artifacts.put_json(
            failure_payload,
            artifact_type="RoleAssignmentFailureArtifact",
        )
        for _attempt in range(3):
            snapshot = self._effect_snapshot(effect)
            if snapshot.state == "TRIAGE_REQUIRED":
                self._release_background_business_lease(effect)
                return {
                    "provider_request_id": str(
                        effect.get("effect_key") or effect.get("effect_id") or ""
                    ),
                    "status": "triage_required",
                }
            if "ROLE_FAILED" not in self.repository.engine.legal_actions(
                snapshot.aggregate_type,
                snapshot.state,
            ):
                return None
            try:
                with self.repository.transaction() as connection:
                    self.repository.dispatch(
                        ActionEnvelope(
                            action_type="ROLE_FAILED",
                            workflow_id=snapshot.workflow_id,
                            aggregate_type=snapshot.aggregate_type,
                            aggregate_id=snapshot.aggregate_id,
                            actor="bunshin-v2-worker-supervisor",
                            expected_version=snapshot.version,
                            idempotency_key=(
                                f"worker-startup-failed:"
                                f"{str(effect.get('effect_key') or effect.get('effect_id') or '')}:"
                                f"generation-{snapshot.version}"
                            ),
                            payload={
                                "failure_artifact_ref": failure_ref.to_dict(),
                                "blocker": {
                                    "kind": "role_startup_failure",
                                    "summary": error_text,
                                    "role": role,
                                    "attempt_count": 1,
                                },
                            },
                        ),
                        _connection=connection,
                    )
                    self._require_cycle_triage(snapshot, _connection=connection)
                self._release_background_business_lease(effect)
                return {
                    "provider_request_id": str(
                        effect.get("effect_key") or effect.get("effect_id") or ""
                    ),
                    "status": "triage_required",
                }
            except AggregateVersionConflict:
                continue
        raise DeferredEffectError(
            "role startup failure settlement lost repeated CAS races"
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

    def _retry_assignment_for_effect(
        self,
        effect: Mapping[str, Any],
        *,
        snapshot: AggregateSnapshot,
        role: str,
        mode: str,
        submission_kind: str,
    ) -> dict[str, Any] | None:
        """Return the durable assignment owned by this logical effect retry.

        A process attempt may fail after many completed model/tool rounds.  The
        supervisor must claim another attempt on the same assignment, prompt
        pack, and cognitive session.  Recompiling attempt-local workspace/LSP
        projections into a new assignment changes its input fingerprint and
        makes the model see a fresh job.
        """

        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "")
        assignment_id = self._assignment_ids_by_effect.get(effect_key, "")
        assignment = (
            self.repository.read_role_assignment(assignment_id)
            if assignment_id
            else None
        )
        if assignment is None and effect_key:
            # The effect-to-assignment index is intentionally an in-memory
            # accelerator.  After a manager restart the durable assignment is
            # still the logical coroutine's owner, so recover it from the
            # execution spec instead of creating a second assignment in the
            # same role session.
            candidates: list[dict[str, Any]] = []
            for candidate in self.repository.list_role_assignments(
                workflow_id=snapshot.workflow_id
            ):
                execution_spec = dict(candidate.get("execution_spec") or {})
                if str(
                    execution_spec.get("effect_key")
                    or execution_spec.get("effect_id")
                    or ""
                ) != effect_key:
                    continue
                candidates.append(dict(candidate))
            if len(candidates) > 1:
                raise SubmissionInvariantError(
                    "logical role effect has multiple durable assignments"
                )
            if candidates:
                assignment = candidates[0]
                assignment_id = str(assignment.get("assignment_id") or "")
                if assignment_id:
                    self._assignment_ids_by_effect[effect_key] = assignment_id
        if assignment is None:
            return None
        expected = (
            snapshot.workflow_id,
            snapshot.aggregate_type.value,
            snapshot.aggregate_id,
            str(role),
            str(mode),
            str(submission_kind),
        )
        actual = (
            str(assignment.get("workflow_id") or ""),
            str(assignment.get("aggregate_type") or ""),
            str(assignment.get("aggregate_id") or ""),
            str(assignment.get("role") or ""),
            str(assignment.get("mode") or ""),
            str(assignment.get("submission_kind") or ""),
        )
        if actual != expected:
            raise SubmissionInvariantError(
                "logical role effect retry resolved to a different durable assignment"
            )
        if str(assignment.get("state") or "") not in {
            RoleAssignmentState.QUEUED.value,
            RoleAssignmentState.CLAIMED.value,
            RoleAssignmentState.RUNNING.value,
            RoleAssignmentState.RETRY_QUEUED.value,
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        }:
            return None
        return dict(assignment)

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
            evaluation_generation=int(
                dict(assignment.get("execution_spec") or {}).get(
                    "evaluation_generation"
                )
                or 0
            ),
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
        evaluation_generation: int = 0,
        exclude_assignment_id: str = "",
    ) -> dict[str, Any] | None:
        semantic_inputs = self._semantic_role_input_identity(
            input_refs,
            role=role,
            mode=mode,
        )
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
            if int(
                dict(candidate.get("execution_spec") or {}).get(
                    "evaluation_generation"
                )
                or 0
            ) != int(evaluation_generation):
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
                self._semantic_role_input_identity(
                    dict(candidate.get("input_refs") or {}),
                    role=role,
                    mode=mode,
                )
                == semantic_inputs
            ):
                return dict(candidate)
        return None

    def _semantic_role_input_identity(
        self,
        input_refs: Mapping[str, Mapping[str, Any]],
        *,
        role: str,
        mode: str,
    ) -> dict[str, dict[str, Any]]:
        """Return the exact immutable semantic inputs for receipt reconciliation."""

        return _semantic_role_input_refs(
            input_refs,
            role=role,
            mode=mode,
        )

    def _role_submission_settlement(
        self,
        effect: Mapping[str, Any],
        *,
        assignment_id: str = "",
        required: bool = True,
    ) -> dict[str, str]:
        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "")
        resolved_assignment_id = str(assignment_id).strip()
        if not resolved_assignment_id:
            resolved_assignment_id = self._assignment_ids_by_effect.get(effect_key, "")
        if not resolved_assignment_id:
            return {}
        assignment = self.repository.read_role_assignment(resolved_assignment_id)
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
            "role_assignment_id": resolved_assignment_id,
            "role_submission_payload_hash": payload_hash,
        }

    @staticmethod
    def _terminal_role_assignment_id(terminal: Mapping[str, Any]) -> str:
        assignment_id = str(
            dict(terminal.get("payload") or {}).get("role_assignment_id") or ""
        ).strip()
        if not assignment_id:
            raise SubmissionInvariantError(
                "role terminal has no durable assignment identity"
            )
        return assignment_id

    def _release_background_business_lease(self, effect: Mapping[str, Any]) -> None:
        try:
            snapshot = self._effect_snapshot(effect)
            if snapshot.state in {
                "REVIEW_QUIESCING",
                "REVIEW_SNAPSHOTTING",
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
        charged_failures = max(1, _charged_role_failure_attempt_count(attempts))
        error_text = f"{error.__class__.__name__}: {error}"
        failure_payload = {
            "kind": "role_assignment_failed",
            "role": str(assignment.get("role") or ""),
            "attempt_count": charged_failures,
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
                    self._require_cycle_triage(snapshot)
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
                failure_generation = max(0, int(snapshot.version))
                with self.repository.transaction() as connection:
                    self.repository.dispatch(
                        ActionEnvelope(
                            action_type="ROLE_FAILED",
                            workflow_id=snapshot.workflow_id,
                            aggregate_type=snapshot.aggregate_type,
                            aggregate_id=snapshot.aggregate_id,
                            actor="bunshin-v2-worker-supervisor",
                            expected_version=snapshot.version,
                            # One durable receipt may be replayed after an operator
                            # resolves triage.  Deduplicate retries inside the
                            # current aggregate generation without mistaking a
                            # later recovery cycle for the already-settled failure.
                            idempotency_key=(
                                f"worker-failed:{assignment_id}:"
                                f"generation-{failure_generation}"
                            ),
                            payload={
                                "failure_artifact_ref": failure_ref.to_dict(),
                                "blocker": {
                                    "kind": "role_failure",
                                    "summary": error_text,
                                    "role": str(current_assignment.get("role") or ""),
                                    "attempt_count": charged_failures,
                                },
                            },
                        ),
                        role_assignment_id=assignment_id,
                        role_submission_payload_hash=str(
                            current_assignment["submission_payload_hash"]
                        ),
                        _connection=connection,
                    )
                    self._require_cycle_triage(
                        snapshot,
                        _connection=connection,
                    )
                return {
                    "provider_request_id": assignment_id,
                    "status": "triage_required",
                }
            except AggregateVersionConflict:
                continue
        raise DeferredEffectError("role failure receipt settlement lost repeated CAS races")

    def _require_cycle_triage(
        self,
        snapshot: AggregateSnapshot,
        *,
        _connection=None,
    ) -> None:
        coordinator = WorkflowCoordinator(self.repository)
        if snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
            coordinator.require_plan_triage(
                workflow_id=snapshot.workflow_id,
                _connection=_connection,
            )
        elif snapshot.aggregate_type == AggregateType.DAG_NODE_RUN:
            coordinator.require_node_triage(
                workflow_id=snapshot.workflow_id,
                node_name=str(
                    snapshot.payload.get("module_name")
                    or snapshot.payload.get("unit_id")
                    or ""
                ),
                _connection=_connection,
            )

    async def recover_background_assignments(self) -> int:
        if self._stopping:
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
            effect = dict(assignment.get("execution_spec") or {})
            effect_key = str(effect.get("effect_key") or assignment["assignment_key"])
            effect["effect_key"] = effect_key
            effect.setdefault("effect_id", f"assignment:{assignment['assignment_id']}")
            runner = self._runner_for_recovered_effect(effect)
            if runner is None:
                continue
            existing_worker = self._background_workers.get(effect_key)
            if existing_worker is not None and not existing_worker.done():
                # The live worker owns both the logical effect and its
                # assignment projection. A periodic recovery scan may observe
                # older durable rows for the same effect, but it must never
                # rebind the projection underneath that worker.
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
            task = asyncio.create_task(
                self._background_worker_loop(effect, runner),
                name=f"bunshin-v2-recovered-{str(assignment['assignment_id'])[-12:]}",
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
                        actor="bunshin-v2-replan",
                        expected_version=node.version,
                        idempotency_key=f"replan-suppress-admission:{node.aggregate_id}:{node.version}",
                        payload={"stale_reason_ref": finding_ref},
                    )
                )
            return {"status": "suppressed_by_replan"}
        implementation = activation.role == OrchestrationRole.IMPLEMENTATION
        target_state = {
            "START_PRODUCING": "PRODUCING",
            "START_REVIEW": "REVIEWING",
            "START_REPAIR": "REPAIRING",
        }[action_type]
        if node.state == target_state and node.payload.get("active_worker_id"):
            self._start_graph_cycle_assignment(
                node=node,
                effect=effect,
                action_type=action_type,
                implementation=implementation,
            )
            return {"provider_request_id": str(node.payload.get("active_worker_id"))}
        cycle = int(node.payload.get("candidate_cycle") or 0) + (1 if implementation else 0)
        invocation_id = _node_role_session_id(node, activation)
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
        try:
            with self.repository.transaction() as connection:
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type=action_type,
                        workflow_id=node.workflow_id,
                        aggregate_type=AggregateType.DAG_NODE_RUN,
                        aggregate_id=node.aggregate_id,
                        actor="bunshin-v2-scheduler",
                        expected_version=node.version,
                        idempotency_key=f"effect:{effect['effect_key']}:admit",
                        payload=payload,
                    ),
                    _connection=connection,
                )
                self._start_graph_cycle_assignment(
                    node=node,
                    effect=effect,
                    action_type=action_type,
                    implementation=implementation,
                    _connection=connection,
                )
        except BaseException:
            try:
                self.repository.release_lease(
                    lease_resource,
                    invocation_id,
                    lease.fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
            raise
        return {"provider_request_id": invocation_id}

    def _start_graph_cycle_assignment(
        self,
        *,
        node: AggregateSnapshot,
        effect: Mapping[str, Any],
        action_type: str,
        implementation: bool,
        _connection=None,
    ) -> None:
        module_name = str(
            node.payload.get("module_name")
            or node.payload.get("unit_id")
            or ""
        )
        coordinator = WorkflowCoordinator(self.repository)
        cycle = coordinator.execution(
            workflow_id=node.workflow_id,
            _connection=_connection,
        ).cycles[module_name]
        coordinator.start_assignment(
            workflow_id=node.workflow_id,
            node_name=module_name,
            slot=(
                CycleSlot.PRODUCER
                if implementation
                else CycleSlot.CHECKER
            ),
            kind=(
                AssignmentKind.REPAIR
                if action_type == "START_REPAIR"
                else AssignmentKind.RECHECK
                if action_type == "START_REVIEW"
                and cycle.last_verdict is not None
                else AssignmentKind.INITIAL
            ),
            input_fingerprint=str(effect["effect_key"]),
            _connection=_connection,
        )

    def _install_verifier_tests_for_repair(
        self,
        node: AggregateSnapshot,
    ) -> dict[str, Any]:
        # Verifier tests are already ordinary commits on the shared Module
        # branch.  A repair resumes from that HEAD and has nothing to install.
        return {}

    def _checkpoint_verifier_tests(
        self,
        *,
        node: AggregateSnapshot,
        review_workspace: Path,
        candidate_ref: ArtifactRef,
        candidate: Mapping[str, Any],
        candidate_digest: str,
        changed_test_paths: list[str],
    ) -> tuple[ArtifactRef, str, dict[str, Any]]:
        if self._execution_adapter(node) != SOFTWARE_GIT_ADAPTER:
            raise SubmissionInvariantError(
                "verifier checkpoint currently requires the software Git adapter"
            )
        if not changed_test_paths:
            raise SubmissionInvariantError("verifier checkpoint has no changed tests")
        node_workspace = Path(str(node.payload.get("workspace_path") or ""))
        if (
            not node_workspace.is_dir()
            or node_workspace.resolve() != review_workspace.resolve()
        ):
            raise SubmissionInvariantError(
                "Verifier must checkpoint tests in the canonical Module worktree"
            )
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
        checkpoint_key = hashlib.sha256(
            f"verifier-checkpoint-v1:{node.aggregate_id}:{candidate_digest}:{tree}".encode(
                "utf-8"
            )
        ).hexdigest()
        existing = subprocess.run(
            [
                "git",
                "-C",
                str(review_workspace),
                "log",
                "--all",
                "--fixed-strings",
                f"--grep=Pal-Assignment-Key: {checkpoint_key}",
                "--format=%H",
                "-n",
                "1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        head = _git_output(review_workspace, "rev-parse", "HEAD")
        if existing:
            existing_parent = _git_output(review_workspace, "rev-parse", f"{existing}^")
            existing_tree = _git_output(review_workspace, "rev-parse", f"{existing}^{{tree}}")
            if existing_parent != candidate_digest or existing_tree != tree:
                raise SubmissionInvariantError(
                    "recovered verifier checkpoint does not match its reviewed parent"
                )
            checkpoint_digest = existing
        else:
            if head != candidate_digest:
                raise SubmissionInvariantError(
                    "Module worktree moved away from the reviewed Coder commit"
                )
            checkpoint_digest = subprocess.run(
                [
                    "git",
                    "-C",
                    str(review_workspace),
                    "-c",
                    "user.name=Pal Bunshin Verifier",
                    "-c",
                    "user.email=bunshin-verifier@localhost",
                    "commit-tree",
                    tree,
                    "-p",
                    candidate_digest,
                    "-m",
                    (
                        f"bunshin verifier checkpoint {node.aggregate_id}\n\n"
                        f"Pal-Assignment-Key: {checkpoint_key}"
                    ),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(review_workspace), "reset", "--hard", checkpoint_digest],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        delta_patch = subprocess.run(
            [
                "git",
                "-C",
                str(review_workspace),
                "diff",
                "--binary",
                candidate_digest,
                checkpoint_digest,
                "--",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
        checkpoint = {
            **dict(candidate),
            "candidate_digest": checkpoint_digest,
            "previous_head_sha": candidate_digest,
            "base_sha": candidate_digest,
            "candidate_tree_sha": tree,
            "delta_patch_sha": hashlib.sha256(delta_patch).hexdigest(),
            "changed_paths": sorted(
                set(str(item) for item in list(candidate.get("changed_paths") or []))
                | set(changed_test_paths)
            ),
            "verifier_test_paths": list(changed_test_paths),
            "candidate_key": checkpoint_key,
        }
        checkpoint_ref = self.service.artifacts.put_json(
            checkpoint,
            artifact_type="GitCheckpointArtifact",
            provenance={"owner": "manager", "role": "verifier"},
            child_refs=(
                (candidate_ref.sha256, "previous_checkpoint"),
            ),
        )
        return checkpoint_ref, checkpoint_digest, checkpoint

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
        expected_invocation_id = _node_role_session_id(node, activation)
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
        try:
            rebound = self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=node.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="bunshin-v2-recovery",
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
        except BaseException:
            try:
                self.repository.release_lease(
                    lease_resource,
                    invocation_id,
                    lease.fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
            raise

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
        try:
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="START_REVIEW",
                    workflow_id=review.workflow_id,
                    aggregate_type=AggregateType.STANDALONE_REVIEW,
                    aggregate_id=review.aggregate_id,
                    actor="bunshin-v2-scheduler",
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
        except BaseException:
            try:
                self.repository.release_lease(
                    lease_resource,
                    invocation_id,
                    lease.fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
            raise
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
        invocation_id = str(node.payload.get("active_worker_id") or "")
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        self.repository.assert_fencing_token(lease_resource, invocation_id, fencing_token)
        skeleton_manifest = (
            self._execution_adapter(node) == SOFTWARE_GIT_ADAPTER
        )
        self._write_node_journal(
            node,
            owner_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            updates={
                "current_micro_plan": (
                    ["reproduce RepairBill", "apply minimal repair", "run focused regression"]
                    if repair
                    else [
                        (
                            "inspect ModuleWorkView"
                            if skeleton_manifest
                            else "inspect UnitWorkView"
                        ),
                        "implement contract",
                        "run focused self-checks",
                    ]
                ),
                "last_safe_point": "worker_started",
            },
        )
        view_ref = UnitWorkViewBuilder(self.service.contracts).build(node)
        work_view = dict(self.service.artifacts.read_json(view_ref))
        references = {
            "module_work_view" if skeleton_manifest else "unit_work_view": view_ref
        }
        if skeleton_manifest:
            architecture_ref = _ref_from_mapping(
                node.payload.get("architecture_manifest_ref")
            )
            architecture_artifact = self.service.artifacts.read_json(
                architecture_ref
            )
            task_value = architecture_artifact.get("requirements_ref")
            if not isinstance(task_value, Mapping) or not task_value.get("sha256"):
                raise ValueError(
                    "SWE Coder requires the immutable task ledger as a final fallback"
                )
            references["task"] = _ref_from_mapping(task_value)
        repair_ref = node.payload.get("repair_bill_ref")
        if isinstance(repair_ref, Mapping) and repair_ref.get("sha256"):
            semantic_repair_view = repair_bill_semantic_view(
                self.service.artifacts,
                repair_ref,
            )
            semantic_repair_view["verifier_tests_are_preinstalled"] = bool(
                node.payload.get("verifier_test_paths")
                or _verification_corpus_files(
                    Path(str(node.payload.get("workspace_path") or "")),
                    dict(
                        dict(node.payload.get("path_policy") or {}).get(
                            "verification_corpus"
                        )
                        or {}
                    ),
                )
            )
            semantic_repair_ref = self.service.artifacts.put_json(
                semantic_repair_view,
                artifact_type="RepairBillSemanticViewArtifact",
                provenance={"owner": "manager", "audience": "coder"},
                child_refs=((str(repair_ref["sha256"]), "repair_bill"),),
            )
            references["repair_bill"] = semantic_repair_ref
        instruction = (
            "This is a fresh repair cycle. Read the bound RepairBill, reproduce it, make the smallest contract-preserving repair, "
            "run the affected regressions, and submit a new Candidate. An earlier submission does not settle this cycle."
            if repair
            else "Implement the current bound UnitWorkView, complete its compact checklist and focused checks, then submit the Candidate."
        )
        if skeleton_manifest:
            instruction = (
                "Read reference:module_work_view and the bound RepairBill once, then immediately call update_checklist with the complete "
                "repair micro-plan; Manager appends every finding item. Use that checklist as the work driver, make the smallest local "
                "contract-preserving repair, run the affected durable regressions, and submit a new Candidate. Use reference:task only as "
                "the final fallback for exact product intent that the local contract does not resolve. An earlier submission does not settle this cycle."
                if repair
                else "Read reference:module_work_view once, then immediately call update_checklist with the complete implementation "
                "micro-plan and use its next action as the work driver. Implement the current bound Module Protocol from the Accepted "
                "Skeleton, run the minimum focused checks, and submit the Candidate. Use reference:task only as the final fallback for "
                "exact product intent that the local contract does not resolve."
            )
        path_policy = dict(node.payload.get("path_policy") or {})
        developer_test_path = str(
            dict(path_policy.get("developer_tests") or {}).get("path") or ""
        ).strip()
        verification_corpus_path = str(
            dict(path_policy.get("verification_corpus") or {}).get("path") or ""
        ).strip()
        coder_workspace = Path(str(node.payload.get("workspace_path") or ""))
        for corpus_path in (developer_test_path, verification_corpus_path):
            if corpus_path:
                _ensure_workspace_directory(coder_workspace, corpus_path)
        coder_read_only_overlays = [
            *(
                list(path_policy.get("contract_paths") or [])
                if str(path_policy.get("contract_mode") or "review_guarded")
                == "file_frozen"
                else []
            ),
            *([verification_corpus_path] if verification_corpus_path else []),
        ]
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
                "workspace_binding": "canonical",
                "project_name": str(node.payload.get("unit_id") or "unit"),
                **_workspace_tooling_from_work_view(work_view),
                "write_path_scopes": list(compiled_module_write_scopes(path_policy)),
                "read_only_overlay_paths": coder_read_only_overlays,
            },
            prepare_workspace=True,
        )
        report = _primary_json_output(terminal)
        if skeleton_manifest:
            # Durable role receipts survive Manager upgrades and are replayed
            # during triage recovery.  Re-apply the current handoff projection
            # before defense-in-depth validation so an older receipt cannot
            # leak Manager-owned WorkItem identity into the business action.
            report = {
                **report,
                "work_items": submission_work_items(
                    report.get("work_items")
                ),
            }
            try:
                _validate_skeleton_coder_report(
                    report,
                    expected_module=str(node.payload.get("module_name") or node.payload.get("unit_id") or ""),
                    work_view=work_view,
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
            child_refs=(
                (
                    view_ref.sha256,
                    "module_work_view" if skeleton_manifest else "unit_work_view",
                ),
            ),
        )
        self._write_node_journal(
            node,
            owner_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            updates={
                "current_micro_plan": [
                    str(dict(item or {}).get("step") or "")
                    for item in list(
                        dict(report.get("checklist") or {}).get("plan") or []
                    )
                    if str(dict(item or {}).get("status") or "")
                    in {"pending", "in_progress"}
                ],
                "completed_checklist": [
                    str(dict(item or {}).get("step") or "")
                    for item in list(
                        dict(report.get("checklist") or {}).get("plan") or []
                    )
                    if str(dict(item or {}).get("status") or "") == "completed"
                ],
                "files_changed": list(report.get("files_changed") or []),
                "open_questions": [],
                "known_failures": (
                    [str(report.get("summary") or "")]
                    if status != "candidate_ready"
                    else []
                ),
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
                **self._role_submission_settlement(
                    effect,
                    assignment_id=self._terminal_role_assignment_id(terminal),
                ),
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
            **self._role_submission_settlement(
                effect,
                assignment_id=self._terminal_role_assignment_id(terminal),
            ),
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
        await self._close_owned_process(
            invocation_id,
            process_group=process_group,
            worker_label="node worker",
        )
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
        contract_ref = _ref_from_mapping(node.payload.get("unit_contract_ref"))
        contract = self.service.artifacts.read_json(contract_ref)
        adapter = self._execution_adapter(node)
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
                self.service.artifacts,
                self._worktree_locks,
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
        try:
            with self.repository.transaction() as connection:
                current = self.repository.read_snapshot(
                    AggregateType.DAG_NODE_RUN,
                    node.aggregate_id,
                    _connection=connection,
                )
                if current is None:
                    raise SubmissionInvariantError(
                        "candidate node disappeared before atomic publication"
                    )
                self.repository.dispatch(
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
                    _connection=connection,
                )
                WorkflowCoordinator(self.repository).producer_submitted(
                    workflow_id=node.workflow_id,
                    node_name=str(
                        node.payload.get("module_name")
                        or node.payload.get("unit_id")
                        or ""
                    ),
                    product_ref=candidate_ref.sha256,
                    _connection=connection,
                )
        finally:
            try:
                self.repository.release_lease(
                    lease_resource,
                    invocation_id,
                    fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
        return {"result_artifact_ref": candidate_ref.to_dict()}

    async def _run_verification(
        self,
        effect: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        node = await self._ensure_node_effect_lease(
            node,
            action_type="REBIND_REVIEWER",
            activation=RoleActivation(
                OrchestrationRole.VERIFIER,
                RoleMode.MODULE,
            ),
        )
        invocation_id = str(node.payload.get("active_worker_id") or "")
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        candidate_ref = _ref_from_mapping(node.payload.get("candidate_ref"))
        adapter = self._execution_adapter(node)
        candidate_digest = str(node.payload.get("candidate_digest") or "")
        if adapter == SOFTWARE_GIT_ADAPTER:
            review_workspace, review_scratch = provision_module_verification_workspace(
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
        view_value = node.payload.get("unit_work_view_ref")
        if not isinstance(view_value, Mapping) or not view_value.get("sha256"):
            raise ValueError("verifier requires the exact work view used by Coder")
        view_ref = _ref_from_mapping(view_value)
        work_view = dict(self.service.artifacts.read_json(view_ref))
        candidate = dict(self.service.artifacts.read_json(candidate_ref))
        skeleton_manifest = adapter == SOFTWARE_GIT_ADAPTER
        candidate_view_ref: ArtifactRef | None = None
        if adapter != SOFTWARE_GIT_ADAPTER or not skeleton_manifest:
            candidate_view_ref = self.service.artifacts.put_json(
                {
                    "module_name": str(node.payload.get("module_name") or node.payload.get("unit_id") or ""),
                    "node_kind": str(node.payload.get("node_kind") or "unit"),
                    "changed_paths": [str(item) for item in list(candidate.get("changed_paths") or [])],
                    "candidate_cycle": int(node.payload.get("candidate_cycle") or 0),
                    "instruction": "Inspect the immutable candidate in the bound review workspace.",
                },
                artifact_type="CandidateSemanticViewArtifact",
                provenance={"owner": "manager", "audience": "verifier"},
                child_refs=((candidate_ref.sha256, "candidate"),),
            )
        system_delivery_view_ref = (
            UnitWorkViewBuilder(self.service.contracts).system_delivery_view(node)
            if bool(node.payload.get("graph_sink"))
            else None
        )
        system_delivery_view = (
            self.service.artifacts.read_json(system_delivery_view_ref)
            if system_delivery_view_ref is not None
            else None
        )
        verification_policy = effective_verification_policy(
            work_view=work_view,
            verification_policy=self._workflow_policy(node.workflow_id, "verification"),
            system_delivery_view=system_delivery_view,
        )
        # The work view limits this run's scope; the complete immutable ledger
        # prevents an upstream summary from narrowing user intent.
        candidate_diff_ref = candidate_view_ref
        git_diff_refs: dict[str, ArtifactRef] = {}
        if adapter == SOFTWARE_GIT_ADAPTER and skeleton_manifest:
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
            raise ValueError("verifier requires a bound Candidate diff or System work view")
        verifier_references = _verifier_reference_refs(
            artifacts=self.service.artifacts,
            node_payload=node.payload,
            module_work_view_ref=view_ref,
            candidate_diff_ref=candidate_diff_ref,
        )
        if system_delivery_view_ref is not None:
            verifier_references["system_delivery_view"] = system_delivery_view_ref
        verifier_references.update(git_diff_refs)
        path_policy = dict(node.payload.get("path_policy") or {})
        developer_test_path = str(
            dict(path_policy.get("developer_tests") or {}).get("path") or ""
        ).strip()
        verification_corpus_path = str(
            dict(path_policy.get("verification_corpus") or {}).get("path") or ""
        ).strip()
        for corpus_path in (
            developer_test_path,
            verification_corpus_path,
        ):
            if corpus_path:
                _ensure_workspace_directory(review_workspace, corpus_path)
        verifier_read_only_overlays = [
            *(
                list(path_policy.get("contract_paths") or [])
                if str(path_policy.get("contract_mode") or "review_guarded")
                == "file_frozen"
                else []
            ),
            *([developer_test_path] if developer_test_path else []),
        ]
        terminal, prompt_ref, terminal_ref = await self._run_profile(
            effect=effect,
            snapshot=node,
            invocation_id=invocation_id,
            lease_resource=lease_resource,
            fencing_token=fencing_token,
            profile=self._profile_for_role(node.workflow_id, "verifier"),
            activation=RoleActivation(
                OrchestrationRole.VERIFIER,
                RoleMode.MODULE,
            ),
            instruction=_semantic_verifier_instruction(
                graph_sink=bool(node.payload.get("graph_sink"))
            ),
            reference_refs=verifier_references,
            workspace_override={
                "kind": "existing_repo",
                "repo_path": str(review_workspace),
                "workspace_binding": (
                    "canonical"
                    if adapter == SOFTWARE_GIT_ADAPTER
                    else "ephemeral_artifact"
                ),
                "project_name": str(node.payload.get("unit_id") or "unit"),
                **_workspace_tooling_from_work_view(work_view),
                "review_scratch_dir": str(review_scratch),
                "workspace_policy": {
                    "mode": "writable_git_branch"
                },
                "verification_scratch_only": (
                    adapter != SOFTWARE_GIT_ADAPTER
                ),
                "write_path_scopes": [
                    dict(
                        dict(node.payload.get("path_policy") or {}).get(
                            "verification_corpus"
                        )
                        or {}
                    )
                ],
                "read_only_overlay_paths": verifier_read_only_overlays,
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
                invocation_id=invocation_id,
                lease_resource=lease_resource,
                fencing_token=fencing_token,
                candidate_ref=candidate_ref,
                candidate_digest=candidate_digest,
                candidate=candidate,
                review_workspace=review_workspace,
                review_scratch=review_scratch,
                execution_adapter=adapter,
                work_view=work_view,
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
        invocation_id: str,
        lease_resource: str,
        fencing_token: int,
        candidate_ref: ArtifactRef,
        candidate_digest: str,
        candidate: Mapping[str, Any],
        review_workspace: Path,
        review_scratch: Path,
        execution_adapter: str,
        work_view: Mapping[str, Any],
        submission: Mapping[str, Any],
        terminal: Mapping[str, Any],
        prompt_ref: ArtifactRef,
        terminal_ref: ArtifactRef,
    ) -> Mapping[str, Any]:
        outcome = str(submission.get("outcome") or "").strip()
        scratch_only = execution_adapter != SOFTWARE_GIT_ADAPTER
        changed_paths = (
            _verification_scratch_paths(review_scratch)
            if scratch_only
            else _verification_workspace_changed_paths(review_workspace, candidate_digest)
        )
        corpus_scope = dict(
            dict(node.payload.get("path_policy") or {}).get(
                "verification_corpus"
            )
            or {}
        )
        current_case_paths = (
            _verification_scratch_paths(review_scratch)
            if scratch_only
            else _verification_corpus_files(review_workspace, corpus_scope)
        )
        errors = semantic_verification_submission_errors(
            submission,
            work_view=work_view,
            changed_paths=changed_paths,
            current_case_paths=current_case_paths,
            corpus_scope=corpus_scope,
            scratch_only=scratch_only,
        )
        normalized_submission = dict(submission)
        if errors:
            raise SubmissionInvariantError(
                "semantic verifier submission failed manager validation:\n- "
                + "\n- ".join(errors)
            )
        findings = structured_findings(submission)
        advisories = structured_advisories(submission)
        reason = str(submission.get("reason") or "").strip()
        receipts = [
            dict(item)
            for item in list(submission.get("tool_receipts") or [])
            if isinstance(item, Mapping)
        ]

        settlement = self._role_submission_settlement(
            effect,
            assignment_id=self._terminal_role_assignment_id(terminal),
        )
        assignment = self.repository.read_role_assignment(
            settlement["role_assignment_id"]
        )
        if assignment is None:
            raise SubmissionInvariantError("verifier assignment disappeared before quiescing")
        submission_ref = dict(assignment.get("submission_artifact_ref") or {})
        pending_ref = self.service.artifacts.put_json(
            {
                "schema_version": "1",
                "submission": normalized_submission,
                "candidate_ref": candidate_ref.to_dict(),
                "implementation_candidate_ref": candidate_ref.to_dict(),
                "candidate_digest": candidate_digest,
                "candidate_git_base": str(
                    candidate_digest
                    if execution_adapter == SOFTWARE_GIT_ADAPTER
                    else ""
                ),
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
        claimed_rebind = False

        def release_rebind() -> None:
            if not claimed_rebind:
                return
            try:
                self.repository.release_lease(
                    lease_resource,
                    invocation_id,
                    fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass

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
            claimed_rebind = True
        self._revoked_tokens.add((invocation_id, fencing_token))
        lease = self.repository.read_lease(lease_resource)
        process_group = int(
            dict((lease or {}).get("metadata") or {}).get("process_group_id") or 0
        )
        review_workspace = Path(str(pending.get("review_workspace") or ""))
        try:
            await self._close_owned_process(
                invocation_id,
                process_group=process_group,
                worker_label="verifier",
            )
            await self._release_managed_lsp_workspace(review_workspace)
            _raise_if_workspace_held(
                review_workspace,
                "a live process still holds the verifier worktree",
            )
        except BaseException:
            release_rebind()
            raise
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
            release_rebind()
            raise
        current = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            node.aggregate_id,
        )
        if current is None:
            self._worktree_locks.release(lock_key)
            release_rebind()
            raise SubmissionInvariantError("verification node disappeared while quiescing")
        try:
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="VERIFIER_QUIESCED",
                    workflow_id=current.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=current.aggregate_id,
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
        except BaseException:
            self._worktree_locks.release(lock_key)
            release_rebind()
            raise
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
        advisories = structured_advisories(submission)
        reason = str(submission.get("reason") or "").strip()
        scratch_only = execution_adapter != SOFTWARE_GIT_ADAPTER
        candidate_git_base = str(
            pending.get("candidate_git_base") or candidate_digest or ""
        )
        changed_paths = (
            _verification_scratch_paths(review_scratch)
            if scratch_only
            else _verification_workspace_changed_paths(
                review_workspace,
                candidate_digest,
            )
        )
        corpus_scope = dict(
            dict(node.payload.get("path_policy") or {}).get(
                "verification_corpus"
            )
            or {}
        )
        outside = [] if scratch_only else [
            path
            for path in changed_paths
            if not _semantic_path_scope_matches(path, corpus_scope)
        ]
        if outside:
            raise SubmissionInvariantError(
                "verifier snapshot contains paths outside the bound module corpus: "
                + ", ".join(outside)
            )
        receipts = [
            dict(item)
            for item in list(submission.get("tool_receipts") or [])
            if isinstance(item, Mapping)
        ]
        workspace_evidence_ref = (
            self._publish_verification_evidence(
                review_scratch=review_scratch,
                candidate_identity=candidate_digest,
            )
            if scratch_only
            else None
        )
        accepted_candidate_ref = candidate_ref
        accepted_candidate_digest = candidate_digest
        accepted_candidate = dict(candidate)
        if not scratch_only and changed_paths:
            (
                accepted_candidate_ref,
                accepted_candidate_digest,
                accepted_candidate,
            ) = self._checkpoint_verifier_tests(
                node=node,
                review_workspace=review_workspace,
                candidate_ref=candidate_ref,
                candidate=candidate,
                candidate_digest=candidate_digest,
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
        report_payload = {
                "schema_version": "2",
                "module_name": str(
                    node.payload.get("module_name") or node.payload.get("unit_id") or ""
                ),
                "outcome": outcome,
                "status": status.value,
                "findings": findings,
                "advisories": advisories,
                "unknown_reason": reason,
                "changed_test_paths": changed_paths,
                "candidate_ref": accepted_candidate_ref.to_dict(),
                "implementation_candidate_ref": candidate_ref.to_dict(),
                "tool_receipts_ref": receipts_ref.to_dict(),
            }
        if workspace_evidence_ref is not None:
            report_payload["workspace_evidence_ref"] = workspace_evidence_ref.to_dict()
        report_children = [
            (accepted_candidate_ref.sha256, "candidate"),
            (candidate_ref.sha256, "implementation_candidate"),
            (receipts_ref.sha256, "tool_receipts"),
        ]
        if workspace_evidence_ref is not None:
            report_children.append((workspace_evidence_ref.sha256, "workspace_evidence"))
        report_ref = self.service.artifacts.put_json(
            report_payload,
            artifact_type="VerificationArtifact",
            provenance={"owner": "manager", "source_role": "verifier"},
            child_refs=tuple(report_children),
        )
        routed_defect = dominant_verification_defect_kind(findings)
        defect_kind = {
            "contract_revision": DefectKind.CONTRACT,
            "architecture_revision": DefectKind.ARCHITECTURE,
            "requirements_revision": DefectKind.REQUIREMENTS,
        }.get(
            outcome,
            DefectKind(routed_defect)
            if routed_defect
            else DefectKind.MODULE,
        )
        target_modules = [
            str(item).strip()
            for item in list(submission.get("target_modules") or [])
            if str(item).strip()
        ]
        repair_node_ids = [
            _resolve_dependency_node_id(
                self.repository,
                node,
                dependency_module=module_name,
            )
            for module_name in target_modules
        ]
        module_node_id = ""
        fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "outcome": outcome,
                    "findings": findings,
                    "changed_test_paths": changed_paths,
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
        coordinator = WorkflowCoordinator(self.repository)
        node_name = str(
            node.payload.get("module_name")
            or node.payload.get("unit_id")
            or ""
        )
        # The node action below publishes effects which may be consumed by a
        # separate Manager loop immediately.  Advance the graph-cycle cursor
        # before dispatching that action; otherwise a REVIEW_FAILED effect can
        # start repair while the cycle still says CHECKER_READY.  This is a
        # prediction of the same mechanical branch used by VerificationService
        # (unknown policy and no-progress are the only blocking branches), not
        # a second semantic verdict.
        failure_history = list(current.payload.get("failure_history") or [])
        if status == VerificationStatus.FAIL:
            failure_history.append(
                {
                    "finding_fingerprint": fingerprint,
                    "candidate_tree_hash": _candidate_tree_fingerprint(
                        accepted_candidate,
                        fallback=accepted_candidate_digest,
                    ),
                }
            )
        blocking_unknown = (
            status == VerificationStatus.UNKNOWN and not unknown_policy.allows()
        )
        blocking_no_progress = (
            status == VerificationStatus.FAIL
            and no_progress_detected(failure_history)
        )
        try:
            with self.repository.transaction() as connection:
                if blocking_unknown or blocking_no_progress:
                    coordinator.require_node_triage(
                        workflow_id=node.workflow_id,
                        node_name=node_name,
                        _connection=connection,
                    )
                else:
                    coordinator.checker_verdict(
                        workflow_id=node.workflow_id,
                        node_name=node_name,
                        accepted=(
                            status in {
                                VerificationStatus.PASS,
                                VerificationStatus.NOT_APPLICABLE,
                            }
                            or status == VerificationStatus.UNKNOWN
                        ),
                        finding_refs=(
                            (repair_ref.sha256,)
                            if repair_ref is not None
                            else ()
                        ),
                        finding_class=(
                            None
                            if status
                            in {
                                VerificationStatus.PASS,
                                VerificationStatus.NOT_APPLICABLE,
                                VerificationStatus.UNKNOWN,
                            }
                            else FindingClass(defect_kind.value)
                        ),
                        dependency_node=(
                            target_modules[0]
                            if target_modules
                            and defect_kind == DefectKind.DEPENDENCY
                            else ""
                        ),
                        accepted_product_ref=(
                            accepted_candidate_ref.sha256
                            if status
                            in {
                                VerificationStatus.PASS,
                                VerificationStatus.NOT_APPLICABLE,
                                VerificationStatus.UNKNOWN,
                            }
                            else ""
                        ),
                        _connection=connection,
                    )
                verdict_result = VerificationService(
                    self.repository,
                    self.service.artifacts,
                ).submit_verdict(
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
                    dependency_node_id=(
                        repair_node_ids[0]
                        if repair_node_ids and defect_kind == DefectKind.DEPENDENCY
                        else ""
                    ),
                    dependency_node_ids=(
                        repair_node_ids
                        if defect_kind == DefectKind.DEPENDENCY
                        else ()
                    ),
                    module_node_id=module_node_id,
                    module_node_ids=(repair_node_ids if module_node_id else ()),
                    system_fingerprint="",
                    accepted_candidate_ref=(
                        accepted_candidate_ref
                        if not scratch_only
                        and accepted_candidate_digest != candidate_digest
                        else None
                    ),
                    accepted_candidate_digest=(
                        accepted_candidate_digest
                        if not scratch_only
                        and accepted_candidate_digest != candidate_digest
                        else ""
                    ),
                    _connection=connection,
                )
        finally:
            try:
                self.repository.release_lease(
                    lease_resource,
                    invocation_id,
                    fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
        return {
            "provider_request_id": invocation_id,
            "result_artifact_ref": report_ref.to_dict(),
        }

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
        await self._close_owned_process(
            invocation_id,
            process_group=process_group,
            worker_label="node worker",
        )
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
                    "node verifier still holds its canonical Module worktree",
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
            with self.repository.transaction() as connection:
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type=action_type,
                        workflow_id=node.workflow_id,
                        aggregate_type=AggregateType.DAG_NODE_RUN,
                        aggregate_id=node.aggregate_id,
                        actor="bunshin-v2-manager",
                        expected_version=current.version,
                        idempotency_key=f"effect:{effect['effect_key']}:stopped",
                    ),
                    _connection=connection,
                )
                WorkflowCoordinator(self.repository).confirm_node_control(
                    workflow_id=node.workflow_id,
                    node_name=str(
                        node.payload.get("module_name")
                        or node.payload.get("unit_id")
                        or ""
                    ),
                    cancel=action_type == "CANCEL_CONFIRMED",
                    _connection=connection,
                )
        fencing_token = int(node.payload.get("fencing_token") or 0)
        if lease_resource and invocation_id and fencing_token:
            try:
                self.repository.release_lease(lease_resource, invocation_id, fencing_token)
            except Exception:
                pass
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
        await self._close_owned_process(
            invocation_id,
            process_group=process_group,
            worker_label="aggregate worker",
        )
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
            with self.repository.transaction() as connection:
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type=action_type,
                        workflow_id=snapshot.workflow_id,
                        aggregate_type=snapshot.aggregate_type,
                        aggregate_id=snapshot.aggregate_id,
                        actor="bunshin-v2-manager",
                        expected_version=current.version,
                        idempotency_key=f"effect:{effect['effect_key']}:stopped",
                    ),
                    _connection=connection,
                )
                if snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
                    WorkflowCoordinator(self.repository).confirm_plan_control(
                        workflow_id=snapshot.workflow_id,
                        cancel=cancel,
                        _connection=connection,
                    )
        fencing_token = int(snapshot.payload.get("fencing_token") or 0)
        if lease_resource and invocation_id and fencing_token:
            try:
                self.repository.release_lease(lease_resource, invocation_id, fencing_token)
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
                self.repository.complete_role_session(
                    session_id,
                    status="cancelled",
                )
        elif cancel and snapshot.aggregate_type == AggregateType.WORKFLOW:
            self.repository.complete_workflow_role_sessions(
                snapshot.workflow_id,
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
                        actor="bunshin-v2-recovery",
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
        if node.state == "REVIEW_QUIESCING":
            return await self._quiesce_verifier_role(effect)
        if node.state == "REVIEW_SNAPSHOTTING":
            return self._snapshot_semantic_verification(effect)
        return {}

    @staticmethod
    def _execution_adapter(node: AggregateSnapshot) -> str:
        adapter = str(node.payload.get("execution_adapter") or "").strip()
        if adapter not in {
            SOFTWARE_GIT_ADAPTER,
            ARTIFACT_BUNDLE_ADAPTER,
        }:
            raise ValueError(
                "DAG node has no supported bound execution adapter"
            )
        return adapter

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
        sink = next(
            (
                item for item in epoch_nodes
                if bool(item.payload.get("graph_sink"))
                and item.state == "ACCEPTED"
            ),
            None,
        )
        if sink is None or any(item.state != "ACCEPTED" for item in epoch_nodes):
            raise ValueError(
                "final delivery requires the declared sink and every executable node ACCEPTED"
            )
        published_sink_ref = WorkflowCoordinator(
            self.repository
        ).published_sink_ref(workflow_id=epoch.workflow_id)
        if (
            str(dict(sink.payload.get("candidate_ref") or {}).get("sha256") or "")
            != published_sink_ref
        ):
            raise ValueError(
                "delivery sink Candidate disagrees with GraphExecution publication"
            )
        verification_ref = _ref_from_mapping(
            sink.payload.get("verification_artifact_ref")
        )
        adapter = self._execution_adapter(sink)
        if adapter == SOFTWARE_GIT_ADAPTER:
            repository = Path(str(sink.payload.get("workspace_path") or ""))
            deliverable_ref = self._publish_verified_git_delivery(
                epoch=epoch,
                delivery_node=sink,
                repository=repository,
                verification_ref=verification_ref,
            )
        elif adapter == ARTIFACT_BUNDLE_ADAPTER:
            deliverable_ref = ArtifactBundleAdapter(
                self.service.runtime_root,
                self.service.artifacts,
            ).publish_deliverable(
                workflow_id=epoch.workflow_id,
                candidate_ref=dict(sink.payload.get("candidate_ref") or {}),
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
                actor="bunshin-v2-manager",
                expected_version=current.version,
                idempotency_key=f"publish:{deliverable_ref.sha256}",
                payload={"published_deliverable_ref": deliverable_ref.to_dict()},
            )
        )
        return {"result_artifact_ref": deliverable_ref.to_dict()}

    def _publish_verified_git_delivery(
        self,
        *,
        epoch: AggregateSnapshot,
        delivery_node: AggregateSnapshot,
        repository: Path,
        verification_ref: ArtifactRef,
    ) -> ArtifactRef:
        if not repository.is_dir():
            raise ValueError("delivery requires the accepted sink module worktree")
        commit_sha = _git_output(repository, "rev-parse", "HEAD")
        manifest_ref = _ref_from_mapping(epoch.payload.get("architecture_manifest_ref"))
        manifest = dict(self.service.artifacts.read_json(manifest_ref))
        snapshot_value = manifest.get("workspace_snapshot_ref")
        source_snapshot: dict[str, Any]
        if isinstance(snapshot_value, Mapping) and snapshot_value.get("sha256"):
            source_snapshot = dict(
                self.service.artifacts.read_json(_ref_from_mapping(snapshot_value))
            )
        else:
            source_snapshot = {"delivery_mode": "local_only"}
        workflow = self.repository.read_snapshot(
            AggregateType.WORKFLOW, epoch.workflow_id
        )
        if workflow is None:
            raise ValueError("delivery workflow is unavailable")
        request = workflow_request_from_snapshot(self.service, workflow)
        repository_layout = dict(manifest.get("repository_layout") or {})
        workflow_key = str(
            delivery_node.payload.get("workflow_key")
            or repository_layout.get("workflow_key")
            or epoch.workflow_id
        )
        task_title = str(
            request.get("title")
            or request.get("goal")
            or request.get("objective")
            or "Bunshin delivery"
        )
        return DeliveryService(
            self.service.runtime_root,
            self.service.artifacts,
        ).publish(
            workflow_id=epoch.workflow_id,
            workflow_key=workflow_key,
            task_title=task_title,
            repository=repository,
            commit_sha=commit_sha,
            source_snapshot=source_snapshot,
            verification_ref=verification_ref,
        )

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
        contract_review = bool(
            request_record
            and str(request_record.get("artifact_type") or "") == CONTRACT_ARTIFACT
        )
        contract_review_workspace = None
        workspace = dict(request.get("workspace") or {})
        if contract_review:
            artifact = dict(self.service.artifacts.read_json(request_ref))
            requirements_ref = _ref_from_mapping(artifact.get("requirements_ref"))
            contract = dict(artifact.get("contract") or {})
            review_view_ref = self.service.artifacts.put_json(
                {
                    "schema_version": "1",
                    "contract": contract,
                },
                artifact_type="ContractReviewViewArtifact",
                provenance={"owner": "manager", "audience": "standalone_reviewer"},
                child_refs=((request_ref.sha256, "contract"),),
            )
            reviewer_inputs = {
                "task": requirements_ref,
                "contract": review_view_ref,
            }
            if self._uses_git_skeleton(review.workflow_id):
                projected = {
                    **artifact,
                    "submission": software_contract_projection(contract),
                }
                contract_review_workspace = (
                    self.service.skeleton.provision_review_worktree(
                        artifact=projected,
                        review_name=f"standalone-{review.aggregate_id}",
                    )
                )
                review_repo = contract_review_workspace.worktree
                review_scratch = (
                    contract_review_workspace.root / "review-scratch"
                )
                review_scratch.mkdir(parents=True, exist_ok=True)
                base_sha = str(artifact.get("skeleton_commit_sha") or "")
            else:
                repo_path = str(
                    workspace.get("repo_path")
                    or workspace.get("cwd")
                    or self.service.runtime_root
                )
                review_repo, review_scratch, base_sha = (
                    _prepare_standalone_review_workspace(
                        self.service.runtime_root,
                        review.aggregate_id,
                        Path(repo_path),
                    )
                )
        else:
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
            profile=self._profile_for_role(review.workflow_id, "reviewer"),
            activation=RoleActivation(OrchestrationRole.REVIEWER, RoleMode.STANDALONE),
            instruction="Perform the requested standalone review. Report evidence-grounded findings and do not modify the target. Repair is a separate explicit workflow.",
            reference_refs=reviewer_inputs,
            workspace_override={
                "kind": "existing_repo",
                "repo_path": str(review_repo),
                "workspace_binding": (
                    "canonical"
                    if contract_review_workspace is not None
                    else "ephemeral_artifact"
                ),
                "project_name": "standalone-review",
                "review_scratch_dir": str(review_scratch),
            },
            prepare_workspace=True,
        )
        payload = _named_json_output(terminal, "contract_review.json")
        try:
            verdict = str(payload.get("verdict") or "").strip().upper()
            if verdict not in {"PASS", "FAIL"}:
                raise ValueError("review verdict must be PASS or FAIL")
            findings = [
                dict(item)
                for item in list(payload.get("findings") or [])
                if isinstance(item, Mapping)
            ]
            advisories = [
                dict(item)
                for item in list(payload.get("advisories") or [])
                if isinstance(item, Mapping)
            ]
            if verdict == "PASS" and findings:
                raise ValueError("PASS review cannot contain blocking findings")
            if verdict == "FAIL" and not findings:
                raise ValueError("FAIL review requires at least one finding")
        except Exception as exc:
            raise SubmissionInvariantError(
                f"accepted review_submit failed manager defense-in-depth validation: {exc}"
            ) from exc
        test_workspace_ref = self._publish_verification_evidence(
            review_scratch=review_scratch,
            candidate_identity=base_sha,
        )
        status = (
            VerificationStatus.PASS
            if verdict == "PASS"
            else VerificationStatus.FAIL
        )
        report_ref = self.service.artifacts.put_json(
            {
                "schema_version": "1",
                "status": status.value,
                "findings": findings,
                "advisories": advisories,
                "work_items": submission_work_items(payload.get("work_items")),
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
            **self._role_submission_settlement(
                effect,
                assignment_id=self._terminal_role_assignment_id(terminal),
            ),
        )
        if contract_review_workspace is not None:
            contract_review_workspace.cleanup()
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
        try:
            return self.repository.dispatch(
                ActionEnvelope(
                    action_type="REBIND_REVIEWER",
                    workflow_id=review.workflow_id,
                    aggregate_type=AggregateType.STANDALONE_REVIEW,
                    aggregate_id=review.aggregate_id,
                    actor="bunshin-v2-recovery",
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
        except BaseException:
            try:
                self.repository.release_lease(
                    lease_resource,
                    invocation_id,
                    lease.fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
            raise

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
            manifest_ref = _ref_from_mapping(
                review.payload.get("review_request_ref")
            )
            record = self.repository.read_artifact_record(manifest_ref.sha256)
            if (
                record is None
                or str(record.get("artifact_type") or "")
                != CONTRACT_ARTIFACT
            ):
                raise ValueError(
                    "review_and_repair requires a ContractArtifact; "
                    "reviewers may not invent a repair contract"
                )
            repair_bill_ref = self._publish_standalone_repair_bill(
                report_ref=report_ref,
                manifest_ref=manifest_ref,
            )
            action_type = "HANDOFF_REPAIR"
            payload["architecture_manifest_ref"] = manifest_ref.to_dict()
            payload["repair_bill_ref"] = repair_bill_ref.to_dict()
        self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=review.workflow_id,
                aggregate_type=AggregateType.STANDALONE_REVIEW,
                aggregate_id=review.aggregate_id,
                actor="bunshin-v2-manager",
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
        contract = dict(artifact.get("contract") or {})
        modules = dict(contract.get("modules") or {})
        implementation_modules = {
            name: module
            for name, module in modules.items()
            if str(dict(module or {}).get("execution") or "") == "produce"
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

    def _start_plan_cycle_assignment(
        self,
        *,
        workflow_id: str,
        effect: Mapping[str, Any],
        slot: CycleSlot,
        _connection=None,
    ) -> None:
        coordinator = WorkflowCoordinator(self.repository)
        cycle = coordinator.ensure_plan_cycle(
            workflow_id=workflow_id,
            _connection=_connection,
        )
        coordinator.start_plan_assignment(
            workflow_id=workflow_id,
            slot=slot,
            kind=(
                AssignmentKind.REVISION
                if cycle.generation > 1
                else AssignmentKind.INITIAL
            ),
            input_fingerprint=str(effect["effect_key"]),
            _connection=_connection,
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
                self._start_plan_cycle_assignment(
                    workflow_id=revision.workflow_id,
                    effect=effect,
                    slot=CycleSlot.PRODUCER,
                )
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
            action_type = (
                start_action
                if start_action in self.repository.engine.legal_actions(
                    AggregateType.ARCHITECTURE_REVISION,
                    revision.state,
                )
                else rebind_action
            )
            with self.repository.transaction() as connection:
                revision = self.repository.dispatch(
                    ActionEnvelope(
                        action_type=action_type,
                        workflow_id=revision.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision.aggregate_id,
                        actor=invocation_id,
                        expected_version=revision.version,
                        idempotency_key=(
                            f"effect:{effect['effect_key']}:{action_type.lower()}:"
                            f"{lease.fencing_token}"
                        ),
                        payload={
                            "fencing_token": lease.fencing_token,
                            "active_worker_id": invocation_id,
                            "lease_resource_key": lease_resource,
                            "active_role": OrchestrationRole.ARCHITECT.value,
                            "active_role_mode": activation.mode.value,
                        },
                    ),
                    _connection=connection,
                ).snapshot
                self._start_plan_cycle_assignment(
                    workflow_id=revision.workflow_id,
                    effect=effect,
                    slot=CycleSlot.PRODUCER,
                    _connection=connection,
                )
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
            submission = _named_json_output(terminal, "architect.yaml")
            contract = dict(submission.get("contract") or {})
            checklist = {
                "kind": "work_items",
                "items": submission_work_items(submission.get("work_items")),
            }
            current = self.repository.read_snapshot(
                AggregateType.ARCHITECTURE_REVISION,
                revision.aggregate_id,
            )
            if current is None:
                raise ValueError("architecture revision disappeared after Architect submission")
            requirements_ref = _ref_from_mapping(current.payload.get("requirements_ref"))
            revision_base_manifest_ref = self._revision_input_base_manifest_ref(revision)
            result_ref = self.service.artifacts.put_json(
                {
                    "schema_version": "2",
                    "contract_schema": str(
                        submission.get("contract_schema") or ""
                    ),
                    "contract": contract,
                    "graph_ir": dict(submission.get("graph_ir") or {}),
                    "graph_source_map_ref": dict(
                        submission.get("graph_source_map_ref") or {}
                    ),
                    "requirements_ref": requirements_ref.to_dict(),
                },
                artifact_type="ContractArtifact",
                provenance={
                    "architecture_revision_id": revision.aggregate_id,
                    "role": "architect",
                },
                child_refs=((requirements_ref.sha256, "requirements"),)
                + (
                    (
                        (
                            str(
                                dict(
                                    submission.get("graph_source_map_ref")
                                    or {}
                                ).get("sha256")
                                or ""
                            ),
                            "graph_source_map",
                        ),
                    )
                    if dict(
                        submission.get("graph_source_map_ref") or {}
                    ).get("sha256")
                    else ()
                ),
            )
            checklist_ref = self.service.artifacts.put_json(
                checklist,
                artifact_type="ArchitectWorkChecklistArtifact",
                provenance={
                    "role": "architect",
                    "authority": "work_cursor_only",
                },
                child_refs=((result_ref.sha256, "architecture_contract"),),
            )
            with self.repository.transaction() as connection:
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="DATA_ARCHITECT_COMPLETED",
                        workflow_id=revision.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision.aggregate_id,
                        actor="bunshin-v2-architect",
                        expected_version=current.version,
                        idempotency_key=f"architect-output:{revision.aggregate_id}:{result_ref.sha256}",
                        payload={
                            "requirements_ref": requirements_ref.to_dict(),
                            "architecture_manifest_ref": result_ref.to_dict(),
                            "architect_checklist_ref": checklist_ref.to_dict(),
                            **(
                                {"revision_base_manifest_ref": revision_base_manifest_ref.to_dict()}
                                if revision_base_manifest_ref is not None
                                else {}
                            ),
                        },
                    ),
                    **self._role_submission_settlement(
                        effect,
                        assignment_id=self._terminal_role_assignment_id(terminal),
                    ),
                    _connection=connection,
                )
                WorkflowCoordinator(self.repository).submit_plan_product(
                    workflow_id=revision.workflow_id,
                    product_ref=result_ref.sha256,
                    _connection=connection,
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
            if (
                record is None
                or str(record.get("artifact_type") or "")
                != CONTRACT_ARTIFACT
            ):
                raise ValueError(
                    "SWE architecture revision requires a ContractArtifact baseline"
                )
            raw_base = dict(
                self.service.artifacts.read_json(base_manifest_ref)
            )
            base_contract = dict(raw_base.get("contract") or {})
            if (
                str(
                    raw_base.get("contract_schema") or ""
                )
                != "software_engineering.v1"
            ):
                raise ValueError(
                    "SWE architecture revision baseline uses another schema"
                )
            base_artifact = {
                **raw_base,
                "submission": (
                    software_contract_projection(
                        base_contract
                    )
                ),
            }
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
        try:
            architecture_workspace = self.service.skeleton.provision_architecture_workspace(
                workflow_id=revision.workflow_id,
                workflow_name=str(request.get("workflow_name") or request.get("goal") or revision.workflow_id),
                revision_name=revision.aggregate_id,
                workspace=dict(request.get("workspace") or {}),
                requirements_ref=requirements_ref,
                base_artifact=base_artifact,
            )
        except ValueError as exc:
            raise PermanentEffectError(str(exc)) from exc
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
            with self.repository.transaction() as connection:
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
                    ),
                    _connection=connection,
                ).snapshot
                self._start_plan_cycle_assignment(
                    workflow_id=revision.workflow_id,
                    effect=effect,
                    slot=CycleSlot.PRODUCER,
                    _connection=connection,
                )
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
                        "immutable_requirement_paths": list(
                            revision_scope.get("immutable_requirement_paths") or []
                        ),
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
            instruction = _contract_architect_instruction(
                finding=finding_payload,
                has_base_manifest=base_manifest_ref is not None,
                has_revision_scope=revision_scope is not None,
            )
            workspace_override: dict[str, Any] = {
                "kind": "existing_repo",
                "repo_path": str(architecture_workspace.worktree),
                "workspace_binding": "canonical",
                "project_name": architecture_workspace.project_name,
                "contract_authoring_mode": True,
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
            role_submission = _named_json_output(terminal, "architect.yaml")
            contract = dict(role_submission.get("contract") or {})
            submission = software_contract_projection(contract)
            authoring_locations = _architect_authoring_locations(
                architect_path(workspace_override),
                contract,
            )
            checklist = {
                "kind": "work_items",
                "items": submission_work_items(
                    role_submission.get("work_items")
                ),
            }
            current = self.repository.read_snapshot(
                AggregateType.ARCHITECTURE_REVISION,
                revision.aggregate_id,
            )
            if current is None:
                raise ValueError("architecture revision disappeared after Architect submission")
            requirements_ref = _ref_from_mapping(current.payload.get("requirements_ref"))
            submission_ref = self.service.artifacts.put_json(
                {
                    **dict(role_submission),
                    "compiled_skeleton_submission": submission,
                    "authoring_locations": authoring_locations,
                },
                artifact_type="ContractSubmissionIntentArtifact",
                provenance={"role": "architect"},
                child_refs=((requirements_ref.sha256, "requirements"),),
            )
            checklist_ref = self.service.artifacts.put_json(
                checklist,
                artifact_type="ArchitectWorkChecklistArtifact",
                provenance={
                    "role": "architect",
                    "authority": "work_cursor_only",
                },
                child_refs=(
                    (submission_ref.sha256, "architecture_submission"),
                ),
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
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="ARCHITECT_SUBMITTED",
                    workflow_id=revision.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision.aggregate_id,
                    actor=invocation_id,
                    expected_version=current.version,
                    idempotency_key=_contract_submit_idempotency_key(
                        revision.aggregate_id,
                        current.version,
                        submission_ref.sha256,
                    ),
                    payload={
                        "requirements_ref": requirements_ref.to_dict(),
                        "pending_architecture_submission_ref": submission_ref.to_dict(),
                        "architect_checklist_ref": checklist_ref.to_dict(),
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
                **self._role_submission_settlement(
                    effect,
                    assignment_id=self._terminal_role_assignment_id(terminal),
                ),
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
        try:
            rebound = self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=revision.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision.aggregate_id,
                    actor="bunshin-v2-recovery",
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
        except BaseException:
            try:
                self.repository.release_lease(
                    lease_resource,
                    invocation_id,
                    lease.fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
            raise

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
        await self._close_owned_process(
            invocation_id,
            process_group=process_group,
            worker_label="architect",
        )
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
        contract_intent = dict(
            self.service.artifacts.read_json(submission_ref)
        )
        submission = dict(
            contract_intent.get("compiled_skeleton_submission") or {}
        )
        if not submission:
            raise ValueError(
                "contract submission has no compiled software skeleton projection"
            )
        requirements_ref = _ref_from_mapping(revision.payload.get("requirements_ref"))
        try:
            skeleton_ref = self.service.skeleton.snapshot_architect_result(
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
                self.service.artifacts.read_json(skeleton_ref)
            )
            manifest_ref = self.service.artifacts.put_json(
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
        except ValueError as exc:
            finding_payload = _stable_architecture_preflight_finding(
                exc,
                contract_intent=contract_intent,
                submission=submission,
            )
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
            try:
                with self.repository.transaction() as connection:
                    current = self.repository.read_snapshot(
                        AggregateType.ARCHITECTURE_REVISION,
                        revision.aggregate_id,
                        _connection=connection,
                    )
                    if current is None:
                        raise SubmissionInvariantError(
                            "architecture revision disappeared before rejection"
                        )
                    self.repository.dispatch(
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
                        _connection=connection,
                    )
                    WorkflowCoordinator(self.repository).reject_plan_product(
                        workflow_id=revision.workflow_id,
                        _connection=connection,
                    )
            finally:
                self._worktree_locks.release(revision.aggregate_id)
                try:
                    self.repository.release_lease(
                        lease_resource,
                        invocation_id,
                        fencing_token,
                    )
                except (LeaseConflict, StaleFencingToken):
                    pass
            return {"result_artifact_ref": finding_ref.to_dict(), "status": "rejected"}
        if workspace_content_fingerprint(workspace_path) != before:
            raise RuntimeError("architecture worktree content changed while the Manager created its commit")
        manifest_payload = self.service.artifacts.read_json(manifest_ref)
        effective_requirements_ref = _ref_from_mapping(manifest_payload.get("requirements_ref"))
        try:
            with self.repository.transaction() as connection:
                current = self.repository.read_snapshot(
                    AggregateType.ARCHITECTURE_REVISION,
                    revision.aggregate_id,
                    _connection=connection,
                )
                if current is None:
                    raise SubmissionInvariantError(
                        "architecture revision disappeared before publication"
                    )
                self.repository.dispatch(
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
                    _connection=connection,
                )
                WorkflowCoordinator(self.repository).submit_plan_product(
                    workflow_id=revision.workflow_id,
                    product_ref=manifest_ref.sha256,
                    _connection=connection,
                )
        finally:
            self._worktree_locks.release(revision.aggregate_id)
            try:
                self.repository.release_lease(
                    lease_resource,
                    invocation_id,
                    fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
        return {"result_artifact_ref": manifest_ref.to_dict()}

    async def _run_architecture_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = self._effect_snapshot(effect)
        if self._architecture_worker_suppressed(
            revision,
            running_state="REVIEWING",
            start_action="START_ARCHITECTURE_REVIEW",
        ):
            return {"status": "superseded"}
        manifest_ref = _ref_from_mapping(
            revision.payload.get("architecture_manifest_ref")
        )
        record = self.repository.read_artifact_record(manifest_ref.sha256)
        if (
            record is None
            or str(record.get("artifact_type") or "") != CONTRACT_ARTIFACT
        ):
            raise SubmissionInvariantError(
                "architecture review requires the current ContractArtifact"
            )
        return await self._run_contract_architecture_review(
            effect, revision, manifest_ref
        )

    async def _run_contract_architecture_review(
        self,
        effect: Mapping[str, Any],
        revision: AggregateSnapshot,
        manifest_ref: ArtifactRef,
    ) -> Mapping[str, Any]:
        """Review one immutable ContractArtifact without rebuilding its authoring model."""

        artifact = dict(self.service.artifacts.read_json(manifest_ref))
        if dict(artifact.get("requirements_ref") or {}) != dict(
            revision.payload.get("requirements_ref") or {}
        ):
            raise ValueError(
                "contract reviewer requirements differ from the Architect input"
            )
        contract = dict(artifact.get("contract") or {})
        contract_schema = str(
            artifact.get("contract_schema") or ""
        )
        invocation_id = architecture_reviewer_session_id(
            revision.workflow_id,
            revision.aggregate_id,
            revision.payload,
        )
        lease_resource = f"architecture:{revision.aggregate_id}:review"
        if revision.state == "REVIEWING":
            active = self.repository.read_lease(lease_resource)
            if (
                active
                and str(active.get("owner_id") or "")
                and _lease_is_live(active)
            ):
                self._start_plan_cycle_assignment(
                    workflow_id=revision.workflow_id,
                    effect=effect,
                    slot=CycleSlot.CHECKER,
                )
                return {
                    "status": "already_running",
                    "active_worker_id": str(active["owner_id"]),
                }
        lease = self.repository.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": revision.workflow_id,
                "aggregate_id": revision.aggregate_id,
                "stage": "contract_review",
            },
        )
        review_workspace = None
        try:
            action_type = (
                "START_ARCHITECTURE_REVIEW"
                if "START_ARCHITECTURE_REVIEW"
                in self.repository.engine.legal_actions(
                    AggregateType.ARCHITECTURE_REVISION,
                    revision.state,
                )
                else "REBIND_ARCHITECTURE_REVIEW"
            )
            with self.repository.transaction() as connection:
                revision = self.repository.dispatch(
                    ActionEnvelope(
                        action_type=action_type,
                        workflow_id=revision.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision.aggregate_id,
                        actor=invocation_id,
                        expected_version=revision.version,
                        idempotency_key=(
                            f"effect:{effect['effect_key']}:"
                            f"{action_type.lower()}:{lease.fencing_token}"
                        ),
                        payload={
                            "fencing_token": lease.fencing_token,
                            "active_worker_id": invocation_id,
                            "lease_resource_key": lease_resource,
                            "active_role": OrchestrationRole.REVIEWER.value,
                            "active_role_mode": RoleMode.ARCHITECTURE.value,
                        },
                    ),
                    _connection=connection,
                ).snapshot
                self._start_plan_cycle_assignment(
                    workflow_id=revision.workflow_id,
                    effect=effect,
                    slot=CycleSlot.CHECKER,
                    _connection=connection,
                )
            requirements_ref = _ref_from_mapping(artifact["requirements_ref"])
            contract_view_ref = self.service.artifacts.put_json(
                {
                    "schema_version": "1",
                    "contract": contract,
                },
                artifact_type="ContractReviewViewArtifact",
                provenance={"owner": "manager", "audience": "reviewer"},
                child_refs=((manifest_ref.sha256, "contract"),),
            )
            references: dict[str, ArtifactRef] = {
                "task": requirements_ref,
                "contract": contract_view_ref,
            }
            workspace_override: dict[str, Any] | None = None
            if self._uses_git_skeleton(revision.workflow_id):
                projected = {
                    **artifact,
                    "submission": software_contract_projection(
                        contract
                    ),
                }
                requirements_payload = self.service.artifacts.read_json(
                    requirements_ref
                )
                review_workspace = self.service.skeleton.provision_review_worktree(
                    artifact=projected,
                    review_name=(
                        f"{revision.aggregate_id}-{manifest_ref.sha256[:12]}"
                    ),
                )
                mechanical = review_architecture_skeleton(
                    projected,
                    worktree=review_workspace.worktree,
                    requirements_payload=requirements_payload,
                )
                if mechanical.verdict != "PASS":
                    review_ref = self.service.artifacts.put_json(
                        mechanical.to_dict(),
                        artifact_type="ContractReviewArtifact",
                        child_refs=((manifest_ref.sha256, "contract"),),
                    )
                    self._dispatch_architecture_review_result(
                        revision,
                        mechanical,
                        review_ref,
                        effect=effect,
                    )
                    return {"result_artifact_ref": review_ref.to_dict()}
                diff_text = subprocess.check_output(
                    [
                        "git",
                        "-C",
                        str(review_workspace.worktree),
                        "diff",
                        "--find-renames",
                        str(artifact.get("base_commit_sha") or ""),
                        str(artifact.get("skeleton_commit_sha") or ""),
                        "--",
                    ],
                    text=True,
                )
                references["contract_diff"] = self.service.artifacts.put_bytes(
                    diff_text.encode("utf-8"),
                    artifact_type="ContractDeclarationDiffArtifact",
                    media_type="text/x-diff",
                    child_refs=((manifest_ref.sha256, "contract"),),
                )
                workspace_override = {
                    "kind": "existing_repo",
                    "repo_path": str(review_workspace.worktree),
                    "workspace_binding": "canonical",
                    "project_name": "contract-review",
                }
            else:
                workflow = self.repository.read_snapshot(
                    AggregateType.WORKFLOW,
                    revision.workflow_id,
                )
                request = workflow_request_from_snapshot(self.service, workflow)
                workspace_override = dict(request.get("workspace") or {})
            _bind_architecture_edit_instruction_for_review(
                references,
                revision,
            )
            finding_value = architecture_revision_finding_value(
                revision.payload
            )
            if finding_value:
                references["prior_finding"] = (
                    self._publish_architecture_finding_view(
                        finding_value,
                        audience="reviewer",
                    )
                )
            root_batch_value = revision.payload.get("replan_finding_batch_ref")
            if root_batch_value:
                references["replan_finding_batch"] = (
                    self._publish_architecture_finding_view(
                        root_batch_value,
                        audience="reviewer",
                    )
                )
            terminal, prompt_ref, terminal_ref = await self._run_profile(
                effect=effect,
                snapshot=revision,
                invocation_id=invocation_id,
                lease_resource=lease_resource,
                fencing_token=lease.fencing_token,
                profile=self._profile_for_role(
                    revision.workflow_id,
                    "reviewer",
                ),
                activation=RoleActivation(
                    OrchestrationRole.REVIEWER,
                    RoleMode.ARCHITECTURE,
                ),
                instruction=(
                    "Review the complete immutable contract against the same "
                    "ordered task ledger used by the Architect. Use contract "
                    "as the module-level semantic truth and, when present, "
                    "contract_diff plus public declarations/comments as the "
                    "symbol-level truth. Audit every requirement and module "
                    "breadth-first, then trace success and material failure "
                    "paths through the contract graph. Regress every bound "
                    "prior finding and every touched accepted invariant before "
                    "reviewing the current diff and its affected semantic "
                    "neighborhood for new defects. A prior PASS is context, "
                    "never evidence for this Candidate. Reuse unchanged "
                    "investigation instead of rereading it. Record all "
                    "independent defects with add_finding, complete the "
                    "checklist, and call review_submit exactly once. Do not "
                    "repair or privately redesign the implementation."
                ),
                reference_refs=references,
                workspace_override=workspace_override,
                prepare_workspace=bool(workspace_override),
            )
            payload = _named_json_output(
                terminal,
                "contract_review.json",
            )
            semantic = _parse_skeleton_review(payload)
            review_ref = self.service.artifacts.put_json(
                {
                    **semantic.to_dict(),
                    "contract_schema": contract_schema,
                    "work_items": submission_work_items(
                        payload.get("work_items")
                    ),
                },
                artifact_type="ContractReviewArtifact",
                child_refs=((manifest_ref.sha256, "contract"),),
            )
            self._dispatch_architecture_review_result(
                revision,
                semantic,
                review_ref,
                effect=effect,
                assignment_id=self._terminal_role_assignment_id(terminal),
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
            return {
                "provider_request_id": invocation_id,
                "result_artifact_ref": review_ref.to_dict(),
            }
        finally:
            if review_workspace is not None:
                review_workspace.cleanup()
            try:
                self.repository.release_lease(
                    lease_resource,
                    invocation_id,
                    lease.fencing_token,
                )
            except Exception:
                pass

    async def _publish_human_architecture_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        revision = self._effect_snapshot(effect)
        manifest_ref = _ref_from_mapping(revision.payload.get("architecture_manifest_ref"))
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            raise ValueError("architecture revision has no workflow")
        stored_card = dict(revision.payload.get("human_review_card_ref") or {})
        if stored_card:
            stored_ref = _ref_from_mapping(stored_card)
            stored_payload = dict(self.service.artifacts.read_json(stored_ref))
            if human_review_card_is_current(
                stored_payload,
                manifest_sha=manifest_ref.sha256,
            ):
                card_ref = stored_ref
                payload = stored_payload
            else:
                stored_card = {}
        if not stored_card:
            actor = str(workflow.payload.get("owner") or "pal")
            record = self.repository.read_artifact_record(manifest_ref.sha256)
            if (
                record
                and str(record.get("artifact_type") or "")
                == CONTRACT_ARTIFACT
            ):
                markdown = self.service.render_human_review_markdown(revision)
                payload = {
                    "render_version": HUMAN_REVIEW_RENDER_VERSION,
                    "workflow_id": revision.workflow_id,
                    "architecture_revision_id": revision.aggregate_id,
                    "manifest_sha": manifest_ref.sha256,
                    "actor_id": actor,
                    "markdown": markdown,
                    "actions": ["accept", "edit", "reject"],
                }
            else:
                raise SubmissionInvariantError(
                    "human architecture review requires a ContractArtifact"
                )
            manifest_payload = dict(self.service.artifacts.read_json(manifest_ref))
            task_ledger_value = manifest_payload.get("requirements_ref")
            replan_batch_value = revision.payload.get("replan_finding_batch_ref")
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
            if isinstance(task_ledger_value, Mapping) and task_ledger_value.get("sha256"):
                task_ledger_ref = _ref_from_mapping(task_ledger_value)
                task_record = self.repository.read_artifact_record(task_ledger_ref.sha256)
                if task_record and str(task_record.get("artifact_type") or "") == TASK_LEDGER_ARTIFACT:
                    for source in self.service.task_ledger.source_attachments(task_ledger_ref):
                        source_ref = _ref_from_mapping(source["artifact_ref"])
                        source_record = self.repository.read_artifact_record(source_ref.sha256)
                        if source_record is None:
                            continue
                        attachments.append(
                            {
                                "path": str(source_record["storage_path"]),
                                "file_name": str(source["name"]).replace("/", "__"),
                                "mime_type": str(source["media_type"]),
                                "caption": "Immutable task ledger: original plus ordered revisions",
                            }
                        )
                        card_children.append((source_ref.sha256, "task_ledger"))
            payload["attachments"] = attachments
            card_children.append((markdown_ref.sha256, "architecture_markdown"))
            if replan_batch_value:
                card_children.append(
                    (_ref_from_mapping(replan_batch_value).sha256, "replan_findings")
                )
            payload["decision_token"] = self.repository.issue_human_decision_token(
                workflow_id=revision.workflow_id,
                architecture_revision_id=revision.aggregate_id,
                manifest_sha=manifest_ref.sha256,
                actor_id=actor,
            )
            card_ref = self.service.artifacts.put_json(
                payload,
                artifact_type="HumanReviewCardArtifact",
                child_refs=tuple(card_children),
            )
            current = self.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision.aggregate_id)
            if current is None:
                raise ValueError("architecture revision disappeared before human review publication")
            persisted_ref = dict(current.payload.get("human_review_card_ref") or {})
            if str(persisted_ref.get("sha256") or "") != card_ref.sha256:
                current = self.repository.dispatch(
                    ActionEnvelope(
                        action_type="HUMAN_REVIEW_PUBLISHED",
                        workflow_id=revision.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision.aggregate_id,
                        actor="bunshin-v2-manager",
                        expected_version=current.version,
                        idempotency_key=(
                            f"human-review-published:v{HUMAN_REVIEW_RENDER_VERSION}:"
                            f"{effect.get('effect_id') or card_ref.sha256}:{card_ref.sha256}"
                        ),
                        payload={"human_review_card_ref": card_ref.to_dict()},
                    )
                ).snapshot
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
        status = str(effect.get("status") or revision.state.lower())
        if self.publish_workflow_event is not None and status in {
            "accepted",
            "revision_requested",
            "rejected",
        }:
            self.publish_workflow_event(
                {
                    "event_kind": "architecture_review_resolved",
                    "workflow_id": revision.workflow_id,
                    "architecture_revision_id": revision.aggregate_id,
                    "status": status,
                    "summary": f"Bunshin architecture decision recorded ({status}).",
                    "resolved_at": str(getattr(revision, "updated_at", "") or ""),
                }
            )
        root = PlanRevisionProjectionStore(self.service.runtime_root).update_status(
            workflow_id=revision.workflow_id,
            revision_id=revision.aggregate_id,
            architecture_artifact=artifact,
            status=status,
        )
        return {"status": status, "projection_path": str(root)}

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
        requirements_ref = _ref_from_mapping(revision.payload.get("requirements_ref"))
        finding_value = architecture_revision_finding_value(revision.payload)
        base_manifest_ref = self._revision_input_base_manifest_ref(revision)
        refs: dict[str, ArtifactRef]
        scoped_revision = base_manifest_ref is not None and finding_value is not None
        if base_manifest_ref is None:
            refs = {"task": requirements_ref}
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
            "Read the ordered read-only task ledger and perform one bounded consistency pass. Then immediately call update_checklist to "
            "complete requirements/design and begin contract encoding. Use that checklist as the execution cursor: work only on its current "
            "phase, externalize each settled phase through the Contract tools before moving on, and batch independent calls in one response. "
            "Resolve only architecture-feasibility questions, declare the smallest complete directional contract DAG and end-to-end "
            "integration, defer private implementation, reconcile the Contract against the task, complete the checklist, and submit."
        )
        if scoped_revision:
            instruction += (
                " This is a guided revision: read revision_scope first and consult task.yaml only when exact upstream task semantics are needed; "
                "do not reread the repository, workflow request, or base manifest. The manager has preseeded the complete base "
                "contract privately. Start from the named semantic targets with the same incremental Contract tools used for initial authoring. "
                "Preserve unrelated semantics unless contract consistency requires a wider correction; revision_scope is repair guidance, not a write fence."
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
        base_artifact = dict(
            self.service.artifacts.read_json(base_manifest_ref)
        )
        if not isinstance(base_artifact.get("contract"), Mapping):
            raise ValueError(
                "architecture revision baseline is not a ContractArtifact"
            )
        scope = {
            "schema_version": "1",
            "findings": [dict(item) for item in list(dict(finding_payload).get("findings") or [])],
            "instruction": (
                "Revise the preseeded architect.yaml in place. Resolve every "
                "finding while preserving unrelated accepted semantics."
            ),
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
        """Run one logical role while its aggregate ownership stays live.

        The aggregate lease belongs to the durable logical coroutine and is
        therefore renewed while it waits for native-process capacity.  The
        attempt lease is deliberately created later, inside
        ``_run_profile_inner``, only after a process permit is available.
        """

        self.repository.renew_lease(
            lease_resource,
            invocation_id,
            fencing_token,
            ttl_seconds=120,
        )
        heartbeat = asyncio.create_task(
            self._lease_heartbeat(
                lease_resource,
                invocation_id,
                fencing_token,
            )
        )
        try:
            return await self._run_profile_inner(
                effect=effect,
                snapshot=snapshot,
                invocation_id=invocation_id,
                lease_resource=lease_resource,
                fencing_token=fencing_token,
                profile=profile,
                activation=activation,
                instruction=instruction,
                reference_refs=reference_refs,
                workspace_override=workspace_override,
                prepare_workspace=prepare_workspace,
            )
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self._role_supervisor.release_process_slot()

    async def _run_profile_inner(
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
        harness_generation = self.harness_registry.snapshot()
        preferred_harness = harness_generation.select(role)
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
        if not bool(workspace.get("v2_role_workspace")):
            invocation_dir = invocation_root(self.service.runtime_root) / invocation_id
            attempt_dir = invocation_dir / "attempts" / f"fence-{fencing_token}"
            workspace.update(
                {
                    "run_dir": str(invocation_dir),
                    "artifact_dir": str(attempt_dir / "artifacts"),
                    "artifact_stage_dir": str(attempt_dir / "artifact-stage"),
                    "log_dir": str(attempt_dir / "logs"),
                    "build_scratch_dir": str(attempt_dir / "build-scratch"),
                    "review_scratch_dir": str(
                        workspace.get("review_scratch_dir")
                        or attempt_dir / "review-scratch"
                    ),
                }
            )
            for key in (
                "artifact_dir",
                "artifact_stage_dir",
                "log_dir",
                "build_scratch_dir",
                "review_scratch_dir",
            ):
                Path(str(workspace[key])).mkdir(parents=True, exist_ok=True)
        contract_authoring = bool(workspace.get("contract_authoring_mode"))
        bound_reference_refs = dict(reference_refs)
        if bool(workspace_policy.get("prepare", False)):
            workspace, preparation = prepare_v2_workspace_environment(
                workspace,
                runtime_root=self.service.runtime_root,
            )
            # LSP is implementation/verification evidence.  Architect and
            # architecture-review roles only author/compile-check contracts;
            # a missing optional language server must never block that phase.
            lsp_role = activation.role in {
                OrchestrationRole.IMPLEMENTATION,
                OrchestrationRole.VERIFIER,
            }
            if (
                lsp_role
                and bool(workspace_policy.get("prewarm_lsp", False))
                and list(workspace.get("languages") or [])
            ):
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
        verification_tool_contract: dict[str, Any] | None = None
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
            system_delivery_view = (
                self.service.artifacts.read_json(
                    bound_reference_refs["system_delivery_view"]
                )
                if "system_delivery_view" in bound_reference_refs
                else None
            )
            family_verification_policy = dict(
                family_policies.get("verification") or {}
            )
            effective_policy = effective_verification_policy(
                work_view=view,
                verification_policy=family_verification_policy,
                system_delivery_view=system_delivery_view,
            )
            if activation.role == OrchestrationRole.VERIFIER:
                verification_tool_contract = (
                    compile_verification_invocation_tool_contract(
                        work_view=view,
                        verification_policy=family_verification_policy,
                        system_delivery_view=system_delivery_view,
                    )
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
            priority = {
                "repair_bill": 0,
                "module_work_view": 1,
                "unit_work_view": 1,
                "workspace_preparation": 2,
            }
            reference_items.sort(key=lambda item: (priority.get(item[0], 3), item[0]))
        for name, ref in reference_items:
            includes: list[str] = []
            if ref.artifact_type == "LocalPathReference":
                path = str(ref.media_type)
            elif ref.artifact_type == TASK_LEDGER_ARTIFACT:
                materialized = self.service.task_ledger.materialize(ref)
                path = str(materialized.root)
                includes = list(materialized.files)
            else:
                record = self.repository.read_artifact_record(ref.sha256)
                if record is None:
                    raise ValueError(f"worker input artifact is unavailable: {name}")
                path = str(
                    self.service.task_ledger.materialize_artifact(
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
                    **({"include": includes} if includes else {}),
                }
            )
        workspace["reference_paths"] = references
        profile_group, profile_name = canonical_role_profile_parts(profile)
        if contract_authoring and activation.role == OrchestrationRole.ARCHITECT:
            invocation_acceptance = [
                "Write only the declaration-level code skeleton in the bound architecture worktree; never compile, build, test, link, or execute it.",
                "Finish only when boundaries and responsibilities, unique state/resource owners, contracts, and closed lifecycle/joins are declared and implementation details are explicitly deferred.",
                "Use update_checklist as the fixed durable phase cursor: settle requirements and the complete semantic module graph first; then write only declaration skeletons; only then fill the Manager-preseeded architect.yaml, reconcile both projections, complete the checklist, and call contract_submit with no arguments. Never work ahead of the current phase.",
            ]
        elif (
            contract_authoring or profile_group == "software_engineering"
        ) and activation == RoleActivation(
            OrchestrationRole.REVIEWER,
            RoleMode.ARCHITECTURE,
        ):
            invocation_acceptance = [
                "This is architecture review, not product verification. Judge whether a future Coder can implement the task from the declarations and semantic DAG; never inspect or execute private bodies merely to show that requested behavior is not implemented yet.",
                "Before reading a bound reference, investigate what its supplied path currently contains and choose a matching tool; never pass an unclassified path to read_file or assume it is a file. Once an exact file is known, read it directly without repeating discovery.",
                "Read the skeleton diff first. Product control flow, private implementation bodies, or complete tests introduced by the Architect are implementation leakage and must be rejected even when they make requested behavior work.",
                "This logical Reviewer persists across Candidates, but no verdict does. For every new Candidate, first regress all prior findings and touched accepted invariants; then inspect the current skeleton diff and affected semantic neighborhood for new defects. Reuse unchanged investigation instead of rereading it.",
                "Review the bound task.yaml ledger in order, code contracts, semantic dependencies, and scenarios; reconcile every exact Manager-recorded question and answer.",
                "Treat the Manager-derived tests/<module_name>/developer and tests/<module_name>/verifier corpora as implementation and verification infrastructure: they are intentionally absent from Architect-declared paths and scenarios, so their absence is not a defect.",
                "Compile only focused declaration/protocol consumers to confirm contracts compose; compilation is not product behavior proof and must not require implementation bodies.",
                "For every Requirement and observable scenario claim, trace declared interface semantics from a concrete entrypoint through data/state/error transitions to a legal terminal. Explicit composable semantics are required; current implementation availability and current end-to-end behavior are outside this verdict.",
                "For every module, verify responsibility, dependency handoffs and consumed outputs, input/output/error/invariant contracts, ownership, lifecycle, optional state machine, and agreement between architect.yaml and declaration comments.",
                "Before PASS, reject every public semantic ambiguity: audit absent/null/empty/zero-length inputs, partial output followed by failure and its commitment/consumption/post-error state, and all permitted copy/move/clone/share/reset/reuse operations on public stateful values. If two conforming implementations may make observably different choices that a consumer must know, add a finding; review_guarded private implementation freedom cannot close that gap.",
                "PASS only when key scenarios traverse the contract graph, failure paths terminate legally, every Requirement maps, no dependency is undeclared, and every observable edge case has one declared outcome or an explicitly declared set of outcomes safe for every consumer.",
                "Inspect the complete Manager-bound scope, record every material defect with add_finding, complete the checklist, and call review_submit once.",
            ]
        elif activation == RoleActivation(
            OrchestrationRole.REVIEWER,
            RoleMode.ARCHITECTURE,
        ):
            invocation_acceptance = [
                "Review the ordered task ledger and the complete immutable contract breadth-first; never stop at the first defect.",
                "Trace each requirement through module dependencies, ownership, lifecycle, errors, and scenarios to a legal success or failure endpoint. Schema validity is not semantic proof.",
                "Record every independent defect with add_finding, complete the checklist, and call review_submit exactly once. Do not repair or redesign private implementation.",
            ]
        elif activation.role == OrchestrationRole.ARCHITECT:
            invocation_acceptance = [
                "Read the ordered task ledger and perform one bounded consistency pass before authoring Contract fields.",
                "Immediately call update_checklist after that pass, then work only on its current phase. Externalize each settled phase to the durable Contract Draft before moving on; batch independent tool calls in one response and sequence only dependent definitions.",
                "Complete the fixed checklist, reconcile topology and end-to-end integration against the task, then call contract_submit with no arguments. The checklist is a cursor, never contract truth or review evidence.",
            ]
        elif activation.role == OrchestrationRole.VERIFIER:
            invocation_acceptance = [
                "Read and run both durable corpora; extend only tests/<module_name>/verifier and only for a demonstrated coverage gap, while tests/<module_name>/developer remains read-only. When this module is the authored graph sink, keep its end-to-end and delivery cases in the same verifier corpus. Run evidence with shell/LSP tools and classified read-only Git queries through shell.",
                "For this assignment, first record every required current/historical regression, then record a current-Candidate diff-risk check for newly introduced defects. A failing regression blocks PASS but never skips the diff-risk phase.",
                "Call exactly one semantic verification outcome tool; do not construct a VerificationPlan or evidence JSON.",
            ]
        elif activation.role == OrchestrationRole.IMPLEMENTATION:
            if self._execution_adapter(snapshot) == SOFTWARE_GIT_ADAPTER:
                invocation_acceptance = [
                    "Implement or repair only the bound module, write focused tests only in tests/<module_name>/developer, and keep tests/<module_name>/verifier read-only.",
                    "Maintain the compact durable checklist with update_checklist; it is a micro-plan, not evidence. Complete it, run the minimum sufficient self-check with ordinary shell or LSP tools, then call candidate_submit with no arguments.",
                ]
            else:
                invocation_acceptance = [
                    "Write the contracted product artifact in the bound workspace and run a focused validation.",
                    "Maintain a compact checklist with update_checklist, run one focused self-check with ordinary available tools, then call candidate_submit with no arguments; do not write producer_report.json.",
                ]
        elif activation.mode == RoleMode.STANDALONE:
            invocation_acceptance = [
                "Review only the bound immutable target and run reproducible read-only probes.",
                "Use the checklist as the audit cursor, record every independent defect with add_finding, then call review_submit with no arguments.",
            ]
        else:
            invocation_acceptance = ["Write the exact primary JSON artifact required by the profile output contract."]
        mandatory_inputs: list[str] = []
        evaluation_generation = (
            int(snapshot.payload.get("architecture_review_generation") or 0)
            if activation
            == RoleActivation(
                OrchestrationRole.REVIEWER,
                RoleMode.ARCHITECTURE,
            )
            else 0
        )
        input_fingerprint = authoring_input_fingerprint(
            {
                "role": role,
                "mode": mode,
                "references": _semantic_role_input_refs(
                    {
                        name: ref.to_dict()
                        for name, ref in bound_reference_refs.items()
                    },
                    role=role,
                    mode=mode,
                ),
                "architecture_revision_base_submission": workspace.get(
                    "architecture_revision_base_submission"
                ),
                "architecture_revision_scope": workspace.get(
                    "architecture_revision_scope"
                ),
                "evaluation_generation": evaluation_generation,
            }
        )
        pack = BunshinInvocationPack(
            invocation_id=invocation_id,
            goal=instruction,
            instruction=instruction,
            acceptance_criteria=invocation_acceptance,
            workspace=workspace,
            profile_group=profile_group,
            profile_name=profile_name,
            bunshin_profile=profile,
            metadata={
                "bunshin_v2": {
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
                    "role_profile_id": profile,
                    "family_binding_sha": str(binding_ref.get("sha256") or ""),
                    "submission_receipt_required": True,
                    "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
                    "authoring_input_fingerprint": input_fingerprint,
                    **(
                        {
                            "verification_tool_contract": verification_tool_contract
                        }
                        if verification_tool_contract is not None
                        else {}
                    ),
                },
                "agent_session": {
                    "session_id": invocation_id,
                    "response_key": input_fingerprint,
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
            if contract_authoring:
                revision_scope = dict(workspace.get("architecture_revision_scope") or {}) or None
            else:
                revision_scope = {"manager_routed_findings": True}
        role_binding = dict(validate_family_binding_payload(binding)[role])
        pinned_profile = dict(role_binding.get("role_profile") or {})
        if not pinned_profile:
            raise ValueError(
                f"FamilyBindingArtifact has no pinned role profile for role {role}"
            )
        pinned_profile = _role_mode_profile_payload(pinned_profile, mode=mode)
        pack = resolve_pinned_bunshin_pack(
            pack,
            profile_payload=pinned_profile,
            family_payload=binding,
        )
        role_protocol = dict(pinned_profile.get("role") or {})
        if str(role_protocol.get("kind") or "") != role:
            raise ValueError(
                f"pinned role protocol kind does not match activation {role}"
            )
        if mode not in {
            str(item)
            for item in list(role_protocol.get("modes") or [])
        }:
            raise ValueError(
                f"pinned role protocol does not support mode {mode}"
            )
        playbook = dict(role_protocol.get("playbook") or {})
        playbook_steps = [
            dict(item)
            for item in list(playbook.get("steps") or [])
            if isinstance(item, Mapping)
        ]
        if playbook_steps:
            pack_value = pack.to_dict()
            metadata = dict(pack_value.get("metadata") or {})
            bunshin_v2 = dict(metadata.get("bunshin_v2") or {})
            bunshin_v2["role_protocol"] = role_protocol
            work_item_seed = [
                {
                    "kind": "phase",
                    "summary": str(item.get("key") or "").replace("_", " "),
                    "status": "pending",
                    "origin": "role_playbook",
                    "required": True,
                }
                for item in playbook_steps
                if str(item.get("key") or "").strip()
            ]
            routed_seen: set[str] = set()
            for reference_name in (
                "repair_bill",
                "prior_finding",
                "revision_finding",
                "replan_finding_batch",
            ):
                reference = bound_reference_refs.get(reference_name)
                if reference is None:
                    continue
                payload = dict(
                    self.service.artifacts.read_json(reference)
                )
                for finding in _manager_routed_findings(payload):
                    identity = str(
                        finding.get("finding_id")
                        or finding.get("finding_key")
                        or stable_hash(finding)[:16]
                    )
                    if identity in routed_seen:
                        continue
                    routed_seen.add(identity)
                    work_item_seed.append(
                        {
                            "kind": "task",
                            "summary": f"resolve finding: {identity}",
                            "status": "pending",
                            "origin": "manager_routed_finding",
                            "required": True,
                        }
                    )
            if (
                activation.role == OrchestrationRole.VERIFIER
                and "system_delivery_view" in bound_reference_refs
            ):
                system_delivery = self.service.artifacts.read_json(
                    bound_reference_refs["system_delivery_view"]
                )
                work_item_seed.extend(
                    _manager_required_system_scenario_work_items(system_delivery)
                )
            bunshin_v2["work_item_seed"] = work_item_seed
            metadata["bunshin_v2"] = bunshin_v2
            # Keep derived role protocol data in the invocation workspace as
            # well as metadata.  The runner merges both projections for live
            # tools, but a checkpoint/recovery probe and the model's visible
            # pack may inspect workspace directly.  Having one complete
            # assignment binding prevents a missing checklist seed or role
            # protocol from masquerading as a submission/orchestration bug.
            workspace_value = dict(pack_value.get("workspace") or {})
            workspace_bunshin_v2 = dict(workspace_value.get("bunshin_v2") or {})
            workspace_bunshin_v2.update(
                {
                    key: value
                    for key, value in bunshin_v2.items()
                    if key not in workspace_bunshin_v2
                }
            )
            workspace_value["bunshin_v2"] = workspace_bunshin_v2
            pack = BunshinInvocationPack.from_dict(
                {**pack_value, "workspace": workspace_value, "metadata": metadata}
            )
        pack = apply_v2_role_capability_policy(pack, activation=activation)
        if activation.role == OrchestrationRole.ARCHITECT and revision_scope is not None:
            pack = apply_v2_revision_scope_capability_policy(pack)
        pack = apply_v2_research_capability_policy(
            pack,
            research_mode=str(snapshot.payload.get("research_mode") or "local_only"),
        )
        if activation.role == OrchestrationRole.VERIFIER:
            view_ref = bound_reference_refs.get("module_work_view")
            if view_ref is not None:
                tool_contract = compile_swe_verification_tool_contract(
                    self.service.artifacts.read_json(view_ref),
                    repair_path_owners=_verification_repair_path_owners(
                        self.repository,
                        snapshot,
                    ),
                )
                pack_value = pack.to_dict()
                metadata = dict(pack_value.get("metadata") or {})
                bunshin_v2 = dict(metadata.get("bunshin_v2") or {})
                bunshin_v2["swe_verification_tool_contract"] = tool_contract
                metadata["bunshin_v2"] = bunshin_v2
                workspace_value = dict(pack_value.get("workspace") or {})
                workspace_bunshin_v2 = dict(workspace_value.get("bunshin_v2") or {})
                workspace_bunshin_v2["swe_verification_tool_contract"] = tool_contract
                workspace_value["bunshin_v2"] = workspace_bunshin_v2
                resolved_profile = dict(pack_value.get("resolved_profile") or {})
                guidance_overrides = merge_tool_guidance_overrides(
                    resolved_profile.get("capability_guidance_overrides"),
                    tool_contract.get("guidance_overrides"),
                )
                resolved_profile["capability_guidance_overrides"] = guidance_overrides
                pack = BunshinInvocationPack.from_dict(
                    {
                        **pack_value,
                        "workspace": workspace_value,
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
                    # Build output is attempt-local.  A durable role prompt
                    # is intentionally reused across retries, but carrying
                    # its previous fence's scratch path into the new process
                    # makes the worker inspect and write stale evidence.
                    "build_scratch_dir": str(attempt_dir / "build-scratch"),
                    "review_scratch_dir": str(
                        bound_workspace.get("review_scratch_dir")
                        or attempt_dir / "review-scratch"
                    ),
                }
            )
            for key in (
                "artifact_dir",
                "artifact_stage_dir",
                "log_dir",
                "build_scratch_dir",
                "review_scratch_dir",
            ):
                Path(str(bound_workspace[key])).mkdir(parents=True, exist_ok=True)
            pack = BunshinInvocationPack.from_dict({**pack.to_dict(), "workspace": bound_workspace})
        if activation.role == OrchestrationRole.ARCHITECT:
            architecture_binding = dict(
                binding.get("architecture_definition") or {}
            )
            template_ref = dict(
                architecture_binding.get("template_ref") or {}
            )
            if not template_ref:
                raise ValueError(
                    "FamilyBindingArtifact has no pinned architect template"
                )
            template_payload = dict(
                self.service.artifacts.read_json(template_ref)
            )
            if (
                str(template_payload.get("specialization_id") or "")
                != str(architecture_binding.get("specialization_id") or "")
                or str(template_payload.get("generation_hash") or "")
                != str(architecture_binding.get("generation_hash") or "")
            ):
                raise ValueError(
                    "pinned architect template does not match its Family binding"
                )
            base_contract: Mapping[str, Any] | None = None
            if base_manifest_ref is not None:
                base_record = self.repository.read_artifact_record(
                    base_manifest_ref.sha256
                )
                if (
                    base_record is not None
                    and str(base_record.get("artifact_type") or "")
                    == "ContractArtifact"
                ):
                    base_value = dict(
                        self.service.artifacts.read_json(base_manifest_ref)
                    )
                    candidate = base_value.get("contract")
                    if isinstance(candidate, Mapping):
                        base_contract = dict(candidate)
            pack_value = pack.to_dict()
            bound_workspace = bind_architect_file(
                dict(pack_value.get("workspace") or {}),
                template=str(template_payload.get("template") or ""),
                base_contract=base_contract,
            )
            pack = BunshinInvocationPack.from_dict(
                {
                    **pack_value,
                    "workspace": bound_workspace,
                }
            )
        submission_kind = _role_submission_kind(
            activation,
            contract_authoring=contract_authoring,
        )
        durable_input_refs = {
            name: ref.to_dict()
            for name, ref in bound_reference_refs.items()
            if ref.artifact_type != "LocalPathReference"
        }
        assignment = self._retry_assignment_for_effect(
            effect,
            snapshot=snapshot,
            role=role,
            mode=mode,
            submission_kind=submission_kind,
        )
        reusable_assignment = (
            None
            if assignment is not None
            else self._reusable_role_assignment(
                workflow_id=snapshot.workflow_id,
                aggregate_type=snapshot.aggregate_type.value,
                aggregate_id=snapshot.aggregate_id,
                role=role,
                mode=mode,
                submission_kind=submission_kind,
                input_refs=durable_input_refs,
                evaluation_generation=evaluation_generation,
            )
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
        role_session = self.repository.ensure_role_session(
            session_id=invocation_id,
            workflow_id=snapshot.workflow_id,
            aggregate_type=snapshot.aggregate_type,
            aggregate_id=snapshot.aggregate_id,
            role=role,
            mode=mode,
            role_profile_id=profile,
            family_binding_sha=str(binding_ref.get("sha256") or ""),
            preferred_harness_id=preferred_harness.harness_id,
            preferred_harness_generation=(
                harness_generation.generation_hash
            ),
            scope_kind=session_scope_kind,
            subject_key=session_subject_key,
        )
        durable_prompt_reused = False
        if assignment is None:
            try:
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
                        role_profile_id=profile,
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
                            "role": role,
                            "payload": dict(effect.get("payload") or {}),
                            "evaluation_generation": evaluation_generation,
                        },
                        submission_kind=submission_kind,
                    )
                )
            except ValueError as exc:
                # Triage/rebind cancellation and assignment creation are
                # separate durable effects.  During that small window the
                # previous assignment is still visible as open; this is a
                # retryable ordering race, not a role failure.
                if "role session already has an open assignment" in str(exc):
                    raise DeferredEffectError(str(exc)) from exc
                raise
        elif assignment["state"] in {
            RoleAssignmentState.CLAIMED.value,
            RoleAssignmentState.RUNNING.value,
        }:
            if self._role_assignment_attempt_is_live(assignment):
                raise DeferredEffectError("role assignment already has a live process attempt")
            assignment = self._queue_active_assignment_retry(
                assignment,
                error_kind="attempt_lease_expired",
                error_text="role attempt lease expired before submission settlement",
            )
            if assignment["state"] != RoleAssignmentState.RETRY_QUEUED.value:
                raise SubmissionInvariantError(
                    "expired active role assignment could not be made retryable"
                )
        # The assignment is the immutable source of truth for authoring
        # identity.  A retry may rebuild the role workspace from newer
        # orchestration inputs, but it must not derive a new fingerprint: the
        # Role Gateway authenticates every draft against this assignment.
        input_fingerprint = _assignment_input_fingerprint(assignment)
        harness_spec = _select_attempt_harness(
            harness_generation,
            role=role,
            prior_attempts=(
                self.repository.list_role_attempts(
                    str(assignment["assignment_id"])
                )
                if assignment is not None
                else ()
            ),
        )
        # A role session is the logical coroutine; a process attempt is only
        # its current shell.  Manager restart/reload can compile a new harness
        # registry generation even when the selected Pal harness is unchanged.
        # Keep the session's pinned generation for a same-harness continuation
        # so an encrypted checkpoint remains restorable across process shells.
        # A real harness switch still gets the current generation and therefore
        # cannot consume a checkpoint authored by another harness.
        session_harness_id = str(
            role_session.get("preferred_harness_id") or ""
        ).strip()
        session_harness_generation = str(
            role_session.get("preferred_harness_generation") or ""
        ).strip()
        completed_harness_attempt = (
            self.repository.read_latest_completed_role_harness_attempt(
                session_id=invocation_id,
                harness_id=harness_spec.harness_id,
            )
        )
        completed_harness_generation = str(
            (completed_harness_attempt or {}).get("harness_generation") or ""
        ).strip()
        if completed_harness_generation:
            effective_harness_generation = completed_harness_generation
        elif (
            session_harness_id == harness_spec.harness_id
            and session_harness_generation
        ):
            effective_harness_generation = session_harness_generation
        else:
            effective_harness_generation = harness_generation.generation_hash
        pal_checkpoint_capable = (
            harness_spec.launch_kind == HARNESS_LAUNCH_PAL_SANDBOX
        )
        if assignment["state"] in {
            RoleAssignmentState.QUEUED.value,
            RoleAssignmentState.RETRY_QUEUED.value,
        }:
            original_prompt_ref = self._durable_assignment_prompt_ref(assignment)
            prior_attempt = self.repository.read_role_attempt(
                str(assignment.get("active_attempt_id") or "")
            )
            same_harness = (
                prior_attempt is None
                or str(prior_attempt.get("harness_id") or PAL_HARNESS_ID)
                == harness_spec.harness_id
            )
            if original_prompt_ref is not None and same_harness:
                durable_pack = BunshinInvocationPack.from_dict(
                    dict(self.service.artifacts.read_json(original_prompt_ref))
                )
                pack = _refresh_ephemeral_role_reference_binds(durable_pack, pack)
                durable_prompt_reused = True
        self._signal_assignment_ready(effect, str(assignment["assignment_id"]))

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
        if not durable_prompt_reused:
            pack_value = pack.to_dict()
            metadata = dict(pack_value.get("metadata") or {})
            metadata["initial_skill_injections"] = self._role_session_skill_injections(
                request=request,
                workflow_id=snapshot.workflow_id,
                session_id=invocation_id,
            )
            pack = BunshinInvocationPack.from_dict(
                {
                    **pack_value,
                    "metadata": metadata,
                }
            )
        # A durable assignment is the queued logical coroutine.  It consumes
        # no native-process capacity and owns no attempt lease.  Wait for a
        # materialization slot before claiming the concrete attempt so queue
        # time can never expire a worker fence or consume retry budget.
        await self._role_supervisor.acquire_process_slot(run_id)
        attempt = self.repository.claim_role_assignment(
            str(assignment["assignment_id"]),
            harness_id=harness_spec.harness_id,
            harness_generation=effective_harness_generation,
        )
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
        # ``attempt_dir`` was provisioned before the assignment was claimed
        # and therefore reflects the caller's stale logical fence on retries.
        # Rebind it to the lease fence that authenticates this process shell;
        # otherwise every resumed worker silently shares an older build,
        # artifact and log directory.
        attempt_dir = (
            invocation_root(self.service.runtime_root)
            / invocation_id
            / "attempts"
            / f"fence-{assignment_lease.fencing_token}"
        )
        continuation_input_path, continuation_output_path = (
            self._prepare_agent_session_attempt(
                session_id=invocation_id,
                attempt_id=str(attempt["attempt_id"]),
            )
        )
        pack_value = pack.to_dict()
        # The durable role workspace is part of the worker's authoring
        # context.  Keep its lease identity in lockstep with the attempt
        # metadata below: retries keep the logical role session, but each
        # materialized attempt gets a new fencing owner.  Leaving the old
        # session id here makes draft_read present a context that the Role
        # Gateway (correctly) rejects as belonging to another assignment.
        workspace_value = dict(pack_value.get("workspace") or {})
        workspace_binding = dict(workspace_value.get("bunshin_v2") or {})
        workspace_binding.update(
            {
                # SubmissionDraftContext is reconstructed from the workspace
                # pack inside the worker.  Keep the complete immutable
                # authoring binding there, not only the per-attempt lease
                # fields; metadata.bunshin_v2 is not visible to that parser.
                "workflow_id": snapshot.workflow_id,
                "aggregate_type": snapshot.aggregate_type.value,
                "aggregate_id": snapshot.aggregate_id,
                "role": role,
                "mode": mode,
                "authoring_input_fingerprint": input_fingerprint,
                "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
                "invocation_id": str(attempt["attempt_id"]),
                "lease_resource": assignment_lease_resource,
                "lease_resource_key": assignment_lease_resource,
                "fencing_token": assignment_lease.fencing_token,
                "harness_id": harness_spec.harness_id,
                "harness_generation": effective_harness_generation,
                "harness_config": dict(harness_spec.config),
            }
        )
        workspace_value["bunshin_v2"] = workspace_binding
        workspace_value.update(
            {
                "artifact_dir": str(attempt_dir / "artifacts"),
                "artifact_stage_dir": str(attempt_dir / "artifact-stage"),
                "log_dir": str(attempt_dir / "logs"),
                "build_scratch_dir": str(attempt_dir / "build-scratch"),
            }
        )
        pack_value["workspace"] = workspace_value
        for key in (
            "artifact_dir",
            "artifact_stage_dir",
            "log_dir",
            "build_scratch_dir",
        ):
            Path(str(workspace_value[key])).mkdir(parents=True, exist_ok=True)
        metadata = dict(pack_value.get("metadata") or {})
        bunshin_v2 = dict(metadata.get("bunshin_v2") or {})
        bunshin_v2.update(
            {
                "invocation_id": str(attempt["attempt_id"]),
                "lease_resource": assignment_lease_resource,
                "lease_resource_key": assignment_lease_resource,
                "fencing_token": assignment_lease.fencing_token,
                # Keep both projections of the authoring binding aligned.
                # Retries may reuse a prompt whose derived workspace changed,
                # but the assignment fingerprint is immutable.
                "authoring_input_fingerprint": input_fingerprint,
                "harness_id": harness_spec.harness_id,
                "harness_generation": effective_harness_generation,
                "harness_config": dict(harness_spec.config),
            }
        )
        metadata["bunshin_v2"] = bunshin_v2
        metadata["agent_session"] = {
            "session_id": invocation_id,
            # A retry reuses the same durable assignment while a new
            # RepairBill receives a new assignment.  This is the semantic turn
            # identity used by the persistent role session.
            "response_key": str(assignment["assignment_id"]),
            "fencing_token": assignment_lease.fencing_token,
            "workflow_id": snapshot.workflow_id,
            "scope_kind": session_scope_kind,
            "subject_key": session_subject_key,
            "stage_key": role_session_stage_key(
                session_scope_kind,
                session_subject_key,
                role,
            ),
            "harness_id": harness_spec.harness_id,
            "harness_generation": effective_harness_generation,
            "continuation_input_path": str(continuation_input_path or ""),
            "continuation_output_path": str(continuation_output_path),
        }
        # Debug logging is runtime policy rather than durable role truth.
        # Snapshot it when the concrete role process is materialized.
        metadata["prompt_log_enabled"] = bool(self.prompt_log_enabled)
        if self.prompt_log_enabled:
            log_dir = str(workspace_value.get("log_dir") or "").strip()
            if log_dir:
                metadata["debug_log"] = {
                    "enabled": True,
                    "path": str(Path(log_dir) / "bunshin-debug.log"),
                }
        else:
            metadata.pop("debug_log", None)
        pack = BunshinInvocationPack.from_dict({**pack_value, "metadata": metadata})
        if harness_spec.launch_kind == HARNESS_LAUNCH_PAL_SANDBOX:
            pack = _bind_role_attempt_sandbox(
                self.service.runtime_root,
                pack,
                run_id=run_id,
                durable_prompt_reused=durable_prompt_reused,
            )
        else:
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
            role_profile_id=profile,
            harness_id=harness_spec.harness_id,
            harness_generation=effective_harness_generation,
            family_binding_sha=str(binding_ref.get("sha256") or ""),
            authoring_contract_version=AUTHORING_CONTRACT_VERSION,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        invocation_dir = invocation_root(self.service.runtime_root) / invocation_id
        invocation_dir.mkdir(parents=True, exist_ok=True)
        # The process pack belongs to the assignment lease, not the caller's
        # stale logical-effect fence.  Keep this path identical to the
        # attempt-local workspace paths above.
        attempt_dir = (
            invocation_dir
            / "attempts"
            / f"fence-{assignment_lease.fencing_token}"
        )
        attempt_dir.mkdir(parents=True, exist_ok=True)
        pack_path = attempt_dir / "pack.json"
        pack_path.write_text(json.dumps(pack.to_dict(), ensure_ascii=False, sort_keys=True), encoding="utf-8")
        argv = [
            *harness_spec.worker_argv,
            "--runtime-root",
            str(self.service.runtime_root),
            "--pack-json",
            str(pack_path),
            "--bunshin-id",
            invocation_id,
            "--run-id",
            run_id,
        ]
        runner_env = python_subprocess_env()
        runner_env[ROLE_GATEWAY_TOKEN_ENV] = assignment_access_token
        if self.runtime_db_path is not None:
            runner_env[BUNSHIN_RUNTIME_DB_PATH_ENV] = str(self.runtime_db_path)
        if harness_spec.launch_kind == HARNESS_LAUNCH_PAL_SANDBOX:
            argv, env = build_sandboxed_runner_invocation(
                runtime_root=self.service.runtime_root,
                pack=pack,
                argv=argv,
                env=runner_env,
            )
        else:
            env = runner_env

        def process_started(owner: WorkerProcessOwner) -> None:
            process = owner.process
            if process is None:
                raise RuntimeError("worker process owner started without a process")
            process_metadata = {
                "workflow_id": snapshot.workflow_id,
                "aggregate_type": snapshot.aggregate_type.value,
                "aggregate_id": snapshot.aggregate_id,
                "process_group_id": owner.process_group_id,
                "workspace_path": str(pack.workspace.get("repo_path") or ""),
                "run_id": run_id,
            }
            self.repository.update_role_attempt_process_group(
                assignment_id=str(assignment["assignment_id"]),
                attempt_id_value=str(attempt["attempt_id"]),
                fencing_token=assignment_lease.fencing_token,
                process_group_id=owner.process_group_id,
            )
            self.repository.update_lease_metadata(
                assignment_lease_resource,
                str(attempt["attempt_id"]),
                assignment_lease.fencing_token,
                {**process_metadata, "role": role},
            )
            self.repository.update_lease_metadata(
                lease_resource,
                invocation_id,
                fencing_token,
                process_metadata,
            )

        def register_process(owner: WorkerProcessOwner) -> None:
            process = owner.process
            if process is None:
                raise RuntimeError("worker process owner registered without a process")
            if invocation_id in self._process_owners or run_id in self._run_to_invocation:
                raise RuntimeError(
                    f"logical worker {invocation_id} already owns a process"
                )
            self._process_owners[invocation_id] = owner
            self._processes[invocation_id] = process
            self._run_to_invocation[run_id] = invocation_id
            if self.register_broker_run is not None:
                self.register_broker_run(run_id, invocation_id, pack, process)

        def unregister_process(owner: WorkerProcessOwner) -> None:
            if not owner.process_group_reaped:
                raise RuntimeError(
                    "worker process accounting cannot close before process-group reap"
                )
            owns_registration = self._process_owners.get(invocation_id) is owner
            if owns_registration and self.unregister_broker_run is not None:
                self.unregister_broker_run(run_id, True)
            if owns_registration:
                self._process_owners.pop(invocation_id, None)
                self._processes.pop(invocation_id, None)
            if (
                owns_registration
                and self._run_to_invocation.get(run_id) == invocation_id
            ):
                self._run_to_invocation.pop(run_id, None)
            # The assignment fence belongs to this concrete native-process
            # attempt.  Once the complete process group is reaped there can be
            # no legitimate late role-gateway call, so close the lease at the
            # same RAII boundary as the process permit.
            with contextlib.suppress(LeaseConflict, StaleFencingToken):
                self.repository.release_lease(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                )

        workspace_path = str(pack.workspace.get("repo_path") or "").strip()
        owner = WorkerProcessOwner(
            argv=tuple(argv),
            env=env,
            invocation_id=invocation_id,
            run_id=run_id,
            workspace=Path(workspace_path) if workspace_path else None,
            workspace_locks=self._worktree_locks,
            on_started=process_started,
            on_registered=register_process,
            on_unregistered=unregister_process,
            heartbeat_factories=(
                lambda: self._lease_heartbeat(
                    lease_resource,
                    invocation_id,
                    fencing_token,
                ),
                lambda: self._lease_heartbeat(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                ),
            ),
            reap_timeout_seconds=5.0,
        )
        events: list[dict[str, Any]] = []
        worker_error = ""
        shell = self._role_supervisor.process_shell(
            owner,
            run_id=run_id,
        )
        async with shell:
            process = owner.process
            if process is None or process.stdout is None:
                raise RuntimeError("worker process has no stdout pipe")
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
                    # A logical role session reuses its invocation id across
                    # attempts.  Keep the concrete attempt identity available
                    # to Manager delivery deduplication without changing the
                    # worker's public payload contract.
                    event["_attempt_id"] = str(attempt["attempt_id"])
                    events.append(event)
                    if self.publish_worker_event is not None:
                        await self.publish_worker_event(event)
                elif str(item.get("kind") or "") == "worker_error":
                    worker_error = str(item.get("error") or "")
            await process.wait()
        process = owner.process
        if process is None:
            raise RuntimeError("worker process owner lost its process")
        stderr = owner.stderr
        assignment_after_process = self.repository.read_role_assignment(
            str(assignment["assignment_id"])
        )
        has_submission_receipt = bool(
            dict((assignment_after_process or {}).get("submission_artifact_ref") or {})
        )
        if process.returncode != 0 and not has_submission_receipt:
            error_tail = _meaningful_stderr_tail(stderr.decode("utf-8", errors="replace"))
            terminal_error_kind, terminal_error, retry_directive = (
                _worker_terminal_failure(events)
            )
            details = (
                terminal_error
                or worker_error
                or error_tail
                or "worker emitted no structured error"
            )
            permanent = retry_directive == "do_not_retry"
            if not permanent:
                self.repository.queue_role_attempt_retry(
                    assignment_id=str(assignment["assignment_id"]),
                    attempt_id_value=str(attempt["attempt_id"]),
                    error_kind=terminal_error_kind or "worker_process_failed",
                    error_text=details,
                )
            with contextlib.suppress(Exception):
                self.repository.release_lease(
                    assignment_lease_resource,
                    str(attempt["attempt_id"]),
                    assignment_lease.fencing_token,
                )
            checkpoint = self._publish_agent_session_checkpoint(
                invocation_id,
                assignment_lease.fencing_token,
                continuation_output_path,
            )
            if checkpoint is not None and pal_checkpoint_capable:
                self.repository.suspend_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    status="interrupted",
                )
            else:
                self.repository.finish_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    status="failed",
                )
            if permanent:
                raise PermanentEffectError(details)
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
            checkpoint = self._publish_agent_session_checkpoint(
                invocation_id,
                assignment_lease.fencing_token,
                continuation_output_path,
            )
            if checkpoint is not None and pal_checkpoint_capable:
                self.repository.suspend_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
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
            checkpoint = self._publish_agent_session_checkpoint(
                invocation_id,
                assignment_lease.fencing_token,
                continuation_output_path,
            )
            if checkpoint is None:
                raise RuntimeError(
                    "worker reached a manager-restart safe point without a durable continuation"
                )
            self.repository.suspend_role_invocation(
                invocation_id=invocation_id,
                fencing_token=fencing_token,
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
            checkpoint = self._publish_agent_session_checkpoint(
                invocation_id,
                assignment_lease.fencing_token,
                continuation_output_path,
            )
            if checkpoint is not None and pal_checkpoint_capable:
                self.repository.suspend_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
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
            checkpoint = self._publish_agent_session_checkpoint(
                invocation_id,
                assignment_lease.fencing_token,
                continuation_output_path,
            )
            if checkpoint is not None and pal_checkpoint_capable:
                self.repository.suspend_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    status="interrupted",
                )
            else:
                self.repository.finish_role_invocation(
                    invocation_id=invocation_id,
                    fencing_token=fencing_token,
                    status="failed",
                )
            raise SubmissionInvariantError(
                "role participant reported completion before its durable submission receipt"
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
        checkpoint = self._publish_agent_session_checkpoint(
            invocation_id,
            assignment_lease.fencing_token,
            continuation_output_path,
        )
        terminal_payload["v2_timing"] = _worker_event_timing(events)
        if checkpoint is not None:
            terminal_payload["session_turn_index"] = int(
                dict(checkpoint.get("metrics") or {}).get(
                    "llm_round_count"
                )
                or 0
            )
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
            ),
        )
        if pal_checkpoint_capable:
            if checkpoint is None:
                raise RuntimeError("resumable role completed without a durable agent-session checkpoint")
            self.repository.suspend_role_invocation(
                invocation_id=invocation_id,
                fencing_token=fencing_token,
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

    def _durable_session_skill_injections(
        self,
        *,
        workflow_id: str,
        session_id: str,
    ) -> list[dict[str, str]] | None:
        for assignment in self.repository.list_role_assignments(
            workflow_id=workflow_id,
        ):
            if str(assignment.get("session_id") or "") != str(session_id):
                continue
            prompt_ref = self._durable_assignment_prompt_ref(assignment)
            if prompt_ref is None:
                continue
            prompt = BunshinInvocationPack.from_dict(
                dict(self.service.artifacts.read_json(prompt_ref))
            )
            return [
                {
                    "skill_id": str(item.get("skill_id") or ""),
                    "system_reminder": str(item.get("system_reminder") or ""),
                }
                for item in list(
                    dict(prompt.metadata or {}).get("initial_skill_injections") or []
                )
                if isinstance(item, Mapping)
                and str(item.get("skill_id") or "").strip()
                and str(item.get("system_reminder") or "").strip()
            ]
        return None

    def _role_session_skill_injections(
        self,
        *,
        request: Mapping[str, Any],
        workflow_id: str,
        session_id: str,
    ) -> list[dict[str, str]]:
        prior = self._durable_session_skill_injections(
            workflow_id=workflow_id,
            session_id=session_id,
        )
        if prior is not None:
            return prior
        return _workflow_skill_injections(request, self.inject_skill)

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
        assignment_id = str(assignment.get("assignment_id") or "")
        payload_hash = str(assignment.get("submission_payload_hash") or "")
        if not assignment_id or not payload_hash:
            raise SubmissionInvariantError(
                "role assignment receipt has no durable assignment identity"
            )
        return {
            "event_kind": "terminal",
            "phase": "completed",
            "payload": {
                **original_payload,
                "status": "completed",
                "summary": str(summary or "Worker submission recorded."),
                # The assignment receipt durably owns exactly one primary
                # submission. Role-local supporting projections must be
                # reconstructed from their durable source, never retained as
                # invocation-directory paths in an otherwise replayable
                # terminal.
                "artifacts": [primary],
                "primary_artifact": primary,
                "submission_receipt": artifact_ref,
                "role_assignment_id": assignment_id,
                "role_submission_payload_hash": payload_hash,
                # Only receipt-only reconciliation is a replay. A fresh role
                # process also settles through the durable receipt, but its
                # original terminal carries the billable worker turn.
                "durable_receipt_replay": original_terminal is None,
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
            raise AgentSessionCheckpointError(
                f"role session disappeared before process start: {session_id}"
            )
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

        store = LogicalCoroutineCheckpointStore(self.service.runtime_root)
        checkpoint = store.read(session_id)
        if checkpoint is None:
            if str(session.get("status") or "") in {"suspended", "interrupted"}:
                raise AgentSessionCheckpointError(
                    "suspended role session has no logical-coroutine checkpoint"
                )
            return None, checkpoint_path
        restored = normalize_agent_session_checkpoint(checkpoint)
        if str(restored.get("logical_coroutine_id") or "") != session_id:
            raise AgentSessionCheckpointError(
                "role session continuation has the wrong session identity"
            )
        if str(restored.get("workflow_id") or "") != str(session.get("workflow_id") or ""):
            raise AgentSessionCheckpointError(
                "role session continuation has the wrong workflow"
            )
        expected_stage = role_session_stage_key(
            str(session.get("scope_kind") or ""),
            str(session.get("subject_key") or ""),
            str(session.get("role") or ""),
        )
        if str(restored.get("stage_key") or "") != expected_stage:
            raise AgentSessionCheckpointError(
                "role session continuation has the wrong stage"
            )
        materialized = store.materialize_input(session_id, restore_path)
        return materialized, checkpoint_path

    def _publish_agent_session_checkpoint(
        self,
        invocation_id: str,
        fencing_token: int,
        checkpoint_path: Path,
    ) -> dict[str, Any] | None:
        if not checkpoint_path.is_file():
            return None
        try:
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("worker checkpoint output is unreadable") from exc
        if not isinstance(payload, dict) or str(payload.get("logical_coroutine_id") or "") != invocation_id:
            raise RuntimeError("worker checkpoint output has the wrong session identity")
        try:
            payload = normalize_agent_session_checkpoint(payload)
        except AgentSessionCheckpointError as exc:
            raise RuntimeError("worker checkpoint output has an invalid envelope") from exc
        if int(payload.get("producer_fencing_token") or 0) != int(fencing_token):
            raise RuntimeError("worker checkpoint output has a stale fencing token")
        session = self.repository.read_role_session(invocation_id)
        if session is None:
            raise RuntimeError("worker checkpoint output has no durable session")
        if str(payload.get("workflow_id") or "") != str(session.get("workflow_id") or ""):
            raise RuntimeError("worker checkpoint output has the wrong workflow")
        expected_stage = role_session_stage_key(
            str(session.get("scope_kind") or ""),
            str(session.get("subject_key") or ""),
            str(session.get("role") or ""),
        )
        if str(payload.get("stage_key") or "") != expected_stage:
            raise RuntimeError("worker checkpoint output has the wrong stage")
        LogicalCoroutineCheckpointStore(self.service.runtime_root).publish(
            payload,
            expected_logical_coroutine_id=invocation_id,
            current_fencing_token=fencing_token,
        )
        with contextlib.suppress(FileNotFoundError):
            checkpoint_path.unlink()
        return payload

    def _profile_for_role(self, workflow_id: str, role: str) -> str:
        role = OrchestrationRole(str(role)).value
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            raise ValueError(f"workflow not found while resolving role {role}: {workflow_id}")
        binding_ref = dict(workflow.payload.get("family_binding_ref") or {})
        if not binding_ref:
            raise ValueError(f"workflow has no FamilyBindingArtifact: {workflow_id}")
        binding = dict(self.service.artifacts.read_json(binding_ref))
        role_binding = dict(validate_family_binding_payload(binding)[role])
        profile = str(
            dict(role_binding.get("role_profile") or {}).get("canonical_profile_id")
            or dict(role_binding.get("role_profile") or {}).get("bunshin_profile")
            or ""
        ).strip()
        if not profile:
            raise ValueError(f"family {binding.get('family_id')} does not bind role {role}")
        return profile

    def _role_binding(
        self,
        workflow_id: str,
        role: str,
    ) -> dict[str, Any]:
        role = OrchestrationRole(str(role)).value
        workflow = self.repository.read_snapshot(
            AggregateType.WORKFLOW,
            workflow_id,
        )
        if workflow is None:
            raise ValueError(
                f"workflow not found while resolving role {role}: "
                f"{workflow_id}"
            )
        binding_ref = dict(
            workflow.payload.get("family_binding_ref") or {}
        )
        if not binding_ref:
            raise ValueError(
                f"workflow has no FamilyBindingArtifact: {workflow_id}"
            )
        binding = dict(self.service.artifacts.read_json(binding_ref))
        return dict(validate_family_binding_payload(binding)[role])

    def _role_participant_kind(self, workflow_id: str, role: str) -> str:
        return str(self._role_binding(workflow_id, role)["participant"])

    def _uses_git_skeleton(self, workflow_id: str) -> bool:
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            return False
        binding_ref = dict(getattr(workflow, "payload", {}).get("family_binding_ref") or {})
        if not binding_ref:
            return False
        binding = dict(self.service.artifacts.read_json(binding_ref))
        validate_family_binding_payload(binding)
        return (
            family_execution_adapter(binding.get("execution_adapter"))
            == SOFTWARE_GIT_ADAPTER
        )

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
        owner = self._process_owners.get(invocation_id or "")
        if owner is None:
            return False
        return await owner.write_control(
            (json.dumps(dict(message), ensure_ascii=False) + "\n").encode("utf-8")
        )

    async def _close_owned_process(
        self,
        invocation_id: str,
        *,
        process_group: int,
        worker_label: str,
    ) -> None:
        owner = self._process_owners.get(str(invocation_id))
        if owner is not None:
            await owner.close()
            return
        if process_group and not await terminate_process_group(
            process_group,
            timeout_seconds=5.0,
        ):
            raise RuntimeError(
                f"{worker_label} process group could not be reaped"
            )

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
            # Ownership ends when the RAII owner unregisters, never when only
            # its leader process happens to acquire a return code.
            if owner_id in self._process_owners:
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

    def _dispatch_architecture_review_result(
        self,
        revision: AggregateSnapshot,
        review: SkeletonReviewResult,
        review_ref: ArtifactRef,
        *,
        effect: Mapping[str, Any] | None = None,
        assignment_id: str = "",
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
        review_generation = int(
            current.payload.get("architecture_review_generation") or 0
        )
        with self.repository.transaction() as connection:
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=current.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=current.aggregate_id,
                    actor="bunshin-v2-architecture-reviewer",
                    expected_version=current.version,
                    idempotency_key=(
                        f"architecture-review:{current.aggregate_id}:"
                        f"generation-{review_generation}:{review_ref.sha256}"
                    ),
                    payload=payload,
                ),
                **self._role_submission_settlement(
                    effect or {},
                    assignment_id=assignment_id,
                ),
                _connection=connection,
            )
            WorkflowCoordinator(self.repository).submit_plan_verdict(
                workflow_id=current.workflow_id,
                accepted=review.verdict == "PASS",
                finding_refs=(
                    ()
                    if review.verdict == "PASS"
                    else (review_ref.sha256,)
                ),
                _connection=connection,
            )

    def _effect_snapshot(self, effect: Mapping[str, Any]) -> AggregateSnapshot:
        aggregate_type = AggregateType(str(effect["aggregate_type"]))
        snapshot = self.repository.read_snapshot(aggregate_type, str(effect["aggregate_id"]))
        if snapshot is None:
            raise ValueError("semantic effect aggregate no longer exists")
        return snapshot

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

    def _publish_verification_evidence(
        self,
        *,
        review_scratch: Path,
        candidate_identity: str,
    ) -> ArtifactRef:
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
        return self.service.artifacts.put_json(
            {
                "schema_version": "2",
                "candidate_identity": candidate_identity,
                "changed_paths": _verification_scratch_paths(review_scratch),
                "files": files,
            },
            artifact_type="VerificationWorkspaceEvidenceArtifact",
        )


def _architect_authoring_locations(
    path: Path,
    contract: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Capture exact authoring lines before the Manager removes its YAML projection."""

    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError:
        lines = []
    result: dict[str, dict[str, Any]] = {}
    for section in ("requirements", "modules", "scenarios"):
        names = [str(name) for name in dict(contract.get(section) or {})]
        section_line = _yaml_top_level_line(lines, section)
        for name in names:
            symbol = f"{section}.{name}"
            result[symbol] = {
                "scope": "workspace",
                "file": ARCHITECT_AUTHORING_RELATIVE_PATH,
                "line": _yaml_mapping_member_line(
                    lines,
                    section=section,
                    member=name,
                    fallback=section_line,
                ),
                "symbol": symbol,
            }
    return result


def _yaml_top_level_line(lines: list[str], key: str) -> int:
    for index, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        if raw == raw.lstrip() and stripped.split("#", 1)[0].rstrip() == f"{key}:":
            return index
    return 1


def _yaml_mapping_member_line(
    lines: list[str],
    *,
    section: str,
    member: str,
    fallback: int,
) -> int:
    in_section = False
    for index, raw in enumerate(lines, start=1):
        content = raw.split("#", 1)[0].rstrip()
        if not content.strip():
            continue
        indentation = len(content) - len(content.lstrip(" "))
        stripped = content.strip()
        if indentation == 0:
            in_section = stripped == f"{section}:"
            continue
        if not in_section or indentation != 2 or ":" not in stripped:
            continue
        candidate = stripped.split(":", 1)[0].strip().strip("\"'")
        if candidate == member:
            return index
    return max(1, int(fallback or 1))


def _stable_architecture_preflight_finding(
    exc: ValueError,
    *,
    contract_intent: Mapping[str, Any],
    submission: Mapping[str, Any],
) -> dict[str, Any]:
    """Compile every Manager finding into the same routable location contract."""

    error = str(exc)
    requirements = {
        str(name): dict(value or {})
        for name, value in dict(submission.get("requirements") or {}).items()
    }
    modules = {
        str(name): dict(value or {})
        for name, value in dict(submission.get("modules") or {}).items()
    }
    scenarios = {
        str(name): dict(value or {})
        for name, value in dict(submission.get("scenarios") or {}).items()
    }
    mentioned: list[str] = []
    affected_modules: set[str] = set()
    for section, values in (
        ("requirements", requirements),
        ("modules", modules),
        ("scenarios", scenarios),
    ):
        for name in values:
            if not re.search(
                rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])",
                error,
            ):
                continue
            mentioned.append(f"{section}.{name}")
            if section == "modules":
                affected_modules.add(name)
            elif section == "requirements":
                owner = str(requirements[name].get("owner") or "")
                if owner in modules:
                    affected_modules.add(owner)
                elif owner in scenarios:
                    affected_modules.update(
                        str(item)
                        for item in list(scenarios[owner].get("modules") or [])
                        if str(item) in modules
                    )
            else:
                affected_modules.update(
                    str(item)
                    for item in list(scenarios[name].get("modules") or [])
                    if str(item) in modules
                )
    symbols = list(dict.fromkeys(mentioned)) or ["architecture"]
    known_locations = {
        str(symbol): dict(location or {})
        for symbol, location in dict(
            contract_intent.get("authoring_locations") or {}
        ).items()
    }
    locations = [
        known_locations.get(
            symbol,
            {
                "scope": "workspace",
                "file": ARCHITECT_AUTHORING_RELATIVE_PATH,
                "line": 1,
                "symbol": symbol,
            },
        )
        for symbol in symbols
    ]
    payload: dict[str, Any] = {
        "finding_kind": "contract_defect",
        "summary": error,
        "source": "stable_architecture_preflight",
        "locations": locations,
        "repair_instruction": (
            "Correct only the rejected semantic DAG, reference, contract skeleton, or path declaration; "
            "preserve unrelated accepted architecture content."
        ),
    }
    if affected_modules:
        payload["affected_modules"] = sorted(affected_modules)
    if isinstance(exc, SemanticReferenceError):
        payload["semantic_reference_error"] = exc.to_dict()
    return payload


def _contract_architect_instruction(
    *,
    finding: Mapping[str, Any],
    has_base_manifest: bool,
    has_revision_scope: bool,
) -> str:
    instruction = (
        "Author the current Architecture Skeleton in the bound worktree. First read the ordered read-only task ledger, perform the required "
        "consistency pass, and inspect only the repository context needed to understand existing public boundaries. Then settle the smallest "
        "complete module-level design: define responsibilities and boundaries, directional public contracts and dependency handoffs, ownership, "
        "lifecycle, invariants, observable errors, optional state machines, and meaningful end-to-end scenario composition. Use update_checklist "
        "as the fixed work cursor: complete the requirements/design phase, then write the matching public declarations without product behavior, "
        "and only then fill the Manager-preseeded architect.yaml. Immediately begin file-edit tool calls once the design is settled; do not spend "
        "another response restating, rehearsing, simulating, "
        "or drafting the settled design in prose. Encode that same design according to the commented structure, reconcile both projections, and complete the checklist. This work is "
        "strictly module-level declaration: never implement "
        "product behavior, private algorithms, test bodies, or build machinery. Use the task-selected language. Ask the user only for an unresolved "
        "requirement or scope-changing decision. Call contract_submit only after the complete YAML and declaration skeleton agree and all checklist phases are completed."
    )
    if has_base_manifest:
        instruction += (
            " This is a revision based on the existing skeleton. Start from revision_finding or the explicit edit instruction and preserve "
            "unrelated declarations, contracts, path scopes, and dependencies unless consistency requires a wider correction. The semantic DAG Draft is already seeded from "
            "the accepted baseline in architect.yaml: do not remove, recreate, or restate unchanged modules. A source-only contract "
            "repair may submit the unchanged semantic DAG after editing the relevant declaration files."
        )
    if finding:
        summary = str(finding.get("summary") or "the bound architecture finding").strip()
        repair = str(finding.get("repair_instruction") or "").strip()
        instruction += (
            " A previous architecture submission was rejected and is not accepted. Read revision_finding before any other work, "
            f"correct this exact defect: {summary}"
            + (f" Repair boundary: {repair}" if repair else "")
            + " Do not report the earlier submit as completion. Call contract_submit again after the correction."
        )
    elif has_base_manifest:
        instruction += (
            " Start from the affected architect.yaml entries and declaration files, widen only when consistency requires it, then call contract_submit with no arguments."
        )
    if has_revision_scope:
        instruction += (
            " Read the bound revision_scope as repair guidance, not as a write fence. Append every bound finding_key to update_checklist as "
            "`resolve finding: <finding_key>` and mark it completed only when you claim the repair is closed. If one physical reference or contract defect affects multiple named modules, "
            "repair every affected module in the same candidate; do not wait for stable preflight to report the same defect one module at a time. "
            "Paths listed as immutable_requirement_paths identify evidence in task.yaml, never writable workspace targets. "
            "If a requirements defect remains unresolved after applying ordered task.yaml revisions over original, call ask_question and wait. The Manager records the exact question and answer in task.yaml before resuming you; continue directly without editing or submitting task data. "
            "The Manager checks checklist and structural closure; the Reviewer decides whether the repair is semantically correct."
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
    modules = dict(submission.get("modules") or {})
    review_view["manager_derived_verification_policy"] = {
        "architect_declares_test_scopes": False,
        "tests_are_product_scenarios": False,
        "developer_corpora": {
            str(name): {
                "kind": "directory",
                "path": module_developer_test_path(str(name)),
                "owner": "coder",
                "verifier_access": "read_only",
            }
            for name, raw_module in modules.items()
            if str(dict(raw_module or {}).get("module_kind") or "")
            == "implementation"
        },
        "verification_corpora": {
            str(name): {
                "kind": "directory",
                "path": module_verification_corpus_path(str(name)),
                "owner": "verifier",
                "coder_access": "read_only",
            }
            for name, raw_module in modules.items()
            if str(dict(raw_module or {}).get("module_kind") or "")
            == "implementation"
        },
    }
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
    """Resolve the Manager-bound workspace actually used by the verifier.

    Software verifiers work directly in the canonical module
    worktree shared with the corresponding producer.  Other adapters may still
    receive an attempt-local role workspace.  The immutable prompt pack records
    which ownership model was selected.
    """

    prompt_pack = artifacts.read_json(prompt_ref)
    workspace = dict(prompt_pack.get("workspace") or {})
    binding = str(workspace.get("workspace_binding") or "").strip().lower()
    if binding != "canonical" and not bool(workspace.get("v2_role_workspace")):
        raise SubmissionInvariantError(
            "verifier prompt pack is not bound to a canonical or isolated role workspace"
        )
    review_workspace = Path(str(workspace.get("repo_path") or ""))
    review_scratch = Path(str(workspace.get("review_scratch_dir") or ""))
    if not review_workspace.is_dir():
        raise SubmissionInvariantError(
            "verifier prompt pack references an unavailable bound workspace"
        )
    if not review_scratch.is_dir():
        raise SubmissionInvariantError(
            "verifier prompt pack references an unavailable review scratch directory"
        )
    return review_workspace, review_scratch


def _semantic_verifier_instruction(*, graph_sink: bool) -> str:
    scratch_rule = (
        "Put every transient configure, build, and test output under the exact "
        "workspace.build_scratch_dir from your invocation pack (for example, pass that path to "
        "CMake with `-B`). Never create build output in the repository worktree; only durable "
        "verifier cases may be written under the bound verification corpus. "
    )
    if graph_sink:
        return (
            scratch_rule
            + "This is the terminal delivery module in the contract graph. Assume accepted dependencies satisfy their public "
            "contracts. Use reference:module_work_view for this module and the separate "
            "reference:system_delivery_view for whole-system requirements and scenarios; then test the real consumer "
            "entrypoint end to end in this node's assembled worktree. The Manager-seeded `verify system scenario: ...` "
            "checklist items are exhaustive: complete each one only after executable evidence covers that scenario's "
            "declared entrypoint, observable behavior, and material failure behavior. Replay the "
            "bound corpora and findings, cover success and material failure paths, and inspect the current diff for new "
            "defects. Submit one outcome bound to this Candidate; no earlier verdict settles it."
        )
    return (
        scratch_rule
        + "This is the next Candidate assignment in your existing module verification session. First replay the bound "
        "developer and verification corpora plus every bound current or historical RepairBill reproducer. Then inspect the "
        "current Candidate diff and semantic neighborhood and run a diff-risk check for newly introduced defects. Submit one "
        "outcome bound to this Candidate; no earlier verdict settles it."
    )


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


def _verification_corpus_files(
    review_workspace: Path,
    corpus_scope: Mapping[str, Any],
) -> list[str]:
    root = review_workspace.resolve()
    target = str(corpus_scope.get("path") or "").replace("\\", "/").strip("/")
    if not target or not root.is_dir():
        return []
    path = (root / target).resolve()
    if not path.is_relative_to(root):
        return []
    if str(corpus_scope.get("kind") or "") == "file":
        return [target] if path.is_file() and not path.is_symlink() else []
    if not path.is_dir():
        return []
    return [
        item.relative_to(root).as_posix()
        for item in sorted(path.rglob("*"))
        if item.is_file() and not item.is_symlink()
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


def _ensure_workspace_directory(workspace: Path, relative_path: str) -> Path:
    root = Path(workspace).resolve()
    if not root.is_dir():
        raise SubmissionInvariantError(
            f"role worktree is unavailable: {workspace}"
        )
    normalized = str(relative_path or "").replace("\\", "/").strip("/")
    if not normalized:
        raise SubmissionInvariantError("role corpus path is empty")
    target = (root / normalized).resolve()
    if not target.is_relative_to(root):
        raise SubmissionInvariantError(
            f"role corpus path escapes its worktree: {relative_path}"
        )
    if target.exists() and not target.is_dir():
        raise SubmissionInvariantError(
            f"role corpus path is not a directory: {relative_path}"
        )
    target.mkdir(parents=True, exist_ok=True)
    return target



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


def _worker_terminal_failure(
    events: list[Mapping[str, Any]],
) -> tuple[str, str, str]:
    """Return the structured failure emitted by the worker, when present."""

    terminal = next(
        (
            item
            for item in reversed(events)
            if str(item.get("event_kind") or "") == "terminal"
        ),
        None,
    )
    if terminal is None:
        return "", "", ""
    payload = dict(terminal.get("payload") or {})
    if str(payload.get("status") or "") != "failed":
        return "", "", ""
    error_kind = str(
        payload.get("error_kind")
        or payload.get("error_type")
        or "worker_terminal_failed"
    ).strip()
    details = str(payload.get("error") or payload.get("summary") or "").strip()
    retry_directive = str(payload.get("retry_directive") or "").strip()
    return error_kind, details, retry_directive


def _worker_event_timing(events: list[Mapping[str, Any]]) -> dict[str, int | float]:
    llm_started: dict[str, datetime] = {}
    tool_started: dict[str, datetime] = {}
    llm_seconds = 0.0
    tool_seconds = 0.0
    input_tokens = 0
    output_tokens = 0
    cost = 0.0
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
            input_tokens += max(0, int(payload.get("input_tokens") or 0))
            output_tokens += max(0, int(payload.get("output_tokens") or 0))
            cost += max(0.0, float(payload.get("cost") or 0.0))
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
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cost": cost,
    }


def _recorded_role_metrics(terminal: Mapping[str, Any]) -> dict[str, int | float]:
    timing = dict(dict(terminal.get("payload") or {}).get("v2_timing") or {})
    return {
        "input_tokens": max(0, int(timing.get("input_tokens") or 0)),
        "output_tokens": max(0, int(timing.get("output_tokens") or 0)),
        "cost": max(0.0, float(timing.get("cost") or 0.0)),
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


def apply_v2_research_capability_policy(pack: BunshinInvocationPack, *, research_mode: str) -> BunshinInvocationPack:
    mode = ResearchMode(str(research_mode or ResearchMode.LOCAL_ONLY))
    if mode == ResearchMode.EXTERNAL_ALLOWED:
        return pack
    denied = {"op_web_search", "op_web_read"}
    return BunshinInvocationPack.from_dict(
        {
            **pack.to_dict(),
            "allowed_capabilities": [
                capability for capability in pack.allowed_capabilities if capability not in denied
            ],
        }
    )


def _role_primary_artifact_name(pack: BunshinInvocationPack) -> str:
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
    pack: BunshinInvocationPack,
    *,
    activation: RoleActivation,
) -> BunshinInvocationPack:
    current = set(pack.allowed_capabilities)
    current.add("op_bunshin_update_checklist")
    if activation.role == OrchestrationRole.ARCHITECT:
        allowed_authoring = {
            "op_bunshin_update_checklist",
            "op_bunshin_contract_submit",
            "op_bunshin_ask_question",
        }
    elif activation == RoleActivation(OrchestrationRole.REVIEWER, RoleMode.ARCHITECTURE):
        allowed_authoring = {
            "op_bunshin_update_checklist",
            "op_bunshin_add_finding",
            "op_bunshin_review_submit",
        }
    else:
        allowed_authoring = {
            # V2 verification has one Manager-owned submission protocol.  A
            # verifier profile may omit the built-in capability group, but
            # binding it to this role must still expose the semantic outcome
            # tools that _run_verification accepts.  Falling back to the
            # legacy VerificationPlan builder gives the worker a submit tool
            # whose durable receipt the Manager can never consume.
            OrchestrationRole.VERIFIER: {
                *SWE_VERIFICATION_CAPABILITIES,
                *VERIFICATION_EVIDENCE_CAPABILITIES,
            },
            OrchestrationRole.REVIEWER: {
                "op_bunshin_update_checklist",
                "op_bunshin_add_finding",
                "op_bunshin_review_submit",
            },
            OrchestrationRole.IMPLEMENTATION: set(CANDIDATE_BUILDER_CAPABILITIES),
        }.get(activation.role)
    if allowed_authoring is None:
        return pack
    allowed_authoring.add("op_bunshin_update_checklist")
    current.update(allowed_authoring)
    forbidden_writes = {
        "op_bunshin_artifact_write",
        "op_bunshin_artifact_edit",
    }
    if activation.role == OrchestrationRole.REVIEWER:
        forbidden_writes.update({"op_file_write", "op_file_edit"})
    if activation.role == OrchestrationRole.ARCHITECT:
        forbidden_writes.difference_update({"op_file_write", "op_file_edit"})
    if (
        activation.role == OrchestrationRole.IMPLEMENTATION
        and str(pack.profile_group or "") != "software_engineering"
    ):
        forbidden_writes.difference_update(
            {"op_bunshin_artifact_write", "op_bunshin_artifact_edit"}
        )
    capabilities = [
        capability
        for capability in sorted(current)
        if capability not in forbidden_writes
        and (not _is_authoring_capability_name(capability) or capability in allowed_authoring)
    ]
    if activation.role == OrchestrationRole.ARCHITECT:
        primary_artifact = "architect.yaml"
        allowed_output_types = ["ContractArtifact"]
    elif activation == RoleActivation(
        OrchestrationRole.REVIEWER,
        RoleMode.ARCHITECTURE,
    ):
        primary_artifact = "contract_review.json"
        allowed_output_types = ["ContractReviewArtifact"]
    elif activation.role == OrchestrationRole.REVIEWER:
        primary_artifact = "contract_review.json"
        allowed_output_types = ["ContractReviewArtifact"]
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
    return BunshinInvocationPack.from_dict(
        {
            **pack_value,
            "allowed_capabilities": capabilities,
            "workspace": workspace,
            "resolved_profile": resolved_profile,
        }
    )


def _is_authoring_capability_name(name: str) -> bool:
    value = str(name or "")
    return value == "op_bunshin_add_finding" or value.startswith(
        (
            "op_bunshin_update_checklist",
            "op_bunshin_requirement",
            "op_bunshin_requirements",
            "op_bunshin_contract",
            "op_bunshin_architecture",
            "op_bunshin_developer",
            "op_bunshin_candidate",
            "op_bunshin_verification",
            "op_bunshin_review_",
            "op_bunshin_standalone_review",
        )
    )


def apply_v2_revision_scope_capability_policy(pack: BunshinInvocationPack) -> BunshinInvocationPack:
    """Revision guidance reuses the normal Architect tool surface."""

    return pack


def _parse_architecture_review(payload: Mapping[str, Any]) -> SkeletonReviewResult:
    verdict = str(payload.get("verdict") or "").strip().upper()
    if verdict not in {"PASS", "FAIL"}:
        raise ValueError("architecture review verdict must be PASS or FAIL")
    structured_advisories(payload)
    findings = tuple(
        SkeletonReviewFinding(
            finding_key=str(
                item.get("finding_id")
                or item.get("finding_key")
                or ""
            ),
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
    structured_advisories(payload)
    raw_findings = structured_findings(payload)
    findings = tuple(
        SkeletonReviewFinding(
            finding_key=str(
                item.get("finding_id")
                or item.get("finding_key")
                or ""
            ),
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


def _bind_architecture_edit_instruction_for_review(
    references: dict[str, ArtifactRef],
    revision: AggregateSnapshot,
) -> bool:
    """Bind a human architecture-edit repair bill to every Reviewer attempt."""

    value = revision.payload.get("edit_instruction_ref")
    if not value:
        return False
    references["edit_instruction"] = _ref_from_mapping(value)
    return True


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
        repository = BunshinV2Repository(runtime_root)
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


def _resolve_dependency_node_id(
    repository: BunshinV2Repository,
    node: AggregateSnapshot,
    *,
    dependency_module: str,
) -> str:
    name = str(dependency_module or "").strip()
    if not name:
        raise ValueError("verification defect route requires a target module")
    current_name = str(
        node.payload.get("module_name") or node.payload.get("unit_id") or ""
    ).strip()
    # A verifier normally reports a defect in the module it is reviewing.  A
    # repair target may also be a direct dependency, but requiring every
    # finding to name a dependency makes a self-owned finding fail only after
    # the verifier has already completed and submitted it.
    if current_name == name:
        return node.aggregate_id
    matches: list[str] = []
    for dependency in _verification_related_module_nodes(repository, node):
        module_name = str(dependency.payload.get("module_name") or dependency.payload.get("unit_id") or "")
        if module_name == name:
            matches.append(dependency.aggregate_id)
    if len(matches) != 1:
        raise ValueError(f"dependency_module {name!r} does not name exactly one direct dependency")
    return matches[0]


def _verification_repair_path_owners(
    repository: BunshinV2Repository,
    node: AggregateSnapshot,
) -> dict[str, list[dict[str, str]]]:
    """Compile immutable module path ownership for Manager-routed repairs."""

    owners: dict[str, list[dict[str, str]]] = {}
    for dependency in _verification_related_module_nodes(
        repository,
        node,
        include_current=True,
    ):
        module_name = str(
            dependency.payload.get("module_name")
            or dependency.payload.get("unit_id")
            or ""
        ).strip()
        if not module_name:
            continue
        policy = dict(dependency.payload.get("path_policy") or {})
        scopes: list[dict[str, str]] = [
            {
                "kind": "file",
                "path": str(path).replace("\\", "/").strip("/"),
            }
            for path in list(policy.get("contract_paths") or [])
            if str(path).strip()
        ]
        scopes.extend(
            {
                "kind": str(dict(raw or {}).get("kind") or ""),
                "path": str(dict(raw or {}).get("path") or "")
                .replace("\\", "/")
                .strip("/"),
            }
            for raw in list(policy.get("implementation_scopes") or [])
        )
        for field in ("developer_tests",):
            raw_scope = dict(policy.get(field) or {})
            if raw_scope:
                scopes.append(
                    {
                        "kind": str(raw_scope.get("kind") or ""),
                        "path": str(raw_scope.get("path") or "")
                        .replace("\\", "/")
                        .strip("/"),
                    }
                )
        normalized = [
            scope
            for scope in scopes
            if scope["kind"] in {"file", "directory"} and scope["path"]
        ]
        if normalized:
            owners[module_name] = list(
                {
                    (scope["kind"], scope["path"]): scope
                    for scope in normalized
                }.values()
            )
    return dict(sorted(owners.items()))


def _verification_related_module_nodes(
    repository: BunshinV2Repository,
    node: AggregateSnapshot,
    *,
    include_current: bool = False,
) -> tuple[AggregateSnapshot, ...]:
    """Return the semantic module closure without exposing or rewriting DAG edges."""

    pending = [
        str(item)
        for item in (
            *list(node.payload.get("dependency_node_ids") or []),
            *list(node.payload.get("contract_dependency_node_ids") or []),
        )
        if str(item)
    ]
    if include_current:
        pending.insert(0, node.aggregate_id)
    visited: set[str] = set()
    modules: list[AggregateSnapshot] = []
    while pending:
        node_id = pending.pop(0)
        if node_id in visited:
            continue
        visited.add(node_id)
        dependency = (
            node
            if node_id == node.aggregate_id
            else repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
        )
        if dependency is None:
            continue
        if str(dependency.payload.get("node_kind") or "unit") == "unit":
            modules.append(dependency)
        pending.extend(
            str(item)
            for item in (
                *list(dependency.payload.get("dependency_node_ids") or []),
                *list(dependency.payload.get("contract_dependency_node_ids") or []),
            )
            if str(item) and str(item) not in visited
        )
    return tuple(modules)


def _manager_routed_findings(payload: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    """Return one deduplicated semantic finding stream from Manager artifacts.

    Replan batches contain both a flattened ``findings`` projection and grouped
    source packets.  Other repair artifacts expose only one of those shapes.
    The worker must see one required WorkItem per semantic finding regardless
    of the Manager-side storage layout.
    """

    candidates: list[Mapping[str, Any]] = [
        item
        for item in list(payload.get("findings") or [])
        if isinstance(item, Mapping)
    ]
    if not candidates:
        candidates.extend(
            item
            for group in list(payload.get("finding_groups") or [])
            if isinstance(group, Mapping)
            for item in list(group.get("findings") or [])
            if isinstance(item, Mapping)
        )
    if not candidates and any(
        key in payload
        for key in ("summary", "finding_kind", "severity", "priority")
    ):
        candidates.append(payload)

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in candidates:
        finding = dict(raw)
        identity = str(
            finding.get("finding_id")
            or finding.get("finding_key")
            or stable_hash(finding)[:16]
        )
        if identity in seen:
            continue
        seen.add(identity)
        result.append(finding)
    return tuple(result)


def _manager_required_system_scenario_work_items(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], ...]:
    """Compile authored delivery scenarios into required sink-verifier work.

    Scenario semantics remain Family-owned data. The Manager only preserves
    their stable semantic names as checklist obligations so a sink verifier
    cannot exercise one representative entrypoint and silently omit another
    authored system scenario.
    """

    scenarios = payload.get("scenarios")
    if not isinstance(scenarios, Mapping):
        return ()
    return tuple(
        {
            "kind": "task",
            "summary": f"verify system scenario: {name}",
            "status": "pending",
            "origin": "manager_system_scenario",
            "required": True,
        }
        for raw_name in scenarios
        for name in (str(raw_name).strip(),)
        if name
    )


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
        ("require_candidate_delta_review", "candidate_delta_review"),
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

    advisories = [
        dict(item or {}) for item in list(report.get("advisories") or [])
    ]
    if advisories:
        lines.extend(("", "## Optional Advisories"))
        for advisory in advisories:
            summary_text = str(advisory.get("summary") or "Advisory").strip()
            lines.append(f"- {summary_text}")

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


def _safe_component(value: str) -> str:
    return "".join(character if character.isalnum() or character in {"-", "_"} else "_" for character in value)


def _git_output(worktree: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(worktree), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr or completed.stdout or "Git command failed"
        )
    return completed.stdout.strip()


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
