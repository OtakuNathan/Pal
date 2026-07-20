from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.task_sources import TASK_SOURCE_BUNDLE_ARTIFACT


class PalV2TaskSourceBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="pal-v2-task-source-"))
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.service = MinionV2WorkflowService(self.root / "runtime")

    def test_preserves_request_and_fenced_source_bytes_exactly(self) -> None:
        request = "Implement the frame parser.\n\nDo not reinterpret examples.\n"
        source = (
            "# Framepipe\n\n"
            "The limit is inclusive.\n\n"
            "```text\n"
            "The limit is exclusive in this compatibility example.\n"
            "```\n"
        )
        (self.repo / "TASK.md").write_bytes(source.encode("utf-8"))

        ref = self.service.task_sources.publish(
            title="Framepipe",
            request_text=request,
            workspace={"repo_path": str(self.repo)},
            source_files=["TASK.md"],
            actor="pal",
            source_channel="test",
        )

        self.assertEqual(ref.artifact_type, TASK_SOURCE_BUNDLE_ARTIFACT)
        materialized = self.service.task_sources.materialize(ref)
        self.assertEqual((materialized.root / "request.md").read_text(encoding="utf-8"), request)
        self.assertEqual((materialized.root / "sources" / "TASK.md").read_text(encoding="utf-8"), source)
        self.assertIn("exclusive in this compatibility example", source)

    def test_requirements_edit_appends_raw_amendment_with_provenance(self) -> None:
        base = self.service.task_sources.publish(
            title="Framepipe",
            request_text="Implement the parser.",
            workspace={"repo_path": str(self.repo)},
            actor="pal",
            source_channel="test",
        )
        amendment = "Preserve a zero-length frame as an empty payload; do not treat it as EOF.\n"

        revised = self.service.task_sources.append_amendment(
            base_ref=base,
            amendment_text=amendment,
            workspace={"repo_path": str(self.repo)},
            actor="nathan",
            source_channel="telegram",
            observed_at="2026-07-20T12:00:00+00:00",
        )

        materialized = self.service.task_sources.materialize(revised)
        amendment_path = materialized.root / "amendments" / "001-human-edit.md"
        self.assertEqual(amendment_path.read_text(encoding="utf-8"), amendment)
        payload = self.service.artifacts.read_json(revised)
        self.assertEqual(payload["amendments"][0]["origin"], "human_edit")
        self.assertEqual(payload["amendments"][0]["observed_at"], "2026-07-20T12:00:00+00:00")

    def test_structured_requirements_input_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "normalized Requirements"):
            self.service.prepare_requirements(
                {
                    "title": "legacy",
                    "requirements": [{"section": "x", "statement": "y"}],
                }
            )


if __name__ == "__main__":
    unittest.main()
