from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import yaml

from pal.minion.v2.architecture_yaml import (
    ArchitectureDraft,
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
        "requirements": {
            "decode_stream_completion": {
                "claim": "decode emits complete payloads and rejects incomplete EOF",
                "owner": "cli_decode_stream",
                "contract_path": [
                    "frame_protocol::feed -> decoder status",
                    "framepipe_cli::decode -> process output and exit",
                ],
            }
        },
        "modules": {
            "frame_protocol": {
                "module_kind": "implementation",
                "behavior_kind": "resource_owner",
                "responsibility": "decode framed byte streams without emitting partial frames",
                "dependencies": {},
                "contract": {
                    "inputs": {
                        "chunks": {
                            "interface": "frame_protocol::feed",
                            "semantics": "accept arbitrary byte chunks in stream order",
                        }
                    },
                    "outputs": {
                        "frames": {
                            "interface": "frame_protocol::feed",
                            "semantics": "return complete payloads in stream order",
                        },
                        "status": {
                            "interface": "frame_protocol::finish",
                            "semantics": "distinguish complete input from incomplete EOF",
                        },
                    },
                    "errors": ["oversized frames fail deterministically"],
                    "invariants": ["partial payloads are never emitted"],
                },
                "ownership": ["each decoder instance owns its buffered input"],
                "lifecycle": {
                    "creation": "starts with an empty buffer",
                    "operation": "accepts chunks until finish",
                    "shutdown": "finish classifies remaining input",
                    "failure": "reports a deterministic error",
                    "cleanup": "destruction releases buffered bytes",
                },
                "state_machine": {
                    "initial": "reading_header",
                    "states": {
                        "reading_header": {
                            "meaning": "waiting for a complete length header",
                            "transitions": {
                                "header_ready": {
                                    "to": "reading_payload",
                                    "effect": "retain the decoded payload length",
                                }
                            },
                        },
                        "reading_payload": {
                            "meaning": "waiting for the declared payload bytes",
                            "transitions": {
                                "frame_ready": {
                                    "to": "reading_header",
                                    "effect": "emit one complete frame",
                                }
                            },
                        },
                    },
                },
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
                "behavior_kind": "workflow",
                "responsibility": "expose frame decoding through the command line",
                "dependencies": {
                    "frame_protocol": {
                        "consumes": ["frames", "status"],
                        "purpose": "decode standard input",
                        "handoff": "feed bytes, print frames, then classify EOF status",
                    }
                },
                "contract": {
                    "inputs": {
                        "stdin_bytes": {
                            "interface": "framepipe_cli::decode",
                            "semantics": "read encoded bytes from standard input",
                        }
                    },
                    "outputs": {
                        "process_result": {
                            "interface": "framepipe_cli::decode",
                            "semantics": "write payloads and return an observable exit status",
                        }
                    },
                    "errors": ["incomplete EOF exits nonzero with a diagnostic"],
                    "invariants": ["only complete frames reach standard output"],
                },
                "ownership": ["the command owns its decoder for one invocation"],
                "lifecycle": {
                    "creation": "constructs a decoder when decode starts",
                    "operation": "streams standard input through the decoder",
                    "shutdown": "finishes the decoder at EOF",
                    "failure": "writes a diagnostic and exits nonzero",
                    "cleanup": "releases command-local resources before exit",
                },
                "state_machine": None,
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
                "requirement_refs": ["decode_stream_completion"],
                "entrypoint": "framepipe decode",
                "contract_flow": [
                    "stdin -> framepipe_cli::decode",
                    "framepipe_cli -> frame_protocol::feed",
                    "frames/status -> stdout/stderr/exit",
                ],
                "observable_behavior": "prints decoded payloads",
                "failure_behavior": "incomplete input exits nonzero with a diagnostic",
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

    def test_initial_template_has_complete_strict_schema_and_valid_example(self) -> None:
        path = prepare_architecture_draft_file(self.workspace)

        self.assertEqual(
            load_architecture_draft(self.workspace),
            {"requirements": {}, "modules": {}, "scenarios": {}},
        )
        text = path.read_text(encoding="utf-8")
        self.assertIn("schema_version: 5", text)
        self.assertIn("requirements: {}", text)
        self.assertIn("modules: {}", text)
        self.assertIn("BEGIN COMPLETE VALID EXAMPLE", text)
        self.assertIn("module_kind enum: implementation | contract_only", text)
        self.assertIn(
            "behavior_kind enum: stateless | resource_owner | service | workflow | adapter",
            text,
        )
        self.assertIn("contract_mode enum: file_frozen | review_guarded", text)
        self.assertIn("kind enum: file | directory", text)
        self.assertIn("consumes:", text)
        self.assertIn("meaning:", text)
        self.assertIn("effect:", text)
        self.assertIn("Do not declare test_scopes", text)

        example_lines: list[str] = []
        inside_example = False
        for line in text.splitlines():
            if line == "# BEGIN COMPLETE VALID EXAMPLE":
                inside_example = True
                continue
            if line == "# END COMPLETE VALID EXAMPLE":
                break
            if inside_example:
                self.assertTrue(line.startswith("#"))
                example_lines.append(line[2:] if line.startswith("# ") else "")
        example = yaml.safe_load("\n".join(example_lines))
        validated = ArchitectureDraft.model_validate(example, strict=True)
        self.assertEqual(validated.schema_version, 5)
        self.assertEqual(
            validated.modules["frame_protocol"].state_machine.states[
                "reading_header"
            ].transitions["header_ready"].effect,
            "retain the decoded payload length",
        )
        self.assertEqual(
            validated.modules["framepipe_cli"].dependencies[
                "frame_protocol"
            ].consumes,
            ["frames", "status"],
        )

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
            "schema_version: 5\nrequirements: {}\nmodules: {}\nmodules: {}\nscenarios: {}\n",
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
                "schema_version: 5\nrequirements: {}\nmodules: &mods {}\nscenarios: *mods\n",
                "unsupported_yaml_feature",
            ),
            (
                "schema_version: 5\nrequirements: {}\nmodules: {}\nscenarios: {}\n---\n{}\n",
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
            "schema_version: 5\nrequirements: {}\nmodules:\n  bad-name:\n    module_kind: implementation\n"
            "    paths:\n      contract_mode: movable\n"
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

    def test_pack_binding_defers_preseeded_file_until_design_is_settled(self) -> None:
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
        self.assertIn("not a discovery input or design checklist", bound.instruction)
        self.assertIn("First read task.yaml", bound.instruction)
        self.assertIn("then write its declaration skeleton", bound.instruction)
        self.assertIn("Only after those phases are complete", bound.instruction)
        self.assertIn("immediately begin edit_file/write_file calls", bound.instruction)
        self.assertIn("do not spend another response restating", bound.instruction)
        self.assertNotIn("Its fixed YAML shape is", bound.instruction)
        self.assertIn("stable snake_case", draft_path.read_text(encoding="utf-8"))
        self.assertIn("contract_flow", draft_path.read_text(encoding="utf-8"))

    def test_module_output_contract_must_be_nonempty(self) -> None:
        submission = _submission()
        submission["modules"]["frame_protocol"]["contract"]["outputs"] = {}

        with self.assertRaises(ArchitectureDraftFileError) as raised:
            write_architecture_draft(self.workspace, submission)

        self.assertEqual(raised.exception.code, "schema_validation_failed")
        self.assertIn(
            "modules.frame_protocol.contract.outputs",
            {item["path"] for item in raised.exception.errors},
        )

    def test_state_transition_target_must_exist(self) -> None:
        submission = _submission()
        submission["modules"]["frame_protocol"]["state_machine"]["states"][
            "reading_payload"
        ]["transitions"]["frame_ready"]["to"] = "missing_state"

        with self.assertRaises(ArchitectureDraftFileError) as raised:
            write_architecture_draft(self.workspace, submission)

        self.assertIn(
            "modules.frame_protocol.state_machine",
            {item["path"] for item in raised.exception.errors},
        )


if __name__ == "__main__":
    unittest.main()
