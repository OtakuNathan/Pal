from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from pal.llm.contracts import CanonicalToolCall
from pal.minion.v2.architecture import ArchitectureArtifactService
from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.minion.v2.contracts import ActionEnvelope, AggregateType
from pal.minion.v2.execution import DagScheduler, ExecutionCompiler, UnitWorkViewBuilder
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.skeleton import (
    ARCHITECTURE_SKELETON_ARTIFACT,
    GitBackedSkeletonService,
    architecture_revision_changed_paths_since,
    architecture_revision_path_states,
    architecture_revision_scope,
    compile_skeleton_markdown,
    review_architecture_skeleton,
    validate_architecture_revision_scope,
    validate_architecture_submission,
)
from pal.minion.v2.task_sources import TaskSourceBundleService
from pal.minion.v2.skeleton_builder import (
    SKELETON_BUILDER_TOOL_SPECS,
    architecture_question_tool_result,
    compile_architecture_review_invocation_tool_contract,
    skeleton_builder_tool_result,
)
from pal.minion.v2.submission_drafts import (
    AUTHORING_CONTRACT_VERSION,
    SubmissionDraftContext,
    SubmissionDraftStore,
)
from pal.minion.v2.workers import MinionV2SemanticWorker


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr or completed.stdout)
    return completed.stdout


async def _record_async(bucket: list[dict[str, object]], payload: dict[str, object]) -> None:
    bucket.append(dict(payload))


def _contract(module: str) -> str:
    return f"""/*
Module: {module}
Responsibility: Define the stable boundary.
Requirements:
  - Route matching must be deterministic.
Provides: A route matching protocol.
Consumes: Immutable route input.
Ownership: The caller owns inputs and results.
Lifecycle: process lifetime; no owned runtime resources.
State: stateless
Invariants: Equal inputs produce equal outputs.
Errors: Invalid rules return a deterministic error.
Compatibility: Existing public signatures remain stable.
*/

class RuleRouter;
"""


