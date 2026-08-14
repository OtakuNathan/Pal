from __future__ import annotations

import copy
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.architecture_templates import ArchitectureTemplateCompiler
from pal.bunshin.v2.contract_protocol import (
    software_contract_projection,
)
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.skeleton import (
    ArchitectureValidationError,
    GitBackedSkeletonService,
    _architect_private_implementation_changes,
    compiled_module_write_scopes,
    validate_architecture_submission,
)
from pal.bunshin.v2.task_ledger import TaskLedgerService
from pal.bunshin.v2.workspace_paths import (
    MANAGER_ARCHITECT_DIRECTORY,
    module_developer_test_path,
    module_verification_corpus_path,
)


_DECODER_CONTRACT = """/*
Module: decoder
Responsibility: Own incremental frame decoding.
Requirements:
  - Decode complete frames.
Provides: decoded_frames.
Consumes: chunks.
Ownership: Decoder owns its buffer and state.
Lifecycle: construction, feed, finish, reset, destruction.
State: ready or failed.
Invariants: one decoder owns its buffered bytes.
Errors: malformed input enters failed state.
Compatibility: public Decoder interface.
*/
class Decoder;
"""


class SoftwareContractAdapterTests(unittest.TestCase):
    """Protect the private Git adapter behind the public Contract protocol."""

    def setUp(self) -> None:
        self.runtime_root = Path(
            tempfile.mkdtemp(prefix="pal-v2-contract-adapter-runtime-")
        )
        self.repo = Path(
            tempfile.mkdtemp(prefix="pal-v2-contract-adapter-repo-")
        )
        (self.repo / "include").mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "include" / "decoder.hpp").write_text(
            _DECODER_CONTRACT,
            encoding="utf-8",
        )
        (self.repo / "include" / "application.hpp").write_text(
            "int run_application();\n",
            encoding="utf-8",
        )
        (self.repo / "src" / "decoder.cpp").write_text(
            "// implementation placeholder\n",
            encoding="utf-8",
        )
        repository = BunshinV2Repository(self.runtime_root)
        artifacts = ContentAddressedArtifactStore(
            self.runtime_root,
            repository,
        )
        self.skeleton = GitBackedSkeletonService(self.runtime_root, artifacts)
        self.requirements_ref = TaskLedgerService(
            self.runtime_root,
            artifacts,
        ).publish(
            title="Decoder",
            task_spec={"objective": "Decode complete frames."},
            actor="test",
            source_channel="test",
        )
        self.requirements = artifacts.read_json(self.requirements_ref)
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        self.contract = copy.deepcopy(definition.example)

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_contract_projection_is_the_only_authored_input_to_git_validation(
        self,
    ) -> None:
        projected = software_contract_projection(self.contract)
        normalized = validate_architecture_submission(
            projected,
            requirements_payload=self.requirements,
            workspace_root=self.repo,
        )

        self.assertEqual(set(normalized), {"requirements", "modules", "scenarios"})
        decoder = normalized["modules"]["decoder"]
        self.assertEqual(decoder["responsibility"], "Own incremental frame decoding.")
        self.assertEqual(decoder["paths"]["contract_mode"], "review_guarded")
        self.assertNotIn("contract_schema", normalized)

    def test_git_adapter_rejects_missing_declared_contract_file(self) -> None:
        (self.repo / "include" / "decoder.hpp").unlink()
        with self.assertRaisesRegex(
            ArchitectureValidationError,
            "contract path does not exist",
        ):
            validate_architecture_submission(
                software_contract_projection(self.contract),
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

    def test_write_scopes_are_derived_from_module_contract(self) -> None:
        module = software_contract_projection(self.contract)["modules"][
            "decoder"
        ]
        scopes = compiled_module_write_scopes(
            {
                **module["paths"],
                "developer_tests": {
                    "kind": "directory",
                    "path": module_developer_test_path("decoder"),
                },
                "verification_corpus": {
                    "kind": "directory",
                    "path": module_verification_corpus_path("decoder"),
                },
            }
        )

        self.assertIn(
            {"kind": "file", "path": "include/decoder.hpp"},
            scopes,
        )
        self.assertIn(
            {"kind": "file", "path": "src/decoder.cpp"},
            scopes,
        )
        self.assertIn(
            {"kind": "directory", "path": "tests/decoder/developer"},
            scopes,
        )
        self.assertNotIn(
            {"kind": "directory", "path": "tests/decoder/verifier"},
            scopes,
        )

    def test_architect_may_change_contracts_but_not_private_implementation(self) -> None:
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.invalid",
                "commit",
                "-qm",
                "base",
            ],
            cwd=self.repo,
            check=True,
        )
        original = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        (self.repo / "include" / "decoder.hpp").write_text(
            _DECODER_CONTRACT + "\n// clarified invariant\n",
            encoding="utf-8",
        )
        (self.repo / "src" / "decoder.cpp").write_text(
            "int leaked_product_body = 1;\n",
            encoding="utf-8",
        )

        violations = _architect_private_implementation_changes(
            self.repo,
            changed_paths=("include/decoder.hpp", "src/decoder.cpp"),
            submission={
                "modules": {
                    "decoder": {
                        "paths": {"contract_paths": ["include/decoder.hpp"]}
                    }
                }
            },
            original_head=original,
        )

        self.assertEqual(violations, ["src/decoder.cpp"])

        co_located_contract = _architect_private_implementation_changes(
            self.repo,
            changed_paths=("src/decoder.cpp",),
            submission={
                "modules": {
                    "decoder": {
                        "paths": {
                            "contract_paths": ["src/decoder.cpp"],
                            "implementation_scopes": [
                                {"kind": "file", "path": "src/decoder.cpp"}
                            ],
                        }
                    }
                }
            },
            base_sha=original,
            original_head=original,
        )
        # Manager enforces authored path ownership only. Whether a co-located
        # contract edit contains declaration semantics or product behavior is
        # an architecture-review judgment, not a textual heuristic.
        self.assertEqual(co_located_contract, [])

    def test_module_paths_cannot_claim_repository_control_state(self) -> None:
        for path in (".git/config", "third_party/lib/.git/config"):
            with self.subTest(control_path=path):
                projected = software_contract_projection(self.contract)
                projected["modules"]["decoder"]["paths"][
                    "implementation_scopes"
                ] = [{"kind": "file", "path": path}]

                with self.assertRaisesRegex(
                    ValueError,
                    "targets Manager or VCS control state",
                ):
                    validate_architecture_submission(
                        projected,
                        requirements_payload=self.requirements,
                        workspace_root=self.repo,
                    )

    def test_new_project_explicit_repo_path_is_created_before_snapshot(self) -> None:
        requested_repo = self.runtime_root / "projects" / "fixed_queue"

        workspace = self.skeleton.provision_architecture_workspace(
            workflow_id="wf-new-project",
            workflow_name="fixed_queue",
            revision_name="revision-1",
            workspace={
                "kind": "new_project",
                "project_name": "fixed_queue",
                "repo_path": str(requested_repo),
            },
            requirements_ref=self.requirements_ref,
        )

        self.assertTrue(requested_repo.is_dir())
        self.assertTrue(workspace.worktree.is_dir())

    def test_existing_repo_path_is_not_created_implicitly(self) -> None:
        missing_repo = self.runtime_root / "missing-existing-repo"

        with self.assertRaisesRegex(ValueError, "workspace source is not a directory"):
            self.skeleton.provision_architecture_workspace(
                workflow_id="wf-existing-repo",
                workflow_name="missing_repo",
                revision_name="revision-1",
                workspace={
                    "kind": "existing_repo",
                    "project_name": "missing_repo",
                    "repo_path": str(missing_repo),
                },
                requirements_ref=self.requirements_ref,
            )

        self.assertFalse(missing_repo.exists())

    def test_clean_git_snapshot_without_github_remote_uses_local_delivery(self) -> None:
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "config", "user.name", "Test"], cwd=self.repo, check=True
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.repo,
            check=True,
        )
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "initial"], cwd=self.repo, check=True
        )

        no_remote = self.skeleton._create_synthetic_snapshot(
            self.runtime_root / "no-remote.git",
            self.repo,
            snapshot_ref="refs/bunshin/snapshots/no-remote",
        )
        self.assertTrue(no_remote["source_clean"])
        self.assertEqual(no_remote["delivery_mode"], "local_only")
        self.assertEqual(
            no_remote["delivery_fallback_reason"],
            "source Git repository has no configured push remote",
        )

        local_remote = self.runtime_root / "local-origin.git"
        subprocess.run(["git", "init", "--bare", "-q", str(local_remote)], check=True)
        subprocess.run(
            ["git", "remote", "add", "origin", str(local_remote)],
            cwd=self.repo,
            check=True,
        )
        local_only = self.skeleton._create_synthetic_snapshot(
            self.runtime_root / "local-remote.git",
            self.repo,
            snapshot_ref="refs/bunshin/snapshots/local-remote",
        )
        self.assertEqual(local_only["delivery_mode"], "local_only")
        self.assertEqual(
            local_only["delivery_fallback_reason"],
            "source push target does not support GitHub pull requests",
        )

    def test_architect_snapshot_preserves_hand_off_without_committing_it(self) -> None:
        workspace = self.skeleton.provision_architecture_workspace(
            workflow_id="wf-hand-off",
            workflow_name="decoder",
            revision_name="revision-1",
            workspace={
                "kind": "existing_repo",
                "project_name": "decoder",
                "repo_path": str(self.repo),
            },
            requirements_ref=self.requirements_ref,
        )
        hand_off = (
            workspace.worktree
            / MANAGER_ARCHITECT_DIRECTORY
            / "architect.yaml"
        )
        hand_off.parent.mkdir(parents=True)
        hand_off.write_text("schema_version: '1'\n", encoding="utf-8")

        snapshot_ref = self.skeleton.snapshot_architect_result(
            workflow_name="wf-hand-off",
            revision_name="revision-1",
            architecture_workspace=workspace,
            submission=software_contract_projection(self.contract),
            requirements_ref=self.requirements_ref,
        )
        snapshot = self.skeleton.artifacts.read_json(snapshot_ref)
        committed_paths = subprocess.run(
            [
                "git",
                "-C",
                str(workspace.worktree),
                "ls-tree",
                "-r",
                "--name-only",
                str(snapshot["skeleton_commit_sha"]),
            ],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()

        self.assertTrue(hand_off.is_file())
        self.assertEqual(
            hand_off.read_text(encoding="utf-8"),
            "schema_version: '1'\n",
        )
        self.assertNotIn(
            f"{MANAGER_ARCHITECT_DIRECTORY}/architect.yaml",
            committed_paths,
        )
        self.assertNotIn(
            f"{MANAGER_ARCHITECT_DIRECTORY}/architect.yaml",
            snapshot["changed_paths"],
        )


if __name__ == "__main__":
    unittest.main()
