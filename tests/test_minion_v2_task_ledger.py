from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.task_ledger import (
    TASK_LEDGER_ARTIFACT,
    effective_task,
)


class PalV2TaskLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal-v2-task-ledger-"))
        self.service = MinionV2WorkflowService(self.root / "runtime")

    def test_materializes_exactly_one_structured_task_yaml(self) -> None:
        original = {
            "objective": "Implement the frame parser.",
            "requirements": {
                "length_limit": {"value": 64, "inclusive": True},
                "zero_length": "empty payload",
            },
        }
        ref = self.service.task_ledger.publish(
            title="Framepipe",
            task_spec=original,
            actor="pal",
            source_channel="test",
        )

        self.assertEqual(ref.artifact_type, TASK_LEDGER_ARTIFACT)
        materialized = self.service.task_ledger.materialize(ref)
        self.assertEqual(materialized.files, ("task.yaml",))
        self.assertEqual(
            [path.name for path in materialized.root.iterdir() if not path.name.startswith(".")],
            ["task.yaml"],
        )
        task_yaml = yaml.safe_load((materialized.root / "task.yaml").read_text(encoding="utf-8"))
        self.assertEqual(task_yaml["original"], original)
        self.assertEqual(task_yaml["revisions"], [])

    def test_revision_preserves_authority_and_applies_only_exact_paths(self) -> None:
        base = self.service.task_ledger.publish(
            title="Framepipe",
            task_spec={
                "protocol": {"limit": 64, "zero_length": "eof"},
                "language": "C++20",
            },
            actor="pal",
            source_channel="test",
        )
        authority = self.service.task_ledger.publish_authority(
            title="Zero length",
            question="Does a zero-length frame mean EOF or an empty payload?",
            answer="It is an empty payload; EOF is out-of-band.",
            origin="architect_user_clarification",
            actor="nathan",
            source_channel="telegram",
            observed_at="2026-07-20T12:00:00+00:00",
        )

        revised = self.service.task_ledger.append_revision(
            base_ref=base,
            authority_ref=authority,
            revision={
                "schema_version": "1",
                "summary": "Treat zero-length frames as empty payloads.",
                "changes": [
                    {
                        "op": "replace",
                        "path": "/protocol/zero_length",
                        "value": "empty payload",
                    }
                ],
            },
            actor="architect-attempt",
            source_channel="role_gateway",
        )

        payload = self.service.artifacts.read_json(revised)
        self.assertEqual(payload["original"]["protocol"]["zero_length"], "eof")
        self.assertEqual(payload["revisions"][0]["sequence"], 1)
        self.assertEqual(payload["revisions"][0]["authority"]["answer"], "It is an empty payload; EOF is out-of-band.")
        self.assertEqual(payload["revisions"][0]["authority"]["observed_at"], "2026-07-20T12:00:00+00:00")
        self.assertEqual(effective_task(payload)["protocol"]["zero_length"], "empty payload")
        self.assertEqual(effective_task(payload)["language"], "C++20")

    def test_revision_rejects_invalid_or_whole_task_changes(self) -> None:
        base = self.service.task_ledger.publish(
            title="Framepipe",
            task_spec={"protocol": {"limit": 64}},
            actor="pal",
            source_channel="test",
        )
        authority = self.service.task_ledger.publish_authority(
            title="Limit",
            question="What is the limit?",
            answer="128",
            origin="architect_user_clarification",
            actor="nathan",
            source_channel="test",
        )
        invalid_revisions = (
            {
                "schema_version": "1",
                "summary": "replace everything",
                "changes": [{"op": "replace", "path": "/", "value": {}}],
            },
            {
                "schema_version": "1",
                "summary": "replace missing",
                "changes": [{"op": "replace", "path": "/protocol/missing", "value": 128}],
            },
            {
                "schema_version": "1",
                "summary": "remove with value",
                "changes": [{"op": "remove", "path": "/protocol/limit", "value": 64}],
            },
        )
        for revision in invalid_revisions:
            with self.subTest(revision=revision), self.assertRaises(ValueError):
                self.service.task_ledger.append_revision(
                    base_ref=base,
                    authority_ref=authority,
                    revision=revision,
                    actor="architect",
                    source_channel="test",
                )

    def test_revision_preserves_explicit_json_null_and_omits_remove_value(self) -> None:
        base = self.service.task_ledger.publish(
            title="Nullable option",
            task_spec={"objective": "Preserve JSON semantics", "obsolete": True},
            actor="pal",
            source_channel="test",
        )
        authority = self.service.task_ledger.publish_authority(
            title="Optional value",
            question="What value should the optional setting use?",
            answer="Use an explicit null and remove the obsolete flag.",
            origin="architect_user_clarification",
            actor="user",
            source_channel="test",
        )

        revised = self.service.task_ledger.append_revision(
            base_ref=base,
            authority_ref=authority,
            revision={
                "schema_version": "1",
                "summary": "Represent the optional value as null and remove obsolete.",
                "changes": [
                    {"op": "add", "path": "/optional", "value": None},
                    {"op": "remove", "path": "/obsolete"},
                ],
            },
            actor="architect",
            source_channel="test",
        )

        payload = self.service.artifacts.read_json(revised)
        self.assertIn("value", payload["revisions"][0]["changes"][0])
        self.assertIsNone(payload["revisions"][0]["changes"][0]["value"])
        self.assertNotIn("value", payload["revisions"][0]["changes"][1])
        self.assertEqual(
            effective_task(payload),
            {"objective": "Preserve JSON semantics", "optional": None},
        )

    def test_structured_legacy_requirements_input_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "normalized Requirements"):
            self.service.prepare_requirements(
                {
                    "title": "legacy",
                    "requirements": [{"section": "x", "statement": "y"}],
                }
            )


if __name__ == "__main__":
    unittest.main()
