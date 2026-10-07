from __future__ import annotations
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.adapters import ARTIFACT_BUNDLE_ADAPTER
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.input_binding import BoundInputError
from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.sessions import architecture_cycle_id, coder_session_id, module_name_from_payload, module_verifier_session_id, node_role_generation
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping


from pal.bunshin.role_input_identity import (
    EPHEMERAL_ROLE_INPUT_NAMES as EPHEMERAL_ROLE_INPUT_NAMES,
    _role_input_is_semantic as _role_input_is_semantic,
    _semantic_role_input_refs as _semantic_role_input_refs,
)


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
    repair_value = node_payload.get("repair_bill_ref")
    if isinstance(repair_value, Mapping) and repair_value.get("sha256"):
        from pal.bunshin.verification import repair_bill_semantic_view

        repair_view = repair_bill_semantic_view(artifacts, repair_value)
        if repair_view.get("route") == "verification_correction":
            references["repair_bill"] = artifacts.put_json(
                repair_view,
                artifact_type="VerifierCorrectionViewArtifact",
                child_refs=((str(repair_value["sha256"]), "original_repair_packet"),),
            )
    return references


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


def _role_workspace_input_binding_roots(
    workspace: Mapping[str, Any],
    *,
    snapshot: AggregateSnapshot,
    workspace_source_root: str,
) -> tuple[Path | None, Path | None]:
    """Resolve where one role attempt's bound inputs bind.

    Returns ``(repo_root, workspace_root)`` with exactly one side set.  A
    ``repo_root`` marks a repository-including workspace whose contract
    already places every declared input at its repository-relative path, so
    binding only re-verifies those files in place.  A ``workspace_root``
    marks an artifact-style workspace that never includes the host
    repository, so the materializer writes the deterministic
    ``inputs/<name>/<repo_path>`` tree beneath it.

    The decision follows the existing workspace contract: an attempt whose
    execution adapter provisioned an artifact workspace never includes the
    repository, a Manager-prepared role workspace includes one exactly when
    it was cloned or copied from a declared source, and every other
    resolved workspace root is the bound repository itself (module
    worktree, architecture or review worktree, or the request repository).
    """

    resolved_root = str(
        workspace.get("repo_path") or workspace.get("workspace_path") or ""
    ).strip()
    artifact_style = (
        str(snapshot.payload.get("execution_adapter") or "") == ARTIFACT_BUNDLE_ADAPTER
        or not resolved_root
        or (bool(workspace.get("v2_role_workspace")) and not workspace_source_root)
    )
    if not artifact_style:
        return Path(resolved_root), None
    fallback_root = str(workspace.get("run_dir") or "").strip()
    if not (resolved_root or fallback_root):
        raise BoundInputError(
            "role workspace has no root for bound-input materialization"
        )
    return None, Path(resolved_root or fallback_root)


def _attach_bound_input_read_only_overlays(
    workspace: dict[str, Any],
    entries: list[dict[str, Any]],
) -> None:
    """Project verified bound inputs into the sandbox's real read-only overlays."""
    if not entries:
        return
    workspace_root = Path(
        str(workspace.get("repo_path") or workspace.get("cwd") or "")
    ).resolve()
    overlay_paths = [
        str(item).strip().replace("\\", "/")
        for item in list(workspace.get("read_only_overlay_paths") or [])
        if str(item).strip()
    ]
    for entry in entries:
        bound_path = Path(str(entry.get("path") or "")).resolve(strict=True)
        if not bound_path.is_relative_to(workspace_root):
            raise BoundInputError(
                f"bound input path {str(bound_path)!r} is outside role workspace "
                f"{str(workspace_root)!r}"
            )
        overlay_paths.append(bound_path.relative_to(workspace_root).as_posix())
    workspace["read_only_overlay_paths"] = list(dict.fromkeys(overlay_paths))
