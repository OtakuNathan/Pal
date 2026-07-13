from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.llm.contracts import CanonicalToolCall
from pal.minion.v2.contract_builder import (
    CONTRACT_BUILDER_TOOL_SPECS,
    contract_builder_tool_result,
    seed_contract_builder_draft,
)
from pal.minion.scoped_execution import _WORKSPACE_TOOL_SPECS


def _unit(unit_id: str) -> dict:
    return {
        "unit_id": unit_id,
        "unit_behavior_kind": "stateless",
        "responsibility": "Produce one bounded result.",
        "owned_area": [f"artifact:{unit_id}"],
        "reference_only_paths": [],
        "provided_interfaces": [],
        "consumed_interfaces": [],
        "ownership": {"owner": unit_id},
        "lifecycle": "n/a",
        "state_model": "stateless",
        "invariants": ["Output is deterministic for the same inputs."],
        "error_behavior": [],
        "compatibility": [],
        "dependency_constraints": [],
        "requirement_ids": ["R-1"],
        "verification_obligations": ["Verify the output contract."],
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


class ContractBuilderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal_v2_contract_builder_"))
        self.workspace = {
            "artifact_dir": str(self.root / "artifacts"),
            "artifact_stage_dir": str(self.root / "stage"),
        }
        self.produced: list[dict] = []

    def tearDown(self) -> None:
        shutil.rmtree(self.root, ignore_errors=True)

    def call(self, stage: str, name: str, args: dict | None = None):
        self.workspace["contract_builder_stage"] = stage
        return contract_builder_tool_result(
            CanonicalToolCall(name=name, args=args or {}),
            self.workspace,
            self.produced,
        )

    def test_registered_builder_schemas_have_no_opaque_objects(self) -> None:
        def opaque_paths(value: object, path: str) -> list[str]:
            if isinstance(value, list):
                return [item for index, child in enumerate(value) for item in opaque_paths(child, f"{path}[{index}]")]
            if not isinstance(value, dict):
                return []
            found = []
            if value.get("type") == "object":
                additional = value.get("additionalProperties")
                if "properties" not in value and not isinstance(additional, dict):
                    found.append(path)
            for key, child in value.items():
                found.extend(opaque_paths(child, f"{path}.{key}"))
            return found

        opaque = [
            path
            for name, spec in CONTRACT_BUILDER_TOOL_SPECS.items()
            for path in opaque_paths(spec["parameters_schema"], name)
        ]
        self.assertEqual(opaque, [])

    def test_unit_kind_enum_is_visible_in_schema_and_description(self) -> None:
        spec = CONTRACT_BUILDER_TOOL_SPECS["op_minion_contract_add_unit_outlines_batch"]
        kind = spec["parameters_schema"]["properties"]["units"]["items"]["properties"]["unit_behavior_kind"]
        expected = ["stateless", "resource_owner", "service", "workflow", "adapter"]
        self.assertEqual(kind["enum"], expected)
        for value in expected:
            self.assertIn(value, spec["description"])

    def test_all_scoped_tool_enum_values_are_named_in_descriptions(self) -> None:
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

        for name, spec in _WORKSPACE_TOOL_SPECS.items():
            inspect(
                spec.get("parameters_schema") or {},
                description=str(spec.get("description") or ""),
                path=name,
            )
        self.assertEqual(gaps, [])

    def test_requirements_submit_is_builder_owned(self) -> None:
        replaced = self.call(
            "requirements",
            "op_minion_requirements_replace_batch",
            {"requirements": [{"statement": "Produce a report", "strength": "hard"}]},
        )
        self.assertTrue(replaced.ok)
        submitted = self.call("requirements", "op_minion_requirements_submit")
        self.assertTrue(submitted.ok)
        payload = json.loads(Path(self.produced[-1]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["requirements"][0]["requirement_id"], "R-1")
        self.assertEqual(self.produced[-1]["role"], "primary")

    def test_architect_builder_exposes_contract_only(self) -> None:
        self.assertTrue(self.call("architect", "op_minion_contract_read").ok)
        rejected = self.call(
            "architect",
            "op_minion_requirements_replace_batch",
            {"requirements": [{"statement": "Produce a report", "strength": "hard"}]},
        )
        self.assertFalse(rejected.ok)
        self.assertIn("not available to architect", rejected.text)

    def test_builder_rejects_missing_writable_artifact_stage(self) -> None:
        result = contract_builder_tool_result(
            CanonicalToolCall(name="op_minion_contract_read", args={}),
            {"contract_builder_stage": "contract"},
            [],
        )

        self.assertFalse(result.ok)
        self.assertIn("requires artifact_stage_dir", result.text)

    def test_contract_builder_rejects_cycles_and_milestones(self) -> None:
        invalid = _unit("a")
        invalid["milestones"] = [{"title": "forbidden"}]
        rejected = self.call("contract", "op_minion_contract_add_unit_outlines_batch", {"units": [invalid]})
        self.assertFalse(rejected.ok)

        for unit_id in ("a", "b"):
            accepted = self.call("contract", "op_minion_contract_add_unit_outlines_batch", {"units": [_unit(unit_id)]})
            self.assertTrue(accepted.ok)
        self.call(
            "contract",
            "op_minion_contract_set_integration",
            {
                "topology": {"depends_on": {"a": ["b"], "b": ["a"]}},
                "integration_contract": {"depends_on": ["a", "b"]},
                "assumption_ledger": {"assumptions": []},
                "risk_ledger": {"risks": []},
            },
        )
        submitted = self.call("contract", "op_minion_contract_submit_sketch")
        self.assertFalse(submitted.ok)
        self.assertIn("dependency cycle", submitted.text)

    def test_architecture_review_is_validated_before_submission(self) -> None:
        invalid = self.call(
            "architecture_review",
            "op_minion_architecture_review_submit",
            {"verdict": "FAIL", "findings": [{"kind": "contract_defect", "summary": "missing ownership"}]},
        )
        self.assertFalse(invalid.ok)
        self.assertIn("invalid finding_kind", invalid.text)

        contradictory = self.call(
            "architecture_review",
            "op_minion_architecture_review_submit",
            {
                "verdict": "PASS",
                "findings": [
                    {
                        "finding_kind": "contract_defect",
                        "summary": "missing ownership",
                    }
                ],
            },
        )
        self.assertFalse(contradictory.ok)
        self.assertIn("PASS architecture review cannot contain findings", contradictory.text)

        valid = self.call(
            "architecture_review",
            "op_minion_architecture_review_submit",
            {
                "verdict": "FAIL",
                "findings": [
                    {
                        "finding_kind": "contract_defect",
                        "summary": "missing ownership",
                        "refs": ["R-1"],
                        "revision_targets": [
                            {"section": "unit", "id": "report", "fields": ["ownership"]}
                        ],
                    }
                ],
            },
        )
        self.assertTrue(valid.ok)

    def test_scoped_revision_rejects_unmarked_semantic_drift(self) -> None:
        base = {
            "global_constraints": [{"id": "C-1", "constraint": "Keep ownership explicit."}],
            "design_decisions": [],
            "gate_checks": [],
            "units": [_unit("foundation"), _unit("window")],
            "cross_unit_contracts": [],
            "topology": {"depends_on": {"foundation": [], "window": ["foundation"]}},
            "integration_contract": {"depends_on": ["foundation", "window"]},
            "assumption_ledger": {"assumptions": []},
            "risk_ledger": {"risks": []},
        }
        self.workspace["contract_builder_stage"] = "contract"
        seed_contract_builder_draft(
            self.workspace,
            base,
            revision_scope={
                "write_targets": [
                    {"section": "unit", "id": "foundation", "fields": ["ownership"], "operation": "update"}
                ]
            },
        )
        scoped_read = self.call("contract", "op_minion_contract_revision_read")
        self.assertTrue(scoped_read.ok, scoped_read.text)
        self.assertEqual(scoped_read.structured["current_values"][0]["target"]["id"], "foundation")

        allowed = _unit("foundation")
        allowed["ownership"] = {"owner": "revised-foundation"}
        self.assertTrue(
            self.call("contract", "op_minion_contract_replace_unit_outlines_batch", {"units": [allowed]}).ok
        )
        forbidden = _unit("window")
        forbidden["ownership"] = {"owner": "revised-window"}
        result = self.call("contract", "op_minion_contract_replace_unit_outlines_batch", {"units": [forbidden]})
        self.assertFalse(result.ok)
        self.assertIn("outside its bound scope", result.text)

    def test_contract_submit_compiles_canonical_bundle(self) -> None:
        self.assertTrue(self.call("contract", "op_minion_contract_add_unit_outlines_batch", {"units": [_unit("report")]}).ok)
        self.assertTrue(
            self.call(
                "contract",
                "op_minion_contract_set_integration",
                {
                    "topology": {"depends_on": {"report": []}},
                    "integration_contract": {"depends_on": ["report"]},
                    "assumption_ledger": {"assumptions": []},
                    "risk_ledger": {"risks": []},
                },
            ).ok
        )
        submitted = self.call("contract", "op_minion_contract_submit_sketch")
        self.assertTrue(submitted.ok)
        bundle = json.loads(Path(self.produced[-1]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(bundle["units"][0]["unit_id"], "report")
        self.assertNotIn("milestones", json.dumps(bundle))

    def test_preseeded_revision_replaces_only_existing_units(self) -> None:
        base = {
            "global_constraints": [],
            "design_decisions": [],
            "gate_checks": [],
            "units": [_unit("foundation"), _unit("window")],
            "cross_unit_contracts": [],
            "topology": {"depends_on": {"foundation": [], "window": ["foundation"]}},
            "integration_contract": {"depends_on": ["foundation", "window"]},
            "assumption_ledger": {"assumptions": []},
            "risk_ledger": {"risks": []},
        }
        self.workspace["contract_builder_stage"] = "contract"
        seed_contract_builder_draft(self.workspace, base)
        replacement = _unit("foundation")
        replacement["requirement_ids"] = ["R-1", "R-2"]

        replaced = self.call(
            "contract",
            "op_minion_contract_replace_unit_outlines_batch",
            {"units": [replacement]},
        )
        unknown = self.call(
            "contract",
            "op_minion_contract_replace_unit_outlines_batch",
            {"units": [_unit("invented")]},
        )

        self.assertTrue(replaced.ok, replaced.text)
        self.assertFalse(unknown.ok)
        self.assertIn("unknown unit_id", unknown.text)
        submitted = self.call("contract", "op_minion_contract_submit_sketch")
        self.assertTrue(submitted.ok, submitted.text)
        payload = json.loads(Path(self.produced[-1]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["units"][0]["requirement_ids"], ["R-1", "R-2"])
        self.assertEqual(payload["units"][1], base["units"][1])

    def test_preseeded_revision_upserts_stable_id_collections(self) -> None:
        base = {
            "global_constraints": [{"id": "C-1", "constraint": "Header-only delivery."}],
            "design_decisions": [{"id": "D-1", "decision": "Keep the old boundary."}],
            "gate_checks": [{"id": "G-1", "check": "Compile headers."}],
            "units": [_unit("implementation")],
            "cross_unit_contracts": [{"id": "X-1", "producer": "implementation", "consumer": "integration"}],
            "topology": {"depends_on": {"implementation": []}},
            "integration_contract": {"depends_on": ["implementation"]},
            "assumption_ledger": {"assumptions": []},
            "risk_ledger": {"risks": []},
        }
        self.workspace["contract_builder_stage"] = "contract"
        seed_contract_builder_draft(self.workspace, base)

        self.assertTrue(
            self.call(
                "contract",
                "op_minion_contract_add_constraints_batch",
                {"constraints": [{"id": "C-1", "constraint": "Create the production implementation."}]},
            ).ok
        )
        self.assertTrue(
            self.call(
                "contract",
                "op_minion_contract_add_gate_checks_batch",
                {"gate_checks": [{"id": "G-1", "check": "Exercise the production behavior."}]},
            ).ok
        )
        submitted = self.call("contract", "op_minion_contract_submit_sketch")

        self.assertTrue(submitted.ok, submitted.text)
        payload = json.loads(Path(self.produced[-1]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(payload["global_constraints"], [{"id": "C-1", "constraint": "Create the production implementation."}])
        self.assertEqual(payload["gate_checks"], [{"id": "G-1", "check": "Exercise the production behavior."}])

    def test_contract_validation_rejects_duplicate_stable_ids(self) -> None:
        base = {
            "global_constraints": [
                {"id": "C-1", "constraint": "First."},
                {"id": "C-1", "constraint": "Contradiction."},
            ],
            "design_decisions": [],
            "gate_checks": [],
            "units": [_unit("implementation")],
            "cross_unit_contracts": [],
            "topology": {"depends_on": {"implementation": []}},
            "integration_contract": {"depends_on": ["implementation"]},
            "assumption_ledger": {"assumptions": []},
            "risk_ledger": {"risks": []},
        }
        self.workspace["contract_builder_stage"] = "contract"
        stage = Path(self.workspace["artifact_stage_dir"]) / ".contract_builder" / "contract.json"
        stage.parent.mkdir(parents=True, exist_ok=True)
        stage.write_text(
            json.dumps({"schema_version": "1", "stage": "contract", "lifecycle": "editing", "payload": base}),
            encoding="utf-8",
        )

        result = self.call("contract", "op_minion_contract_validate")

        self.assertFalse(result.ok)
        self.assertIn("duplicate global_constraints id: C-1", result.text)

    def test_revision_seed_collapses_legacy_duplicate_ids_using_latest_value(self) -> None:
        base = {
            "global_constraints": [
                {"id": "C-1", "constraint": "Header-only delivery."},
                {"id": "C-1", "constraint": "Create the production implementation."},
            ],
            "design_decisions": [],
            "gate_checks": [],
            "units": [_unit("implementation")],
            "cross_unit_contracts": [],
            "topology": {"depends_on": {"implementation": []}},
            "integration_contract": {"depends_on": ["implementation"]},
            "assumption_ledger": {"assumptions": []},
            "risk_ledger": {"risks": []},
        }
        self.workspace["contract_builder_stage"] = "contract"

        seed_contract_builder_draft(self.workspace, base)
        submitted = self.call("contract", "op_minion_contract_submit_sketch")

        self.assertTrue(submitted.ok, submitted.text)
        payload = json.loads(Path(self.produced[-1]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(
            payload["global_constraints"],
            [{"id": "C-1", "constraint": "Create the production implementation."}],
        )


if __name__ == "__main__":
    unittest.main()
