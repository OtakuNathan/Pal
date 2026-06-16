from __future__ import annotations

import subprocess
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.minion.workspace_environment import prepare_workspace_environment
from pal.shared import TaskContextPack


GENERATED_COMMIT_EXCLUDES = (
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob)**/.mypy_cache/**",
    ":(exclude,glob)**/.ruff_cache/**",
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
    "__pycache__/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
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
    repo_path = _task_repo_path(runtime_root, workspace, task_id)
    created_repo = not (repo_path / ".git").exists()

    if created_repo:
        if source_repo and not _same_local_path(source_repo, repo_path):
            _clone_repo(source_repo, repo_path)
        else:
            repo_path.mkdir(parents=True, exist_ok=True)
            _git(repo_path, "init", check=True)
            _git(repo_path, "checkout", "-B", "main", check=True)
    _ensure_git_identity(repo_path)
    if not _has_head(repo_path):
        _git(repo_path, "commit", "--allow-empty", "-m", "minion: initialize task repo", check=True)
    _ensure_local_git_excludes(repo_path)
    environment = prepare_workspace_environment(repo_path, pack, workspace, write_files=created_repo, runtime_root=runtime_root)
    if environment:
        workspace["languages"] = list(environment.get("languages") or [])
        workspace["lsp_setup"] = dict(environment.get("lsp_setup") or {})
        created_files = [str(item) for item in list(environment.get("created_files") or []) if str(item).strip()]
        if created_files:
            _git(repo_path, "add", "--", *created_files, check=True)
            if _has_staged_changes(repo_path):
                _git(repo_path, "commit", "-m", "minion: prepare workspace environment", check=True)
                workspace["lsp_setup"]["baseline_commit_sha"] = _current_head(repo_path)

    base_ref = str(workspace.get("base_ref") or _current_branch(repo_path) or "HEAD").strip() or "HEAD"
    if not _git(repo_path, "rev-parse", "--verify", base_ref).ok:
        base_ref = "HEAD"
    base_sha = _git(repo_path, "rev-parse", base_ref, check=True).stdout.strip()
    branch = str(workspace.get("work_order_branch") or f"work_order_{_safe_ref(pack.work_order_id)}").strip()
    merge_target = str(workspace.get("merge_target") or base_ref).strip() or base_ref
    if _git(repo_path, "rev-parse", "--verify", branch).ok:
        _git(repo_path, "checkout", branch, check=True)
    else:
        _git(repo_path, "checkout", "-B", branch, base_ref, check=True)

    workspace.update(
        {
            "repo_path": str(repo_path),
            "base_ref": base_ref,
            "base_sha": base_sha,
            "work_order_branch": branch,
            "merge_target": merge_target,
            "workspace_kind": "git_repo",
        }
    )
    artifact_dir = repo_path / "minion_outputs" / _safe_ref(pack.work_order_id)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    workspace.setdefault("run_dir", str(artifact_dir))
    workspace["artifact_dir"] = str(artifact_dir)
    workspace["workspace_policy"] = {"mode": "writable_git_branch"}
    workspace["completion_policy"] = {"evidence": "git_commit", "requires_capability_evidence": True}
    if source_repo:
        workspace.setdefault("source_repo", source_repo)
    return _with_checkpoint_commit_capability(TaskContextPack.from_dict({**pack.to_dict(), "workspace": workspace}))


def prepare_task_workspace(runtime_root: Path, pack: TaskContextPack, *, run_id: str = "") -> TaskContextPack:
    workspace = _normalize_workspace_paths(pack.workspace)
    workspace_policy = _policy_from_pack(pack, "workspace_policy")
    completion_policy = _policy_from_pack(pack, "completion_policy")
    mode = str(workspace_policy.get("mode") or "").strip().lower()
    evidence = str(completion_policy.get("evidence") or "").strip().lower()
    if not mode and not evidence and "op_exec_shell" in {str(item) for item in pack.allowed_capabilities}:
        mode = "writable_git_branch"
        evidence = "git_commit"
        workspace_policy = {"mode": mode}
        completion_policy = {"evidence": evidence, "requires_capability_evidence": True}
    if mode == "writable_git_branch" or evidence == "git_commit":
        prepared = prepare_git_task_environment(runtime_root, pack)
        prepared_workspace = dict(prepared.workspace)
        prepared_workspace["workspace_policy"] = {**workspace_policy, "mode": "writable_git_branch"}
        if completion_policy:
            prepared_workspace["completion_policy"] = dict(completion_policy)
        return _with_checkpoint_commit_capability(TaskContextPack.from_dict({**prepared.to_dict(), "workspace": prepared_workspace}))
    if mode == "read_only_repo":
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
        return _with_folder_workspace(runtime_root, pack, workspace, run_id=run_id)
    if workspace_policy:
        workspace["workspace_policy"] = dict(workspace_policy)
    if completion_policy:
        workspace["completion_policy"] = dict(completion_policy)
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
    status_result = _git(repo, "status", "--porcelain", check=True)
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


def finalize_work_order_branch(repo_path: Path, *, work_order_branch: str, merge_target: str, message: str) -> dict[str, Any]:
    repo = Path(repo_path)
    if not (repo / ".git").exists():
        return {"status": "error", "error": "workspace is not a git repository", "repo_path": str(repo)}
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


def _ensure_git_identity(repo_path: Path) -> None:
    if not _git(repo_path, "config", "user.email").stdout.strip():
        _git(repo_path, "config", "user.email", "minion@pal.local", check=True)
    if not _git(repo_path, "config", "user.name").stdout.strip():
        _git(repo_path, "config", "user.name", "Pal Minion", check=True)


def _ensure_local_git_excludes(repo_path: Path) -> None:
    exclude_path = Path(repo_path) / ".git" / "info" / "exclude"
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


def _clone_repo(source: str, repo_path: Path) -> None:
    repo_path = Path(repo_path)
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    if repo_path.exists() and any(repo_path.iterdir()):
        raise RuntimeError(f"target task repo is not empty and is not a git repository: {repo_path}")
    completed = subprocess.run(
        ["git", "clone", str(source), str(repo_path)],
        cwd=str(repo_path.parent),
        capture_output=True,
        text=True,
        timeout=180,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or f"git clone {source} failed")


def _task_repo_path(runtime_root: Path, workspace: dict[str, Any], task_id: str) -> Path:
    explicit = str(workspace.get("task_repo_path") or workspace.get("target_repo_path") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    return runtime_root / "data" / "minion" / "repos" / task_id


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
    if str(run_id or "").strip():
        for key in ("run_dir", "artifact_dir", "log_dir"):
            prepared_workspace.pop(key, None)
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


def _same_local_path(source: str, repo_path: Path) -> bool:
    try:
        return Path(source).expanduser().resolve() == Path(repo_path).expanduser().resolve()
    except Exception:
        return False


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
