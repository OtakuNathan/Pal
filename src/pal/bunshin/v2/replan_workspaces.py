from __future__ import annotations
from pal.bunshin.v2.workspace_git import _git as _git
from pal.bunshin.v2.workspace_git import _git_dir as _git_dir
from pal.bunshin.v2.workspace_git import _git_bytes as _git_bytes
from pal.bunshin.v2.workspace_git import _git_is_ancestor as _git_is_ancestor
from pal.bunshin.v2.workspace_git import git_changed_paths as git_changed_paths
from pal.bunshin.v2.workspace_git import _git_branch_exists as _git_branch_exists
from pal.bunshin.v2.workspace_git import _add_branch_worktree as _add_branch_worktree
from pal.bunshin.v2.workspace_git import _safe_ref as _safe_ref
from pal.bunshin.v2.workspace_resources import workspace_process_holders as workspace_process_holders
from pal.bunshin.v2.workspace_resources import format_workspace_process_holders as format_workspace_process_holders
import subprocess
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.v2.contract_runtime import ContractArtifactAccess
from pal.bunshin.v2.artifacts import ArtifactRef
from pal.bunshin.v2.contracts import AggregateSnapshot
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.sessions import coder_session_id, module_verifier_session_id, node_role_generation


def _snapshot_module_workspace_for_replan(
    *,
    contracts: ContractArtifactAccess,
    source_node: AggregateSnapshot,
    target_node: AggregateSnapshot,
) -> tuple[dict[str, Any], str]:
    """Commit in-flight Module assets without changing its linear history."""

    worktree = Path(str(source_node.payload.get("workspace_path") or ""))
    if not worktree.is_dir():
        raise ValueError("preserved Module has no stable worktree")
    current_head = _git(worktree, "rev-parse", "HEAD").strip()
    _git(worktree, "add", "-A")
    tree_sha = _git(worktree, "write-tree").strip()
    current_tree = _git(worktree, "rev-parse", f"{current_head}^{{tree}}").strip()
    if tree_sha == current_tree:
        preserved_digest = current_head
    else:
        preserved_digest = _git(
            worktree,
            "-c",
            "user.name=Pal Bunshin",
            "-c",
            "user.email=bunshin@localhost",
            "commit-tree",
            tree_sha,
            "-p",
            current_head,
            "-m",
            (
                f"bunshin module checkpoint {target_node.aggregate_id}\n\n"
                f"Pal-Assignment-Key: replan-{_safe_ref(target_node.aggregate_id)}"
            ),
        ).strip()
        _git(worktree, "reset", "--hard", preserved_digest)
    changed_paths = [
        item.decode("utf-8", errors="surrogateescape")
        for item in _git_bytes(
            worktree,
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            current_head,
            preserved_digest,
            "--",
        ).split(b"\0")
        if item
    ]
    ref = contracts.artifacts.put_json(
        {
            "schema_version": "3",
            "candidate_digest": preserved_digest,
            "base_sha": current_head,
            "architecture_base_sha": str(source_node.payload.get("epoch_base_sha") or ""),
            "previous_head_sha": current_head,
            "changed_paths": sorted(set(changed_paths)),
            "capture_kind": "module_replan_assets",
            "source_node_run_id": source_node.aggregate_id,
        },
        artifact_type="GitCheckpointArtifact",
    )
    return ref.to_dict(), preserved_digest


