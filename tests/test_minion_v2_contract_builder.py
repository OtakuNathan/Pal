from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.llm.contracts import CanonicalToolCall
from pal.minion.v2.contract_builder import contract_builder_tool_result


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
        "evidence_ids": ["E-1"],
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


if __name__ == "__main__":
    unittest.main()
