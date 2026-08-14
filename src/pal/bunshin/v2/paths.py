from __future__ import annotations

import fcntl
from contextlib import contextmanager
import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4


_UNSAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_UNSAFE_REF_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


def bunshin_data_root(runtime_root: Path) -> Path:
    return Path(runtime_root) / "data" / "bunshin"


def artifact_store_root(runtime_root: Path) -> Path:
    return bunshin_data_root(runtime_root) / "artifacts" / "sha256"


def runtime_spool_root(runtime_root: Path) -> Path:
    return bunshin_data_root(runtime_root) / "runtime"


def invocation_root(runtime_root: Path) -> Path:
    return runtime_spool_root(runtime_root) / "invocations"


def role_workspace_root(runtime_root: Path) -> Path:
    return runtime_spool_root(runtime_root) / "role-workspaces"


def artifact_epoch_root(runtime_root: Path) -> Path:
    return runtime_spool_root(runtime_root) / "artifact-epochs"


def verification_scratch_root(runtime_root: Path) -> Path:
    return runtime_spool_root(runtime_root) / "verification"


def standalone_review_root(runtime_root: Path) -> Path:
    return runtime_spool_root(runtime_root) / "standalone-reviews"


def deliverable_root(runtime_root: Path) -> Path:
    return bunshin_data_root(runtime_root) / "deliverables"


def plan_revision_root(
    runtime_root: Path,
    *,
    repository_layout: Mapping[str, Any],
    workflow_id: str,
    revision_id: str,
) -> Path:
    layout = dict(repository_layout or {})
    project_key = _path_component(str(layout.get("project_key") or "project"), fallback="project")
    workflow_key = _path_component(
        str(layout.get("workflow_key") or _workflow_key("workflow", workflow_id)),
        fallback=f"workflow-{_short_hash(workflow_id)}",
    )
    revision_key = _short_key(revision_id, prefix="revision")
    return bunshin_data_root(runtime_root) / "plan_revisions" / project_key / workflow_key / revision_key


@dataclass(frozen=True)
class ProjectGitLayout:
    project_name: str
    project_key: str
    project_root: Path
    common_git_dir: Path
    workflow_name: str
    workflow_key: str
    workflow_branch: str

    @property
    def workflow_worktree_root(self) -> Path:
        return self.project_root / "worktrees" / self.workflow_key

    @property
    def workflow_metadata_root(self) -> Path:
        return self.project_root / "workflows" / self.workflow_key

    @property
    def workspace_snapshot_marker(self) -> Path:
        return self.workflow_metadata_root / "workspace_snapshot_ref.json"

    @property
    def branch_namespace(self) -> str:
        return self.workflow_branch.removesuffix("/main")

    def architecture_worktree(self, revision_name: str = "") -> Path:
        """Return the workflow-lifetime Architecture worktree.

        Architecture revisions are commits on one branch, not separate
        workspaces.  ``revision_name`` remains accepted so callers do not need
        to invent a second workspace identity while dispatching a revision.
        """

        return self.workflow_worktree_root / "architecture"

    def architecture_branch(self, revision_name: str = "") -> str:
        """Return the workflow-lifetime Architecture branch."""

        return f"{self.branch_namespace}/architecture"

    def module_worktree(self, module_name: str) -> Path:
        """Return the workflow-lifetime worktree owned by one Module identity."""

        return (
            self.workflow_worktree_root
            / "modules"
            / _path_component(module_name, fallback="module")
        )

    def module_branch(self, module_name: str) -> str:
        """Return the workflow-lifetime branch owned by one Module identity."""

        return (
            f"{self.branch_namespace}/module/"
            f"{_ref_component(module_name, fallback='module')}"
        )

    def to_artifact_dict(self) -> dict[str, str]:
        return {
            "project_name": self.project_name,
            "project_key": self.project_key,
            "workflow_name": self.workflow_name,
            "workflow_key": self.workflow_key,
            "workflow_branch": self.workflow_branch,
        }


def resolve_project_git_layout(
    runtime_root: Path,
    *,
    workspace: Mapping[str, Any],
    workflow_id: str,
    workflow_name: str,
    stored_layout: Mapping[str, Any] | None = None,
) -> ProjectGitLayout:
    stored = dict(stored_layout or {})
    source_key = _workspace_source_key(workspace)
    project_name = str(stored.get("project_name") or _project_name(workspace)).strip()
    preferred_key = str(stored.get("project_key") or _path_component(project_name, fallback="project"))
    repos_root = bunshin_data_root(runtime_root) / "repos"
    if stored.get("project_key") and not str(workspace.get("repo_path") or workspace.get("cwd") or "").strip():
        existing_source_key = _project_marker_source_key(repos_root / _path_component(preferred_key, fallback="project"))
        if existing_source_key:
            source_key = existing_source_key
    project_key = _select_project_key(
        repos_root,
        preferred_key=preferred_key,
        project_name=project_name,
        source_key=source_key,
    )
    project_root = repos_root / project_key
    project_root.mkdir(parents=True, exist_ok=True)
    _write_project_marker(
        project_root,
        project_name=project_name,
        project_key=project_key,
        source_key=source_key,
    )
    resolved_workflow_name = str(stored.get("workflow_name") or workflow_name or project_name).strip()
    workflow_key = str(stored.get("workflow_key") or _workflow_key(resolved_workflow_name, workflow_id))
    workflow_branch = str(stored.get("workflow_branch") or f"bunshin/{workflow_key}/main")
    return ProjectGitLayout(
        project_name=project_name,
        project_key=project_key,
        project_root=project_root,
        common_git_dir=project_root / "project.git",
        workflow_name=resolved_workflow_name,
        workflow_key=workflow_key,
        workflow_branch=workflow_branch,
    )


