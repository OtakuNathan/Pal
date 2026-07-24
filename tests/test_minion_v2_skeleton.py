from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from pal.llm.contracts import CanonicalToolCall
from pal.minion.scoped_execution import (
    MinionScopedExecutionOpMinionArtifactEditInput,
    _WORKSPACE_TOOL_SPECS,
)
from pal.minion.v2.architecture import ArchitectureArtifactService
from pal.minion.v2.architecture_yaml import (
    ArchitectureDraft,
    load_architecture_draft,
    prepare_architecture_draft_file,
    write_architecture_draft,
)
from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.minion.v2.contracts import ActionEnvelope, AggregateType
from pal.minion.v2.execution import (
    DagScheduler,
    ExecutionCompiler,
    UnitWorkViewBuilder,
    _validate_skeleton_candidate_paths,
)
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.review_findings import (
    ADD_FINDING_CAPABILITY,
    MinionV2ReviewAddFindingInput,
    add_finding_tool_result,
)
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.skeleton import (
    ARCHITECTURE_SKELETON_ARTIFACT,
    ArchitectureValidationError,
    GitBackedSkeletonService,
    architecture_revision_changed_paths_since,
    architecture_revision_path_states,
    architecture_revision_scope,
    compile_skeleton_markdown,
    compiled_module_write_scopes,
    review_architecture_skeleton,
    validate_architecture_changed_paths,
    validate_architecture_revision_scope,
    validate_architecture_submission,
)
from pal.minion.v2.task_ledger import TaskLedgerService, TaskRevisionAuthority
from pal.minion.v2.skeleton_builder import (
    SKELETON_BUILDER_TOOL_SPECS,
    ask_question_tool_result,
    compile_architecture_review_invocation_tool_contract,
    skeleton_builder_tool_result,
)
from pal.minion.v2.submission_drafts import (
    AUTHORING_CONTRACT_VERSION,
    SubmissionDraftContext,
    SubmissionDraftStore,
)
from pal.minion.v2.semantic_orchestration import SemanticOrchestrator


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
        self.requirements_ref = TaskLedgerService(
            self.runtime_root,
            self.artifacts,
        ).publish(
            title="Tiny router",
            task_spec={
                "objective": "Route matching must be deterministic.",
                "compatibility": "Existing public signatures remain stable.",
            },
            actor="test",
            source_channel="test",
        )
        self.requirements = self.artifacts.read_json(self.requirements_ref)
        self.builder_call_index = 0
        self.builder_lease_index = 0

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_normalized_architecture_contains_requirement_mapping_module_protocol_and_paths(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        normalized = validate_architecture_submission(
            self._submission(),
            requirements_payload=self.requirements,
            workspace_root=self.repo,
        )

        self.assertEqual(set(normalized), {"requirements", "modules", "scenarios"})
        self.assertEqual(
            set(normalized["modules"]["router"]),
            {
                "module_kind",
                "behavior_kind",
                "responsibility",
                "dependencies",
                "contract",
                "ownership",
                "lifecycle",
                "state_machine",
                "paths",
            },
        )
        self.assertEqual(
            normalized["modules"]["router"]["paths"]["contract_mode"],
            "file_frozen",
        )

    def test_requirement_mapping_requires_reference_and_owner_closure(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        for mutate, expected in (
            (
                lambda value: value["scenarios"]["router_end_to_end"].update(
                    {"requirement_refs": ["unknown_requirement"]}
                ),
                "references unknown requirements",
            ),
            (
                lambda value: value["requirements"]["deterministic_routing"].update(
                    {"owner": "unknown_owner"}
                ),
                "owner references an unknown module or scenario",
            ),
            (
                lambda value: value["scenarios"]["router_end_to_end"].update(
                    {"requirement_refs": []}
                ),
                "requires at least one requirement reference",
            ),
        ):
            with self.subTest(expected=expected):
                submission = self._submission()
                mutate(submission)
                with self.assertRaisesRegex(ArchitectureValidationError, expected):
                    validate_architecture_submission(
                        submission,
                        requirements_payload=self.requirements,
                        workspace_root=self.repo,
                    )

    def test_review_guarded_contract_compiles_as_its_module_writable_file(self) -> None:
        submission = self._submission()
        submission["modules"]["router"]["paths"] = {
            **submission["modules"]["router"]["paths"],
            "contract_mode": "review_guarded",
            "contract_paths": ["src/router.cpp"],
            "implementation_scopes": [],
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
        self.assertIn(
            {"kind": "file", "path": "src/router.cpp"},
            compiled_module_write_scopes(normalized["modules"]["router"]["paths"]),
        )

    def test_review_guarded_contract_deduplicates_explicit_implementation_scope(self) -> None:
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
        scopes = compiled_module_write_scopes(normalized["modules"]["router"]["paths"])
        self.assertEqual(
            scopes.count({"kind": "file", "path": "src/router.cpp"}),
            1,
        )

    def test_review_guarded_contract_is_a_candidate_write_path_without_duplicate_scope(self) -> None:
        policy = {
            "contract_mode": "review_guarded",
            "contract_paths": ["src/main.cpp"],
            "implementation_scopes": [{"kind": "file", "path": "CMakeLists.txt"}],
            "developer_tests": {"kind": "directory", "path": "tests/cli/developer"},
            "verification_corpus": {"kind": "directory", "path": "tests/cli/verification"},
            "reference_only": [],
        }

        _validate_skeleton_candidate_paths(["src/main.cpp"], policy)
        with self.assertRaisesRegex(ValueError, "outside its compiled module write scopes"):
            _validate_skeleton_candidate_paths(["src/other.cpp"], policy)

    def test_coder_can_write_developer_tests_but_not_verifier_corpus(self) -> None:
        policy = {
            "contract_mode": "file_frozen",
            "contract_paths": ["include/router.h"],
            "implementation_scopes": [{"kind": "file", "path": "src/router.cpp"}],
            "developer_tests": {
                "kind": "directory",
                "path": "tests/router/developer",
            },
            "verification_corpus": {
                "kind": "directory",
                "path": "tests/router/verification",
            },
            "reference_only": [],
        }

        _validate_skeleton_candidate_paths(
            ["src/router.cpp", "tests/router/developer/test_router.cpp"],
            policy,
        )
        with self.assertRaisesRegex(ValueError, "outside its compiled module write scopes"):
            _validate_skeleton_candidate_paths(
                ["tests/router/verification/test_router.cpp"],
                policy,
            )

    def test_file_frozen_contract_overrides_its_module_writable_overlap(self) -> None:
        submission = self._submission()
        submission["modules"]["router"]["paths"] = {
            **submission["modules"]["router"]["paths"],
            "contract_mode": "file_frozen",
            "contract_paths": ["src/router.cpp"],
        }

        normalized = validate_architecture_submission(
            submission,
            requirements_payload=self.requirements,
            workspace_root=self.repo,
        )
        with self.assertRaisesRegex(ValueError, "frozen architecture contracts"):
            _validate_skeleton_candidate_paths(
                ["src/router.cpp"],
                normalized["modules"]["router"]["paths"],
            )

    def test_review_guarded_contract_cannot_be_owned_by_verification_corpus(self) -> None:
        corpus_contract = self.repo / "tests" / "router" / "verification" / "contract.h"
        corpus_contract.parent.mkdir(parents=True)
        corpus_contract.write_text(_contract("router"), encoding="utf-8")
        submission = self._submission()
        submission["modules"]["router"]["paths"] = {
            **submission["modules"]["router"]["paths"],
            "contract_mode": "review_guarded",
            "contract_paths": ["tests/router/verification/contract.h"],
        }

        with self.assertRaisesRegex(ValueError, "verification_corpus.*review_guarded contract"):
            validate_architecture_submission(
                submission,
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

    def test_architecture_validation_allows_future_implementation_paths(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        (self.repo / "src" / "router.cpp").unlink()
        submission = self._submission()
        submission["modules"]["router"]["paths"]["implementation_scopes"] = [
            {"kind": "file", "path": "future/router.cpp"},
            {"kind": "directory", "path": "generated/router"},
        ]

        normalized = validate_architecture_submission(
            submission,
            requirements_payload=self.requirements,
            workspace_root=self.repo,
        )

        self.assertEqual(
            normalized["modules"]["router"]["paths"]["implementation_scopes"],
            [
                {"kind": "file", "path": "future/router.cpp"},
                {"kind": "directory", "path": "generated/router"},
            ],
        )
        self.assertFalse((self.repo / "future" / "router.cpp").exists())
        self.assertFalse((self.repo / "generated" / "router").exists())

    def test_architect_changed_paths_exclude_future_implementation_scopes(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        submission = self._submission()

        with self.assertRaisesRegex(ValueError, "outside declared contract skeleton paths"):
            validate_architecture_changed_paths(submission, ["src/router.cpp"])

        self.assertEqual(
            validate_architecture_changed_paths(submission, ["include/router.h"]),
            ("include/router.h",),
        )

    def test_top_level_implementation_directory_is_valid(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(
            _contract("router"), encoding="utf-8"
        )
        submission = self._submission()
        submission["modules"]["router"]["paths"]["implementation_scopes"] = [
            {"kind": "directory", "path": "src"}
        ]

        normalized = validate_architecture_submission(
            submission,
            requirements_payload=self.requirements,
            workspace_root=self.repo,
        )

        self.assertEqual(
            normalized["modules"]["router"]["paths"]["implementation_scopes"],
            [{"kind": "directory", "path": "src"}],
        )

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
        consumer = json.loads(json.dumps(submission["modules"]["router"]))
        consumer["responsibility"] = "consume routed results"
        consumer["dependencies"] = {
            "router": {
                "consumes": ["route_result"],
                "purpose": "obtain deterministic routes",
                "handoff": "submit a request and consume its route result",
            }
        }
        consumer["paths"] = {
            "contract_mode": "file_frozen",
            "contract_paths": ["include/consumer.h"],
            "implementation_scopes": [{"kind": "file", "path": "src/consumer.cpp"}],
            "reference_only": [],
        }
        submission["modules"]["consumer"] = consumer
        submission["modules"]["router"]["dependencies"] = {
            "consumer": {
                "consumes": ["route_result"],
                "purpose": "form the deliberate cycle under test",
                "handoff": "consume the consumer result",
            }
        }
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_architecture_submission(
                submission,
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

    def test_semantic_dependencies_do_not_serialize_implementation_coders(self) -> None:
        workspace = self._provision_complete_workspace("parallel-contracts", "initial")
        (workspace.worktree / "include" / "consumer.h").write_text(
            _contract("consumer"), encoding="utf-8"
        )
        submission = self._submission()
        consumer = json.loads(json.dumps(submission["modules"]["router"]))
        consumer["responsibility"] = "consume routed results"
        consumer["dependencies"] = {
            "router": {
                "consumes": ["route_result"],
                "purpose": "obtain deterministic routes",
                "handoff": "submit a request and consume its route result",
            }
        }
        consumer["paths"] = {
            "contract_mode": "file_frozen",
            "contract_paths": ["include/consumer.h"],
            "implementation_scopes": [{"kind": "file", "path": "src/consumer.cpp"}],
            "reference_only": [],
        }
        submission["modules"]["consumer"] = consumer
        submission["scenarios"]["router_end_to_end"]["modules"] = ["router", "consumer"]
        manifest_ref = self.service.snapshot_architect_result(
            workflow_name="parallel-contracts",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=submission,
            requirements_ref=self.requirements_ref,
        )
        self.assertFalse((workspace.worktree / "src" / "consumer.cpp").exists())
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
        self.assertEqual(
            consumer.payload["path_policy"]["verification_corpus"],
            {"kind": "directory", "path": "tests/consumer/verification"},
        )
        self.assertEqual(
            consumer.payload["path_policy"]["developer_tests"],
            {"kind": "directory", "path": "tests/consumer/developer"},
        )
        self.assertEqual(scenario.state, "BLOCKED_BY_DEPS")
        self.assertEqual(
            set(scenario.payload["dependency_node_ids"]),
            set(compilation.unit_node_ids.values()),
        )

    def test_architect_question_resumes_only_after_manager_records_revision(self) -> None:
        workspace = self._bind_builder_workspace(
            {
                "repo_path": str(self.repo),
                "artifact_dir": str(self.runtime_root / "question-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "question-stage"),
            },
            role="architect",
            mode="author",
        )
        observed: list[dict[str, object]] = []

        async def answer(payload: dict[str, object], timeout: float | None) -> dict[str, object]:
            observed.append({"payload": payload, "timeout": timeout})
            return {
                "answers": [
                    {"question_id": "architecture-question", "answer": "Preserve the public API"}
                ],
                "task_revision": {
                    "appended": True,
                    "sequence": 1,
                    "requirements_ref": {"sha256": "manager-owned"},
                },
            }

        result = asyncio.run(
            ask_question_tool_result(
                CanonicalToolCall(
                    name="op_minion_ask_question",
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
        self.assertEqual(
            result.structured["status"],
            "answered_revision_recorded",
        )
        self.assertEqual(result.structured["task_revision"]["sequence"], 1)
        self.assertNotIn(
            "op_minion_task_revision_submit",
            SKELETON_BUILDER_TOOL_SPECS,
        )
        self.assertFalse(
            (Path(str(workspace["artifact_stage_dir"])) / "task_revision.yaml").exists()
        )

    def test_snapshot_uses_preappended_task_ledger_without_hidden_merge(self) -> None:
        workspace = self._provision_complete_workspace("clarified-task", "initial")
        ledger = TaskLedgerService(self.runtime_root, self.artifacts)
        authority = TaskRevisionAuthority(
            title="Compatibility",
            question="Which compatibility boundary is binding?",
            answer="Preserve the existing public API; adapters may be added behind it.",
            origin="architect_user_clarification",
            observed_at="2026-07-20T12:00:00+00:00",
        )
        revised_ref = ledger.append_revision(
            base_ref=self.requirements_ref,
            authority=authority,
            actor="minion-manager",
            source_channel="user_clarification",
        )

        manifest_ref = self.service.snapshot_architect_result(
            workflow_name="clarified-task",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=revised_ref,
        )

        manifest = self.artifacts.read_json(manifest_ref)
        effective_requirements_ref = ArtifactRef.from_mapping(manifest["requirements_ref"])
        effective_requirements = self.artifacts.read_json(effective_requirements_ref)
        self.assertEqual(effective_requirements_ref.sha256, revised_ref.sha256)
        self.assertEqual(len(effective_requirements["revisions"]), 1)
        self.assertEqual(
            effective_requirements["revisions"][0]["authority"]["answer"],
            "Preserve the existing public API; adapters may be added behind it.",
        )

    def test_contract_only_module_is_frozen_without_a_coder_node(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        (self.repo / "include" / "route_types.h").write_text(
            _contract("route_types").replace("class RuleRouter;", "struct RouteInput;"),
            encoding="utf-8",
        )
        submission = self._submission()
        route_types = json.loads(json.dumps(submission["modules"]["router"]))
        route_types["module_kind"] = "contract_only"
        route_types["responsibility"] = "define the immutable route data shape"
        route_types["paths"] = {
            "contract_mode": "file_frozen",
            "contract_paths": ["include/route_types.h"],
            "implementation_scopes": [],
            "reference_only": [],
        }
        submission["modules"]["route_types"] = route_types

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
        manifest_ref = self.service.snapshot_architect_result(
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
                "profile": "software_engineering.v2_coder",
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

    def test_semantic_dependencies_can_consume_contract_only_module(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        (self.repo / "include" / "route_types.h").write_text(_contract("route_types"), encoding="utf-8")
        submission = self._submission()
        route_types = json.loads(json.dumps(submission["modules"]["router"]))
        route_types["module_kind"] = "contract_only"
        route_types["responsibility"] = "define the immutable route data shape"
        route_types["paths"] = {
            "contract_mode": "file_frozen",
            "contract_paths": ["include/route_types.h"],
            "implementation_scopes": [],
            "reference_only": [],
        }
        submission["modules"]["route_types"] = route_types
        submission["modules"]["router"]["dependencies"] = {
            "route_types": {
                "consumes": ["route_result"],
                "purpose": "reuse the accepted route result shape",
                "handoff": "return the shared route result",
            }
        }
        normalized = validate_architecture_submission(
            submission,
            requirements_payload=self.requirements,
            workspace_root=self.repo,
        )
        self.assertEqual(
            set(normalized["modules"]["router"]["dependencies"]),
            {"route_types"},
        )

    def test_semantic_dependency_must_consume_declared_provider_outputs(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        (self.repo / "include" / "route_types.h").write_text(_contract("route_types"), encoding="utf-8")
        submission = self._submission()
        route_types = json.loads(json.dumps(submission["modules"]["router"]))
        route_types["module_kind"] = "contract_only"
        route_types["paths"] = {
            "contract_mode": "file_frozen",
            "contract_paths": ["include/route_types.h"],
            "implementation_scopes": [],
            "reference_only": [],
        }
        submission["modules"]["route_types"] = route_types
        submission["modules"]["router"]["dependencies"] = {
            "route_types": {
                "consumes": ["missing_output"],
                "purpose": "exercise provider-output validation",
                "handoff": "consume the shared shape",
            }
        }

        with self.assertRaisesRegex(
            ArchitectureValidationError,
            "consumes unknown outputs from route_types: missing_output",
        ):
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
        artifact_ref = self.service.snapshot_architect_result(
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
        skeleton_ref = self.service.snapshot_architect_result(
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

        manifest_ref = self.service.snapshot_architect_result(
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
        skeleton_ref = self.service.snapshot_architect_result(
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
        first_ref = self.service.snapshot_architect_result(
            workflow_name="idempotent",
            revision_name="initial",
            architecture_workspace=workspace,
            submission=self._submission(),
            requirements_ref=self.requirements_ref,
        )
        first = self.artifacts.read_json(first_ref)

        second_ref = self.service.snapshot_architect_result(
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
            self.service.snapshot_architect_result(
                workflow_name="git-mutation",
                revision_name="initial",
                architecture_workspace=workspace,
                submission=self._submission(),
                requirements_ref=self.requirements_ref,
            )

    def test_execution_epoch_starts_every_node_from_skeleton_and_handoff_hides_manager_identity(self) -> None:
        workspace = self._provision_complete_workspace("execution", "initial")
        skeleton_ref = self.service.snapshot_architect_result(
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
        module_contract = self.artifacts.read_json(router.payload["unit_contract_ref"])
        self.assertEqual(
            set(module_contract["requirements"]),
            {"deterministic_routing"},
        )
        self.assertEqual(module_contract["module"]["responsibility"], self._submission()["modules"]["router"]["responsibility"])
        scenario_node = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            compilation.verification_node_ids["router_end_to_end"],
        )
        assert scenario_node is not None
        scenario_contract = self.artifacts.read_json(
            scenario_node.payload["unit_contract_ref"]
        )
        self.assertEqual(
            set(scenario_contract["requirements"]),
            {"deterministic_routing"},
        )
        work_view_ref = UnitWorkViewBuilder(architecture).build(router, dependency_outputs={})
        work_view = self.artifacts.read_json(work_view_ref)
        encoded = json.dumps(work_view, sort_keys=True)
        self.assertEqual(work_view["module_name"], "router")
        self.assertIn("requirements", work_view)
        self.assertNotIn("coverage_claims", work_view)
        self.assertNotIn("contract_consumption", work_view)
        self.assertEqual(
            set(work_view["requirements"]),
            {"deterministic_routing"},
        )
        self.assertEqual(work_view["module"]["responsibility"], self._submission()["modules"]["router"]["responsibility"])
        self.assertEqual(
            work_view["developer_tests"],
            {"kind": "directory", "path": "tests/router/developer"},
        )
        self.assertEqual(
            work_view["verification_corpus"],
            {"kind": "directory", "path": "tests/router/verification"},
        )
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
        skeleton_ref = self.service.snapshot_architect_result(
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
            SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root)).execute_semantic_effect(
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

    def test_skeleton_builder_surface_contains_only_question_submit_and_review_tools(self) -> None:
        projected_specs = {
            canonical_path: {
                **{key: value for key, value in spec.items() if key != "InputModel"},
                "input_schema": spec["InputModel"].model_json_schema(mode="validation"),
            }
            for canonical_path, spec in SKELETON_BUILDER_TOOL_SPECS.items()
        }
        encoded = json.dumps(projected_specs, sort_keys=True)
        self.assertIn("op_minion_architecture_submit", encoded)
        self.assertIn("op_minion_ask_question", encoded)
        self.assertNotIn("architecture_module_upsert", encoded)
        self.assertNotIn("architecture_module_remove", encoded)
        self.assertNotIn("architecture_scenario_upsert", encoded)
        self.assertNotIn("architecture_scenario_remove", encoded)
        self.assertIn("architecture.yaml", encoded)
        self.assertNotIn("architecture_verification", encoded)
        self.assertNotIn("consume_contract", encoded)
        self.assertNotIn("cover_requirement", encoded)
        self.assertNotIn('"items": {"type": "object"}', encoded)
        self.assertNotIn("test_files", encoded)
        self.assertNotIn("test_directories", encoded)
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

    def test_reloadable_minion_owns_changed_tool_models(self) -> None:
        self.assertEqual(
            MinionV2ReviewAddFindingInput.__module__,
            "pal.minion.v2.review_findings",
        )
        MinionV2ReviewAddFindingInput.model_validate(
            {
                "finding_key": "task_ledger_conflict",
                "finding_kind": "requirements_defect",
                "priority": "p0",
                "summary": "The effective task contains contradictory obligations.",
                "locations": [
                    {"scope": "task_ledger", "file": "task.yaml", "line": 3}
                ],
            },
            strict=True,
        )
        with self.assertRaises(ValueError):
            MinionV2ReviewAddFindingInput.model_validate(
                {
                    "finding_key": "removed_citation_scope",
                    "finding_kind": "requirements_defect",
                    "priority": "p0",
                    "summary": "Legacy citation scopes are forbidden.",
                    "locations": [
                        {
                            "scope": "task" + "_source",
                            "file": "TASK" + ".md",
                            "line": 1,
                        }
                    ],
                },
                strict=True,
            )
        self.assertNotIn("op_minion_task_revision_submit", SKELETON_BUILDER_TOOL_SPECS)
        self.assertEqual(
            MinionScopedExecutionOpMinionArtifactEditInput.__module__,
            "pal.minion.scoped_execution",
        )
        MinionScopedExecutionOpMinionArtifactEditInput.model_validate(
            {
                "relative_path": "review_notes.yaml",
                "content": "schema_version: '1'\n",
                "operation": "replace",
                "create_if_missing": False,
            },
            strict=True,
        )
        with self.assertRaises(ValueError):
            MinionScopedExecutionOpMinionArtifactEditInput.model_validate(
                {
                    "relative_path": "review_notes.yaml",
                    "old_" + "string": "before",
                    "new_" + "string": "after",
                },
                strict=True,
            )
        self.assertIs(
            _WORKSPACE_TOOL_SPECS["op_minion_artifact_edit"]["InputModel"],
            MinionScopedExecutionOpMinionArtifactEditInput,
        )

    def test_architecture_yaml_schema_accepts_dynamic_semantic_name_maps(self) -> None:
        schema = ArchitectureDraft.model_json_schema(mode="validation")
        requirement_map = schema["properties"]["requirements"]
        module_map = schema["properties"]["modules"]
        scenario_map = schema["properties"]["scenarios"]
        self.assertIn(r"^[a-z][a-z0-9_]{1,79}$", requirement_map["patternProperties"])
        self.assertIn(r"^[a-z][a-z0-9_]{1,79}$", module_map["patternProperties"])
        self.assertIn(r"^[a-z][a-z0-9_]{1,79}$", scenario_map["patternProperties"])

        payload = {"schema_version": 4, **self._submission()}
        ArchitectureDraft.model_validate(payload, strict=True)
        for invalid_name in ("CLI Decode Streaming", "a", "2fast", "bad-name", "a" * 81):
            with self.subTest(invalid_name=invalid_name), self.assertRaises(ValueError):
                invalid = json.loads(json.dumps(payload))
                invalid["modules"][invalid_name] = invalid["modules"].pop("router")
                ArchitectureDraft.model_validate(invalid, strict=True)

    def test_architect_question_description_exposes_semantic_triggers(self) -> None:
        description = str(
            SKELETON_BUILDER_TOOL_SPECS["op_minion_ask_question"]["description"]
        )

        for trigger in (
            "contradiction",
            "material ambiguity",
            "incorrect or infeasible requirement",
            "user preference",
            "modification scope",
            "implementation scope",
        ):
            with self.subTest(trigger=trigger):
                self.assertIn(trigger, description)
        self.assertIn("Use this tool proactively before architecture design", description)
        self.assertIn("Do not silently choose precedence", description)
        self.assertIn("identify the exact issue and the decision needed", description)
        self.assertIn("suspends the current tool call", description)
        self.assertEqual(
            SKELETON_BUILDER_TOOL_SPECS["op_minion_ask_question"]["alias"],
            "ask_question",
        )

    def test_architecture_review_pass_requires_semantic_contract_composition(self) -> None:
        description = str(
            SKELETON_BUILDER_TOOL_SPECS["op_minion_architecture_review_pass"]["description"]
        )

        self.assertIn("PASS with no arguments", description)
        self.assertIn("every module protocol is complete", description)
        self.assertIn("ownership and lifecycle close", description)
        self.assertIn("provider outputs satisfy consumer dependencies", description)
        self.assertIn("success and failure observations", description)
        self.assertIn("unresolved public semantic ambiguity", description)
        self.assertIn("partial-success failure path", description)
        self.assertIn("review_guarded implementation freedom", description)
        self.assertIn("hypothetical implementation behavior is not semantic proof", description)

    def test_architecture_submit_persists_requirement_mapping(self) -> None:
        (self.repo / "include").mkdir()
        (self.repo / "include" / "router.h").write_text(_contract("router"), encoding="utf-8")
        requirements_path = self.runtime_root / "requirements.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        workspace = self._bind_builder_workspace({
            "repo_path": str(self.repo),
            "reference_paths": [{"name": "requirements", "path": str(requirements_path)}],
            "artifact_dir": str(self.runtime_root / "artifacts"),
            "artifact_stage_dir": str(self.runtime_root / "artifact-stage"),
        }, role="architect", mode="author")
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
        self.assertEqual(set(artifact), {"requirements", "modules", "scenarios"})
        self.assertEqual(
            artifact["scenarios"]["router_end_to_end"]["requirement_refs"],
            ["deterministic_routing"],
        )

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
            mode="author",
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

    def test_architecture_review_fail_keeps_structured_locations(self) -> None:
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
        }, role="reviewer", mode="architecture")
        finding = self._builder_call(
            workspace,
            ADD_FINDING_CAPABILITY,
            {
                "finding_key": "invented_router_contract_incomplete",
                "finding_kind": "contract_defect",
                "priority": "p1",
                "summary": "invented_router does not expose the required deterministic consumer contract.",
                "locations": [{"scope": "workspace", "file": "README.md", "line": 1}],
            },
        )
        self.assertTrue(finding.ok, finding.text)
        result = self._builder_call(workspace, "op_minion_architecture_review_fail")
        self.assertTrue(result.ok, result.text)
        artifact = json.loads(
            (Path(workspace["artifact_stage_dir"]) / "architecture_review.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(artifact["findings"][0]["finding_key"], "invented_router_contract_incomplete")
        self.assertEqual(artifact["findings"][0]["locations"][0]["file"], "README.md")
        self.assertNotIn("findings_markdown", artifact)

    def test_architecture_review_finding_accepts_semantic_task_ledger_citation(self) -> None:
        requirements_path = self.runtime_root / "review-requirements.json"
        requirements_path.write_text(json.dumps(self.requirements), encoding="utf-8")
        workspace = self._bind_builder_workspace({
            "repo_path": str(self.repo),
            "reference_paths": [{"name": "requirements", "path": str(requirements_path)}],
            "artifact_dir": str(self.runtime_root / "review-artifacts"),
            "artifact_stage_dir": str(self.runtime_root / "review-stage"),
        }, role="reviewer", mode="architecture")
        finding = self._builder_call(
            workspace,
            ADD_FINDING_CAPABILITY,
            {
                "finding_key": "deterministic_routing_omitted",
                "finding_kind": "requirements_defect",
                "priority": "p0",
                "summary": "The Skeleton omits deterministic routing required by the task source.",
                "locations": [{"scope": "task_ledger", "file": "task.yaml", "line": 3}],
            },
        )
        self.assertTrue(finding.ok, finding.llm_text)
        result = self._builder_call(workspace, "op_minion_architecture_review_fail")
        self.assertTrue(result.ok, result.llm_text)
        artifact = json.loads(
            (Path(workspace["artifact_stage_dir"]) / "architecture_review.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(artifact["verdict"], "FAIL")
        self.assertEqual(artifact["findings"][0]["locations"][0]["file"], "task.yaml")

    def test_architecture_review_submit_needs_no_positive_audit_bookkeeping(self) -> None:
        workspace = self._review_builder_workspace()
        result = self._builder_call(
            workspace,
            "op_minion_architecture_review_pass",
            {},
        )

        self.assertTrue(result.ok, result.llm_text)
        artifact = json.loads(
            (Path(workspace["artifact_stage_dir"]) / "architecture_review.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(artifact["verdict"], "PASS")
        self.assertEqual(artifact["review_scope"]["module_names"], ["router"])
        self.assertEqual(
            artifact["review_scope"]["requirement_names"],
            ["deterministic_routing"],
        )
        self.assertNotIn("obligation_traces", artifact)
        self.assertNotIn("decision_traces", artifact)
        self.assertNotIn("verification_node_names", artifact["review_scope"])

    def test_architecture_review_pass_rejects_legacy_trace_arguments(self) -> None:
        workspace = self._review_builder_workspace()
        result = self._builder_call(
            workspace,
            "op_minion_architecture_review_pass",
            {"decision_traces": []},
        )

        self.assertFalse(result.ok)
        self.assertIn("takes no arguments", result.llm_text)

    def test_architecture_review_finding_compiles_fail(self) -> None:
        workspace = self._review_builder_workspace()
        recorded = self._builder_call(
            workspace,
            ADD_FINDING_CAPABILITY,
            {
                "finding_key": "router_result_flow_missing",
                "finding_kind": "contract_defect",
                "priority": "p1",
                "summary": "router does not expose the consumer-visible result flow.",
                "locations": [{"scope": "workspace", "file": "include/router.h", "line": 1}],
            },
        )
        self.assertTrue(recorded.ok, recorded.llm_text)
        submitted = self._builder_call(workspace, "op_minion_architecture_review_fail")
        self.assertTrue(submitted.ok, submitted.llm_text)
        artifact = json.loads(
            (Path(workspace["artifact_stage_dir"]) / "architecture_review.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(artifact["verdict"], "FAIL")
        self.assertEqual(artifact["findings"][0]["finding_key"], "router_result_flow_missing")

    def test_architecture_review_tool_contract_binds_module_and_requirement_catalog(self) -> None:
        contract = compile_architecture_review_invocation_tool_contract(
            task_ledger={},
            architecture=self._submission(),
        )

        self.assertNotIn("hard_requirements", contract)
        self.assertEqual(contract["module_names"], ["router"])
        self.assertEqual(contract["requirement_names"], ["deterministic_routing"])
        self.assertEqual(contract["contract_version"], "6")
        self.assertNotIn("decision_pairs", contract)
        self.assertNotIn("pass_example", contract)
        self.assertNotIn("verification_node_names", contract)
        descriptions = json.dumps(contract["guidance_overrides"], ensure_ascii=False)
        self.assertIn("router", descriptions)
        self.assertIn("responsibility", descriptions)
        self.assertIn("lifecycle", descriptions)
        self.assertIn("Syntax and compilation are supporting checks", descriptions)
        self.assertIn("PASS is forbidden while any public input, state, or call-sequence ambiguity remains", descriptions)
        self.assertIn("absent/null/empty/zero-length", descriptions)
        self.assertIn("partial output followed by failure", descriptions)
        self.assertIn("copy/move/clone/share/reset/reuse semantics", descriptions)
        self.assertIn("compile probe cannot establish", descriptions)
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
        }, role="architect", mode="revision")
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
            mode="revision",
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
            mode="revision",
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
            mode="revision",
        )

        result = self._builder_call(workspace, "op_minion_architecture_submit")

        self.assertFalse(result.ok)
        self.assertIn("no source or semantic change", result.text)

    def test_revision_scope_is_rechecked_during_stable_snapshot(self) -> None:
        initial = self._provision_complete_workspace("stable-revision", "initial")
        base_ref = self.service.snapshot_architect_result(
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
        accepted_ref = self.service.snapshot_architect_result(
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
            self.service.snapshot_architect_result(
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

    def test_requirements_finding_keeps_task_ledger_location_immutable(self) -> None:
        scope = architecture_revision_scope(
            self._submission(),
            {
                "findings": [
                    {
                        "finding_kind": "requirements_defect",
                        "summary": "The task example contradicts its expected output.",
                        "locations": [
                            {
                                "scope": "task_ledger",
                                "file": "task.yaml",
                                "line": 78,
                            }
                        ],
                    }
                ]
            },
        )

        self.assertEqual(scope["immutable_requirement_paths"], ["task.yaml"])
        self.assertNotIn("task.yaml", scope["allowed_paths"])
        self.assertEqual(scope["affected_modules"], ["router"])
        self.assertTrue(scope["allow_topology_changes"])

    def test_yaml_revision_edit_replaces_one_dag_node(self) -> None:
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
            mode="revision",
        )
        produced: list[dict[str, object]] = []

        revised = load_architecture_draft(workspace)
        revised["modules"]["router"]["paths"]["reference_only"] = ["README.md"]
        write_architecture_draft(workspace, revised)
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
            {
                "module_kind",
                "behavior_kind",
                "responsibility",
                "dependencies",
                "contract",
                "ownership",
                "lifecycle",
                "state_machine",
                "paths",
            },
        )

    def test_invalid_yaml_schema_is_rejected_without_advancing_submission(self) -> None:
        base = self._submission()
        workspace = self._bind_builder_workspace(
            {
                "repo_path": str(self.repo),
                "artifact_dir": str(self.runtime_root / "invalid-upsert-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "invalid-upsert-stage"),
                "architecture_revision_base_submission": base,
            },
            role="architect",
            mode="revision",
        )

        draft_path = Path(str(workspace["architecture_draft_path"]))
        header, separator, live_document = draft_path.read_text(
            encoding="utf-8"
        ).rpartition("\nschema_version: 4\n")
        self.assertTrue(separator)
        draft_path.write_text(
            header
            + separator
            + live_document.replace(
                "      reference_only: []",
                "      reference_only: []\n      surprise: true",
            ),
            encoding="utf-8",
        )
        result = self._builder_call(workspace, "op_minion_architecture_submit")

        self.assertFalse(result.ok)
        self.assertEqual(result.structured["error_type"], "ArchitectureDraftFileError")
        self.assertEqual(result.structured["code"], "schema_validation_failed")
        self.assertIn("modules.router.paths.surprise", str(result.structured["errors"]))

    def test_invalid_module_is_excluded_from_graph_analysis_without_key_error(self) -> None:
        submission = self._submission()
        submission["modules"]["router"] = {
            "module_kind": "implementation",
            "paths": {"contract_paths": []},
        }

        with self.assertRaises(ArchitectureValidationError) as raised:
            validate_architecture_submission(
                submission,
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

        report = raised.exception.to_dict()
        self.assertEqual(report["error_type"], "ArchitectureValidationError")
        self.assertGreaterEqual(len(report["errors"]), 2)
        self.assertNotIn("KeyError", str(report))

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
        base_ref = self.service.snapshot_architect_result(
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

        accepted_ref = self.service.snapshot_architect_result(
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
        }, role="architect", mode="revision")
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
        }, role="architect", mode="revision")
        produced: list[dict[str, object]] = []

        revised = load_architecture_draft(workspace)
        route_status = json.loads(json.dumps(revised["modules"]["router"]))
        route_status["module_kind"] = "contract_only"
        route_status["responsibility"] = "define the shared route status shape"
        route_status["paths"] = {
            "contract_mode": "file_frozen",
            "contract_paths": ["include/route_status.h"],
            "implementation_scopes": [],
            "reference_only": [],
        }
        revised["modules"]["route_status"] = route_status
        write_architecture_draft(workspace, revised)
        result = self._builder_call(
            workspace, "op_minion_architecture_submit", produced=produced
        )

        self.assertTrue(result.ok, result.text)
        merged = json.loads(Path(produced[0]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(set(merged["modules"]), {"router", "route_status"})

    def test_software_workflow_imports_skeleton_and_rejects_legacy_contract_graph(self) -> None:
        workspace = self._provision_complete_workspace("external", "initial")
        skeleton_ref = self.service.snapshot_architect_result(
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
                "profile": "software_engineering.v2_coder",
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
        other_requirements_ref = workflows.task_ledger.publish(
            title="Different task source",
            task_spec={"objective": "Implement different behavior."},
            actor="test",
            source_channel="test",
        )
        with self.assertRaisesRegex(
            ValueError,
            "task ledger differs from the imported architecture",
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
        initial_ref = self.service.snapshot_architect_result(
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
        changed_ref = self.service.snapshot_architect_result(
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
        manifest_ref = self.service.snapshot_architect_result(
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
        worker = SemanticOrchestrator(
            workflows,
            publish_human_review=lambda payload: _record_async(published, payload),
        )
        effect = {
            "effect_type": "publish_architecture_review_request",
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
        self.assertIn("task.yaml", attachment_names)
        projected_ledger = yaml.safe_load(
            Path(attachment_names["task.yaml"]).read_text(encoding="utf-8")
        )
        self.assertEqual(projected_ledger, self.requirements)
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
            "requirements": {
                "deterministic_routing": {
                    "claim": "routing remains deterministic for empty and populated rule sets",
                    "owner": "router",
                    "contract_path": ["router::route -> router public result"],
                }
            },
            "modules": {
                "router": {
                    "module_kind": "implementation",
                    "behavior_kind": "service",
                    "responsibility": "select one deterministic route from immutable rules",
                    "dependencies": {},
                    "contract": {
                        "inputs": {
                            "route_request": {
                                "interface": "router::route",
                                "semantics": "accept one immutable route request",
                            }
                        },
                        "outputs": {
                            "route_result": {
                                "interface": "router::route",
                                "semantics": "return the same selected route for the same rules and input",
                            }
                        },
                        "errors": ["invalid rules fail deterministically"],
                        "invariants": ["routing does not mutate the configured rules"],
                    },
                    "ownership": ["each router owns its configured immutable rules"],
                    "lifecycle": {
                        "creation": "constructs from validated immutable rules",
                        "operation": "routes requests without mutating rules",
                        "shutdown": "accepts no new requests after destruction begins",
                        "failure": "returns a deterministic invalid-rule error",
                        "cleanup": "releases owned rules at destruction",
                    },
                    "state_machine": None,
                    "paths": {
                        "contract_mode": "file_frozen",
                        "contract_paths": ["include/router.h"],
                        "implementation_scopes": [{"kind": "file", "path": "src/router.cpp"}],
                        "reference_only": [],
                    },
                }
            },
            "scenarios": {
                "router_end_to_end": {
                    "modules": ["router"],
                    "requirement_refs": ["deterministic_routing"],
                    "entrypoint": "tests/test_router.cpp",
                    "contract_flow": ["consumer -> router::route -> route_result"],
                    "observable_behavior": "A consumer can route one rule through the public router contract.",
                    "failure_behavior": "Invalid rules produce the declared deterministic error.",
                    "environment": "Project host test environment",
                }
            },
        }

    def _review_builder_workspace(self) -> dict[str, object]:
        contract_path = self.repo / "include" / "router.h"
        contract_path.parent.mkdir(parents=True, exist_ok=True)
        contract_path.write_text(_contract("router"), encoding="utf-8")
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
            role="reviewer",
            mode="architecture",
        )

    def _bind_builder_workspace(
        self,
        workspace: dict[str, object],
        *,
        role: str,
        mode: str,
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
                    "mode": mode,
                    "authoring_input_fingerprint": f"input-{self.builder_lease_index}",
                    "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
                },
            }
        )
        if role == "architect":
            prepare_architecture_draft_file(workspace)
        return workspace

    def _builder_call(
        self,
        workspace: dict[str, object],
        name: str,
        args: dict[str, object] | None = None,
        produced: list[dict[str, object]] | None = None,
    ):
        self.builder_call_index += 1
        call = CanonicalToolCall(
            name=name,
            args=args or {},
            call_id=f"builder-call-{self.builder_call_index}",
        )
        if name == ADD_FINDING_CAPABILITY:
            return add_finding_tool_result(call, workspace)
        return skeleton_builder_tool_result(
            call,
            workspace,
            produced if produced is not None else [],
        )

    def _author_submission(
        self,
        workspace: dict[str, object],
        submission: dict[str, object],
    ) -> None:
        write_architecture_draft(workspace, submission)


if __name__ == "__main__":
    unittest.main()
