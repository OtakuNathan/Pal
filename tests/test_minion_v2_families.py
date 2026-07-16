from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pal.minion.catalog import MinionCatalogService
from pal.minion.profiles import MinionProfileRegistry, resolve_pinned_minion_pack
from pal.minion.scoped_execution import MinionScopedExecutionRuntime
from pal.minion.workspace_tools import _normalized_reference_paths
from pal.execution.contracts import CapabilityDescriptor, CapabilityResult
from pal.execution.runtime import ExecutionRuntime
from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.adapters import prepare_v2_role_workspace, prepare_v2_workspace_environment
from pal.minion.v2.architecture import ArchitectureArtifactService, ResearchMode
from pal.minion.v2.catalog import MinionV2Catalog
from pal.minion.v2.contracts import AggregateType
from pal.minion.v2.execution import ExecutionCompiler
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.workers import (
    apply_v2_bound_input_capability_policy,
    apply_v2_research_capability_policy,
    apply_v2_role_capability_policy,
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
        ref = MinionV2Catalog(self.root, self.store).publish_family_binding("lifestyle")
        binding = self.store.read_json(ref)
        self.assertEqual(binding["workflow_template"], "contract_dag.v2")
        self.assertTrue({"architect", "architecture_reviewer", "producer", "repair", "verifier"} <= set(binding["roles"]))
        self.assertEqual(set(binding["adapters"].values()), {"artifact_bundle.v2"})
        self.assertEqual(set(binding["profile_hashes"]), set(binding["roles"]))
        self.assertEqual(set(binding["profile_definitions"]), set(binding["roles"]))
        self.assertEqual(binding["policies"]["llm"]["temperature"], 0.05)
        self.assertEqual(binding["policies"]["llm"]["llm_round_timeout_seconds"], 900)

    def test_general_family_is_a_complete_data_driven_contract_dag(self) -> None:
        ref = MinionV2Catalog(self.root, self.store).publish_family_binding("general")
        binding = self.store.read_json(ref)
        self.assertEqual(binding["workflow_template"], "contract_dag.v2")
        self.assertTrue({"architect", "architecture_reviewer", "producer", "repair", "verifier"} <= set(binding["roles"]))
        self.assertEqual(set(binding["adapters"].values()), {"artifact_bundle.v2"})
        self.assertEqual(set(binding["profile_hashes"]), set(binding["roles"]))
        self.assertEqual(set(binding["profile_definitions"]), set(binding["roles"]))

    def test_family_binding_pins_profile_definition_across_catalog_refresh(self) -> None:
        ref = MinionV2Catalog(self.root, self.store).publish_family_binding("software_engineering")
        binding = self.store.read_json(ref)
        pinned = dict(binding["profile_definitions"]["producer"])
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
            family_payload=dict(binding["manifest"]),
        )

        self.assertEqual(current.display_name, "Future Workflow Coder")
        self.assertEqual(resolved.resolved_profile["display_name"], original_name)

    def test_architect_cannot_bypass_builder_but_producer_can_write_workspace(self) -> None:
        planner = apply_v2_role_capability_policy(
            self._pack("lifestyle.architect"),
            role="architect",
        )
        self.assertIn("op_minion_contract_submit", planner.allowed_capabilities)
        self.assertNotIn("op_file_write", planner.allowed_capabilities)
        self.assertNotIn("op_minion_artifact_write", planner.allowed_capabilities)

        producer = apply_v2_role_capability_policy(
            self._pack("lifestyle.nutrition_checkin_producer"),
            role="producer",
        )
        self.assertIn("op_file_write", producer.allowed_capabilities)
        self.assertIn("op_minion_artifact_write", producer.allowed_capabilities)
        self.assertIn("op_minion_candidate_submit", producer.allowed_capabilities)
        self.assertIn("op_minion_developer_test", producer.allowed_capabilities)
        self.assertNotIn("op_web_search", producer.allowed_capabilities)

    def test_artifact_producer_cannot_fabricate_manager_submission_report(self) -> None:
        repo = self.root / "guard-repo"
        repo.mkdir()
        scoped = MinionScopedExecutionRuntime(
            ExecutionRuntime(),
            ["op_minion_artifact_write", "op_file_write"],
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
                    name="op_minion_artifact_write",
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
                    name="op_minion_artifact_write",
                    args={
                        "relative_path": "checkin.json",
                        "content": '{"status":"recorded"}',
                        "role": "deliverable",
                    },
                )
            )
        )
        self.assertTrue(product.ok, product.text)

        workspace_report = asyncio.run(
            scoped.execute_tool_async(
                CanonicalToolCall(
                    name="op_file_write",
                    args={
                        "path": "producer_report.json",
                        "content": "{}",
                        "mode": "create",
                    },
                )
            )
        )
        self.assertFalse(workspace_report.ok)
        self.assertIn("Manager-owned", workspace_report.llm_text)

    def test_verifier_can_only_submit_through_the_schema_bound_builder(self) -> None:
        for profile in (
            "software_engineering.v2_verifier",
            "general.verifier",
            "lifestyle.verifier",
        ):
            with self.subTest(profile=profile):
                verifier = apply_v2_role_capability_policy(self._pack(profile), role="verifier")
                self.assertIn("op_minion_verification_submit", verifier.allowed_capabilities)
                self.assertIn("op_exec_shell", verifier.allowed_capabilities)
                self.assertNotIn("op_minion_artifact_write", verifier.allowed_capabilities)
                self.assertNotIn("op_minion_artifact_edit", verifier.allowed_capabilities)

        standalone = apply_v2_role_capability_policy(
            self._pack("software_engineering.v2_reviewer"),
            role="reviewer",
        )
        self.assertIn("op_minion_standalone_review_submit", standalone.allowed_capabilities)
        self.assertIn("op_exec_shell", standalone.allowed_capabilities)
        self.assertNotIn("op_minion_artifact_write", standalone.allowed_capabilities)
        self.assertNotIn("op_minion_verification_submit", standalone.allowed_capabilities)

        coder = apply_v2_role_capability_policy(
            self._pack("software_engineering.v2_coder"),
            role="producer",
        )
        self.assertIn("op_minion_candidate_submit", coder.allowed_capabilities)
        self.assertNotIn("op_minion_artifact_write", coder.allowed_capabilities)

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
            scoped.resolve_llm_tool_name("verification_submit"),
            "op_minion_verification_submit",
        )
        self.assertEqual(
            scoped.get_capability_spec("verification_submit")["name"],
            "verification_submit",
        )
        self.assertFalse(spec["parameters_schema"]["additionalProperties"])
        self.assertEqual(spec["parameters_schema"]["properties"], {})

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
        self.assertIn("takes no arguments", result.llm_text)
        self.assertFalse((self.root / "artifact-stage" / "verification_plan.json").exists())

    def test_worker_only_canonical_names_are_exposed_through_role_native_aliases(self) -> None:
        scoped = MinionScopedExecutionRuntime(
            ExecutionRuntime(),
            [
                "op_minion_input_read",
                "op_minion_repair_checklist",
                "op_minion_contract_submit",
                "op_minion_candidate_submit",
            ],
            workspace={
                "artifact_dir": str(self.root / "alias-artifacts"),
                "artifact_stage_dir": str(self.root / "alias-stage"),
            },
        )

        expected = {
            "op_minion_input_read": "input_read",
            "op_minion_repair_checklist": "repair_checklist",
            "op_minion_contract_submit": "contract_submit",
            "op_minion_candidate_submit": "candidate_submit",
        }
        for canonical, public_name in expected.items():
            with self.subTest(canonical=canonical):
                spec = scoped.get_capability_spec(canonical)
                self.assertIsNotNone(spec)
                assert spec is not None
                self.assertEqual(spec["name"], public_name)
                self.assertEqual(scoped.resolve_llm_tool_name(public_name), canonical)
                self.assertNotIn("minion_", spec["description"])

    def test_manager_injects_bound_input_protocol_independently_from_profile(self) -> None:
        coder = self._pack("software_engineering.v2_coder")
        self.assertNotIn("op_minion_input_read", coder.allowed_capabilities)
        injected = apply_v2_bound_input_capability_policy(
            coder,
            mandatory_inputs=["repair_bill", "unit_work_view"],
            repair_checklist={
                "module_name": "font_backend",
                "findings": [
                    {
                        "case": "released_font_rejected",
                        "summary": "Released fonts remain usable.",
                        "locations": [{"path": "src/font.cpp", "symbol": "measure"}],
                    }
                ],
            },
        )

        self.assertIn("op_minion_input_read", injected.allowed_capabilities)
        self.assertIn("op_minion_repair_checklist", injected.allowed_capabilities)
        overrides = dict(injected.resolved_profile["capability_description_overrides"])
        self.assertIn("repair_bill, unit_work_view", overrides["op_minion_input_read"])
        self.assertIn("released_font_rejected", overrides["op_minion_repair_checklist"])

    def test_manager_does_not_inject_input_tools_without_bound_inputs(self) -> None:
        coder = self._pack("software_engineering.v2_coder")
        unchanged = apply_v2_bound_input_capability_policy(
            coder,
            mandatory_inputs=[],
        )
        self.assertEqual(unchanged.allowed_capabilities, coder.allowed_capabilities)

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
        self.assertIn("op_file_write", software.allowed_capabilities)
        self.assertIn("op_file_edit", software.allowed_capabilities)
        self.assertEqual(software.workspace.get("workspace_policy", {}).get("mode"), "writable_git_branch")

    def test_bound_input_reader_exposes_only_the_named_immutable_file(self) -> None:
        bound = self.root / "workflow-request.json"
        sibling = self.root / "secret.txt"
        bound.write_text('{"goal":"bounded"}\n', encoding="utf-8")
        sibling.write_text("not-bound\n", encoding="utf-8")

        class FakeRuntime:
            async def execute_tool_async(self, call, **_kwargs):
                path = Path(str(call.args["file_path"]))
                return CanonicalToolResult(
                    name=call.name,
                    ok=True,
                    text=path.read_text(encoding="utf-8"),
                    llm_text=path.read_text(encoding="utf-8"),
                    status=RuntimeStatus.OK,
                )

        workspace = {"reference_paths": [{"name": "workflow_request", "path": str(bound), "truth_source": True}]}
        scoped = MinionScopedExecutionRuntime(FakeRuntime(), ["op_minion_input_read"], workspace=workspace)

        result = asyncio.run(
            scoped.execute_tool_async(
                CanonicalToolCall(name="op_minion_input_read", args={"name": "workflow_request"})
            )
        )
        missing = asyncio.run(
            scoped.execute_tool_async(
                CanonicalToolCall(name="op_minion_input_read", args={"name": "secret"})
            )
        )
        generic_reader = MinionScopedExecutionRuntime(FakeRuntime(), ["op_file_read"], workspace=workspace)
        sibling_attempt = asyncio.run(
            generic_reader.execute_tool_async(
                CanonicalToolCall(
                    name="op_file_read",
                    args={"reference_name": "workflow_request", "path": sibling.name},
                )
            )
        )

        self.assertTrue(result.ok, result.text)
        self.assertIn('"bounded"', result.llm_text)
        self.assertFalse(missing.ok)
        self.assertNotIn("not-bound", missing.llm_text)
        self.assertFalse(sibling_attempt.ok)
        self.assertIn("outside the declared immutable input", sibling_attempt.llm_text)

    def test_reference_normalization_preserves_manager_bound_input_routing(self) -> None:
        bound = self.root / "bound-input.json"
        bound.write_text('{"value":true}\n', encoding="utf-8")

        references = _normalized_reference_paths(
            {
                "reference_paths": [
                    {
                        "name": "module_work_view",
                        "path": str(bound),
                        "bound_input": True,
                        "required": True,
                    }
                ]
            }
        )

        self.assertTrue(references[0]["bound_input"])
        self.assertTrue(references[0]["required"])

        class FakeGateway:
            def request_sync(self, operation, payload):
                self.operation = operation
                self.payload = dict(payload)
                return {
                    "content": '1 {"value":true}',
                    "start_line": 1,
                    "returned_lines": 1,
                    "total_lines": 1,
                    "has_more": False,
                }

        gateway = FakeGateway()
        workspace = {"runtime_root": str(self.root), "reference_paths": references}
        scoped = MinionScopedExecutionRuntime(object(), ["op_minion_input_read"], workspace=workspace)
        with patch("pal.minion.v2.worker_gateway.worker_gateway_client_from_env", return_value=gateway):
            result = asyncio.run(
                scoped.execute_tool_async(
                    CanonicalToolCall(name="op_minion_input_read", args={"name": "module_work_view"})
                )
            )

        self.assertTrue(result.ok, result.text)
        self.assertEqual(gateway.operation, "bound_input_read")
        self.assertEqual(gateway.payload["name"], "module_work_view")
        self.assertIn('"value":true', result.llm_text)

    def test_software_profiles_preserve_engineering_rules_and_lsp_surface(self) -> None:
        coder = self._pack("software_engineering.v2_coder")
        behavior = str(coder.resolved_profile["behavior_fragment"])
        self.assertIn("test-only hacks", behavior)
        self.assertIn("nonexistent API", behavior)
        self.assertIn("independent adversarial verification", behavior)
        self.assertIn("not a completion or acceptance claim", behavior)
        self.assertIn("test_debugging", coder.allowed_skills)
        self.assertIn("op_lsp_definition", coder.allowed_capabilities)
        self.assertIn("op_lsp_references", coder.allowed_capabilities)
        self.assertIn("op_lsp_diagnostics", coder.allowed_capabilities)
        overrides = dict(coder.resolved_profile["capability_description_overrides"])
        self.assertIn("After reading relevant source", overrides["op_lsp_definition"])
        self.assertIn("do not prove runtime behavior", overrides["op_lsp_diagnostics"])
        self.assertIn("manager creates the candidate", overrides["op_git"])

        binding_ref = MinionV2Catalog(self.root, self.store).publish_family_binding("software_engineering")
        binding = self.store.read_json(binding_ref)
        self.assertEqual(binding["roles"]["reviewer"], "software_engineering.v2_reviewer")
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

    def test_product_requirements_do_not_absorb_family_workflow_policy(self) -> None:
        service = MinionV2WorkflowService(self.root)
        prepared = service.prepare_requirements(
            {
                "title": "Tiny router",
                "sections": {
                    "Routing": ["Route requests deterministically."],
                },
                # Workflow policy belongs to FamilyBindingArtifact even if a
                # caller accidentally includes it in this request envelope.
                "policies": {"verification": {"require_warning_clean": True}},
            }
        )
        requirements = self.store.read_json(prepared["requirements_ref"])
        binding = self.store.read_json(
            MinionV2Catalog(self.root, self.store).publish_family_binding(
                "software_engineering"
            )
        )

        self.assertEqual(
            requirements["sections"],
            {"Routing": ["Route requests deterministically."]},
        )
        self.assertNotIn("policies", requirements)
        self.assertTrue(binding["policies"]["verification"]["require_warning_clean"])

    def test_software_architecture_and_verification_profiles_preserve_rigorous_methods(self) -> None:
        architect = str(self._pack("software_engineering.v2_architect").resolved_profile["behavior_fragment"])
        architecture_review = str(
            self._pack("software_engineering.v2_architecture_reviewer").resolved_profile["behavior_fragment"]
        )
        verifier = str(self._pack("software_engineering.v2_verifier").resolved_profile["behavior_fragment"])
        generic = str(self._pack("general.generic").resolved_profile["behavior_fragment"])

        self.assertIn("RequirementsArtifact is immutable and authoritative", architect)
        self.assertIn("feasibility", architect)
        self.assertIn("foundation, language/runtime bridge", architect)
        self.assertIn("one candidate-review cycle", architect)
        self.assertIn("Never reduce the core goal to a stub", architect)
        self.assertIn("Maintain three distinct semantic graphs", architect)
        self.assertIn("Do not invent a universal integration/join node", architect)
        self.assertIn("Never include opaque IDs, handles, SHA values, milestones", architect)
        self.assertIn("Audit breadth-first in one pass", architecture_review)
        self.assertIn("Verification Topology", architecture_review)
        self.assertIn("covers entry is only the Architect's claim", architecture_review)
        self.assertIn("one audit for every bound hard Requirement", str(
            self._pack("software_engineering.v2_architecture_reviewer").resolved_profile["output_contract_fragment"]
        ))
        self.assertIn("unique data/worker/object/resource ownership", architecture_review)
        self.assertIn("one candidate-review cycle", architecture_review)
        self.assertIn(
            "Confirm independent Coders can implement each implementation module",
            architecture_review,
        )
        self.assertIn("verify a local repair without reopening unchanged architecture", architecture_review)
        self.assertIn("happens-before", verifier)
        self.assertIn("exact public delivery surface", verifier)
        self.assertIn("VerificationPolicy", verifier)
        self.assertIn("RequirementPatch", verifier)
        self.assertIn("exact Manager-assembled Candidate combination", verifier)
        self.assertIn("must not dirty the immutable candidate", verifier)
        self.assertIn("do not invent facts", generic)
        self.assertIn("Perform the detailed local", str(self._pack("software_engineering.v2_coder").resolved_profile["behavior_fragment"]))
        coder = str(self._pack("software_engineering.v2_coder").resolved_profile["behavior_fragment"])
        self.assertIn("implementation_scopes and test_scopes", coder)
        self.assertNotIn("owned_impl", coder)
        self.assertNotIn("owned_test", coder)

    def test_profile_tool_description_override_is_applied_to_scoped_surface(self) -> None:
        researcher = self._pack("software_engineering.v2_architect")
        override = str(researcher.resolved_profile["capability_description_overrides"]["op_web_search"])
        base = ExecutionRuntime()
        base.register_capability(
            CapabilityDescriptor(
                name="web_search",
                canonical_path="op_web_search",
                family="web",
                source="test",
                description="generic web description",
            ),
            lambda _call: CapabilityResult(status=RuntimeStatus.OK, text="ok"),
        )
        scoped = MinionScopedExecutionRuntime(
            base,
            ["op_web_search"],
            capability_description_overrides=dict(
                researcher.resolved_profile["capability_description_overrides"]
            ),
        )

        spec = scoped.get_capability_spec("op_web_search")

        self.assertIsNotNone(spec)
        assert spec is not None
        self.assertEqual(spec["description"], override)
        self.assertIn("may require user approval", spec["description"])
        self.assertIn("local project source", spec["description"])

    def test_role_workspace_provisioning_never_injects_capabilities(self) -> None:
        source = self.root / "source"
        source.mkdir()
        (source / "reference.txt").write_text("truth", encoding="utf-8")
        planner = apply_v2_role_capability_policy(self._pack("lifestyle.architect"), role="architect")
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

    def test_lifestyle_task_compiles_artifact_workspace_epoch(self) -> None:
        service = MinionV2WorkflowService(self.root)
        service.create_task(
            {
                "task_id": "nutrition-task",
                "title": "Weekly nutrition check-in",
                "objective": "Produce a non-medical structured check-in",
                "family_id": "lifestyle",
                "workspace": {"kind": "artifact_project", "project_name": "nutrition"},
            }
        )
        prepared = service.prepare_requirements(
            {"requirements": [{"requirement_id": "R-1", "statement": "Produce a check-in", "strength": "hard"}]}
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
        requirements = architecture.publish_requirements(
            {"requirements": [{"requirement_id": "R-1", "statement": "Produce a check-in", "strength": "hard"}]}
        )
        evidence = architecture.publish_evidence_catalog(
            {
                "evidence": [
                    {
                        "evidence_id": "E-1",
                        "source_kind": "user_supplied",
                        "location": "workflow input",
                        "summary": "User requested a check-in",
                        "supports_requirement_ids": ["R-1"],
                    }
                ]
            },
            requirements_ref=requirements,
            research_mode=ResearchMode.LOCAL_ONLY,
        )
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
                "requirement_ids": ["R-1"],
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
                "requirements_ref": requirements.to_dict(),
                "global_constraints_ref": fragment([], "GlobalConstraintsArtifact").to_dict(),
                "design_decisions_ref": fragment([], "DesignDecisionsArtifact").to_dict(),
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