def cleanup_workflow_worktrees(
    runtime_root: Path,
    *,
    repository_layout: Mapping[str, Any],
) -> tuple[str, ...]:
    """Remove every internal worktree owned by one terminal workflow.

    Delivery checkouts live outside this layout and are intentionally retained.
    The shared bare repository and content-addressed artifacts are also retained.
    """

    layout = resolve_project_git_layout(
        runtime_root,
        workspace={},
        workflow_id="",
        workflow_name=str(repository_layout.get("workflow_name") or "workflow"),
        stored_layout=repository_layout,
    )
    root = layout.workflow_worktree_root.resolve()
    removed: list[str] = []
    if layout.common_git_dir.is_dir():
        listed = subprocess.run(
            [
                "git",
                f"--git-dir={layout.common_git_dir}",
                "worktree",
                "list",
                "--porcelain",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if listed.returncode != 0:
            raise RuntimeError(
                listed.stderr or listed.stdout or "failed to enumerate workflow worktrees"
            )
        for line in listed.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            worktree = Path(line.removeprefix("worktree ").strip()).resolve()
            if worktree != root and not worktree.is_relative_to(root):
                continue
            retired = subprocess.run(
                [
                    "git",
                    f"--git-dir={layout.common_git_dir}",
                    "worktree",
                    "remove",
                    "--force",
                    str(worktree),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if retired.returncode != 0:
                raise RuntimeError(
                    retired.stderr
                    or retired.stdout
                    or f"failed to retire workflow worktree {worktree}"
                )
            removed.append(str(worktree))
        subprocess.run(
            ["git", f"--git-dir={layout.common_git_dir}", "worktree", "prune"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    shutil.rmtree(layout.workflow_worktree_root, ignore_errors=True)
    shutil.rmtree(layout.workflow_metadata_root, ignore_errors=True)
    return tuple(sorted(removed))


def inferred_project_name(workspace: Mapping[str, Any]) -> str:
    return _project_name(workspace)


@contextmanager
def project_git_layout_lock(layout: ProjectGitLayout) -> Iterator[None]:
    lock_path = layout.project_root / ".git-layout.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _project_name(workspace: Mapping[str, Any]) -> str:
    explicit = str(workspace.get("project_name") or "").strip()
    if explicit:
        return explicit
    source = str(workspace.get("repo_path") or workspace.get("cwd") or "").strip()
    if source:
        name = Path(source).expanduser().name
        if name.endswith(".git"):
            name = name[:-4]
        if name:
            return name
    return "project"


def _workspace_source_key(workspace: Mapping[str, Any]) -> str:
    source = str(workspace.get("repo_path") or workspace.get("cwd") or "").strip()
    if source:
        return os.path.realpath(Path(source).expanduser())
    return f"new-project:{_project_name(workspace)}"


def _select_project_key(
    repos_root: Path,
    *,
    preferred_key: str,
    project_name: str,
    source_key: str,
) -> str:
    preferred = _path_component(preferred_key, fallback="project")
    candidate = repos_root / preferred
    if _project_root_matches(candidate, source_key=source_key):
        return preferred
    suffix = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:8]
    disambiguated = _path_component(f"{preferred}-{suffix}", fallback=f"project-{suffix}")
    target = repos_root / disambiguated
    if _project_root_matches(target, source_key=source_key):
        return disambiguated
    raise ValueError(
        f"project repository path collision for {project_name!r}; "
        f"both {candidate} and {target} belong to different sources"
    )


def _project_root_matches(path: Path, *, source_key: str) -> bool:
    if not path.exists():
        return True
    marker = path / "project.json"
    if not marker.is_file():
        return False
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return str(payload.get("source_key") or "") == source_key


def _project_marker_source_key(path: Path) -> str:
    marker = path / "project.json"
    if not marker.is_file():
        return ""
    try:
        payload = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(payload.get("source_key") or "")


def _write_project_marker(
    project_root: Path,
    *,
    project_name: str,
    project_key: str,
    source_key: str,
) -> None:
    marker = project_root / "project.json"
    payload = {
        "schema_version": "1",
        "project_name": project_name,
        "project_key": project_key,
        "source_key": source_key,
    }
    if marker.is_file():
        current = json.loads(marker.read_text(encoding="utf-8"))
        if current != payload:
            raise ValueError(f"project repository identity changed: {project_root}")
        return
    temporary = marker.with_name(f".{marker.name}.{uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, marker)


def _workflow_key(workflow_name: str, workflow_id: str) -> str:
    readable = _ref_component(workflow_name, fallback="workflow")[:48].strip(".-") or "workflow"
    return f"{readable}-{_short_hash(workflow_id)}"


def _short_key(value: str, *, prefix: str) -> str:
    readable = _path_component(value, fallback=prefix)
    if len(readable) <= 48 and not readable.startswith(("wf_", "rev_", "arch_", "epoch_")):
        return readable
    return f"{prefix}-{_short_hash(value)}"


def _short_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]


def _path_component(value: str, *, fallback: str) -> str:
    normalized = _UNSAFE_COMPONENT.sub("-", str(value).strip()).strip(".-")
    return normalized[:80] or fallback


def _ref_component(value: str, *, fallback: str) -> str:
    normalized = _UNSAFE_REF_COMPONENT.sub("-", str(value).strip()).strip(".-")
    normalized = normalized.replace("..", ".")
    if normalized.endswith(".lock"):
        normalized = normalized[:-5] + "-lock"
    return normalized[:80] or fallback
