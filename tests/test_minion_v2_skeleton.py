from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.architecture_templates import ArchitectureTemplateCompiler
from pal.minion.v2.contract_protocol import (
    software_contract_projection,
)
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.skeleton import (
    ArchitectureValidationError,
    compiled_module_write_scopes,
    module_developer_test_path,
    module_verification_corpus_path,
    validate_architecture_submission,
)
from pal.minion.v2.task_ledger import TaskLedgerService


_DECODER_CONTRACT = """/*
Module: decoder
Responsibility: Own incremental frame decoding.
Requirements:
  - Decode complete frames.
Provides: decoded_frames.
Consumes: chunks.
Ownership: Decoder owns its buffer and state.
Lifecycle: construction, feed, finish, reset, destruction.
State: ready or failed.
Invariants: one decoder owns its buffered bytes.
Errors: malformed input enters failed state.
Compatibility: public Decoder interface.
*/
class Decoder;
"""


class SoftwareContractAdapterTests(unittest.TestCase):
    """Protect the private Git adapter behind the public Contract protocol."""

    def setUp(self) -> None:
        self.runtime_root = Path(
            tempfile.mkdtemp(prefix="pal-v2-contract-adapter-runtime-")
        )
        self.repo = Path(
            tempfile.mkdtemp(prefix="pal-v2-contract-adapter-repo-")
        )
        (self.repo / "include").mkdir()
        (self.repo / "src").mkdir()
        (self.repo / "include" / "decoder.hpp").write_text(
            _DECODER_CONTRACT,
            encoding="utf-8",
        )
        (self.repo / "src" / "decoder.cpp").write_text(
            "// implementation placeholder\n",
            encoding="utf-8",
        )
        repository = MinionV2Repository(self.runtime_root)
        artifacts = ContentAddressedArtifactStore(
            self.runtime_root,
            repository,
        )
        self.requirements_ref = TaskLedgerService(
            self.runtime_root,
            artifacts,
        ).publish(
            title="Decoder",
            task_spec={"objective": "Decode complete frames."},
            actor="test",
            source_channel="test",
        )
        self.requirements = artifacts.read_json(self.requirements_ref)
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        self.contract = copy.deepcopy(definition.example)

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_contract_projection_is_the_only_authored_input_to_git_validation(
        self,
    ) -> None:
        projected = software_contract_projection(self.contract)
        normalized = validate_architecture_submission(
            projected,
            requirements_payload=self.requirements,
            workspace_root=self.repo,
        )

        self.assertEqual(set(normalized), {"requirements", "modules", "scenarios"})
        decoder = normalized["modules"]["decoder"]
        self.assertEqual(decoder["responsibility"], "Own incremental frame decoding.")
        self.assertEqual(decoder["paths"]["contract_mode"], "review_guarded")
        self.assertNotIn("contract_schema", normalized)

    def test_git_adapter_rejects_missing_declared_contract_file(self) -> None:
        (self.repo / "include" / "decoder.hpp").unlink()
        with self.assertRaisesRegex(
            ArchitectureValidationError,
            "contract path does not exist",
        ):
            validate_architecture_submission(
                software_contract_projection(self.contract),
                requirements_payload=self.requirements,
                workspace_root=self.repo,
            )

    def test_write_scopes_are_derived_from_module_contract(self) -> None:
        module = software_contract_projection(self.contract)["modules"][
            "decoder"
        ]
        scopes = compiled_module_write_scopes(
            {
                **module["paths"],
                "developer_tests": {
                    "kind": "directory",
                    "path": module_developer_test_path("decoder"),
                },
                "verification_corpus": {
                    "kind": "directory",
                    "path": module_verification_corpus_path("decoder"),
                },
            }
        )

        self.assertIn(
            {"kind": "file", "path": "include/decoder.hpp"},
            scopes,
        )
        self.assertIn(
            {"kind": "file", "path": "src/decoder.cpp"},
            scopes,
        )
        self.assertIn(
            {"kind": "directory", "path": "tests/decoder/developer"},
            scopes,
        )
        self.assertNotIn(
            {"kind": "directory", "path": "tests/decoder/verifier"},
            scopes,
        )


if __name__ == "__main__":
    unittest.main()
