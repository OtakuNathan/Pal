from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.core import PalCore
from pal.execution import register_with_core as register_execution_with_core
from pal.minion.catalog import MinionCatalogService
from pal.minion.profiles import MinionProfileRegistry, resolve_pinned_minion_pack
from pal.minion.scoped_execution import (
    MinionScopedExecutionRuntime,
    MinionScopedExecutionShellInput,
)
from pal.minion.workspace_tools import _normalized_reference_paths
from pal.execution.contracts import CapabilityResult
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import EmptyToolInput, OpaqueToolOutput, Tool, ToolGuidance
from pal.execution.tool_semantics import DIRECT_EXTERNAL_READ
from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.adapters import prepare_v2_role_workspace, prepare_v2_workspace_environment
from pal.minion.v2.architecture import ArchitectureArtifactService
from pal.minion.v2.catalog import MinionV2Catalog
from pal.minion.v2.contracts import AggregateType
from pal.minion.v2.execution import ExecutionCompiler
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.semantic_orchestration import (
    apply_v2_research_capability_policy,
    apply_v2_role_capability_policy,
)
from pal.minion.v2.semantic_orchestration.orchestrator import (
    _role_mode_profile_payload,
)
from pal.shared import MinionInvocationPack, RuntimeStatus
from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult


