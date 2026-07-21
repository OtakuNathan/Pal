from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from pal.llm.contracts import CanonicalToolCall
from pal.minion.scoped_execution import MinionScopedExecutionRuntime, _WORKSPACE_TOOL_SPECS
from pal.minion.v2.contract_builder import (
    CONTRACT_BUILDER_TOOL_SPECS,
    contract_builder_tool_result,
    seed_contract_builder_draft,
)
from pal.minion.v2.candidate_builder import CANDIDATE_BUILDER_TOOL_SPECS
from pal.minion.v2.skeleton_builder import SKELETON_BUILDER_TOOL_SPECS
from pal.minion.v2.verification_builder import VERIFICATION_BUILDER_TOOL_SPECS
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.review_findings import ADD_FINDING_CAPABILITY, add_finding_tool_result
from pal.minion.v2.submission_drafts import AUTHORING_CONTRACT_VERSION
from pal.execution.runtime import ExecutionRuntime


class ContractBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal-v2-contract-builder-"))
        self.repository = MinionV2Repository(self.root)
        self.repository.ensure_schema()
        self.invocation = "inv_contract"
        self.resource = "architecture:arch_1:contract"
        lease = self.repository.claim_lease(self.resource, self.invocation, ttl_seconds=60)
        task_source_path = self.root / "request.md"
        task_source_path.write_text(
            "Produce a deterministic report through a real entrypoint.\n",
            encoding="utf-8",
        )
        self.workspace = {
            "runtime_root": str(self.root),
            "artifact_dir": str(self.root / "artifacts"),
            "artifact_stage_dir": str(self.root / "artifact-stage"),
            "reference_paths": [
                {
                    "name": "task",
                    "path": str(task_source_path),
                    "truth_source": True,
                }
            ],
            "minion_v2": {
                "workflow_id": "wf_contract",
                "invocation_id": self.invocation,
                "lease_resource_key": self.resource,
                "fencing_token": lease.fencing_token,
                "role": "architect",
                "mode": "author",
                "authoring_input_fingerprint": "contract-input-v1",
                "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
            },
        }
        self.produced: list[dict[str, object]] = []
        self.call_index = 0

    def call(self, stage: str, name: str, args: dict | None = None):
        self.call_index += 1
        self.workspace["contract_builder_stage"] = stage
        call = CanonicalToolCall(name=name, args=args or {}, call_id=f"call-{self.call_index}")
        if name == ADD_FINDING_CAPABILITY:
            return add_finding_tool_result(call, self.workspace)
        return contract_builder_tool_result(
            call,
            self.workspace,
            self.produced,
        )

    def add_complete_unit(
        self,
        name: str,
        *,
        depends_on: list[str] | None = None,
    ) -> None:
        operations = [
            (
                "op_minion_contract_unit_upsert",
                {
                    "name": name,
                    "behavior_kind": "stateless",
                    "responsibility": f"Own the {name} report boundary.",
                    "owned_area": [f"{name}/"],
                    "reference_only_paths": [],
                    "depends_on": depends_on or [],
                },
            ),
            (
                "op_minion_contract_unit_add_interface",
                {
                    "unit": name,
                    "direction": "provided",
                    "name": f"{name}_report",
                    "data_shape": "immutable report value",
                    "valid_when": "input has been normalized",
                    "lifetime": "valid for the delivery call",
                    "ownership": f"{name} owns construction; consumer owns returned value",
                    "error_behavior": "invalid input returns a deterministic error",
                    "compatibility": "preserve the public report shape",
                },
            ),
            (
                "op_minion_contract_unit_set_ownership",
                {"unit": name, "statement": f"{name} exclusively owns its mutable state."},
            ),
            (
                "op_minion_contract_unit_set_lifecycle",
                {"unit": name, "description": "process/import lifetime; no runtime resources"},
            ),
            (
                "op_minion_contract_unit_set_state",
                {"unit": name, "description": "stateless"},
            ),
            (
                "op_minion_contract_unit_add_rule",
                {
                    "unit": name,
                    "kind": "invariant",
                    "statement": "Equal normalized inputs produce equal report values.",
                },
            ),
            (
                "op_minion_contract_unit_add_rule",
                {
                    "unit": name,
                    "kind": "error_behavior",
                    "statement": "Invalid input returns an explicit error value.",
                },
            ),
            (
                "op_minion_contract_unit_add_rule",
                {
                    "unit": name,
                    "kind": "compatibility",
                    "statement": "The public report shape remains stable.",
                },
            ),
        ]
        for capability, args in operations:
            result = self.call("contract", capability, args)
            self.assertTrue(result.ok, result.text)

    def test_normalized_requirements_builder_is_not_available(self) -> None:
        result = self.call(
            "requirements",
            "op_minion_requirement_upsert",
            {
                "section": "Delivery",
                "statement": "Produce a deterministic report through a real entrypoint.",
                "strength": "hard",
            },
        )
        self.assertFalse(result.ok)
        self.assertFalse(
            (Path(self.workspace["artifact_stage_dir"]) / "requirements.json").exists()
        )

    def test_contract_submit_compiles_manager_owned_identity(self) -> None:
        self.add_complete_unit("report")
        integration = self.call(
            "contract",
            "op_minion_contract_set_integration",
            {
                "depends_on": ["report"],
                "entrypoint": "report_cli",
                "dataflow": ["input -> report -> output"],
                "completion_condition": "the report is emitted",
                "failure_behavior": "return a deterministic non-zero result",
            },
        )
        self.assertTrue(integration.ok, integration.text)
        submitted = self.call("contract", "op_minion_contract_submit")
        self.assertTrue(submitted.ok, submitted.text)

        artifact = json.loads(
            (Path(self.workspace["artifact_stage_dir"]) / "architecture_bundle.json").read_text()
        )
        self.assertEqual(artifact["units"][0]["unit_id"], "report")
        self.assertNotIn("requirement_ids", artifact["units"][0])
        self.assertEqual(artifact["topology"]["depends_on"], {"report": []})

    def test_submit_rejects_cycle_after_local_semantic_edits(self) -> None:
        self.add_complete_unit("source", depends_on=["sink"])
        self.add_complete_unit("sink", depends_on=["source"])
        self.assertTrue(
            self.call(
                "contract",
                "op_minion_contract_set_integration",
                {
                    "depends_on": ["source", "sink"],
                    "entrypoint": "report_cli",
                    "completion_condition": "the report is emitted",
                    "failure_behavior": "return a deterministic error",
                },
            ).ok
        )
        rejected = self.call("contract", "op_minion_contract_submit")
        self.assertFalse(rejected.ok)
        self.assertIn("dependency cycle", rejected.text)

    def test_review_finding_is_structured_and_manager_infers_fail(self) -> None:
        base = self._complete_base_contract()
        self.workspace.update(
            {
                "contract_review_base_payload": base,
            }
        )
        self.workspace["minion_v2"].update(
            {"role": "reviewer", "mode": "architecture"}
        )
        finding = self.call(
            "architecture_review",
            ADD_FINDING_CAPABILITY,
            {
                "finding_key": "report_ownership_incomplete",
                "finding_kind": "contract_defect",
                "priority": "p1",
                "summary": "The ownership rule does not cover the returned report value.",
            },
        )
        self.assertTrue(finding.ok, finding.text)
        submitted = self.call("architecture_review", "op_minion_architecture_review_submit")
        self.assertTrue(submitted.ok, submitted.text)
        artifact = json.loads(
            (Path(self.workspace["artifact_stage_dir"]) / "architecture_review.json").read_text()
        )
        self.assertEqual(artifact["verdict"], "FAIL")
        self.assertEqual(artifact["findings"][0]["finding_key"], "report_ownership_incomplete")
        self.assertEqual(artifact["findings"][0]["priority"], "p1")

    def test_revision_scope_rejects_unrelated_semantic_change(self) -> None:
        base = self._complete_base_contract()
        self._advance_fence()
        seed_contract_builder_draft(
            self.workspace,
            base,
            revision_scope={
                "write_targets": [
                    {"section": "unit", "id": "report", "fields": ["ownership"], "operation": "update"}
                ]
            },
        )
        allowed = self.call(
            "contract",
            "op_minion_contract_unit_set_ownership",
            {"unit": "report", "statement": "report owns construction and transfers the immutable value."},
        )
        self.assertTrue(allowed.ok, allowed.text)
        rejected = self.call(
            "contract",
            "op_minion_contract_unit_add_rule",
            {
                "unit": "report",
                "kind": "compatibility",
                "statement": "A new unrelated compatibility rule.",
            },
        )
        self.assertFalse(rejected.ok)
        self.assertIn("outside its bound scope", rejected.text)

    def test_old_document_compiler_tools_are_not_hydrated(self) -> None:
        scoped = MinionScopedExecutionRuntime(
            ExecutionRuntime(),
            ["op_minion_contract_read", "op_minion_contract_add_unit_outlines_batch"],
            workspace=self.workspace,
        )
        self.assertIsNone(scoped.get_capability_spec("op_minion_contract_read"))
        self.assertIsNone(scoped.get_capability_spec("op_minion_contract_add_unit_outlines_batch"))

    def test_add_finding_is_role_bound_and_includes_a_valid_example(self) -> None:
        self.workspace["minion_v2"].update(
            {"role": "reviewer", "mode": "architecture"}
        )
        scoped = MinionScopedExecutionRuntime(
            ExecutionRuntime(),
            [ADD_FINDING_CAPABILITY],
            workspace=self.workspace,
        )

        provider = scoped.build_llm_tool_contracts()[0]["function"]

        self.assertEqual(provider["name"], "add_finding")
        self.assertEqual(
            provider["input_schema"]["properties"]["finding_kind"]["enum"],
            ["requirements_defect", "contract_defect", "architecture_defect"],
        )
        self.assertIn("Valid example:", provider["description"])
        self.assertNotIn("Input schema:", provider["description"])

    def test_authoring_enums_are_named_in_tool_descriptions(self) -> None:
        gaps: list[str] = []

        def inspect(value: object, *, description: str, path: str) -> None:
            if isinstance(value, list):
                for index, child in enumerate(value):
                    inspect(child, description=description, path=f"{path}[{index}]")
                return
            if not isinstance(value, dict):
                return
            if isinstance(value.get("enum"), list):
                missing = [str(item) for item in value["enum"] if str(item) not in description]
                if missing:
                    gaps.append(f"{path}: {', '.join(missing)}")
            for key, child in value.items():
                inspect(child, description=description, path=f"{path}.{key}")

        authoring_specs = {
            **CONTRACT_BUILDER_TOOL_SPECS,
            **CANDIDATE_BUILDER_TOOL_SPECS,
            **SKELETON_BUILDER_TOOL_SPECS,
            **VERIFICATION_BUILDER_TOOL_SPECS,
        }
        for name, spec in _WORKSPACE_TOOL_SPECS.items():
            if name in authoring_specs:
                inspect(
                    spec["InputModel"].model_json_schema(mode="validation"),
                    description=str(spec.get("description") or ""),
                    path=name,
                )
        self.assertEqual(gaps, [])

    def _complete_base_contract(self) -> dict:
        self.add_complete_unit("report")
        self.assertTrue(
            self.call(
                "contract",
                "op_minion_contract_set_integration",
                {
                    "depends_on": ["report"],
                    "entrypoint": "report_cli",
                    "completion_condition": "the report is emitted",
                    "failure_behavior": "return a deterministic error",
                },
            ).ok
        )
        submitted = self.call("contract", "op_minion_contract_submit")
        self.assertTrue(submitted.ok, submitted.text)
        return json.loads(
            (Path(self.workspace["artifact_stage_dir"]) / "architecture_bundle.json").read_text()
        )

    def _advance_fence(self) -> None:
        binding = dict(self.workspace["minion_v2"])
        self.repository.release_lease(
            self.resource,
            self.invocation,
            int(binding["fencing_token"]),
        )
        lease = self.repository.claim_lease(self.resource, self.invocation, ttl_seconds=60)
        binding["fencing_token"] = lease.fencing_token
        self.workspace["minion_v2"] = binding


if __name__ == "__main__":
    unittest.main()
