from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.minion.v2.adapters import ArtifactBundleAdapter, prepare_v2_role_workspace, provision_artifact_workspaces
from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.repository import MinionV2Repository
from pal.shared import MinionInvocationPack


class ArtifactBundleAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_v2_artifact_adapter_"))
        self.repository = MinionV2Repository(self.root)
        self.store = ContentAddressedArtifactStore(self.root, self.repository)
        self.adapter = ArtifactBundleAdapter(self.root, self.store)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def test_candidate_integration_and_publish_without_git(self) -> None:
        workspaces = provision_artifact_workspaces(self.root, epoch_id="epoch-1", unit_ids=["facts", "report"])
        facts = Path(workspaces["facts"]["workspace_path"])
        report = Path(workspaces["report"]["workspace_path"])
        (facts / "facts.json").write_text(json.dumps({"protein_g": 120}), encoding="utf-8")
        (report / "checkin.json").write_text(json.dumps({"status": "on_track"}), encoding="utf-8")

        facts_ref, facts_digest = self.adapter.snapshot_candidate(
            workspace=facts,
            owned_area=["artifact:facts"],
            reference_only_paths=[],
            unit_contract_hash="facts-contract",
            dependency_output_hashes={},
            environment_fingerprint="test",
        )
        report_ref, report_digest = self.adapter.snapshot_candidate(
            workspace=report,
            owned_area=["artifact:report"],
            reference_only_paths=[],
            unit_contract_hash="report-contract",
            dependency_output_hashes={"facts": facts_digest},
            environment_fingerprint="test",
        )
        self.assertEqual(facts_digest, facts_ref.sha256)
        self.assertEqual(report_digest, report_ref.sha256)

        integration_ref, integration_digest = self.adapter.integrate_candidates(
            integration_workspace=Path(workspaces["integration"]["workspace_path"]),
            ordered_candidates=[
                {"node_run_id": "facts", "candidate_ref": facts_ref.to_dict()},
                {"node_run_id": "report", "candidate_ref": report_ref.to_dict()},
            ],
            architecture_manifest_sha="manifest",
        )
        self.assertEqual(integration_digest, integration_ref.sha256)
        verification_ref = self.store.put_json({"status": "PASS"}, artifact_type="VerificationArtifact")
        published_ref = self.adapter.publish_deliverable(
            workflow_id="workflow-1",
            candidate_ref=integration_ref.to_dict(),
            verification_ref=verification_ref,
        )
        published = self.store.read_json(published_ref)
        paths = {item["path"] for item in published["files"]}
        self.assertEqual(paths, {"facts.json", "checkin.json"})
        destination = Path(published["destination"])
        self.assertTrue((destination / "facts.json").is_file())
        self.assertTrue((destination / "checkin.json").is_file())

    def test_candidate_enforces_owned_and_reference_only_paths(self) -> None:
        workspace = self.root / "workspace"
        workspace.mkdir()
        (workspace / "private.json").write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "ownership"):
            self.adapter.snapshot_candidate(
                workspace=workspace,
                owned_area=["public/**"],
                reference_only_paths=[],
                unit_contract_hash="contract",
                dependency_output_hashes={},
                environment_fingerprint="test",
            )
        with self.assertRaisesRegex(ValueError, "reference_only"):
            self.adapter.snapshot_candidate(
                workspace=workspace,
                owned_area=["**"],
                reference_only_paths=["private.json"],
                unit_contract_hash="contract",
                dependency_output_hashes={},
                environment_fingerprint="test",
            )

    def test_role_workspace_binds_writable_invocation_directories(self) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "input.txt").write_text("truth", encoding="utf-8")
        pack = MinionInvocationPack(
            invocation_id="inv-role-workspace",
            goal="research",
            workspace={"repo_path": str(source)},
        )

        prepared = prepare_v2_role_workspace(self.root, pack, run_id="run-role-workspace")

        role_workspace = Path(prepared.workspace["repo_path"])
        self.assertEqual((role_workspace / "input.txt").read_text(encoding="utf-8"), "truth")
        self.assertTrue(role_workspace.is_relative_to(self.root / "data" / "minion" / "v2" / "role-workspaces"))
        for key in ("run_dir", "artifact_dir", "artifact_stage_dir", "log_dir", "review_scratch_dir"):
            path = Path(prepared.workspace[key])
            self.assertTrue(path.is_dir(), key)
            self.assertTrue(path.is_relative_to(self.root / "data" / "minion" / "v2" / "invocations" / "inv-role-workspace"))


if __name__ == "__main__":
    unittest.main()
