from __future__ import annotations
from pal.bunshin.workspace_git import _git as _git
from pal.bunshin.workspace_git import _git_dir as _git_dir
from pal.bunshin.workspace_git import _git_commit_exists as _git_commit_exists
from pal.bunshin.workspace_git import _force_branch as _force_branch
from pal.bunshin.workspace_git import _add_branch_worktree as _add_branch_worktree
from pal.bunshin.workspace_git import _clone_bundle_repository as _clone_bundle_repository
from pal.bunshin.workspace_git import _fetch_bundle_repository as _fetch_bundle_repository
from pal.bunshin.workspace_git import _safe_ref as _safe_ref
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.adapters import SOFTWARE_GIT_ADAPTER
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import AggregateSnapshot
from pal.bunshin.paths import ProjectGitLayout, project_git_layout_lock, resolve_project_git_layout, verification_scratch_root


def provision_module_worktrees(
    runtime_root: Path,
    *,
    workflow_id: str,
    workflow_name: str,
    unit_ids: list[str],
    workspace: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    layout = resolve_project_git_layout(
        runtime_root,
        workspace=workspace,
        workflow_id=workflow_id,
        workflow_name=workflow_name,
    )
    common_git_dir = layout.common_git_dir
    base_sha, base_tree_sha = _ensure_generic_project_repository(
        layout,
        workspace=workspace,
    )
    result: dict[str, dict[str, str]] = {}
    for unit_id in unit_ids:
        worktree = layout.module_worktree(unit_id)
        branch = layout.module_branch(unit_id)
        if not worktree.exists():
            worktree.parent.mkdir(parents=True, exist_ok=True)
            _add_branch_worktree(common_git_dir, worktree=worktree, branch=branch, start_sha=base_sha)
        result[unit_id] = {
            "workspace_path": str(worktree),
            "common_git_dir": str(common_git_dir),
            "worktree_branch": branch,
            "workflow_branch": layout.workflow_branch,
            "workflow_key": layout.workflow_key,
            "project_name": layout.project_name,
            "project_key": layout.project_key,
            "epoch_base_sha": base_sha,
            "epoch_base_tree_sha": base_tree_sha,
            "base_digest": base_sha,
            "base_sha": base_sha,
        }
    return result


def provision_skeleton_module_worktrees(
    runtime_root: Path,
    *,
    artifacts: ContentAddressedArtifactStore,
    workflow_id: str,
    workflow_name: str,
    unit_ids: list[str],
    workspace: Mapping[str, Any] | None = None,
    architecture_artifact: Mapping[str, Any],
) -> dict[str, dict[str, str]]:
    layout = resolve_project_git_layout(
        runtime_root,
        workspace=dict(workspace or {}),
        workflow_id=workflow_id,
        workflow_name=workflow_name,
        stored_layout=dict(architecture_artifact.get("repository_layout") or {}),
    )
    common_git_dir = layout.common_git_dir
    skeleton_sha = str(architecture_artifact.get("skeleton_commit_sha") or "")
    skeleton_tree = str(architecture_artifact.get("skeleton_tree_sha") or "")
    bundle_ref = ArtifactRef.from_mapping(dict(architecture_artifact.get("git_bundle_ref") or {}))
    if not skeleton_sha or not skeleton_tree or not bundle_ref.sha256:
        raise ValueError("software ContractArtifact is missing its commit, tree, or Git bundle")
    with project_git_layout_lock(layout):
        if not common_git_dir.exists():
            _clone_bundle_repository(
                common_git_dir,
                bundle_bytes=artifacts.read_bytes(bundle_ref),
            )
        elif not _git_commit_exists(common_git_dir, skeleton_sha):
            _fetch_bundle_repository(
                common_git_dir,
                bundle_bytes=artifacts.read_bytes(bundle_ref),
                namespace=f"refs/bunshin/imports/{bundle_ref.sha256[:16]}",
            )
        restored_tree = _git_dir(
            common_git_dir,
            "rev-parse",
            f"{skeleton_sha}^{{tree}}",
        ).strip()
        if restored_tree != skeleton_tree:
            raise RuntimeError("restored skeleton Git bundle does not match the accepted tree")
        _force_branch(common_git_dir, layout.workflow_branch, skeleton_sha)
    result: dict[str, dict[str, str]] = {}
    for unit_id in unit_ids:
        worktree = layout.module_worktree(unit_id)
        branch = layout.module_branch(unit_id)
        if not worktree.exists():
            worktree.parent.mkdir(parents=True, exist_ok=True)
            _add_branch_worktree(
                common_git_dir,
                worktree=worktree,
                branch=branch,
                start_sha=skeleton_sha,
            )
        result[unit_id] = {
            "workspace_path": str(worktree),
            "common_git_dir": str(common_git_dir),
            "worktree_branch": branch,
            "workflow_branch": layout.workflow_branch,
            "workflow_key": layout.workflow_key,
            "project_name": layout.project_name,
            "project_key": layout.project_key,
            "epoch_base_sha": skeleton_sha,
            "epoch_base_tree_sha": skeleton_tree,
            "base_digest": skeleton_sha,
            "base_sha": skeleton_sha,
            "execution_adapter": SOFTWARE_GIT_ADAPTER,
        }
    return result


def provision_module_verification_workspace(
    runtime_root: Path,
    *,
    node: AggregateSnapshot,
    candidate_digest: str,
) -> tuple[Path, Path]:
    if not candidate_digest:
        raise ValueError("module verification requires candidate_digest")
    worktree = Path(str(node.payload.get("workspace_path") or ""))
    if not worktree.is_dir():
        raise ValueError("module verification requires its Module worktree")
    scratch = (
        verification_scratch_root(runtime_root)
        / "modules"
        / _safe_ref(str(node.payload.get("module_name") or node.payload.get("unit_id") or "module"))
        / _safe_ref(candidate_digest)
    )
    scratch.mkdir(parents=True, exist_ok=True)
    if _git(worktree, "rev-parse", "HEAD").strip() != candidate_digest:
        raise RuntimeError("Module worktree is not bound to the Candidate SHA")
    return worktree, scratch


def _ensure_generic_project_repository(
    layout: ProjectGitLayout,
    *,
    workspace: Mapping[str, Any],
) -> tuple[str, str]:
    common_git_dir = layout.common_git_dir
    with project_git_layout_lock(layout):
        if not common_git_dir.exists():
            source = str(workspace.get("repo_path") or workspace.get("cwd") or "").strip()
            if source:
                completed = subprocess.run(
                    [
                        "git",
                        "clone",
                        "--bare",
                        "--no-hardlinks",
                        str(Path(source).expanduser()),
                        str(common_git_dir),
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    check=False,
                )
            else:
                with tempfile.TemporaryDirectory(
                    prefix="pal-generic-project-",
                    dir=layout.project_root,
                ) as temporary:
                    seed = Path(temporary) / "seed"
                    seed.mkdir()
                    _git(seed, "init", "-q", "-b", "main")
                    _git(
                        seed,
                        "-c",
                        "user.name=Pal Bunshin",
                        "-c",
                        "user.email=bunshin@localhost",
                        "commit",
                        "--allow-empty",
                        "-qm",
                        "V2 epoch base",
                    )
                    completed = subprocess.run(
                        ["git", "clone", "--bare", str(seed), str(common_git_dir)],
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        check=False,
                    )
            if completed.returncode != 0:
                raise RuntimeError(
                    completed.stderr
                    or completed.stdout
                    or "failed to initialize project repository"
                )
        base_sha = _git_dir(common_git_dir, "rev-parse", "HEAD").strip()
        base_tree_sha = _git_dir(common_git_dir, "rev-parse", "HEAD^{tree}").strip()
        _force_branch(common_git_dir, layout.workflow_branch, base_sha)
    return base_sha, base_tree_sha
