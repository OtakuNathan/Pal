from __future__ import annotations
from pal.bunshin.semantic_orchestration.workspace_safety import _git_output
import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.adapters import SOFTWARE_GIT_ADAPTER
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import AggregateSnapshot, SubmissionInvariantError
from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts


@dataclass
class VerifierTests:
    workflow_facts: WorkflowFacts
    artifacts: ContentAddressedArtifactStore

    def install_verifier_tests_for_repair(
        self,
        node: AggregateSnapshot,
    ) -> dict[str, Any]:
        # Verifier tests are already ordinary commits on the shared Module
        # branch.  A repair resumes from that HEAD and has nothing to install.
        return {}

    def checkpoint_verifier_tests(
        self,
        *,
        node: AggregateSnapshot,
        review_workspace: Path,
        candidate_ref: ArtifactRef,
        candidate: Mapping[str, Any],
        candidate_digest: str,
        changed_test_paths: list[str],
    ) -> tuple[ArtifactRef, str, dict[str, Any]]:
        if self.workflow_facts.execution_adapter(node) != SOFTWARE_GIT_ADAPTER:
            raise SubmissionInvariantError(
                "verifier checkpoint currently requires the software Git adapter"
            )
        if not changed_test_paths:
            raise SubmissionInvariantError("verifier checkpoint has no changed tests")
        node_workspace = Path(str(node.payload.get("workspace_path") or ""))
        if (
            not node_workspace.is_dir()
            or node_workspace.resolve() != review_workspace.resolve()
        ):
            raise SubmissionInvariantError(
                "Verifier must checkpoint tests in the canonical Module worktree"
            )
        subprocess.run(
            ["git", "-C", str(review_workspace), "add", "-A", "--"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        tree = subprocess.run(
            ["git", "-C", str(review_workspace), "write-tree"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        checkpoint_key = hashlib.sha256(
            f"verifier-checkpoint-v1:{node.aggregate_id}:{candidate_digest}:{tree}".encode(
                "utf-8"
            )
        ).hexdigest()
        existing = subprocess.run(
            [
                "git",
                "-C",
                str(review_workspace),
                "log",
                "--all",
                "--fixed-strings",
                f"--grep=Pal-Assignment-Key: {checkpoint_key}",
                "--format=%H",
                "-n",
                "1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        head = _git_output(review_workspace, "rev-parse", "HEAD")
        if existing:
            existing_parent = _git_output(review_workspace, "rev-parse", f"{existing}^")
            existing_tree = _git_output(review_workspace, "rev-parse", f"{existing}^{{tree}}")
            if existing_parent != candidate_digest or existing_tree != tree:
                raise SubmissionInvariantError(
                    "recovered verifier checkpoint does not match its reviewed parent"
                )
            checkpoint_digest = existing
        else:
            if head != candidate_digest:
                raise SubmissionInvariantError(
                    "Module worktree moved away from the reviewed Coder commit"
                )
            checkpoint_digest = subprocess.run(
                [
                    "git",
                    "-C",
                    str(review_workspace),
                    "-c",
                    "user.name=Pal Bunshin Verifier",
                    "-c",
                    "user.email=bunshin-verifier@localhost",
                    "commit-tree",
                    tree,
                    "-p",
                    candidate_digest,
                    "-m",
                    (
                        f"bunshin verifier checkpoint {node.aggregate_id}\n\n"
                        f"Pal-Assignment-Key: {checkpoint_key}"
                    ),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(review_workspace), "reset", "--hard", checkpoint_digest],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        delta_patch = subprocess.run(
            [
                "git",
                "-C",
                str(review_workspace),
                "diff",
                "--binary",
                candidate_digest,
                checkpoint_digest,
                "--",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout
        checkpoint = {
            **dict(candidate),
            "candidate_digest": checkpoint_digest,
            "previous_head_sha": candidate_digest,
            "base_sha": candidate_digest,
            "candidate_tree_sha": tree,
            "delta_patch_sha": hashlib.sha256(delta_patch).hexdigest(),
            "changed_paths": sorted(
                set(str(item) for item in list(candidate.get("changed_paths") or []))
                | set(changed_test_paths)
            ),
            "verifier_test_paths": list(changed_test_paths),
            "candidate_key": checkpoint_key,
        }
        checkpoint_ref = self.artifacts.put_json(
            checkpoint,
            artifact_type="GitCheckpointArtifact",
            provenance={"owner": "manager", "role": "verifier"},
            child_refs=(
                (candidate_ref.sha256, "previous_checkpoint"),
            ),
        )
        return checkpoint_ref, checkpoint_digest, checkpoint
