from __future__ import annotations

import subprocess
import json
import shutil
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.minion.execution_strategy import (
    PREPARE_GIT_WORKTREE,
    PREPARE_READ_ONLY_REPO,
    execution_strategy_from_pack,
    normalize_execution_strategy,
    prepare_kind,
)
from pal.minion.workspace_environment import prepare_workspace_environment
from pal.shared import TaskContextPack


GENERATED_COMMIT_EXCLUDES = (
    ":(exclude,glob)**/.minion/**",
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob)**/.mypy_cache/**",
    ":(exclude,glob)**/.ruff_cache/**",
    ":(exclude,glob)**/.cache/**",
    ":(exclude,glob)**/.coverage",
    ":(exclude,glob)**/htmlcov/**",
    ":(exclude,glob)**/coverage/**",
    ":(exclude,glob)**/dist/**",
    ":(exclude,glob)**/build/**",
    ":(exclude,glob)**/target/**",
    ":(exclude,glob)**/minion_outputs/**",
    ":(exclude,glob)**/*.py[cod]",
    ":(exclude,glob)**/*.o",
    ":(exclude,glob)**/*.obj",
    ":(exclude,glob)**/*.a",
    ":(exclude,glob)**/*.so",
    ":(exclude,glob)**/*.dylib",
    ":(exclude,glob)**/*.dll",
    ":(exclude,glob)**/*.exe",
    ":(exclude,glob)**/*.class",
)

LOCAL_GIT_EXCLUDES = (
    ".minion/",
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
    ".cache/",
    ".coverage",
    "htmlcov/",
    "coverage/",
    "dist/",
    "build/",
    "target/",
    "minion_outputs/",
    "*.py[cod]",
    "*.o",
    "*.obj",
    "*.a",
    "*.so",
    "*.dylib",
    "*.dll",
    "*.exe",
    "*.class",
)

GENERATED_ENVIRONMENT_UNION_MERGE_FILES = frozenset(
    {
        "compile_flags.txt",
    }
)


CHECKPOINT_COMMIT_CAPABILITY = "op_minion_checkpoint_commit"


