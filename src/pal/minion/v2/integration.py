from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore


class IntegrationOwnershipDefect(RuntimeError):
    pass


class CandidateUnionConflict(RuntimeError):
    pass


@dataclass
class CandidateUnionService:
    artifacts: ContentAddressedArtifactStore

    def compose(
        self,
        *,
        publish_worktree: Path,
        ordered_candidates: Sequence[Mapping[str, Any]],
        architecture_skeleton_ref: Mapping[str, Any],
    ) -> tuple[ArtifactRef, str]:
        applied: list[dict[str, str]] = []
        seen_modules: set[str] = set()
        for candidate in ordered_candidates:
            module_name = str(candidate.get("module_name") or "")
            candidate_digest = str(candidate.get("candidate_digest") or "")
            if not module_name or not candidate_digest:
                raise ValueError("candidate union requires module_name and candidate_digest")
            if module_name in seen_modules:
                raise ValueError(f"candidate union contains module more than once: {module_name}")
            try:
                _git(publish_worktree, "cherry-pick", candidate_digest)
            except subprocess.CalledProcessError as exc:
                _git_no_check(publish_worktree, "cherry-pick", "--abort")
                raise CandidateUnionConflict(
                    f"module {module_name} conflicted during deterministic candidate union"
                ) from exc
            seen_modules.add(module_name)
            applied.append({"module_name": module_name, "candidate_digest": candidate_digest})
        commit_sha = _git(publish_worktree, "rev-parse", "HEAD").strip()
        ref = self.artifacts.put_json(
            {
                "schema_version": "1",
                "architecture_skeleton_ref": dict(architecture_skeleton_ref),
                "commit_sha": commit_sha,
                "applied_module_candidates": applied,
            },
            artifact_type="CandidateUnionArtifact",
            child_refs=tuple(
                (str(candidate["candidate_ref"]["sha256"]), "module_candidate")
                for candidate in ordered_candidates
                if dict(candidate.get("candidate_ref") or {}).get("sha256")
            ),
        )
        return ref, commit_sha

    def publish(
        self,
        *,
        repository: Path,
        union_ref: ArtifactRef,
        commit_sha: str,
        branch_name: str,
        verification_refs: Sequence[Mapping[str, Any]],
        scenario_fingerprints: Mapping[str, str],
    ) -> ArtifactRef:
        if not branch_name.strip() or branch_name.startswith("-"):
            raise ValueError("invalid final branch name")
        if not verification_refs or set(scenario_fingerprints) == set():
            raise ValueError("final publish requires accepted scenario verification evidence")
        _git(repository, "branch", "-f", branch_name, commit_sha)
        if _git(repository, "rev-parse", branch_name).strip() != commit_sha:
            raise RuntimeError("published branch does not resolve to the deterministic candidate union")
        children = [(union_ref.sha256, "candidate_union")]
        children.extend(
            (str(ref["sha256"]), "scenario_verification")
            for ref in verification_refs
            if ref.get("sha256")
        )
        return self.artifacts.put_json(
            {
                "schema_version": "2",
                "branch_name": branch_name,
                "commit_sha": commit_sha,
                "candidate_union_ref": union_ref.to_dict(),
                "verification_refs": [dict(item) for item in verification_refs],
                "scenario_fingerprints": dict(scenario_fingerprints),
            },
            artifact_type="PublishedBranchArtifact",
            child_refs=tuple(children),
        )


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
