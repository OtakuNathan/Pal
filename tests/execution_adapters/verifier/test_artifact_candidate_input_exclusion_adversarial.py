"""Verifier cases for the artifact-bundle bound-input exclusion.

Adversarial composition checks derived from the module contract
(``artifact_candidate_input_exclusion``) and the ``input_binding`` edge
declaration.  ``input_binding.is_bound_input_path`` is a declaration
skeleton in this isolated worktree, so these tests substitute its
documented contract (first-path-component match on
``BOUND_INPUTS_ROOT``) at the adapter's import site — exactly the
composition boundary the adapter must honor — and additionally prove
the adapter applies the predicate's decisions verbatim instead of
re-implementing containment with a component-membership test.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pal.bunshin.v2 import adapters
from pal.bunshin.v2.adapters import (
    ArtifactBundleAdapter,
    artifact_tree_fingerprint,
)
from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.input_binding import BOUND_INPUTS_ROOT
from pal.bunshin.v2.repository import BunshinV2Repository


def _documented_is_bound_input_path(relative_path: object) -> bool:
    """Documented contract of ``input_binding.is_bound_input_path``.

    True iff the first component of the workspace-relative POSIX path is
    exactly the bound-inputs root; pure and total.
    """
    text = str(relative_path)
    return bool(text) and text.split("/", 1)[0] == BOUND_INPUTS_ROOT


def _legacy_tree_fingerprint(workspace: Path) -> str:
    """The pre-change tree fingerprint algorithm, verbatim.

    Before this Candidate, ``_tree_fingerprint`` digested every regular
    file except ``.pal-candidate`` staging trees, with no bound-input
    exclusion.  Workspaces without an ``inputs`` root must fingerprint
    identically under the new algorithm.
    """
    digest = hashlib.sha256()
    if not workspace.exists():
        return digest.hexdigest()
    for path in sorted(
        item for item in workspace.rglob("*") if item.is_file() and ".pal-candidate" not in item.parts
    ):
        digest.update(path.relative_to(workspace).as_posix().encode("utf-8"))
        digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii"))
    return digest.hexdigest()


class ArtifactCandidateInputExclusionAdversarialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_v2_input_exclusion_verifier_"))
        self.repository = BunshinV2Repository(self.root)
        self.store = ContentAddressedArtifactStore(self.root, self.repository)
        self.adapter = ArtifactBundleAdapter(self.root, self.store)
        patcher = mock.patch.object(
            adapters, "is_bound_input_path", _documented_is_bound_input_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _snapshot(self, workspace: Path):
        return self.adapter.snapshot_candidate(
            workspace=workspace,
            reference_only_paths=[],
            unit_contract_hash="contract",
            dependency_output_hashes={},
            environment_fingerprint="test",
        )

    def test_staging_and_deep_bound_inputs_excluded_everywhere(self) -> None:
        """``.pal-candidate`` staging and deeply nested bound inputs stay
        out of the candidate, fingerprint, verification workspace, and
        deliverable after the enumeration refactor."""
        workspace = self.root / "workspace"
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (workspace / "report.md").write_text("# report\n", encoding="utf-8")
        (workspace / ".pal-candidate" / "staging").mkdir(parents=True)
        (workspace / ".pal-candidate" / "staging" / "partial.txt").write_text("staged\n", encoding="utf-8")
        (workspace / "inputs" / "a" / "b" / "c" / "d").mkdir(parents=True)
        (workspace / "inputs" / "a" / "b" / "c" / "d" / "deep.md").write_text("deep\n", encoding="utf-8")
        (workspace / "inputs" / "x.md").write_text("bound\n", encoding="utf-8")

        expected = {"report.md", "src/main.py"}
        ref, _ = self._snapshot(workspace)
        payload = self.store.read_json(ref)
        self.assertEqual({item["path"] for item in payload["files"]}, expected)

        clean = self.root / "clean"
        (clean / "src").mkdir(parents=True)
        (clean / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (clean / "report.md").write_text("# report\n", encoding="utf-8")
        self.assertEqual(payload["tree_fingerprint"], artifact_tree_fingerprint(clean))

        verification_ref = self.store.put_json({"status": "PASS"}, artifact_type="VerificationArtifact")
        published = self.adapter.publish_deliverable(
            workflow_id="workflow-1",
            candidate_ref=ref.to_dict(),
            verification_ref=verification_ref,
        )
        published_payload = self.store.read_json(published)
        self.assertEqual({item["path"] for item in published_payload["files"]}, expected)
        destination = Path(published_payload["destination"])
        self.assertFalse((destination / "inputs").exists())
        self.assertFalse((destination / ".pal-candidate").exists())
        self.assertTrue((destination / "report.md").is_file())

        candidate_dir, _scratch = self.adapter.prepare_verification_workspace(
            review_id="review-1", candidate_ref=ref.to_dict()
        )
        self.assertFalse((candidate_dir / "inputs").exists())
        self.assertFalse((candidate_dir / ".pal-candidate").exists())

    def test_first_component_semantics_through_adapter_paths(self) -> None:
        """The adapter feeds exact workspace-relative POSIX paths to the
        predicate: the root-level path ``inputs`` itself is excluded,
        while near-miss first components remain bundle content."""
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "inputs").write_text("root-level bound input file\n", encoding="utf-8")
        for near_miss in ("Inputs", "inputsx", "input", "inputs_"):
            (workspace / near_miss).mkdir()
            (workspace / near_miss / "keep.txt").write_text("keep\n", encoding="utf-8")
        (workspace / "report.md").write_text("# report\n", encoding="utf-8")

        ref, _ = self._snapshot(workspace)
        payload = self.store.read_json(ref)
        self.assertEqual(
            {item["path"] for item in payload["files"]},
            {
                "report.md",
                "Inputs/keep.txt",
                "inputsx/keep.txt",
                "input/keep.txt",
                "inputs_/keep.txt",
            },
        )

    def test_adapter_applies_predicate_decisions_verbatim(self) -> None:
        """Exclusion equals the predicate's selection exactly: the adapter
        never re-implements containment, so an arbitrary predicate
        decision is honored in both directions."""
        workspace = self.root / "workspace"
        (workspace / "src").mkdir(parents=True)
        (workspace / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (workspace / "report.md").write_text("# report\n", encoding="utf-8")
        (workspace / "inputs").mkdir()
        (workspace / "inputs" / "bound.txt").write_text("bound\n", encoding="utf-8")

        with mock.patch.object(
            adapters, "is_bound_input_path", side_effect=lambda path: path == "src/main.py"
        ):
            ref, _ = self._snapshot(workspace)
        payload = self.store.read_json(ref)
        self.assertEqual({item["path"] for item in payload["files"]}, {"report.md", "inputs/bound.txt"})

        with mock.patch.object(
            adapters, "is_bound_input_path", side_effect=lambda path: path == "inputs/bound.txt"
        ):
            ref, _ = self._snapshot(workspace)
        payload = self.store.read_json(ref)
        self.assertEqual({item["path"] for item in payload["files"]}, {"report.md", "src/main.py"})

    def test_fingerprint_matches_pre_change_algorithm_without_inputs_root(self) -> None:
        """A workspace with no bound-inputs root fingerprints identically
        to the pre-change algorithm, staging trees included."""
        workspace = self.root / "workspace"
        (workspace / "src" / "inputs").mkdir(parents=True)
        (workspace / "src" / "inputs" / "data.py").write_text("DATA = 1\n", encoding="utf-8")
        (workspace / "docs").mkdir()
        (workspace / "docs" / "notes.md").write_text("notes\n", encoding="utf-8")
        (workspace / ".pal-candidate").mkdir()
        (workspace / ".pal-candidate" / "staged.txt").write_text("staged\n", encoding="utf-8")
        self.assertEqual(artifact_tree_fingerprint(workspace), _legacy_tree_fingerprint(workspace))

    def test_staging_only_workspace_keeps_empty_candidate_error(self) -> None:
        """Staging trees never satisfy the non-empty candidate requirement;
        the pre-existing error is unchanged."""
        workspace = self.root / "staging-only"
        (workspace / ".pal-candidate").mkdir(parents=True)
        (workspace / ".pal-candidate" / "staged.txt").write_text("staged\n", encoding="utf-8")
        (workspace / "inputs").mkdir()
        (workspace / "inputs" / "bound.txt").write_text("bound\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "contains no files"):
            self._snapshot(workspace)


if __name__ == "__main__":
    unittest.main()
