from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.llm.contracts import CanonicalToolCall
from pal.minion.v2.contract_builder import contract_builder_tool_result, seed_contract_builder_draft


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

    def test_evidence_can_be_persisted_incrementally(self) -> None:
        first = self.call(
            "evidence",
            "op_minion_evidence_add_batch",
            {
                "evidence": [
                    {
                        "evidence_id": "E-1",
                        "source_kind": "local",
                        "location": "src/a.py:1-3",
                        "line_start": 1,
                        "line_end": 3,
                        "summary": "Defines the public contract.",
                        "supports_requirement_ids": ["R-1"],
                    }
                ]
            },
        )
        self.assertTrue(first.ok)
        second = self.call(
            "evidence",
            "op_minion_evidence_add_batch",
            {
                "evidence": [
                    {
                        "evidence_id": "E-2",
                        "source_kind": "local",
                        "location": "src/b.py:4-8",
                        "line_start": 4,
                        "line_end": 8,
                        "summary": "Shows the required lifecycle.",
                        "supports_requirement_ids": ["R-1"],
                    }
                ]
            },
        )
        self.assertTrue(second.ok)
        submitted = self.call("evidence", "op_minion_evidence_submit")
        self.assertTrue(submitted.ok)
        payload = json.loads(Path(self.produced[-1]["path"]).read_text(encoding="utf-8"))
        self.assertEqual([item["evidence_id"] for item in payload["evidence"]], ["E-1", "E-2"])

        duplicate = self.call(
            "evidence",
            "op_minion_evidence_add_batch",
            {"evidence": [{"evidence_id": "E-2", "source_kind": "local", "location": "x", "content_sha256": "a" * 64}]},
        )
        self.assertFalse(duplicate.ok)
        self.assertIn("duplicate evidence_id", duplicate.text)

    def test_builder_rejects_missing_writable_artifact_stage(self) -> None:
        result = contract_builder_tool_result(
            CanonicalToolCall(name="op_minion_evidence_add_batch", args={"evidence": []}),
            {"contract_builder_stage": "evidence"},
            [],
        )

        self.assertFalse(result.ok)
        self.assertIn("requires artifact_stage_dir", result.text)

    def test_evidence_builder_rejects_noncanonical_source_kind(self) -> None:
        result = self.call(
            "evidence",
            "op_minion_evidence_add_batch",
            {
                "evidence": [
                    {
                        "evidence_id": "E-1",
                        "source_kind": "reference",
                        "location": "qtbase.patch:1-2",
                        "line_start": 1,
                        "line_end": 2,
                        "summary": "Ambiguous source kinds must be corrected before submission.",
                        "supports_requirement_ids": ["R-1"],
                    }
                ]
            },
        )

        self.assertFalse(result.ok)
        self.assertIn("invalid evidence source_kind: reference", result.text)

    def test_evidence_submit_requires_complete_bound_requirement_coverage(self) -> None:
        requirements = self.root / "requirements.json"
        requirements.write_text(
            json.dumps(
                {
                    "requirements": [
                        {"requirement_id": "R-1"},
                        {"requirement_id": "R-2"},
                    ]
                }
            ),
            encoding="utf-8",
        )
        self.workspace["reference_paths"] = [{"name": "requirements", "path": str(requirements)}]
        added = self.call(
            "evidence",
            "op_minion_evidence_add_batch",
            {
                "evidence": [
                    {
                        "evidence_id": "E-1",
                        "source_kind": "local",
                        "location": "src/a.py:1-2",
                        "line_start": 1,
                        "line_end": 2,
                        "summary": "Supports only the first requirement.",
                        "supports_requirement_ids": ["R-1"],
                    }
                ]
            },
        )

        self.assertTrue(added.ok)
        submitted = self.call("evidence", "op_minion_evidence_submit")
        self.assertFalse(submitted.ok)
        self.assertIn("requirements lack supporting evidence: R-2", submitted.text)

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
                    }
                ],
            },
        )
        self.assertTrue(valid.ok)

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


if __name__ == "__main__":
    unittest.main()