@dataclass(frozen=True)
class GitCommandResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def prepare_git_task_environment(runtime_root: Path, pack: TaskContextPack) -> TaskContextPack:
    metadata = dict(pack.metadata)
    task_id = _safe_ref(str(metadata.get("task_id") or f"task_{pack.work_order_id}"))
    workspace = _normalize_workspace_paths(pack.workspace)
    source_repo = _source_repo(workspace)
    if not source_repo:
        source_repo = str(workspace.get("repo_path") or "").strip()
    project_name = _project_name(metadata, workspace, task_id=task_id, source_repo=source_repo)
    module_name = _module_name(metadata, workspace)
    work_order_root_id = str(metadata.get("parent_work_order_id") or workspace.get("parent_work_order_id") or pack.work_order_id).strip()
    project_root = _project_root_path(runtime_root, workspace, project_name, work_order_id=work_order_root_id)
    repo_path = _project_repo_path(runtime_root, workspace, project_name, work_order_id=work_order_root_id, module_name=module_name)
    common_git_dir = _project_common_git_dir(project_root)
    created_repo = not (repo_path / ".git").exists()
    branch = str(workspace.get("work_order_branch") or f"work_order_{_safe_ref(pack.work_order_id)}").strip()
    default_root_branch = (
        f"work_order_root_{_safe_ref(work_order_root_id)}"
        if str(work_order_root_id) == str(pack.work_order_id)
        else f"work_order_{_safe_ref(work_order_root_id)}"
    )
    root_branch = str(workspace.get("root_work_order_branch") or default_root_branch).strip()
    worktree_base_ref = ""
    workspace_kind = "git_worktree"

    if created_repo:
        _ensure_project_git_store(common_git_dir, source_repo=source_repo, workspace=workspace)
        initial_base_ref = _worktree_base_ref_from_git_dir(common_git_dir, workspace)
        _ensure_project_root_branch(project_root, common_git_dir, branch=root_branch, base_ref=initial_base_ref)
        workspace.setdefault("root_merge_target", initial_base_ref)
        worktree_base_ref = str(workspace.get("base_ref") or root_branch).strip() or root_branch
        _add_worktree_from_git_dir(common_git_dir, repo_path, branch=branch, base_ref=worktree_base_ref)
    _ensure_git_identity(repo_path)
    if not _has_head(repo_path):
        _git(repo_path, "commit", "--allow-empty", "-m", "minion: initialize project repo", check=True)
    _ensure_local_git_excludes(repo_path)
    environment_baseline_ref = ""
    workspace_environment_policy = _policy_from_pack(pack, "workspace_environment_policy")
    if workspace_environment_policy:
        workspace["workspace_environment_policy"] = dict(workspace_environment_policy)
    environment = prepare_workspace_environment(
        repo_path,
        pack,
        workspace,
        write_files=created_repo,
        runtime_root=runtime_root,
        policy=workspace_environment_policy,
    )
    if environment:
        workspace["languages"] = list(environment.get("languages") or [])
        workspace["lsp_setup"] = dict(environment.get("lsp_setup") or {})
        execution_env = dict(environment.get("execution_env") or {})
        if execution_env:
            workspace["execution_env"] = execution_env
        created_files = [str(item) for item in list(environment.get("created_files") or []) if str(item).strip()]
        if created_files:
            _git(repo_path, "add", "--", *created_files, check=True)
            if _has_staged_changes(repo_path):
                _git(repo_path, "commit", "-m", "minion: prepare workspace environment", check=True)
                workspace["lsp_setup"]["baseline_commit_sha"] = _current_head(repo_path)
                environment_baseline_ref = _current_branch(repo_path) or _current_head(repo_path)

    base_ref = str(workspace.get("base_ref") or environment_baseline_ref or worktree_base_ref or _current_branch(repo_path) or "HEAD").strip() or "HEAD"
    if not _git(repo_path, "rev-parse", "--verify", base_ref).ok:
        base_ref = "HEAD"
    base_sha = _git(repo_path, "rev-parse", base_ref, check=True).stdout.strip()
    merge_target = str(workspace.get("merge_target") or worktree_base_ref or base_ref).strip() or base_ref
    if _git(repo_path, "rev-parse", "--verify", branch).ok:
        _git(repo_path, "checkout", branch, check=True)
    else:
        _git(repo_path, "checkout", "-B", branch, base_ref, check=True)

    workspace.update(
        {
            "repo_path": str(repo_path),
            "project_name": project_name,
            "runtime_project_path": str(project_root),
            "work_order_repo_root": str(project_root),
            "common_git_dir": str(common_git_dir),
            "base_ref": base_ref,
            "base_sha": base_sha,
            "work_order_branch": branch,
            "root_work_order_branch": root_branch,
            "root_merge_target": str(workspace.get("root_merge_target") or worktree_base_ref),
            "merge_target": merge_target,
            "workspace_kind": workspace_kind,
        }
    )
    artifact_dir = _project_minion_dir(project_root) / "artifacts" / _safe_ref(pack.work_order_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    workspace.setdefault("run_dir", str(artifact_dir))
    workspace["artifact_dir"] = str(artifact_dir)
    workspace["workspace_policy"] = {"mode": "writable_git_branch"}
    workspace["completion_policy"] = {"evidence": "git_commit", "requires_capability_evidence": True}
    workspace["execution_strategy"] = normalize_execution_strategy(
        workspace.get("execution_strategy"),
        workspace_policy=workspace["workspace_policy"],
        completion_policy=workspace["completion_policy"],
        gate_policy=_policy_from_pack(pack, "gate_policy"),
        output_policy=_policy_from_pack(pack, "output_policy"),
    )
    if module_name:
        workspace["module_name"] = module_name
    if source_repo:
        workspace.setdefault("source_repo", source_repo)
    return _with_checkpoint_commit_capability(TaskContextPack.from_dict({**pack.to_dict(), "workspace": workspace}))


def prepare_task_workspace(runtime_root: Path, pack: TaskContextPack, *, run_id: str = "") -> TaskContextPack:
    workspace = _normalize_workspace_paths(pack.workspace)
    workspace_policy = _policy_from_pack(pack, "workspace_policy")
    completion_policy = _policy_from_pack(pack, "completion_policy")
    gate_policy = _policy_from_pack(pack, "gate_policy")
    output_policy = _policy_from_pack(pack, "output_policy")
    execution_strategy = execution_strategy_from_pack(
        pack,
        workspace_policy=workspace_policy,
        completion_policy=completion_policy,
        gate_policy=gate_policy,
        output_policy=output_policy,
    )
    kind = prepare_kind(execution_strategy)
    if kind == PREPARE_GIT_WORKTREE:
        prepared = prepare_git_task_environment(runtime_root, pack)
        prepared_workspace = dict(prepared.workspace)
        prepared_workspace["workspace_policy"] = {**workspace_policy, "mode": "writable_git_branch"}
        if completion_policy:
            prepared_workspace["completion_policy"] = dict(completion_policy)
        prepared_workspace["execution_strategy"] = dict(execution_strategy)
        return _with_checkpoint_commit_capability(TaskContextPack.from_dict({**prepared.to_dict(), "workspace": prepared_workspace}))
    if kind == PREPARE_READ_ONLY_REPO:
        source_repo = _source_repo(workspace)
        repo_path = str(workspace.get("repo_path") or "").strip()
        if not repo_path and source_repo and _is_local_path(source_repo):
            repo_path = source_repo
        if repo_path:
            workspace["repo_path"] = str(Path(repo_path).expanduser())
        if source_repo:
            workspace.setdefault("source_repo", source_repo)
        workspace["workspace_policy"] = {**workspace_policy, "mode": "read_only_repo"}
        if completion_policy:
            workspace["completion_policy"] = dict(completion_policy)
        workspace["execution_strategy"] = dict(execution_strategy)
        return _with_folder_workspace(runtime_root, pack, workspace, run_id=run_id)
    if workspace_policy:
        workspace["workspace_policy"] = dict(workspace_policy)
    if completion_policy:
        workspace["completion_policy"] = dict(completion_policy)
    workspace["execution_strategy"] = dict(execution_strategy)
    return _with_folder_workspace(runtime_root, pack, workspace, run_id=run_id)


def _with_checkpoint_commit_capability(pack: TaskContextPack) -> TaskContextPack:
    completion_policy = _policy_from_pack(pack, "completion_policy")
    if str(completion_policy.get("evidence") or "").strip().lower() != "git_commit":
        return pack
    metadata = pack.metadata if isinstance(pack.metadata, dict) else {}
    if bool(metadata.get("allow_text_only_completion") or completion_policy.get("allow_artifact_evidence")):
        return pack
    allowed = [str(item).strip() for item in list(pack.allowed_capabilities or []) if str(item).strip()]
    if CHECKPOINT_COMMIT_CAPABILITY not in allowed:
        allowed.append(CHECKPOINT_COMMIT_CAPABILITY)
    return TaskContextPack.from_dict({**pack.to_dict(), "allowed_capabilities": allowed})


def commit_milestone(repo_path: Path, *, work_order_id: str, milestone_index: int, title: str = "") -> dict[str, Any]:
    repo = Path(repo_path)
    if not (repo / ".git").exists():
        return {"status": "error", "error": "workspace is not a git repository", "repo_path": str(repo)}
    _ensure_git_identity(repo)
    status = _git(repo, "status", "--porcelain", check=True).stdout.strip()
    head = _current_head(repo)
    if not status:
        return {"status": "no_changes", "commit_sha": head, "repo_path": str(repo)}
    staged = _stage_milestone_changes(repo)
    if not staged.ok:
        return {
            "status": "error",
            "error": staged.stderr or staged.stdout or "git add failed",
            "repo_path": str(repo),
            "returncode": staged.returncode,
        }
    if not _has_staged_changes(repo):
        return {
            "status": "no_changes",
            "commit_sha": head,
            "repo_path": str(repo),
            "ignored_generated_changes": True,
        }
    message_title = str(title or f"milestone {milestone_index}").strip()
    message = f"minion({work_order_id}): complete milestone {milestone_index} - {message_title}"
    committed = _git(repo, "commit", "-m", message)
    if not committed.ok:
        return {
            "status": "error",
            "error": committed.stderr or committed.stdout or "git commit failed",
            "repo_path": str(repo),
            "returncode": committed.returncode,
        }
    return {"status": "committed", "commit_sha": _current_head(repo), "repo_path": str(repo), "message": message}


def inspect_milestone_checkpoint(repo_path: Path, *, base_sha: str = "") -> dict[str, Any]:
    repo = Path(repo_path)
    if not (repo / ".git").exists():
        return {"status": "error", "error": "workspace is not a git repository", "repo_path": str(repo)}
    status_result = _git_status_excluding_generated(repo)
    head = _current_head(repo)
    base = str(base_sha or "").strip()
    changed_since_base = bool(base and head and head != base)
    payload = {
        "commit_sha": head,
        "repo_path": str(repo),
        "base_sha": base,
        "changed_since_base": changed_since_base,
    }
    if status_result.stdout.strip():
        return {
            **payload,
            "status": "uncommitted_changes",
            "summary": "workspace has uncommitted changes; run op_minion_checkpoint_commit",
        }
    if changed_since_base:
        return {
            **payload,
            "status": "committed",
            "summary": "milestone checkpoint commit exists",
        }
    return {**payload, "status": "no_changes", "summary": "no milestone checkpoint commit exists"}


def _git_status_excluding_generated(repo: Path) -> GitCommandResult:
    return _git(repo, "status", "--porcelain", "--", ".", *GENERATED_COMMIT_EXCLUDES, check=True)


def finalize_work_order_branch(repo_path: Path, *, work_order_branch: str, merge_target: str, message: str) -> dict[str, Any]:
    repo = _finalize_repo_for_worktree(Path(repo_path))
    if not (repo / ".git").exists():
        return {"status": "error", "error": "workspace is not a git repository", "repo_path": str(repo)}
    _ensure_git_identity(repo)
    branch = str(work_order_branch or "").strip()
    target = str(merge_target or "").strip()
    if not branch or not target:
        return {"status": "error", "error": "work_order_branch and merge_target are required", "repo_path": str(repo)}
    if not _git(repo, "rev-parse", "--verify", branch).ok:
        return {"status": "error", "error": f"unknown work order branch: {branch}", "repo_path": str(repo)}
    if not _git(repo, "rev-parse", "--verify", target).ok:
        return {"status": "error", "error": f"unknown merge target: {target}", "repo_path": str(repo)}
    _git(repo, "checkout", target, check=True)
    merge_base = _git(repo, "merge-base", target, branch, check=True).stdout.strip()
    staged = _git(repo, "merge", "--squash", branch)
    if not staged.ok:
        _git(repo, "merge", "--abort")
        return {"status": "error", "error": staged.stderr or staged.stdout or "git merge --squash failed", "repo_path": str(repo)}
    if not _git(repo, "status", "--porcelain", check=True).stdout.strip():
        return {"status": "no_changes", "commit_sha": _current_head(repo), "repo_path": str(repo), "merge_base": merge_base}
    committed = _git(repo, "commit", "-m", str(message or f"minion: finalize {branch}"))
    if not committed.ok:
        return {"status": "error", "error": committed.stderr or committed.stdout or "git finalize commit failed", "repo_path": str(repo)}
    return {
        "status": "committed",
        "commit_sha": _current_head(repo),
        "repo_path": str(repo),
        "work_order_branch": branch,
        "merge_target": target,
        "merge_base": merge_base,
    }


def _finalize_repo_for_worktree(repo_path: Path) -> Path:
    repo = Path(repo_path)
    common_git_dir = _git_common_dir(repo)
    if common_git_dir is None:
        return repo
    project_root = _cleanup_project_root({}, common_git_dir=common_git_dir)
    if project_root is None or project_root.resolve() == repo.resolve():
        return repo
    if (project_root / ".git").exists() and _git(project_root, "rev-parse", "--is-inside-work-tree").ok:
        return project_root
    return repo


def prepare_dependency_integration_baseline(
    runtime_root: Path,
    workspace: dict[str, Any],
    *,
    project_name: str,
    parent_work_order_id: str,
    module_id: str,
    dependency_outputs: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create a git baseline that contains all completed dependency branches."""
    _ = runtime_root
    outputs = _integration_dependency_outputs(dependency_outputs)
    if len(outputs) <= 1:
        return {}
    first_repo = Path(str(outputs[0].get("repo_path") or "")).expanduser()
    if not (first_repo / ".git").exists():
        return {}
    common_git_dir = _git_common_dir(first_repo)
    if common_git_dir is None:
        return {}
    base_ref = str(outputs[0].get("ref") or "").strip()
    if not base_ref or not _git_bare(common_git_dir, "rev-parse", "--verify", base_ref).ok:
        return {}
    integration_branch = f"integration_{_safe_ref(parent_work_order_id)}_{_safe_ref(module_id)}"
    project_root = _cleanup_project_root(workspace, common_git_dir=common_git_dir) or common_git_dir.parent.parent
    integration_dir = _project_minion_dir(project_root) / "integrations" / _safe_ref(module_id or "join")
    _remove_integration_worktree(common_git_dir, integration_dir)
    _add_worktree_from_git_dir(common_git_dir, integration_dir, branch=integration_branch, base_ref=base_ref)
    _ensure_git_identity(integration_dir)
    merged_outputs = [dict(outputs[0])]
    for output in outputs[1:]:
        ref = str(output.get("ref") or "").strip()
        if not ref:
            continue
        if not _git(integration_dir, "rev-parse", "--verify", ref).ok:
            raise RuntimeError(f"unknown dependency ref for {module_id}: {ref}")
        merged = _git(integration_dir, "merge", "--no-ff", "--no-edit", ref)
        if not merged.ok:
            resolved = _resolve_generated_environment_conflicts(integration_dir)
            if resolved.get("status") == "resolved":
                merged_outputs.append(dict(output))
                continue
            _git(integration_dir, "merge", "--abort")
            module = str(output.get("module_id") or "")
            detail = merged.stderr or merged.stdout or f"git merge {ref} failed"
            raise RuntimeError(f"failed to integrate dependency {module or ref} for {module_id}: {detail}")
        merged_outputs.append(dict(output))
    return {
        "source_repo": str(integration_dir),
        "base_ref": integration_branch,
        "merge_target": integration_branch,
        "dependency_integration_baseline": {
            "mode": "parallel_dependency_baseline",
            "module_id": str(module_id or ""),
            "project_name": str(project_name or workspace.get("project_name") or ""),
            "repo_path": str(integration_dir),
            "branch": integration_branch,
            "commit_sha": _current_head(integration_dir),
            "dependencies": merged_outputs,
        },
    }


def _resolve_generated_environment_conflicts(repo_path: Path) -> dict[str, Any]:
    repo = Path(repo_path)
    unresolved = _git(repo, "diff", "--name-only", "--diff-filter=U")
    if not unresolved.ok:
        return {"status": "failed", "reason": "list_unmerged_failed", "stderr": unresolved.stderr}
    paths = [line.strip() for line in unresolved.stdout.splitlines() if line.strip()]
    if not paths:
        return {"status": "none"}
    unsupported = [path for path in paths if path not in GENERATED_ENVIRONMENT_UNION_MERGE_FILES]
    if unsupported:
        return {"status": "unsupported", "unsupported_paths": unsupported, "paths": paths}
    resolved_paths: list[str] = []
    for path in paths:
        merged = _merged_compile_flags_content(
            [
                _git_stage_text(repo, 2, path),
                _git_stage_text(repo, 3, path),
            ]
        )
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(merged, encoding="utf-8")
        added = _git(repo, "add", "--", path)
        if not added.ok:
            return {"status": "failed", "reason": "git_add_failed", "path": path, "stderr": added.stderr}
        resolved_paths.append(path)
    remaining = _git(repo, "diff", "--name-only", "--diff-filter=U")
    if not remaining.ok:
        return {"status": "failed", "reason": "list_remaining_failed", "stderr": remaining.stderr}
    remaining_paths = [line.strip() for line in remaining.stdout.splitlines() if line.strip()]
    if remaining_paths:
        return {"status": "failed", "reason": "unresolved_paths_remaining", "paths": remaining_paths}
    committed = _git(repo, "commit", "--no-edit")
    if not committed.ok:
        return {"status": "failed", "reason": "commit_failed", "stdout": committed.stdout, "stderr": committed.stderr}
    return {"status": "resolved", "paths": resolved_paths, "commit_sha": _current_head(repo)}


def _git_stage_text(repo_path: Path, stage: int, path: str) -> str:
    result = _git(Path(repo_path), "show", f":{int(stage)}:{path}")
    return result.stdout if result.ok else ""


def _merged_compile_flags_content(contents: list[str]) -> str:
    lines: list[str] = []
    seen: set[str] = set()
    for content in contents:
        for raw_line in str(content or "").splitlines():
            line = raw_line.rstrip()
            key = line.strip()
            if not key or key in seen:
                continue
            seen.add(key)
            lines.append(line)
    return "\n".join(lines) + ("\n" if lines else "")


def publish_completed_plan_workspace(
    workspace: dict[str, Any],
    *,
    module_outputs: list[dict[str, Any]],
    final_repo_path: str,
) -> dict[str, Any]:
    final_repo = Path(str(final_repo_path or "")).expanduser() if str(final_repo_path or "").strip() else None
    if final_repo is None or not (final_repo / ".git").exists():
        return {"status": "skipped", "reason": "missing_final_repo_path"}
    common_git_dir = _git_common_dir(final_repo)
    if common_git_dir is None:
        return {"status": "skipped", "reason": "missing_common_git_dir", "final_repo_path": str(final_repo)}
    project_root = _cleanup_project_root(workspace, common_git_dir=common_git_dir)
    if project_root is None:
        return {"status": "skipped", "reason": "missing_project_root", "final_repo_path": str(final_repo)}
    final_resolved = final_repo.resolve()
    root_branch = str((workspace or {}).get("root_work_order_branch") or "").strip()
    if not root_branch:
        root_id = str((workspace or {}).get("parent_work_order_id") or "").strip()
        root_branch = f"work_order_{_safe_ref(root_id)}" if root_id else ""
    if not root_branch:
        return {"status": "skipped", "reason": "missing_root_work_order_branch", "final_repo_path": str(final_resolved)}
    final_branch = str((workspace or {}).get("work_order_branch") or _current_branch(final_resolved)).strip()
    if not final_branch:
        return {"status": "skipped", "reason": "missing_final_branch", "final_repo_path": str(final_resolved)}
    if not _git_bare(common_git_dir, "rev-parse", "--verify", final_branch).ok:
        return {
            "status": "error",
            "reason": "unknown_final_branch",
            "final_branch": final_branch,
            "final_repo_path": str(final_resolved),
        }
    root_merge_target = str((workspace or {}).get("root_merge_target") or "").strip()
    if not root_merge_target:
        root_merge_target = str((workspace or {}).get("merge_target") or (workspace or {}).get("base_ref") or "HEAD").strip() or "HEAD"
    if not (project_root / ".git").exists():
        _write_project_gitfile(project_root, common_git_dir)
    _ensure_git_identity(project_root)
    _ensure_local_git_excludes(project_root)
    if not _git(project_root, "rev-parse", "--verify", root_branch).ok:
        _git(project_root, "checkout", "-B", root_branch, root_merge_target, check=True)
    else:
        _git(project_root, "checkout", root_branch, check=True)
    dirty = _git(project_root, "status", "--porcelain", "--untracked-files=no", check=True).stdout.strip()
    if dirty:
        return {
            "status": "error",
            "reason": "project_root_has_uncommitted_tracked_changes",
            "project_root": str(project_root),
            "dirty": dirty,
        }
    _git(project_root, "reset", "--hard", final_branch, check=True)
    candidates: list[Path] = []
    for output in module_outputs:
        if not isinstance(output, dict):
            continue
        repo_path = str(output.get("repo_path") or "").strip()
        if repo_path:
            candidates.append(Path(repo_path).expanduser())
    candidates.append(final_resolved)
    integration = workspace.get("dependency_integration_baseline")
    if isinstance(integration, dict):
        repo_path = str(integration.get("repo_path") or "").strip()
        if repo_path:
            candidates.append(Path(repo_path).expanduser())
    integrations_root = _project_minion_dir(project_root) / "integrations"
    if integrations_root.is_dir():
        candidates.extend([item for item in integrations_root.iterdir() if item.is_dir()])
    removed: list[str] = []
    skipped: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved == project_root:
            skipped.append({"path": str(resolved), "reason": "project_root"})
            continue
        if not _is_relative_to(resolved, project_root):
            skipped.append({"path": str(resolved), "reason": "outside_project_root"})
            continue
        if resolved == common_git_dir or _is_relative_to(common_git_dir, resolved):
            skipped.append({"path": str(resolved), "reason": "common_git_dir"})
            continue
        if not resolved.exists():
            continue
        removed_result = _remove_completed_worktree(common_git_dir, resolved)
        if removed_result.ok:
            removed.append(str(resolved))
        else:
            errors.append({"path": str(resolved), "error": removed_result.stderr or removed_result.stdout or "worktree remove failed"})
    _git_bare(common_git_dir, "worktree", "prune")
    with contextlib.suppress(OSError):
        integrations_root.rmdir()
    worktrees_root = _project_minion_dir(project_root) / "worktrees"
    with contextlib.suppress(OSError):
        worktrees_root.rmdir()
    return {
        "status": "ok" if not errors else "partial",
        "mode": "publish_final_to_project_root",
        "project_repo_path": str(project_root),
        "project_root": str(project_root),
        "common_git_dir": str(common_git_dir),
        "work_order_branch": root_branch,
        "merge_target": root_merge_target,
        "final_repo_path": str(final_resolved),
        "final_branch": final_branch,
        "commit_sha": _current_head(project_root),
        "removed": removed,
        "skipped": skipped,
        "errors": errors,
    }


def _integration_dependency_outputs(dependency_outputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen_refs: set[str] = set()
    for item in dependency_outputs:
        if not isinstance(item, dict):
            continue
        repo_path = str(item.get("repo_path") or "").strip()
        ref = str(item.get("branch") or item.get("work_order_branch") or item.get("commit_sha") or "").strip()
        if not repo_path or not ref or ref in seen_refs:
            continue
        seen_refs.add(ref)
        result.append(
            {
                "module_id": str(item.get("module_id") or ""),
                "module_name": str(item.get("module_name") or item.get("module_id") or ""),
                "child_work_order_id": str(item.get("child_work_order_id") or ""),
                "repo_path": repo_path,
                "ref": ref,
                "branch": str(item.get("branch") or item.get("work_order_branch") or ""),
                "commit_sha": str(item.get("commit_sha") or ""),
            }
        )
    return result


def _cleanup_project_root(workspace: dict[str, Any], *, common_git_dir: Path) -> Path | None:
    for key in ("work_order_repo_root", "runtime_project_path"):
        value = str((workspace or {}).get(key) or "").strip()
        if value:
            path = Path(value).expanduser()
            if path.exists():
                return path.resolve()
    parent = common_git_dir.parent
    if parent.name == ".minion":
        project_root = parent.parent
        return project_root.resolve() if project_root.exists() else None
    return parent.resolve() if parent.exists() else None


def _project_minion_dir(project_root: Path) -> Path:
    return Path(project_root) / ".minion"


def _project_common_git_dir(project_root: Path) -> Path:
    return _project_minion_dir(project_root) / "git"


def _git_common_dir(repo_path: Path) -> Path | None:
    result = _git(repo_path, "rev-parse", "--git-common-dir")
    if not result.ok:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    common = Path(raw)
    if not common.is_absolute():
        common = Path(repo_path) / common
    return common.resolve()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _remove_integration_worktree(git_dir: Path, path: Path) -> None:
    path = Path(path)
    if path.exists():
        subprocess.run(
            ["git", f"--git-dir={git_dir}", "worktree", "remove", "--force", str(path)],
            cwd=str(Path(git_dir).parent),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    if path.exists():
        shutil.rmtree(path)
    _git_bare(git_dir, "worktree", "prune")


def _remove_completed_worktree(git_dir: Path, path: Path) -> GitCommandResult:
    completed = subprocess.run(
        ["git", f"--git-dir={git_dir}", "worktree", "remove", "--force", str(path)],
        cwd=str(Path(git_dir).parent),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode == 0:
        return GitCommandResult(ok=True, stdout=completed.stdout or "", stderr=completed.stderr or "", returncode=0)
    if path.exists():
        try:
            shutil.rmtree(path)
            return GitCommandResult(ok=True, stdout=completed.stdout or "", stderr=completed.stderr or "", returncode=0)
        except OSError as exc:
            return GitCommandResult(
                ok=False,
                stdout=completed.stdout or "",
                stderr=str(exc) or completed.stderr or "",
                returncode=int(completed.returncode),
            )
    return GitCommandResult(ok=True, stdout=completed.stdout or "", stderr=completed.stderr or "", returncode=0)


def _ensure_git_identity(repo_path: Path) -> None:
    if not _git(repo_path, "config", "--local", "user.email").stdout.strip():
        _git(repo_path, "config", "--local", "user.email", "minion@pal.local", check=True)
    if not _git(repo_path, "config", "--local", "user.name").stdout.strip():
        _git(repo_path, "config", "--local", "user.name", "Pal Minion", check=True)


def _ensure_local_git_excludes(repo_path: Path) -> None:
    git_path = _git(repo_path, "rev-parse", "--git-path", "info/exclude")
    raw_path = git_path.stdout.strip() if git_path.ok else ""
    exclude_path = Path(raw_path) if raw_path else Path(repo_path) / ".git" / "info" / "exclude"
    if not exclude_path.is_absolute():
        exclude_path = Path(repo_path) / exclude_path
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    missing = [pattern for pattern in LOCAL_GIT_EXCLUDES if pattern not in existing.splitlines()]
    if not missing:
        return
    prefix = "" if not existing or existing.endswith("\n") else "\n"
    block = "\n".join(["# Pal minion generated artifact excludes", *missing])
    exclude_path.write_text(f"{existing}{prefix}{block}\n", encoding="utf-8")


def _has_head(repo_path: Path) -> bool:
    return _git(repo_path, "rev-parse", "--verify", "HEAD").ok


def _current_head(repo_path: Path) -> str:
    result = _git(repo_path, "rev-parse", "HEAD")
    return result.stdout.strip() if result.ok else ""


def _stage_milestone_changes(repo_path: Path) -> GitCommandResult:
    return _git(repo_path, "add", "-A", "--", ".", *GENERATED_COMMIT_EXCLUDES)


def _has_staged_changes(repo_path: Path) -> bool:
    result = _git(repo_path, "diff", "--cached", "--quiet")
    return result.returncode == 1


def _current_branch(repo_path: Path) -> str:
    result = _git(repo_path, "symbolic-ref", "--short", "HEAD")
    return result.stdout.strip() if result.ok else ""


def _git(repo_path: Path, *args: str, check: bool = False) -> GitCommandResult:
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo_path),
        capture_output=True,
        text=True,
        timeout=60,
    )
    result = GitCommandResult(
        ok=completed.returncode == 0,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        returncode=int(completed.returncode),
    )
    if check and not result.ok:
        raise RuntimeError(result.stderr or result.stdout or f"git {' '.join(args)} failed")
    return result


def _git_bare(git_dir: Path, *args: str, check: bool = False) -> GitCommandResult:
    completed = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        cwd=str(Path(git_dir).parent),
        capture_output=True,
        text=True,
        timeout=60,
    )
    result = GitCommandResult(
        ok=completed.returncode == 0,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
        returncode=int(completed.returncode),
    )
    if check and not result.ok:
        raise RuntimeError(result.stderr or result.stdout or f"git --git-dir={git_dir} {' '.join(args)} failed")
    return result


def _ensure_project_git_store(git_dir: Path, *, source_repo: str, workspace: dict[str, Any]) -> None:
    git_dir = Path(git_dir)
    project_root = _cleanup_project_root(workspace, common_git_dir=git_dir) or git_dir.parent.parent
    if (project_root / ".git").exists() and _git(project_root, "rev-parse", "--git-dir").ok:
        _ensure_git_identity(project_root)
        _ensure_local_git_excludes(project_root)
        return
    if git_dir.exists() and _git_bare(git_dir, "rev-parse", "--git-dir").ok:
        _write_project_gitfile(project_root, git_dir)
        _ensure_git_identity(project_root)
        _ensure_local_git_excludes(project_root)
        return
    source = str(source_repo or "").strip()
    if source:
        _clone_project_checkout(source, project_root)
        _move_project_git_dir(project_root, git_dir)
    else:
        _init_project_checkout(project_root, branch=str(workspace.get("base_ref") or "main").strip() or "main")
        _move_project_git_dir(project_root, git_dir)
    _ensure_git_identity(project_root)
    _ensure_local_git_excludes(project_root)


def _clone_project_checkout(source: str, project_root: Path) -> None:
    project_root = Path(project_root)
    if project_root.exists() and any(project_root.iterdir()):
        raise RuntimeError(f"target project root is not empty: {project_root}")
    project_root.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["git", "clone", str(source), str(project_root)],
        cwd=str(project_root.parent),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or f"git clone {source} failed")


def _init_project_checkout(project_root: Path, *, branch: str) -> None:
    project_root = Path(project_root)
    if project_root.exists() and any(project_root.iterdir()):
        raise RuntimeError(f"target project root is not empty: {project_root}")
    project_root.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["git", "init", str(project_root)],
        cwd=str(project_root.parent),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or f"git init {project_root} failed")
    _ensure_git_identity(project_root)
    _git(project_root, "checkout", "-B", _safe_ref(branch or "main"), check=True)
    _git(project_root, "commit", "--allow-empty", "-m", "minion: initialize project repo", check=True)


def _move_project_git_dir(project_root: Path, git_dir: Path) -> None:
    project_root = Path(project_root)
    git_dir = Path(git_dir)
    original_git = project_root / ".git"
    if not original_git.exists():
        raise RuntimeError(f"project checkout is missing .git: {project_root}")
    git_dir.parent.mkdir(parents=True, exist_ok=True)
    if git_dir.exists():
        raise RuntimeError(f"target common git dir is not empty: {git_dir}")
    shutil.move(str(original_git), str(git_dir))
    _write_project_gitfile(project_root, git_dir)


def _write_project_gitfile(project_root: Path, git_dir: Path) -> None:
    project_root = Path(project_root)
    git_dir = Path(git_dir)
    project_root.mkdir(parents=True, exist_ok=True)
    relative = git_dir
    with contextlib.suppress(ValueError):
        relative = git_dir.relative_to(project_root)
    value = relative.as_posix() if not relative.is_absolute() else str(relative)
    (project_root / ".git").write_text(f"gitdir: {value}\n", encoding="utf-8")


def _ensure_project_root_branch(project_root: Path, git_dir: Path, *, branch: str, base_ref: str) -> None:
    _ = git_dir
    project_root = Path(project_root)
    branch = str(branch or "").strip()
    if not branch:
        return
    base = str(base_ref or "HEAD").strip() or "HEAD"
    _ensure_git_identity(project_root)
    _ensure_local_git_excludes(project_root)
    if _git(project_root, "rev-parse", "--verify", branch).ok:
        _git(project_root, "checkout", branch, check=True)
    else:
        _git(project_root, "checkout", "-B", branch, base, check=True)


def _worktree_base_ref_from_git_dir(git_dir: Path, workspace: dict[str, Any]) -> str:
    base_ref = str(workspace.get("base_ref") or "").strip()
    if not base_ref:
        branch = _git_bare(git_dir, "symbolic-ref", "--short", "HEAD").stdout.strip()
        base_ref = branch or "HEAD"
    if not _git_bare(git_dir, "rev-parse", "--verify", base_ref).ok:
        base_ref = "HEAD"
    return base_ref


def _add_worktree_from_git_dir(git_dir: Path, repo_path: Path, *, branch: str, base_ref: str) -> None:
    repo_path = Path(repo_path)
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    if repo_path.exists() and any(repo_path.iterdir()):
        raise RuntimeError(f"target project repo is not empty and is not a git worktree: {repo_path}")
    completed = subprocess.run(
        ["git", f"--git-dir={git_dir}", "worktree", "add", "-B", str(branch), str(repo_path), str(base_ref or "HEAD")],
        cwd=str(git_dir.parent),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or f"git worktree add {repo_path} failed")


def _project_root_path(runtime_root: Path, workspace: dict[str, Any], project_name: str, *, work_order_id: str) -> Path:
    explicit = str(workspace.get("task_project_path") or workspace.get("target_project_path") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return runtime_root / "data" / "minion" / "repos" / _safe_ref(work_order_id or "work_order") / _safe_ref(project_name)


def _project_repo_path(
    runtime_root: Path,
    workspace: dict[str, Any],
    project_name: str,
    *,
    work_order_id: str,
    module_name: str = "",
) -> Path:
    explicit = str(workspace.get("task_repo_path") or workspace.get("target_repo_path") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    project_root = _project_root_path(runtime_root, workspace, project_name, work_order_id=work_order_id)
    module = _safe_ref(str(module_name or "").strip())
    if not module:
        return project_root
    return _project_minion_dir(project_root) / "worktrees" / module


def _project_name(metadata: dict[str, Any], workspace: dict[str, Any], *, task_id: str, source_repo: str) -> str:
    for source in (metadata, workspace):
        for key in ("project_name", "project_key", "project_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    plan_artifact = metadata.get("plan_artifact")
    if isinstance(plan_artifact, dict):
        plan_metadata = plan_artifact.get("metadata")
        if isinstance(plan_metadata, dict):
            for key in ("project_name", "project_key", "project_id"):
                value = str(plan_metadata.get(key) or "").strip()
                if value:
                    return value
    source_name = _source_repo_project_name(source_repo)
    if source_name:
        return source_name
    return str(task_id or "").strip() or "default_project"


def _module_name(metadata: dict[str, Any], workspace: dict[str, Any]) -> str:
    for source in (metadata, workspace):
        for key in ("module_name", "parent_module_name", "module_id", "parent_module_id"):
            value = str(source.get(key) or "").strip()
            if value:
                return value
    return ""


def _source_repo_project_name(source_repo: str) -> str:
    source = str(source_repo or "").strip()
    if not source:
        return ""
    if _is_local_path(source):
        try:
            return Path(source).expanduser().resolve().name
        except OSError:
            return Path(source).expanduser().name
    text = source.rstrip("/")
    if not text:
        return ""
    leaf = text.rsplit("/", 1)[-1]
    if leaf.endswith(".git"):
        leaf = leaf[:-4]
    if ":" in leaf:
        leaf = leaf.rsplit(":", 1)[-1]
    return leaf


def _source_repo(workspace: dict[str, Any]) -> str:
    for key in ("source_repo", "source_repo_path", "source_path", "clone_from", "repo_url", "remote_url"):
        value = str(workspace.get(key) or "").strip()
        if value:
            return value
    return ""


def _normalize_workspace_paths(workspace: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(workspace or {})
    workspace_type = str(normalized.get("type") or normalized.get("workspace_type") or "").strip().lower()
    cwd = str(normalized.get("cwd") or normalized.get("working_dir") or normalized.get("working_directory") or "").strip()
    if cwd and workspace_type in {"local_repo", "repo", "git_repo", "repository"}:
        normalized.setdefault("repo_path", cwd)
        normalized.setdefault("source_repo", cwd)
    return normalized


def _with_folder_workspace(runtime_root: Path, pack: TaskContextPack, workspace: dict[str, Any], *, run_id: str = "") -> TaskContextPack:
    prepared_workspace = dict(workspace)
    workspace_environment_policy = _policy_from_pack(pack, "workspace_environment_policy")
    if workspace_environment_policy:
        prepared_workspace["workspace_environment_policy"] = dict(workspace_environment_policy)
    repo_path = str(prepared_workspace.get("repo_path") or "").strip()
    if repo_path:
        environment = prepare_workspace_environment(
            Path(repo_path).expanduser(),
            pack,
            prepared_workspace,
            write_files=False,
            runtime_root=runtime_root,
            policy=workspace_environment_policy,
        )
        if environment:
            prepared_workspace["languages"] = list(environment.get("languages") or [])
            prepared_workspace["lsp_setup"] = dict(environment.get("lsp_setup") or {})
            execution_env = dict(environment.get("execution_env") or {})
            if execution_env:
                prepared_workspace["execution_env"] = execution_env
    if str(run_id or "").strip():
        preserved_artifact_dir = bool(prepared_workspace.get("preserve_artifact_dir"))
        for key in ("run_dir", "log_dir"):
            prepared_workspace.pop(key, None)
        if not preserved_artifact_dir:
            prepared_workspace.pop("artifact_dir", None)
    profile = _safe_ref(pack.minion_profile or "generic")
    run_part = _safe_ref(run_id or str(pack.metadata.get("run_id") or "") or pack.work_order_id)
    run_dir = Path(str(prepared_workspace.get("run_dir") or runtime_root / "data" / "minion" / "workspaces" / f"{run_part}_{profile}"))
    artifact_dir = Path(str(prepared_workspace.get("artifact_dir") or run_dir / "deliverables"))
    log_dir = Path(str(prepared_workspace.get("log_dir") or run_dir / "logs"))
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    prepared_workspace.update(
        {
            "workspace_kind": "folder",
            "run_dir": str(run_dir),
            "artifact_dir": str(artifact_dir),
            "log_dir": str(log_dir),
        }
    )
    prepared = TaskContextPack.from_dict({**pack.to_dict(), "workspace": prepared_workspace})
    _write_folder_workspace_metadata(run_dir, prepared)
    return prepared


def _write_folder_workspace_metadata(run_dir: Path, pack: TaskContextPack) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "work_order.json").write_text(
        json.dumps(pack.to_dict(), ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    metadata = {
        "work_order_id": pack.work_order_id,
        "minion_profile": pack.minion_profile,
        "workspace": dict(pack.workspace),
        "artifacts": list(pack.artifacts),
        "metadata": dict(pack.metadata),
    }
    (run_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, sort_keys=True, indent=2),
        encoding="utf-8",
    )


def _is_local_path(value: str) -> bool:
    if not value:
        return False
    if "://" in value:
        return False
    return Path(value).expanduser().exists()


def _policy_from_pack(pack: TaskContextPack, key: str) -> dict[str, Any]:
    workspace_value = pack.workspace.get(key)
    if isinstance(workspace_value, dict):
        return dict(workspace_value)
    profile = dict(pack.resolved_profile or {})
    effective_key = f"effective_{key}"
    if isinstance(profile.get(effective_key), dict):
        return dict(profile.get(effective_key) or {})
    if isinstance(profile.get(key), dict):
        return dict(profile.get(key) or {})
    return {}


def _safe_ref(value: str) -> str:
    normalized = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in str(value or "").strip())
    return normalized.strip("_")[:80] or "task"
