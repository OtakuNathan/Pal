from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping

from pydantic import BaseModel, ConfigDict

from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.paths import bunshin_data_root


class DeliveryReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal["1"] = "1"
    kind: Literal["pull_request", "local_checkout"]
    commit_sha: str
    branch_name: str
    base_branch: str
    pr_url: str = ""
    local_path: str = ""
    fallback_reason: str = ""
    verification_ref: dict[str, Any]


@dataclass
class DeliveryService:
    runtime_root: Path
    artifacts: ContentAddressedArtifactStore

    def publish(
        self,
        *,
        workflow_id: str,
        workflow_key: str,
        task_title: str,
        repository: Path,
        commit_sha: str,
        source_snapshot: Mapping[str, Any],
        verification_ref: ArtifactRef,
    ) -> ArtifactRef:
        if _git(repository, "rev-parse", f"{commit_sha}^{{commit}}").strip() != commit_sha:
            raise ValueError("delivery commit is unavailable in the sink module repository")
        branch_name = f"pal/bunshin/{_ref_slug(workflow_key or workflow_id)}"
        base_branch = str(source_snapshot.get("source_branch") or "").strip()
        fallback_reason = ""
        if (
            source_snapshot.get("source_clean") is True
            and str(source_snapshot.get("delivery_mode") or "")
            == "pull_request_preferred"
            and base_branch
        ):
            try:
                pr_url = self._publish_pull_request(
                    repository=repository,
                    commit_sha=commit_sha,
                    branch_name=branch_name,
                    base_branch=base_branch,
                    task_title=task_title,
                    source_snapshot=source_snapshot,
                )
            except Exception as exc:
                fallback_reason = f"{exc.__class__.__name__}: {exc}"[-1000:]
            else:
                receipt = DeliveryReceipt(
                    kind="pull_request",
                    commit_sha=commit_sha,
                    branch_name=branch_name,
                    base_branch=base_branch,
                    pr_url=pr_url,
                    verification_ref=verification_ref.to_dict(),
                )
                return self._store(receipt, verification_ref)
        elif str(source_snapshot.get("delivery_mode") or "") == "local_only":
            fallback_reason = str(
                source_snapshot.get("delivery_fallback_reason")
                or "source workspace was dirty, non-Git, detached, or had no push target"
            )[-1000:]

        local_path = self._publish_local_checkout(
            workflow_id=workflow_id,
            workflow_key=workflow_key,
            task_title=task_title,
            repository=repository,
            commit_sha=commit_sha,
        )
        receipt = DeliveryReceipt(
            kind="local_checkout",
            commit_sha=commit_sha,
            branch_name=branch_name,
            base_branch=base_branch,
            local_path=str(local_path),
            fallback_reason=fallback_reason,
            verification_ref=verification_ref.to_dict(),
        )
        return self._store(receipt, verification_ref)

    def _publish_pull_request(
        self,
        *,
        repository: Path,
        commit_sha: str,
        branch_name: str,
        base_branch: str,
        task_title: str,
        source_snapshot: Mapping[str, Any],
    ) -> str:
        source_repo_value = str(source_snapshot.get("source_repo_path") or "").strip()
        remote_name = str(source_snapshot.get("source_remote_name") or "").strip()
        original_head = str(source_snapshot.get("original_head") or "").strip()
        if not source_repo_value:
            raise RuntimeError("source Git repository path is unavailable")
        source_repo = Path(source_repo_value).expanduser()
        if not source_repo.is_dir():
            raise RuntimeError("source Git repository is no longer available")
        if not remote_name:
            raise RuntimeError("source Git repository has no configured push remote")
        if not original_head:
            raise RuntimeError("source Git HEAD is unavailable")
        if not _is_ancestor(repository, original_head, commit_sha):
            raise RuntimeError("verified commit does not preserve source Git ancestry")
        remote_url = _git(source_repo, "remote", "get-url", "--push", remote_name).strip()
        if not remote_url:
            raise RuntimeError("source remote has no push URL")
        if not is_github_pull_request_remote(remote_url):
            raise RuntimeError("source push target does not support GitHub pull requests")
        preflight = subprocess.run(
            ["gh", "repo", "view", "--json", "nameWithOwner"],
            cwd=source_repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if preflight.returncode != 0:
            raise RuntimeError(
                _command_failure_message("GitHub PR delivery preflight failed", preflight)
            )
        pushed = subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "push",
                remote_url,
                f"{commit_sha}:refs/heads/{branch_name}",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if pushed.returncode != 0:
            raise RuntimeError(
                _command_failure_message("delivery branch push failed", pushed)
            )
        existing = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch_name,
                "--base",
                base_branch,
                "--state",
                "open",
                "--json",
                "url",
                "--jq",
                ".[0].url",
            ],
            cwd=source_repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if existing.returncode == 0 and existing.stdout.strip():
            return existing.stdout.strip()
        created = subprocess.run(
            [
                "gh",
                "pr",
                "create",
                "--base",
                base_branch,
                "--head",
                branch_name,
                "--title",
                task_title or f"Bunshin delivery {branch_name}",
                "--body",
                (
                    "Implemented and system-verified by Pal Bunshin.\n\n"
                    f"Verified commit: `{commit_sha}`"
                ),
            ],
            cwd=source_repo,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if created.returncode != 0 or not created.stdout.strip():
            raise RuntimeError(
                _command_failure_message(
                    "delivery branch was pushed but PR creation failed",
                    created,
                )
            )
        return created.stdout.strip().splitlines()[-1].strip()

    def _publish_local_checkout(
        self,
        *,
        workflow_id: str,
        workflow_key: str,
        task_title: str,
        repository: Path,
        commit_sha: str,
    ) -> Path:
        component = _path_slug(task_title or workflow_key or "delivery")
        destination = (
            bunshin_data_root(self.runtime_root)
            / "deliveries"
            / f"{component}-{_path_slug(workflow_id)[:8]}"
        )
        if destination.exists():
            current = subprocess.run(
                ["git", "-C", str(destination), "rev-parse", "HEAD"],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
            )
            if current.returncode == 0 and current.stdout.strip() == commit_sha:
                return destination
            shutil.rmtree(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        cloned = subprocess.run(
            [
                "git",
                "clone",
                "--no-local",
                "--no-checkout",
                str(repository),
                str(destination),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if cloned.returncode != 0:
            raise RuntimeError(
                cloned.stderr or cloned.stdout or "failed to create local delivery repository"
            )
        checked_out = subprocess.run(
            ["git", "-C", str(destination), "checkout", "--detach", commit_sha],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if checked_out.returncode != 0:
            shutil.rmtree(destination, ignore_errors=True)
            raise RuntimeError(
                checked_out.stderr
                or checked_out.stdout
                or "failed to check out local delivery commit"
            )
        subprocess.run(
            ["git", "-C", str(destination), "remote", "remove", "origin"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return destination

    def _store(
        self,
        receipt: DeliveryReceipt,
        verification_ref: ArtifactRef,
    ) -> ArtifactRef:
        return self.artifacts.put_json(
            receipt.model_dump(mode="json"),
            artifact_type="DeliveryReceiptArtifact",
            provenance={"owner": "manager", "kind": receipt.kind},
            child_refs=((verification_ref.sha256, "sink_verification"),),
        )

def _is_ancestor(repository: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=repository,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return result.returncode == 0


def is_github_pull_request_remote(remote_url: str) -> bool:
    """Return whether a push URL identifies a github.com repository."""

    value = remote_url.strip()
    if re.match(
        r"^git@github\.com:[^/\s]+/[^/\s]+(?:\.git)?/?$",
        value,
        re.IGNORECASE,
    ):
        return True
    return bool(
        re.match(
            r"^(?:https?|ssh|git)://(?:[^/@\s]+@)?github\.com/[^/\s]+/[^/\s]+(?:\.git)?/?$",
            value,
            re.IGNORECASE,
        )
    )


def _command_failure_message(
    prefix: str,
    completed: subprocess.CompletedProcess[str],
) -> str:
    detail = (completed.stderr or completed.stdout or "").strip()
    detail = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", detail)
    detail = re.sub(r"(https?://)[^/@\s:]+:[^/@\s]+@", r"\1***@", detail)
    detail = re.sub(
        r"\b(?:github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9_]+)\b",
        "[REDACTED]",
        detail,
    )
    detail = " ".join(detail.split())[-700:]
    suffix = f" (exit {completed.returncode})"
    if detail:
        suffix += f": {detail}"
    return prefix + suffix


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout


def _ref_slug(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._/-]+", "-", value.strip())
    normalized = re.sub(r"/+", "/", normalized).strip("./-")
    return normalized or "workflow"


def _path_slug(value: str, *, max_length: int = 120) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
    normalized = normalized.strip(".-")
    if not normalized:
        return "workflow"
    # A task title is user-controlled and can be arbitrarily long.  This
    # component is used as one directory name, so keep it comfortably below
    # NAME_MAX even after the workflow-id suffix is appended by the caller.
    normalized = normalized[:max(1, int(max_length))].rstrip(".-")
    return normalized or "workflow"
