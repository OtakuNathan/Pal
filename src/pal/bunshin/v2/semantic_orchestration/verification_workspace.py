from __future__ import annotations
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.contracts import SubmissionInvariantError
from pal.bunshin.v2.workspace_git import git_changed_paths
from pal.bunshin.v2.paths import standalone_review_root
from pal.bunshin.v2.semantic_orchestration.workspace_safety import _git_output
from pal.bunshin.v2.semantic_orchestration.review_results import _ref_from_mapping
from pal.bunshin.v2.semantic_orchestration.workspace_safety import _safe_component


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
