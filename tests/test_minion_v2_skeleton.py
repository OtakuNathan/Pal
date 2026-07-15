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
from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.contracts import ActionEnvelope, AggregateType
from pal.minion.v2.execution import DagScheduler, ExecutionCompiler, UnitWorkViewBuilder
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.skeleton import (
    ARCHITECTURE_SKELETON_ARTIFACT,
    GitBackedSkeletonService,
    SemanticReferenceError,
    architecture_revision_changed_paths_since,
    architecture_revision_path_states,
    architecture_revision_scope,
    compile_skeleton_markdown,
    contract_comment_missing_sections,
    requirements_semantic_view,
    resolve_requirement_reference,
    review_architecture_skeleton,
    validate_architecture_revision_scope,
    validate_architecture_submission,
)
from pal.minion.v2.skeleton_builder import (
    SKELETON_BUILDER_TOOL_SPECS,
    compile_architecture_review_invocation_tool_contract,
    skeleton_builder_tool_result,
)
from pal.minion.v2.submission_drafts import AUTHORING_CONTRACT_VERSION
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
        self.requirements = {
            "title": "Tiny router",
            "sections": {
                "Routing": ["Route matching must be deterministic."],
                "Compatibility": ["Existing public signatures remain stable."],
            },
        }
        self.requirements_ref = self.artifacts.put_json(
            self.requirements,
            artifact_type="RequirementsArtifact",
        )
        self.builder_call_index = 0
        self.builder_lease_index = 0

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_semantic_requirement_view_hides_internal_ids_and_resolves_exact_text(self) -> None:
        legacy = {
            "requirements": [
                {
                    "requirement_id": "REQ-SECRET",
                    "section": "Routing",
                    "statement": "Route matching must be deterministic.",
                    "strength": "hard",
                }
            ]
        }
        view = requirements_semantic_view(legacy)
        self.assertNotIn("REQ-SECRET", json.dumps(view))
        resolved = resolve_requirement_reference(
            {"section": "Routing", "requirement": "Route matching must be deterministic"},
            legacy,
        )
        self.assertEqual(resolved.section, "Routing")

    def test_normalized_architecture_refs_do_not_copy_requirement_strength(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        normalized = validate_architecture_submission(
            self._submission(),
            requirements_payload=self.requirements,
            workspace_root=self.repo,
        )

        for module in normalized["modules"].values():
            for reference in module["covers"]:
                self.assertEqual(set(reference), {"section", "requirement"})
        for node in normalized["verification_nodes"].values():
            for reference in node["covers"]:
                self.assertEqual(set(reference), {"section", "requirement"})

    def test_requirement_resolution_never_silently_binds_a_near_match(self) -> None:
        with self.assertRaises(SemanticReferenceError) as raised:
            resolve_requirement_reference(
                {"section": "Routing", "requirement": "Route matching should be stable."},
                self.requirements,
            )
        self.assertTrue(raised.exception.possible_matches)

    def test_requirement_resolution_requires_section_for_duplicate_text(self) -> None:
        payload = {"sections": {"One": ["Keep it stable."], "Two": ["Keep it stable."]}}
        with self.assertRaisesRegex(SemanticReferenceError, "ambiguous"):
            resolve_requirement_reference({"requirement": "Keep it stable."}, payload)

    def test_contract_comment_reports_missing_semantic_sections(self) -> None:
        missing = contract_comment_missing_sections("Module: tiny_router\nState: stateless\n", module_name="tiny_router")
        self.assertIn("Ownership", missing)
        self.assertIn("Lifecycle", missing)

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
        submission["verification_nodes"]["router_consumer_probe"]["depends_on"].append("other")
        submission["verification_nodes"]["router_consumer_probe"]["consumes"].append(
            {"module": "other", "path": "include/other.h", "symbol": "RuleRouter"}
        )
        (self.repo / "include" / "other.h").write_text(_contract("other"), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "overlap"):
            validate_architecture_submission(
                submission,
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

    def test_verification_node_requires_complete_construction_dependency_closure(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        (self.repo / "include" / "consumer.h").write_text(_contract("consumer"), encoding="utf-8")
        (self.repo / "src" / "consumer.cpp").write_text("// consumer\n", encoding="utf-8")
        (self.repo / "tests" / "test_consumer.cpp").write_text("// consumer test\n", encoding="utf-8")
        submission = self._submission()
        submission["modules"]["consumer"] = {
            "module_kind": "implementation",
            "depends_on": ["router"],
            "consumes": [{"module": "router", "path": "include/router.h", "symbol": "RuleRouter"}],
            "paths": {
                "contract_paths": ["include/consumer.h"],
                "implementation_scopes": [{"kind": "file", "path": "src/consumer.cpp"}],
                "test_scopes": [{"kind": "file", "path": "tests/test_consumer.cpp"}],
                "reference_only": [],
            },
            "covers": submission["modules"]["router"]["covers"],
            "evidence": [],
        }
        scenario = submission["verification_nodes"]["router_consumer_probe"]
        scenario["depends_on"] = ["consumer"]
        scenario["consumes"] = [
            {"module": "consumer", "path": "include/consumer.h", "symbol": "RuleRouter"}
        ]
        with self.assertRaisesRegex(ValueError, "complete construction dependency closure.*router"):
            validate_architecture_submission(
                submission,
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

    def test_verification_and_implementation_names_cannot_collide(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        submission = self._submission()
        submission["verification_nodes"]["router"] = submission["verification_nodes"].pop(
            "router_consumer_probe"
        )
        with self.assertRaisesRegex(ValueError, "distinct semantic names"):
            validate_architecture_submission(
                submission,
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

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
            "depends_on": [],
            "consumes": [],
            "paths": {
                "contract_paths": ["include/route_types.h"],
                "implementation_scopes": [],
                "test_scopes": [],
                "reference_only": [],
            },
            "covers": submission["modules"]["router"]["covers"],
            "evidence": [],
        }
        submission["modules"]["router"]["consumes"] = [
            {
                "module": "route_types",
                "path": "include/route_types.h",
                "symbol": "RouteInput",
            }
        ]
        scenario = submission["verification_nodes"]["router_consumer_probe"]
        scenario["consumes"].append(
            {
                "module": "route_types",
                "path": "include/route_types.h",
                "symbol": "RouteInput",
            }
        )
        submission["verification_nodes"]["route_types_contract_probe"] = {
            "kind": "consumer_probe",
            "depends_on": [],
            "consumes": [
                {
                    "module": "route_types",
                    "path": "include/route_types.h",
                    "symbol": "RouteInput",
                }
            ],
            "covers": submission["modules"]["router"]["covers"],
            "entrypoints": [
                {
                    "kind": "source_symbol",
                    "path": "include/route_types.h",
                    "symbol": "RouteInput",
                }
            ],
            "environment": {},
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
        self.assertIn("router_consumer_probe", compilation.verification_node_ids)
        self.assertIn("route_types_contract_probe", compilation.verification_node_ids)

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

    def test_verification_depends_on_rejects_contract_only_module(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        submission = self._submission()
        submission["modules"]["router"]["module_kind"] = "contract_only"
        submission["modules"]["router"]["paths"]["implementation_scopes"] = []
        submission["modules"]["router"]["paths"]["test_scopes"] = []
        with self.assertRaisesRegex(ValueError, "depends_on may name implementation Candidates only"):
            validate_architecture_submission(
                submission,
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

    def test_contract_only_consumer_probe_may_have_no_candidate_dependency(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        submission = self._submission()
        submission["modules"]["router"]["module_kind"] = "contract_only"
        submission["modules"]["router"]["paths"]["implementation_scopes"] = []
        submission["modules"]["router"]["paths"]["test_scopes"] = []
        submission["verification_nodes"]["router_consumer_probe"]["depends_on"] = []

        normalized = validate_architecture_submission(
            submission,
            requirements_payload=self.requirements,
            workspace_root=self.repo,
        )

        self.assertEqual(
            normalized["verification_nodes"]["router_consumer_probe"]["depends_on"],
            [],
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
        self.assertEqual(work_view["requirements"]["sections"], self.requirements["sections"])
        self.assertEqual(
            work_view["historical_repair_bills"][0]["case_name"],
            "reset preserves deterministic routing",
        )
        for forbidden in ("workflow_id", "revision_id", "node_run_id", "epoch_id", "sha256"):
            self.assertNotIn(forbidden, encoded)
        for forbidden_value in ("hidden-workflow", "hidden-node", "hidden-candidate", "hidden-fingerprint"):
            self.assertNotIn(forbidden_value, encoded)

    def test_verification_scenario_runs_on_the_declared_candidate_union(self) -> None:
        workspace = self._provision_complete_workspace("scenario-union", "initial")
        skeleton_ref = self.service.snapshot_architecture(
            workflow_name="scenario-union",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        architecture = ArchitectureArtifactService(self.artifacts, self.repository)
        compilation = ExecutionCompiler(self.repository, architecture).compile_epoch(
            workflow_id="scenario-union",
            epoch_id="scenario-union-epoch",
            manifest_ref=skeleton_ref,
        )
        scheduler = DagScheduler(self.repository)
        self.assertEqual(
            scheduler.schedule_ready_nodes(
                workflow_id="scenario-union",
                epoch_id="scenario-union-epoch",
                max_new_nodes=2,
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
            {"status": "PASS"}, artifact_type="VerificationArtifact"
        )
        self._accept_candidate(
            router_id,
            candidate_ref=candidate_ref.to_dict(),
            candidate_digest=candidate_digest,
            verification_ref=verification_ref.to_dict(),
        )
        scenario_id = compilation.verification_node_ids["router_consumer_probe"]
        self.assertEqual(
            scheduler.schedule_ready_nodes(
                workflow_id="scenario-union",
                epoch_id="scenario-union-epoch",
                max_new_nodes=2,
            ),
            (scenario_id,),
        )
        processor = MinionV2OutboxProcessor(MinionV2WorkflowService(self.runtime_root))
        result = processor._prepare_verification_scenario(
            {
                "effect_key": "scenario-union:prepare",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": scenario_id,
            }
        )
        scenario = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, scenario_id)
        assert scenario is not None
        self.assertEqual(scenario.state, "QUEUED")
        MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root))._admit_node_worker(
            {
                "effect_key": "scenario-union:admit",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": scenario_id,
            },
            action_type="START_SCENARIO_VERIFICATION",
            role="scenario_verifier",
        )
        scenario = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, scenario_id)
        assert scenario is not None
        self.assertEqual(scenario.state, "VERIFYING")
        self.assertGreater(int(scenario.payload["fencing_token"]), 0)
        self.assertEqual(scenario.payload["worker_role"], "scenario_verifier")
        scenario_worktree = Path(scenario.payload["workspace_path"])
        self.assertEqual(
            (scenario_worktree / "src" / "router.cpp").read_text(encoding="utf-8"),
            "int route_rule() { return 17; }\n",
        )
        union = self.artifacts.read_json(scenario.payload["scenario_candidate_union_ref"])
        self.assertEqual(
            union["applied_module_candidates"],
            [{"module_name": "router", "candidate_digest": candidate_digest}],
        )
        self.assertEqual(result["result_artifact_ref"], scenario.payload["scenario_work_view_ref"])

        scenario_verification_ref = self.artifacts.put_json(
            {
                "status": "PASS",
                "scenario_fingerprint": scenario.payload["scenario_fingerprint"],
            },
            artifact_type="VerificationArtifact",
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="VERIFICATION_PASSED",
                workflow_id=scenario.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=scenario.aggregate_id,
                actor="test",
                expected_version=scenario.version,
                idempotency_key="scenario-union:passed",
                payload={
                    "verification_artifact_ref": scenario_verification_ref.to_dict(),
                    "scenario_fingerprint": scenario.payload["scenario_fingerprint"],
                },
            )
        )
        epoch = self.repository.read_snapshot(
            AggregateType.EXECUTION_EPOCH,
            "scenario-union-epoch",
        )
        assert epoch is not None
        self.repository.dispatch(
            ActionEnvelope(
                action_type="ALL_REQUIRED_NODES_ACCEPTED",
                workflow_id=epoch.workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id=epoch.aggregate_id,
                actor="test",
                expected_version=epoch.version,
                idempotency_key="scenario-union:ready-to-publish",
                payload={
                    "accepted_candidate_refs": [candidate_ref.to_dict()],
                    "verification_artifact_refs": [scenario_verification_ref.to_dict()],
                },
            )
        )
        publish_result = asyncio.run(
            MinionV2SemanticWorker(MinionV2WorkflowService(self.runtime_root)).execute_semantic_effect(
                {
                    "effect_type": "publish_final_deliverable",
                    "effect_id": "scenario-union:publish",
                    "workflow_id": "scenario-union",
                    "aggregate_type": AggregateType.EXECUTION_EPOCH.value,
                    "aggregate_id": "scenario-union-epoch",
                }
            )
        )
        published = self.artifacts.read_json(publish_result["result_artifact_ref"])
        self.assertEqual(published["branch_name"], router.payload["workflow_branch"])
        self.assertEqual(
            _git(router_worktree, "rev-parse", published["branch_name"]).strip(),
            published["commit_sha"],
        )
        publish_worktree = (
            Path(router.payload["common_git_dir"]).parent
            / "worktrees"
            / router.payload["workflow_key"]
            / "publish"
        )
        self.assertEqual(
            (publish_worktree / "src" / "router.cpp").read_text(encoding="utf-8"),
            "int route_rule() { return 17; }\n",
        )

    def test_skeleton_builder_schema_contains_semantics_not_manager_identity(self) -> None:
        encoded = json.dumps(SKELETON_BUILDER_TOOL_SPECS, sort_keys=True)
        self.assertIn("contract_paths", encoded)
        self.assertIn("op_minion_architecture_verification_upsert", encoded)
        self.assertIn("op_minion_architecture_module_consume_contract", encoded)
        self.assertNotIn('"items": {"type": "object"}', encoded)
        self.assertNotIn('"prefix"', encoded)
        self.assertIn("requirement", encoded)
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

    def test_architecture_submit_preflight_keeps_invalid_submission_in_the_live_turn(self) -> None:
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
        submission = self._submission()
        submission["modules"]["router"]["covers"][0]["requirement"] = (
            "Route matching should probably be deterministic."
        )
        produced: list[dict[str, object]] = []
        self._author_submission(workspace, submission)
        rejected = self._builder_call(
            workspace, "op_minion_architecture_submit", produced=produced
        )
        self.assertFalse(rejected.ok)
        self.assertEqual(produced, [])
        self.assertTrue(rejected.structured["possible_matches"])
        self.assertFalse(
            (Path(workspace["artifact_stage_dir"]) / "architecture_submission.json").exists()
        )

        self.assertTrue(
            self._builder_call(
                workspace,
                "op_minion_architecture_module_remove",
                {"name": "router"},
            ).ok
        )
        self._author_submission(
            workspace,
            {"modules": self._submission()["modules"], "verification_nodes": {}},
        )
        accepted = self._builder_call(
            workspace, "op_minion_architecture_submit", produced=produced
        )
        self.assertTrue(accepted.ok)
        self.assertEqual(len(produced), 1)

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

    def test_architecture_review_submit_rejects_unknown_module_before_worker_exit(self) -> None:
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
        recorded = self._builder_call(
            workspace,
            "op_minion_architecture_review_finding",
            {
                "finding_kind": "contract_defect",
                "summary": "The contract is incomplete.",
                "severity": "error",
                "affected_modules": ["invented_router"],
                "requirement_section": "Routing",
                "requirement": "Route matching must be deterministic.",
                "path": "README.md",
                "contract_section": "Contract",
            },
        )
        self.assertTrue(recorded.ok, recorded.text)
        result = self._builder_call(workspace, "op_minion_skeleton_review_submit")

        self.assertFalse(result.ok)
        self.assertIn("unknown modules: invented_router", result.llm_text)
        self.assertIn("Allowed exact module names: router", result.llm_text)
        self.assertFalse((Path(workspace["artifact_stage_dir"]) / "architecture_review.json").exists())

    def test_architecture_review_submit_rejects_rewritten_requirement_before_worker_exit(self) -> None:
        requirements_path = self.runtime_root / "review-requirements.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        workspace = self._bind_builder_workspace({
            "repo_path": str(self.repo),
            "reference_paths": [{"name": "requirements", "path": str(requirements_path)}],
            "artifact_dir": str(self.runtime_root / "review-artifacts"),
            "artifact_stage_dir": str(self.runtime_root / "review-stage"),
        }, role="architecture_reviewer")
        recorded = self._builder_call(
            workspace,
            "op_minion_architecture_review_finding",
            {
                "finding_kind": "requirements_defect",
                "summary": "The requirement was narrowed.",
                "severity": "error",
                "affected_modules": [],
                "requirement_section": "Routing",
                "requirement": "Routing should be deterministic enough.",
            },
        )
        self.assertTrue(recorded.ok, recorded.text)
        result = self._builder_call(workspace, "op_minion_skeleton_review_submit")

        self.assertFalse(result.ok)
        self.assertIn("outside its bound work view", result.llm_text)
        self.assertIn("Routing: Route matching must be deterministic.", result.llm_text)

    def test_architecture_review_submit_requires_complete_bound_audits(self) -> None:
        workspace = self._review_builder_workspace()

        result = self._builder_call(workspace, "op_minion_skeleton_review_submit")

        self.assertFalse(result.ok)
        self.assertIn("missing hard Requirements audits", result.llm_text)
        self.assertIn("Routing: Route matching must be deterministic.", result.llm_text)

    def test_architecture_review_audits_compile_into_durable_pass_artifact(self) -> None:
        workspace = self._review_builder_workspace()
        self._record_complete_review_audits(workspace)

        submitted = self._builder_call(workspace, "op_minion_skeleton_review_submit")

        self.assertTrue(submitted.ok, submitted.llm_text)
        artifact = json.loads(
            Path(str(dict(submitted.structured["artifact"])["path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(artifact["verdict"], "PASS")
        self.assertEqual(len(artifact["audit"]["requirements"]), 2)
        self.assertEqual([item["name"] for item in artifact["audit"]["modules"]], ["router"])
        self.assertEqual(
            [item["name"] for item in artifact["audit"]["verification_nodes"]],
            ["router_consumer_probe"],
        )

    def test_architecture_review_defect_audit_requires_finding_and_compiles_fail(self) -> None:
        workspace = self._review_builder_workspace()
        self._record_complete_review_audits(workspace)
        defect = self._builder_call(
            workspace,
            "op_minion_architecture_review_module_audit",
            {
                "name": "router",
                "classification": "sound",
                "dependency_topology": "sound",
                "contract_flow": "defect",
                "ownership_lifecycle_state": "sound",
                "scope": "sufficient",
                "rationale": "The public contract does not define the result flow needed by its consumer.",
            },
        )
        self.assertTrue(defect.ok, defect.llm_text)

        missing_finding = self._builder_call(workspace, "op_minion_skeleton_review_submit")
        self.assertFalse(missing_finding.ok)
        self.assertIn("defects but no typed finding", missing_finding.llm_text)

        finding = self._builder_call(
            workspace,
            "op_minion_architecture_review_finding",
            {
                "finding_kind": "contract_defect",
                "summary": "The router contract omits its consumer-visible result flow.",
                "severity": "error",
                "affected_modules": ["router"],
                "requirement_section": "Routing",
                "requirement": "Route matching must be deterministic.",
                "path": "include/router.h",
                "contract_section": "Provides",
            },
        )
        self.assertTrue(finding.ok, finding.llm_text)

        submitted = self._builder_call(workspace, "op_minion_skeleton_review_submit")
        self.assertTrue(submitted.ok, submitted.llm_text)
        artifact = json.loads(
            Path(str(dict(submitted.structured["artifact"])["path"])).read_text(encoding="utf-8")
        )
        self.assertEqual(artifact["verdict"], "FAIL")
        self.assertEqual(artifact["audit"]["modules"][0]["contract_flow"], "defect")

    def _record_complete_review_audits(self, workspace: dict[str, object]) -> None:
        for section, requirement in (
            ("Routing", "Route matching must be deterministic."),
            ("Compatibility", "Existing public signatures remain stable."),
        ):
            result = self._builder_call(
                workspace,
                "op_minion_architecture_review_requirement_audit",
                {
                    "section": section,
                    "requirement": requirement,
                    "assessment": "supported",
                    "modules": ["router"],
                    "delivery_paths": ["include/router.h", "src/router.cpp"],
                    "verification_nodes": ["router_consumer_probe"],
                    "rationale": "The frozen router contract and writable implementation feed the real consumer probe.",
                },
            )
            self.assertTrue(result.ok, result.llm_text)
        module = self._builder_call(
            workspace,
            "op_minion_architecture_review_module_audit",
            {
                "name": "router",
                "classification": "sound",
                "dependency_topology": "sound",
                "contract_flow": "complete",
                "ownership_lifecycle_state": "sound",
                "scope": "sufficient",
                "rationale": "The module has one stable contract, one local implementation scope, and no hidden dependency.",
            },
        )
        self.assertTrue(module.ok, module.llm_text)
        verification = self._builder_call(
            workspace,
            "op_minion_architecture_review_verification_audit",
            {
                "name": "router_consumer_probe",
                "candidate_combination": "sound",
                "contract_consumption": "sound",
                "entrypoint_environment": "sound",
                "requirement_proof": "sound",
                "rationale": "The consumer probe binds the router Candidate to its public source entrypoint.",
            },
        )
        self.assertTrue(verification.ok, verification.llm_text)

    def test_architecture_review_tool_contract_binds_exact_semantic_catalog(self) -> None:
        contract = compile_architecture_review_invocation_tool_contract(
            requirements=self.requirements,
            architecture=self._submission(),
        )

        self.assertEqual(len(contract["hard_requirements"]), 2)
        self.assertEqual(contract["module_names"], ["router"])
        self.assertEqual(contract["verification_node_names"], ["router_consumer_probe"])
        descriptions = json.dumps(contract["description_overrides"], ensure_ascii=False)
        self.assertIn("Route matching must be deterministic.", descriptions)
        self.assertIn("router_consumer_probe", descriptions)
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
        self.assertTrue(
            self._builder_call(
                workspace,
                "op_minion_architecture_module_add_reference",
                {"module": "router", "kind": "workspace_file", "path": "include/router.h"},
            ).ok
        )
        result = self._builder_call(
            workspace, "op_minion_architecture_submit", produced=produced
        )
        self.assertTrue(result.ok, result.text)
        merged = json.loads(Path(produced[0]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(set(merged["modules"]), {"router"})
        self.assertEqual(
            merged["modules"]["router"]["evidence"],
            [{"kind": "workspace_file", "path": "include/router.h"}],
        )

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
        self.assertTrue(
            self._builder_call(
                rejected_workspace,
                "op_minion_architecture_module_add_reference",
                {"module": "router", "kind": "workspace_file", "path": "include/router.h"},
            ).ok
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
        revised_submission["modules"]["router"]["evidence"] = [
            {"kind": "workspace_file", "path": "include/router.h"}
        ]
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
            self.artifacts.read_json(accepted_ref)["submission"]["modules"]["router"][
                "evidence"
            ],
            [{"kind": "workspace_file", "path": "include/router.h"}],
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

    def test_semantic_reference_finding_scopes_the_module_that_declared_it(self) -> None:
        base = self._submission()
        reference = {
            "kind": "reference_file",
            "reference_name": "routing_docs",
            "path": "api.md",
            "section": "Router lifecycle",
        }
        base["modules"]["router"]["evidence"] = [reference]
        base["modules"]["router_adapter"] = {
            **json.loads(json.dumps(base["modules"]["router"])),
            "evidence": [
                {
                    **reference,
                    "section": "Adapter lifecycle",
                }
            ],
        }

        scope = architecture_revision_scope(
            base,
            {
                "finding_kind": "contract_defect",
                "summary": "Evidence path does not exist in its declared root.",
                "semantic_reference_error": {
                    "error": "Evidence path does not exist in its declared root.",
                    "reference": reference,
                    "possible_matches": [],
                },
            },
        )

        self.assertEqual(scope["affected_modules"], ["router", "router_adapter"])
        self.assertEqual(scope["affected_verification_nodes"], ["router_consumer_probe"])
        self.assertEqual(scope["allowed_paths"], ["api.md"])
        self.assertFalse(scope["allow_topology_changes"])

    def test_module_reference_add_replaces_the_same_semantic_locator(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        requirements_path = self.runtime_root / "reference-upsert-requirements.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        base = self._submission()
        base["modules"]["router"]["evidence"] = [
            {
                "kind": "reference_file",
                "reference_name": "wrong_root",
                "path": "README.md",
                "section": "Router contract",
            }
        ]
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
            "op_minion_architecture_module_add_reference",
            {
                "module": "router",
                "kind": "workspace_file",
                "path": "README.md",
                "section": "Router contract",
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
            payload["modules"]["router"]["evidence"],
            [
                {
                    "kind": "workspace_file",
                    "path": "README.md",
                    "section": "Router contract",
                }
            ],
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

    def test_revision_scope_ignores_manager_owned_requirement_strength(self) -> None:
        base = self._submission()
        base["modules"]["other"] = json.loads(
            json.dumps(base["modules"]["router"])
        )
        base["verification_nodes"]["other_probe"] = json.loads(
            json.dumps(base["verification_nodes"]["router_consumer_probe"])
        )
        base["verification_nodes"]["other_probe"]["depends_on"] = ["other"]
        base["verification_nodes"]["other_probe"]["consumes"][0]["module"] = "other"
        for owner in (
            *base["modules"].values(),
            *base["verification_nodes"].values(),
        ):
            for requirement in owner["covers"]:
                requirement["strength"] = "hard"
        revised = json.loads(json.dumps(base))
        for owner in (
            *revised["modules"].values(),
            *revised["verification_nodes"].values(),
        ):
            for requirement in owner["covers"]:
                requirement.pop("strength", None)
        revised["modules"]["router"]["evidence"] = [
            {"kind": "workspace_file", "path": "README.md"}
        ]
        scope = architecture_revision_scope(
            base,
            {
                "finding_kind": "contract_defect",
                "affected_modules": ["router"],
            },
        )

        validate_architecture_revision_scope(
            base_submission=base,
            revised_submission=revised,
            changed_paths=[],
            scope=scope,
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
        # preflight found one bad semantic reference.
        (revision.worktree / "src" / "router.cpp").write_text(
            "// broad human edit\n", encoding="utf-8"
        )
        rejected_submission = json.loads(json.dumps(base_artifact["submission"]))
        rejected_reference = {
            "kind": "reference_file",
            "reference_name": "missing_reference_root",
            "path": "README.md",
            "section": "Router contract",
        }
        rejected_submission["modules"]["router"]["evidence"] = [rejected_reference]
        rejected_states = architecture_revision_path_states(
            revision.worktree,
            revision.base_sha,
        )
        finding = {
            "finding_kind": "contract_defect",
            "summary": "Evidence path does not exist in its declared root.",
            "semantic_reference_error": {
                "error": "Evidence path does not exist in its declared root.",
                "reference": rejected_reference,
                "possible_matches": [],
            },
        }
        revised_submission = json.loads(json.dumps(rejected_submission))
        revised_submission["modules"]["router"]["evidence"] = [
            {"kind": "workspace_file", "path": "README.md"}
        ]

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
        self.assertEqual(
            accepted["submission"]["modules"]["router"]["evidence"],
            [{"kind": "workspace_file", "path": "README.md"}],
        )
        self.assertIn("src/router.cpp", accepted["changed_paths"])

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

        self.assertTrue(
            self._builder_call(
                workspace,
                "op_minion_architecture_module_add_reference",
                {"module": "router", "kind": "workspace_file", "path": "include/router.h"},
            ).ok
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

        calls = [
            (
                "op_minion_architecture_module_upsert",
                {
                    "name": "route_status",
                    "module_kind": "contract_only",
                    "depends_on": [],
                    "contract_paths": ["include/route_status.h"],
                },
            ),
            (
                "op_minion_architecture_module_consume_contract",
                {
                    "consumer": "router",
                    "provider": "route_status",
                    "path": "include/route_status.h",
                    "symbol": "RouteStatus",
                },
            ),
            (
                "op_minion_architecture_verification_consume_contract",
                {
                    "consumer": "router_consumer_probe",
                    "provider": "route_status",
                    "path": "include/route_status.h",
                    "symbol": "RouteStatus",
                },
            ),
        ]
        for requirement in list(base["modules"]["router"]["covers"]):
            calls.append(
                (
                    "op_minion_architecture_module_cover_requirement",
                    {"name": "route_status", **dict(requirement)},
                )
            )
        for capability, args in calls:
            updated = self._builder_call(workspace, capability, args)
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
                "ARCHITECT_COMPLETED",
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

    def _submission(self) -> dict[str, object]:
        return {
            "modules": {
                "router": {
                    "module_kind": "implementation",
                    "depends_on": [],
                    "consumes": [],
                    "paths": {
                        "contract_paths": ["include/router.h"],
                        "implementation_scopes": [{"kind": "file", "path": "src/router.cpp"}],
                        "test_scopes": [{"kind": "file", "path": "tests/test_router.cpp"}],
                        "reference_only": [],
                    },
                    "covers": [
                        {
                            "section": "Routing",
                            "requirement": "Route matching must be deterministic.",
                        },
                        {
                            "section": "Compatibility",
                            "requirement": "Existing public signatures remain stable.",
                        },
                    ],
                    "evidence": [],
                }
            },
            "verification_nodes": {
                "router_consumer_probe": {
                    "kind": "consumer_probe",
                    "depends_on": ["router"],
                    "consumes": [
                        {"module": "router", "path": "include/router.h", "symbol": "RuleRouter"}
                    ],
                    "covers": [
                        {
                            "section": "Routing",
                            "requirement": "Route matching must be deterministic.",
                        },
                        {
                            "section": "Compatibility",
                            "requirement": "Existing public signatures remain stable.",
                        },
                    ],
                    "entrypoints": [
                        {"kind": "source_symbol", "path": "include/router.h", "symbol": "RuleRouter"}
                    ],
                    "environment": {},
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
                "depends_on": list(module.get("depends_on") or []),
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
            for consumed in list(module.get("consumes") or []):
                item = dict(consumed)
                result = self._builder_call(
                    workspace,
                    "op_minion_architecture_module_consume_contract",
                    {
                        "consumer": module_name,
                        "provider": item["module"],
                        "path": item["path"],
                        **({"symbol": item["symbol"]} if item.get("symbol") else {}),
                    },
                )
                self.assertTrue(result.ok, result.text)
            for requirement in list(module.get("covers") or []):
                item = dict(requirement)
                result = self._builder_call(
                    workspace,
                    "op_minion_architecture_module_cover_requirement",
                    {"name": module_name, **item},
                )
                self.assertTrue(result.ok, result.text)
            for evidence in list(module.get("evidence") or []):
                result = self._builder_call(
                    workspace,
                    "op_minion_architecture_module_add_reference",
                    {"module": module_name, **dict(evidence)},
                )
                self.assertTrue(result.ok, result.text)
        for node_name, raw_node in dict(submission["verification_nodes"]).items():
            node = dict(raw_node)
            result = self._builder_call(
                workspace,
                "op_minion_architecture_verification_upsert",
                {
                    "name": node_name,
                    "kind": node["kind"],
                    "depends_on": list(node.get("depends_on") or []),
                },
            )
            self.assertTrue(result.ok, result.text)
            for consumed in list(node.get("consumes") or []):
                item = dict(consumed)
                result = self._builder_call(
                    workspace,
                    "op_minion_architecture_verification_consume_contract",
                    {
                        "consumer": node_name,
                        "provider": item["module"],
                        "path": item["path"],
                        **({"symbol": item["symbol"]} if item.get("symbol") else {}),
                    },
                )
                self.assertTrue(result.ok, result.text)
            for requirement in list(node.get("covers") or []):
                result = self._builder_call(
                    workspace,
                    "op_minion_architecture_verification_cover_requirement",
                    {"name": node_name, **dict(requirement)},
                )
                self.assertTrue(result.ok, result.text)
            for entrypoint in list(node.get("entrypoints") or []):
                result = self._builder_call(
                    workspace,
                    "op_minion_architecture_verification_add_entrypoint",
                    {"name": node_name, **dict(entrypoint)},
                )
                self.assertTrue(result.ok, result.text)
            result = self._builder_call(
                workspace,
                "op_minion_architecture_verification_set_environment",
                {"name": node_name, **dict(node.get("environment") or {})},
            )
            self.assertTrue(result.ok, result.text)


if __name__ == "__main__":
    unittest.main()