class MinionV2FamilyBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_v2_family_"))
        self.repository = MinionV2Repository(self.root)
        self.store = ContentAddressedArtifactStore(self.root, self.repository)

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def _pack(self, profile: str) -> MinionInvocationPack:
        group, name = profile.split(".", 1)
        return MinionProfileRegistry(runtime_root=self.root).resolve_pack(
            MinionInvocationPack(
                invocation_id="inv-family",
                goal="test",
                profile_group=group,
                profile_name=name,
                minion_profile=profile,
            )
        )

    def test_lifestyle_binding_resolves_all_roles_and_artifact_adapters(self) -> None:
        ref = MinionV2Catalog(self.root, self.store).publish_family_binding(
            "lifestyle.nutrition_checkin_producer"
        )
        binding = self.store.read_json(ref)
        self.assertEqual(binding["schema_version"], "3")
        self.assertEqual(binding["workflow_template"], "contract_dag.v2")
        self.assertEqual(
            set(binding["role_bindings"]),
            {"architect", "reviewer", "implementation", "verifier"},
        )
        self.assertEqual(set(binding["adapters"].values()), {"artifact_bundle.v2"})
        self.assertEqual(
            {
                item["executor_profile"]["canonical_profile_id"]
                for item in binding["role_bindings"].values()
            },
            {"lifestyle.nutrition_checkin_producer"},
        )
        self.assertEqual(binding["policies"]["llm"]["temperature"], 0.05)
        self.assertEqual(binding["policies"]["llm"]["llm_round_timeout_seconds"], 3000)

    def test_general_family_is_a_complete_data_driven_contract_dag(self) -> None:
        ref = MinionV2Catalog(self.root, self.store).publish_family_binding("generic")
        binding = self.store.read_json(ref)
        self.assertEqual(binding["workflow_template"], "contract_dag.v2")
        self.assertEqual(
            set(binding["role_bindings"]),
            {"architect", "reviewer", "implementation", "verifier"},
        )
        self.assertEqual(set(binding["adapters"].values()), {"artifact_bundle.v2"})
        self.assertEqual(binding["primary_profile"]["canonical_profile_id"], "generic")

    def test_family_binding_pins_profile_definition_across_catalog_refresh(self) -> None:
        ref = MinionV2Catalog(self.root, self.store).publish_family_binding(
            "software_engineering.v2_coder"
        )
        binding = self.store.read_json(ref)
        pinned = dict(binding["role_bindings"]["implementation"]["executor_profile"])
        original_name = str(pinned["display_name"])

        MinionCatalogService(self.root).set_profile_override(
            profile="software_engineering.v2_coder",
            changes={"display_name": "Future Workflow Coder"},
        )
        current = MinionProfileRegistry(runtime_root=self.root).get("software_engineering.v2_coder")
        resolved = resolve_pinned_minion_pack(
            MinionInvocationPack(
                invocation_id="inv-pinned-profile",
                goal="test",
                profile_group="software_engineering",
                profile_name="v2_coder",
                minion_profile="software_engineering.v2_coder",
            ),
            profile_payload=pinned,
            family_payload=dict(binding),
        )

        self.assertEqual(current.display_name, "Future Workflow Coder")
        self.assertEqual(resolved.resolved_profile["display_name"], original_name)

    def test_software_family_selects_internal_role_profiles_independent_of_task_profile(self) -> None:
        ref = MinionV2Catalog(self.root, self.store).publish_family_binding(
            "software_engineering.v2_architect"
        )
        binding = self.store.read_json(ref)

        self.assertEqual(
            binding["primary_profile"]["canonical_profile_id"],
            "software_engineering.v2_architect",
        )
        self.assertEqual(
            {
                role: value["executor_profile"]["canonical_profile_id"]
                for role, value in binding["role_bindings"].items()
            },
            {
                "architect": "software_engineering.v2_architect",
                "reviewer": "software_engineering.v2_reviewer",
                "implementation": "software_engineering.v2_coder",
                "verifier": "software_engineering.v2_verifier",
            },
        )
        self.assertEqual(
            binding["role_bindings"]["implementation"]["selector"],
            "software_engineering.v2_coder",
        )

    def test_software_task_creation_cannot_bind_architect_as_implementation(self) -> None:
        service = MinionV2WorkflowService(self.root)
        created = service.create_task(
            {
                "title": "Software task",
                "objective": "Exercise the software family role binding",
                "profile": "software_engineering.v2_architect",
                "workspace": {
                    "kind": "new_project",
                    "project_name": "software-role-binding",
                    "primary_language": "python",
                },
            }
        )

        binding = self.store.read_json(created["family_binding_ref"])
        self.assertEqual(
            binding["role_bindings"]["implementation"]["executor_profile"][
                "canonical_profile_id"
            ],
            "software_engineering.v2_coder",
        )

    def test_architect_cannot_bypass_builder_but_producer_can_write_workspace(self) -> None:
        planner = apply_v2_role_capability_policy(
            self._pack("lifestyle.architect"),
            activation=RoleActivation(OrchestrationRole.ARCHITECT, RoleMode.AUTHOR),
        )
        self.assertIn("op_minion_contract_submit", planner.allowed_capabilities)
        self.assertNotIn("op_file_write", planner.allowed_capabilities)
        self.assertNotIn("op_minion_artifact_write", planner.allowed_capabilities)

        producer = apply_v2_role_capability_policy(
            self._pack("lifestyle.nutrition_checkin_producer"),
            activation=RoleActivation(OrchestrationRole.IMPLEMENTATION, RoleMode.PRODUCE),
        )
        self.assertIn("op_file_write", producer.allowed_capabilities)
        self.assertIn("op_minion_artifact_write", producer.allowed_capabilities)
        self.assertIn("op_minion_candidate_update_checklist", producer.allowed_capabilities)
        self.assertIn("op_minion_candidate_submit", producer.allowed_capabilities)
        for capability in (
            "op_minion_developer_test",
            "op_minion_developer_compile_check",
            "op_minion_developer_lsp_check",
            "op_minion_developer_check_unavailable",
        ):
            self.assertNotIn(capability, producer.allowed_capabilities)
        self.assertNotIn("op_web_search", producer.allowed_capabilities)

    def test_task_creation_requires_primary_profile_and_ignores_no_family_shortcut(self) -> None:
        service = MinionV2WorkflowService(self.root)
        with self.assertRaisesRegex(ValueError, "explicit primary minion profile"):
            service.create_task(
                {
                    "title": "Unbound task",
                    "objective": "Must not infer an executor from a family",
                    "family_id": "software_engineering",
                    "workspace": {"kind": "new_project", "project_name": "unbound"},
                }
            )

    def test_artifact_producer_cannot_fabricate_manager_submission_report(self) -> None:
        repo = self.root / "guard-repo"
        repo.mkdir()
        scoped = MinionScopedExecutionRuntime(
            ExecutionRuntime(),
            ["op_minion_artifact_write"],
            workspace={
                "repo_path": str(repo),
                "artifact_dir": str(self.root / "guard-artifacts"),
                "artifact_stage_dir": str(self.root / "guard-stage"),
                "manager_owned_submission_paths": ["producer_report.json"],
            },
        )
        rejected = asyncio.run(
            scoped.execute_tool_async(
                CanonicalToolCall(
                    name="artifact_write",
                    args={
                        "relative_path": "producer_report.json",
                        "content": "{}",
                    },
                )
            )
        )
        self.assertFalse(rejected.ok)
        self.assertIn("Manager-owned", rejected.llm_text)

        product = asyncio.run(
            scoped.execute_tool_async(
                CanonicalToolCall(
                    name="artifact_write",
                    args={
                        "relative_path": "checkin.json",
                        "content": '{"status":"recorded"}',
                        "role": "deliverable",
                    },
                )
            )
        )
        self.assertTrue(product.ok, product.text)

    def test_verifier_can_only_submit_through_the_schema_bound_builder(self) -> None:
        software = apply_v2_role_capability_policy(
            self._pack("software_engineering.v2_verifier"),
            activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
        )
        self.assertIn("op_minion_verification_pass", software.allowed_capabilities)
        self.assertIn(
            "op_minion_verification_request_module_repair",
            software.allowed_capabilities,
        )
        self.assertIn(
            "op_minion_verification_run_diff_risk",
            software.allowed_capabilities,
        )
        self.assertIn(
            "op_minion_verification_run_historical_regression",
            software.allowed_capabilities,
        )
        self.assertIn("op_file_read", software.allowed_capabilities)
        self.assertNotIn("op_minion_verification_submit", software.allowed_capabilities)
        self.assertIn("op_file_write", software.allowed_capabilities)
        self.assertNotIn(
            "op_minion_verification_scratch_write",
            software.allowed_capabilities,
        )
        system = apply_v2_role_capability_policy(
            self._pack("software_engineering.v2_verifier"),
            activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.SYSTEM),
        )
        self.assertIn(
            "op_minion_verification_scratch_write",
            system.allowed_capabilities,
        )

        for profile in ("general.verifier", "lifestyle.verifier"):
            with self.subTest(profile=profile):
                verifier = apply_v2_role_capability_policy(
                    self._pack(profile),
                    activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
                )
                self.assertIn("op_minion_verification_pass", verifier.allowed_capabilities)
                self.assertIn(
                    "op_minion_verification_request_module_repair",
                    verifier.allowed_capabilities,
                )
                self.assertNotIn(
                    "op_minion_verification_scratch_write",
                    verifier.allowed_capabilities,
                )
                self.assertNotIn("op_minion_verification_submit", verifier.allowed_capabilities)
                self.assertIn("op_exec_shell", verifier.allowed_capabilities)
                self.assertNotIn("op_minion_artifact_write", verifier.allowed_capabilities)
                self.assertNotIn("op_minion_artifact_edit", verifier.allowed_capabilities)

        standalone = apply_v2_role_capability_policy(
            self._pack("software_engineering.v2_reviewer"),
            activation=RoleActivation(OrchestrationRole.REVIEWER, RoleMode.STANDALONE),
        )
        self.assertIn("op_minion_standalone_review_submit", standalone.allowed_capabilities)
        self.assertIn("op_exec_shell", standalone.allowed_capabilities)
        self.assertNotIn("op_minion_artifact_write", standalone.allowed_capabilities)
        self.assertNotIn("op_minion_verification_submit", standalone.allowed_capabilities)

        for profile in (
            "general.generic",
            "lifestyle.nutrition_checkin_producer",
        ):
            for activation in (
                RoleActivation(OrchestrationRole.REVIEWER, RoleMode.ARCHITECTURE),
                RoleActivation(OrchestrationRole.REVIEWER, RoleMode.STANDALONE),
                RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
            ):
                with self.subTest(profile=profile, activation=activation):
                    bound = apply_v2_role_capability_policy(
                        self._pack(profile), activation=activation
                    )
                    self.assertIn("op_minion_add_finding", bound.allowed_capabilities)

        coder = apply_v2_role_capability_policy(
            self._pack("software_engineering.v2_coder"),
            activation=RoleActivation(OrchestrationRole.IMPLEMENTATION, RoleMode.PRODUCE),
        )
        self.assertIn("op_minion_candidate_submit", coder.allowed_capabilities)
        self.assertNotIn("op_minion_artifact_write", coder.allowed_capabilities)

    def test_verifier_profile_compiles_to_one_mode_specific_contract(self) -> None:
        base = dict(
            self._pack("software_engineering.v2_verifier").resolved_profile
        )

        module = _role_mode_profile_payload(base, mode="module")
        system = _role_mode_profile_payload(base, mode="system")

        self.assertIn("exactly one module Candidate", module["identity_fragment"])
        self.assertIn("Do not search for task.yaml", module["behavior_fragment"])
        self.assertNotIn(
            "single workflow-level System Verifier",
            module["behavior_fragment"],
        )
        self.assertIn(
            "single workflow-level System Verifier",
            system["identity_fragment"],
        )
        self.assertIn("Do not split scenarios into workers", system["behavior_fragment"])
        self.assertIn("real system and delivery tests", system["behavior_fragment"])
        self.assertNotEqual(module["behavior_fragment"], system["behavior_fragment"])

    def test_role_binding_replaces_the_shared_profiles_output_contract(self) -> None:
        cases = (
            (
                "general.generic",
                RoleActivation(OrchestrationRole.ARCHITECT, RoleMode.AUTHOR),
                "architecture_bundle.json",
                ["ArchitecturePlanningStageOutput"],
            ),
            (
                "lifestyle.nutrition_checkin_producer",
                RoleActivation(OrchestrationRole.REVIEWER, RoleMode.ARCHITECTURE),
                "architecture_review.json",
                ["ArchitectureReviewStageOutput"],
            ),
            (
                "general.generic",
                RoleActivation(OrchestrationRole.REVIEWER, RoleMode.STANDALONE),
                "standalone_review.json",
                ["StandaloneReviewReport"],
            ),
            (
                "general.generic",
                RoleActivation(OrchestrationRole.VERIFIER, RoleMode.SYSTEM),
                "verification_submission.json",
                ["SemanticVerificationSubmissionArtifact"],
            ),
            (
                "lifestyle.nutrition_checkin_producer",
                RoleActivation(OrchestrationRole.IMPLEMENTATION, RoleMode.REPAIR),
                "producer_report.json",
                ["UnitProducerReport", "UnitSplitRequest"],
            ),
            (
                "software_engineering.v2_architect",
                RoleActivation(OrchestrationRole.ARCHITECT, RoleMode.REVISION),
                "architecture_submission.json",
                ["ArchitectureSkeletonSubmission"],
            ),
            (
                "software_engineering.v2_reviewer",
                RoleActivation(OrchestrationRole.REVIEWER, RoleMode.ARCHITECTURE),
                "architecture_review.json",
                ["ArchitectureReviewStageOutput"],
            ),
            (
                "software_engineering.v2_coder",
                RoleActivation(OrchestrationRole.IMPLEMENTATION, RoleMode.PRODUCE),
                "coder_report.json",
                ["ModuleCoderReport", "ModuleSplitRequest"],
            ),
        )
        for profile, activation, primary_artifact, output_types in cases:
            with self.subTest(profile=profile, activation=activation):
                bound = apply_v2_role_capability_policy(
                    self._pack(profile),
                    activation=activation,
                )
                workspace_policy = dict(bound.workspace["output_policy"])
                resolved_policy = dict(
                    bound.resolved_profile["effective_output_policy"]
                )
                self.assertEqual(workspace_policy["primary_artifact"], primary_artifact)
                self.assertEqual(workspace_policy["allowed_output_types"], output_types)
                self.assertEqual(resolved_policy, workspace_policy)

    def test_verification_submit_is_hydrated_in_the_scoped_runtime(self) -> None:
        scoped = MinionScopedExecutionRuntime(
            ExecutionRuntime(),
            ["op_minion_verification_submit"],
            workspace={
                "artifact_dir": str(self.root / "artifacts"),
                "artifact_stage_dir": str(self.root / "artifact-stage"),
            },
        )
        spec = scoped.get_capability_spec("op_minion_verification_submit")
        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec["name"], "verification_submit")
        self.assertEqual(
            scoped.resolve_capability_address("verification_submit"),
            "op_minion_verification_submit",
        )
        self.assertEqual(
            scoped.get_capability_spec("verification_submit")["name"],
            "verification_submit",
        )
        self.assertFalse(spec["input_schema"]["additionalProperties"])
        self.assertEqual(spec["input_schema"]["properties"], {})

        result = asyncio.run(
            scoped.execute_tool_async(
                CanonicalToolCall(
                    name="verification_submit",
                    args={
                        "cases": [
                            {
                                "name": "bounded smoke probe",
                                "case_kind": "unit",
                                "command": ["python", "-m", "pytest", "-q"],
                                "expected_exit_codes": [0],
                                "requirements": [],
                                "locations": [],
                                "invariants": [],
                                "description": "Run the focused project suite.",
                            }
                        ],
                        "findings": [],
                        "reviewer_summary": "Run the bounded smoke probe.",
                    },
                )
            )
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.status, "invalid_arguments")
        self.assertIn("Extra inputs are not permitted", result.llm_text)
        self.assertFalse((self.root / "artifact-stage" / "verification_plan.json").exists())

    def test_worker_only_canonical_names_are_exposed_through_role_native_aliases(self) -> None:
        scoped = MinionScopedExecutionRuntime(
            ExecutionRuntime(),
            [
                "op_minion_contract_submit",
                "op_minion_candidate_submit",
                "op_minion_ask_question",
            ],
            workspace={
                "artifact_dir": str(self.root / "alias-artifacts"),
                "artifact_stage_dir": str(self.root / "alias-stage"),
            },
        )

        expected = {
            "op_minion_contract_submit": "contract_submit",
            "op_minion_candidate_submit": "candidate_submit",
            "op_minion_ask_question": "ask_question",
        }
        for canonical, public_name in expected.items():
            with self.subTest(canonical=canonical):
                spec = scoped.get_capability_spec(canonical)
                self.assertIsNotNone(spec)
                assert spec is not None
                self.assertEqual(spec["name"], public_name)
                self.assertEqual(scoped.resolve_capability_address(public_name), canonical)
                self.assertNotIn("minion_", spec["description"])

    def test_architect_has_no_external_research_surface(self) -> None:
        architect = self._pack("lifestyle.architect")
        self.assertNotIn("op_web_search", architect.allowed_capabilities)
        self.assertNotIn("op_web_read", architect.allowed_capabilities)
        local = apply_v2_research_capability_policy(architect, research_mode="local_only")
        self.assertNotIn("op_web_search", local.allowed_capabilities)
        self.assertNotIn("op_web_read", local.allowed_capabilities)

    def test_software_architect_gets_web_only_when_research_mode_allows_it(self) -> None:
        architect = self._pack("software_engineering.v2_architect")
        self.assertIn("op_web_search", architect.allowed_capabilities)
        self.assertIn("op_web_read", architect.allowed_capabilities)
        local = apply_v2_research_capability_policy(architect, research_mode="local_only")
        external = apply_v2_research_capability_policy(architect, research_mode="external_allowed")
        self.assertNotIn("op_web_search", local.allowed_capabilities)
        self.assertNotIn("op_web_read", local.allowed_capabilities)
        self.assertIn("op_web_search", external.allowed_capabilities)
        self.assertIn("op_web_read", external.allowed_capabilities)

    def test_architect_roles_receive_only_contract_builder(self) -> None:
        for profile in ("general.architect", "lifestyle.architect"):
            with self.subTest(profile=profile):
                requirements = self._pack(profile)
                self.assertNotIn("op_minion_requirements_replace_batch", requirements.allowed_capabilities)
                self.assertNotIn("op_minion_requirements_submit", requirements.allowed_capabilities)
                self.assertIn("op_file_read", requirements.allowed_capabilities)
                self.assertNotIn("op_minion_input_read", requirements.allowed_capabilities)
                self.assertNotIn("op_minion_evidence_submit", requirements.allowed_capabilities)
                self.assertIn("op_minion_contract_submit", requirements.allowed_capabilities)
                self.assertNotIn("op_file_write", requirements.allowed_capabilities)
                self.assertEqual(requirements.workspace.get("workspace_policy", {}).get("mode"), "read_only_repo")

        software = self._pack("software_engineering.v2_architect")
        self.assertIn("op_minion_architecture_submit", software.allowed_capabilities)
        self.assertNotIn("op_minion_contract_submit", software.allowed_capabilities)
        self.assertNotIn("op_minion_artifact_edit", software.allowed_capabilities)
        self.assertNotIn("op_minion_task_revision_submit", software.allowed_capabilities)
        self.assertIn("op_file_write", software.allowed_capabilities)
        self.assertIn("op_file_edit", software.allowed_capabilities)
        self.assertEqual(software.workspace.get("workspace_policy", {}).get("mode"), "writable_git_branch")

    def test_file_reader_uses_sandbox_visible_path_without_reference_selector(self) -> None:
        bound = self.root / "workflow-request.json"
        bound.write_text('{"goal":"bounded"}\n', encoding="utf-8")

        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        generic_reader = MinionScopedExecutionRuntime(
            core.context.execution_runtime,
            ["op_file_read"],
            workspace={"repo_path": str(self.root)},
        )
        spec = generic_reader.get_capability_spec("op_file_read")
        assert spec is not None
        properties = dict(spec["input_schema"]["properties"])
        self.assertIn("file_path", properties)
        self.assertNotIn("root", properties)
        self.assertNotIn("reference_name", properties)

        result = asyncio.run(
            generic_reader.execute_tool_async(
                CanonicalToolCall(
                    name="read_file",
                    args={"file_path": str(bound)},
                )
            )
        )

        self.assertTrue(result.ok, result.text)
        self.assertIn('"bounded"', result.llm_text)

    def test_scoped_file_tools_reject_verifier_corpus_before_host_mutation(self) -> None:
        repo = self.root / "scoped-file-guard"
        product = repo / "src" / "router.py"
        verifier_case = repo / "tests" / "router" / "verification" / "test_router.py"
        product.parent.mkdir(parents=True)
        verifier_case.parent.mkdir(parents=True)
        product.write_text("old\n", encoding="utf-8")
        verifier_case.write_text("assert False\n", encoding="utf-8")
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        scoped = MinionScopedExecutionRuntime(
            core.context.execution_runtime,
            ["op_file_write"],
            workspace={
                "repo_path": str(repo),
                "write_path_scopes": [
                    {"kind": "directory", "path": "src"},
                ],
                "read_only_overlay_paths": [
                    "tests/router/verifier",
                ],
            },
        )

        rejected = asyncio.run(
            scoped.execute_tool_async(
                CanonicalToolCall(
                    name="write_file",
                    args={
                        "file_path": "tests/router/verifier/test_router.py",
                        "content": "assert True\n",
                    },
                )
            )
        )
        accepted = asyncio.run(
            scoped.execute_tool_async(
                CanonicalToolCall(
                    name="write_file",
                    args={
                        "file_path": "src/new_router.py",
                        "content": "new\n",
                    },
                )
            )
        )

        self.assertFalse(rejected.ok)
        self.assertEqual(rejected.structured["reason"], "path_not_writable")
        self.assertEqual(rejected.structured["policy_reason"], "read_only_overlay")
        self.assertEqual(verifier_case.read_text(encoding="utf-8"), "assert False\n")
        self.assertTrue(accepted.ok, accepted.llm_text)
        self.assertEqual((repo / "src" / "new_router.py").read_text(), "new\n")

    def test_private_workspace_search_is_not_registered(self) -> None:
        scoped = MinionScopedExecutionRuntime(
            ExecutionRuntime(),
            ["op_search"],
            workspace={"repo_path": str(self.root)},
        )
        self.assertIsNone(scoped.get_capability_spec("op_search"))
        self.assertNotIn("search", scoped.registry_generation.direct_aliases)

    def test_workspace_tool_replaces_inherited_descriptor_once(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")

        scoped = MinionScopedExecutionRuntime(
            core.context.execution_runtime,
            ["op_file_read"],
            workspace={"repo_path": str(self.root)},
        )

        generation = scoped.registry_generation
        self.assertEqual(list(generation.direct_aliases).count("read_file"), 1)
        self.assertEqual(
            generation.direct_aliases["read_file"].canonical_path,
            "op_file_read",
        )

    def test_verifier_keeps_the_truthful_shell_input_schema(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        base_description = str(
            core.context.execution_runtime.registry_generation.provider_specs["run_shell"][
                "function"
            ]["description"]
        )
        self.assertNotIn("assigned task", base_description)
        scoped = MinionScopedExecutionRuntime(
            core.context.execution_runtime,
            ["op_exec_shell"],
            workspace={
                "repo_path": str(self.root),
                "minion_v2": {"role": "verifier"},
            },
            capability_guidance_overrides={
                "op_exec_shell": {
                    "use_when": "Role-local shell use text.",
                    "do_not_use_when": "Role-local shell prohibition.",
                    "failure_next_steps": "Role-local recovery.",
                }
            },
        )

        spec = scoped.get_capability_spec("op_exec_shell")
        assert spec is not None
        properties = dict(spec["input_schema"]["properties"])
        self.assertIn("cwd", properties)
        self.assertEqual(properties["timeout_ms"]["default"], 180_000)
        self.assertEqual(
            MinionScopedExecutionShellInput.model_validate(
                {"cmd": "python -m pytest tests/"}
            ).timeout_ms,
            180_000,
        )
        provider = {
            item["function"]["name"]: item["function"]
            for item in scoped.build_llm_tool_contracts()
        }["run_shell"]
        description = str(provider["description"])
        self.assertIn("Use when: Use bounded workspace discovery", description)
        self.assertIn("rg --files, rg, find, grep, and ls", description)
        self.assertIn("Stay focused on your assigned task", description)
        self.assertIn("Once an exact file is known, call read_file", description)
        self.assertIn("Git is available here only for classified read-only inspection", description)
        self.assertIn("Git mutations and unknown Git subcommands are trapped", description)
        self.assertNotIn("Minion", description)
        self.assertIn("If a command is trapped, do not retry it", description)
        self.assertNotIn("Role-local", description)
        result = asyncio.run(
            scoped.execute_tool_async(
                CanonicalToolCall(
                    name="run_shell",
                    args={"cmd": "true"},
                    call_id="shell-default-timeout",
                )
            )
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.structured["timeout_ms"], 180_000)

        with self.assertRaisesRegex(ValueError, "unknown fields"):
            MinionScopedExecutionRuntime(
                core.context.execution_runtime,
                ["op_exec_shell"],
                capability_guidance_overrides={
                    "op_exec_shell": {"summary": "unstructured legacy override"}
                },
            )

    def test_workflow_tool_guidance_override_is_compiled_into_provider_surface(self) -> None:
        core = PalCore()
        register_execution_with_core(core.context)
        core.publish_module_capabilities("execution")
        scoped = MinionScopedExecutionRuntime(
            core.context.execution_runtime,
            ["op_file_read"],
            workspace={"repo_path": str(self.root)},
            capability_guidance_overrides={
                "op_file_read": {
                    "use_when": "Use this reader only after an exact path is known.",
                    "do_not_use_when": "Do not use it for broad repository archaeology.",
                }
            },
        )

        provider = {
            item["function"]["name"]: item["function"]
            for item in scoped.build_llm_tool_contracts()
        }["read_file"]

        self.assertIn(
            "Use when: Use this reader only after an exact path is known.",
            provider["description"],
        )
        self.assertIn(
            "Do not use when: Do not use it for broad repository archaeology.",
            provider["description"],
        )
        self.assertIn(
            "refer to the earlier read result and do not call read_file again",
            provider["description"],
        )

    def test_reference_normalization_preserves_semantic_reference_name(self) -> None:
        bound = self.root / "bound-input.json"
        bound.write_text('{"value":true}\n', encoding="utf-8")

        references = _normalized_reference_paths(
            {
                "reference_paths": [
                    {
                        "name": "module_work_view",
                        "path": str(bound),
                        "required": True,
                    }
                ]
            }
        )

        self.assertEqual(references[0]["name"], "module_work_view")
        self.assertTrue(references[0]["required"])

    def test_software_profiles_preserve_engineering_rules_and_lsp_surface(self) -> None:
        coder = self._pack("software_engineering.v2_coder")
        behavior = str(coder.resolved_profile["behavior_fragment"])
        self.assertIn("standard-library", behavior)
        self.assertIn("hand-rolled infrastructure", behavior)
        self.assertIn("fabricate an API", behavior)
        self.assertIn("test adapter", behavior)
        self.assertIn("production backend", behavior)
        self.assertIn("independent verification", behavior)
        self.assertIn("test_debugging", coder.allowed_skills)
        architect = self._pack("software_engineering.v2_architect")
        self.assertIn("op_exec_shell", architect.allowed_capabilities)
        self.assertNotIn("op_search", architect.allowed_capabilities)
        self.assertIn("op_file_read", architect.allowed_capabilities)
        self.assertNotIn("op_git", architect.allowed_capabilities)
        self.assertNotIn("op_tree", architect.allowed_capabilities)
        self.assertIn("op_lsp_definition", coder.allowed_capabilities)
        self.assertIn("op_lsp_references", coder.allowed_capabilities)
        self.assertIn("op_lsp_diagnostics", coder.allowed_capabilities)
        self.assertNotIn("op_lsp_prepare_workspace", coder.allowed_capabilities)
        self.assertNotIn("op_lsp_doctor", coder.allowed_capabilities)
        self.assertNotIn("op_minion_developer_note", coder.allowed_capabilities)
        self.assertNotIn("op_git", coder.allowed_capabilities)
        self.assertEqual(
            coder.workspace.get("workspace_policy", {}).get("mode"),
            "writable_git_branch",
        )
        self.assertEqual(
            self._pack("software_engineering.v2_verifier")
            .workspace.get("workspace_policy", {})
            .get("mode"),
            "writable_git_branch",
        )
        self.assertEqual(
            self._pack("software_engineering.v2_reviewer")
            .workspace.get("workspace_policy", {})
            .get("mode"),
            "read_only_repo",
        )
        overrides = dict(coder.resolved_profile["capability_guidance_overrides"])
        self.assertIn(
            "Manager-prepared and recognition-probed",
            overrides["op_lsp_status"]["use_when"],
        )
        self.assertIn("After reading relevant source", overrides["op_lsp_definition"]["use_when"])
        self.assertIn("do not prove runtime behavior", overrides["op_lsp_diagnostics"]["use_when"])
        self.assertNotIn("op_git", overrides)
        self.assertNotIn("op_exec_shell", overrides)

        for profile in (
            "software_engineering.v2_coder",
            "software_engineering.v2_reviewer",
            "software_engineering.v2_verifier",
        ):
            with self.subTest(profile=profile):
                capabilities = self._pack(profile).allowed_capabilities
                self.assertNotIn("op_search", capabilities)
                self.assertNotIn("op_path_delete", capabilities)

        reviewer_overrides = dict(
            self._pack("software_engineering.v2_reviewer").resolved_profile[
                "capability_guidance_overrides"
            ]
        )
        verifier_overrides = dict(
            self._pack("software_engineering.v2_verifier").resolved_profile[
                "capability_guidance_overrides"
            ]
        )
        self.assertNotIn("op_exec_shell", reviewer_overrides)
        self.assertNotIn("op_exec_shell", verifier_overrides)
        self.assertNotIn("op_git", reviewer_overrides)
        self.assertNotIn("op_git", verifier_overrides)
        self.assertNotIn(
            "op_git",
            self._pack("software_engineering.v2_reviewer").allowed_capabilities,
        )
        self.assertNotIn(
            "op_git",
            self._pack("software_engineering.v2_verifier").allowed_capabilities,
        )

        verifier_behavior = str(
            self._pack("software_engineering.v2_verifier").resolved_profile["behavior_fragment"]
        )
        self.assertIn("Manager-bound contract as adjudication truth", verifier_behavior)
        self.assertIn("smallest sufficient checks", verifier_behavior)
        self.assertNotIn("SystemVerificationWorkView", verifier_behavior)
        self.assertNotIn("tests/<module_name>/verifier", verifier_behavior)
        architecture_review_behavior = str(
            self._pack("software_engineering.v2_reviewer").resolved_profile[
                "behavior_fragment"
            ]
        )
        self.assertIn(
            "required production platform/API/backend must remain explicit",
            architecture_review_behavior,
        )
        self.assertIn("classify an unfamiliar bound path", architecture_review_behavior)
        self.assertIn(
            "Do not pass an unclassified bound reference path",
            reviewer_overrides["op_file_read"]["do_not_use_when"],
        )

        binding_ref = MinionV2Catalog(self.root, self.store).publish_family_binding(
            "software_engineering.v2_coder"
        )
        binding = self.store.read_json(binding_ref)
        self.assertEqual(
            binding["role_bindings"]["reviewer"]["executor_profile"]["canonical_profile_id"],
            "software_engineering.v2_reviewer",
        )
        self.assertEqual(binding["policies"]["llm"]["temperature"], 0.05)
        self.assertTrue(binding["policies"]["verification"]["require_warning_clean"])

        forbidden_author_fields = (
            "workflow_id",
            "revision_id",
            "module_id",
            "unit_id",
            "requirement_id",
            "evidence_id",
            "finding_id",
            "case_id",
            "artifact_sha",
            "json_pointer",
        )
        for profile_name in (
            "software_engineering.v2_coder",
            "software_engineering.v2_verifier",
            "software_engineering.v2_reviewer",
        ):
            output_contract = str(self._pack(profile_name).resolved_profile["output_contract_fragment"])
            for field_name in forbidden_author_fields:
                self.assertNotIn(field_name, output_contract, f"{profile_name} exposes {field_name}")

    def test_worker_authoring_tools_never_expose_manager_identity_fields(self) -> None:
        from pal.minion.v2.candidate_builder import CANDIDATE_BUILDER_TOOL_SPECS
        from pal.minion.v2.contract_builder import CONTRACT_BUILDER_TOOL_SPECS
        from pal.minion.v2.skeleton_builder import SKELETON_BUILDER_TOOL_SPECS
        from pal.minion.v2.swe_verification import SWE_VERIFICATION_TOOL_SPECS
        from pal.minion.v2.verification_builder import VERIFICATION_BUILDER_TOOL_SPECS

        forbidden_exact = {
            "handle",
            "refs",
            "json_pointer",
            "artifact_sha",
            "input_read",
        }

        def property_names(schema):
            result = []
            if not isinstance(schema, dict):
                return result
            for name, child in dict(schema.get("properties") or {}).items():
                result.append(str(name))
                result.extend(property_names(child))
            result.extend(property_names(schema.get("items")))
            return result

        tool_groups = (
            CANDIDATE_BUILDER_TOOL_SPECS,
            CONTRACT_BUILDER_TOOL_SPECS,
            SKELETON_BUILDER_TOOL_SPECS,
            SWE_VERIFICATION_TOOL_SPECS,
            VERIFICATION_BUILDER_TOOL_SPECS,
        )
        for group in tool_groups:
            for capability, spec in group.items():
                with self.subTest(capability=capability):
                    for name in property_names(spec["InputModel"].model_json_schema(mode="validation")):
                        lowered = name.casefold()
                        self.assertNotIn(lowered, forbidden_exact)
                        self.assertFalse(lowered.endswith("_id"), name)
                        self.assertFalse(lowered.endswith("_ref"), name)
                        self.assertFalse(lowered.endswith("_sha"), name)

    def test_scoped_architecture_provider_exposes_only_yaml_submit_contract(self) -> None:
        scoped = MinionScopedExecutionRuntime(
            ExecutionRuntime(),
            ["op_minion_architecture_submit"],
            workspace={},
        )

        provider = scoped.build_llm_tool_contracts()[0]["function"]

        self.assertEqual(provider["name"], "architecture_submit")
        self.assertEqual(provider["input_schema"]["properties"], {})
        self.assertIn("architecture.yaml", provider["description"])
        self.assertIn("dynamic snake_case maps", provider["description"])

    def test_product_requirements_do_not_absorb_family_workflow_policy(self) -> None:
        service = MinionV2WorkflowService(self.root)
        with self.assertRaisesRegex(ValueError, "normalized Requirements"):
            service.prepare_requirements(
                {
                    "title": "Tiny router",
                    "sections": {"Routing": ["Route requests deterministically."]},
                }
            )
        prepared = service.prepare_requirements(
            {
                "title": "Tiny router",
                "task_spec": {"objective": "Route requests deterministically."},
                # Workflow policy belongs to FamilyBindingArtifact even if a
                # caller accidentally includes it in this request envelope.
                "policies": {"verification": {"require_warning_clean": True}},
            }
        )
        task_ledger = self.store.read_json(prepared["requirements_ref"])
        binding = self.store.read_json(
            MinionV2Catalog(self.root, self.store).publish_family_binding(
                "software_engineering.v2_coder"
            )
        )

        self.assertEqual(task_ledger["title"], "Tiny router")
        self.assertEqual(
            task_ledger["original"],
            {"objective": "Route requests deterministically."},
        )
        self.assertNotIn("policies", task_ledger["original"])
        self.assertTrue(binding["policies"]["verification"]["require_warning_clean"])

    def test_software_architecture_and_verification_profiles_preserve_rigorous_methods(self) -> None:
        architect = str(self._pack("software_engineering.v2_architect").resolved_profile["behavior_fragment"])
        architecture_review = str(
            self._pack("software_engineering.v2_reviewer").resolved_profile["behavior_fragment"]
        )
        coder = str(
            self._pack("software_engineering.v2_coder").resolved_profile[
                "behavior_fragment"
            ]
        )
        verifier_profile = dict(
            self._pack("software_engineering.v2_verifier").resolved_profile
        )
        verifier = str(
            _role_mode_profile_payload(
                verifier_profile,
                mode="module",
            )["behavior_fragment"]
        )
        system_verifier = str(
            _role_mode_profile_payload(
                verifier_profile,
                mode="system",
            )["behavior_fragment"]
        )
        generic = str(self._pack("general.generic").resolved_profile["behavior_fragment"])

        # Stable role philosophy remains explicit even though invocation-specific
        # and mechanically enforced rules are no longer duplicated here.
        self.assertIn("immutable task.yaml ledger as product truth", architect)
        self.assertIn("one bounded consistency pass", architect)
        self.assertIn("newer text wins only where meanings conflict", architect)
        self.assertIn("Mechanically verify examples", architect)
        self.assertIn("call ask_question and wait", architect)
        self.assertIn("Design the smallest complete system at module level", architect)
        self.assertIn("Do not read architecture.yaml during discovery", architect)
        self.assertIn("Once the design is settled", architect)
        self.assertIn("immediately begin file-edit tool calls", architect)
        self.assertIn("do not spend another response restating", architect)
        self.assertIn("every state, worker, object, and resource has exactly one owner", architect)
        self.assertIn("lifecycle transitions and composition joins close", architect)
        self.assertIn("private implementation is explicitly deferred", architect)
        self.assertIn("one acyclic Contract Dependency Graph", architect)
        self.assertIn("A composition root or runtime entrypoint is a product module", architect)
        self.assertIn("Build commands, manifests, test runners", architect)
        self.assertIn("meaningful end-to-end scenarios", architect)
        self.assertIn("established project or language/runtime primitives", architect)
        self.assertIn("type system and compiler can reject invalid programs early", architect)
        self.assertIn("C++ may use strong types, RAII, concepts, or SFINAE", architect)
        self.assertIn("participates in overload resolution", architect)
        self.assertIn("later function-body failure does not satisfy it", architect)
        self.assertIn("without unnecessary dynamic allocation", architect)
        self.assertIn("object-address side table", architecture_review)
        self.assertIn("Audit requirement preservation", architecture_review)
        self.assertIn("Review semantic composition, not private implementation", architecture_review)
        self.assertIn("Every state, worker, object, and resource needs one owner", architecture_review)
        self.assertIn("Perform an ambiguity audit", architecture_review)
        self.assertIn("partial output followed by error", architecture_review)
        self.assertIn("copy/move/share/reset/reuse", architecture_review)
        self.assertIn("Compilation and LSP support", architecture_review)
        self.assertIn("Tests remain verification evidence", architecture_review)
        self.assertIn("On revision, regress the prior finding", architecture_review)
        self.assertIn("disposition=advisory with priority=p2", architecture_review)
        self.assertIn("Never block acceptance for a stylistic type-level abstraction", architecture_review)
        self.assertIn("missing enforcement is a blocking contract_defect", architecture_review)
        self.assertIn("positive and negative declaration probe", architecture_review)

        self.assertIn("Accepted Skeleton declarations", coder)
        self.assertIn("dependency's public contract as an axiom", coder)
        self.assertIn("Work depth-first inside the owned module", coder)
        self.assertIn("standard-library", coder)
        self.assertIn("hand-rolled infrastructure", coder)
        self.assertIn("Functional Core / Imperative Shell", coder)
        self.assertIn("process-global registry", coder)
        self.assertIn("make illegal states or malformed data hard to represent", coder)
        self.assertIn("Prefer compile-time rejection", coder)
        self.assertIn("Preserve every accepted declaration's static constraints exactly", coder)
        self.assertIn("Avoid dynamic allocation, unnecessary copying", coder)
        self.assertIn("update_checklist as a micro-plan, not proof", coder)
        self.assertIn("submit immediately for independent verification", coder)

        self.assertIn("complete adjudication scope", verifier)
        self.assertIn("dependency public contract as an axiom", verifier)
        self.assertIn("Never inspect, audit, or infer", verifier)
        self.assertIn("Do not search for task.yaml", verifier)
        self.assertIn("reuse them across Candidate repairs", verifier)
        self.assertIn("material complexity or resource growth", verifier)
        self.assertIn("SystemVerificationWorkView", system_verifier)
        self.assertIn("Do not split scenarios into workers", system_verifier)
        self.assertIn("real system and delivery tests", system_verifier)
        self.assertIn("PTY/tmux/expect-style harness", system_verifier)
        self.assertIn("first public boundary", system_verifier)
        self.assertIn("successful real-boundary evidence", system_verifier)
        self.assertIn("do not invent facts", generic)
        self.assertIn("never acceptance evidence", verifier)
        self.assertIn("minimum sufficient focused build/test path", verifier)
        self.assertIn("disposition=advisory with priority=p2", verifier)
        self.assertIn("Do not block acceptance for stylistic type-level abstraction", verifier)
        self.assertIn("positive and negative consumer compile probe", verifier)
        self.assertIn("does not prove overload exclusion", verifier)
        self.assertIn("positive and negative external-consumer compile probes", system_verifier)
        self.assertNotIn("owned_impl", coder)
        self.assertNotIn("owned_test", coder)

        # Prompt budgets prevent mechanical policy from creeping back into every
        # role while leaving the semantic philosophy readable in one place.
        self.assertLess(len(architect), 7_000)
        self.assertLess(len(architecture_review), 7_000)
        self.assertLess(len(coder), 5_500)
        for behavior in (architect, architecture_review, coder):
            self.assertNotIn("Do not run git commit", behavior)
            self.assertNotIn("tests/<module_name>/developer", behavior)
            self.assertNotIn("tests/<module_name>/verifier", behavior)

    def test_every_architect_profile_gates_design_on_source_consistency(self) -> None:
        for profile in (
            "general.architect",
            "lifestyle.architect",
            "software_engineering.v2_architect",
        ):
            with self.subTest(profile=profile):
                behavior = str(self._pack(profile).resolved_profile["behavior_fragment"])
                self.assertIn("bounded consistency pass", behavior)
                self.assertIn("mechanically verify", behavior.lower())
                self.assertIn("call ask_question and wait", behavior)

    def test_profile_tool_guidance_override_is_applied_to_scoped_surface(self) -> None:
        researcher = self._pack("software_engineering.v2_architect")
        override = dict(
            researcher.resolved_profile["capability_guidance_overrides"]["op_web_search"]
        )
        base = ExecutionRuntime()
        base.register_tool(
            Tool(
                alias="web_search",
                canonical_path="op_web_search",
                family="web",
                source="test",
                InputModel=EmptyToolInput,
                OutputModel=OpaqueToolOutput,
                guidance=ToolGuidance(
                    purpose="generic web description",
                    use_when="generic web description",
                    do_not_use_when="Do not use for local-only research.",
                    failure_next_steps="Correct input or inspect the returned failure.",
                ),
                execution=DIRECT_EXTERNAL_READ,
                search_text="generic web search research",
                handler=lambda _value: CapabilityResult(status=RuntimeStatus.OK, text="ok", llm_text="ok"),
            )
        )
        scoped = MinionScopedExecutionRuntime(
            base,
            ["op_web_search"],
            capability_guidance_overrides=dict(
                researcher.resolved_profile["capability_guidance_overrides"]
            ),
        )

        spec = {
            item["function"]["name"]: item["function"]
            for item in scoped.build_llm_tool_contracts()
        }["web_search"]

        self.assertIn("Purpose: generic web description", spec["description"])
        self.assertIn(f"Use when: {override['use_when']}", spec["description"])
        self.assertIn(f"Do not use when: {override['do_not_use_when']}", spec["description"])
        self.assertIn(f"Failure next steps: {override['failure_next_steps']}", spec["description"])

    def test_role_workspace_provisioning_never_injects_capabilities(self) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "reference.txt").write_text("truth", encoding="utf-8")
        planner = apply_v2_role_capability_policy(
            self._pack("lifestyle.architect"),
            activation=RoleActivation(OrchestrationRole.ARCHITECT, RoleMode.AUTHOR),
        )
        planner = MinionInvocationPack.from_dict(
            {**planner.to_dict(), "workspace": {**dict(planner.workspace), "repo_path": str(source)}}
        )
        prepared = prepare_v2_role_workspace(self.root, planner, run_id="planner-run")
        self.assertEqual(prepared.allowed_capabilities, planner.allowed_capabilities)
        self.assertFalse(any("checkpoint" in item for item in prepared.allowed_capabilities))
        self.assertTrue((Path(prepared.workspace["repo_path"]) / "reference.txt").is_file())

    def test_workspace_preparation_detects_languages_without_modifying_source(self) -> None:
        source = self.root / "prepared-source"
        source.mkdir()
        (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
        before = (source / "main.py").read_bytes()

        workspace, report = prepare_v2_workspace_environment({"repo_path": str(source)})

        self.assertIn("python", workspace["languages"])
        self.assertEqual(workspace["primary_language"], "python")
        self.assertFalse(report["source_modified"])
        self.assertEqual((source / "main.py").read_bytes(), before)
        self.assertTrue(report["environment_fingerprint"])

    def test_workspace_preparation_preserves_declared_primary_language_for_lsp_manager(self) -> None:
        source = self.root / "mixed-language-source"
        source.mkdir()
        (source / "main.py").write_text("print('ok')\n", encoding="utf-8")
        (source / "native.cpp").write_text("int native_value = 1;\n", encoding="utf-8")

        workspace, report = prepare_v2_workspace_environment(
            {
                "repo_path": str(source),
                "primary_language": "python",
            },
            runtime_root=self.root,
        )

        self.assertEqual(workspace["primary_language"], "python")
        self.assertEqual(workspace["languages"][0], "python")
        self.assertNotIn("lsp_setup", workspace)
        self.assertNotIn("lsp_setup", report)

    def test_cpp_workspace_preparation_leaves_lsp_context_to_lsp_manager(self) -> None:
        source = self.root / "cpp-source"
        source.mkdir()
        (source / "main.cpp").write_text('#include "include/value.h"\nint main() { return value(); }\n', encoding="utf-8")
        include = source / "include"
        include.mkdir()
        (include / "value.h").write_text("inline int value() { return 0; }\n", encoding="utf-8")
        before = sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())

        workspace, report = prepare_v2_workspace_environment(
            {
                "repo_path": str(source),
                "primary_language": "cpp",
                "cpp_standard": "c++14",
            },
            runtime_root=self.root,
        )

        after = sorted(path.relative_to(source).as_posix() for path in source.rglob("*") if path.is_file())
        self.assertEqual(workspace["primary_language"], "cpp")
        self.assertEqual(workspace["cpp_standard"], "c++14")
        self.assertNotIn("lsp_setup", workspace)
        self.assertEqual(before, after)
        self.assertFalse(report["source_modified"])

    def test_lifestyle_task_compiles_artifact_workspace_epoch(self) -> None:
        service = MinionV2WorkflowService(self.root)
        service.create_task(
            {
                "task_id": "nutrition-task",
                "title": "Weekly nutrition check-in",
                "objective": "Produce a non-medical structured check-in",
                "profile": "lifestyle.nutrition_checkin_producer",
                "workspace": {"kind": "artifact_project", "project_name": "nutrition"},
            }
        )
        prepared = service.prepare_requirements(
            {
                "title": "Weekly nutrition check-in",
                "task_spec": {
                    "objective": "Produce a non-medical structured check-in."
                },
            }
        )
        service.start_workflow(
            {
                "workflow_id": "nutrition-workflow",
                "task_id": "nutrition-task",
                "operation": "new_requirement",
                "goal": "Summarize declared nutrition observations without inventing facts",
                "requirements_ref": prepared["requirements_ref"],
            }
        )
        architecture = ArchitectureArtifactService(self.store, self.repository)
        unit = architecture.publish_unit_contract(
            {
                "unit_id": "checkin",
                "unit_behavior_kind": "stateless",
                "responsibility": "Produce the structured check-in artifact.",
                "owned_area": ["artifact:checkin"],
                "reference_only_paths": [],
                "provided_interfaces": [{"name": "structured_checkin"}],
                "consumed_interfaces": [],
                "ownership": {"rule": "checkin exclusively owns the emitted artifact."},
                "lifecycle": "n/a",
                "state_model": "stateless",
                "invariants": ["No undeclared health facts are introduced."],
                "error_behavior": ["Invalid source observations fail deterministically."],
                "compatibility": ["The declared check-in schema remains stable."],
                "dependency_constraints": [],
                "verification_obligations": ["Validate JSON and source coverage."],
                "complexity_budget": {
                    "target_file_count": 1,
                    "estimated_context_tokens": 1000,
                    "public_interface_count": 1,
                    "cross_unit_contract_count": 0,
                    "stateful_resource_count": 0,
                    "expected_candidate_cycles": 1,
                    "platform_dependency_level": 0,
                },
                "split_conditions": [],
            }
        )
        fragment = lambda value, kind: architecture.publish_fragment(value, artifact_type=kind)
        manifest = architecture.publish_manifest(
            {
                "requirements_ref": dict(prepared["requirements_ref"]),
                "global_constraints_ref": fragment([], "GlobalConstraintsArtifact").to_dict(),
                "gate_checks_ref": fragment([], "ArchitectureGateChecksArtifact").to_dict(),
                "unit_contract_refs": [unit.to_dict()],
                "cross_unit_contract_refs": [],
                "topology_ref": fragment({"depends_on": {"checkin": []}}, "TopologyArtifact").to_dict(),
                "integration_contract_ref": fragment({"depends_on": ["checkin"]}, "IntegrationContractArtifact").to_dict(),
                "assumption_ledger_ref": fragment({"assumptions": []}, "AssumptionLedgerArtifact").to_dict(),
                "risk_ledger_ref": fragment({"risks": []}, "RiskLedgerArtifact").to_dict(),
            }
        )
        compilation = ExecutionCompiler(self.repository, architecture).compile_epoch(
            workflow_id="nutrition-workflow",
            epoch_id="nutrition-epoch",
            manifest_ref=manifest,
        )
        node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, compilation.unit_node_ids["checkin"])
        self.assertEqual(node.payload["execution_adapter"], "artifact_bundle.v2")
        self.assertEqual(node.payload["node_kind"], "unit")
        self.assertTrue(Path(node.payload["workspace_path"]).is_dir())


if __name__ == "__main__":
    unittest.main()
