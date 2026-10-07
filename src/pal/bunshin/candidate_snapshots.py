from __future__ import annotations
from pal.bunshin.workspace_git import _git as _git
from pal.bunshin.workspace_git import _git_bytes as _git_bytes
from pal.bunshin.workspace_git import git_changed_paths as git_changed_paths
from pal.bunshin.workspace_resources import WorkspaceLockRegistry as WorkspaceLockRegistry
import fnmatch
import hashlib
import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import AggregateType
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.skeleton import compiled_module_write_scopes
from pal.bunshin.execution_values import workspace_content_fingerprint


@dataclass
class CandidateSnapshotService:
    repository: BunshinV2Repository
    artifacts: ContentAddressedArtifactStore
    worktree_locks: WorkspaceLockRegistry

    def create_candidate(
        self,
        *,
        node_run_id: str,
        worker_id: str,
        lease_resource_key: str,
        fencing_token: int,
        worktree: Path,
        expected_workspace_fingerprint: str,
        reference_only_paths: list[str],
        path_policy: Mapping[str, Any] | None = None,
        base_sha: str,
        candidate_baseline_sha: str,
        unit_contract_hash: str,
        dependency_output_hashes: Mapping[str, str],
        environment_fingerprint: str,
        repair_bill_ref: Mapping[str, Any] | None = None,
    ) -> tuple[ArtifactRef, str]:
        self.repository.leases.assert_fencing_token(lease_resource_key, worker_id, fencing_token)
        if not self.worktree_locks.is_held(node_run_id):
            raise RuntimeError("candidate snapshot requires the quiescer's exclusive worktree lock")
        try:
            node = self.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, node_run_id)
            if node is None:
                raise ValueError("candidate snapshot requires a durable DAG node run")
            expected_contract_hash = str(dict(node.payload.get("unit_contract_ref") or {}).get("sha256") or "")
            if not expected_contract_hash or unit_contract_hash != expected_contract_hash:
                raise ValueError("candidate unit contract hash does not match the DAG node contract")
            expected_environment = str(node.payload.get("environment_fingerprint") or "")
            if expected_environment and environment_fingerprint != expected_environment:
                raise ValueError("candidate environment fingerprint does not match the execution epoch")
            current_head = _git(worktree, "rev-parse", "HEAD").strip()
            before = workspace_content_fingerprint(worktree)
            if before != expected_workspace_fingerprint:
                raise RuntimeError("worktree changed after quiescing")
            if not candidate_baseline_sha:
                raise ValueError("candidate requires the accepted Architecture baseline")
            # A Module branch is a normal linear history.  Validate only the
            # current role's delta; prior Coder and Verifier commits are
            # already durable ancestors of ``base_sha``.
            changed_paths = git_changed_paths(worktree, base_sha)
            if path_policy:
                _validate_skeleton_candidate_paths(
                    changed_paths,
                    path_policy,
                )
            else:
                _validate_reference_only_paths(changed_paths, reference_only_paths)
            candidate_key = hashlib.sha256(
                json.dumps(
                    {
                        "node_run_id": node_run_id,
                        "assignment_base_sha": base_sha,
                        "previous_head_sha": base_sha,
                        "workspace_fingerprint": before,
                        "unit_contract_hash": unit_contract_hash,
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest()
            existing_sha = _find_candidate_commit(worktree, candidate_key)
            if current_head not in {base_sha, existing_sha}:
                raise ValueError(
                    "coder changed Git HEAD; commits, merges, rebases, checkouts, and resets are manager-owned operations"
                )
            if existing_sha:
                candidate_digest = existing_sha
            elif not changed_paths:
                # A role may legitimately prove that the accepted baseline
                # already satisfies the Module Protocol.  Record the handoff
                # against the current HEAD without manufacturing an empty
                # content commit.
                candidate_digest = base_sha
            else:
                _git(worktree, "add", "-A")
                message = (
                    f"bunshin module checkpoint {node_run_id}\n\n"
                    f"Pal-Assignment-Key: {candidate_key}"
                )
                tree_sha = _git(worktree, "write-tree").strip()
                candidate_digest = _git(
                    worktree,
                    "-c",
                    "user.name=Pal Bunshin",
                    "-c",
                    "user.email=bunshin@localhost",
                    "commit-tree",
                    tree_sha,
                    "-p",
                    base_sha,
                    "-m",
                    message,
                ).strip()
                _git(
                    worktree,
                    "update-ref",
                    f"refs/pal/checkpoints/{candidate_key}",
                    candidate_digest,
                )
                _git(worktree, "reset", "--hard", candidate_digest)
            after = workspace_content_fingerprint(worktree)
            if before != after:
                raise RuntimeError("worktree content changed while candidate commit was created")
            baseline_tree_sha = _git(worktree, "rev-parse", f"{base_sha}^{{tree}}").strip()
            candidate_tree_sha = _git(worktree, "rev-parse", f"{candidate_digest}^{{tree}}").strip()
            delta_patch = _git_bytes(worktree, "diff", "--binary", base_sha, candidate_digest, "--")
            candidate = {
                "schema_version": "3",
                "node_run_id": node_run_id,
                "candidate_digest": candidate_digest,
                "base_sha": base_sha,
                "architecture_base_sha": candidate_baseline_sha,
                "previous_head_sha": base_sha,
                "baseline_tree_sha": baseline_tree_sha,
                "candidate_tree_sha": candidate_tree_sha,
                "delta_patch_sha": hashlib.sha256(delta_patch).hexdigest(),
                "repair_bill_ref": dict(repair_bill_ref or {}),
                "unit_contract_hash": unit_contract_hash,
                "dependency_output_hashes": dict(dependency_output_hashes),
                "environment_fingerprint": environment_fingerprint,
                "workspace_fingerprint": before,
                "changed_paths": changed_paths,
                "candidate_key": candidate_key,
            }
            child_refs = ()
            if repair_bill_ref and repair_bill_ref.get("sha256"):
                child_refs = ((str(repair_bill_ref["sha256"]), "repair_bill"),)
            ref = self.artifacts.put_json(
                candidate,
                artifact_type="GitCheckpointArtifact",
                child_refs=child_refs,
            )
            return ref, candidate_digest
        finally:
            self.worktree_locks.release(node_run_id)


def _validate_reference_only_paths(changed_paths: list[str], reference_only_paths: list[str]) -> None:
    reference_violations = [path for path in changed_paths if _matches_any(path, reference_only_paths)]
    if reference_violations:
        raise ValueError(f"candidate modified reference-only paths: {reference_violations}")


def _validate_skeleton_candidate_paths(
    changed_paths: list[str],
    policy: Mapping[str, Any],
) -> None:
    contract_mode = str(policy.get("contract_mode") or "file_frozen")
    if contract_mode not in {"file_frozen", "review_guarded"}:
        raise ValueError(f"unknown contract enforcement mode: {contract_mode}")
    frozen = {str(item).replace(os.sep, "/") for item in list(policy.get("contract_paths") or [])}
    references = {str(item).replace(os.sep, "/") for item in list(policy.get("reference_only") or [])}
    writable = list(compiled_module_write_scopes(policy))
    frozen_violations = sorted(
        path
        for path in changed_paths
        if contract_mode == "file_frozen" and path.replace(os.sep, "/") in frozen
    )
    if frozen_violations:
        raise ValueError("candidate modified frozen architecture contracts: " + ", ".join(frozen_violations))
    reference_violations = sorted(path for path in changed_paths if path.replace(os.sep, "/") in references)
    if reference_violations:
        raise ValueError("candidate modified reference-only paths: " + ", ".join(reference_violations))
    outside = sorted(
        path
        for path in changed_paths
        if not any(_path_scope_matches(path, scope) for scope in writable)
    )
    if outside:
        raise ValueError("candidate changed paths outside its compiled module write scopes: " + ", ".join(outside))


def _path_scope_matches(path: str, scope: Mapping[str, Any]) -> bool:
    normalized = str(path).replace(os.sep, "/").strip("/")
    target = str(scope.get("path") or "").replace(os.sep, "/").strip("/")
    kind = str(scope.get("kind") or "")
    if not target:
        return False
    if kind == "file":
        return normalized == target
    if kind == "directory":
        return normalized == target or normalized.startswith(target + "/")
    return False


def _matches_any(path: str, patterns: list[str]) -> bool:
    normalized = path.replace(os.sep, "/")
    for pattern in patterns:
        candidate = str(pattern).replace(os.sep, "/")
        if candidate.endswith("/**") and normalized.startswith(candidate[:-3].rstrip("/") + "/"):
            return True
        if fnmatch.fnmatchcase(normalized, candidate):
            return True
    return False


def _find_candidate_commit(worktree: Path, candidate_key: str) -> str:
    try:
        # Checkpoint idempotency is branch-local. A matching commit reachable
        # only from another Module branch does not settle this assignment.
        output = _git(
            worktree,
            "log",
            "HEAD",
            "--format=%H%x00%B%x00",
            "--grep",
            f"Pal-Assignment-Key: {candidate_key}",
            "-n",
            "1",
        )
    except subprocess.CalledProcessError:
        return ""
    parts = output.split("\x00")
    return parts[0].strip() if parts and candidate_key in output else ""
