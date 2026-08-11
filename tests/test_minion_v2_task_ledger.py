from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.task_ledger import (
    TASK_LEDGER_ARTIFACT,
    TaskRevisionAuthority,
    validate_task_ledger,
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

    def test_materialized_yaml_preserves_authoritative_text_exactly(self) -> None:
        authoritative_text = (
            "# Framepipe\n\n"
            "The stream is split into these chunks exactly:\n"
            "\n"
            "    0000 / 0248 / 69\n"
            "\n"
            "The expected output is `FRAME 4869`.\n"
            "Preserve leading zeroes, punctuation, blank lines, and both trailing newlines.\n\n"
        )
        original = {
            "authoritative_text": authoritative_text,
            "source_name": "TASK.md",
        }
        ref = self.service.task_ledger.publish(
            title="Framepipe contradictory example",
            task_spec=original,
            actor="pal",
            source_channel="test",
        )

        materialized = self.service.task_ledger.materialize(ref)
        task_yaml = yaml.safe_load((materialized.root / "task.yaml").read_text(encoding="utf-8"))

        self.assertEqual(task_yaml["original"]["authoritative_text"], authoritative_text)
        self.assertEqual(
            task_yaml["original"]["authoritative_text"].encode("utf-8"),
            authoritative_text.encode("utf-8"),
        )

    def test_manager_appends_exact_authority_without_compiled_delta(self) -> None:
        base = self.service.task_ledger.publish(
            title="Framepipe",
            task_spec={
                "protocol": {"limit": 64, "zero_length": "eof"},
                "language": "C++20",
            },
            actor="pal",
            source_channel="test",
        )
        authority = TaskRevisionAuthority(
            title="Zero length",
            question="Does a zero-length frame mean EOF or an empty payload?",
            answer="It is an empty payload; EOF is out-of-band.",
            origin="architect_user_clarification",
            observed_at="2026-07-20T12:00:00+00:00",
        )

        revised = self.service.task_ledger.append_revision(
            base_ref=base,
            authority=authority,
            actor="minion-manager",
            source_channel="user_clarification",
        )

        payload = self.service.artifacts.read_json(revised)
        self.assertEqual(payload["original"]["protocol"]["zero_length"], "eof")
        self.assertEqual(payload["revisions"][0]["sequence"], 1)
        self.assertEqual(payload["revisions"][0]["authority"]["answer"], "It is an empty payload; EOF is out-of-band.")
        self.assertEqual(payload["revisions"][0]["authority"]["observed_at"], "2026-07-20T12:00:00+00:00")
        self.assertEqual(set(payload["revisions"][0]), {"sequence", "authority"})

    def test_revision_rejects_blank_manager_communication(self) -> None:
        base = self.service.task_ledger.publish(
            title="Framepipe",
            task_spec={"protocol": {"limit": 64}},
            actor="pal",
            source_channel="test",
        )
        with self.assertRaises(ValueError):
            self.service.task_ledger.append_revision(
                base_ref=base,
                authority={
                    "title": "Limit",
                    "question": "What is the limit?",
                    "answer": " ",
                    "observed_at": "2026-07-20T12:00:00+00:00",
                    "origin": "architect_user_clarification",
                },
                actor="minion-manager",
                source_channel="test",
            )

    def test_historical_compiled_fields_are_not_projected_to_roles(self) -> None:
        normalized = validate_task_ledger(
            {
                "schema_version": "1",
                "title": "Historical",
                "original": {"objective": "Keep one truth source"},
                "revisions": [
                    {
                        "sequence": 1,
                        "authority": {
                            "title": "Choice",
                            "question": "Which behavior?",
                            "answer": "Use the newer behavior.",
                            "observed_at": "2026-07-20T12:00:00+00:00",
                            "origin": "architect_user_clarification",
                        },
                        "summary": "old derived text",
                        "changes": [
                            {
                                "op": "replace",
                                "path": "/objective",
                                "value": "derived rewrite",
                            }
                        ],
                    }
                ],
            }
        )
        self.assertEqual(
            set(normalized["revisions"][0]),
            {"sequence", "authority"},
        )

    def test_unknown_revision_fields_remain_forbidden(self) -> None:
        with self.assertRaisesRegex(ValueError, "unexpected"):
            validate_task_ledger(
                {
                    "schema_version": "1",
                    "title": "Strict",
                    "original": {"objective": "Keep one truth source"},
                    "revisions": [
                        {
                            "sequence": 1,
                            "authority": {
                                "title": "Choice",
                                "question": "Which behavior?",
                                "answer": "Use the newer behavior.",
                                "observed_at": "2026-07-23T12:00:00+00:00",
                                "origin": "architect_user_clarification",
                            },
                            "unexpected": "must not be silently discarded",
                        }
                    ],
                }
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
