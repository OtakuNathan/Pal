from __future__ import annotations

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.minion.profiles import MinionProfileRegistry
from pal.minion.scoped_execution import MinionScopedExecutionRuntime
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
from pal.minion.v2.workers import apply_v2_research_capability_policy, apply_v2_role_capability_policy
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
        self.assertTrue({"requirements", "research", "planner", "architecture_reviewer", "producer", "repair", "verifier"} <= set(binding["roles"]))
        self.assertEqual(set(binding["adapters"].values()), {"artifact_bundle.v2"})
        self.assertEqual(set(binding["profile_hashes"]), set(binding["roles"]))
        self.assertEqual(binding["policies"]["llm"]["temperature"], 0.05)

    def test_general_family_is_a_complete_data_driven_contract_dag(self) -> None:
        ref = MinionV2Catalog(self.root, self.store).publish_family_binding("general")
        binding = self.store.read_json(ref)
        self.assertEqual(binding["workflow_template"], "contract_dag.v2")
        self.assertTrue({"requirements", "research", "planner", "architecture_reviewer", "producer", "repair", "verifier"} <= set(binding["roles"]))
        self.assertEqual(set(binding["adapters"].values()), {"artifact_bundle.v2"})
        self.assertEqual(set(binding["profile_hashes"]), set(binding["roles"]))

    def test_planner_cannot_bypass_builder_but_producer_can_write_workspace(self) -> None:
        planner = apply_v2_role_capability_policy(
            self._pack("lifestyle.contract_planner"),
            role="planner",
        )
        self.assertIn("op_minion_contract_submit_sketch", planner.allowed_capabilities)
        self.assertNotIn("op_file_write", planner.allowed_capabilities)
        self.assertNotIn("op_minion_artifact_write", planner.allowed_capabilities)

        producer = apply_v2_role_capability_policy(
            self._pack("lifestyle.nutrition_checkin_producer"),
            role="producer",
        )
        self.assertIn("op_file_write", producer.allowed_capabilities)
        self.assertIn("op_minion_artifact_write", producer.allowed_capabilities)
        self.assertNotIn("op_web_search", producer.allowed_capabilities)

    def test_local_only_research_removes_external_search(self) -> None:
        researcher = self._pack("lifestyle.researcher")
        self.assertIn("op_web_search", researcher.allowed_capabilities)
        local = apply_v2_research_capability_policy(researcher, research_mode="local_only")
        self.assertNotIn("op_web_search", local.allowed_capabilities)
        self.assertNotIn("op_web_read", local.allowed_capabilities)

    def test_requirements_roles_only_receive_the_controlled_builder(self) -> None:
        for profile in (
            "general.requirements_analyst",
            "lifestyle.requirements_analyst",
            "software_engineering.v2_requirements_analyst",
        ):
            with self.subTest(profile=profile):
                requirements = self._pack(profile)
                self.assertIn("op_minion_requirements_replace_batch", requirements.allowed_capabilities)
                self.assertIn("op_minion_requirements_submit", requirements.allowed_capabilities)
                self.assertIn("op_minion_input_read", requirements.allowed_capabilities)
                self.assertNotIn("op_tree", requirements.allowed_capabilities)
                self.assertNotIn("op_search", requirements.allowed_capabilities)
                self.assertNotIn("op_file_read", requirements.allowed_capabilities)
                self.assertNotIn("op_git", requirements.allowed_capabilities)
                self.assertEqual(requirements.workspace.get("workspace_policy", {}).get("mode"), "artifact_only")

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

    def test_software_architecture_and_verification_profiles_preserve_rigorous_methods(self) -> None:
        requirements = str(self._pack("software_engineering.v2_requirements_analyst").resolved_profile["behavior_fragment"])
        research = str(self._pack("software_engineering.v2_researcher").resolved_profile["behavior_fragment"])
        planner = str(self._pack("software_engineering.v2_contract_planner").resolved_profile["behavior_fragment"])
        architecture_review = str(
            self._pack("software_engineering.v2_architecture_reviewer").resolved_profile["behavior_fragment"]
        )
        verifier = str(self._pack("software_engineering.v2_verifier").resolved_profile["behavior_fragment"])
        generic = str(self._pack("general.generic").resolved_profile["behavior_fragment"])

        self.assertIn("every member of an enumerated", requirements)
        self.assertIn("strict allowlist", requirements)
        self.assertIn("read-only truth-source", research)
        self.assertIn("ownership transfer or borrowing", planner)
        self.assertIn("Assign each concrete file", planner)
        self.assertIn("one candidate-review cycle", planner)
        self.assertIn("claim-driven trace", architecture_review)
        self.assertIn("unrelated fragment drift", architecture_review)
        self.assertIn("structured complexity budget", architecture_review)
        self.assertIn("happens-before", verifier)
        self.assertIn("exact public delivery surface", verifier)
        self.assertIn("VerificationPolicy", verifier)
        self.assertIn("must not dirty the immutable candidate", verifier)
        self.assertIn("do not invent facts", generic)

    def test_profile_tool_description_override_is_applied_to_scoped_surface(self) -> None:
        researcher = self._pack("software_engineering.v2_researcher")
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
        planner = apply_v2_role_capability_policy(self._pack("lifestyle.contract_planner"), role="planner")
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
        service.start_workflow(
            {
                "workflow_id": "nutrition-workflow",
                "task_id": "nutrition-task",
                "operation": "new_requirement",
                "goal": "Summarize declared nutrition observations without inventing facts",
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
                "provided_interfaces": [],
                "consumed_interfaces": [],
                "ownership": {"owner": "checkin"},
                "lifecycle": "n/a",
                "state_model": "stateless",
                "invariants": ["No undeclared health facts are introduced."],
                "error_behavior": [],
                "compatibility": [],
                "dependency_constraints": [],
                "requirement_ids": ["R-1"],
                "evidence_ids": ["E-1"],
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
                "evidence_catalog_ref": evidence.to_dict(),
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