class MinionV2SkeletonTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal-v2-skeleton-runtime-"))
        self.repo = Path(tempfile.mkdtemp(prefix="pal-v2-skeleton-repo-"))
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "README.md").write_text("base\n", encoding="utf-8")
        (self.repo / "deleted.txt").write_text("remove me\n", encoding="utf-8")
        (self.repo / "src").mkdir()
        (self.repo / "src" / "router.cpp").write_text("// implementation skeleton\n", encoding="utf-8")
        (self.repo / "tests").mkdir()
        (self.repo / "tests" / "test_router.cpp").write_text("// test skeleton\n", encoding="utf-8")
        _git(self.repo, "add", "-A")
        _git(
            self.repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        )
        self.repository = MinionV2Repository(self.runtime_root)
        self.artifacts = ContentAddressedArtifactStore(self.runtime_root, self.repository)
        self.service = GitBackedSkeletonService(self.runtime_root, self.artifacts)
        self.requirements_ref = TaskSourceBundleService(
            self.runtime_root,
            self.artifacts,
        ).publish(
            title="Tiny router",
            request_text=(
                "Route matching must be deterministic.\n"
                "Existing public signatures remain stable.\n"
            ),
            workspace={"repo_path": str(self.repo)},
            actor="test",
            source_channel="test",
        )
        self.requirements = self.artifacts.read_json(self.requirements_ref)
        self.builder_call_index = 0
        self.builder_lease_index = 0

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_normalized_architecture_contains_only_module_dag_and_paths(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        normalized = validate_architecture_submission(
            self._submission(),
            requirements_payload=self.requirements,
            workspace_root=self.repo,
        )

        self.assertEqual(set(normalized), {"modules", "scenarios"})
        self.assertEqual(
            set(normalized["modules"]["router"]),
            {"module_kind", "contract_dependencies", "paths"},
        )
        self.assertEqual(
            normalized["modules"]["router"]["paths"]["contract_mode"],
            "file_frozen",
        )

    def test_review_guarded_contract_may_share_its_module_writable_file(self) -> None:
        submission = self._submission()
        submission["modules"]["router"]["paths"] = {
            **submission["modules"]["router"]["paths"],
            "contract_mode": "review_guarded",
            "contract_paths": ["src/router.cpp"],
        }

        normalized = validate_architecture_submission(
            submission,
            requirements_payload=self.requirements,
            workspace_root=self.repo,
        )

        self.assertEqual(
            normalized["modules"]["router"]["paths"]["contract_mode"],
            "review_guarded",
        )

    def test_file_frozen_contract_rejects_its_module_writable_overlap(self) -> None:
        submission = self._submission()
        submission["modules"]["router"]["paths"] = {
            **submission["modules"]["router"]["paths"],
            "contract_mode": "file_frozen",
            "contract_paths": ["src/router.cpp"],
        }

        with self.assertRaisesRegex(ValueError, "file_frozen contract"):
            validate_architecture_submission(
                submission,
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

    def test_review_guarded_contract_cannot_be_owned_by_test_scope(self) -> None:
        submission = self._submission()
        submission["modules"]["router"]["paths"] = {
            **submission["modules"]["router"]["paths"],
            "contract_mode": "review_guarded",
            "contract_paths": ["tests/test_router.cpp"],
        }

        with self.assertRaisesRegex(ValueError, "review_guarded contract"):
            validate_architecture_submission(
                submission,
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

    def test_architecture_validation_reports_all_current_path_errors_together(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        (self.repo / "src" / "router.cpp").unlink()
        (self.repo / "tests" / "test_router.cpp").unlink()

        with self.assertRaises(ValueError) as raised:
            validate_architecture_submission(
                self._submission(),
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

        message = str(raised.exception)
        self.assertIn("src/router.cpp", message)
        self.assertIn("tests/test_router.cpp", message)
        self.assertIn("consistent errors", message)

    def test_submission_rejects_overlapping_owned_scopes(self) -> None:
        contract = self.repo / "include" / "router.h"
        contract.parent.mkdir()
        contract.write_text(_contract("router"), encoding="utf-8")
        submission = self._submission()
        submission["modules"]["other"] = {
            **submission["modules"]["router"],
            "paths": {
                **submission["modules"]["router"]["paths"],
                "contract_paths": ["include/other.h"],
            },
        }
        (self.repo / "include" / "other.h").write_text(_contract("other"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "overlap"):
            validate_architecture_submission(
                submission,
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

    def test_construction_dag_rejects_a_cycle(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        (self.repo / "include" / "consumer.h").write_text(_contract("consumer"), encoding="utf-8")
        (self.repo / "src" / "consumer.cpp").write_text("// consumer\n", encoding="utf-8")
        (self.repo / "tests" / "test_consumer.cpp").write_text("// consumer test\n", encoding="utf-8")
        submission = self._submission()
        submission["modules"]["consumer"] = {
            "module_kind": "implementation",
            "contract_dependencies": ["router"],
            "paths": {
                "contract_paths": ["include/consumer.h"],
                "implementation_scopes": [{"kind": "file", "path": "src/consumer.cpp"}],
                "test_scopes": [{"kind": "file", "path": "tests/test_consumer.cpp"}],
                "reference_only": [],
            },
        }
        submission["modules"]["router"]["contract_dependencies"] = ["consumer"]
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_architecture_submission(
                submission,
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

    def test_contract_dependencies_do_not_serialize_implementation_coders(self) -> None:
        workspace = self._provision_complete_workspace("parallel-contracts", "initial")
        (workspace.worktree / "include" / "consumer.h").write_text(
            _contract("consumer"), encoding="utf-8"
        )
        (workspace.worktree / "src" / "consumer.cpp").write_text(
            "// consumer implementation skeleton\n", encoding="utf-8"
        )
        (workspace.worktree / "tests" / "test_consumer.cpp").write_text(
            "// consumer test skeleton\n", encoding="utf-8"
        )
        submission = self._submission()
        submission["modules"]["consumer"] = {
            "module_kind": "implementation",
            "contract_dependencies": ["router"],
            "paths": {
                "contract_mode": "file_frozen",
                "contract_paths": ["include/consumer.h"],
                "implementation_scopes": [{"kind": "file", "path": "src/consumer.cpp"}],
                "test_scopes": [{"kind": "file", "path": "tests/test_consumer.cpp"}],
                "reference_only": [],
            },
        }
        submission["scenarios"]["router_end_to_end"]["modules"] = ["router", "consumer"]
        manifest_ref = self.service.snapshot_architecture(
            workflow_name="parallel-contracts",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=submission,
            requirements_ref=self.requirements_ref,
        )
        compilation = ExecutionCompiler(
            self.repository,
            ArchitectureArtifactService(self.artifacts, self.repository),
        ).compile_epoch(
            workflow_id="parallel-contracts",
            epoch_id="parallel-contracts-epoch",
            manifest_ref=manifest_ref,
        )

        queued = DagScheduler(self.repository).schedule_ready_nodes(
            workflow_id="parallel-contracts",
            epoch_id=compilation.epoch_id,
            max_new_nodes=2,
        )

        self.assertEqual(set(queued), set(compilation.unit_node_ids.values()))
        consumer = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            compilation.unit_node_ids["consumer"],
        )
        scenario = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            compilation.verification_node_ids["router_end_to_end"],
        )
        assert consumer is not None and scenario is not None
        self.assertEqual(consumer.payload["dependency_node_ids"], [])
        self.assertEqual(
            consumer.payload["contract_dependency_node_ids"],
            [compilation.unit_node_ids["router"]],
        )
        self.assertEqual(scenario.state, "BLOCKED_BY_DEPS")
        self.assertEqual(
            set(scenario.payload["dependency_node_ids"]),
            set(compilation.unit_node_ids.values()),
        )

    def test_architect_question_returns_in_place_and_records_task_source_amendment(self) -> None:
        workspace = self._bind_builder_workspace(
            {
                "repo_path": str(self.repo),
                "artifact_dir": str(self.runtime_root / "question-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "question-stage"),
            },
            role="architect",
        )
        observed: list[dict[str, object]] = []

        async def answer(payload: dict[str, object], timeout: float | None) -> dict[str, object]:
            observed.append({"payload": payload, "timeout": timeout})
            return {
                "answers": [
                    {"question_id": "architecture-question", "answer": "Preserve the public API"}
                ]
            }

        result = asyncio.run(
            architecture_question_tool_result(
                CanonicalToolCall(
                    name="op_minion_architecture_ask_user",
                    args={
                        "title": "Compatibility",
                        "question": "Which compatibility boundary is binding?",
                        "option_1": "Preserve API: no caller migration",
                        "option_2": "Allow adapter: retain an explicit facade",
                        "option_3": "Break API: migrate all consumers",
                    },
                    call_id="ask-architecture-question",
                ),
                workspace,
                [],
                request_user=answer,
            )
        )

        self.assertTrue(result.ok, result.llm_text)
        self.assertEqual(result.structured["answer"], "Preserve the public API")
        self.assertIsNone(observed[0]["timeout"])
        context = SubmissionDraftContext.from_workspace(workspace, draft_kind="architecture")
        draft = SubmissionDraftStore(self.runtime_root).read(context, seed={})
        submission = dict(dict(draft.payload["definitions"])["submission"])
        self.assertEqual(len(submission["clarification_refs"]), 1)

    def test_architect_clarification_becomes_the_snapshot_task_source(self) -> None:
        workspace = self._provision_complete_workspace("clarified-task", "initial")
        amendment_ref = self.artifacts.put_bytes(
            b"Preserve the existing public API; adapters may be added behind it.\n",
            artifact_type="TaskSourceAmendmentArtifact",
            media_type="text/markdown",
            provenance={"origin": "architect_user_clarification"},
        )
        submission = self._submission()
        submission["clarification_refs"] = [amendment_ref.to_dict()]

        manifest_ref = self.service.snapshot_architecture(
            workflow_name="clarified-task",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=submission,
            requirements_ref=self.requirements_ref,
        )

        manifest = self.artifacts.read_json(manifest_ref)
        effective_requirements_ref = ArtifactRef.from_mapping(manifest["requirements_ref"])
        effective_requirements = self.artifacts.read_json(effective_requirements_ref)
        self.assertNotEqual(effective_requirements_ref.sha256, self.requirements_ref.sha256)
        self.assertEqual(len(effective_requirements["amendments"]), 1)
        self.assertEqual(
            effective_requirements["amendments"][0]["artifact_ref"],
            amendment_ref.to_dict(),
        )
        self.assertNotIn("clarification_refs", manifest["submission"])

    def test_contract_only_module_is_frozen_without_a_coder_node(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        (self.repo / "include" / "route_types.h").write_text(
            _contract("route_types").replace("class RuleRouter;", "struct RouteInput;"),
            encoding="utf-8",
        )
        submission = self._submission()
        submission["modules"]["route_types"] = {
            "module_kind": "contract_only",
            "contract_dependencies": [],
            "paths": {
                "contract_paths": ["include/route_types.h"],
                "implementation_scopes": [],
                "test_scopes": [],
                "reference_only": [],
            },
        }

        normalized = validate_architecture_submission(
            submission,
            requirements_payload=self.requirements,
            workspace_root=self.repo,
        )
        self.assertEqual(normalized["modules"]["route_types"]["module_kind"], "contract_only")

        workspace = self.service.provision_architecture_workspace(
            workflow_id="contract-only",
            revision_name="initial",
            workspace={"repo_path": str(self.repo)},
            requirements_ref=self.requirements_ref,
        )
        (workspace.worktree / "include").mkdir(exist_ok=True)
        (workspace.worktree / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        (workspace.worktree / "include" / "route_types.h").write_text(
            _contract("route_types").replace("class RuleRouter;", "struct RouteInput;"),
            encoding="utf-8",
        )
        manifest_ref = self.service.snapshot_architecture(
            workflow_name="contract-only",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=submission,
            requirements_ref=self.requirements_ref,
        )
        compilation = ExecutionCompiler(
            self.repository,
            ArchitectureArtifactService(self.artifacts, self.repository),
        ).compile_epoch(
            workflow_id="contract-only",
            epoch_id="contract-only-epoch",
            manifest_ref=manifest_ref,
        )
        self.assertEqual(set(compilation.unit_node_ids), {"router"})
        self.assertEqual(set(compilation.verification_node_ids), {"router_end_to_end"})

        workflows = MinionV2WorkflowService(self.runtime_root)
        workflows.create_task(
            {
                "task_id": "contract-only-repair-task",
                "title": "Repair router with frozen route types",
                "objective": "Repair the one implementation module",
                "family_id": "software_engineering",
                "workspace": {"kind": "existing_repo", "repo_path": str(self.repo)},
            }
        )
        repair = workflows.start_workflow(
            {
                "task_id": "contract-only-repair-task",
                "operation": "review_and_repair",
                "artifact_ref": manifest_ref.to_dict(),
            }
        )
        self.assertEqual(repair["state"], "CREATED")

    def test_contract_only_module_rejects_fake_writable_units(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        submission = self._submission()
        submission["modules"]["router"]["module_kind"] = "contract_only"
        with self.assertRaisesRegex(ValueError, "contract_only.*cannot declare"):
            validate_architecture_submission(
                submission,
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

    def test_contract_dependencies_can_consume_contract_only_module(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        (self.repo / "include" / "route_types.h").write_text(_contract("route_types"), encoding="utf-8")
        submission = self._submission()
        submission["modules"]["route_types"] = {
            "module_kind": "contract_only",
            "contract_dependencies": [],
            "paths": {
                "contract_paths": ["include/route_types.h"],
                "implementation_scopes": [],
                "test_scopes": [],
                "reference_only": [],
            },
        }
        submission["modules"]["router"]["contract_dependencies"] = ["route_types"]
        normalized = validate_architecture_submission(
            submission,
            requirements_payload=self.requirements,
            workspace_root=self.repo,
        )
        self.assertEqual(
            normalized["modules"]["router"]["contract_dependencies"],
            ["route_types"],
        )

    def test_dirty_workspace_snapshot_and_skeleton_bundle_are_self_contained(self) -> None:
        (self.repo / "README.md").write_text("dirty tracked\n", encoding="utf-8")
        (self.repo / "deleted.txt").unlink()
        (self.repo / "untracked.txt").write_text("untracked\n", encoding="utf-8")

        workspace = self.service.provision_architecture_workspace(
            workflow_id="tiny-router",
            revision_name="initial",
            workspace={"repo_path": str(self.repo)},
            requirements_ref=self.requirements_ref,
        )
        self.assertEqual((workspace.worktree / "README.md").read_text(encoding="utf-8"), "dirty tracked\n")
        self.assertEqual((workspace.worktree / "untracked.txt").read_text(encoding="utf-8"), "untracked\n")
        self.assertFalse((workspace.worktree / "deleted.txt").exists())

        (workspace.worktree / "include").mkdir()
        (workspace.worktree / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        artifact_ref = self.service.snapshot_architecture(
            workflow_name="tiny-router",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        self.assertEqual(artifact_ref.artifact_type, ARCHITECTURE_SKELETON_ARTIFACT)
        artifact = self.artifacts.read_json(artifact_ref)
        self.assertIn("include/router.h", artifact["changed_paths"])
        self.assertIn("untracked.txt", _git(workspace.worktree, "ls-tree", "-r", "--name-only", artifact["base_commit_sha"]))
        self.assertNotIn("deleted.txt", _git(workspace.worktree, "ls-tree", "-r", "--name-only", artifact["base_commit_sha"]))

        bundle_ref = artifact["git_bundle_ref"]
        bundle_path = self.runtime_root / "recovered.bundle"
        bundle_path.write_bytes(self.artifacts.read_bytes(bundle_ref))
        recovered = self.runtime_root / "recovered.git"
        completed = subprocess.run(
            ["git", "clone", "--bare", str(bundle_path), str(recovered)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        recovered_tree = subprocess.check_output(
            ["git", f"--git-dir={recovered}", "rev-parse", f"{artifact['skeleton_commit_sha']}^{{tree}}"],
            text=True,
        ).strip()
        self.assertEqual(recovered_tree, artifact["skeleton_tree_sha"])

        review = review_architecture_skeleton(
            artifact,
            worktree=workspace.worktree,
            requirements_payload=self.requirements,
        )
        self.assertEqual(review.verdict, "PASS")
        markdown = compile_skeleton_markdown(artifact, requirements_payload=self.requirements)
        self.assertIn("### router", markdown)
        self.assertNotIn(artifact_ref.sha256, markdown)

    def test_project_repo_contains_readable_workflow_and_is_shared_with_execution(self) -> None:
        workflow_id = "wf_4ee6259e8d2b41f0b6db35b04b87deca"
        workspace = self.service.provision_architecture_workspace(
            workflow_id=workflow_id,
            workflow_name="Tiny Router Delivery",
            revision_name="rev_7432f60a16414b30b3393379950d84c0",
            workspace={"repo_path": str(self.repo)},
            requirements_ref=self.requirements_ref,
        )
        self.assertEqual(workspace.common_git_dir.parent.name, self.repo.name)
        self.assertNotIn(workflow_id, str(workspace.common_git_dir.parent))
        self.assertTrue(workspace.workflow_branch.startswith("minion/Tiny-Router-Delivery-"))
        self.assertTrue(workspace.workflow_branch.endswith("/main"))

        self.assertIn("/architecture/revision-", workspace.architecture_branch)

        (workspace.worktree / "include").mkdir()
        (workspace.worktree / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        skeleton_ref = self.service.snapshot_architecture(
            workflow_name="Tiny Router Delivery",
            revision_name="rev_7432f60a16414b30b3393379950d84c0",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        skeleton = self.artifacts.read_json(skeleton_ref)
        compilation = ExecutionCompiler(
            self.repository,
            ArchitectureArtifactService(self.artifacts, self.repository),
        ).compile_epoch(
            workflow_id=workflow_id,
            epoch_id="epoch_a6cb179a741e4a77bcd36c9910c3a18c",
            manifest_ref=skeleton_ref,
        )
        node = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            compilation.unit_node_ids["router"],
        )
        assert node is not None
        self.assertEqual(Path(node.payload["common_git_dir"]), workspace.common_git_dir)
        self.assertEqual(node.payload["workflow_branch"], workspace.workflow_branch)
        self.assertIn("/node/router-", node.payload["worktree_branch"])
        self.assertEqual(
            _git(workspace.worktree, "rev-parse", workspace.workflow_branch).strip(),
            skeleton["skeleton_commit_sha"],
        )

        review_workspace = self.service.provision_review_worktree(
            artifact=skeleton,
            review_name="tiny-router",
        )
        review_root = review_workspace.root
        self.assertTrue(review_root.is_relative_to(Path("/tmp")))
        self.assertFalse(review_root.is_relative_to(self.runtime_root))
        self.assertEqual(review_workspace.common_git_dir, workspace.common_git_dir)
        review_workspace.cleanup()
        self.assertFalse(review_root.exists())

    def test_manager_does_not_validate_contract_comment_semantics(self) -> None:
        workspace = self.service.provision_architecture_workspace(
            workflow_id="warning-review",
            revision_name="initial",
            workspace={"repo_path": str(self.repo)},
            requirements_ref=self.requirements_ref,
        )
        (workspace.worktree / "include").mkdir()
        (workspace.worktree / "include" / "router.h").write_text(
            "class RuleRouter;\n", encoding="utf-8"
        )

        manifest_ref = self.service.snapshot_architecture(
            workflow_name="warning-review",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        manifest = self.artifacts.read_json(manifest_ref)
        report = self.artifacts.read_json(manifest["validation_report_ref"])

        self.assertEqual(report["status"], "valid")
        self.assertEqual(report["warnings"], [])
        markdown = compile_skeleton_markdown(
            manifest,
            requirements_payload=self.requirements,
        )
        self.assertNotIn("Manager Advisory Warnings", markdown)

    def test_review_restores_missing_canonical_repository_from_bundle(self) -> None:
        workspace = self.service.provision_architecture_workspace(
            workflow_id="restore-review-repository",
            workflow_name="Restore Review Repository",
            revision_name="initial",
            workspace={"repo_path": str(self.repo)},
            requirements_ref=self.requirements_ref,
        )
        (workspace.worktree / "include").mkdir()
        (workspace.worktree / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        skeleton_ref = self.service.snapshot_architecture(
            workflow_name="Restore Review Repository",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        skeleton = self.artifacts.read_json(skeleton_ref)
        canonical_git_dir = workspace.common_git_dir
        shutil.rmtree(canonical_git_dir)

        review_workspace = self.service.provision_review_worktree(
            artifact=skeleton,
            review_name="restored-review",
        )

        self.assertEqual(review_workspace.common_git_dir, canonical_git_dir)
        self.assertFalse(review_workspace.temporary_common_git_dir)
        self.assertEqual(
            _git(review_workspace.worktree, "rev-parse", "HEAD").strip(),
            skeleton["skeleton_commit_sha"],
        )
        review_workspace.cleanup()

    def test_architecture_snapshot_is_idempotent_and_reuses_workspace_snapshot_after_restart(self) -> None:
        workspace = self._provision_complete_workspace("idempotent", "initial")
        first_ref = self.service.snapshot_architecture(
            workflow_name="idempotent",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        first = self.artifacts.read_json(first_ref)

        second_ref = self.service.snapshot_architecture(
            workflow_name="idempotent",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        second = self.artifacts.read_json(second_ref)

        self.assertEqual(second["skeleton_commit_sha"], first["skeleton_commit_sha"])
        self.assertEqual(second["changed_paths"], first["changed_paths"])
        self.assertEqual(
            _git(workspace.worktree, "rev-list", "--count", f"{workspace.base_sha}..HEAD").strip(),
            "1",
        )

        restarted = GitBackedSkeletonService(self.runtime_root, self.artifacts)
        recovered = restarted.provision_architecture_workspace(
            workflow_id="idempotent",
            revision_name="revision-two",
            workspace={"repo_path": str(self.repo)},
            requirements_ref=self.requirements_ref,
            base_artifact=first,
        )
        self.assertEqual(recovered.original_head, workspace.original_head)
        self.assertEqual(recovered.source_fingerprint, workspace.source_fingerprint)
        self.assertEqual(recovered.base_sha, first["skeleton_commit_sha"])

    def test_architect_cannot_mutate_git_history(self) -> None:
        workspace = self._provision_complete_workspace("git-mutation", "initial")
        _git(workspace.worktree, "add", "-A")
        _git(
            workspace.worktree,
            "-c",
            "user.name=Architect",
            "-c",
            "user.email=architect@example.invalid",
            "commit",
            "-qm",
            "architect-owned commit",
        )
        with self.assertRaisesRegex(ValueError, "manager-owned operations"):
            self.service.snapshot_architecture(
                workflow_name="git-mutation",
                revision_name="initial",
                architecture_workspace=workspace,
                submission=self._submission(),
                requirements_ref=self.requirements_ref,
            )

    def test_execution_epoch_starts_every_node_from_skeleton_and_handoff_hides_manager_identity(self) -> None:
        workspace = self._provision_complete_workspace("execution", "initial")
        skeleton_ref = self.service.snapshot_architecture(
            workflow_name="execution",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        skeleton = self.artifacts.read_json(skeleton_ref)
        architecture = ArchitectureArtifactService(self.artifacts, self.repository)
        repair_ref = self.artifacts.put_json(
            {
                "workflow_id": "hidden-workflow",
                "node_run_id": "hidden-node",
                "candidate_digest": "hidden-candidate",
                "finding_fingerprint": "hidden-fingerprint",
                "module_name": "router",
                "defect_kind": "module_defect",
                "severity": "major",
                "finding_section": "invariant",
                "finding_summary": "Routing order changes after reset.",
                "failure_reason": "The reset probe returns a different route.",
                "case_name": "reset preserves deterministic routing",
                "requirements": [
                    {"section": "Routing", "requirement": "Route matching must be deterministic."}
                ],
                "locations": [{"path": "src/router.cpp", "symbol": "reset"}],
                "invariants": ["Reset preserves route ordering."],
                "expected": {"route": "first"},
                "actual": {"route": "second"},
                "suggested_repair_boundary": ["src/router.cpp"],
            },
            artifact_type="RepairBillArtifact",
        )
        compilation = ExecutionCompiler(self.repository, architecture).compile_epoch(
            workflow_id="execution",
            epoch_id="execution-epoch",
            manifest_ref=skeleton_ref,
            initial_repair_bill_ref=repair_ref.to_dict(),
        )

        for node_id in compilation.node_run_ids:
            node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
            self.assertIsNotNone(node)
            assert node is not None
            self.assertEqual(_git(Path(node.payload["workspace_path"]), "rev-parse", "HEAD").strip(), skeleton["skeleton_commit_sha"])

        router = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, compilation.unit_node_ids["router"])
        assert router is not None
        self.assertEqual(router.payload["historical_repair_bill_refs"], [repair_ref.to_dict()])
        work_view_ref = UnitWorkViewBuilder(architecture).build(router, dependency_outputs={})
        work_view = self.artifacts.read_json(work_view_ref)
        encoded = json.dumps(work_view, sort_keys=True)
        self.assertEqual(work_view["module_name"], "router")
        self.assertNotIn("requirements", work_view)
        self.assertNotIn("coverage_claims", work_view)
        self.assertNotIn("contract_consumption", work_view)
        self.assertEqual(
            work_view["historical_repair_bills"][0]["case_name"],
            "reset preserves deterministic routing",
        )
        for forbidden in ("workflow_id", "revision_id", "node_run_id", "epoch_id", "sha256"):
            self.assertNotIn(forbidden, encoded)
        for forbidden_value in ("hidden-workflow", "hidden-node", "hidden-candidate", "hidden-fingerprint"):
            self.assertNotIn(forbidden_value, encoded)

    def test_scenario_verification_artifact_closes_final_candidate_union(self) -> None:
        workspace = self._provision_complete_workspace("module-union", "initial")
        skeleton_ref = self.service.snapshot_architecture(
            workflow_name="module-union",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        architecture = ArchitectureArtifactService(self.artifacts, self.repository)
        compilation = ExecutionCompiler(self.repository, architecture).compile_epoch(
            workflow_id="module-union",
            epoch_id="module-union-epoch",
            manifest_ref=skeleton_ref,
        )
        self.assertEqual(
            DagScheduler(self.repository).schedule_ready_nodes(
                workflow_id="module-union",
                epoch_id="module-union-epoch",
                max_new_nodes=1,
            ),
            (compilation.unit_node_ids["router"],),
        )
        router_id = compilation.unit_node_ids["router"]
        router = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, router_id)
        assert router is not None
        router_worktree = Path(router.payload["workspace_path"])
        (router_worktree / "src" / "router.cpp").write_text(
            "int route_rule() { return 17; }\n", encoding="utf-8"
        )
        _git(router_worktree, "add", "src/router.cpp")
        _git(
            router_worktree,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "router candidate",
        )
        candidate_digest = _git(router_worktree, "rev-parse", "HEAD").strip()
        candidate_ref = self.artifacts.put_json(
            {
                "base_sha": router.payload["base_sha"],
                "candidate_digest": candidate_digest,
                "changed_paths": ["src/router.cpp"],
            },
            artifact_type="CandidateSnapshotArtifact",
        )
        verification_ref = self.artifacts.put_json(
            {"status": "PASS", "candidate_digest": candidate_digest},
            artifact_type="VerificationArtifact",
        )
        self._accept_candidate(
            router_id,
            candidate_ref=candidate_ref.to_dict(),
            candidate_digest=candidate_digest,
            verification_ref=verification_ref.to_dict(),
        )
        scenario_ref = self.artifacts.put_json(
            {"status": "PASS", "scenario": "router_end_to_end"},
            artifact_type="VerificationArtifact",
        )
        scenario_id = compilation.verification_node_ids["router_end_to_end"]
        self._accept_verification_scenario(
            scenario_id,
            dependency_node_ids=[router_id],
            verification_ref=scenario_ref.to_dict(),
            scenario_fingerprint="router-end-to-end-fingerprint",
        )
        epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, "module-union-epoch")
        assert epoch is not None
        self.repository.dispatch(
            ActionEnvelope(
                action_type="ALL_REQUIRED_NODES_ACCEPTED",
                workflow_id=epoch.workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id=epoch.aggregate_id,
                actor="test",
                expected_version=epoch.version,
                idempotency_key="module-union:ready-to-publish",
                payload={
                    "accepted_candidate_refs": [candidate_ref.to_dict()],
                    "verification_artifact_refs": [scenario_ref.to_dict()],
                },
            )
        )
        publish_result = asyncio.run(
            MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root)).execute_semantic_effect(
                {
                    "effect_type": "publish_final_deliverable",
                    "effect_id": "module-union:publish",
                    "workflow_id": "module-union",
                    "aggregate_type": AggregateType.EXECUTION_EPOCH.value,
                    "aggregate_id": "module-union-epoch",
                }
            )
        )
        published = self.artifacts.read_json(publish_result["result_artifact_ref"])
        self.assertEqual(published["verification_refs"], [scenario_ref.to_dict()])
        self.assertEqual(
            published["scenario_fingerprints"],
            {"router_end_to_end": "router-end-to-end-fingerprint"},
        )
        self.assertEqual(
            (Path(router.payload["common_git_dir"]).parent / "worktrees" / router.payload["workflow_key"] / "publish" / "src" / "router.cpp").read_text(encoding="utf-8"),
            "int route_rule() { return 17; }\n",
        )

    def test_skeleton_builder_schema_contains_semantics_not_manager_identity(self) -> None:
        encoded = json.dumps(SKELETON_BUILDER_TOOL_SPECS, sort_keys=True)
        self.assertIn("contract_paths", encoded)
        self.assertIn("op_minion_architecture_module_upsert", encoded)
        self.assertIn("op_minion_architecture_submit", encoded)
        self.assertNotIn("architecture_verification", encoded)
        self.assertNotIn("consume_contract", encoded)
        self.assertNotIn("cover_requirement", encoded)
        self.assertNotIn('"items": {"type": "object"}', encoded)
        self.assertNotIn('"prefix"', encoded)
        for forbidden in (
            "workflow_id",
            "revision_id",
            "requirement_id",
            "evidence_id",
            "finding_id",
            "artifact_ref",
            "sha256",
        ):
            self.assertNotIn(f'"{forbidden}"', encoded)

    def test_architecture_submit_needs_no_semantic_coverage_index(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        requirements_path = self.runtime_root / "requirements.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        workspace = self._bind_builder_workspace({
            "repo_path": str(self.repo),
            "reference_paths": [{"name": "requirements", "path": str(requirements_path)}],
            "artifact_dir": str(self.runtime_root / "artifacts"),
            "artifact_stage_dir": str(self.runtime_root / "artifact-stage"),
        }, role="architect")
        produced: list[dict[str, object]] = []
        self._author_submission(workspace, self._submission())
        submitted = self._builder_call(
            workspace, "op_minion_architecture_submit", produced=produced
        )
        self.assertTrue(submitted.ok, submitted.llm_text)
        self.assertEqual(len(produced), 1)
        self.assertNotIn("warnings", submitted.structured)
        artifact = json.loads(
            (Path(workspace["artifact_stage_dir"]) / "architecture_submission.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(set(artifact), {"modules", "scenarios"})

    def test_architecture_submit_rejects_undeclared_changed_path_before_quiescing(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        base_sha = _git(self.repo, "rev-parse", "HEAD").strip()
        (self.repo / "README.md").write_text("architect changed this\n", encoding="utf-8")
        requirements_path = self.runtime_root / "requirements-live-paths.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        workspace = self._bind_builder_workspace(
            {
                "repo_path": str(self.repo),
                "architecture_base_sha": base_sha,
                "reference_paths": [
                    {"name": "requirements", "path": str(requirements_path)}
                ],
                "artifact_dir": str(self.runtime_root / "live-path-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "live-path-stage"),
            },
            role="architect",
        )
        self._author_submission(workspace, self._submission())
        produced: list[dict[str, object]] = []

        rejected = self._builder_call(
            workspace,
            "op_minion_architecture_submit",
            produced=produced,
        )

        self.assertFalse(rejected.ok)
        self.assertIn("outside declared", rejected.llm_text)
        self.assertIn("README.md", rejected.llm_text)
        self.assertEqual(produced, [])
        self.assertFalse(
            (Path(workspace["artifact_stage_dir"]) / "architecture_submission.json").exists()
        )

    def test_architecture_review_fail_keeps_semantic_locations_in_markdown(self) -> None:
        requirements_path = self.runtime_root / "review-requirements.json"
        architecture_path = self.runtime_root / "review-architecture.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        architecture_path.write_text(
            json.dumps({"modules": {"router": self._submission()["modules"]["router"]}}),
            encoding="utf-8",
        )
        workspace = self._bind_builder_workspace({
            "repo_path": str(self.repo),
            "reference_paths": [
                {"name": "requirements", "path": str(requirements_path)},
                {"name": "architecture_index", "path": str(architecture_path)},
            ],
            "artifact_dir": str(self.runtime_root / "review-artifacts"),
            "artifact_stage_dir": str(self.runtime_root / "review-stage"),
        }, role="architecture_reviewer")
        result = self._builder_call(
            workspace,
            "op_minion_architecture_review_fail",
            {
                "findings": (
                    "## [MAJOR] invented_router contract is incomplete\n\n"
                    "Requirement: Route matching must be deterministic.\n\n"
                    "Evidence: `README.md` does not expose the required consumer contract."
                ),
            },
        )
        self.assertTrue(result.ok, result.text)
        artifact = json.loads(
            (Path(workspace["artifact_stage_dir"]) / "architecture_review.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertIn("invented_router", artifact["findings_markdown"])
        self.assertNotIn("findings", artifact)

    def test_architecture_review_finding_accepts_semantic_task_source_citation(self) -> None:
        requirements_path = self.runtime_root / "review-requirements.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        workspace = self._bind_builder_workspace({
            "repo_path": str(self.repo),
            "reference_paths": [{"name": "requirements", "path": str(requirements_path)}],
            "artifact_dir": str(self.runtime_root / "review-artifacts"),
            "artifact_stage_dir": str(self.runtime_root / "review-stage"),
        }, role="architecture_reviewer")
        result = self._builder_call(
            workspace,
            "op_minion_architecture_review_fail",
            {
                "findings": (
                    "## [BLOCKER] Requirement narrowed\n\n"
                    "The binding source `sources/TASK.md` requires deterministic routing, "
                    "but the Skeleton omits that behavior."
                ),
            },
        )
        self.assertTrue(result.ok, result.llm_text)
        artifact = json.loads(
            (Path(workspace["artifact_stage_dir"]) / "architecture_review.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(artifact["verdict"], "FAIL")
        self.assertIn("sources/TASK.md", artifact["findings_markdown"])

    def test_architecture_review_submit_needs_no_positive_audit_bookkeeping(self) -> None:
        workspace = self._review_builder_workspace()

        result = self._builder_call(workspace, "op_minion_architecture_review_pass")

        self.assertTrue(result.ok, result.llm_text)
        artifact = json.loads(
            (Path(workspace["artifact_stage_dir"]) / "architecture_review.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(artifact["verdict"], "PASS")
        self.assertEqual(artifact["review_scope"]["module_names"], ["router"])
        self.assertNotIn("verification_node_names", artifact["review_scope"])

    def test_architecture_review_finding_compiles_fail(self) -> None:
        workspace = self._review_builder_workspace()
        submitted = self._builder_call(
            workspace,
            "op_minion_architecture_review_fail",
            {
                "findings": (
                    "## [MAJOR] router result flow is missing\n\n"
                    "Requirement: Route matching must be deterministic.\n\n"
                    "Evidence: `include/router.h` does not expose the consumer-visible result flow."
                ),
            },
        )
        self.assertTrue(submitted.ok, submitted.llm_text)
        artifact = json.loads(
            (Path(workspace["artifact_stage_dir"]) / "architecture_review.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(artifact["verdict"], "FAIL")
        self.assertIn("router", artifact["findings_markdown"])

    def test_architecture_review_tool_contract_binds_only_module_catalog(self) -> None:
        contract = compile_architecture_review_invocation_tool_contract(
            task_sources={},
            architecture=self._submission(),
        )

        self.assertNotIn("hard_requirements", contract)
        self.assertEqual(contract["module_names"], ["router"])
        self.assertNotIn("verification_node_names", contract)
        descriptions = json.dumps(contract["description_overrides"], ensure_ascii=False)
        self.assertIn("router", descriptions)
        self.assertIn("where private state/resources live", descriptions)
        self.assertIn("syntax and compilation are not semantic proof", descriptions)
        self.assertIn("missing legal storage seam", descriptions)
        self.assertNotIn("workflow_id", descriptions)

    def test_revision_submit_merges_semantic_patch_and_rejects_out_of_scope_paths(self) -> None:
        (self.repo / "include").mkdir()
        contract_path = self.repo / "include" / "router.h"
        contract_path.write_text(_contract("router"), encoding="utf-8")
        _git(self.repo, "add", "include/router.h")
        _git(
            self.repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "add architecture contract",
        )
        base_sha = _git(self.repo, "rev-parse", "HEAD").strip()
        base = self._submission()
        finding = {
            "findings": [
                {
                    "finding_kind": "contract_defect",
                    "summary": "Clarify the public router contract.",
                    "affected_modules": ["router"],
                    "locations": [{"path": "include/router.h", "section": "Compatibility"}],
                }
            ]
        }
        requirements_path = self.runtime_root / "revision-requirements.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        workspace = self._bind_builder_workspace({
            "repo_path": str(self.repo),
            "reference_paths": [{"name": "requirements", "path": str(requirements_path)}],
            "artifact_dir": str(self.runtime_root / "revision-artifacts"),
            "artifact_stage_dir": str(self.runtime_root / "revision-stage"),
            "architecture_revision_base_submission": base,
            "architecture_revision_scope": architecture_revision_scope(base, finding),
            "architecture_revision_base_sha": base_sha,
        }, role="architect")
        contract_path.write_text(_contract("router") + "// clarified\n", encoding="utf-8")
        produced: list[dict[str, object]] = []
        result = self._builder_call(
            workspace, "op_minion_architecture_submit", produced=produced
        )
        self.assertTrue(result.ok, result.text)
        merged = json.loads(Path(produced[0]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(set(merged["modules"]), {"router"})

        _git(self.repo, "checkout", "--", "include/router.h")
        (self.repo / "tests" / "test_router.cpp").write_text(
            "// unrelated revision\n", encoding="utf-8"
        )
        rejected_workspace = self._bind_builder_workspace(
            {
                **{key: value for key, value in workspace.items() if key != "minion_v2"},
                "artifact_dir": str(self.runtime_root / "revision-rejected-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "revision-rejected-stage"),
            },
            role="architect",
        )
        rejected = self._builder_call(rejected_workspace, "op_minion_architecture_submit")
        self.assertFalse(rejected.ok)
        self.assertIn("outside the finding scope", rejected.text)

    def test_revision_submit_accepts_scoped_source_change_with_unchanged_dag(self) -> None:
        (self.repo / "include").mkdir()
        contract_path = self.repo / "include" / "router.h"
        contract_path.write_text(_contract("router"), encoding="utf-8")
        _git(self.repo, "add", "include/router.h")
        _git(
            self.repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "add architecture contract",
        )
        base_sha = _git(self.repo, "rev-parse", "HEAD").strip()
        base = self._submission()
        finding = {
            "findings": [
                {
                    "finding_kind": "contract_defect",
                    "summary": "Clarify the public router contract.",
                    "affected_modules": ["router"],
                    "locations": [{"path": "include/router.h", "section": "Compatibility"}],
                }
            ]
        }
        requirements_path = self.runtime_root / "source-only-requirements.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        workspace = self._bind_builder_workspace(
            {
                "repo_path": str(self.repo),
                "reference_paths": [
                    {"name": "requirements", "path": str(requirements_path)}
                ],
                "artifact_dir": str(self.runtime_root / "source-only-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "source-only-stage"),
                "architecture_revision_base_submission": base,
                "architecture_revision_scope": architecture_revision_scope(base, finding),
                "architecture_revision_base_sha": base_sha,
            },
            role="architect",
        )
        contract_path.write_text(_contract("router") + "// clarified\n", encoding="utf-8")
        produced: list[dict[str, object]] = []

        result = self._builder_call(
            workspace,
            "op_minion_architecture_submit",
            produced=produced,
        )

        self.assertTrue(result.ok, result.text)
        self.assertEqual(
            json.loads(Path(produced[0]["path"]).read_text(encoding="utf-8")),
            base,
        )

    def test_revision_submit_rejects_revision_with_no_source_or_dag_change(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        _git(self.repo, "add", "include/router.h")
        _git(
            self.repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "add architecture contract",
        )
        base = self._submission()
        finding = {
            "findings": [
                {
                    "finding_kind": "contract_defect",
                    "summary": "Clarify the public router contract.",
                    "affected_modules": ["router"],
                    "locations": [{"path": "include/router.h"}],
                }
            ]
        }
        requirements_path = self.runtime_root / "unchanged-revision-requirements.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        workspace = self._bind_builder_workspace(
            {
                "repo_path": str(self.repo),
                "reference_paths": [
                    {"name": "requirements", "path": str(requirements_path)}
                ],
                "artifact_dir": str(self.runtime_root / "unchanged-revision-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "unchanged-revision-stage"),
                "architecture_revision_base_submission": base,
                "architecture_revision_scope": architecture_revision_scope(base, finding),
                "architecture_revision_base_sha": _git(
                    self.repo, "rev-parse", "HEAD"
                ).strip(),
            },
            role="architect",
        )

        result = self._builder_call(workspace, "op_minion_architecture_submit")

        self.assertFalse(result.ok)
        self.assertIn("no source or semantic change", result.text)

    def test_revision_scope_is_rechecked_during_stable_snapshot(self) -> None:
        initial = self._provision_complete_workspace("stable-revision", "initial")
        base_ref = self.service.snapshot_architecture(
            workflow_name="stable-revision",
            revision_name="initial",
            architecture_workspace=initial,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        base_artifact = self.artifacts.read_json(base_ref)
        finding = {
            "findings": [
                {
                    "finding_kind": "contract_defect",
                    "summary": "Clarify the public router contract.",
                    "affected_modules": ["router"],
                    "locations": [
                        {"path": "include/router.h", "section": "Compatibility"}
                    ],
                }
            ]
        }
        scope = architecture_revision_scope(base_artifact["submission"], finding)
        revision = self.service.provision_architecture_workspace(
            workflow_id="stable-revision",
            revision_name="allowed",
            workspace={"repo_path": str(self.repo)},
            requirements_ref=self.requirements_ref,
            base_artifact=base_artifact,
        )
        (revision.worktree / "include" / "router.h").write_text(
            _contract("router") + "// clarified\n",
            encoding="utf-8",
        )
        revised_submission = json.loads(json.dumps(base_artifact["submission"]))
        accepted_ref = self.service.snapshot_architecture(
            workflow_name="stable-revision",
            revision_name="allowed",
            architecture_workspace=revision,
            submission=revised_submission,
            requirements_ref=self.requirements_ref,
            revision_base_artifact=base_artifact,
            revision_scope=scope,
        )
        self.assertEqual(
            self.artifacts.read_json(accepted_ref)["submission"],
            revised_submission,
        )

        rejected_revision = self.service.provision_architecture_workspace(
            workflow_id="stable-revision",
            revision_name="rejected",
            workspace={"repo_path": str(self.repo)},
            requirements_ref=self.requirements_ref,
            base_artifact=base_artifact,
        )
        (rejected_revision.worktree / "tests" / "test_router.cpp").write_text(
            "// unrelated revision\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "outside the finding scope"):
            self.service.snapshot_architecture(
                workflow_name="stable-revision",
                revision_name="rejected",
                architecture_workspace=rejected_revision,
                submission=revised_submission,
                requirements_ref=self.requirements_ref,
                revision_base_artifact=base_artifact,
                revision_scope=scope,
            )

    def test_replan_batch_scope_uses_repair_bill_module_and_boundary(self) -> None:
        base = self._submission()
        scope = architecture_revision_scope(
            base,
            {
                "finding_groups": [
                    {
                        "finding_fingerprint": "keyboard-abi",
                        "defect_kind": "contract_defect",
                    }
                ],
                "findings": [
                    {
                        "finding_kind": "contract_defect",
                        "summary": "The public router declaration has the wrong linkage.",
                        "module_name": "router",
                        "suggested_repair_boundary": ["include/router.h"],
                    }
                ],
            },
        )

        self.assertEqual(scope["affected_modules"], ["router"])
        self.assertEqual(scope["allowed_paths"], ["include/router.h"])
        self.assertFalse(scope["allow_topology_changes"])

    def test_module_upsert_fully_replaces_one_dag_node(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        requirements_path = self.runtime_root / "reference-upsert-requirements.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        base = self._submission()
        workspace = self._bind_builder_workspace(
            {
                "repo_path": str(self.repo),
                "reference_paths": [
                    {"name": "requirements", "path": str(requirements_path)}
                ],
                "artifact_dir": str(self.runtime_root / "reference-upsert-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "reference-upsert-stage"),
                "architecture_revision_base_submission": base,
                "architecture_revision_base_sha": _git(
                    self.repo, "rev-parse", "HEAD"
                ).strip(),
            },
            role="architect",
        )
        produced: list[dict[str, object]] = []

        result = self._builder_call(
            workspace,
            "op_minion_architecture_module_upsert",
            {
                "name": "router",
                "module_kind": "implementation",
                "contract_mode": "file_frozen",
                "contract_dependencies": [],
                "contract_paths": ["include/router.h"],
                "implementation_files": ["src/router.cpp"],
                "test_files": ["tests/test_router.cpp"],
                "reference_only": ["README.md"],
            },
        )
        self.assertTrue(result.ok, result.text)
        submitted = self._builder_call(
            workspace,
            "op_minion_architecture_submit",
            produced=produced,
        )

        self.assertTrue(submitted.ok, submitted.text)
        payload = json.loads(Path(produced[0]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(
            payload["modules"]["router"]["paths"]["reference_only"],
            ["README.md"],
        )
        self.assertEqual(
            set(payload["modules"]["router"]),
            {"module_kind", "contract_dependencies", "paths"},
        )

    def test_rejected_candidate_path_baseline_ignores_preexisting_revision_changes(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        _git(self.repo, "add", "include/router.h")
        _git(
            self.repo,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "add contract",
        )
        base_sha = _git(self.repo, "rev-parse", "HEAD").strip()

        # This change belongs to the original Human Edit and predates the
        # stable-snapshot finding.
        (self.repo / "src" / "router.cpp").write_text(
            "// revised implementation skeleton\n", encoding="utf-8"
        )
        rejected_states = architecture_revision_path_states(self.repo, base_sha)

        self.assertEqual(
            architecture_revision_changed_paths_since(
                self.repo,
                base_sha,
                rejected_states,
            ),
            [],
        )

        (self.repo / "include" / "router.h").write_text(
            _contract("router") + "// local repair\n", encoding="utf-8"
        )
        self.assertEqual(
            architecture_revision_changed_paths_since(
                self.repo,
                base_sha,
                rejected_states,
            ),
            ["include/router.h"],
        )

    def test_stable_snapshot_scopes_against_rejected_candidate_not_revision_origin(self) -> None:
        initial = self._provision_complete_workspace("repair-baseline", "initial")
        base_ref = self.service.snapshot_architecture(
            workflow_name="repair-baseline",
            revision_name="initial",
            architecture_workspace=initial,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        base_artifact = self.artifacts.read_json(base_ref)
        revision = self.service.provision_architecture_workspace(
            workflow_id="repair-baseline",
            revision_name="revision",
            workspace={"repo_path": str(self.repo)},
            requirements_ref=self.requirements_ref,
            base_artifact=base_artifact,
        )

        # A broad, already-submitted Human Edit changed source before stable
        # review narrowed the repair to the contract path.
        (revision.worktree / "src" / "router.cpp").write_text(
            "// broad human edit\n", encoding="utf-8"
        )
        rejected_submission = json.loads(json.dumps(base_artifact["submission"]))
        rejected_states = architecture_revision_path_states(
            revision.worktree,
            revision.base_sha,
        )
        finding = {
            "finding_kind": "contract_defect",
            "summary": "Clarify the frozen router contract.",
            "affected_modules": ["router"],
            "locations": [{"path": "include/router.h", "section": "Contract"}],
        }
        revised_submission = json.loads(json.dumps(rejected_submission))
        (revision.worktree / "include" / "router.h").write_text(
            _contract("router") + "// local repair\n", encoding="utf-8"
        )

        accepted_ref = self.service.snapshot_architecture(
            workflow_name="repair-baseline",
            revision_name="revision",
            architecture_workspace=revision,
            submission=revised_submission,
            requirements_ref=self.requirements_ref,
            revision_base_artifact={"submission": rejected_submission},
            revision_scope=architecture_revision_scope(rejected_submission, finding),
            revision_base_path_states=rejected_states,
        )

        accepted = self.artifacts.read_json(accepted_ref)
        self.assertEqual(accepted["submission"], revised_submission)
        self.assertIn("src/router.cpp", accepted["changed_paths"])
        self.assertIn("include/router.h", accepted["changed_paths"])

    def test_human_edit_revision_is_patch_only_without_inventing_a_machine_scope(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        requirements_path = self.runtime_root / "human-edit-requirements.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        workspace = self._bind_builder_workspace({
            "repo_path": str(self.repo),
            "reference_paths": [{"name": "requirements", "path": str(requirements_path)}],
            "artifact_dir": str(self.runtime_root / "human-edit-artifacts"),
            "artifact_stage_dir": str(self.runtime_root / "human-edit-stage"),
            "architecture_revision_base_submission": self._submission(),
            "architecture_revision_base_sha": _git(self.repo, "rev-parse", "HEAD").strip(),
        }, role="architect")
        produced: list[dict[str, object]] = []
        (self.repo / "include" / "router.h").write_text(
            _contract("router") + "// human clarification\n", encoding="utf-8"
        )
        result = self._builder_call(
            workspace, "op_minion_architecture_submit", produced=produced
        )

        self.assertTrue(result.ok, result.text)
        merged = json.loads(Path(produced[0]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(set(merged["modules"]), {"router"})

    def test_architecture_defect_scope_allows_a_new_declared_module_only(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        (self.repo / "include" / "route_status.h").write_text(
            _contract("route_status").replace("class RuleRouter;", "struct RouteStatus;"),
            encoding="utf-8",
        )
        base = self._submission()
        finding = {
            "findings": [
                {
                    "finding_kind": "architecture_defect",
                    "summary": "Separate the shared route result shape from router behavior.",
                    "affected_modules": ["router"],
                    "locations": [{"path": "include/router.h", "section": "Provides"}],
                }
            ]
        }
        requirements_path = self.runtime_root / "topology-requirements.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        workspace = self._bind_builder_workspace({
            "repo_path": str(self.repo),
            "reference_paths": [{"name": "requirements", "path": str(requirements_path)}],
            "artifact_dir": str(self.runtime_root / "topology-artifacts"),
            "artifact_stage_dir": str(self.runtime_root / "topology-stage"),
            "architecture_revision_base_submission": base,
            "architecture_revision_scope": architecture_revision_scope(base, finding),
            "architecture_revision_base_sha": _git(self.repo, "rev-parse", "HEAD").strip(),
        }, role="architect")
        produced: list[dict[str, object]] = []

        updated = self._builder_call(
            workspace,
            "op_minion_architecture_module_upsert",
            {
                "name": "route_status",
                "module_kind": "contract_only",
                "contract_mode": "file_frozen",
                "contract_dependencies": [],
                "contract_paths": ["include/route_status.h"],
            },
        )
        self.assertTrue(updated.ok, updated.text)
        result = self._builder_call(
            workspace, "op_minion_architecture_submit", produced=produced
        )

        self.assertTrue(result.ok, result.text)
        merged = json.loads(Path(produced[0]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(set(merged["modules"]), {"router", "route_status"})

    def test_software_workflow_imports_skeleton_and_rejects_legacy_contract_graph(self) -> None:
        workspace = self._provision_complete_workspace("external", "initial")
        skeleton_ref = self.service.snapshot_architecture(
            workflow_name="external",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        workflows = MinionV2WorkflowService(self.runtime_root)
        workflows.create_task(
            {
                "task_id": "external-task",
                "title": "External tiny router skeleton",
                "objective": "Implement the accepted tiny router skeleton",
                "family_id": "software_engineering",
                "workspace": {"kind": "existing_repo", "repo_path": str(self.repo)},
            }
        )
        started = workflows.start_workflow(
            {
                "task_id": "external-task",
                "operation": "review_then_execute",
                "artifact_ref": skeleton_ref.to_dict(),
            }
        )
        self.assertEqual(started["state"], "CREATED")
        workflow = workflows.repository.read_snapshot(
            AggregateType.WORKFLOW,
            str(started["workflow_id"]),
        )
        request = workflows.artifacts.read_json(dict(workflow.payload["request_ref"]))
        self.assertEqual(request["requirements_ref"], self.requirements_ref.to_dict())
        other_requirements_ref = workflows.task_sources.publish(
            title="Different task source",
            request_text="Implement different behavior.\n",
            workspace={"repo_path": str(self.repo)},
            actor="test",
            source_channel="test",
        )
        with self.assertRaisesRegex(
            ValueError,
            "task sources differ from the imported architecture",
        ):
            workflows.start_workflow(
                {
                    "task_id": "external-task",
                    "operation": "review_then_execute",
                    "artifact_ref": skeleton_ref.to_dict(),
                    "requirements_ref": other_requirements_ref.to_dict(),
                }
            )
        repair = workflows.start_workflow(
            {
                "task_id": "external-task",
                "operation": "review_and_repair",
                "artifact_ref": skeleton_ref.to_dict(),
            }
        )
        self.assertEqual(repair["state"], "CREATED")

        legacy_ref = self.artifacts.put_json(
            {"legacy": True},
            artifact_type="ArchitectureContractArtifact",
        )
        with self.assertRaisesRegex(ValueError, "legacy SWE JSON contract graph"):
            workflows.start_workflow(
                {
                    "task_id": "external-task",
                    "operation": "review_then_execute",
                    "artifact_ref": legacy_ref.to_dict(),
                }
            )
        with self.assertRaisesRegex(ValueError, "legacy SWE JSON contract graph"):
            workflows.start_workflow(
                {
                    "task_id": "external-task",
                    "operation": "review_and_repair",
                    "artifact_ref": legacy_ref.to_dict(),
                }
            )

    def test_candidate_reuse_transplants_module_diff_and_stops_when_contract_changes(self) -> None:
        workspace = self._provision_complete_workspace("reuse", "initial")
        initial_ref = self.service.snapshot_architecture(
            workflow_name="reuse",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        architecture = ArchitectureArtifactService(self.artifacts, self.repository)
        compiler = ExecutionCompiler(self.repository, architecture)
        source = compiler.compile_epoch(
            workflow_id="reuse",
            epoch_id="reuse-source",
            manifest_ref=initial_ref,
        )
        source_node_id = source.unit_node_ids["router"]
        DagScheduler(self.repository).schedule_ready_nodes(
            workflow_id="reuse", epoch_id="reuse-source", max_new_nodes=1
        )
        source_node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, source_node_id)
        assert source_node is not None
        source_worktree = Path(source_node.payload["workspace_path"])
        (source_worktree / "src").mkdir(exist_ok=True)
        (source_worktree / "src" / "router.cpp").write_text(
            "int route_rule() { return 1; }\n", encoding="utf-8"
        )
        _git(source_worktree, "add", "src/router.cpp")
        _git(
            source_worktree,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "router candidate",
        )
        source_candidate_digest = _git(source_worktree, "rev-parse", "HEAD").strip()
        candidate_ref = self.artifacts.put_json(
            {
                "base_sha": source_node.payload["base_sha"],
                "candidate_digest": source_candidate_digest,
                "changed_paths": ["src/router.cpp"],
            },
            artifact_type="CandidateSnapshotArtifact",
        )
        verification_ref = self.artifacts.put_json(
            {"status": "PASS"}, artifact_type="VerificationArtifact"
        )
        self._accept_candidate(
            source_node_id,
            candidate_ref=candidate_ref.to_dict(),
            candidate_digest=source_candidate_digest,
            verification_ref=verification_ref.to_dict(),
        )

        reused = compiler.compile_epoch(
            workflow_id="reuse",
            epoch_id="reuse-target",
            manifest_ref=initial_ref,
            reuse_from_epoch_id="reuse-source",
        )
        reused_node = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN, reused.unit_node_ids["router"]
        )
        assert reused_node is not None
        self.assertEqual(reused_node.state, "ACCEPTED")
        self.assertTrue((Path(reused_node.payload["workspace_path"]) / "src" / "router.cpp").is_file())
        self.assertNotEqual(reused_node.payload["candidate_digest"], source_candidate_digest)

        initial = self.artifacts.read_json(initial_ref)
        changed_workspace = self.service.provision_architecture_workspace(
            workflow_id="reuse",
            revision_name="changed-contract",
            workspace={"repo_path": str(self.repo)},
            requirements_ref=self.requirements_ref,
            base_artifact=initial,
        )
        changed_contract = _contract("router").replace(
            "Existing public signatures remain stable.",
            "Existing public signatures and result ordering remain stable.",
        )
        (changed_workspace.worktree / "include" / "router.h").write_text(
            changed_contract, encoding="utf-8"
        )
        changed_ref = self.service.snapshot_architecture(
            workflow_name="reuse",
            revision_name="changed-contract",
            architecture_workspace=changed_workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        changed = compiler.compile_epoch(
            workflow_id="reuse",
            epoch_id="reuse-changed",
            manifest_ref=changed_ref,
            reuse_from_epoch_id="reuse-source",
        )
        changed_node = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN, changed.unit_node_ids["router"]
        )
        assert changed_node is not None
        self.assertEqual(changed_node.state, "BLOCKED_BY_DEPS")

    def test_human_review_publish_persists_and_reuses_the_card(self) -> None:
        workspace = self._provision_complete_workspace("human-card", "initial")
        manifest_ref = self.service.snapshot_architecture(
            workflow_name="human-card",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        workflows = MinionV2WorkflowService(self.runtime_root)
        workflow_id = "wf_human_card"
        revision_id = "arch_human_card"
        workflows.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="nathan",
                source_channel="socket:test",
                expected_version=0,
                idempotency_key="create-human-card",
                payload={
                    "owner": "nathan",
                    "active_channel": "socket:test",
                    "control_route": {"channel_kind": "socket", "endpoint_id": "test"},
                },
            )
        )
        workflows.repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="nathan",
                expected_version=1,
                idempotency_key="start-human-card",
            )
        )
        review_ref = workflows.artifacts.put_json(
            {"verdict": "PASS", "findings": []},
            artifact_type="ArchitectureReviewArtifact",
        )
        sequence = [
            ("CREATE_ARCHITECTURE_REVISION", {"requirements_ref": self.requirements_ref.to_dict()}),
            ("START_ARCHITECT", {"fencing_token": 1, "active_worker_id": "inv-architect"}),
            (
                "ARCHITECT_SUBMITTED",
                {
                    "requirements_ref": self.requirements_ref.to_dict(),
                    "pending_architecture_submission_ref": {"sha256": "pending-architecture"},
                    "architecture_workspace_path": str(workspace.worktree),
                    "fencing_token": 1,
                },
            ),
            (
                "ARCHITECT_QUIESCED",
                {
                    "fencing_token": 1,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "architecture-tree",
                },
            ),
            (
                "ARCHITECTURE_SNAPSHOTTED",
                {
                    "requirements_ref": self.requirements_ref.to_dict(),
                    "architecture_manifest_ref": manifest_ref.to_dict(),
                },
            ),
            ("START_ARCHITECTURE_REVIEW", {"fencing_token": 2, "active_worker_id": "inv-reviewer"}),
            (
                "ARCHITECTURE_REVIEW_PASSED",
                {
                    "review_artifact_ref": review_ref.to_dict(),
                    "architecture_manifest_ref": manifest_ref.to_dict(),
                },
            ),
        ]
        for action_type, payload in sequence:
            current = workflows.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision_id)
            workflows.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision_id,
                    actor="test",
                    expected_version=current.version if current is not None else 0,
                    idempotency_key=f"human-card:{action_type}",
                    payload=payload,
                )
            )
        published: list[dict[str, object]] = []
        worker = MinionV2SemanticWorker(
            workflows,
            publish_human_review=lambda payload: _record_async(published, payload),
        )
        effect = {
            "effect_type": "publish_human_architecture_review",
            "effect_id": "effect-human-card",
            "workflow_id": workflow_id,
            "aggregate_type": AggregateType.ARCHITECTURE_REVISION.value,
            "aggregate_id": revision_id,
        }

        first = asyncio.run(worker.execute_semantic_effect(effect))
        second = asyncio.run(worker.execute_semantic_effect({**effect, "effect_id": "effect-human-card-retry"}))

        current = workflows.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision_id)
        assert current is not None
        self.assertEqual(current.state, "HUMAN_REVIEW")
        self.assertEqual(
            current.payload["human_review_card_ref"]["sha256"],
            first["result_artifact_ref"]["sha256"],
        )
        self.assertEqual(first["result_artifact_ref"], second["result_artifact_ref"])
        self.assertEqual(len(published), 2)
        self.assertEqual(published[0]["card_ref"], published[1]["card_ref"])
        card = workflows.artifacts.read_json(first["result_artifact_ref"])
        attachment_names = {
            str(item["file_name"]): str(item["path"])
            for item in list(card.get("attachments") or [])
        }
        self.assertIn("architecture.md", attachment_names)
        self.assertIn("request.md", attachment_names)
        self.assertEqual(
            Path(attachment_names["request.md"]).read_text(encoding="utf-8"),
            "Route matching must be deterministic.\n"
            "Existing public signatures remain stable.\n",
        )
        projection_root = self.runtime_root / "data" / "minion" / "plan_revisions"
        plans = list(projection_root.rglob("plan.md"))
        self.assertEqual(len(plans), 1)
        self.assertIn("### router", plans[0].read_text(encoding="utf-8"))
        status = json.loads((plans[0].parent / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "reviewed_pending_human")
        self.assertTrue((plans[0].parent / "review.json").is_file())
        asyncio.run(
            worker.execute_semantic_effect(
                {
                    "effect_type": "materialize_plan_revision",
                    "effect_id": "effect-human-card-accepted",
                    "workflow_id": workflow_id,
                    "aggregate_type": AggregateType.ARCHITECTURE_REVISION.value,
                    "aggregate_id": revision_id,
                    "status": "accepted",
                }
            )
        )
        accepted_status = json.loads(
            (plans[0].parent / "status.json").read_text(encoding="utf-8")
        )
        self.assertEqual(accepted_status["status"], "accepted")

    def _provision_complete_workspace(self, workflow_id: str, revision_name: str):
        workspace = self.service.provision_architecture_workspace(
            workflow_id=workflow_id,
            revision_name=revision_name,
            workspace={"repo_path": str(self.repo)},
            requirements_ref=self.requirements_ref,
        )
        (workspace.worktree / "include").mkdir(exist_ok=True)
        (workspace.worktree / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        return workspace

    def _accept_candidate(
        self,
        node_id: str,
        *,
        candidate_ref: dict[str, object],
        candidate_digest: str,
        verification_ref: dict[str, object],
    ) -> None:
        sequence = [
            ("START_PRODUCING", {"fencing_token": 1}),
            ("SUBMIT_CANDIDATE", {"fencing_token": 1}),
            (
                "QUIESCE_COMPLETED",
                {
                    "fencing_token": 1,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "tree",
                },
            ),
            (
                "CANDIDATE_SNAPSHOTTED",
                {
                    "candidate_ref": candidate_ref,
                    "candidate_digest": candidate_digest,
                    "workspace_fingerprint": "tree",
                },
            ),
            ("START_REVIEW", {"fencing_token": 2}),
            (
                "SUBMIT_SEMANTIC_VERIFICATION",
                {"pending_verification_ref": {"sha256": "pending-verification"}},
            ),
            (
                "VERIFIER_QUIESCED",
                {
                    "fencing_token": 2,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "verification-tree",
                },
            ),
            (
                "REVIEW_PASSED",
                {
                    "verification_artifact_ref": verification_ref,
                    "output_hashes": {"public_surface": "router-v1"},
                },
            ),
        ]
        for action_type, payload in sequence:
            node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
            assert node is not None
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=node.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="test",
                    expected_version=node.version,
                    idempotency_key=f"{node_id}:{action_type}:{node.version}",
                    payload=payload,
                )
            )

    def _accept_verification_scenario(
        self,
        node_id: str,
        *,
        dependency_node_ids: list[str],
        verification_ref: dict[str, object],
        scenario_fingerprint: str,
    ) -> None:
        sequence = [
            (
                "VERIFICATION_DEPENDENCIES_ACCEPTED",
                {
                    "accepted_dependency_node_ids": dependency_node_ids,
                    "epoch_frozen": False,
                },
            ),
            (
                "VERIFICATION_PREPARED",
                {
                    "scenario_fingerprint": scenario_fingerprint,
                    "scenario_candidate_union_ref": {"sha256": "scenario-union"},
                    "scenario_commit_sha": "scenario-commit",
                    "verification_workspace_fingerprint": "scenario-tree",
                },
            ),
            ("START_SCENARIO_VERIFICATION", {"fencing_token": 3}),
            (
                "SUBMIT_SEMANTIC_VERIFICATION",
                {"pending_verification_ref": {"sha256": "pending-scenario-verification"}},
            ),
            (
                "VERIFIER_QUIESCED",
                {
                    "fencing_token": 3,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "scenario-verification-tree",
                },
            ),
            (
                "VERIFICATION_PASSED",
                {
                    "verification_artifact_ref": verification_ref,
                    "scenario_fingerprint": scenario_fingerprint,
                },
            ),
        ]
        for action_type, payload in sequence:
            node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
            assert node is not None
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=node.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="test",
                    expected_version=node.version,
                    idempotency_key=f"{node_id}:{action_type}:{node.version}",
                    payload=payload,
                )
            )

    def _submission(self) -> dict[str, object]:
        return {
            "modules": {
                "router": {
                    "module_kind": "implementation",
                    "contract_dependencies": [],
                    "paths": {
                        "contract_mode": "file_frozen",
                        "contract_paths": ["include/router.h"],
                        "implementation_scopes": [{"kind": "file", "path": "src/router.cpp"}],
                        "test_scopes": [{"kind": "file", "path": "tests/test_router.cpp"}],
                        "reference_only": [],
                    },
                }
            },
            "scenarios": {
                "router_end_to_end": {
                    "modules": ["router"],
                    "entrypoint": "tests/test_router.cpp",
                    "observable_behavior": "A consumer can route one rule through the public router contract.",
                    "environment": "Project host test environment",
                }
            },
        }

    def _review_builder_workspace(self) -> dict[str, object]:
        requirements_path = self.runtime_root / f"review-requirements-{self.builder_lease_index}.json"
        architecture_path = self.runtime_root / f"review-architecture-{self.builder_lease_index}.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        architecture_path.write_text(json.dumps(self._submission()), encoding="utf-8")
        return self._bind_builder_workspace(
            {
                "repo_path": str(self.repo),
                "reference_paths": [
                    {"name": "requirements", "path": str(requirements_path)},
                    {"name": "architecture_index", "path": str(architecture_path)},
                ],
                "artifact_dir": str(self.runtime_root / f"review-artifacts-{self.builder_lease_index}"),
                "artifact_stage_dir": str(self.runtime_root / f"review-stage-{self.builder_lease_index}"),
            },
            role="architecture_reviewer",
        )

    def _bind_builder_workspace(
        self,
        workspace: dict[str, object],
        *,
        role: str,
    ) -> dict[str, object]:
        self.builder_lease_index += 1
        invocation_id = f"inv_builder_{self.builder_lease_index}"
        resource = f"builder:{self.builder_lease_index}"
        lease = self.repository.claim_lease(resource, invocation_id, ttl_seconds=60)
        workspace.update(
            {
                "runtime_root": str(self.runtime_root),
                "minion_v2": {
                    "workflow_id": "wf_builder",
                    "invocation_id": invocation_id,
                    "lease_resource_key": resource,
                    "fencing_token": lease.fencing_token,
                    "role": role,
                    "authoring_input_fingerprint": f"input-{self.builder_lease_index}",
                    "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
                },
            }
        )
        return workspace

    def _builder_call(
        self,
        workspace: dict[str, object],
        name: str,
        args: dict[str, object] | None = None,
        produced: list[dict[str, object]] | None = None,
    ):
        self.builder_call_index += 1
        return skeleton_builder_tool_result(
            CanonicalToolCall(
                name=name,
                args=args or {},
                call_id=f"builder-call-{self.builder_call_index}",
            ),
            workspace,
            produced if produced is not None else [],
        )

    def _author_submission(
        self,
        workspace: dict[str, object],
        submission: dict[str, object],
    ) -> None:
        for module_name, raw_module in dict(submission["modules"]).items():
            module = dict(raw_module)
            paths = dict(module["paths"])
            args: dict[str, object] = {
                "name": module_name,
                "module_kind": module["module_kind"],
                "contract_mode": str(paths.get("contract_mode") or "file_frozen"),
                "contract_dependencies": list(module.get("contract_dependencies") or []),
                "contract_paths": list(paths.get("contract_paths") or []),
                "implementation_files": [
                    str(item["path"])
                    for item in list(paths.get("implementation_scopes") or [])
                    if str(dict(item).get("kind")) == "file"
                ],
                "implementation_directories": [
                    str(item["path"])
                    for item in list(paths.get("implementation_scopes") or [])
                    if str(dict(item).get("kind")) == "directory"
                ],
                "test_files": [
                    str(item["path"])
                    for item in list(paths.get("test_scopes") or [])
                    if str(dict(item).get("kind")) == "file"
                ],
                "test_directories": [
                    str(item["path"])
                    for item in list(paths.get("test_scopes") or [])
                    if str(dict(item).get("kind")) == "directory"
                ],
                "reference_only": list(paths.get("reference_only") or []),
            }
            result = self._builder_call(workspace, "op_minion_architecture_module_upsert", args)
            self.assertTrue(result.ok, result.text)
        for scenario_name, raw_scenario in dict(submission.get("scenarios") or {}).items():
            scenario = dict(raw_scenario)
            result = self._builder_call(
                workspace,
                "op_minion_architecture_scenario_upsert",
                {"name": scenario_name, **scenario},
            )
            self.assertTrue(result.ok, result.text)


if __name__ == "__main__":
    unittest.main()
