from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pal.bunshin.v2.adapters import ArtifactBundleAdapter, prepare_v2_role_workspace, provision_artifact_workspaces
from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.paths import resolve_project_git_layout
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.shared import BunshinInvocationPack


class ArtifactBundleAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_v2_artifact_adapter_"))
        self.repository = BunshinV2Repository(self.root)
        self.store = ContentAddressedArtifactStore(self.root, self.repository)
        self.adapter = ArtifactBundleAdapter(self.root, self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_declared_sink_composes_and_publishes_without_hidden_workspace(self) -> None:
        workspaces = provision_artifact_workspaces(self.root, epoch_id="epoch-1", unit_ids=["facts", "report"])
        facts = Path(workspaces["facts"]["workspace_path"])
        report = Path(workspaces["report"]["workspace_path"])
        (facts / "facts.json").write_text(json.dumps({"protein_g": 120}), encoding="utf-8")
        facts_ref, facts_digest = self.adapter.snapshot_candidate(
            workspace=facts,
            reference_only_paths=[],
            unit_contract_hash="facts-contract",
            dependency_output_hashes={},
            environment_fingerprint="test",
        )
        # The graph executor materializes accepted provider output into the
        # authored sink workspace; there is no Manager-created integration
        # workspace or integration worker.
        self.adapter.materialize_candidate(facts_ref, report)
        (report / "checkin.json").write_text(json.dumps({"status": "on_track"}), encoding="utf-8")
        report_ref, report_digest = self.adapter.snapshot_candidate(
            workspace=report,
            reference_only_paths=[],
            unit_contract_hash="report-contract",
            dependency_output_hashes={"facts": facts_digest},
            environment_fingerprint="test",
        )
        self.assertEqual(facts_digest, facts_ref.sha256)
        self.assertEqual(report_digest, report_ref.sha256)

        self.assertNotIn("integration", workspaces)
        verification_ref = self.store.put_json({"status": "PASS"}, artifact_type="VerificationArtifact")
        published_ref = self.adapter.publish_deliverable(
            workflow_id="workflow-1",
            candidate_ref=report_ref.to_dict(),
            verification_ref=verification_ref,
        )
        published = self.store.read_json(published_ref)
        paths = {item["path"] for item in published["files"]}
        self.assertEqual(paths, {"facts.json", "checkin.json"})
        destination = Path(published["destination"])
        self.assertTrue((destination / "facts.json").is_file())
        self.assertTrue((destination / "checkin.json").is_file())

    def test_candidate_allows_workspace_files_and_enforces_reference_only_paths(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "private.json").write_text("{}", encoding="utf-8")
        ref, _ = self.adapter.snapshot_candidate(
            workspace=workspace,
            reference_only_paths=[],
            unit_contract_hash="contract",
            dependency_output_hashes={},
            environment_fingerprint="test",
        )
        self.assertEqual({item["path"] for item in self.store.read_json(ref)["files"]}, {"private.json"})
        with self.assertRaisesRegex(ValueError, "reference-only"):
            self.adapter.snapshot_candidate(
                workspace=workspace,
                reference_only_paths=["private.json"],
                unit_contract_hash="contract",
                dependency_output_hashes={},
                environment_fingerprint="test",
            )

    def test_role_workspace_binds_writable_invocation_directories(self) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "input.txt").write_text("truth", encoding="utf-8")
        pack = BunshinInvocationPack(
            invocation_id="inv-role-workspace",
            goal="research",
            workspace={"repo_path": str(source)},
        )

        prepared = prepare_v2_role_workspace(self.root, pack, run_id="run-role-workspace")

        role_workspace = Path(prepared.workspace["repo_path"])
        self.assertEqual((role_workspace / "input.txt").read_text(encoding="utf-8"), "truth")
        self.assertTrue(
            role_workspace.is_relative_to(
                self.root / "data" / "bunshin" / "runtime" / "role-workspaces"
            )
        )
        for key in ("run_dir", "artifact_dir", "artifact_stage_dir", "log_dir", "review_scratch_dir"):
            path = Path(prepared.workspace[key])
            self.assertTrue(path.is_dir(), key)
            self.assertTrue(
                path.is_relative_to(
                    self.root
                    / "data"
                    / "bunshin"
                    / "runtime"
                    / "invocations"
                    / "inv-role-workspace"
                )
            )

    def test_role_workspace_isolates_attempt_artifacts_but_keeps_shared_run_journal(self) -> None:
        source = self.root / "attempt-source"
        source.mkdir()
        pack = BunshinInvocationPack(
            invocation_id="inv-attempt-isolation",
            workspace={"repo_path": str(source)},
        )

        first = prepare_v2_role_workspace(
            self.root,
            pack,
            run_id="run-attempt-isolation",
            attempt_key="fence-1",
        )
        (source / "candidate.txt").write_text("second", encoding="utf-8")
        second = prepare_v2_role_workspace(
            self.root,
            pack,
            run_id="run-attempt-isolation",
            attempt_key="fence-2",
        )

        self.assertEqual(first.workspace["run_dir"], second.workspace["run_dir"])
        self.assertNotEqual(first.workspace["repo_path"], second.workspace["repo_path"])
        self.assertFalse((Path(first.workspace["repo_path"]) / "candidate.txt").exists())
        self.assertEqual(
            (Path(second.workspace["repo_path"]) / "candidate.txt").read_text(encoding="utf-8"),
            "second",
        )
        self.assertIn("attempts/fence-1", first.workspace["repo_path"])
        self.assertIn("attempts/fence-2", second.workspace["repo_path"])
        self.assertNotEqual(first.workspace["artifact_dir"], second.workspace["artifact_dir"])
        self.assertIn("attempts/fence-1", first.workspace["artifact_stage_dir"])
        self.assertIn("attempts/fence-2", second.workspace["artifact_stage_dir"])

    def test_role_workspace_clones_exact_linked_worktree_head(self) -> None:
        repository = self.root / "linked-source-repository"
        repository.mkdir()
        subprocess.run(["git", "init", "-q", str(repository)], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.email", "pal@example.invalid"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repository), "config", "user.name", "Pal Tests"],
            check=True,
        )
        (repository / "value.txt").write_text("default branch\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(repository), "add", "value.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(repository), "commit", "-qm", "default branch"],
            check=True,
        )
        linked_worktree = self.root / "candidate-worktree"
        subprocess.run(
            [
                "git",
                "-C",
                str(repository),
                "worktree",
                "add",
                "-q",
                "-b",
                "candidate",
                str(linked_worktree),
            ],
            check=True,
        )
        (linked_worktree / "value.txt").write_text("candidate branch\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(linked_worktree), "add", "value.txt"], check=True)
        subprocess.run(
            ["git", "-C", str(linked_worktree), "commit", "-qm", "candidate"],
            check=True,
        )
        source_head = subprocess.check_output(
            ["git", "-C", str(linked_worktree), "rev-parse", "HEAD"],
            text=True,
        ).strip()
        pack = BunshinInvocationPack(
            invocation_id="inv-linked-role-workspace",
            workspace={"repo_path": str(linked_worktree)},
        )

        prepared = prepare_v2_role_workspace(
            self.root,
            pack,
            run_id="run-linked-role-workspace",
            attempt_key="fence-1",
        )

        role_workspace = Path(prepared.workspace["repo_path"])
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(role_workspace), "rev-parse", "HEAD"],
                text=True,
            ).strip(),
            source_head,
        )
        self.assertEqual(
            (role_workspace / "value.txt").read_text(encoding="utf-8"),
            "candidate branch\n",
        )
        self.assertEqual(
            subprocess.check_output(
                ["git", "-C", str(role_workspace), "status", "--porcelain"],
                text=True,
            ),
            "",
        )

    def test_project_layout_uses_repo_name_and_disambiguates_same_named_sources(self) -> None:
        first_source = self.root / "one" / "shared-project"
        second_source = self.root / "two" / "shared-project"
        first_source.mkdir(parents=True)
        second_source.mkdir(parents=True)

        first = resolve_project_git_layout(
            self.root,
            workspace={"repo_path": str(first_source)},
            workflow_id="wf-first",
            workflow_name="First delivery",
        )
        second = resolve_project_git_layout(
            self.root,
            workspace={"repo_path": str(second_source)},
            workflow_id="wf-second",
            workflow_name="Second delivery",
        )

        self.assertEqual(first.project_key, "shared-project")
        self.assertTrue(second.project_key.startswith("shared-project-"))
        self.assertNotEqual(first.project_root, second.project_root)
        self.assertTrue(first.workflow_branch.endswith("/main"))
        self.assertTrue(second.workflow_branch.endswith("/main"))


if __name__ == "__main__":
    unittest.main()
