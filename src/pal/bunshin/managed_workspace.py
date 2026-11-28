"""Persistent task Git data; worker worktrees remain owned by the Git adapter."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping

from pal.bunshin.catalog_store import atomic_write_json
from pal.bunshin.workspace_git import _git, _git_is_ancestor
from pal.foundation.managed_workspace_io import managed_workspace_write_gate


def managed_workspace(root: Path, workspace: Mapping[str, Any], title: str, family: str) -> dict[str, Any]:
    value = dict(workspace)
    if value.get("kind") != "managed_git" and not value.get("managed_git"):
        return value
    if family != "general":
        raise ValueError("managed Git is currently available for general tasks")
    root = root.resolve()
    if title == "__PROACTIVE__":
        path = root / "proactive"
    else:
        stem = re.sub(r"[^\w-]+", "-", title.casefold()).strip("-")
        if not stem:
            raise ValueError("managed task requires a semantic name")
        path = root / "data" / "bunshin" / "general" / stem
    if value.get("repo_path") and Path(value["repo_path"]).resolve() != path.resolve():
        raise ValueError("managed workspace path is derived from the Task title")
    if path.is_symlink() or path.resolve() != path.absolute():
        raise ValueError("managed workspace must be a canonical directory")
    return {**value, "kind": "existing_repo", "repo_path": str(path),
            "project_name": title, "managed_git": True}


def _commit(root: Path, message: str) -> None:
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=Pal", "-c", "user.email=pal@localhost",
         "commit", "--allow-empty", "-m", message)


def _validate_tree(root: Path, revision: str) -> None:
    for entry in _git(root, "ls-tree", "-r", revision).splitlines():
        if entry.split(None, 1)[0] not in {"100644", "100755"}:
            raise ValueError("managed Git accepts regular files only")


@dataclass(frozen=True)
class PromotionIntent:
    before: str
    target: str
    candidate: str
    receipt: str

    def __post_init__(self) -> None:
        if any(not re.fullmatch(r"[0-9a-f]{40}", value) for value in (self.before, self.target, self.candidate)):
            raise ValueError("invalid managed promotion commit")
        if not re.fullmatch(r"pal-delivery-[0-9a-f]{64}\.json", self.receipt):
            raise ValueError("invalid managed promotion receipt identity")


def _marker(root: Path) -> Path:
    return root / ".git" / "pal-promotion.json"


def _reconcile(root: Path) -> None:
    marker = _marker(root)
    if not marker.exists():
        return
    if (root / ".git").is_symlink() or not (root / ".git").is_dir():
        raise ValueError("invalid managed Git directory")
    intent = PromotionIntent(**json.loads(marker.read_text()))
    head = _git(root, "rev-parse", "HEAD").strip()
    if head not in {intent.before, intent.target}:
        raise RuntimeError("managed workspace head changed during publication")
    if _git(root, "status", "--porcelain", "--untracked-files=all").strip():
        # A dirty tree can reflect an interrupted checkout or an external
        # editor. Preserve it for explicit recovery rather than reset it.
        raise RuntimeError("managed workspace has unresolved publication changes")
    if head != intent.target:
        _git(root, "merge", "--ff-only", intent.target)
    atomic_write_json(root / ".git" / intent.receipt, {"candidate": intent.candidate, "target": intent.target})
    marker.unlink()


def capture_managed_head(runtime: Path, root: Path) -> str:
    """Caller holds the exclusive managed-workspace gate."""
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        _git(root, "init", "-b", "main")
        _commit(root, "Initialize managed task data")
    if not (root / ".git").is_dir() or (root / ".git").is_symlink():
        raise ValueError("managed workspace requires its own Git directory")
    _reconcile(root)
    if _git(root, "status", "--porcelain", "--untracked-files=all").strip():
        _commit(root, "Capture foreground task data")
    head = _git(root, "rev-parse", "HEAD").strip()
    _validate_tree(root, head)
    return head


def promote_managed_candidate(runtime: Path, root: Path, repository: Path,
                              base: str, candidate: str, workflow_id: str) -> str:
    with managed_workspace_write_gate(runtime):
        key = hashlib.sha256(workflow_id.encode()).hexdigest()
        if (root / ".git").is_symlink() or root.is_symlink():
            raise ValueError("managed workspace cannot be a symlink")
        _reconcile(root)
        receipt = root / ".git" / ("pal-delivery-" + key + ".json")
        if receipt.exists():
            saved = json.loads(receipt.read_text())
            if saved["candidate"] != candidate:
                raise RuntimeError("managed delivery already bound to another candidate")
            return str(saved["target"])
        current = capture_managed_head(runtime, root)
        ref = "refs/pal/deliveries/" + key
        _git(root, "fetch", "--no-tags", str(repository), candidate + ":" + ref)
        _validate_tree(root, candidate)
        if not _git_is_ancestor(root, base, current) or not _git_is_ancestor(root, base, candidate):
            raise ValueError("managed publication must retain the shared baseline")
        if _git_is_ancestor(root, candidate, current):
            target = current
        else:
            def changed(end: str) -> set[str]:
                return set(_git(root, "diff", "--name-only", "--no-renames", "-z", base, end).strip("\0").split("\0")) - {""}
            foreground, background = changed(current), changed(candidate)
            if any(a == b or a.startswith(b + "/") or b.startswith(a + "/")
                   for a in foreground for b in background):
                raise RuntimeError(f"managed workspace conflict: foreground {current}, accepted {candidate}; resolve in {root} and retry delivery")
            if current == base:
                target = candidate
            else:
                with tempfile.TemporaryDirectory(prefix="pal-managed-merge-") as tmp:
                    merge = Path(tmp) / "merge"
                    _git(root, "worktree", "add", "--detach", str(merge), current)
                    try:
                        _git(merge, "-c", "user.name=Pal", "-c", "user.email=pal@localhost",
                             "merge", "--no-ff", "--no-edit", candidate)
                        target = _git(merge, "rev-parse", "HEAD").strip()
                        _git(root, "update-ref", "refs/pal/promotions/" + key, target)
                    finally:
                        _git(root, "worktree", "remove", "--force", str(merge))
        # Keep the exact promotion intent before advancing the canonical head.
        atomic_write_json(_marker(root), asdict(PromotionIntent(current, target, candidate, receipt.name)))
        _reconcile(root)
        return target


def migrate_proactive_data(runtime: Path) -> None:
    """Offline idempotent import. Preserve the original data as a rollback copy."""
    import shutil
    from pal.foundation.proactive_workspace import LEGACY_PROACTIVE_DATA

    def inventory(path: Path) -> dict[str, str]:
        if path.is_symlink():
            raise ValueError("proactive source contains an unexpected symlink")
        files = [path] if path.is_file() else sorted(path.rglob("*"))
        result = {}
        for item in files:
            if item.is_symlink():
                raise ValueError("proactive source contains an unexpected symlink")
            if item.is_file():
                result[str(item.relative_to(path)) if item != path else "."] = hashlib.sha256(item.read_bytes()).hexdigest()
        return result

    canonical = runtime / "proactive"
    backup = runtime / "backups" / "proactive-workspace-v32"
    with managed_workspace_write_gate(runtime):
        for name in LEGACY_PROACTIVE_DATA:
            old = runtime / "data" / name
            target = canonical / name
            saved = backup / name
            if old.is_symlink():
                if old.resolve() != target.resolve() or not target.exists():
                    raise ValueError("unexpected proactive compatibility link")
                continue
            source = old if old.exists() else saved
            if not source.exists():
                continue
            if target.exists():
                if inventory(source) != inventory(target):
                    raise ValueError(f"proactive migration destination conflict: {name}")
            else:
                canonical.mkdir(parents=True, exist_ok=True)
                # Copy into a sibling and atomically publish only a complete tree.
                with tempfile.TemporaryDirectory(prefix=".proactive-import-", dir=canonical) as staging:
                    staged = Path(staging) / name
                    if source.is_dir():
                        shutil.copytree(source, staged, symlinks=True)
                    else:
                        shutil.copy2(source, staged)
                    if inventory(source) != inventory(staged):
                        raise ValueError(f"proactive migration verification failed: {name}")
                    staged.rename(target)
            if old.exists():
                backup.mkdir(parents=True, exist_ok=True)
                if saved.exists():
                    raise ValueError(f"proactive migration backup conflict: {name}")
                old.rename(saved)
            old.parent.mkdir(parents=True, exist_ok=True)
            old.symlink_to(target, target_is_directory=target.is_dir())
        if canonical.exists():
            capture_managed_head(runtime, canonical)
