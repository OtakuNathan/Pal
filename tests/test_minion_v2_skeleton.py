from __future__ import annotations

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
    compile_skeleton_markdown,
    contract_comment_missing_sections,
    requirements_semantic_view,
    resolve_requirement_reference,
    review_architecture_skeleton,
    validate_architecture_submission,
)
from pal.minion.v2.skeleton_builder import (
    SKELETON_BUILDER_TOOL_SPECS,
    skeleton_builder_tool_result,
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

    def test_skeleton_builder_schema_contains_semantics_not_manager_identity(self) -> None:
        encoded = json.dumps(SKELETON_BUILDER_TOOL_SPECS, sort_keys=True)
        self.assertIn("contract_paths", encoded)
        self.assertIn("verification_nodes", encoded)
        self.assertIn("consumes", encoded)
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
        workspace = {
            "repo_path": str(self.repo),
            "reference_paths": [{"name": "requirements", "path": str(requirements_path)}],
            "artifact_dir": str(self.runtime_root / "artifacts"),
            "artifact_stage_dir": str(self.runtime_root / "artifact-stage"),
        }
        submission = self._submission()
        submission["modules"]["router"]["covers"][0]["requirement"] = (
            "Route matching should probably be deterministic."
        )
        produced: list[dict[str, object]] = []
        rejected = skeleton_builder_tool_result(
            CanonicalToolCall(name="op_minion_architecture_submit", args=submission),
            workspace,
            produced,
        )
        self.assertFalse(rejected.ok)
        self.assertEqual(produced, [])
        self.assertTrue(rejected.structured["possible_matches"])
        self.assertFalse(
            (Path(workspace["artifact_stage_dir"]) / "architecture_submission.json").exists()
        )

        accepted = skeleton_builder_tool_result(
            CanonicalToolCall(name="op_minion_architecture_submit", args=self._submission()),
            workspace,
            produced,
        )
        self.assertTrue(accepted.ok)
        self.assertEqual(len(produced), 1)

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


if __name__ == "__main__":
    unittest.main()
