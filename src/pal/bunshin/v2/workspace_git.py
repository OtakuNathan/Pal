from __future__ import annotations
import subprocess
import tempfile
from pathlib import Path


def _git(worktree: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=worktree,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout


def _git_dir(git_dir: Path, *args: str) -> str:
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ).stdout


def _git_bytes(worktree: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=worktree,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ).stdout


def _git_is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    return subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repository,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _git_ref_exists(workspace: Path, ref_name: str) -> bool:
    return subprocess.run(
        ["git", "rev-parse", "--quiet", "--verify", ref_name],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _abort_cherry_pick(workspace: Path) -> None:
    subprocess.run(
        ["git", "cherry-pick", "--abort"],
        cwd=workspace,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )


def git_changed_paths(worktree: Path, base_sha: str) -> list[str]:
    # Report both sides of a rename so moving a frozen/reference-only path into
    # an owned scope cannot bypass the path policy.
    output = _git_bytes(worktree, "diff", "--name-only", "--no-renames", "-z", base_sha, "--")
    tracked = [item.decode("utf-8", errors="surrogateescape") for item in output.split(b"\0") if item]
    untracked_output = _git_bytes(worktree, "ls-files", "--others", "--exclude-standard", "-z")
    untracked = [item.decode("utf-8", errors="surrogateescape") for item in untracked_output.split(b"\0") if item]
    return sorted(set(tracked + untracked))


def _git_commit_exists(git_dir: Path, commit_sha: str) -> bool:
    if not git_dir.is_dir() or not commit_sha:
        return False
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", "cat-file", "-e", f"{commit_sha}^{{commit}}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _git_branch_exists(git_dir: Path, branch: str) -> bool:
    return subprocess.run(
        ["git", f"--git-dir={git_dir}", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0


def _force_branch(git_dir: Path, branch: str, commit_sha: str) -> None:
    subprocess.run(
        ["git", "check-ref-format", "--branch", branch],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )
    _git_dir(git_dir, "update-ref", f"refs/heads/{branch}", commit_sha)


def _add_branch_worktree(
    git_dir: Path,
    *,
    worktree: Path,
    branch: str,
    start_sha: str,
) -> None:
    if _git_branch_exists(git_dir, branch):
        command = ["git", f"--git-dir={git_dir}", "worktree", "add", str(worktree), branch]
    else:
        command = [
            "git",
            f"--git-dir={git_dir}",
            "worktree",
            "add",
            "-b",
            branch,
            str(worktree),
            start_sha,
        ]
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            completed.stderr
            or completed.stdout
            or f"failed to create stable workflow worktree {worktree}"
        )


def _clone_bundle_repository(common_git_dir: Path, *, bundle_bytes: bytes) -> None:
    common_git_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pal-skeleton-clone-") as temporary:
        bundle = Path(temporary) / "architecture.bundle"
        bundle.write_bytes(bundle_bytes)
        completed = subprocess.run(
            ["git", "clone", "--bare", str(bundle), str(common_git_dir)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "failed to restore project repository")


def _fetch_bundle_repository(
    common_git_dir: Path,
    *,
    bundle_bytes: bytes,
    namespace: str,
) -> None:
    with tempfile.TemporaryDirectory(prefix="pal-skeleton-fetch-") as temporary:
        bundle = Path(temporary) / "architecture.bundle"
        bundle.write_bytes(bundle_bytes)
        completed = subprocess.run(
            [
                "git",
                f"--git-dir={common_git_dir}",
                "fetch",
                str(bundle),
                f"+refs/heads/*:{namespace}/*",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout or "failed to import skeleton bundle")


def _safe_ref(value: str) -> str:
    return "".join(char if char.isalnum() or char in {"-", "_"} else "_" for char in str(value))[:80] or "node"
