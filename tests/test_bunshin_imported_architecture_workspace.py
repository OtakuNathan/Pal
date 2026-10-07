"""Imported contracts use durable Git evidence and new-workflow ownership."""
from __future__ import annotations

import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.paths import bunshin_data_root, cleanup_workflow_worktrees
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.skeleton import (
    ARCHITECTURE_SKELETON_BUNDLE_ARTIFACT,
    GitBackedSkeletonService,
)


def _git(path: Path, *args: str) -> str:
    return subprocess.check_output(
        ["git", "-C", str(path), *args], text=True, stderr=subprocess.PIPE
    ).strip()


class ImportedArchitectureWorkspaceTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="pal-imported-architecture-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repository = BunshinRepository(self.root)
        self.artifacts = ContentAddressedArtifactStore(self.root, self.repository.artifacts)
        self.skeleton = GitBackedSkeletonService(self.root, self.artifacts)
        self.requirements_ref = self.artifacts.put_json({}, artifact_type="TaskLedgerArtifact")
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "contract.txt").write_text("Original contract\n", encoding="utf-8")
        self.workspace = {"kind": "existing_repo", "repo_path": str(self.source), "project_name": "project"}
        self.original = self.skeleton.provision_architecture_workspace(
            workflow_id="wf_source", workflow_name="Source workflow", revision_name="revision-1",
            workspace=self.workspace, requirements_ref=self.requirements_ref,
        )
        (self.original.worktree / "contract.txt").write_text("Frozen contract\n", encoding="utf-8")
        _git(self.original.worktree, "add", "contract.txt")
        _git(self.original.worktree, "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
             "commit", "-qm", "Freeze contract")
        bundle = self.root / "source.bundle"
        _git(self.original.worktree, "bundle", "create", str(bundle), self.original.architecture_branch)
        self.bundle_ref = self.artifacts.put_bytes(
            bundle.read_bytes(), artifact_type=ARCHITECTURE_SKELETON_BUNDLE_ARTIFACT,
            media_type="application/x-git-bundle",
        )
        self.artifact = {
            "graph_ir": {"graph_id": "wf_source"},
            "git_bundle_ref": self.bundle_ref.to_dict(),
            "base_commit_sha": self.original.base_sha,
            "base_tree_sha": self.original.base_tree_sha,
            "skeleton_commit_sha": _git(self.original.worktree, "rev-parse", "HEAD"),
            "skeleton_tree_sha": _git(self.original.worktree, "rev-parse", "HEAD^{tree}"),
            "repository_layout": {
                "project_name": self.original.project_name,
                "project_key": self.original.project_key,
                "workflow_name": self.original.workflow_name,
                "workflow_key": self.original.workflow_key,
                "workflow_branch": self.original.workflow_branch,
            },
        }
        self.artifact_ref = self.artifacts.put_json(self.artifact, artifact_type="ContractArtifact")
        self.original_artifact_bytes = self.artifacts.read_bytes(self.artifact_ref)

    def _retire_source(self) -> None:
        cleanup_workflow_worktrees(self.root, repository_layout=self.artifact["repository_layout"])
        self.assertFalse(self.original.worktree.exists())

    def _review(self, **kwargs):
        review = self.skeleton.provision_review_worktree(
            artifact=kwargs.pop("artifact", self.artifact),
            review_name=kwargs.pop("review_name", "imported-review"),
            workflow_id=kwargs.pop("workflow_id", "wf_restart"), **kwargs,
        )
        self.addCleanup(review.cleanup)
        return review

    def _assert_no_review_scratch(self) -> None:
        root = bunshin_data_root(self.root) / "runtime" / "architecture-review"
        self.assertEqual(list(root.iterdir()) if root.exists() else [], [])

    def test_imported_review_survives_source_cleanup_and_missing_git_repository(self) -> None:
        self._retire_source()
        shutil.rmtree(self.original.common_git_dir)
        before = copy.deepcopy(self.artifact)

        review = self._review()

        self.assertTrue(review.owns_worktree)
        self.assertNotEqual(review.common_git_dir, self.original.common_git_dir)
        self.assertEqual(_git(review.worktree, "rev-parse", "HEAD"), self.artifact["skeleton_commit_sha"])
        self.assertEqual(_git(review.worktree, "rev-parse", "HEAD^{tree}"), self.artifact["skeleton_tree_sha"])
        self.assertEqual((review.worktree / "contract.txt").read_text(), "Frozen contract\n")
        self.assertIn("+Frozen contract", _git(
            review.worktree, "diff", self.artifact["base_commit_sha"], self.artifact["skeleton_commit_sha"]
        ))
        self.assertEqual(_git(review.worktree, "rev-parse", "--abbrev-ref", "HEAD"), "HEAD")
        self.assertFalse(self.original.worktree.exists())
        self.assertFalse(self.original.common_git_dir.exists())
        self.assertEqual(self.artifact, before)
        self.assertEqual(self.artifacts.read_bytes(self.artifact_ref), self.original_artifact_bytes)
        review.cleanup()
        self.assertFalse(review.root.exists())

    def test_imported_reviews_are_unique_isolated_and_cleanup_only_their_own_root(self) -> None:
        old_refs = _git(self.original.common_git_dir, "show-ref")
        first = self._review(review_name="../../same/name")
        second = self._review(review_name="../../same/name")
        self.assertNotEqual(first.root, second.root)
        self.assertEqual(first.root.parent, bunshin_data_root(self.root) / "runtime" / "architecture-review")
        (first.worktree / "contract.txt").write_text("Reviewer scratch\n")
        first.cleanup()
        self.assertFalse(first.root.exists())
        self.assertTrue(second.worktree.exists())
        self.assertEqual((second.worktree / "contract.txt").read_text(), "Frozen contract\n")
        self.assertEqual((self.original.worktree / "contract.txt").read_text(), "Frozen contract\n")
        self.assertEqual(_git(self.original.common_git_dir, "show-ref"), old_refs)

    def test_same_workflow_review_preserves_canonical_worktree(self) -> None:
        review = self._review(workflow_id="wf_source")
        self.assertEqual(review.worktree, self.original.worktree)
        self.assertFalse(review.owns_worktree)
        review.cleanup()
        self.assertTrue(self.original.worktree.exists())

    def test_same_workflow_missing_worktree_cannot_fall_back_to_bundle(self) -> None:
        self._retire_source()
        for workflow_id in ("wf_source", ""):
            with self.subTest(workflow_id=workflow_id), self.assertRaisesRegex(
                RuntimeError, "requires the canonical Architecture worktree"
            ):
                self._review(workflow_id=workflow_id)
        artifact_without_origin = {**self.artifact, "graph_ir": {}}
        with self.assertRaisesRegex(RuntimeError, "requires the canonical Architecture worktree"):
            self._review(artifact=artifact_without_origin)

    def test_same_workflow_wrong_head_cannot_fall_back_to_bundle(self) -> None:
        _git(self.original.worktree, "reset", "--hard", self.artifact["base_commit_sha"])
        with self.assertRaisesRegex(RuntimeError, "not bound to the reviewed commit"):
            self._review(workflow_id="wf_source")

    def test_imported_review_rejects_non_durable_or_wrong_type_bundle(self) -> None:
        invalid_refs = [
            {**self.bundle_ref.to_dict(), "durable": False},
            {**self.bundle_ref.to_dict(), "artifact_type": "OtherArtifact"},
            {**self.bundle_ref.to_dict(), "byte_size": self.bundle_ref.byte_size + 1},
            {**self.bundle_ref.to_dict(), "sha256": "0" * 64},
        ]
        for ref in invalid_refs:
            with self.subTest(ref=ref), self.assertRaisesRegex(ValueError, "durable architecture bundle"):
                self._review(artifact={**self.artifact, "git_bundle_ref": ref})
        self._assert_no_review_scratch()

    def test_imported_review_rejects_corrupted_durable_bundle(self) -> None:
        record = self.repository.artifacts.read_artifact_record(self.bundle_ref.sha256)
        Path(record["storage_path"]).write_bytes(b"corrupted")
        with self.assertRaisesRegex(IOError, "digest verification"):
            self._review()
        self._assert_no_review_scratch()

    def test_imported_review_rejects_missing_durable_bundle_bytes(self) -> None:
        record = self.repository.artifacts.read_artifact_record(self.bundle_ref.sha256)
        Path(record["storage_path"]).unlink()
        with self.assertRaises(FileNotFoundError):
            self._review()
        self._assert_no_review_scratch()

    def test_imported_review_cleans_up_if_artifact_bytes_are_not_a_git_bundle(self) -> None:
        invalid = self.artifacts.put_bytes(
            b"not a Git bundle", artifact_type=ARCHITECTURE_SKELETON_BUNDLE_ARTIFACT,
            media_type="application/x-git-bundle",
        )
        with self.assertRaises(RuntimeError):
            self._review(artifact={**self.artifact, "git_bundle_ref": invalid.to_dict()})
        self._assert_no_review_scratch()

    def test_imported_review_rejects_missing_or_inexact_commit_and_tree_ids(self) -> None:
        for key in ("base_commit_sha", "base_tree_sha", "skeleton_commit_sha", "skeleton_tree_sha"):
            for value in ("", "HEAD", "-a", self.artifact[key][:12]):
                with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, "exact base"):
                    self._review(artifact={**self.artifact, key: value})
        self._assert_no_review_scratch()

    def test_imported_review_cleans_up_on_commit_or_tree_mismatch(self) -> None:
        for key, value in (
            ("base_commit_sha", "0" * 40),
            ("skeleton_commit_sha", "0" * 40),
            ("base_tree_sha", self.artifact["skeleton_tree_sha"]),
            ("skeleton_tree_sha", self.artifact["base_tree_sha"]),
            ("skeleton_commit_sha", self.artifact["skeleton_tree_sha"]),
        ):
            with self.subTest(key=key), self.assertRaises((ValueError, RuntimeError)):
                self._review(artifact={**self.artifact, key: value})
            self._assert_no_review_scratch()

    def test_imported_review_cleans_up_after_checkout_failure(self) -> None:
        from pal.bunshin import skeleton
        run_git = skeleton._git_dir

        def fail_checkout(git_dir, *args):
            if args[:2] == ("worktree", "add"):
                raise RuntimeError("injected checkout failure")
            return run_git(git_dir, *args)

        with patch.object(skeleton, "_git_dir", fail_checkout), self.assertRaisesRegex(RuntimeError, "injected"):
            self._review()
        self._assert_no_review_scratch()
        self.assertTrue(self.original.worktree.exists())

    def test_imported_review_requires_base_commit_to_be_skeleton_ancestor(self) -> None:
        reversed_history = {
            **self.artifact,
            "base_commit_sha": self.artifact["skeleton_commit_sha"],
            "base_tree_sha": self.artifact["skeleton_tree_sha"],
            "skeleton_commit_sha": self.artifact["base_commit_sha"],
            "skeleton_tree_sha": self.artifact["base_tree_sha"],
        }
        with self.assertRaises(RuntimeError):
            self._review(artifact=reversed_history)
        self._assert_no_review_scratch()

    def test_imported_repair_uses_new_workflow_layout_and_exact_source_skeleton(self) -> None:
        self._retire_source()
        old_refs = _git(self.original.common_git_dir, "show-ref")
        before = copy.deepcopy(self.artifact)
        options = dict(
            workflow_id="wf_restart", workflow_name="Restart workflow", revision_name="revision-2",
            workspace=self.workspace, requirements_ref=self.requirements_ref, base_artifact=self.artifact,
        )

        repair = self.skeleton.provision_architecture_workspace(**options)

        self.assertEqual(repair.project_key, self.original.project_key)
        self.assertEqual(repair.project_name, self.original.project_name)
        self.assertEqual(repair.common_git_dir, self.original.common_git_dir)
        self.assertEqual(repair.workflow_name, "Restart workflow")
        self.assertNotEqual(repair.workflow_key, self.original.workflow_key)
        self.assertNotEqual(repair.workflow_branch, self.original.workflow_branch)
        self.assertNotEqual(repair.architecture_branch, self.original.architecture_branch)
        self.assertNotEqual(repair.worktree, self.original.worktree)
        self.assertEqual(repair.base_sha, self.artifact["skeleton_commit_sha"])
        self.assertEqual(repair.base_tree_sha, self.artifact["skeleton_tree_sha"])
        self.assertEqual(_git(repair.worktree, "rev-parse", "HEAD"), self.artifact["skeleton_commit_sha"])
        self.assertFalse(self.original.worktree.exists())
        for line in old_refs.splitlines():
            self.assertIn(line, _git(self.original.common_git_dir, "show-ref").splitlines())
        self.assertEqual(self.artifact, before)
        self.assertEqual(self.artifacts.read_bytes(self.artifact_ref), self.original_artifact_bytes)
        self.assertEqual(self.skeleton.provision_architecture_workspace(**options), repair)

    def test_same_workflow_repair_reuses_its_existing_layout(self) -> None:
        repair = self.skeleton.provision_architecture_workspace(
            workflow_id="wf_source", workflow_name="New display name", revision_name="revision-2",
            workspace=self.workspace, requirements_ref=self.requirements_ref, base_artifact=self.artifact,
        )
        self.assertEqual(repair.worktree, self.original.worktree)
        self.assertEqual(repair.workflow_key, self.original.workflow_key)
        self.assertEqual(repair.workflow_branch, self.original.workflow_branch)
        self.assertEqual(repair.base_sha, self.artifact["skeleton_commit_sha"])
