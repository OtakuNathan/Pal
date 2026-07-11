from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore


class IntegrationOwnershipDefect(RuntimeError):
    pass


@dataclass
class IntegrationService:
    artifacts: ContentAddressedArtifactStore

    def integrate_candidates(
        self,
        *,
        integration_worktree: Path,
        ordered_candidates: Sequence[Mapping[str, Any]],
        architecture_manifest_sha: str,
    ) -> tuple[ArtifactRef, str]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for candidate in ordered_candidates:
            candidate_digest = str(candidate.get("candidate_digest") or "")
            node_run_id = str(candidate.get("node_run_id") or "")
            if not candidate_digest or not node_run_id:
                raise ValueError("integration candidate requires node_run_id and candidate_digest")
            if candidate_digest in seen or _is_ancestor(integration_worktree, candidate_digest, "HEAD"):
                continue
            try:
                _git(integration_worktree, "cherry-pick", candidate_digest)
            except subprocess.CalledProcessError as exc:
                _git_no_check(integration_worktree, "cherry-pick", "--abort")
                raise IntegrationOwnershipDefect(
                    f"candidate {node_run_id} conflicted during deterministic integration"
                ) from exc
            seen.add(candidate_digest)
            merged.append({"node_run_id": node_run_id, "candidate_digest": candidate_digest})
        integration_sha = _git(integration_worktree, "rev-parse", "HEAD").strip()
        payload = {
            "schema_version": "1",
            "architecture_manifest_sha": architecture_manifest_sha,
            "integration_candidate_digest": integration_sha,
            "merged_candidates": merged,
        }
        ref = self.artifacts.put_json(payload, artifact_type="IntegrationCandidateArtifact")
        return ref, integration_sha

    def publish_final_deliverable(
        self,
        *,
        repository: Path,
        integration_candidate_digest: str,
        branch_name: str,
        verification_ref: ArtifactRef,
    ) -> ArtifactRef:
        if not branch_name.strip() or branch_name.startswith("-"):
            raise ValueError("invalid final branch name")
        _git(repository, "branch", "-f", branch_name, integration_candidate_digest)
        resolved_sha = _git(repository, "rev-parse", branch_name).strip()
        if resolved_sha != integration_candidate_digest:
            raise RuntimeError("published branch does not resolve to accepted integration candidate")
        return self.artifacts.put_json(
            {
                "schema_version": "1",
                "branch_name": branch_name,
                "commit_sha": resolved_sha,
                "verification_ref": verification_ref.to_dict(),
            },
            artifact_type="PublishedBranchArtifact",
            child_refs=((verification_ref.sha256, "verification"),),
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


def _git(repository: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repository,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout


def _git_no_check(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repository,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