def _merge_architecture_into_preserved_module(
    *,
    source_node: AggregateSnapshot,
    target_node: AggregateSnapshot,
) -> str:
    worktree = Path(str(target_node.payload.get("workspace_path") or ""))
    target_skeleton = str(target_node.payload.get("epoch_base_sha") or "")
    if not worktree.is_dir() or not target_skeleton:
        raise RuntimeError("preserved Module has incomplete canonical worktree metadata")
    if _git(worktree, "status", "--porcelain").strip():
        raise RuntimeError("preserved Module worktree is dirty after Manager checkpoint")
    current_head = _git(worktree, "rev-parse", "HEAD").strip()
    if _git_is_ancestor(worktree, target_skeleton, current_head):
        return current_head
    merged = subprocess.run(
        [
            "git",
            "-C",
            str(worktree),
            "-c",
            "user.name=Pal Bunshin",
            "-c",
            "user.email=bunshin@localhost",
            "merge",
            "--no-ff",
            "--no-edit",
            target_skeleton,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if merged.returncode != 0:
        subprocess.run(
            ["git", "-C", str(worktree), "merge", "--abort"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        raise RuntimeError(
            "accepted Architecture cannot be merged into preserved Module "
            f"{source_node.payload.get('module_name') or source_node.payload.get('unit_id')}: "
            + (merged.stderr or merged.stdout or "unknown Git merge failure")
        )
    return _git(worktree, "rev-parse", "HEAD").strip()


def _checkpoint_preserved_module_head(
    *,
    contracts: ContractArtifactAccess,
    target_node: AggregateSnapshot,
    source_candidate_ref: Mapping[str, Any],
    source_candidate_digest: str,
    target_contract_ref: Mapping[str, Any],
    module_head: str,
) -> ArtifactRef:
    worktree = Path(str(target_node.payload.get("workspace_path") or ""))
    changed_paths = git_changed_paths(worktree, source_candidate_digest)
    payload = {
        "schema_version": "3",
        "node_run_id": target_node.aggregate_id,
        "candidate_digest": module_head,
        "base_sha": source_candidate_digest,
        "architecture_base_sha": str(target_node.payload.get("epoch_base_sha") or ""),
        "previous_head_sha": source_candidate_digest,
        "candidate_tree_sha": _git(
            worktree, "rev-parse", f"{module_head}^{{tree}}"
        ).strip(),
        "changed_paths": changed_paths,
        "unit_contract_hash": str(target_contract_ref.get("sha256") or ""),
        "carried_forward_from_candidate": str(
            source_candidate_ref.get("sha256") or ""
        ),
    }
    return contracts.artifacts.put_json(
        payload,
        artifact_type="GitCheckpointArtifact",
        child_refs=(
            (str(source_candidate_ref["sha256"]), "previous_checkpoint"),
            (str(target_contract_ref["sha256"]), "module_contract"),
        ),
    )


def _retire_removed_module_resources(
    *,
    repository: BunshinV2Repository,
    workflow_id: str,
    source_nodes: Mapping[str, AggregateSnapshot],
    target_module_names: set[str],
) -> tuple[str, ...]:
    """Retire only Module identities deleted by the accepted architecture.

    The old epoch has already drained before its replacement can compile.  A
    process holder here therefore means Manager accounting is wrong, so the
    cutover fails instead of inventing another ownership mechanism.
    """

    retired: list[str] = []
    for module_name in sorted(set(source_nodes) - set(target_module_names)):
        source_node = source_nodes[module_name]
        workspace = Path(str(source_node.payload.get("workspace_path") or ""))
        common_git_dir = Path(str(source_node.payload.get("common_git_dir") or ""))
        branch = str(source_node.payload.get("worktree_branch") or "")
        if workspace.is_dir():
            holders = workspace_process_holders(workspace)
            if holders:
                raise RuntimeError(
                    f"removed Module {module_name!r} still has live workspace holders:\n"
                    + format_workspace_process_holders(holders)
                )

        generation = node_role_generation(source_node.payload)
        for session_id in (
            coder_session_id(workflow_id, module_name, generation),
            module_verifier_session_id(workflow_id, module_name, generation),
        ):
            repository.role_sessions.complete_role_session(session_id, status="cancelled")

        if workspace.is_dir():
            if not common_git_dir.is_dir() or "/module/" not in branch:
                raise RuntimeError(
                    f"removed Module {module_name!r} has invalid stable worktree metadata"
                )
            completed = subprocess.run(
                [
                    "git",
                    f"--git-dir={common_git_dir}",
                    "worktree",
                    "remove",
                    "--force",
                    str(workspace),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                raise RuntimeError(
                    completed.stderr
                    or completed.stdout
                    or f"failed to retire Module worktree {workspace}"
                )
        if branch and _git_branch_exists(common_git_dir, branch):
            _git_dir(common_git_dir, "branch", "-D", branch)
        retired.append(module_name)
    return tuple(retired)


def _recreate_replaced_module_worktree(
    *,
    repository: BunshinV2Repository,
    workflow_id: str,
    source_node: AggregateSnapshot,
    target_node: AggregateSnapshot,
) -> None:
    workspace = Path(str(target_node.payload.get("workspace_path") or ""))
    common_git_dir = Path(str(target_node.payload.get("common_git_dir") or ""))
    branch = str(target_node.payload.get("worktree_branch") or "")
    target_base = str(
        target_node.payload.get("epoch_base_sha")
        or target_node.payload.get("base_sha")
        or ""
    )
    if not workspace or not common_git_dir.is_dir() or not branch or not target_base:
        raise RuntimeError("replaced Module has incomplete canonical worktree metadata")
    holders = workspace_process_holders(workspace) if workspace.is_dir() else ()
    if holders:
        raise RuntimeError(
            "replaced Module still has live workspace holders:\n"
            + format_workspace_process_holders(holders)
        )
    generation = node_role_generation(source_node.payload)
    module_name = str(
        source_node.payload.get("module_name")
        or source_node.payload.get("unit_id")
        or ""
    )
    for session_id in (
        coder_session_id(workflow_id, module_name, generation),
        module_verifier_session_id(workflow_id, module_name, generation),
    ):
        repository.role_sessions.complete_role_session(session_id, status="cancelled")
    if workspace.is_dir():
        removed = subprocess.run(
            [
                "git",
                f"--git-dir={common_git_dir}",
                "worktree",
                "remove",
                "--force",
                str(workspace),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if removed.returncode != 0:
            raise RuntimeError(
                removed.stderr
                or removed.stdout
                or f"failed to retire replaced Module worktree {workspace}"
            )
    if _git_branch_exists(common_git_dir, branch):
        _git_dir(common_git_dir, "branch", "-D", branch)
    workspace.parent.mkdir(parents=True, exist_ok=True)
    _add_branch_worktree(
        common_git_dir,
        worktree=workspace,
        branch=branch,
        start_sha=target_base,
    )


def _carry_forward_candidate(
    source_node: AggregateSnapshot,
    target_node: AggregateSnapshot,
    candidate_digest: str,
) -> None:
    source_git = Path(str(source_node.payload.get("common_git_dir") or ""))
    target_git = Path(str(target_node.payload.get("common_git_dir") or ""))
    target_worktree = Path(str(target_node.payload.get("workspace_path") or ""))
    if not source_git.is_dir() or not target_git.is_dir() or not target_worktree.is_dir():
        raise ValueError("Candidate carry-forward worktree metadata is incomplete")
    ref_name = f"refs/pal-bunshin-v2/carry-forward/{_safe_ref(target_node.aggregate_id)}"
    completed = subprocess.run(
        ["git", f"--git-dir={target_git}", "fetch", "--no-tags", str(source_git), f"{candidate_digest}:{ref_name}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "failed to import reusable candidate")
    _git(target_worktree, "reset", "--hard", candidate_digest)
