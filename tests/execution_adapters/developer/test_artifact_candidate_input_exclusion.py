"""Developer tests for the artifact-bundle bound-input exclusion.

These tests exercise ``ArtifactBundleAdapter`` bundle enumeration against
the ``input_binding`` exclusion boundary: files under the bound-inputs
root (first path component ``inputs``) never enter candidate snapshots,
tree fingerprints, verification workspaces, or deliverables, while
worker-authored paths that merely contain an ``inputs`` component
elsewhere always remain bundle content.

``input_binding.is_bound_input_path`` is a declaration skeleton in this
isolated worktree, so these tests substitute its documented contract
(first-path-component match on ``BOUND_INPUTS_ROOT``) at the adapter's
import site.  The production path still delegates every containment
decision to the authoritative predicate; composition with the real
implementation is verifier work.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pal.bunshin.v2 import adapters, input_binding
from pal.bunshin.v2.adapters import (
    ArtifactBundleAdapter,
    artifact_tree_fingerprint,
    provision_artifact_workspaces,
)
from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.input_binding import BOUND_INPUTS_ROOT
from pal.bunshin.v2.repository import BunshinV2Repository

WORKER_FILES = {
    "report.md",
    "src/main.py",
    "src/inputs/data.py",
    "docs/inputs/notes.md",
    "pkg/inputs/keep.txt",
}


def _documented_is_bound_input_path(relative_path: object) -> bool:
    """Documented contract of ``input_binding.is_bound_input_path``.

    True iff the first component of the workspace-relative POSIX path is
    exactly the bound-inputs root; pure and total.
    """
    text = str(relative_path)
    return bool(text) and text.split("/", 1)[0] == BOUND_INPUTS_ROOT


class ArtifactCandidateInputExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_v2_input_exclusion_"))
        self.repository = BunshinV2Repository(self.root)
        self.store = ContentAddressedArtifactStore(self.root, self.repository)
        self.adapter = ArtifactBundleAdapter(self.root, self.store)
        # Capture the authoritative objects the adapter module bound at
        # import time before any test substitutes the predicate.
        self.authoritative_predicate = adapters.is_bound_input_path
        patcher = mock.patch.object(
            adapters, "is_bound_input_path", _documented_is_bound_input_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _write_workspace(self, workspace: Path, *, with_bound_inputs: bool) -> None:
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "report.md").write_text("# report\n", encoding="utf-8")
        (workspace / "src").mkdir()
        (workspace / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (workspace / "src" / "inputs").mkdir()
        (workspace / "src" / "inputs" / "data.py").write_text("DATA = 1\n", encoding="utf-8")
        (workspace / "docs" / "inputs").mkdir(parents=True)
        (workspace / "docs" / "inputs" / "notes.md").write_text("notes\n", encoding="utf-8")
        (workspace / "pkg" / "inputs").mkdir(parents=True)
        (workspace / "pkg" / "inputs" / "keep.txt").write_text("keep\n", encoding="utf-8")
        if with_bound_inputs:
            (workspace / "inputs" / "specs" / "d1").mkdir(parents=True)
            (workspace / "inputs" / "specs" / "d1" / "requirements.md").write_text("req\n", encoding="utf-8")
            (workspace / "inputs" / "notes.txt").write_text("bound\n", encoding="utf-8")

    def _snapshot(self, workspace: Path):
        return self.adapter.snapshot_candidate(
            workspace=workspace,
            reference_only_paths=[],
            unit_contract_hash="contract",
            dependency_output_hashes={},
            environment_fingerprint="test",
        )

    def test_snapshot_excludes_bound_inputs_and_keeps_worker_paths(self) -> None:
        with_inputs = self.root / "with-inputs"
        without_inputs = self.root / "without-inputs"
        self._write_workspace(with_inputs, with_bound_inputs=True)
        self._write_workspace(without_inputs, with_bound_inputs=False)

        ref, _ = self._snapshot(with_inputs)
        payload = self.store.read_json(ref)
        self.assertEqual({item["path"] for item in payload["files"]}, WORKER_FILES)
        # The tree fingerprint equals the same workspace without
        # materialized bound inputs.
        self.assertEqual(payload["tree_fingerprint"], artifact_tree_fingerprint(without_inputs))
        self.assertEqual(artifact_tree_fingerprint(with_inputs), artifact_tree_fingerprint(without_inputs))

    def test_base_digest_stays_stable_across_input_materialization(self) -> None:
        workspaces = provision_artifact_workspaces(self.root, epoch_id="epoch-1", unit_ids=["unit"])
        workspace = Path(workspaces["unit"]["workspace_path"])
        base = workspaces["unit"]["base_digest"]
        # Manager-side materialization of bound inputs never moves the tree.
        (workspace / "inputs" / "specs").mkdir(parents=True)
        (workspace / "inputs" / "specs" / "requirements.md").write_text("req\n", encoding="utf-8")
        self.assertEqual(artifact_tree_fingerprint(workspace), base)

    def test_deliverable_and_verification_workspace_exclude_bound_inputs(self) -> None:
        workspace = self.root / "workspace"
        self._write_workspace(workspace, with_bound_inputs=True)
        ref, _ = self._snapshot(workspace)

        verification_ref = self.store.put_json({"status": "PASS"}, artifact_type="VerificationArtifact")
        published = self.adapter.publish_deliverable(
            workflow_id="workflow-1",
            candidate_ref=ref.to_dict(),
            verification_ref=verification_ref,
        )
        published_payload = self.store.read_json(published)
        self.assertEqual({item["path"] for item in published_payload["files"]}, WORKER_FILES)
        destination = Path(published_payload["destination"])
        self.assertFalse((destination / "inputs").exists())
        self.assertTrue((destination / "src" / "inputs" / "data.py").is_file())

        candidate_dir, _scratch = self.adapter.prepare_verification_workspace(
            review_id="review-1", candidate_ref=ref.to_dict()
        )
        self.assertFalse((candidate_dir / "inputs").exists())
        self.assertTrue((candidate_dir / "docs" / "inputs" / "notes.md").is_file())

    def test_enumeration_delegates_every_path_to_the_authoritative_predicate(self) -> None:
        workspace = self.root / "workspace"
        self._write_workspace(workspace, with_bound_inputs=True)
        with mock.patch.object(
            adapters, "is_bound_input_path", wraps=_documented_is_bound_input_path
        ) as predicate:
            self._snapshot(workspace)
        seen = {call.args[0] for call in predicate.call_args_list}
        # Worker paths are decided by the predicate too, including paths
        # that merely contain an "inputs" component elsewhere.
        self.assertTrue(seen & WORKER_FILES)
        self.assertIn("src/inputs/data.py", seen)
        self.assertIn("inputs/notes.txt", seen)

    def test_workspace_with_only_bound_inputs_keeps_existing_empty_candidate_error(self) -> None:
        workspace = self.root / "only-inputs"
        workspace.mkdir()
        (workspace / "inputs").mkdir()
        (workspace / "inputs" / "notes.txt").write_text("bound\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "contains no files"):
            self._snapshot(workspace)

    def test_adapter_binds_the_authoritative_boundary_objects(self) -> None:
        self.assertIs(adapters.BOUND_INPUTS_ROOT, input_binding.BOUND_INPUTS_ROOT)
        self.assertIs(self.authoritative_predicate, input_binding.is_bound_input_path)


if __name__ == "__main__":
    unittest.main()
