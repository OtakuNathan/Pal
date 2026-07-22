from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pal.minion.v2.architecture_yaml import (
    ArchitectureDraftFileError,
    load_architecture_draft,
    prepare_architecture_draft_file,
    write_architecture_draft,
)
from pal.minion.v2.semantic_orchestration.orchestrator import (
    _bind_architecture_yaml_draft,
)
from pal.shared import MinionInvocationPack


def _submission() -> dict[str, object]:
    return {
        "modules": {
            "frame_protocol": {
                "module_kind": "implementation",
                "contract_dependencies": [],
                "paths": {
                    "contract_mode": "file_frozen",
                    "contract_paths": ["include/frame_protocol.h"],
                    "implementation_scopes": [
                        {"kind": "file", "path": "src/frame_protocol.cpp"}
                    ],
                    "reference_only": [],
                },
            },
            "framepipe_cli": {
                "module_kind": "implementation",
                "contract_dependencies": ["frame_protocol"],
                "paths": {
                    "contract_mode": "review_guarded",
                    "contract_paths": ["src/cli.h"],
                    "implementation_scopes": [
                        {"kind": "directory", "path": "src"}
                    ],
                    "reference_only": [],
                },
            },
        },
        "scenarios": {
            "cli_decode_stream": {
                "modules": ["frame_protocol", "framepipe_cli"],
                "entrypoint": "framepipe decode",
                "observable_behavior": "prints decoded payloads",
                "environment": "project host",
            }
        },
    }


class ArchitectureYamlDraftTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="pal-architecture-yaml-")
        self.root = Path(self.temporary.name)
        self.workspace: dict[str, object] = {
            "runtime_root": str(self.root),
            "run_dir": str(self.root / "run"),
        }

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_initial_template_has_dynamic_empty_maps_and_examples(self) -> None:
        path = prepare_architecture_draft_file(self.workspace)

        self.assertEqual(load_architecture_draft(self.workspace), {"modules": {}, "scenarios": {}})
        text = path.read_text(encoding="utf-8")
        self.assertIn("schema_version: 1", text)
        self.assertIn("modules: {}", text)
        self.assertIn("Module example:", text)

    def test_revision_is_preseeded_from_complete_validated_submission(self) -> None:
        self.workspace["architecture_revision_base_submission"] = _submission()

        path = prepare_architecture_draft_file(self.workspace)

        self.assertEqual(load_architecture_draft(self.workspace), _submission())
        self.assertIn("framepipe_cli:", path.read_text(encoding="utf-8"))

    def test_retry_preserves_existing_architect_edits(self) -> None:
        self.workspace["architecture_revision_base_submission"] = _submission()
        path = prepare_architecture_draft_file(self.workspace)
        edited = load_architecture_draft(self.workspace)
        del edited["modules"]["framepipe_cli"]
        write_architecture_draft(self.workspace, edited)

        self.assertEqual(prepare_architecture_draft_file(self.workspace), path)
        self.assertNotIn("framepipe_cli", load_architecture_draft(self.workspace)["modules"])

    def test_duplicate_keys_are_rejected_instead_of_overwritten(self) -> None:
        path = prepare_architecture_draft_file(self.workspace)
        path.write_text(
            "schema_version: 1\nmodules: {}\nmodules: {}\nscenarios: {}\n",
            encoding="utf-8",
        )

        with self.assertRaises(ArchitectureDraftFileError) as raised:
            load_architecture_draft(self.workspace)

        self.assertEqual(raised.exception.code, "invalid_yaml")
        self.assertIn("duplicate key", str(raised.exception))

    def test_yaml_aliases_and_multiple_documents_are_rejected(self) -> None:
        path = prepare_architecture_draft_file(self.workspace)
        for text, code in (
            (
                "schema_version: 1\nmodules: &mods {}\nscenarios: *mods\n",
                "unsupported_yaml_feature",
            ),
            (
                "schema_version: 1\nmodules: {}\nscenarios: {}\n---\n{}\n",
                "multiple_yaml_documents",
            ),
        ):
            with self.subTest(code=code):
                path.write_text(text, encoding="utf-8")
                with self.assertRaises(ArchitectureDraftFileError) as raised:
                    load_architecture_draft(self.workspace)
                self.assertEqual(raised.exception.code, code)

    def test_schema_errors_return_exact_yaml_path(self) -> None:
        path = prepare_architecture_draft_file(self.workspace)
        path.write_text(
            "schema_version: 1\nmodules:\n  bad-name:\n    module_kind: implementation\n"
            "    contract_dependencies: []\n    paths:\n      contract_mode: movable\n"
            "      contract_paths: []\n      implementation_scopes: []\n      reference_only: []\n"
            "scenarios: {}\n",
            encoding="utf-8",
        )

        with self.assertRaises(ArchitectureDraftFileError) as raised:
            load_architecture_draft(self.workspace)

        error_paths = {item["path"] for item in raised.exception.errors}
        self.assertIn("modules.bad-name.[key]", error_paths)
        self.assertIn("modules.bad-name.paths.contract_mode", error_paths)

    def test_yaml_12_boolean_words_remain_strings(self) -> None:
        submission = _submission()
        submission["scenarios"]["cli_decode_stream"]["environment"] = "on"
        write_architecture_draft(self.workspace, submission)

        self.assertEqual(
            load_architecture_draft(self.workspace)["scenarios"]["cli_decode_stream"]["environment"],
            "on",
        )

    def test_pack_binding_names_the_preseeded_file_and_fixed_shape(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="inv_yaml",
            goal="Design the skeleton.",
            instruction="Design the skeleton.",
            workspace={
                **self.workspace,
                "architecture_revision_base_submission": _submission(),
            },
        )

        bound = _bind_architecture_yaml_draft(pack)

        draft_path = Path(str(bound.workspace["architecture_draft_path"]))
        self.assertEqual(load_architecture_draft(bound.workspace), _submission())
        self.assertIn(str(draft_path), bound.instruction)
        self.assertIn("stable snake_case", bound.instruction)
        self.assertIn("any number of entries", bound.instruction)


if __name__ == "__main__":
    unittest.main()
