from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from pal.execution.tool_facade import (
    EffectOutcome,
    RejectedResult,
    RetryDirective,
    rejection,
)
from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2 import (
    ActionEnvelope,
    AggregateType,
    ArtifactRef,
    ContentAddressedArtifactStore,
    MinionV2Repository,
)
from pal.minion.v2.contracts import AggregateSnapshot
from pal.minion.v2.integration import IntegrationOwnershipDefect, IntegrationService
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.review_findings import ADD_FINDING_CAPABILITY, add_finding_tool_result
from pal.minion.v2.verification import (
    DefectKind,
    DefectPropagationService,
    UnknownPolicy,
    VerificationCaseKind,
    VerificationCaseResult,
    VerificationCaseRunner,
    VerificationCaseSpec,
    VerificationService,
    VerificationStatus,
    candidate_reuse_fingerprint,
    finding_fingerprint,
    no_progress_detected,
    repair_bill_semantic_view,
)
from pal.minion.v2.verification_builder import (
    VERIFICATION_BUILDER_TOOL_SPECS,
    compile_verification_invocation_tool_contract,
    dominant_verification_defect_kind,
    effective_verification_policy,
    verification_builder_tool_result,
)
from pal.minion.v2.candidate_builder import (
    CANDIDATE_BUILDER_TOOL_SPECS,
    MinionV2CandidateUpdateChecklistInput,
    candidate_checklist_context,
    candidate_builder_tool_result,
)
from pal.minion.v2.swe_verification import (
    SWE_VERIFICATION_TOOL_SPECS,
    _changed_paths,
    compile_swe_verification_tool_contract,
    infer_repair_target_modules,
    swe_verification_tool_result,
)
from pal.minion.v2.submission_drafts import AUTHORING_CONTRACT_VERSION
from pal.minion.v2.task_ledger import TaskLedgerService
from pal.minion.v2.role_protocol import RoleAssignmentRequest
from pal.shared import RuntimeStatus
from pal.minion.v2.semantic_orchestration.orchestrator import (
    SemanticOrchestrator,
    _compile_standalone_review_markdown,
    _confirmed_verification_findings,
    _recorded_verification_case_results,
    _reject_manager_identity_fields,
    _routable_verification_findings,
    _resolve_dependency_node_id,
    _resolve_verification_defect_targets,
    _seed_durable_verification_scratch,
    _module_verifier_git_diff_refs,
    _validate_skeleton_coder_report,
    _validate_semantic_verification_plan_shape,
    _verifier_reference_refs,
    _verification_case_specs,
    _verification_corpus_path_owners,
    _verification_findings,
    _verification_repair_path_owners,
    _verification_workspace_changed_paths,
    _verification_workspace_from_prompt_pack,
)


class _FakeExecutionAdapter:
    def __init__(self) -> None:
        self.calls: list[CanonicalToolCall] = []
        self.lsp_structured: dict[str, object] = {
            "status": "ok",
            "diagnostics": [],
            "diagnostics_state": "fresh",
        }

    async def execute_tool_async(self, call: CanonicalToolCall, **_kwargs: object) -> CanonicalToolResult:
        self.calls.append(call)
        if call.name == "op_exec_shell":
            completed = subprocess.run(
                str(call.args.get("cmd") or ""),
                cwd=str(call.args.get("cwd") or "."),
                shell=True,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            return CanonicalToolResult(
                name=call.name,
                ok=True,
                text="shell completed",
                llm_text="shell completed",
                structured={
                    "returncode": completed.returncode,
                    "stdout": completed.stdout,
                    "stderr": completed.stderr,
                },
                status=RuntimeStatus.OK,
                call_id=call.call_id,
            )
        if call.name == "op_lsp_diagnostics":
            return CanonicalToolResult(
                name=call.name,
                ok=True,
                text="diagnostics",
                llm_text="diagnostics",
                structured=dict(self.lsp_structured),
                status=RuntimeStatus.OK,
                call_id=call.call_id,
            )
        raise AssertionError(f"unexpected delegated tool: {call.name}")


class MinionV2VerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_minion_v2_verify_"))
        self.repository = MinionV2Repository(self.runtime_root)
        self.store = ContentAddressedArtifactStore(self.runtime_root, self.repository)
        self.verification = VerificationService(self.repository, self.store)
        self.adapter = _FakeExecutionAdapter()
        self.call_index = 0
        self.lease_index = 0

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def _git_repo(self, name: str = "repo") -> tuple[Path, str]:
        root = self.runtime_root / name
        root.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(
            ["git", "-C", str(root), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(root), "config", "user.name", "Test"],
            check=True,
        )
        (root / "src").mkdir()
        (root / "tests" / "router" / "verification").mkdir(parents=True)
        (root / "src" / "router.py").write_text("def route(value):\n    return value\n")
        (root / "tests" / "router" / "verification" / "test_router.py").write_text(
            "def test_route():\n    assert True\n"
        )
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "commit", "-qm", "base"], check=True)
        digest = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        return root, digest

    def test_swe_verifier_uses_semantic_outcomes_and_manager_recorded_evidence(self) -> None:
        repo, _digest = self._git_repo("semantic-tool")
        (repo / "tests" / "router" / "verification" / "test_router.py").write_text(
            "def test_route():\n    assert True\n\ndef test_empty():\n    assert True\n"
        )
        workspace = self._bind_workspace(
            {
                "repo_path": str(repo),
                "artifact_dir": str(self.runtime_root / "semantic-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "semantic-stage"),
                "write_path_scopes": [
                    {"kind": "directory", "path": "tests/router/verification"}
                ],
                "review_tool_evidence_refs": [
                    {
                        "kind": "test_write",
                        "tool_name": "op_file_edit",
                        "ok": True,
                        "args": {"path": "tests/router/verification/test_router.py"},
                    },
                    {
                        "kind": "command",
                        "tool_name": "op_exec_shell",
                        "ok": True,
                        "args": {"cmd": "python -m pytest tests/router/verification/test_router.py"},
                        "output_sha256": "ok",
                    },
                ],
            },
            role="verifier",
        )
        result = swe_verification_tool_result(
            CanonicalToolCall(
                name="op_minion_verification_pass",
                args={},
            ),
            workspace,
            [],
        )
        self.assertTrue(result.ok, result.text)
        submission = json.loads(
            (self.runtime_root / "semantic-stage" / "verification_submission.json").read_text()
        )
        self.assertEqual(submission["outcome"], "pass")
        self.assertEqual(
            submission["changed_test_paths"],
            ["tests/router/verification/test_router.py"],
        )
        self.assertEqual(submission["findings"], [])
        self.assertNotIn("obligation_coverage", submission)
        self.assertNotIn("findings_markdown", submission)

        contract = compile_swe_verification_tool_contract(
            {
                "module_name": "window_runtime",
                "dependencies": {
                    "drawing_backend": {},
                    "event_input": {},
                },
            }
        )
        self.assertEqual(
            contract["dependency_targets"],
            ["drawing_backend", "event_input"],
        )
        performance_guidance = contract["guidance_overrides"][
            ADD_FINDING_CAPABILITY
        ]["use_when"]
        self.assertIn("Material performance defects are valid findings", performance_guidance)
        self.assertIn("representative scaling probe", performance_guidance)
        self.assertIn("bounded contract-preserving optimization direction", performance_guidance)
        dependency_guidance = contract["guidance_overrides"][
            "op_minion_verification_request_dependency_repairs"
        ]
        self.assertIn(
            "first excluding the current module's handling of contract-legal dependency behavior",
            dependency_guidance["use_when"],
        )
        self.assertIn(
            "actual accepted implementation at its public contract boundary",
            dependency_guidance["use_when"],
        )
        self.assertIn(
            "dependency source is absent from this module workspace",
            dependency_guidance["do_not_use_when"],
        )
        self.assertIn(
            "Verifier-authored test double failed",
            dependency_guidance["do_not_use_when"],
        )
        dependency_tool_description = SWE_VERIFICATION_TOOL_SPECS[
            "op_minion_verification_request_dependency_repairs"
        ]["description"]
        self.assertIn(
            "actual accepted implementation",
            dependency_tool_description,
        )
        self.assertIn(
            "the disputed behavior is unspecified",
            dependency_tool_description,
        )

    def test_scenario_module_repair_derives_target_from_finding_location(self) -> None:
        workspace = self._bind_workspace(
            {
                "artifact_dir": str(self.runtime_root / "scenario-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "scenario-stage"),
                "verification_scenario": True,
                "verification_scratch_only": True,
                "review_tool_evidence_refs": [
                    {
                        "kind": "command",
                        "tool_name": "op_exec_shell",
                        "ok": False,
                        "args": {"cmd": "python tests/streaming_decoder/repro.py"},
                        "output_sha256": "failed-reproducer",
                    }
                ],
            },
            role="verifier",
            mode="scenario",
        )
        scratch = Path(str(workspace["review_scratch_dir"]))
        (scratch / "streaming_decoder_repro.py").write_text(
            "raise AssertionError('broken corpus')\n",
            encoding="utf-8",
        )
        contract = compile_swe_verification_tool_contract(
            {
                "verification_name": "streaming_round_trip",
                "scenario": {"modules": ["encoder", "streaming_decoder"]},
                "modules": {
                    "encoder": {
                        "paths": {
                            "contract_paths": ["include/framepipe/encoder.hpp"],
                            "implementation_scopes": [
                                {"kind": "file", "path": "src/encoder.cpp"}
                            ],
                        }
                    },
                    "streaming_decoder": {
                        "paths": {
                            "contract_paths": ["include/framepipe/decoder.hpp"],
                            "implementation_scopes": [
                                {"kind": "file", "path": "src/decoder.cpp"}
                            ],
                        }
                    },
                },
            }
        )
        binding = dict(workspace["minion_v2"])
        binding["swe_verification_tool_contract"] = contract
        workspace["minion_v2"] = binding

        finding = add_finding_tool_result(
            CanonicalToolCall(
                name=ADD_FINDING_CAPABILITY,
                args={
                    "finding_key": "decoder_developer_corpus_is_broken",
                    "finding_kind": "module_defect",
                    "priority": "p1",
                    "summary": "The decoder developer corpus does not compile.",
                    "locations": [
                        {
                            "scope": "workspace",
                            "file": "tests/streaming_decoder/developer/test_decoder.cpp",
                            "line": 17,
                        }
                    ],
                },
                call_id="scenario-finding",
            ),
            workspace,
        )
        self.assertTrue(finding.ok, finding.text)

        result = swe_verification_tool_result(
            CanonicalToolCall(
                name="op_minion_verification_request_module_repair",
                args={},
                call_id="scenario-repair",
            ),
            workspace,
            [],
        )

        self.assertTrue(result.ok, result.text)
        submission = json.loads(
            (self.runtime_root / "scenario-stage" / "verification_submission.json").read_text()
        )
        self.assertEqual(submission["outcome"], "module_repair")
        self.assertEqual(submission["target_modules"], ["streaming_decoder"])

    def test_scenario_corpus_repair_routes_only_to_the_owning_verifier(self) -> None:
        workspace = self._bind_workspace(
            {
                "artifact_dir": str(self.runtime_root / "corpus-repair-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "corpus-repair-stage"),
                "verification_scenario": True,
                "verification_scratch_only": True,
                "review_tool_evidence_refs": [
                    {
                        "kind": "command",
                        "tool_name": "op_exec_shell",
                        "ok": False,
                        "args": {"cmd": "false"},
                        "output_sha256": "bad-double",
                    }
                ],
            },
            role="verifier",
            mode="scenario",
        )
        scratch = Path(str(workspace["review_scratch_dir"]))
        (scratch / "corpus_repro.cpp").write_text(
            "int main() { return 1; }\n",
            encoding="utf-8",
        )
        contract = compile_swe_verification_tool_contract(
            {
                "verification_name": "streaming_round_trip",
                "modules": {
                    "streaming_decoder": {
                        "paths": {
                            "implementation_scopes": [
                                {"kind": "file", "path": "src/decoder.cpp"}
                            ],
                            "verification_corpus": {
                                "kind": "directory",
                                "path": "tests/streaming_decoder/verification",
                            },
                        }
                    }
                },
            }
        )
        binding = dict(workspace["minion_v2"])
        binding["swe_verification_tool_contract"] = contract
        workspace["minion_v2"] = binding
        finding = add_finding_tool_result(
            CanonicalToolCall(
                name=ADD_FINDING_CAPABILITY,
                args={
                    "finding_key": "streaming_decoder_double_has_wrong_expectation",
                    "finding_kind": "verification_defect",
                    "priority": "p1",
                    "summary": "The isolation double asserts an output forbidden by the module contract.",
                    "locations": [
                        {
                            "scope": "workspace",
                            "file": "tests/streaming_decoder/verification/test_double.cpp",
                            "line": 22,
                        }
                    ],
                },
            ),
            workspace,
        )
        self.assertTrue(finding.ok, finding.llm_text)

        result = swe_verification_tool_result(
            CanonicalToolCall(
                name="op_minion_verification_request_corpus_repair",
                args={},
            ),
            workspace,
            [],
        )

        self.assertTrue(result.ok, result.llm_text)
        submission = json.loads(
            (
                self.runtime_root
                / "corpus-repair-stage"
                / "verification_submission.json"
            ).read_text()
        )
        self.assertEqual(submission["outcome"], "verification_repairs")
        self.assertEqual(submission["target_modules"], ["streaming_decoder"])

    def test_swe_verifier_reuses_an_unchanged_durable_corpus(self) -> None:
        repo, _digest = self._git_repo("unchanged-verifier-corpus")
        workspace = self._bind_workspace(
            {
                "repo_path": str(repo),
                "artifact_dir": str(self.runtime_root / "unchanged-corpus-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "unchanged-corpus-stage"),
                "write_path_scopes": [
                    {"kind": "directory", "path": "tests/router/verification"}
                ],
                "review_tool_evidence_refs": [
                    {
                        "kind": "command",
                        "tool_name": "op_exec_shell",
                        "ok": True,
                        "args": {
                            "cmd": (
                                "python -m pytest "
                                "tests/router/verification/test_router.py"
                            )
                        },
                        "output_sha256": "ok",
                    }
                ],
            },
            role="verifier",
        )

        result = swe_verification_tool_result(
            CanonicalToolCall(
                name="op_minion_verification_pass",
                args={},
                call_id="unchanged-corpus-pass",
            ),
            workspace,
            [],
        )

        self.assertTrue(result.ok, result.llm_text)
        submission = json.loads(
            (
                self.runtime_root
                / "unchanged-corpus-stage"
                / "verification_submission.json"
            ).read_text()
        )
        self.assertEqual(submission["changed_test_paths"], [])

    def test_swe_verifier_requires_a_case_when_corpus_is_empty(self) -> None:
        repo, _digest = self._git_repo("empty-verifier-corpus")
        corpus = repo / "tests" / "router" / "verification"
        for path in corpus.iterdir():
            path.unlink()
        subprocess.run(
            ["git", "-C", str(repo), "add", "-A"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "empty corpus"],
            check=True,
        )
        workspace = self._bind_workspace(
            {
                "repo_path": str(repo),
                "artifact_dir": str(self.runtime_root / "empty-corpus-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "empty-corpus-stage"),
                "write_path_scopes": [
                    {"kind": "directory", "path": "tests/router/verification"}
                ],
                "review_tool_evidence_refs": [
                    {
                        "kind": "command",
                        "tool_name": "op_exec_shell",
                        "ok": True,
                        "args": {"cmd": "true"},
                        "output_sha256": "ok",
                    }
                ],
            },
            role="verifier",
        )

        result = swe_verification_tool_result(
            CanonicalToolCall(
                name="op_minion_verification_pass",
                args={},
                call_id="empty-corpus-pass",
            ),
            workspace,
            [],
        )

        self.assertFalse(result.ok)
        self.assertIn("bound verification corpus is empty", result.llm_text)

    def test_scenario_repair_requires_a_module_owned_location(self) -> None:
        with self.assertRaisesRegex(ValueError, "cannot derive a repair owner"):
            infer_repair_target_modules(
                [
                    {
                        "finding_key": "unowned_failure",
                        "locations": [
                            {
                                "scope": "workspace",
                                "file": "tests/scenario/repro.cpp",
                                "line": 1,
                            }
                        ],
                    }
                ],
                {
                    "decoder": [
                        {"kind": "file", "path": "src/decoder.cpp"},
                    ]
                },
            )

    def test_artifact_verifier_uses_durable_scratch_as_its_test_delta(self) -> None:
        scratch = self.runtime_root / "artifact-verifier-scratch"
        scratch.mkdir()
        (scratch / "unsafe_claim_probe.md").write_text(
            "Reproduce the unsupported safety claim.\n",
            encoding="utf-8",
        )

        self.assertEqual(
            _changed_paths(
                {
                    "verification_scratch_only": True,
                    "review_scratch_dir": str(scratch),
                }
            ),
            ["review_scratch/unsafe_claim_probe.md"],
        )

    def test_scenario_workspace_snapshot_never_treats_fingerprint_as_git_revision(self) -> None:
        repo, scenario_commit_sha = self._git_repo("scenario-snapshot")
        scratch = self.runtime_root / "scenario-snapshot-scratch"
        scratch.mkdir()
        (scratch / "end_to_end_probe.txt").write_text("passed\n", encoding="utf-8")
        scenario_fingerprint = "a" * 64

        ref = SemanticOrchestrator(
            MinionV2WorkflowService(self.runtime_root)
        )._publish_verification_workspace(
            review_worktree=repo,
            review_scratch=scratch,
            candidate_identity=scenario_fingerprint,
            git_base_sha=scenario_commit_sha,
            execution_adapter="software_git.v2",
            include_candidate_patch=False,
        )

        snapshot = self.store.read_json(ref)
        self.assertEqual(snapshot["candidate_identity"], scenario_fingerprint)
        self.assertEqual(snapshot["git_base_sha"], scenario_commit_sha)
        self.assertEqual(
            snapshot["changed_paths"],
            ["review_scratch/end_to_end_probe.txt"],
        )
        self.assertEqual(snapshot["workspace_patch_base64"], "")

    def test_workspace_snapshot_uses_explicit_git_base_not_semantic_identity(self) -> None:
        repo, candidate_commit_sha = self._git_repo("module-snapshot")
        test_path = repo / "tests" / "router" / "verification" / "test_router.py"
        test_path.write_text(
            "def test_route():\n    assert True\n\ndef test_empty():\n    assert True\n",
            encoding="utf-8",
        )
        scratch = self.runtime_root / "module-snapshot-scratch"
        scratch.mkdir()

        ref = SemanticOrchestrator(
            MinionV2WorkflowService(self.runtime_root)
        )._publish_verification_workspace(
            review_worktree=repo,
            review_scratch=scratch,
            candidate_identity="b" * 64,
            git_base_sha=candidate_commit_sha,
            execution_adapter="software_git.v2",
        )

        snapshot = self.store.read_json(ref)
        self.assertEqual(
            snapshot["changed_paths"],
            ["tests/router/verification/test_router.py"],
        )
        self.assertTrue(snapshot["workspace_patch_base64"])

    def test_semantic_repair_packet_installs_verifier_tests_before_repair(self) -> None:
        repo, candidate_digest = self._git_repo("repair-install")
        test_path = repo / "tests" / "router" / "verification" / "test_router.py"
        test_path.write_text(
            "def test_route():\n    assert True\n\ndef test_empty_rejected():\n    assert False\n"
        )
        patch_bytes = subprocess.run(
            ["git", "-C", str(repo), "diff", "--binary", candidate_digest, "--"],
            stdout=subprocess.PIPE,
            check=True,
        ).stdout
        subprocess.run(
            ["git", "-C", str(repo), "reset", "--hard", candidate_digest],
            check=True,
            stdout=subprocess.DEVNULL,
        )
        test_delta_ref = self.store.put_json(
            {
                "schema_version": "1",
                "candidate_digest": candidate_digest,
                "workspace_patch_base64": base64.b64encode(patch_bytes).decode(),
                "files": [],
            },
            artifact_type="VerificationTestWorkspaceArtifact",
        )
        repair_ref = self.store.put_json(
            {
                "artifact_kind": "semantic_repair_packet",
                "module_name": "router",
                "findings": [
                    {
                        "finding_key": "empty_input_accepted",
                        "finding_kind": "module_defect",
                        "priority": "p1",
                        "summary": "Empty input is accepted unexpectedly.",
                        "locations": [],
                    }
                ],
                "test_delta_ref": test_delta_ref.to_dict(),
            },
            artifact_type="RepairPacketArtifact",
        )
        node = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            workflow_id="wf-router",
            state="REPAIR_QUEUED",
            version=1,
            created_at="2026-07-19T00:00:00+00:00",
            updated_at="2026-07-19T00:00:00+00:00",
            payload={
                "workspace_path": str(repo),
                "candidate_digest": candidate_digest,
                "repair_bill_ref": repair_ref.to_dict(),
                "base_sha": candidate_digest,
                "path_policy": {
                    "implementation_scopes": [{"kind": "directory", "path": "src"}],
                    "verification_corpus": {
                        "kind": "directory",
                        "path": "tests/router/verification",
                    },
                },
            },
        )
        installed = SemanticOrchestrator(
            MinionV2WorkflowService(self.runtime_root)
        )._install_verifier_tests_for_repair(node)
        self.assertIn("test_empty_rejected", test_path.read_text())
        self.assertEqual(
            installed["verifier_test_paths"],
            ["tests/router/verification/test_router.py"],
        )
        self.assertIn(
            "tests/router/verification/test_router.py",
            installed["manager_seeded_corpus_hashes"],
        )

    def test_semantic_repair_reuses_existing_verifier_corpus_without_new_delta(self) -> None:
        repo, baseline_digest = self._git_repo("repair-existing-corpus")
        promoted_test = (
            repo
            / "tests"
            / "router"
            / "verification"
            / "test_promoted_regression.py"
        )
        promoted_test.write_text("def test_promoted_regression():\n    assert True\n")
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "promote verifier corpus"],
            check=True,
        )
        candidate_digest = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        test_delta_ref = self.store.put_json(
            {
                "schema_version": "1",
                "candidate_digest": candidate_digest,
                "changed_paths": [],
                "workspace_patch_base64": "",
                "files": [],
            },
            artifact_type="VerificationTestWorkspaceArtifact",
        )
        repair_ref = self.store.put_json(
            {
                "artifact_kind": "semantic_repair_packet",
                "module_name": "router",
                "findings": [
                    {
                        "finding_key": "existing_case_failed",
                        "finding_kind": "module_defect",
                        "priority": "p1",
                        "summary": "The existing durable regression failed.",
                        "locations": [
                            {
                                "scope": "workspace",
                                "file": "src/router.py",
                                "line": 1,
                            }
                        ],
                    }
                ],
                "test_delta_ref": test_delta_ref.to_dict(),
                "changed_test_paths": [],
            },
            artifact_type="RepairPacketArtifact",
        )
        node = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router-existing-corpus",
            workflow_id="wf-router",
            state="REPAIR_QUEUED",
            version=1,
            created_at="2026-07-19T00:00:00+00:00",
            updated_at="2026-07-19T00:00:00+00:00",
            payload={
                "workspace_path": str(repo),
                "candidate_digest": candidate_digest,
                "base_sha": baseline_digest,
                "repair_bill_ref": repair_ref.to_dict(),
                "path_policy": {
                    "implementation_scopes": [
                        {"kind": "directory", "path": "src"}
                    ],
                    "verification_corpus": {
                        "kind": "directory",
                        "path": "tests/router/verification",
                    },
                },
            },
        )

        installed = SemanticOrchestrator(
            MinionV2WorkflowService(self.runtime_root)
        )._install_verifier_tests_for_repair(node)

        self.assertEqual(installed["verifier_test_paths"], [])
        self.assertEqual(
            installed["manager_seeded_corpus_hashes"],
            {
                "tests/router/verification/test_promoted_regression.py": (
                    hashlib.sha256(promoted_test.read_bytes()).hexdigest()
                )
            },
        )
        self.assertTrue(
            (
                repo
                / "tests"
                / "router"
                / "verification"
                / "test_router.py"
            ).is_file()
        )

    def test_semantic_verifier_changed_paths_preserve_both_sides_of_rename(self) -> None:
        repo, _candidate_digest = self._git_repo("renamed-verifier-test")
        source = repo / "tests" / "router" / "verification" / "test_router.py"
        destination = repo / "tests" / "router" / "verification" / "test_routes.py"
        source.rename(destination)

        self.assertEqual(
            _changed_paths({"repo_path": str(repo)}),
            ["tests/router/verification/test_router.py", "tests/router/verification/test_routes.py"],
        )

    def test_semantic_verifier_submission_persists_original_candidate_for_snapshot(self) -> None:
        repo, candidate_digest = self._git_repo("semantic-pending")
        candidate_ref = self.store.put_json(
            {"candidate_digest": candidate_digest},
            artifact_type="CandidateSnapshotArtifact",
        )
        submission_ref = self.store.put_json(
            {"outcome": "pass"},
            artifact_type="SemanticVerificationSubmissionArtifact",
        )
        prompt_ref = self.store.put_json(
            {"role": "verifier"},
            artifact_type="RolePromptPackArtifact",
        )
        terminal_ref = self.store.put_json(
            {"finish_reason": "stop"},
            artifact_type="RoleTerminalArtifact",
        )
        node = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            workflow_id="wf-router",
            state="REVIEWING",
            version=4,
            created_at="2026-07-19T00:00:00+00:00",
            updated_at="2026-07-19T00:00:00+00:00",
            payload={
                "path_policy": {
                    "verification_corpus": {
                        "kind": "directory",
                        "path": "tests/router/verification",
                    },
                }
            },
        )
        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        settlement = {
            "role_assignment_id": "assignment-verifier",
            "role_submission_payload_hash": "submission-hash",
        }
        submission = {
            "outcome": "pass",
            "changed_test_paths": [],
            "tool_receipts": [
                {"kind": "command", "ok": True, "structured": {}},
            ],
        }

        with (
            patch.object(
                worker,
                "_role_submission_settlement",
                return_value=settlement,
            ),
            patch.object(
                worker.repository,
                "read_role_assignment",
                return_value={"submission_artifact_ref": submission_ref.to_dict()},
            ),
            patch.object(worker.repository, "read_snapshot", return_value=node),
            patch.object(worker.repository, "dispatch") as dispatch,
            patch.object(worker, "_record_role_turn"),
        ):
            result = worker._complete_semantic_verifier(
                effect={"effect_key": "verify-effect"},
                node=node,
                scenario_mode=False,
                invocation_id="verifier-attempt",
                lease_resource="node:router:review",
                fencing_token=1,
                candidate_ref=candidate_ref,
                candidate_digest=candidate_digest,
                candidate={"candidate_digest": candidate_digest},
                review_workspace=repo,
                review_scratch=self.runtime_root / "semantic-pending-scratch",
                execution_adapter="software_git.v2",
                work_view={"requirements": {}},
                submission=submission,
                terminal={},
                prompt_ref=prompt_ref,
                terminal_ref=terminal_ref,
            )

        pending_ref = result["result_artifact_ref"]
        pending = self.store.read_json(pending_ref)
        self.assertEqual(pending["candidate_ref"], candidate_ref.to_dict())
        self.assertTrue(pending["submitted_workspace_fingerprint"])
        dispatched_action = dispatch.call_args.args[0]
        self.assertEqual(dispatched_action.action_type, "SUBMIT_SEMANTIC_VERIFICATION")

    def test_verifier_settlement_uses_prompt_bound_isolated_workspace(self) -> None:
        source, candidate_digest = self._git_repo("verifier-workspace-source")
        provisioned = self.runtime_root / "provisioned-review"
        role_workspace = self.runtime_root / "role-review"
        subprocess.run(["git", "clone", "-q", str(source), str(provisioned)], check=True)
        subprocess.run(["git", "clone", "-q", str(source), str(role_workspace)], check=True)
        (role_workspace / "tests" / "router" / "verification" / "test_router.py").write_text(
            "def test_route():\n    assert True\n\ndef test_empty():\n    assert True\n"
        )
        review_scratch = self.runtime_root / "role-review-scratch"
        review_scratch.mkdir()
        prompt_ref = self.store.put_json(
            {
                "workspace": {
                    "repo_path": str(role_workspace),
                    "review_scratch_dir": str(review_scratch),
                    "v2_role_workspace": True,
                }
            },
            artifact_type="RolePromptPackArtifact",
        )

        actual_workspace, actual_scratch = _verification_workspace_from_prompt_pack(
            artifacts=self.store,
            prompt_ref=prompt_ref,
        )

        self.assertEqual(actual_workspace, role_workspace)
        self.assertEqual(actual_scratch, review_scratch)
        self.assertEqual(
            _verification_workspace_changed_paths(actual_workspace, candidate_digest),
            ["tests/router/verification/test_router.py"],
        )
        self.assertEqual(
            _verification_workspace_changed_paths(provisioned, candidate_digest),
            [],
        )

    def test_cross_module_verifier_tests_remain_external_to_repair_worktree(self) -> None:
        repo, baseline_digest = self._git_repo("external-repair-test")
        promoted_test = repo / "tests" / "router" / "verification" / "test_router.py"
        promoted_test.write_text(
            "def test_route():\n    assert True\n\n"
            "def test_promoted_regression():\n    assert True\n"
        )
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "commit", "-qm", "promote verifier corpus"],
            check=True,
        )
        candidate_digest = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        test_delta_ref = self.store.put_json(
            {
                "schema_version": "1",
                "candidate_digest": candidate_digest,
                "changed_paths": ["tests/integration/test_pipeline.py"],
                "workspace_patch_base64": "",
                "files": [],
            },
            artifact_type="VerificationTestWorkspaceArtifact",
        )
        repair_ref = self.store.put_json(
            {
                "artifact_kind": "semantic_repair_packet",
                "module_name": "router",
                "findings": [
                    {
                        "finding_key": "pipeline_upstream_defect",
                        "finding_kind": "dependency_defect",
                        "priority": "p1",
                        "summary": "The full pipeline exposes an upstream defect.",
                        "locations": [],
                    }
                ],
                "test_delta_ref": test_delta_ref.to_dict(),
            },
            artifact_type="RepairPacketArtifact",
        )
        node = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            workflow_id="wf-router",
            state="REPAIR_QUEUED",
            version=1,
            created_at="2026-07-19T00:00:00+00:00",
            updated_at="2026-07-19T00:00:00+00:00",
            payload={
                "workspace_path": str(repo),
                "candidate_digest": candidate_digest,
                "base_sha": baseline_digest,
                "repair_bill_ref": repair_ref.to_dict(),
                "path_policy": {
                    "implementation_scopes": [{"kind": "directory", "path": "src"}],
                    "verification_corpus": {
                        "kind": "directory",
                        "path": "tests/router/verification",
                    },
                },
            },
        )

        installed = SemanticOrchestrator(
            MinionV2WorkflowService(self.runtime_root)
        )._install_verifier_tests_for_repair(node)

        self.assertEqual(installed["verifier_test_paths"], [])
        self.assertEqual(
            installed["external_verifier_test_paths"],
            ["tests/integration/test_pipeline.py"],
        )
        self.assertEqual(
            installed["manager_seeded_corpus_hashes"],
            {
                "tests/router/verification/test_router.py": hashlib.sha256(
                    promoted_test.read_bytes()
                ).hexdigest()
            },
        )
        self.assertFalse((repo / "tests" / "integration" / "test_pipeline.py").exists())
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(repo), "status", "--porcelain"],
                stdout=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout,
            "",
        )

    def test_pass_promotes_verifier_test_delta_into_candidate(self) -> None:
        repo, candidate_digest = self._git_repo("pass-promotion")
        test_path = repo / "tests" / "router" / "verification" / "test_router.py"
        test_path.write_text(
            "def test_route():\n    assert True\n\ndef test_empty():\n    assert True\n"
        )
        candidate = {
            "schema_version": "2",
            "candidate_digest": candidate_digest,
            "base_sha": candidate_digest,
            "candidate_tree_sha": subprocess.run(
                ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"],
                stdout=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip(),
            "changed_paths": ["src/router.py"],
        }
        candidate_ref = self.store.put_json(
            candidate,
            artifact_type="CandidateSnapshotArtifact",
        )
        test_delta_ref = self.store.put_json(
            {"changed_paths": ["tests/router/verification/test_router.py"]},
            artifact_type="VerificationTestWorkspaceArtifact",
        )
        node = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            workflow_id="wf-router",
            state="REVIEW_SNAPSHOTTING",
            version=1,
            created_at="2026-07-19T00:00:00+00:00",
            updated_at="2026-07-19T00:00:00+00:00",
            payload={"execution_adapter": "software_git.v2"},
        )
        promoted_ref, promoted_digest, promoted = SemanticOrchestrator(
            MinionV2WorkflowService(self.runtime_root)
        )._promote_verifier_tests(
            node=node,
            review_workspace=repo,
            candidate_ref=candidate_ref,
            candidate=candidate,
            candidate_digest=candidate_digest,
            test_delta_ref=test_delta_ref,
            changed_test_paths=["tests/router/verification/test_router.py"],
        )
        self.assertNotEqual(promoted_digest, candidate_digest)
        self.assertIn("tests/router/verification/test_router.py", promoted["changed_paths"])
        self.assertEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(repo),
                    "show",
                    f"{promoted_digest}:tests/router/verification/test_router.py",
                ],
                stdout=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout,
            test_path.read_text(),
        )
        self.assertEqual(
            self.store.read_json(promoted_ref)["verifier_test_delta_ref"]["sha256"],
            test_delta_ref.sha256,
        )

    def test_pass_imports_promoted_verifier_commit_into_durable_node_repo(self) -> None:
        node_repo, base_digest = self._git_repo("promotion-node")
        (node_repo / "src" / "router.py").write_text(
            "def route(value):\n    return str(value)\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(node_repo), "add", "src/router.py"], check=True)
        subprocess.run(
            ["git", "-C", str(node_repo), "commit", "-qm", "candidate"],
            check=True,
        )
        candidate_digest = subprocess.run(
            ["git", "-C", str(node_repo), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        review_repo = self.runtime_root / "promotion-review"
        subprocess.run(
            ["git", "clone", "-q", str(node_repo), str(review_repo)],
            check=True,
        )
        test_path = review_repo / "tests" / "router" / "verification" / "test_router.py"
        test_path.write_text(
            "def test_route():\n    assert True\n\ndef test_empty():\n    assert True\n",
            encoding="utf-8",
        )
        candidate = {
            "schema_version": "2",
            "candidate_digest": candidate_digest,
            "base_sha": base_digest,
            "candidate_tree_sha": subprocess.run(
                ["git", "-C", str(node_repo), "rev-parse", "HEAD^{tree}"],
                stdout=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip(),
            "changed_paths": ["src/router.py"],
        }
        candidate_ref = self.store.put_json(
            candidate,
            artifact_type="CandidateSnapshotArtifact",
        )
        test_delta_ref = self.store.put_json(
            {"changed_paths": ["tests/router/verification/test_router.py"]},
            artifact_type="VerificationTestWorkspaceArtifact",
        )
        node = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router-isolated-review",
            workflow_id="wf-router",
            state="REVIEW_SNAPSHOTTING",
            version=1,
            created_at="2026-07-19T00:00:00+00:00",
            updated_at="2026-07-19T00:00:00+00:00",
            payload={
                "execution_adapter": "software_git.v2",
                "workspace_path": str(node_repo),
            },
        )

        worker = SemanticOrchestrator(MinionV2WorkflowService(self.runtime_root))
        _promoted_ref, promoted_digest, _promoted = worker._promote_verifier_tests(
            node=node,
            review_workspace=review_repo,
            candidate_ref=candidate_ref,
            candidate=candidate,
            candidate_digest=candidate_digest,
            test_delta_ref=test_delta_ref,
            changed_test_paths=["tests/router/verification/test_router.py"],
        )

        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(node_repo), "rev-parse", "HEAD"],
                stdout=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip(),
            promoted_digest,
        )
        self.assertEqual(
            subprocess.run(
                [
                    "git",
                    "-C",
                    str(node_repo),
                    "show",
                    f"{promoted_digest}:tests/router/verification/test_router.py",
                ],
                stdout=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout,
            test_path.read_text(encoding="utf-8"),
        )
        self.assertEqual(
            subprocess.run(
                ["git", "-C", str(node_repo), "rev-parse", f"{promoted_digest}^"],
                stdout=subprocess.PIPE,
                text=True,
                check=True,
            ).stdout.strip(),
            base_digest,
        )
        consumer = self.runtime_root / "promotion-consumer"
        subprocess.run(["git", "clone", "-q", str(node_repo), str(consumer)], check=True)
        subprocess.run(
            ["git", "-C", str(consumer), "reset", "--hard", base_digest],
            stdout=subprocess.PIPE,
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(consumer), "cherry-pick", promoted_digest],
            stdout=subprocess.PIPE,
            check=True,
        )
        self.assertEqual(
            (consumer / "src" / "router.py").read_text(encoding="utf-8"),
            "def route(value):\n    return str(value)\n",
        )
        self.assertEqual(
            (consumer / "tests" / "router" / "verification" / "test_router.py").read_text(
                encoding="utf-8"
            ),
            test_path.read_text(encoding="utf-8"),
        )

    def test_verifier_receives_scoped_view_and_exact_task_ledgers(self) -> None:
        requirements_ref = TaskLedgerService(self.runtime_root, self.store).publish(
            title="Router",
            task_spec={
                "objective": "Route matching must preserve the user's exact semantics."
            },
            actor="test",
            source_channel="test",
        )
        manifest_ref = self.store.put_json(
            {"requirements_ref": requirements_ref.to_dict()},
            artifact_type="ArchitectureSkeletonArtifact",
        )
        work_view_ref = self.store.put_json(
            {"module_name": "router"},
            artifact_type="ModuleWorkViewArtifact",
        )
        candidate_view_ref = self.store.put_json(
            {"module_name": "router", "changed_paths": ["src/router.py"]},
            artifact_type="CandidateSemanticViewArtifact",
        )

        references = _verifier_reference_refs(
            artifacts=self.store,
            node_payload={
                "architecture_manifest_ref": manifest_ref.to_dict(),
                "producer_report_ref": self.store.put_json(
                    {
                        "checklist": {
                            "plan": [
                                {
                                    "step": "implement exact routing semantics",
                                    "status": "completed",
                                }
                            ],
                        }
                    },
                    artifact_type="ProducerReportArtifact",
                ).to_dict(),
            },
            module_work_view_ref=work_view_ref,
            candidate_diff_ref=candidate_view_ref,
        )

        self.assertEqual(references["module_work_view"], work_view_ref)
        self.assertEqual(references["candidate_diff"], candidate_view_ref)
        self.assertEqual(references["task"], requirements_ref)
        self.assertEqual(
            self.store.read_json(references["coder_report"])["checklist"]["plan"],
            [
                {
                    "step": "implement exact routing semantics",
                    "status": "completed",
                }
            ],
        )
        task_ledger = self.store.read_json(references["task"])
        self.assertEqual(
            task_ledger["original"]["objective"],
            "Route matching must preserve the user's exact semantics.",
        )

    def test_module_verifier_receives_candidate_contract_and_repair_git_diffs(self) -> None:
        repo, skeleton_sha = self._git_repo("verifier-diffs")
        (repo / "src" / "router.py").write_text(
            "def route(value, *, strict=False):\n    return value\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", "src/router.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "candidate one"], check=True)
        previous_candidate = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        (repo / "src" / "router.py").write_text(
            "def route(value, *, strict=False):\n    return value if not strict else str(value)\n",
            encoding="utf-8",
        )
        subprocess.run(["git", "-C", str(repo), "add", "src/router.py"], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "candidate repair"], check=True)
        candidate_digest = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            stdout=subprocess.PIPE,
            text=True,
            check=True,
        ).stdout.strip()
        architecture_ref = self.store.put_json(
            {"skeleton_commit_sha": skeleton_sha},
            artifact_type="ArchitectureSkeletonArtifact",
        )
        candidate = {
            "changed_paths": ["src/router.py"],
            "parent_candidate_digest": previous_candidate,
            "previous_head_sha": previous_candidate,
        }
        candidate_ref = self.store.put_json(
            candidate,
            artifact_type="CandidateSnapshotArtifact",
        )

        references = _module_verifier_git_diff_refs(
            artifacts=self.store,
            node_payload={
                "architecture_manifest_ref": architecture_ref.to_dict(),
                "path_policy": {
                    "contract_mode": "review_guarded",
                    "contract_paths": ["src/router.py"],
                },
            },
            candidate=candidate,
            candidate_ref=candidate_ref,
            candidate_digest=candidate_digest,
            review_worktree=repo,
        )

        self.assertEqual(
            set(references),
            {"candidate_diff", "contract_diff", "repair_diff"},
        )
        candidate_diff = self.store.read_bytes(references["candidate_diff"]).decode("utf-8")
        contract_diff = self.store.read_bytes(references["contract_diff"]).decode("utf-8")
        repair_diff = self.store.read_bytes(references["repair_diff"]).decode("utf-8")
        self.assertIn("Accepted Skeleton -> current Candidate", candidate_diff)
        self.assertIn("strict=False", contract_diff)
        self.assertIn("Previous Candidate -> repaired Candidate", repair_diff)
        self.assertIn("str(value)", repair_diff)

    @staticmethod
    def _verification_submission() -> dict[str, object]:
        return {
            "cases": [
                {
                    "name": "released resource rejects use",
                    "case_kind": "contract_adversarial",
                    "command": ["python", "-m", "pytest", "tests/test_resource.py", "-q"],
                    "expected_exit_codes": [0],
                    "locations": [{"path": "src/resource.py", "symbol": "use"}],
                    "invariants": ["Released is terminal."],
                    "description": "Exercise the public operation after release.",
                }
            ],
            "findings": [],
            "reviewer_summary": "The Manager should run the declared adversarial case.",
        }

    def _bind_workspace(
        self,
        workspace: dict[str, object],
        *,
        role: str,
        mode: str | None = None,
    ) -> dict[str, object]:
        resolved_mode = mode or {
            "implementation": "produce",
            "reviewer": "standalone",
            "verifier": "module",
        }[role]
        self.lease_index += 1
        invocation = f"inv_verify_{self.lease_index}"
        resource = f"verify:{self.lease_index}"
        lease = self.repository.claim_lease(resource, invocation, ttl_seconds=60)
        workspace.update(
            {
                "runtime_root": str(self.runtime_root),
                "review_scratch_dir": str(self.runtime_root / f"scratch-{self.lease_index}"),
                "minion_v2": {
                    "workflow_id": "wf_verify",
                    "invocation_id": invocation,
                    "lease_resource_key": resource,
                    "fencing_token": lease.fencing_token,
                    "role": role,
                    "mode": resolved_mode,
                    "authoring_input_fingerprint": f"verify-input-{self.lease_index}",
                    "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
                },
            }
        )
        Path(str(workspace["review_scratch_dir"])).mkdir(parents=True, exist_ok=True)
        return workspace

    def _verification_call(
        self,
        workspace: dict[str, object],
        name: str,
        args: dict[str, object] | None = None,
        produced: list[dict[str, object]] | None = None,
    ):
        self.call_index += 1
        call = CanonicalToolCall(
            name=name,
            args=args or {},
            call_id=f"verification-call-{self.call_index}",
        )
        if name == ADD_FINDING_CAPABILITY:
            return add_finding_tool_result(call, workspace)
        return asyncio.run(
            verification_builder_tool_result(
                call,
                workspace,
                produced if produced is not None else [],
                original_adapter=self.adapter,
            )
        )

    def _advance_worker_fence(self, workspace: dict[str, object]) -> dict[str, object]:
        binding = dict(workspace["minion_v2"])
        resource = str(binding["lease_resource_key"])
        invocation = str(binding["invocation_id"])
        self.repository.release_lease(
            resource,
            invocation,
            int(binding["fencing_token"]),
        )
        lease = self.repository.claim_lease(resource, invocation, ttl_seconds=60)
        return {
            **workspace,
            "minion_v2": {
                **binding,
                "fencing_token": lease.fencing_token,
            },
        }

    def _candidate_call(
        self,
        workspace: dict[str, object],
        name: str,
        args: dict[str, object] | None = None,
        produced: list[dict[str, object]] | None = None,
    ):
        self.call_index += 1
        return asyncio.run(
            candidate_builder_tool_result(
                CanonicalToolCall(
                    name=name,
                    args=args or {},
                    call_id=f"candidate-call-{self.call_index}",
                ),
                workspace,
                produced if produced is not None else [],
            )
        )

    def _record_lifecycle_case(
        self,
        workspace: dict[str, object],
        *,
        command: str = "printf verified",
    ):
        return self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            {
                "name": "released resource rejects use",
                "command": command,
                "description": "Exercise the public operation after release.",
                "path": "src/resource.py",
                "symbol": "use",
                "invariants": ["Released is terminal."],
            },
        )

    def test_verifier_submit_schema_is_the_only_completion_shape(self) -> None:
        schema = VERIFICATION_BUILDER_TOOL_SPECS["op_minion_verification_submit"][
            "InputModel"
        ].model_json_schema(mode="validation")
        self.assertEqual(schema["properties"], {})
        self.assertFalse(schema["additionalProperties"])
        self.assertNotIn("required", schema)

    def test_verifier_submit_rejects_self_defined_report_fields_in_the_live_turn(self) -> None:
        artifact_dir = self.runtime_root / "artifacts"
        stage_dir = self.runtime_root / "artifact-stage"
        workspace = self._bind_workspace({
            "artifact_dir": str(artifact_dir),
            "artifact_stage_dir": str(stage_dir),
        }, role="verifier")
        produced: list[dict[str, object]] = []
        result = self._verification_call(
            workspace,
            "op_minion_verification_submit",
            {"verdict": "PASS", "contract_clauses": []},
            produced,
        )
        self.assertFalse(result.ok)
        self.assertIn("takes no arguments", result.llm_text)
        self.assertEqual(produced, [])
        self.assertFalse((stage_dir / "verification_plan.json").exists())

    def test_verifier_submit_validates_and_materializes_only_the_canonical_artifact(self) -> None:
        artifact_dir = self.runtime_root / "artifacts"
        stage_dir = self.runtime_root / "artifact-stage"
        work_view = self.runtime_root / "module-work-view.json"
        work_view.write_text(
            json.dumps(
                {
                    "requirements": {
                        "sections": {"Lifecycle": ["Released resources reject further use."]}
                    }
                }
            ),
            encoding="utf-8",
        )
        workspace = self._bind_workspace({
            "repo_path": str(self.runtime_root),
            "artifact_dir": str(artifact_dir),
            "artifact_stage_dir": str(stage_dir),
            "reference_paths": [{"name": "module_work_view", "path": str(work_view)}],
        }, role="verifier")
        produced: list[dict[str, object]] = []
        recorded = self._record_lifecycle_case(workspace)
        self.assertTrue(recorded.ok, recorded.text)
        result = self._verification_call(
            workspace, "op_minion_verification_submit", produced=produced
        )
        self.assertTrue(result.ok)
        self.assertEqual(len(produced), 1)
        self.assertEqual(produced[0]["relative_path"], "verification_plan.json")
        compiled = json.loads((stage_dir / "verification_plan.json").read_text(encoding="utf-8"))
        self.assertEqual(compiled["cases"][0]["name"], "released resource rejects use")
        self.assertEqual(compiled["recorded_results"][0]["status"], "PASS")
        self.assertNotIn("verdict", compiled)

    def test_case_execution_ignores_manager_requirement_catalogs(self) -> None:
        artifact_dir = self.runtime_root / "artifacts"
        stage_dir = self.runtime_root / "artifact-stage"
        work_view = self.runtime_root / "module-work-view.json"
        work_view.write_text(
            json.dumps(
                {
                    "requirements": {
                        "sections": {
                            "Validation": ["Use the exact bound requirement text."],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        workspace = self._bind_workspace({
            "repo_path": str(self.runtime_root),
            "artifact_dir": str(artifact_dir),
            "artifact_stage_dir": str(stage_dir),
            "reference_paths": [{"name": "module_work_view", "path": str(work_view)}],
        }, role="verifier")
        result = self._record_lifecycle_case(workspace)
        self.assertTrue(result.ok, result.llm_text)
        self.assertEqual(result.structured["case"]["requirements"], [])
        self.assertEqual(len(self.adapter.calls), 1)
        self.assertFalse((stage_dir / "verification_plan.json").exists())

    def test_verifier_submit_enforces_policy_before_worker_exit(self) -> None:
        artifact_dir = self.runtime_root / "artifacts"
        stage_dir = self.runtime_root / "artifact-stage"
        work_view = self.runtime_root / "module-work-view.json"
        policy = self.runtime_root / "verification-policy.json"
        work_view.write_text(
            json.dumps(
                {
                    "requirements": {
                        "sections": {
                            "Lifecycle": ["Released resources reject further use."],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        policy.write_text(json.dumps({"lsp_policy": "when_available"}), encoding="utf-8")
        workspace = self._bind_workspace({
            "repo_path": str(self.runtime_root),
            "artifact_dir": str(artifact_dir),
            "artifact_stage_dir": str(stage_dir),
            "reference_paths": [
                {"name": "module_work_view", "path": str(work_view)},
                {"name": "verification_policy", "path": str(policy)},
            ],
        }, role="verifier")
        self.assertTrue(self._record_lifecycle_case(workspace).ok)
        result = self._verification_call(workspace, "op_minion_verification_submit")

        self.assertFalse(result.ok)
        self.assertIn("requires LSP evidence", result.llm_text)
        self.assertFalse((stage_dir / "verification_plan.json").exists())

    def test_verifier_submit_reports_all_current_policy_errors_together(self) -> None:
        work_view = self.runtime_root / "module-work-view-all-errors.json"
        policy = self.runtime_root / "verification-policy-all-errors.json"
        work_view.write_text(
            json.dumps(
                {
                    "requirements": {
                        "sections": {
                            "Lifecycle": ["Released resources reject further use."],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        policy.write_text(
            json.dumps(
                {
                    "require_warning_clean": True,
                    "require_consumer_probe": True,
                    "require_public_surface_dogfood": True,
                    "lsp_policy": "when_available",
                    "allowed_obligations": [
                        "focused_tests",
                        "warning_clean",
                        "consumer_probe",
                        "public_surface_dogfood",
                        "lsp",
                    ],
                }
            ),
            encoding="utf-8",
        )
        workspace = self._bind_workspace(
            {
                "repo_path": str(self.runtime_root),
                "artifact_dir": str(self.runtime_root / "all-error-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "all-error-stage"),
                "reference_paths": [
                    {"name": "module_work_view", "path": str(work_view)},
                    {"name": "verification_policy", "path": str(policy)},
                ],
            },
            role="verifier",
        )
        self.assertTrue(self._record_lifecycle_case(workspace).ok)

        result = self._verification_call(workspace, "op_minion_verification_submit")

        self.assertFalse(result.ok)
        self.assertIn("verification_submit found 4 consistent errors", result.llm_text)
        self.assertIn("warning_clean evidence", result.llm_text)
        self.assertIn("consumer_probe evidence", result.llm_text)
        self.assertIn("public_surface_dogfood evidence", result.llm_text)
        self.assertIn("LSP evidence", result.llm_text)

    def test_lsp_evidence_uses_nested_operation_diagnostics_for_status(self) -> None:
        source = self.runtime_root / "sample.cpp"
        source.write_text("int main() { return 0; }\n", encoding="utf-8")
        workspace = self._bind_workspace(
            {
                "repo_path": str(self.runtime_root),
                "artifact_dir": str(self.runtime_root / "lsp-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "lsp-stage"),
                "primary_language": "cpp",
                "languages": ["cpp"],
                "lsp_environment_fingerprint": "prepared-lsp-environment",
            },
            role="verifier",
        )
        self.adapter.lsp_structured = {
            "status": "ok",
            "evidence": {"environment_fingerprint": "prepared-lsp-environment"},
            "result": {
                "status": "ok",
                "diagnostics_state": "fresh",
                "diagnostics": [
                    {
                        "severity": 1,
                        "code": "pp_file_not_found",
                        "message": "header file not found",
                    }
                ],
            },
        }

        result = self._verification_call(
            workspace,
            "op_minion_verification_run_lsp_check",
            {"name": "sample diagnostics", "file": "sample.cpp"},
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.structured["case"]["status"], "FAIL")
        self.assertEqual(
            result.structured["case"]["environment"]["environment_fingerprint"],
            "prepared-lsp-environment",
        )
        self.assertEqual(result.structured["execution"], self.adapter.lsp_structured)
        self.assertIn("header file not found", result.llm_text)
        delegated = self.adapter.calls[-1]
        self.assertEqual(delegated.args["workspace_root"], str(self.runtime_root))
        self.assertNotIn("primary_language", delegated.args)
        self.assertNotIn("workspace_languages", delegated.args)
        self.assertNotIn("lsp_setup", delegated.args)

    def test_verification_lsp_tool_requires_manager_prepared_entrypoint(self) -> None:
        description = VERIFICATION_BUILDER_TOOL_SPECS[
            "op_minion_verification_run_lsp_check"
        ]["description"]

        self.assertIn("Manager-prepared context", description)
        self.assertIn("instead of invoking a language-server executable", description)
        self.assertIn("do not repair the LSP environment yourself", description)

    def test_verifier_keeps_running_when_historical_order_fails_submit(self) -> None:
        stage_dir = self.runtime_root / "artifact-stage-historical-order"
        work_view = self.runtime_root / "module-work-view-historical-order.json"
        workspace = self._bind_workspace(
            {
                "repo_path": str(self.runtime_root),
                "artifact_dir": str(self.runtime_root / "artifacts-historical-order"),
                "artifact_stage_dir": str(stage_dir),
            },
            role="verifier",
        )
        adversarial = self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            {"name": "adversarial first", "command": "exit 0", "path": "src/router.py"},
        )
        self.assertTrue(adversarial.ok, adversarial.text)
        work_view.write_text(
            json.dumps({"historical_repair_bills": [{"summary": "prior failure"}]}),
            encoding="utf-8",
        )
        workspace["reference_paths"] = [
            {"name": "module_work_view", "path": str(work_view)}
        ]
        historical = self._verification_call(
            workspace,
            "op_minion_verification_run_historical_regression",
            {"name": "historical second", "command": "exit 0", "path": "src/router.py"},
        )
        self.assertTrue(historical.ok, historical.text)

        submitted = self._verification_call(
            workspace, "op_minion_verification_submit", produced=[]
        )

        self.assertFalse(submitted.ok)
        self.assertIn("historical failures before", submitted.llm_text)
        self.assertFalse((stage_dir / "verification_plan.json").exists())
        status = self._verification_call(
            workspace, "op_minion_verification_draft_status"
        )
        self.assertTrue(status.ok, status.text)

    def test_verifier_allows_compile_before_historical_but_gates_adversarial(self) -> None:
        stage_dir = self.runtime_root / "artifact-stage-historical-admission"
        work_view = self.runtime_root / "module-work-view-historical-admission.json"
        work_view.write_text(
            json.dumps({"historical_repair_bills": [{"summary": "prior failure"}]}),
            encoding="utf-8",
        )
        workspace = self._bind_workspace(
            {
                "repo_path": str(self.runtime_root),
                "artifact_dir": str(self.runtime_root / "artifacts-historical-admission"),
                "artifact_stage_dir": str(stage_dir),
                "reference_paths": [
                    {"name": "module_work_view", "path": str(work_view)}
                ],
            },
            role="verifier",
        )
        compile_result = self._verification_call(
            workspace,
            "op_minion_verification_run_compile_check",
            {"name": "compile probe", "command": "exit 0", "path": "src/router.py"},
        )
        self.assertTrue(compile_result.ok, compile_result.text)
        premature = self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            {"name": "premature adversarial", "command": "exit 0", "path": "src/router.py"},
        )
        self.assertFalse(premature.ok)
        self.assertIn("historical RepairBill regression", premature.llm_text)
        historical = self._verification_call(
            workspace,
            "op_minion_verification_run_historical_regression",
            {"name": "historical regression", "command": "exit 0", "path": "src/router.py"},
        )
        self.assertTrue(historical.ok, historical.text)
        adversarial = self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            {"name": "adversarial after regression", "command": "exit 0", "path": "src/router.py"},
        )
        self.assertTrue(adversarial.ok, adversarial.text)

        submitted = self._verification_call(
            workspace, "op_minion_verification_submit", produced=[]
        )

        self.assertTrue(submitted.ok, submitted.text)
        self.assertTrue((stage_dir / "verification_plan.json").is_file())

    def test_verifier_replays_every_named_repair_finding_before_new_cases(self) -> None:
        stage_dir = self.runtime_root / "artifact-stage-named-regressions"
        work_view = self.runtime_root / "module-work-view-named-regressions.json"
        work_view.write_text(
            json.dumps(
                {
                    "historical_repair_bills": [
                        {
                            "findings": [
                                {
                                    "case": "gradient_underflow_probe",
                                    "summary": "Gradient channels underflow.",
                                },
                                {
                                    "case": "dashed_line_clip_bypass",
                                    "summary": "Dashed lines bypass clipping.",
                                },
                            ]
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        workspace = self._bind_workspace(
            {
                "repo_path": str(self.runtime_root),
                "artifact_dir": str(self.runtime_root / "artifacts-named-regressions"),
                "artifact_stage_dir": str(stage_dir),
                "reference_paths": [
                    {"name": "module_work_view", "path": str(work_view)}
                ],
            },
            role="verifier",
        )
        first = self._verification_call(
            workspace,
            "op_minion_verification_run_historical_regression",
            {
                "name": "gradient_underflow_probe",
                "command": "exit 0",
                "path": "src/router.py",
            },
        )
        self.assertTrue(first.ok, first.text)
        premature = self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            {"name": "new risk", "command": "exit 0", "path": "src/router.py"},
        )
        self.assertFalse(premature.ok)
        self.assertIn("dashed_line_clip_bypass", premature.llm_text)
        second = self._verification_call(
            workspace,
            "op_minion_verification_run_historical_regression",
            {
                "name": "dashed_line_clip_bypass",
                "command": "exit 0",
                "path": "src/router.py",
            },
        )
        self.assertTrue(second.ok, second.text)
        adversarial = self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            {"name": "new risk", "command": "exit 0", "path": "src/router.py"},
        )
        self.assertTrue(adversarial.ok, adversarial.text)
        submitted = self._verification_call(
            workspace, "op_minion_verification_submit", produced=[]
        )
        self.assertTrue(submitted.ok, submitted.text)

    def test_repeated_historical_failure_can_submit_without_new_exploration(self) -> None:
        stage_dir = self.runtime_root / "artifact-stage-failed-regression"
        work_view = self.runtime_root / "module-work-view-failed-regression.json"
        work_view.write_text(
            json.dumps(
                {
                    "module_name": "router",
                    "historical_repair_bills": [
                        {
                            "findings": [
                                {
                                    "case": "empty_input_is_stable",
                                    "summary": "Empty input regressed.",
                                }
                            ]
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        workspace = self._bind_workspace(
            {
                "repo_path": str(self.runtime_root),
                "artifact_dir": str(self.runtime_root / "artifacts-failed-regression"),
                "artifact_stage_dir": str(stage_dir),
                "reference_paths": [
                    {"name": "module_work_view", "path": str(work_view)}
                ],
            },
            role="verifier",
        )
        failed = self._verification_call(
            workspace,
            "op_minion_verification_run_historical_regression",
            {
                "name": "empty_input_is_stable",
                "command": "exit 1",
                "path": "src/router.py",
            },
        )
        self.assertTrue(failed.ok, failed.text)
        finding = self._verification_call(
            workspace,
            ADD_FINDING_CAPABILITY,
            {
                "finding_key": "empty_input_is_stable",
                "finding_kind": "module_defect",
                "priority": "p1",
                "summary": "The historical failure remains reproducible.",
                "locations": [{"scope": "workspace", "file": "src/router.py", "line": 1}],
            },
        )
        self.assertTrue(finding.ok, finding.text)
        submitted = self._verification_call(
            workspace, "op_minion_verification_submit", produced=[]
        )
        self.assertTrue(submitted.ok, submitted.text)

    def test_candidate_defect_tool_binds_module_and_derives_report_fields(self) -> None:
        artifact_dir = self.runtime_root / "artifacts"
        stage_dir = self.runtime_root / "artifact-stage"
        work_view = self.runtime_root / "module-work-view.json"
        work_view.write_text(
            json.dumps(
                {
                    "module_name": "font_backend",
                    "requirements": {
                        "sections": {"Font": ["Render text through the native backend."]}
                    },
                    "implementation_scopes": [{"kind": "directory", "path": "src/font"}],
                    "verification_corpus": {
                        "kind": "directory",
                        "path": "tests/font_backend",
                    },
                }
            ),
            encoding="utf-8",
        )
        workspace = self._bind_workspace({
            "artifact_dir": str(artifact_dir),
            "artifact_stage_dir": str(stage_dir),
            "reference_paths": [{"name": "unit_work_view", "path": str(work_view)}],
        }, role="implementation")
        result = self._candidate_call(
            workspace,
            "op_minion_candidate_report_architecture_defect",
            {
            "summary": "The frozen contract cannot represent the required native state.",
                "requirement_section": "Font",
                "requirement": "Render text through the native backend.",
                "path": "include/ohos_font.h",
                "symbol": "OHOSFont",
            },
        )
        self.assertTrue(result.ok, result.text)
        report = json.loads((stage_dir / "coder_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["affected_module"], "font_backend")
        self.assertEqual(report["files_changed"], [])

    def test_workflow_wrapper_preserves_embedded_rejected_result(self) -> None:
        from pal.execution.runtime import ExecutionRuntime
        from pal.minion.scoped_execution import _immutable_workflow_tool

        runtime = ExecutionRuntime(runtime_root=self.runtime_root)
        rejected = rejection(
            "checklist_unfinished",
            "finish the checklist before submission",
        )

        def handler(call, _context):
            return CanonicalToolResult(
                name=call.name,
                ok=False,
                text=rejected.llm_text,
                llm_text=rejected.llm_text,
                structured=rejected.model_dump(mode="json"),
                call_id=call.call_id,
                status=rejected.error_code,
                invocation_result=rejected,
            )

        runtime.register_tool(
            _immutable_workflow_tool(
                name="op_minion_candidate_submit",
                spec=CANDIDATE_BUILDER_TOOL_SPECS[
                    "op_minion_candidate_submit"
                ],
                handler=handler,
            )
        )
        try:
            result = asyncio.run(
                runtime.execute_tool_async(
                    CanonicalToolCall(name="candidate_submit", args={})
                )
            )
            self.assertIsInstance(result.invocation_result, RejectedResult)
            self.assertEqual(
                result.invocation_result.effect,
                EffectOutcome.NOT_STARTED,
            )
            self.assertEqual(
                result.invocation_result.retry,
                RetryDirective.CORRECT_INPUT,
            )
        finally:
            runtime.shutdown()

    def test_candidate_submit_accepts_review_guarded_contract_as_live_git_delta(self) -> None:
        repo = self.runtime_root / "candidate-repo"
        (repo / "src/font").mkdir(parents=True)
        source = repo / "src/font/backend.cpp"
        source.write_text("int render() { return 0; }\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Pal Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "pal@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        source.write_text("int render() { return 1; }\n", encoding="utf-8")
        work_view = self.runtime_root / "candidate-work-view.json"
        work_view.write_text(
            json.dumps(
                {
                    "module_name": "font_backend",
                    "requirements": {"sections": {}},
                    "contract_mode": "review_guarded",
                    "contract_paths": ["src/font/backend.cpp"],
                    "implementation_scopes": [],
                    "verification_corpus": {
                        "kind": "directory",
                        "path": "tests/font_backend",
                    },
                }
            ),
            encoding="utf-8",
        )
        stage_dir = self.runtime_root / "candidate-stage"
        workspace = self._bind_workspace({
            "repo_path": str(repo),
            "artifact_dir": str(self.runtime_root / "candidate-artifacts"),
            "artifact_stage_dir": str(stage_dir),
            "reference_paths": [{"name": "unit_work_view", "path": str(work_view)}],
        }, role="implementation")
        planned = self._candidate_call(
            workspace,
            "op_minion_candidate_update_checklist",
            {
                "plan": [
                    {
                        "step": "implement review-guarded render contract",
                        "status": "in_progress",
                    },
                    {
                        "step": "run focused render check",
                        "status": "pending",
                    },
                ],
            },
        )
        self.assertTrue(planned.ok, planned.text)
        self.assertIn(
            "in_progress: implement review-guarded render contract",
            candidate_checklist_context(workspace),
        )
        unfinished = self._candidate_call(workspace, "op_minion_candidate_submit")
        self.assertFalse(unfinished.ok)
        self.assertIn("unfinished work", unfinished.llm_text)
        self.assertIsInstance(unfinished.invocation_result, RejectedResult)
        self.assertEqual(
            unfinished.invocation_result.effect,
            EffectOutcome.NOT_STARTED,
        )
        self.assertEqual(
            unfinished.invocation_result.retry,
            RetryDirective.CORRECT_INPUT,
        )
        self.assertEqual(
            unfinished.invocation_result.affordances[0].tool,
            "update_checklist",
        )
        self.assertTrue(
            all(
                item["status"] == "completed"
                for item in unfinished.invocation_result.affordances[0].arguments[
                    "plan"
                ]
            )
        )
        completed = self._candidate_call(
            workspace,
            "op_minion_candidate_update_checklist",
            {
                "plan": [
                    {
                        "step": "implement review-guarded render contract",
                        "status": "completed",
                    },
                    {
                        "step": "run focused render check",
                        "status": "completed",
                    },
                ],
            },
        )
        self.assertTrue(completed.ok, completed.text)
        accepted = self._candidate_call(workspace, "op_minion_candidate_submit")
        self.assertTrue(accepted.ok)
        report = json.loads((stage_dir / "coder_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["files_changed"], ["src/font/backend.cpp"])
        self.assertNotIn("tests_run", report)
        self.assertEqual(
            report["checklist"]["plan"],
            [
                {
                    "step": "implement review-guarded render contract",
                    "status": "completed",
                },
                {
                    "step": "run focused render check",
                    "status": "completed",
                },
            ],
        )

    def test_candidate_checklist_contract_is_reloadable_and_evidence_free(self) -> None:
        self.assertEqual(
            MinionV2CandidateUpdateChecklistInput.__module__,
            "pal.minion.v2.candidate_builder",
        )
        schema = MinionV2CandidateUpdateChecklistInput.model_json_schema(
            mode="validation"
        )
        encoded = json.dumps(schema, sort_keys=True)
        self.assertIn('"plan"', encoded)
        self.assertIn('"step"', encoded)
        self.assertIn('"status"', encoded)
        self.assertIn('"pending"', encoded)
        self.assertIn('"in_progress"', encoded)
        self.assertIn('"completed"', encoded)
        self.assertNotIn("evidence", schema["properties"])
        self.assertNotIn("tests", schema["properties"])

    def test_rejected_candidate_submit_does_not_publish_primary_report(self) -> None:
        repo = self.runtime_root / "rejected-candidate-repo"
        (repo / "src/font").mkdir(parents=True)
        source = repo / "src/font/backend.cpp"
        source.write_text("int render() { return 0; }\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.name", "Pal Test"], check=True)
        subprocess.run(["git", "-C", str(repo), "config", "user.email", "pal@example.invalid"], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), "commit", "-qm", "base"], check=True)
        source.write_text("int render() { return 1; }\n", encoding="utf-8")
        work_view = self.runtime_root / "rejected-candidate-work-view.json"
        work_view.write_text(
            json.dumps(
                {
                    "module_name": "font_backend",
                    "requirements": {"sections": {}},
                    "implementation_scopes": [{"kind": "directory", "path": "src/font"}],
                    "verification_corpus": {
                        "kind": "directory",
                        "path": "tests/font_backend",
                    },
                }
            ),
            encoding="utf-8",
        )
        stage_dir = self.runtime_root / "rejected-candidate-stage"
        workspace = self._bind_workspace(
            {
                "repo_path": str(repo),
                "artifact_dir": str(self.runtime_root / "rejected-candidate-artifacts"),
                "artifact_stage_dir": str(stage_dir),
                "reference_paths": [{"name": "unit_work_view", "path": str(work_view)}],
            },
            role="implementation",
        )
        planned = self._candidate_call(
            workspace,
            "op_minion_candidate_update_checklist",
            {
                "plan": [
                    {
                        "step": "implement and check the render contract",
                        "status": "completed",
                    }
                ]
            },
        )
        self.assertTrue(planned.ok, planned.text)
        produced: list[dict[str, object]] = []
        with (
            patch(
                "pal.minion.v2.candidate_builder.SubmissionDraftStore.uses_role_gateway",
                new_callable=PropertyMock,
                return_value=True,
            ),
            patch(
                "pal.minion.v2.candidate_builder.SubmissionDraftStore.mark_submitted",
                side_effect=ValueError("role submission is missing required input reads: repair_bill"),
            ),
        ):
            rejected = self._candidate_call(
                workspace,
                "op_minion_candidate_submit",
                produced=produced,
            )

        self.assertFalse(rejected.ok)
        self.assertIn("missing required input reads", rejected.llm_text)
        self.assertEqual(produced, [])
        self.assertFalse((stage_dir / "coder_report.json").exists())

    def test_artifact_candidate_submit_generates_producer_report(self) -> None:
        workspace_root = self.runtime_root / "artifact-product"
        workspace_root.mkdir()
        product_path = workspace_root / "checkin.json"
        product_path.write_text('{"status":"recorded"}\n', encoding="utf-8")
        work_view = self.runtime_root / "artifact-work-view.json"
        work_view.write_text(
            json.dumps(
                {
                    "unit_contract": {"unit_id": "checkin"},
                    "requirements": [
                        {
                            "section": "Check-in",
                            "statement": "Produce a structured check-in.",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        stage_dir = self.runtime_root / "artifact-candidate-stage"
        workspace = self._bind_workspace(
            {
                "repo_path": str(workspace_root),
                "artifact_dir": str(self.runtime_root / "artifact-candidate-output"),
                "artifact_stage_dir": str(stage_dir),
                "manager_owned_submission_paths": ["producer_report.json"],
                "reference_paths": [
                    {"name": "unit_work_view", "path": str(work_view)}
                ],
            },
            role="implementation",
        )
        produced = [
            {
                "path": str(product_path),
                "relative_path": "checkin.json",
                "role": "primary",
            }
        ]
        planned = self._candidate_call(
            workspace,
            "op_minion_candidate_update_checklist",
            {
                "plan": [
                    {
                        "step": "produce and validate the check-in",
                        "status": "completed",
                    }
                ]
            },
            produced,
        )
        self.assertTrue(planned.ok, planned.text)
        accepted = self._candidate_call(
            workspace,
            "op_minion_candidate_submit",
            produced=produced,
        )
        self.assertTrue(accepted.ok, accepted.text)
        self.assertEqual(produced[0]["role"], "deliverable")
        self.assertEqual(produced[-1]["relative_path"], "producer_report.json")
        report = json.loads(
            (stage_dir / "producer_report.json").read_text(encoding="utf-8")
        )
        self.assertEqual(report["files_changed"], ["checkin.json"])
        self.assertNotIn("tests_run", report)

    def test_artifact_candidate_submit_rejects_an_empty_product_workspace(self) -> None:
        workspace_root = self.runtime_root / "empty-artifact-product"
        workspace_root.mkdir()
        work_view = self.runtime_root / "empty-artifact-work-view.json"
        work_view.write_text(
            json.dumps(
                {
                    "unit_contract": {"unit_id": "checkin"},
                    "requirements": [],
                }
            ),
            encoding="utf-8",
        )
        workspace = self._bind_workspace(
            {
                "repo_path": str(workspace_root),
                "artifact_dir": str(self.runtime_root / "empty-artifact-output"),
                "artifact_stage_dir": str(self.runtime_root / "empty-artifact-stage"),
                "reference_paths": [
                    {"name": "unit_work_view", "path": str(work_view)}
                ],
            },
            role="implementation",
        )
        planned = self._candidate_call(
            workspace,
            "op_minion_candidate_update_checklist",
            {
                "plan": [
                    {
                        "step": "inspect the expected product workspace",
                        "status": "completed",
                    }
                ]
            },
        )
        self.assertTrue(planned.ok, planned.text)
        submitted = self._candidate_call(workspace, "op_minion_candidate_submit")
        self.assertFalse(submitted.ok)
        self.assertIn("contracted product file", submitted.llm_text)

    def test_artifact_candidate_submit_rejects_shell_written_manager_report(self) -> None:
        workspace_root = self.runtime_root / "polluted-artifact-product"
        workspace_root.mkdir()
        (workspace_root / "producer_report.json").write_text("{}\n", encoding="utf-8")
        work_view = self.runtime_root / "polluted-artifact-work-view.json"
        work_view.write_text(
            json.dumps({"unit_contract": {"unit_id": "checkin"}, "requirements": []}),
            encoding="utf-8",
        )
        workspace = self._bind_workspace(
            {
                "repo_path": str(workspace_root),
                "artifact_dir": str(self.runtime_root / "polluted-artifact-output"),
                "artifact_stage_dir": str(self.runtime_root / "polluted-artifact-stage"),
                "manager_owned_submission_paths": ["producer_report.json"],
                "reference_paths": [
                    {"name": "unit_work_view", "path": str(work_view)}
                ],
            },
            role="implementation",
        )
        planned = self._candidate_call(
            workspace,
            "op_minion_candidate_update_checklist",
            {
                "plan": [
                    {
                        "step": "inspect the artifact workspace",
                        "status": "completed",
                    }
                ]
            },
        )
        self.assertTrue(planned.ok, planned.text)
        submitted = self._candidate_call(workspace, "op_minion_candidate_submit")
        self.assertFalse(submitted.ok)
        self.assertIn("Manager-owned submission files", submitted.llm_text)

    def test_recorded_check_is_reused_only_while_worktree_is_unchanged(self) -> None:
        repo = self.runtime_root / "reuse-repo"
        repo.mkdir()
        source = repo / "value.txt"
        source.write_text("first\n", encoding="utf-8")
        workspace = self._bind_workspace(
            {
                "repo_path": str(repo),
                "artifact_dir": str(self.runtime_root / "reuse-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "reuse-stage"),
            },
            role="verifier",
        )
        args = {
            "name": "value exists",
            "command": "test -f value.txt",
            "description": "Check the current candidate tree.",
        }
        first = self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            args,
        )
        second = self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            args,
        )
        self.assertTrue(first.ok, first.text)
        self.assertTrue(second.ok, second.text)
        self.assertTrue(second.structured["reused"])
        self.assertEqual(len(self.adapter.calls), 1)

        source.write_text("second\n", encoding="utf-8")
        third = self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            args,
        )
        self.assertTrue(third.ok, third.text)
        self.assertFalse(bool(third.structured.get("reused")))
        self.assertEqual(len(self.adapter.calls), 2)

    def test_case_reuse_hashes_only_its_declared_scratch_probe(self) -> None:
        repo = self.runtime_root / "probe-reuse-repo"
        repo.mkdir()
        workspace = self._bind_workspace(
            {"repo_path": str(repo)}, role="verifier"
        )
        first_write = self._verification_call(
            workspace,
            "op_minion_verification_scratch_write",
            {"path": "priority_probe.py", "content": "print('first')\n"},
        )
        self.assertTrue(first_write.ok, first_write.text)
        scratch_path = Path(str(first_write.structured["scratch_path"]))
        self.assertEqual(
            scratch_path,
            Path(str(workspace["review_scratch_dir"])).resolve()
            / "priority_probe.py",
        )
        self.assertIn(str(scratch_path), first_write.llm_text)
        args = {
            "name": "priority probe",
            "command": "python ../scratch-does-not-matter.py 2>/dev/null || true",
            "path": "src/router.py",
            "probe_path": "priority_probe.py",
        }
        first = self._verification_call(
            workspace, "op_minion_verification_run_adversarial_case", args
        )
        self.assertTrue(first.ok, first.text)
        self.assertTrue(
            self._verification_call(
                workspace,
                "op_minion_verification_scratch_write",
                {"path": "unrelated.py", "content": "print('unrelated')\n"},
            ).ok
        )
        second = self._verification_call(
            workspace, "op_minion_verification_run_adversarial_case", args
        )
        self.assertTrue(second.structured["reused"])
        replacement = self._verification_call(
            workspace,
            "op_minion_verification_scratch_write",
            {"path": "priority_probe.py", "content": "print('second')\n"},
        )
        self.assertTrue(replacement.ok, replacement.text)
        self.assertEqual(
            scratch_path.read_text(encoding="utf-8"),
            "print('second')\n",
        )
        third = self._verification_call(
            workspace, "op_minion_verification_run_adversarial_case", args
        )
        self.assertFalse(bool(third.structured.get("reused")))

    def test_invocation_tool_contract_is_stable_and_structural(self) -> None:
        work_view = {
            "module_name": "rule_router",
            "contract_paths": ["src/rule_router/protocol.py"],
            "dependencies": {"rule_model": {}},
            "contract_consumption": [
                {
                    "module": "rule_model",
                    "path": "src/rule_model/protocol.py",
                    "symbol": "Rule",
                }
            ],
            "entrypoints": ["tests/test_rule_router.py"],
        }
        policy = {"require_focused_tests": True, "lsp_policy": "when_available"}
        first = compile_verification_invocation_tool_contract(
            work_view=work_view, verification_policy=policy
        )
        second = compile_verification_invocation_tool_contract(
            work_view=work_view, verification_policy=policy
        )
        self.assertEqual(first, second)
        self.assertEqual(first["fingerprint"], second["fingerprint"])
        self.assertNotIn("requirements", first)
        run_description = first["guidance_overrides"][
            "op_minion_verification_run_adversarial_case"
        ]["use_when"]
        self.assertIn("src/rule_router/protocol.py", run_description)
        self.assertIn("tests/test_rule_router.py", run_description)
        self.assertNotIn("workflow_id", json.dumps(first))
        self.assertNotIn("invocation_id", json.dumps(first))
        self.assertIn(
            "op_minion_verification_run_consumer_probe",
            first["allowed_capabilities"],
        )
        self.assertNotIn(
            "op_minion_verification_run_dogfood",
            first["allowed_capabilities"],
        )
        self.assertNotIn(
            "op_minion_verification_run_platform_probe",
            first["allowed_capabilities"],
        )
        self.assertNotIn(
            "op_minion_verification_run_historical_regression",
            first["allowed_capabilities"],
        )
        self.assertFalse(first["verification_policy"]["require_historical_regressions"])
        self.assertNotIn(
            "historical_regressions",
            first["verification_policy"]["allowed_obligations"],
        )
        self.assertNotIn(
            "op_minion_verification_scratch_write",
            first["guidance_overrides"],
        )
        performance_guidance = first["guidance_overrides"][
            ADD_FINDING_CAPABILITY
        ]["use_when"]
        self.assertIn("A performance finding is valid", performance_guidance)
        self.assertIn("representative scaling probe", performance_guidance)
        self.assertIn("do not record speculative micro-optimizations", performance_guidance)
        self.assertIn("bounded contract-preserving optimization direction", performance_guidance)

        scenario = compile_verification_invocation_tool_contract(
            work_view={
                **work_view,
                "verification_name": "rule_router_scenario",
            },
            verification_policy=policy,
        )
        scratch_guidance = scenario["guidance_overrides"][
            "op_minion_verification_scratch_write"
        ]
        self.assertIn("returned scratch_path", scratch_guidance["use_when"])
        self.assertIn(
            "call verification_scratch_write again",
            scratch_guidance["use_when"],
        )
        self.assertIn("product source", scratch_guidance["do_not_use_when"])

    def test_historical_regression_policy_is_enabled_only_for_bound_repair_bills(self) -> None:
        empty = effective_verification_policy(
            work_view={"module_name": "rule_router", "historical_repair_bills": []},
            verification_policy={"require_historical_regressions": True},
        )
        self.assertFalse(empty["require_historical_regressions"])
        self.assertNotIn("historical_regressions", empty["allowed_obligations"])

        contract = compile_verification_invocation_tool_contract(
            work_view={
                "module_name": "rule_router",
                "historical_repair_bills": [
                    {
                        "case_name": "empty_input_is_stable",
                        "finding_summary": "Empty input previously regressed.",
                    }
                ],
            },
            verification_policy={"require_historical_regressions": True},
        )
        self.assertTrue(contract["verification_policy"]["require_historical_regressions"])
        self.assertIn(
            "op_minion_verification_run_historical_regression",
            contract["allowed_capabilities"],
        )
        self.assertEqual(
            [item["case"] for item in contract["required_historical_regressions"]],
            ["empty_input_is_stable"],
        )

    def test_empty_history_contract_rejects_unavailable_history_case(self) -> None:
        contract = compile_verification_invocation_tool_contract(
            work_view={"module_name": "rule_router", "historical_repair_bills": []},
            verification_policy={"require_historical_regressions": True},
        )
        workspace = self._bind_workspace(
            {"repo_path": str(self.runtime_root)}, role="verifier"
        )
        binding = dict(workspace["minion_v2"])
        binding["verification_tool_contract"] = contract
        workspace["minion_v2"] = binding

        result = self._verification_call(
            workspace,
            "op_minion_verification_check_unavailable",
            {
                "name": "no historical bills",
                "obligation": "historical_regressions",
                "reason": "There is no RepairBill to replay.",
                "path": "src/router.py",
            },
        )

        self.assertFalse(result.ok)
        self.assertIn("outside the bound node contract", result.llm_text)

    def test_scenario_tool_contract_exposes_only_declared_usage_mode(self) -> None:
        dogfood = compile_verification_invocation_tool_contract(
            work_view={
                "verification_name": "window_text_rendering",
                "kind": "dogfood",
                "requirements": [
                    {"section": "Rendering", "requirement": "Render a frame."}
                ],
                "accepted_modules": [{"module_name": "window_runtime"}],
                "entrypoints": [
                    {"kind": "build_target", "target": "window_demo"}
                ],
            },
            verification_policy={"require_public_surface_dogfood": True},
        )
        capabilities = set(dogfood["allowed_capabilities"])
        self.assertIn("op_minion_verification_run_dogfood", capabilities)
        self.assertNotIn("op_minion_verification_run_consumer_probe", capabilities)
        self.assertNotIn("op_minion_verification_run_platform_probe", capabilities)

        platform = compile_verification_invocation_tool_contract(
            work_view={
                "verification_name": "native_font_probe",
                "kind": "consumer_probe",
                "requirements": [
                    {"section": "Fonts", "requirement": "Measure native text."}
                ],
                "accepted_modules": [{"module_name": "font_backend"}],
                "entrypoints": [
                    {"kind": "platform_probe", "target": "ohos_font_probe"}
                ],
            },
            verification_policy={},
        )
        platform_capabilities = set(platform["allowed_capabilities"])
        self.assertIn(
            "op_minion_verification_run_consumer_probe", platform_capabilities
        )
        self.assertIn(
            "op_minion_verification_run_platform_probe", platform_capabilities
        )
        self.assertNotIn(
            "op_minion_verification_run_dogfood", platform_capabilities
        )

    def test_module_verifier_rejects_out_of_scope_dogfood_call(self) -> None:
        work_view = {
            "module_name": "rule_router",
            "requirements": {
                "sections": {"Routing": ["Route matching is deterministic."]}
            },
            "contract_paths": ["src/rule_router/protocol.py"],
        }
        contract = compile_verification_invocation_tool_contract(
            work_view=work_view,
            verification_policy={"require_public_surface_dogfood": True},
        )
        workspace = self._bind_workspace(
            {"repo_path": str(self.runtime_root)}, role="verifier"
        )
        binding = dict(workspace["minion_v2"])
        binding["verification_tool_contract"] = contract
        workspace["minion_v2"] = binding

        result = self._verification_call(
            workspace,
            "op_minion_verification_run_dogfood",
            {
                "name": "unscoped whole-product dogfood",
                "command": "exit 0",
                "path": "src/rule_router/protocol.py",
            },
        )

        self.assertFalse(result.ok)
        self.assertIn("outside the bound node contract", result.llm_text)
        self.assertEqual(self.adapter.calls, [])

    def test_mixed_defects_keep_all_findings_and_route_highest_scope(self) -> None:
        findings = [
            {"finding_key": "runtime_case", "finding_kind": "module_defect"},
            {"finding_key": "contract_case", "finding_kind": "contract_defect"},
            {"finding_key": "dependency_case", "finding_kind": "dependency_defect"},
        ]
        self.assertEqual(
            dominant_verification_defect_kind(findings), "contract_defect"
        )

    def test_mixed_defect_submission_preserves_findings_and_compiles_one_route(self) -> None:
        stage_dir = self.runtime_root / "mixed-stage"
        workspace = self._bind_workspace(
            {
                "repo_path": str(self.runtime_root),
                "artifact_dir": str(self.runtime_root / "mixed-artifacts"),
                "artifact_stage_dir": str(stage_dir),
            },
            role="verifier",
        )
        for name in ("runtime shape", "contract shape"):
            recorded = self._verification_call(
                workspace,
                "op_minion_verification_run_adversarial_case",
                {"name": name, "command": "exit 7", "path": "src/router.py"},
            )
            self.assertTrue(recorded.ok, recorded.text)
        for finding_kind, case in (
            ("module_defect", "runtime shape"),
            ("contract_defect", "contract shape"),
        ):
            finding = self._verification_call(
                workspace,
                ADD_FINDING_CAPABILITY,
                {
                    "finding_key": case.replace(" ", "_"),
                    "finding_kind": finding_kind,
                    "priority": "p1",
                    "summary": f"{case} failed",
                },
            )
            self.assertTrue(finding.ok, finding.text)
        submitted = self._verification_call(
            workspace, "op_minion_verification_submit"
        )
        self.assertTrue(submitted.ok, submitted.text)
        compiled = json.loads(
            (stage_dir / "verification_plan.json").read_text(encoding="utf-8")
        )
        self.assertEqual(compiled["defect_kind"], "contract_defect")
        self.assertEqual(len(compiled["findings"]), 2)
        self.assertEqual(
            {item["finding_kind"] for item in compiled["findings"]},
            {"module_defect", "contract_defect"},
        )

    def test_standalone_review_keeps_case_evidence_free_of_manager_requirement_bindings(self) -> None:
        artifact_dir = self.runtime_root / "artifacts"
        stage_dir = self.runtime_root / "artifact-stage"
        review_view = self.runtime_root / "review-view.json"
        review_view.write_text(
            json.dumps(
                {
                    "requirements": {
                        "sections": {"Lifecycle": ["Released resources reject further use."]}
                    }
                }
            ),
            encoding="utf-8",
        )
        workspace = self._bind_workspace({
            "repo_path": str(self.runtime_root),
            "artifact_dir": str(artifact_dir),
            "artifact_stage_dir": str(stage_dir),
            "reference_paths": [{"name": "review_request", "path": str(review_view)}],
        }, role="reviewer")
        recorded = self._record_lifecycle_case(workspace)
        self.assertTrue(recorded.ok, recorded.text)
        self.assertEqual(recorded.structured["case"]["requirements"], [])
        self.assertTrue(
            self._verification_call(
                workspace,
                "op_minion_review_conclusion",
                {
                    "verdict": "changes_requested",
                    "summary": "A lifecycle defect blocks approval.",
                    "scope": "resource lifecycle",
                },
            ).ok
        )
        result = self._verification_call(workspace, "op_minion_standalone_review_submit")
        self.assertTrue(result.ok, result.text)
        compiled = json.loads((stage_dir / "standalone_review.json").read_text(encoding="utf-8"))
        self.assertEqual(compiled["cases"][0]["requirements"], [])

    def test_case_execution_does_not_semantically_bind_requirement_text(self) -> None:
        workspace = self._bind_workspace(
            {
                "repo_path": str(self.runtime_root),
            },
            role="verifier",
        )
        result = self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            {
                "name": "fractional priority",
                "command": "exit 7",
            },
        )
        self.assertTrue(result.ok, result.llm_text)
        self.assertEqual(result.structured["case"]["requirements"], [])
        self.assertEqual(len(self.adapter.calls), 1)

    def test_draft_case_and_finding_are_upserted_and_removable(self) -> None:
        workspace = self._bind_workspace(
            {"repo_path": str(self.runtime_root)}, role="verifier"
        )
        first = self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            {
                "name": "priority shape",
                "command": "exit 7",
                "path": "src/router.py",
            },
        )
        self.assertTrue(first.ok, first.text)
        module_finding = self._verification_call(
            workspace,
            ADD_FINDING_CAPABILITY,
            {
                "finding_key": "fractional_priority_accepted",
                "finding_kind": "module_defect",
                "priority": "p1",
                "summary": "Fractional priority is accepted.",
            },
        )
        self.assertTrue(module_finding.ok, module_finding.text)
        contract_finding = self._verification_call(
            workspace,
            ADD_FINDING_CAPABILITY,
            {
                "finding_key": "priority_shape_underspecified",
                "finding_kind": "contract_defect",
                "priority": "p1",
                "summary": "Priority shape is underspecified.",
            },
        )
        self.assertTrue(contract_finding.ok, contract_finding.text)
        status = self._verification_call(
            workspace, "op_minion_verification_draft_status"
        )
        self.assertTrue(status.ok, status.text)
        self.assertEqual(len(status.structured["cases"]), 1)
        self.assertEqual(len(status.structured["findings"]), 2)
        self.assertEqual(
            {item["finding_kind"] for item in status.structured["findings"]},
            {"module_defect", "contract_defect"},
        )
        duplicate = self._verification_call(
            workspace,
            ADD_FINDING_CAPABILITY,
            {
                "finding_key": "fractional_priority_accepted",
                "finding_kind": "module_defect",
                "priority": "p0",
                "summary": "Fractional priority is accepted and corrupts ordering.",
            },
        )
        self.assertTrue(duplicate.ok, duplicate.text)
        self.assertTrue(duplicate.structured["replaced"])
        removed = self._verification_call(
            workspace,
            "op_minion_verification_remove_finding",
            {
                "finding_key": "priority_shape_underspecified",
                "reason": "The finding was classified against the wrong contract layer.",
            },
        )
        self.assertTrue(removed.ok, removed.text)
        after_removal = self._verification_call(
            workspace, "op_minion_verification_draft_status"
        )
        self.assertEqual(
            [item["summary"] for item in after_removal.structured["findings"]],
            ["Fractional priority is accepted and corrupts ordering."],
        )
        removed_case = self._verification_call(
            workspace,
            "op_minion_verification_remove_case",
            {"name": "priority shape", "reason": "The probe exercised a superseded test fixture."},
        )
        self.assertTrue(removed_case.ok, removed_case.text)
        final = self._verification_call(
            workspace, "op_minion_verification_draft_status"
        )
        self.assertEqual(final.structured["cases"], [])
        self.assertEqual(
            [item["finding_key"] for item in final.structured["findings"]],
            ["fractional_priority_accepted"],
        )

    def test_retry_fence_preserves_failed_evidence_and_finding_for_submit(self) -> None:
        stage_dir = self.runtime_root / "artifact-stage"
        workspace = self._bind_workspace(
            {
                "artifact_dir": str(self.runtime_root / "artifacts"),
                "artifact_stage_dir": str(stage_dir),
                "repo_path": str(self.runtime_root),
            },
            role="verifier",
        )
        failed = self._record_lifecycle_case(workspace, command="exit 7")
        self.assertTrue(failed.ok, failed.text)
        finding = self._verification_call(
            workspace,
            ADD_FINDING_CAPABILITY,
            {
                "finding_key": "released_resource_remains_usable",
                "finding_kind": "module_defect",
                "priority": "p1",
                "summary": "Released resource remains usable.",
            },
        )
        self.assertTrue(finding.ok, finding.text)

        retry_workspace = self._advance_worker_fence(workspace)
        status = self._verification_call(
            retry_workspace,
            "op_minion_verification_draft_status",
        )
        self.assertTrue(status.ok, status.text)
        self.assertEqual(status.structured["cases"], [
            {"name": "released resource rejects use", "status": "FAIL"}
        ])
        self.assertEqual(len(status.structured["findings"]), 1)

        produced: list[dict[str, object]] = []
        submitted = self._verification_call(
            retry_workspace,
            "op_minion_verification_submit",
            produced=produced,
        )
        self.assertTrue(submitted.ok, submitted.text)
        compiled = json.loads((stage_dir / "verification_plan.json").read_text(encoding="utf-8"))
        self.assertEqual(compiled["defect_kind"], "module_defect")
        self.assertEqual(len(compiled["findings"]), 1)

    def test_manager_accepts_exact_submission_receipt_from_prior_fence(self) -> None:
        stage_dir = self.runtime_root / "artifact-stage-receipt"
        workspace = self._bind_workspace(
            {
                "artifact_dir": str(self.runtime_root / "artifacts-receipt"),
                "artifact_stage_dir": str(stage_dir),
                "repo_path": str(self.runtime_root),
            },
            role="verifier",
        )
        recorded = self._record_lifecycle_case(workspace)
        self.assertTrue(recorded.ok, recorded.text)
        submitted = self._verification_call(
            workspace,
            "op_minion_verification_submit",
            produced=[],
        )
        self.assertTrue(submitted.ok, submitted.text)
        plan = json.loads(
            (stage_dir / "verification_plan.json").read_text(encoding="utf-8")
        )
        cases = _verification_case_specs(plan["cases"])
        retry_workspace = self._advance_worker_fence(workspace)
        binding = dict(retry_workspace["minion_v2"])

        results = _recorded_verification_case_results(
            plan,
            cases=cases,
            artifacts=self.store,
            runtime_root=self.runtime_root,
            workflow_id=str(binding["workflow_id"]),
            invocation_id=str(binding["invocation_id"]),
            lease_resource_key=str(binding["lease_resource_key"]),
            fencing_token=int(binding["fencing_token"]),
            role="verifier",
            mode="module",
            draft_kind="verification",
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, VerificationStatus.PASS)

    def test_manager_resolves_fenced_attempt_to_logical_role_session(self) -> None:
        logical_session_id = "inv_logical_verifier"
        input_fingerprint = "logical-verifier-input"
        self.repository.ensure_role_session(
            session_id=logical_session_id,
            workflow_id="wf_verify",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node_drawing",
            role="verifier",
            mode="module",
            executor_profile_id="software_engineering.v2_verifier",
            family_binding_sha="binding",
        )
        assignment = self.repository.create_role_assignment(
            RoleAssignmentRequest(
                assignment_key="logical-verifier-assignment",
                session_id=logical_session_id,
                workflow_id="wf_verify",
                aggregate_type=AggregateType.DAG_NODE_RUN.value,
                aggregate_id="node_drawing",
                role="verifier",
                mode="module",
                executor_profile_id="software_engineering.v2_verifier",
                family_binding_sha="binding",
                input_fingerprint=input_fingerprint,
                required_inputs=(),
                input_refs={},
                execution_spec={"effect_type": "run_verifier_role"},
                submission_kind="verification",
            )
        )
        attempt = self.repository.claim_role_assignment(assignment["assignment_id"])
        lease_resource_key = f"assignment:{assignment['assignment_id']}"
        lease = self.repository.claim_lease(
            lease_resource_key,
            str(attempt["attempt_id"]),
            ttl_seconds=60,
        )
        prompt_ref = self.store.put_json(
            {"role": "verifier"},
            artifact_type="RolePromptPackArtifact",
        )
        self.repository.start_role_attempt(
            assignment_id=str(assignment["assignment_id"]),
            attempt_id_value=str(attempt["attempt_id"]),
            lease_resource_key=lease_resource_key,
            fencing_token=lease.fencing_token,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        stage_dir = self.runtime_root / "artifact-stage-attempt-receipt"
        workspace = {
            "runtime_root": str(self.runtime_root),
            "repo_path": str(self.runtime_root),
            "review_scratch_dir": str(self.runtime_root / "scratch-attempt-receipt"),
            "artifact_dir": str(self.runtime_root / "artifacts-attempt-receipt"),
            "artifact_stage_dir": str(stage_dir),
            "minion_v2": {
                "workflow_id": "wf_verify",
                "invocation_id": str(attempt["attempt_id"]),
                "lease_resource_key": lease_resource_key,
                "fencing_token": lease.fencing_token,
                "role": "verifier",
                "mode": "module",
                "authoring_input_fingerprint": input_fingerprint,
                "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
            },
        }
        Path(str(workspace["review_scratch_dir"])).mkdir(parents=True, exist_ok=True)
        recorded = self._record_lifecycle_case(workspace)
        self.assertTrue(recorded.ok, recorded.text)
        submitted = self._verification_call(
            workspace,
            "op_minion_verification_submit",
            produced=[],
        )
        self.assertTrue(submitted.ok, submitted.text)
        plan = json.loads(
            (stage_dir / "verification_plan.json").read_text(encoding="utf-8")
        )

        results = _recorded_verification_case_results(
            plan,
            cases=_verification_case_specs(plan["cases"]),
            artifacts=self.store,
            runtime_root=self.runtime_root,
            workflow_id="wf_verify",
            invocation_id=logical_session_id,
            lease_resource_key="node:node_drawing:verifier",
            fencing_token=999,
            role="verifier",
            mode="module",
            draft_kind="verification",
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, VerificationStatus.PASS)

    def test_rerun_keeps_finding_until_reviewer_withdraws_it(self) -> None:
        workspace = self._bind_workspace(
            {"repo_path": str(self.runtime_root)},
            role="verifier",
        )
        failed = self._record_lifecycle_case(workspace, command="exit 7")
        self.assertTrue(failed.ok, failed.text)
        finding = self._verification_call(
            workspace,
            ADD_FINDING_CAPABILITY,
            {
                "finding_key": "released_resource_remains_usable",
                "finding_kind": "module_defect",
                "priority": "p1",
                "summary": "Released resource remains usable.",
            },
        )
        self.assertTrue(finding.ok, finding.text)
        retry_workspace = self._advance_worker_fence(workspace)

        still_failed = self._record_lifecycle_case(retry_workspace, command="exit 8")
        self.assertTrue(still_failed.ok, still_failed.text)
        failed_status = self._verification_call(
            retry_workspace,
            "op_minion_verification_draft_status",
        )
        self.assertEqual(len(failed_status.structured["findings"]), 1)

        passed = self._record_lifecycle_case(retry_workspace, command="exit 0")
        self.assertTrue(passed.ok, passed.text)
        passed_status = self._verification_call(
            retry_workspace,
            "op_minion_verification_draft_status",
        )
        self.assertEqual(
            passed_status.structured["cases"],
            [{"name": "released resource rejects use", "status": "PASS"}],
        )
        self.assertEqual(len(passed_status.structured["findings"]), 1)
        withdrawn = self._verification_call(
            retry_workspace,
            "op_minion_verification_remove_finding",
            {
                "finding_key": "released_resource_remains_usable",
                "reason": "The rerun proves the defect is no longer present.",
            },
        )
        self.assertTrue(withdrawn.ok, withdrawn.text)

    def test_withdrawing_finding_requires_audit_reason(self) -> None:
        workspace = self._bind_workspace(
            {"repo_path": str(self.runtime_root)},
            role="verifier",
        )
        result = self._verification_call(
            workspace,
            "op_minion_verification_remove_finding",
            {"finding_key": "missing_finding", "reason": ""},
        )
        self.assertFalse(result.ok)
        self.assertIn("audit reason", result.llm_text)

    def test_case_order_uses_manager_recording_sequence_not_case_name(self) -> None:
        workspace = self._bind_workspace(
            {"repo_path": str(self.runtime_root)}, role="verifier"
        )
        historical = self._verification_call(
            workspace,
            "op_minion_verification_run_historical_regression",
            {"name": "z historical first", "command": "exit 0", "path": "src/router.py"},
        )
        adversarial = self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            {"name": "a adversarial second", "command": "exit 0", "path": "src/router.py"},
        )
        self.assertTrue(historical.ok, historical.text)
        self.assertTrue(adversarial.ok, adversarial.text)

        status = self._verification_call(
            workspace, "op_minion_verification_draft_status"
        )
        self.assertEqual(
            [item["name"] for item in status.structured["cases"]],
            ["z historical first", "a adversarial second"],
        )

    def test_finding_rejects_invalid_priority(self) -> None:
        workspace = self._bind_workspace(
            {"repo_path": str(self.runtime_root)}, role="verifier"
        )
        recorded = self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            {"name": "passing probe", "command": "exit 0", "path": "src/router.py"},
        )
        self.assertTrue(recorded.ok, recorded.text)

        finding = self._verification_call(
            workspace,
            ADD_FINDING_CAPABILITY,
            {
                "finding_key": "passing_probe_claim",
                "finding_kind": "module_defect",
                "priority": "major",
                "summary": "This must not be accepted.",
            },
        )
        self.assertFalse(finding.ok)
        self.assertIn("priority", finding.llm_text)

    def test_case_runner_persists_command_output(self) -> None:
        result = VerificationCaseRunner(self.store).run(
            VerificationCaseSpec(
                case_id="case_1",
                case_name="deterministic invariant probe",
                case_kind=VerificationCaseKind.CONTRACT_ADVERSARIAL,
                command=("sh", "-c", "printf pass-output"),
                locations=({"path": "src/module.py", "section": "Invariants"},),
            ),
            cwd=self.runtime_root,
        )
        self.assertEqual(result.status, VerificationStatus.PASS)
        self.assertEqual(self.store.read_bytes(result.stdout_ref), b"pass-output")
        self.assertTrue(self.repository.artifact_is_durable(str(result.stderr_ref["sha256"])))

    def test_durable_verification_scratch_recovers_latest_attempt_once(self) -> None:
        attempts = self.runtime_root / "attempts"
        older = attempts / "fence-1" / "review-scratch"
        newer = attempts / "fence-2" / "review-scratch"
        older.mkdir(parents=True)
        newer.mkdir(parents=True)
        (older / "probe.py").write_text("older", encoding="utf-8")
        (newer / "probe.py").write_text("newer", encoding="utf-8")
        os.utime(older.parent, (1, 1))
        os.utime(newer.parent, (2, 2))
        durable = self.runtime_root / "durable-scratch"

        _seed_durable_verification_scratch(attempts, durable)
        self.assertEqual((durable / "probe.py").read_text(encoding="utf-8"), "newer")

        latest = attempts / "fence-3" / "review-scratch"
        latest.mkdir(parents=True)
        (latest / "probe.py").write_text("must not replace durable state", encoding="utf-8")
        os.utime(latest.parent, (3, 3))
        _seed_durable_verification_scratch(attempts, durable)
        self.assertEqual((durable / "probe.py").read_text(encoding="utf-8"), "newer")

    def test_verifier_uses_semantic_names_and_manager_generates_case_keys(self) -> None:
        plan = {
            "cases": [
                {
                    "name": "released resource rejects use",
                    "case_kind": "contract_adversarial",
                    "command": ["sh", "-c", "exit 7"],
                    "expected_exit_codes": [0],
                    "locations": [{"path": "src/resource.py", "symbol": "use"}],
                    "invariants": ["Released is terminal."],
                    "description": "Exercise use after release.",
                }
            ],
            "findings": [
                {
                    "case": "released resource rejects use",
                    "finding_section": "lifecycle",
                    "summary": "Use after release succeeds.",
                    "failure_reason": "The public method returns success.",
                    "severity": "blocker",
                    "suggested_repair_boundary": ["src/resource.py"],
                }
            ],
        }
        _reject_manager_identity_fields(plan, owner="test verifier")
        cases = _verification_case_specs(plan["cases"])
        findings = _verification_findings(plan, cases)
        self.assertTrue(cases[0].case_id.startswith("case_"))
        self.assertNotEqual(cases[0].case_id, cases[0].case_name)
        self.assertEqual(cases[0].requirements, ())
        self.assertEqual(findings[0]["case_name"], cases[0].case_name)
        with self.assertRaisesRegex(ValueError, "Manager-owned identity"):
            _reject_manager_identity_fields({**plan, "finding_id": "F-1"}, owner="test verifier")

    def test_verifier_cannot_author_requirement_records(self) -> None:
        work_view = self.runtime_root / "patch-work-view.json"
        work_view.write_text(
            json.dumps(
                {
                    "requirements": {
                        "sections": {"Lifecycle": ["Released resources reject further use."]}
                    }
                }
            ),
            encoding="utf-8",
        )
        workspace = self._bind_workspace(
            {
                "repo_path": str(self.runtime_root),
                "artifact_dir": str(self.runtime_root / "patch-artifacts"),
                "artifact_stage_dir": str(self.runtime_root / "patch-stage"),
                "reference_paths": [{"name": "module_work_view", "path": str(work_view)}],
            },
            role="verifier",
        )
        failed = self._record_lifecycle_case(workspace, command="exit 7")
        self.assertTrue(failed.ok, failed.text)
        finding = self._verification_call(
            workspace,
            ADD_FINDING_CAPABILITY,
            {
                "finding_key": "reset_semantics_missing",
                "finding_kind": "contract_defect",
                "priority": "p1",
                "summary": "Reset semantics are missing from the product contract.",
                "locations": [{"scope": "workspace", "file": "include/router.h", "line": 1, "symbol": "reset"}],
            },
        )
        self.assertTrue(finding.ok, finding.text)
        proposal = self._verification_call(
            workspace,
            "op_minion_verification_propose_requirement_patch",
            {
                "patch_kind": "derived_constraint",
                "section": "Lifecycle",
                "requirement": "Reset preserves configured precedence.",
                "strength": "hard",
                "reason": "A reproduced public consumer observes reordered routes.",
                "affected_modules": ["router"],
                "contract_path": "include/router.h",
                "contract_symbol": "reset",
            },
        )
        self.assertFalse(proposal.ok)
        self.assertIn("unknown verification authoring capability", proposal.llm_text)
        self.assertNotIn(
            "op_minion_verification_propose_requirement_patch",
            VERIFICATION_BUILDER_TOOL_SPECS,
        )

    def test_standalone_review_markdown_exposes_semantics_not_manager_identity(self) -> None:
        markdown = _compile_standalone_review_markdown(
            {
                "status": "FAIL",
                "reviewer_summary": "The released-state contract is violated.",
                "report_ref": {"sha256": "hidden-report-digest"},
                "findings": [
                    {
                        "finding_key": "use_after_release_succeeds",
                        "finding_kind": "contract_defect",
                        "priority": "p0",
                        "summary": "Use after release succeeds.",
                        "locations": [
                            {
                                "scope": "workspace",
                                "file": "src/resource.py",
                                "line": 42,
                                "symbol": "use",
                            }
                        ],
                    }
                ],
                "cases": [
                    {
                        "name": "released resource rejects use",
                        "status": "FAIL",
                        "command": ["pytest", "tests/test_resource.py"],
                        "stdout_ref": {"sha256": "hidden-stdout-digest"},
                    }
                ],
                "test_gaps": ["Platform shutdown could not be simulated."],
            }
        )
        self.assertIn("Use after release succeeds.", markdown)
        self.assertIn("use_after_release_succeeds", markdown)
        self.assertIn("src/resource.py:42::use", markdown)
        self.assertIn("released resource rejects use", markdown)
        self.assertNotIn("hidden-report-digest", markdown)
        self.assertNotIn("hidden-stdout-digest", markdown)
        self.assertNotIn("sha256", markdown)

    def test_coder_defect_report_is_bound_to_module_and_requirement_text(self) -> None:
        report = {
            "checklist": {
                "plan": [],
            },
            "files_changed": [],
            "status": "architecture_defect",
            "summary": "The frozen contract cannot represent native state.",
            "affected_module": "font_backend",
            "locations": [{"path": "include/ohos_font.h", "symbol": "OHOSFont"}],
            "requirements": [
                {
                    "section": "Font rendering",
                    "requirement": "Support native font creation and rendering.",
                }
            ],
        }
        work_view = {
            "module_name": "font_backend",
            "requirements": {
                "sections": {
                    "Font rendering": ["Support native font creation and rendering."]
                }
            }
        }
        _validate_skeleton_coder_report(
            report,
            expected_module="font_backend",
            work_view=work_view,
        )
        with self.assertRaisesRegex(ValueError, "bound module"):
            _validate_skeleton_coder_report(
                {**report, "affected_module": "drawing_backend"},
                expected_module="font_backend",
                work_view=work_view,
            )
        _validate_skeleton_coder_report(
            {
                **report,
                "requirements": [
                    {"section": "Font rendering", "requirement": "A natural-language defect citation."}
                ],
            },
            expected_module="font_backend",
            work_view=work_view,
        )

    def test_case_order_is_blocking_only_for_historical_repair_bills(self) -> None:
        node = self._reviewing_node("node_case_order")
        candidate = self.store.put_json(
            {"candidate_digest": "case-order"}, artifact_type="CandidateSnapshotArtifact"
        )
        runner = VerificationCaseRunner(self.store)
        adversarial = runner.run(
            VerificationCaseSpec(
                case_id="case_adversarial",
                case_name="adversarial",
                case_kind=VerificationCaseKind.CONTRACT_ADVERSARIAL,
                command=("sh", "-c", "exit 0"),
                locations=({"path": "src/router.py"},),
            ),
            cwd=self.runtime_root,
        )
        historical = runner.run(
            VerificationCaseSpec(
                case_id="case_historical",
                case_name="historical",
                case_kind=VerificationCaseKind.HISTORICAL_REGRESSION,
                command=("sh", "-c", "exit 0"),
                locations=({"path": "src/router.py"},),
            ),
            cwd=self.runtime_root,
        )
        compile_result = runner.run(
            VerificationCaseSpec(
                case_id="case_compile",
                case_name="compile",
                case_kind=VerificationCaseKind.COMPILE,
                command=("sh", "-c", "exit 0"),
                locations=({"path": "src/router.py"},),
            ),
            cwd=self.runtime_root,
        )

        _, status = self.verification.publish_report(
            node=node,
            candidate_ref=candidate.to_dict(),
            case_results=[adversarial, historical],
            reviewer_summary="There is no historical RepairBill in this cycle.",
        )
        self.assertEqual(status, VerificationStatus.PASS)

        historical_node = AggregateSnapshot(
            aggregate_type=node.aggregate_type,
            aggregate_id=node.aggregate_id,
            workflow_id=node.workflow_id,
            state=node.state,
            version=node.version,
            payload={**node.payload, "historical_repair_bill_refs": [{"sha256": "bill"}]},
            created_at=node.created_at,
            updated_at=node.updated_at,
        )
        with self.assertRaisesRegex(ValueError, "historical failures before"):
            self.verification.publish_report(
                node=historical_node,
                candidate_ref=candidate.to_dict(),
                case_results=[adversarial, historical],
                reviewer_summary="The historical regression ran too late.",
            )
        _, status = self.verification.publish_report(
            node=historical_node,
            candidate_ref=candidate.to_dict(),
            case_results=[historical, adversarial],
            reviewer_summary="Historical regression ran first.",
        )
        self.assertEqual(status, VerificationStatus.PASS)
        _, status = self.verification.publish_report(
            node=historical_node,
            candidate_ref=candidate.to_dict(),
            case_results=[compile_result, historical, adversarial],
            reviewer_summary="A compile smoke check may precede the historical regression.",
        )
        self.assertEqual(status, VerificationStatus.PASS)

    def test_repair_bill_has_stable_fingerprint_and_regression_obligation(self) -> None:
        node = self._reviewing_node("node_repair")
        candidate = self.store.put_json({"candidate_digest": "c1"}, artifact_type="CandidateSnapshotArtifact")
        output = self.store.put_bytes(b"failure", artifact_type="VerificationStdoutArtifact")
        case_result = VerificationCaseRunner(self.store).run(
            VerificationCaseSpec(
                case_id="case_fail",
                case_name="released resource rejects use",
                case_kind=VerificationCaseKind.CONTRACT_ADVERSARIAL,
                command=("sh", "-c", "exit 7"),
                requirements=(
                    {
                        "section": "Lifecycle",
                        "requirement": "Released resources reject further use.",
                    },
                ),
                locations=({"path": "src/module/core.py", "symbol": "use_resource"},),
                invariants=("A released resource cannot return to ready.",),
            ),
            cwd=self.runtime_root,
        )
        report_ref, status = self.verification.publish_report(
            node=node,
            candidate_ref=candidate.to_dict(),
            case_results=[case_result],
            reviewer_summary="Invariant can be broken.",
        )
        self.assertEqual(status, VerificationStatus.FAIL)
        repair_ref, fingerprint = self.verification.publish_repair_bill(
            node=node,
            candidate_digest="c1",
            verification_ref=report_ref,
            defect_kind=DefectKind.MODULE,
            severity="high",
            minimal_reproducer_ref=output.to_dict(),
            test_artifact_ref=output.to_dict(),
            expected={"returncode": 0},
            actual={"returncode": 7},
            suggested_repair_boundary=["src/module/**"],
            finding_section="invariant",
            finding_summary="Invariant can be broken",
            failure_reason="case_fail exits 7",
            case_name="released resource rejects use",
            requirements=[
                {
                    "section": "Lifecycle",
                    "requirement": "Released resources reject further use.",
                }
            ],
            locations=[{"path": "src/module/core.py", "symbol": "use_resource"}],
            invariants=["A released resource cannot return to ready."],
        )
        repair = self.store.read_json(repair_ref)
        self.assertEqual(repair["finding_fingerprint"], fingerprint)
        self.assertIn("regression_test_obligation", repair)
        self.assertEqual(repair["finding_section"], "invariant")
        self.assertNotIn("finding_id", repair)
        self.assertNotIn("affected_refs", repair)
        self.assertEqual(repair["locations"], [{"path": "src/module/core.py", "symbol": "use_resource"}])
        self.assertEqual(
            fingerprint,
            finding_fingerprint(
                defect_kind=DefectKind.MODULE,
                contract_refs=[
                    "requirement:Lifecycle:Released resources reject further use.",
                    "location:src/module/core.py:use_resource:",
                    "invariant:A released resource cannot return to ready.",
                ],
                reproducer_hash=hashlib.sha256(
                    json.dumps(
                        {"semantic_case_names": ["released resource rejects use"]},
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
                expected={"returncode": 0},
                actual={"returncode": 7},
            ),
        )
        semantic = repair_bill_semantic_view(self.store, repair_ref)
        encoded = str(semantic)
        self.assertEqual(semantic["case_name"], "released resource rejects use")
        self.assertIn("src/module/core.py", encoded)
        for forbidden in ("workflow_id", "node_run_id", "candidate_digest", "sha256", "_ref"):
            self.assertNotIn(forbidden, encoded)

    def test_repair_bill_can_batch_all_dominant_findings(self) -> None:
        node = self._reviewing_node("node_batch_repair")
        report = self.store.put_json(
            {"status": "FAIL"}, artifact_type="VerificationArtifact"
        )
        reproducer = self.store.put_json(
            {"cases": [{"name": "integer"}, {"name": "finite"}]},
            artifact_type="VerificationReproducerSetArtifact",
        )
        findings = [
            {
                "case_name": "integer",
                "finding_section": "interface",
                "summary": "Fractional priority accepted",
                "failure_reason": "3.5 is accepted",
                "requirements": [
                    {"section": "Validation", "requirement": "Priority must be an integer."}
                ],
                "locations": [{"path": "src/router.py", "symbol": "Rule"}],
                "invariants": [],
                "severity": "major",
                "defect_kind": "contract_defect",
            },
            {
                "case_name": "finite",
                "finding_section": "interface",
                "summary": "Infinite priority accepted",
                "failure_reason": "Infinity is accepted",
                "requirements": [
                    {"section": "Validation", "requirement": "Priority must be finite."}
                ],
                "locations": [{"path": "src/router.py", "symbol": "Rule"}],
                "invariants": [],
                "severity": "major",
                "defect_kind": "contract_defect",
            },
        ]
        repair_ref, fingerprint = self.verification.publish_repair_bill(
            node=node,
            candidate_digest="candidate",
            verification_ref=report,
            defect_kind=DefectKind.CONTRACT,
            severity="major",
            minimal_reproducer_ref=reproducer.to_dict(),
            test_artifact_ref=reproducer.to_dict(),
            expected={"cases": ["integer", "finite"]},
            actual={"cases": ["accepted", "accepted"]},
            suggested_repair_boundary=["src/router.py"],
            finding_section="interface",
            finding_summary=findings[0]["summary"],
            failure_reason=findings[0]["failure_reason"],
            case_name=findings[0]["case_name"],
            requirements=findings[0]["requirements"],
            locations=findings[0]["locations"],
            findings=findings,
        )
        repair = self.store.read_json(repair_ref)
        self.assertEqual(repair["finding_fingerprint"], fingerprint)
        self.assertEqual(len(repair["findings"]), 2)
        semantic = repair_bill_semantic_view(self.store, repair_ref)
        self.assertEqual(len(semantic["findings"]), 2)

    def test_fail_routing_excludes_unknown_findings_from_repair_bill_scope(self) -> None:
        results = [
            VerificationCaseResult(
                case_id="case_fail",
                case_kind=VerificationCaseKind.CONTRACT_ADVERSARIAL,
                status=VerificationStatus.FAIL,
                command=("false",),
                exit_code=1,
                stdout_ref={},
                stderr_ref={},
                environment={},
                summary="contract failed",
            ),
            VerificationCaseResult(
                case_id="case_unknown",
                case_kind=VerificationCaseKind.PLATFORM_ASSUMPTION,
                status=VerificationStatus.UNKNOWN,
                command=(),
                exit_code=None,
                stdout_ref={},
                stderr_ref={},
                environment={},
                summary="device unavailable",
            ),
        ]
        findings = [
            {
                "case_id": "case_fail",
                "defect_kind": "contract_defect",
                "summary": "public contract is invalid",
            },
            {
                "case_id": "case_unknown",
                "defect_kind": "architecture_defect",
                "summary": "OHOS device is unavailable",
            },
        ]

        routed = _routable_verification_findings(
            findings,
            results,
            status=VerificationStatus.FAIL,
        )

        self.assertEqual([item["case_id"] for item in routed], ["case_fail"])
        self.assertEqual(
            dominant_verification_defect_kind(routed),
            "contract_defect",
        )

    def test_unknown_case_does_not_synthesize_a_module_finding(self) -> None:
        cases = [
            VerificationCaseSpec(
                case_id="case_unknown",
                case_name="lsp unavailable",
                case_kind=VerificationCaseKind.PLATFORM_ASSUMPTION,
                command=("<unavailable>", "lsp"),
                description="The workspace has no configured LSP server.",
                locations=({"path": "src/router.py"},),
            ),
            VerificationCaseSpec(
                case_id="case_fail",
                case_name="invalid route",
                case_kind=VerificationCaseKind.CONTRACT_ADVERSARIAL,
                command=("false",),
                description="Invalid input violates the route contract.",
                locations=({"path": "src/router.py"},),
            ),
        ]
        results = [
            VerificationCaseResult(
                case_id="case_unknown",
                case_kind=VerificationCaseKind.PLATFORM_ASSUMPTION,
                status=VerificationStatus.UNKNOWN,
                command=("<unavailable>", "lsp"),
                exit_code=None,
                stdout_ref={},
                stderr_ref={},
                environment={"runner": "unavailable"},
                summary="LSP is unavailable.",
            ),
            VerificationCaseResult(
                case_id="case_fail",
                case_kind=VerificationCaseKind.CONTRACT_ADVERSARIAL,
                status=VerificationStatus.FAIL,
                command=("false",),
                exit_code=1,
                stdout_ref={},
                stderr_ref={},
                environment={},
                summary="The route is invalid.",
            ),
        ]

        findings = _confirmed_verification_findings([], cases, results)

        self.assertEqual([item["case_id"] for item in findings], ["case_fail"])
        self.assertEqual(findings[0]["severity"], "major")

    def test_module_defect_on_module_node_does_not_resolve_current_module_as_dependency(self) -> None:
        node = self._reviewing_node("node_local_module_defect")

        dependency_node_id, module_node_id = _resolve_verification_defect_targets(
            self.repository,
            node,
            plan={"affected_module": str(node.payload.get("module_name") or "router")},
            status=VerificationStatus.FAIL,
            defect_kind=DefectKind.MODULE,
            scenario_mode=False,
        )

        self.assertEqual(dependency_node_id, "")
        self.assertEqual(module_node_id, "")

    def test_scenario_repair_targets_do_not_replace_dependency_graph(self) -> None:
        node_id = "node_scenario_repair_route"
        dependency_ids = ("node_encoder", "node_streaming_decoder")
        scenario_fingerprint = "scenario-repair-fingerprint"
        contract = self.store.put_json(
            {"verification_name": "streaming_round_trip"},
            artifact_type="VerificationScenarioContractArtifact",
        )
        union = self.store.put_json(
            {"scenario": "streaming_round_trip"},
            artifact_type="CandidateUnionArtifact",
        )
        actions = [
            (
                "CREATE_NODE_RUN",
                {
                    "unit_contract_ref": contract.to_dict(),
                    "epoch_id": "epoch",
                    "node_kind": "verification",
                    "dependency_node_ids": list(dependency_ids),
                },
            ),
            (
                "VERIFICATION_DEPENDENCIES_ACCEPTED",
                {
                    "accepted_dependency_node_ids": list(dependency_ids),
                    "epoch_frozen": False,
                },
            ),
            (
                "VERIFICATION_PREPARED",
                {
                    "scenario_fingerprint": scenario_fingerprint,
                    "scenario_candidate_union_ref": union.to_dict(),
                    "scenario_commit_sha": "scenario-commit",
                    "verification_workspace_fingerprint": "scenario-tree",
                },
            ),
            ("START_SCENARIO_VERIFICATION", {"fencing_token": 1}),
            (
                "SUBMIT_SEMANTIC_VERIFICATION",
                {"pending_verification_ref": {"sha256": "pending-scenario"}},
            ),
            (
                "VERIFIER_QUIESCED",
                {
                    "fencing_token": 1,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "verification-tree",
                },
            ),
        ]
        for action_type, payload in actions:
            snapshot = self.repository.read_snapshot(
                AggregateType.DAG_NODE_RUN,
                node_id,
            )
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id="wf_verify",
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node_id,
                    actor="test",
                    expected_version=snapshot.version if snapshot else 0,
                    idempotency_key=f"{node_id}:{action_type}",
                    payload=payload,
                )
            )
        node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
        assert node is not None
        verification_ref = self.store.put_json(
            {"status": "FAIL", "scenario_fingerprint": scenario_fingerprint},
            artifact_type="VerificationArtifact",
        )
        repair_ref = self.store.put_json(
            {"finding": "decoder corpus is broken"},
            artifact_type="RepairPacketArtifact",
        )

        failed = self.verification.submit_verdict(
            node=node,
            verification_ref=verification_ref,
            status=VerificationStatus.FAIL,
            actor="verifier",
            repair_bill_ref=repair_ref,
            finding_fingerprint_value="decoder-corpus-finding",
            defect_kind=DefectKind.MODULE,
            module_node_ids=("node_streaming_decoder",),
            scenario_fingerprint=scenario_fingerprint,
        ).snapshot

        self.assertEqual(failed.state, "STALE")
        self.assertEqual(
            failed.payload["dependency_node_ids"],
            list(dependency_ids),
        )
        self.assertEqual(
            failed.payload["repair_target_node_ids"],
            ["node_streaming_decoder"],
        )
        self.assertEqual(
            failed.payload["repair_target_node_id"],
            "node_streaming_decoder",
        )

    def test_scenario_verification_defect_reopens_verifier_not_coder(self) -> None:
        node_id = "node_scenario_verifier_repair"
        scenario_fingerprint = "scenario-verifier-repair-fingerprint"
        contract = self.store.put_json(
            {"verification_name": "streaming_round_trip"},
            artifact_type="VerificationScenarioContractArtifact",
        )
        union = self.store.put_json(
            {"scenario": "streaming_round_trip"},
            artifact_type="CandidateUnionArtifact",
        )
        actions = [
            (
                "CREATE_NODE_RUN",
                {
                    "unit_contract_ref": contract.to_dict(),
                    "epoch_id": "epoch",
                    "node_kind": "verification",
                    "dependency_node_ids": ["node_streaming_decoder"],
                },
            ),
            (
                "VERIFICATION_DEPENDENCIES_ACCEPTED",
                {
                    "accepted_dependency_node_ids": ["node_streaming_decoder"],
                    "epoch_frozen": False,
                },
            ),
            (
                "VERIFICATION_PREPARED",
                {
                    "scenario_fingerprint": scenario_fingerprint,
                    "scenario_candidate_union_ref": union.to_dict(),
                    "scenario_commit_sha": "scenario-commit",
                    "verification_workspace_fingerprint": "scenario-tree",
                },
            ),
            ("START_SCENARIO_VERIFICATION", {"fencing_token": 1}),
            (
                "SUBMIT_SEMANTIC_VERIFICATION",
                {"pending_verification_ref": {"sha256": "pending-verification"}},
            ),
            (
                "VERIFIER_QUIESCED",
                {
                    "fencing_token": 1,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "verification-tree",
                },
            ),
        ]
        for action_type, payload in actions:
            snapshot = self.repository.read_snapshot(
                AggregateType.DAG_NODE_RUN,
                node_id,
            )
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id="wf_verify",
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node_id,
                    actor="test",
                    expected_version=snapshot.version if snapshot else 0,
                    idempotency_key=f"{node_id}:{action_type}",
                    payload=payload,
                )
            )
        node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
        assert node is not None
        verification_ref = self.store.put_json(
            {"status": "FAIL", "scenario_fingerprint": scenario_fingerprint},
            artifact_type="VerificationArtifact",
        )
        repair_ref = self.store.put_json(
            {"finding": "verifier corpus is broken"},
            artifact_type="RepairPacketArtifact",
        )

        verdict = self.verification.submit_verdict(
            node=node,
            verification_ref=verification_ref,
            status=VerificationStatus.FAIL,
            actor="verifier",
            repair_bill_ref=repair_ref,
            finding_fingerprint_value="verifier-corpus-finding",
            defect_kind=DefectKind.VERIFICATION,
            module_node_ids=("node_streaming_decoder",),
            scenario_fingerprint=scenario_fingerprint,
        )
        failed = verdict.snapshot

        self.assertEqual(failed.state, "STALE")
        pending_effects = self.repository.claim_outbox(
            "verification-defect-test",
            limit=100,
        )
        self.assertIn(
            "reopen_verifier_and_stale_descendants",
            {str(item.get("effect_type") or "") for item in pending_effects},
        )

    def test_manager_resolves_scenario_repair_owner_from_immutable_path_policy(self) -> None:
        contract = self.store.put_json(
            {"contract": "framepipe"},
            artifact_type="UnitContractArtifact",
        )
        for node_id, module_name, source_path in (
            ("node_encoder", "encoder", "src/encoder.cpp"),
            ("node_streaming_decoder", "streaming_decoder", "src/decoder.cpp"),
        ):
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="CREATE_NODE_RUN",
                    workflow_id="wf_verify",
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node_id,
                    actor="test",
                    expected_version=0,
                    idempotency_key=f"create:{node_id}",
                    payload={
                        "epoch_id": "epoch",
                        "node_kind": "unit",
                        "module_name": module_name,
                        "unit_id": module_name,
                        "unit_contract_ref": contract.to_dict(),
                        "dependency_node_ids": [],
                        "contract_dependency_node_ids": [],
                        "path_policy": {
                            "contract_paths": [
                                f"include/framepipe/{module_name}.hpp"
                            ],
                            "implementation_scopes": [
                                {"kind": "file", "path": source_path}
                            ],
                            "developer_tests": {
                                "kind": "directory",
                                "path": f"tests/{module_name}/developer",
                            },
                            "verification_corpus": {
                                "kind": "directory",
                                "path": f"tests/{module_name}/verification",
                            },
                        },
                    },
                )
            )
        scenario_id = "node_streaming_scenario"
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_NODE_RUN",
                workflow_id="wf_verify",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=scenario_id,
                actor="test",
                expected_version=0,
                idempotency_key=f"create:{scenario_id}",
                payload={
                    "epoch_id": "epoch",
                    "node_kind": "verification",
                    "module_name": "streaming_round_trip",
                    "unit_contract_ref": contract.to_dict(),
                    "dependency_node_ids": [
                        "node_encoder",
                        "node_streaming_decoder",
                    ],
                },
            )
        )
        scenario = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            scenario_id,
        )
        assert scenario is not None

        owners = _verification_repair_path_owners(self.repository, scenario)
        corpus_owners = _verification_corpus_path_owners(
            self.repository,
            scenario,
        )
        targets = infer_repair_target_modules(
            [
                {
                    "finding_key": "decoder_developer_corpus_is_broken",
                    "locations": [
                        {
                            "scope": "workspace",
                            "file": "tests/streaming_decoder/developer/test_decoder.cpp",
                            "line": 17,
                        }
                    ],
                }
            ],
            owners,
        )

        self.assertEqual(targets, ["streaming_decoder"])
        with self.assertRaisesRegex(ValueError, "cannot derive a repair owner"):
            infer_repair_target_modules(
                [
                    {
                        "finding_key": "bad_verifier_double",
                        "locations": [
                            {
                                "scope": "workspace",
                                "file": "tests/streaming_decoder/verification/test_double.cpp",
                                "line": 8,
                            }
                        ],
                    }
                ],
                owners,
            )
        self.assertEqual(
            infer_repair_target_modules(
                [
                    {
                        "finding_key": "bad_verifier_double",
                        "locations": [
                            {
                                "scope": "workspace",
                                "file": "tests/streaming_decoder/verification/test_double.cpp",
                                "line": 8,
                            }
                        ],
                    }
                ],
                corpus_owners,
            ),
            ["streaming_decoder"],
        )
        self.assertEqual(
            _resolve_dependency_node_id(
                self.repository,
                scenario,
                dependency_module=targets[0],
            ),
            "node_streaming_decoder",
        )

    def test_repair_effect_reopens_only_explicit_manager_targets(self) -> None:
        repair_ref = self.store.put_json(
            {"finding": "decoder corpus is broken"},
            artifact_type="RepairPacketArtifact",
        )
        node = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node_streaming_scenario",
            workflow_id="wf_verify",
            state="STALE",
            version=7,
            created_at="2026-07-23T00:00:00+00:00",
            updated_at="2026-07-23T00:00:00+00:00",
            payload={
                "epoch_id": "epoch",
                "dependency_node_ids": [
                    "node_encoder",
                    "node_streaming_decoder",
                    "node_cli",
                ],
                "repair_target_node_id": "node_streaming_decoder",
                "repair_target_node_ids": ["node_streaming_decoder"],
                "repair_bill_ref": repair_ref.to_dict(),
            },
        )
        processor = MinionV2OutboxProcessor(
            MinionV2WorkflowService(self.runtime_root)
        )

        with (
            patch.object(processor, "_effect_snapshot", return_value=node),
            patch.object(
                DefectPropagationService,
                "propagate_dependency_defect",
                return_value=(),
            ) as propagate,
        ):
            asyncio.run(
                processor._execute_mechanical(
                    {
                        "effect_type": "reopen_dependency_and_stale_descendants",
                        "effect_key": "repair-route",
                    }
                )
            )

        self.assertEqual(propagate.call_count, 1)
        self.assertEqual(
            propagate.call_args.kwargs["dependency_node_id"],
            "node_streaming_decoder",
        )
        self.assertEqual(
            propagate.call_args.kwargs["reopen_action"],
            "REOPEN_DEPENDENCY",
        )

        with (
            patch.object(processor, "_effect_snapshot", return_value=node),
            patch.object(
                DefectPropagationService,
                "propagate_dependency_defect",
                return_value=(),
            ) as reopen_verifier,
        ):
            asyncio.run(
                processor._execute_mechanical(
                    {
                        "effect_type": "reopen_verifier_and_stale_descendants",
                        "effect_key": "verifier-repair-route",
                    }
                )
            )
        self.assertEqual(
            reopen_verifier.call_args.kwargs["reopen_action"],
            "REOPEN_VERIFICATION",
        )

    def test_verification_defect_reopens_accepted_module_at_verifier_queue(self) -> None:
        node = self._reviewing_node("node_reopen_verifier")
        verification_ref = self.store.put_json(
            {"status": "PASS"},
            artifact_type="VerificationArtifact",
        )
        accepted = self.verification.submit_verdict(
            node=node,
            verification_ref=verification_ref,
            status=VerificationStatus.PASS,
            actor="verifier",
        ).snapshot
        self.assertEqual(accepted.state, "ACCEPTED")
        repair_ref = self.store.put_json(
            {"finding": "incorrect verifier corpus"},
            artifact_type="RepairPacketArtifact",
        )

        DefectPropagationService(self.repository).propagate_dependency_defect(
            workflow_id=accepted.workflow_id,
            epoch_id=str(accepted.payload.get("epoch_id") or ""),
            dependency_node_id=accepted.aggregate_id,
            repair_bill_ref=repair_ref,
            reopen_action="REOPEN_VERIFICATION",
        )

        reopened = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            accepted.aggregate_id,
        )
        assert reopened is not None
        self.assertEqual(reopened.state, "REVIEW_QUEUED")
        self.assertEqual(
            reopened.payload["repair_bill_ref"],
            repair_ref.to_dict(),
        )

    def test_unknown_hard_semantics_requires_human_waiver(self) -> None:
        assumption = self.store.put_json({"owner": "platform", "verification_plan": "device CI"}, artifact_type="AssumptionLedgerArtifact")
        waiver = self.store.put_json({"actor": "nathan"}, artifact_type="HumanWaiverArtifact")
        self.assertFalse(
            UnknownPolicy(
                architecture_allows_platform_unknown=True,
                assumption_ref=assumption.to_dict(),
                hard_or_core_semantics=True,
            ).allows()
        )
        self.assertTrue(
            UnknownPolicy(
                architecture_allows_platform_unknown=True,
                assumption_ref=assumption.to_dict(),
                hard_or_core_semantics=True,
                human_waiver_ref=waiver.to_dict(),
            ).allows()
        )

    def test_three_identical_failures_without_tree_change_require_triage(self) -> None:
        history = [
            {"finding_fingerprint": "same", "candidate_tree_hash": "same-tree"},
            {"finding_fingerprint": "same", "candidate_tree_hash": "same-tree"},
            {"finding_fingerprint": "same", "candidate_tree_hash": "same-tree"},
        ]
        self.assertTrue(no_progress_detected(history))
        self.assertFalse(no_progress_detected([{**item, "candidate_tree_hash": str(index)} for index, item in enumerate(history)]))

    def test_candidate_reuse_fingerprint_requires_all_inputs(self) -> None:
        values = {
            "unit_contract_hash": "1",
            "relevant_requirements_hash": "2",
            "relevant_evidence_hash": "3",
            "global_constraint_hash": "4",
            "owned_area_hash": "5",
            "dependency_set_hash": "6",
            "dependency_interface_hash": "7",
            "dependency_output_hash": "8",
            "integration_contract_subset_hash": "9",
            "environment_policy_hash": "10",
        }
        first = candidate_reuse_fingerprint(**values)
        second = candidate_reuse_fingerprint(**values)
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            candidate_reuse_fingerprint(**{**values, "relevant_evidence_hash": ""})

    def test_verdicts_drive_pass_fail_unknown_and_not_applicable_states(self) -> None:
        verification_ref = self.store.put_json({"status": "test"}, artifact_type="VerificationArtifact")
        repair_ref = self.store.put_json({"finding": "test"}, artifact_type="RepairBillArtifact")
        assumption_ref = self.store.put_json({"owner": "platform"}, artifact_type="AssumptionLedgerArtifact")
        cases = (
            ("pass", VerificationStatus.PASS, None, None, "ACCEPTED"),
            ("not_applicable", VerificationStatus.NOT_APPLICABLE, None, None, "ACCEPTED"),
            (
                "unknown_allowed",
                VerificationStatus.UNKNOWN,
                UnknownPolicy(True, assumption_ref.to_dict(), False),
                None,
                "ACCEPTED",
            ),
            ("fail", VerificationStatus.FAIL, None, repair_ref, "REPAIR_QUEUED"),
            ("unknown_blocked", VerificationStatus.UNKNOWN, None, None, "TRIAGE_REQUIRED"),
        )
        for name, status, policy, repair, expected_state in cases:
            with self.subTest(status=name):
                node = self._reviewing_node(f"node_verdict_{name}")
                result = self.verification.submit_verdict(
                    node=node,
                    verification_ref=verification_ref,
                    status=status,
                    actor="reviewer",
                    unknown_policy=policy,
                    repair_bill_ref=repair,
                    finding_fingerprint_value="fingerprint" if repair else "",
                    candidate_tree_hash="tree" if repair else "",
                )
                self.assertEqual(result.snapshot.state, expected_state)
                if name == "unknown_blocked":
                    self.assertEqual(
                        result.snapshot.payload["blocker"],
                        {"kind": "blocking_unknown"},
                    )
                    self.assertNotIn("repair_bill_ref", result.snapshot.payload)

    def test_contract_defect_routes_finding_without_mutating_task_truth(self) -> None:
        node = self._reviewing_node("node_contract_defect")
        verification_ref = self.store.put_json(
            {"status": "FAIL"}, artifact_type="VerificationArtifact"
        )
        repair_ref = self.store.put_json(
            {"finding": "contract"}, artifact_type="RepairBillArtifact"
        )
        result = self.verification.submit_verdict(
            node=node,
            verification_ref=verification_ref,
            status=VerificationStatus.FAIL,
            actor="verifier",
            repair_bill_ref=repair_ref,
            finding_fingerprint_value="contract-fingerprint",
            candidate_tree_hash="candidate-tree",
            defect_kind=DefectKind.CONTRACT,
        )
        self.assertEqual(result.snapshot.state, "STALE")
        self.assertEqual(result.snapshot.payload["repair_bill_ref"], repair_ref.to_dict())

    def test_requirement_finding_replan_keeps_the_accepted_task_ledger(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        base_requirements_ref = service.task_ledger.publish(
            title="Router",
            task_spec={
                "objective": "Route matching must be deterministic.",
                "reset": {"precedence": "unspecified"},
            },
            actor="test",
            source_channel="test",
        )
        repair_ref = self.store.put_json(
            {"summary": "Reset changes route precedence."}, artifact_type="RepairBillArtifact"
        )
        request_ref = self.store.put_json(
            {"requirements_ref": base_requirements_ref.to_dict()},
            artifact_type="WorkflowRequestArtifact",
        )
        manifest_ref = self.store.put_json(
            {"requirements_ref": base_requirements_ref.to_dict()},
            artifact_type="ArchitectureSkeletonArtifact",
        )
        topology_ref = self.store.put_json({}, artifact_type="SkeletonTopologyArtifact")
        contract_ref = self.store.put_json({}, artifact_type="ArchitectureSkeletonModuleContractArtifact")
        workflow_id = "wf_task_ledger_replan"
        epoch_id = "epoch_task_ledger_replan"
        node_id = "node_task_ledger_replan"
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="test",
                expected_version=0,
                idempotency_key=f"{workflow_id}:create",
                payload={"request_ref": request_ref.to_dict()},
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="test",
                expected_version=1,
                idempotency_key=f"{workflow_id}:start",
            )
        )
        for action_type, payload in (
            (
                "CREATE_EXECUTION_EPOCH",
                {
                    "architecture_manifest_ref": manifest_ref.to_dict(),
                    "topology_ref": topology_ref.to_dict(),
                },
            ),
            ("START_EXECUTION", {}),
            ("NODES_COMPILED", {"node_ids": [node_id]}),
        ):
            snapshot = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch_id,
                    actor="test",
                    expected_version=snapshot.version if snapshot else 0,
                    idempotency_key=f"{epoch_id}:{action_type}",
                    payload=payload,
                )
            )
        node_actions = [
            (
                "CREATE_NODE_RUN",
                {
                    "unit_contract_ref": contract_ref.to_dict(),
                    "epoch_id": epoch_id,
                    "architecture_manifest_ref": manifest_ref.to_dict(),
                    "dependency_node_ids": [],
                },
            ),
            ("DEPENDENCIES_ACCEPTED", {"accepted_dependency_node_ids": [], "epoch_frozen": False}),
            ("START_PRODUCING", {"fencing_token": 1}),
            ("SUBMIT_CANDIDATE", {"fencing_token": 1}),
            (
                "QUIESCE_COMPLETED",
                {
                    "fencing_token": 1,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "tree",
                },
            ),
            (
                "CANDIDATE_SNAPSHOTTED",
                {
                    "candidate_ref": contract_ref.to_dict(),
                    "candidate_digest": "candidate",
                    "workspace_fingerprint": "tree",
                },
            ),
            ("START_REVIEW", {"fencing_token": 2}),
            (
                "SUBMIT_SEMANTIC_VERIFICATION",
                {"pending_verification_ref": {"sha256": "pending-task-ledger-replan"}},
            ),
            (
                "VERIFIER_QUIESCED",
                {
                    "fencing_token": 2,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "verification-tree",
                },
            ),
            (
                "CONTRACT_DEFECT",
                {"repair_bill_ref": repair_ref.to_dict()},
            ),
        ]
        for action_type, payload in node_actions:
            snapshot = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node_id,
                    actor="test",
                    expected_version=snapshot.version if snapshot else 0,
                    idempotency_key=f"{node_id}:{action_type}",
                    payload=payload,
                )
            )

        processor = MinionV2OutboxProcessor(service)
        processor._request_epoch_replan(
            {
                "effect_key": "task-ledger-replan",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": node_id,
            }
        )
        processor._freeze_epoch_for_replan(
            {
                "effect_key": "task-ledger-freeze",
                "aggregate_type": AggregateType.EXECUTION_EPOCH.value,
                "aggregate_id": epoch_id,
            }
        )
        processor._create_replan_revision(
            {
                "effect_key": "task-ledger-create-revision",
                "aggregate_type": AggregateType.EXECUTION_EPOCH.value,
                "aggregate_id": epoch_id,
            }
        )
        revisions = [
            item
            for item in self.repository.list_workflow_snapshots(workflow_id)
            if item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
        ]
        self.assertEqual(len(revisions), 1)
        revision = revisions[0]
        self.assertEqual(revision.state, "ARCHITECT_QUEUED")
        self.assertEqual(revision.payload["requirements_ref"], base_requirements_ref.to_dict())
        self.assertIn("replan_finding_batch_ref", revision.payload)
        self.assertEqual(revision.payload["base_architecture_manifest_ref"], manifest_ref.to_dict())

    def test_accepted_unit_publishes_reviewable_memory_candidate(self) -> None:
        node = self._reviewing_node("node_memory")
        verification_ref = self.store.put_json({"status": "PASS"}, artifact_type="VerificationArtifact")
        accepted = self.verification.submit_verdict(
            node=node,
            verification_ref=verification_ref,
            status=VerificationStatus.PASS,
            actor="verifier",
        ).snapshot
        service = MinionV2WorkflowService(self.runtime_root)
        processor = MinionV2OutboxProcessor(service)

        result = processor._publish_accepted_memory_candidate(
            {
                "effect_key": "accepted-memory",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": accepted.aggregate_id,
            }
        )

        updated = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, accepted.aggregate_id)
        memory_ref = updated.payload["memory_candidate_ref"]
        memory = self.store.read_json(memory_ref)
        self.assertEqual(memory["review_status"], "pending_human_review")
        self.assertEqual(memory["verification_artifact_ref"]["sha256"], verification_ref.sha256)
        self.assertEqual(result["result_artifact_ref"]["sha256"], memory_ref["sha256"])

    def test_dependency_defect_reopens_upstream_and_stales_accepted_downstream(self) -> None:
        verification_ref = self.store.put_json({"status": "pass"}, artifact_type="VerificationArtifact")
        repair_ref = self.store.put_json({"finding": "dependency"}, artifact_type="RepairBillArtifact")
        upstream = self._reviewing_node("node_upstream")
        upstream = self.verification.submit_verdict(
            node=upstream,
            verification_ref=verification_ref,
            status=VerificationStatus.PASS,
            actor="reviewer",
        ).snapshot
        downstream = self._reviewing_node("node_downstream", dependency_node_ids=(upstream.aggregate_id,))
        downstream = self.verification.submit_verdict(
            node=downstream,
            verification_ref=verification_ref,
            status=VerificationStatus.PASS,
            actor="reviewer",
        ).snapshot

        affected = DefectPropagationService(self.repository).propagate_dependency_defect(
            workflow_id=upstream.workflow_id,
            epoch_id="epoch",
            dependency_node_id=upstream.aggregate_id,
            repair_bill_ref=repair_ref,
        )

        self.assertEqual(affected, (downstream.aggregate_id,))
        self.assertEqual(
            self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, upstream.aggregate_id).state,
            "REPAIR_QUEUED",
        )
        self.assertEqual(
            self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, downstream.aggregate_id).state,
            "STALE",
        )

    def test_dependency_defect_stales_only_verification_scenarios_in_its_actual_closure(self) -> None:
        verification_ref = self.store.put_json(
            {"status": "PASS", "scenario_fingerprint": "scenario-a"},
            artifact_type="VerificationArtifact",
        )
        other_verification_ref = self.store.put_json(
            {"status": "PASS", "scenario_fingerprint": "scenario-b"},
            artifact_type="VerificationArtifact",
        )
        repair_ref = self.store.put_json(
            {"finding": "dependency"}, artifact_type="RepairBillArtifact"
        )
        module_a = self.verification.submit_verdict(
            node=self._reviewing_node("node_module_a"),
            verification_ref=verification_ref,
            status=VerificationStatus.PASS,
            actor="reviewer",
        ).snapshot
        module_b = self.verification.submit_verdict(
            node=self._reviewing_node("node_module_b"),
            verification_ref=other_verification_ref,
            status=VerificationStatus.PASS,
            actor="reviewer",
        ).snapshot
        scenario_a = self._accepted_scenario(
            "node_scenario_a",
            dependency_node_ids=(module_a.aggregate_id,),
            scenario_fingerprint="scenario-a",
            verification_ref=verification_ref,
        )
        scenario_b = self._accepted_scenario(
            "node_scenario_b",
            dependency_node_ids=(module_b.aggregate_id,),
            scenario_fingerprint="scenario-b",
            verification_ref=other_verification_ref,
        )

        affected = DefectPropagationService(self.repository).propagate_dependency_defect(
            workflow_id=module_a.workflow_id,
            epoch_id="epoch",
            dependency_node_id=module_a.aggregate_id,
            repair_bill_ref=repair_ref,
        )

        self.assertEqual(affected, (scenario_a.aggregate_id,))
        self.assertEqual(
            self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, scenario_a.aggregate_id).state,
            "STALE",
        )
        self.assertEqual(
            self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, scenario_b.aggregate_id).state,
            "ACCEPTED",
        )

    def _reviewing_node(
        self,
        node_id: str,
        *,
        dependency_node_ids: tuple[str, ...] = (),
    ) -> AggregateSnapshot:
        artifact = self.store.put_json({"contract": node_id}, artifact_type="UnitContractArtifact")
        actions = [
            (
                "CREATE_NODE_RUN",
                {
                    "unit_contract_ref": artifact.to_dict(),
                    "epoch_id": "epoch",
                    "dependency_node_ids": list(dependency_node_ids),
                },
            ),
            (
                "DEPENDENCIES_ACCEPTED",
                {"accepted_dependency_node_ids": list(dependency_node_ids), "epoch_frozen": False},
            ),
            ("START_PRODUCING", {"fencing_token": 1}),
            ("SUBMIT_CANDIDATE", {"fencing_token": 1}),
            (
                "QUIESCE_COMPLETED",
                {"fencing_token": 1, "process_group_reaped": True, "exclusive_workspace_lock": True, "workspace_fingerprint": "tree"},
            ),
            ("CANDIDATE_SNAPSHOTTED", {"candidate_ref": artifact.to_dict(), "candidate_digest": "c1", "workspace_fingerprint": "tree"}),
            ("START_REVIEW", {"fencing_token": 2}),
            (
                "SUBMIT_SEMANTIC_VERIFICATION",
                {"pending_verification_ref": {"sha256": f"pending-{node_id}"}},
            ),
            (
                "VERIFIER_QUIESCED",
                {
                    "fencing_token": 2,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "verification-tree",
                },
            ),
        ]
        for action_type, payload in actions:
            snapshot = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id="wf_verify",
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node_id,
                    actor="test",
                    expected_version=snapshot.version if snapshot else 0,
                    idempotency_key=f"{node_id}:{action_type}",
                    payload=payload,
                )
            )
        return self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)

    def _accepted_scenario(
        self,
        node_id: str,
        *,
        dependency_node_ids: tuple[str, ...],
        scenario_fingerprint: str,
        verification_ref,
    ) -> AggregateSnapshot:
        contract = self.store.put_json(
            {"verification_name": node_id},
            artifact_type="VerificationScenarioContractArtifact",
        )
        union = self.store.put_json(
            {"scenario": node_id}, artifact_type="CandidateUnionArtifact"
        )
        actions = [
            (
                "CREATE_NODE_RUN",
                {
                    "unit_contract_ref": contract.to_dict(),
                    "epoch_id": "epoch",
                    "node_kind": "verification",
                    "dependency_node_ids": list(dependency_node_ids),
                },
            ),
            (
                "VERIFICATION_DEPENDENCIES_ACCEPTED",
                {
                    "accepted_dependency_node_ids": list(dependency_node_ids),
                    "epoch_frozen": False,
                },
            ),
            (
                "VERIFICATION_PREPARED",
                {
                    "scenario_fingerprint": scenario_fingerprint,
                    "scenario_candidate_union_ref": union.to_dict(),
                    "scenario_commit_sha": f"commit-{node_id}",
                    "verification_workspace_fingerprint": f"tree-{node_id}",
                },
            ),
            ("START_SCENARIO_VERIFICATION", {"fencing_token": 1}),
            (
                "SUBMIT_SEMANTIC_VERIFICATION",
                {"pending_verification_ref": {"sha256": f"pending-{node_id}"}},
            ),
            (
                "VERIFIER_QUIESCED",
                {
                    "fencing_token": 1,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": f"verification-tree-{node_id}",
                },
            ),
            (
                "VERIFICATION_PASSED",
                {
                    "verification_artifact_ref": verification_ref.to_dict(),
                    "scenario_fingerprint": scenario_fingerprint,
                },
            ),
        ]
        for action_type, payload in actions:
            snapshot = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id="wf_verify",
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node_id,
                    actor="test",
                    expected_version=snapshot.version if snapshot else 0,
                    idempotency_key=f"{node_id}:{action_type}",
                    payload=payload,
                )
            )
        snapshot = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
        assert snapshot is not None
        return snapshot


class MinionV2IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_minion_v2_integration_"))
        self.repository = MinionV2Repository(self.runtime_root)
        self.store = ContentAddressedArtifactStore(self.runtime_root, self.repository)
        self.repo = self.runtime_root / "repo"
        self.repo.mkdir()
        self._git("init", "-q")
        self._git("config", "user.name", "Test")
        self._git("config", "user.email", "test@example.com")
        (self.repo / "base.txt").write_text("base\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-qm", "base")
        self.base = self._git("rev-parse", "HEAD").strip()

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def test_integration_cherry_picks_candidates_in_declared_order(self) -> None:
        sha_a = self._candidate("candidate-a", "a.txt", "a\n")
        sha_b = self._candidate("candidate-b", "b.txt", "b\n")
        self._git("checkout", "-q", "--detach", self.base)
        ref, integration_sha = IntegrationService(self.store).integrate_candidates(
            integration_worktree=self.repo,
            ordered_candidates=[
                {"node_run_id": "a", "candidate_digest": sha_a},
                {"node_run_id": "b", "candidate_digest": sha_b},
            ],
            architecture_manifest_sha="manifest",
        )
        self.assertEqual(self._git("rev-parse", "HEAD").strip(), integration_sha)
        self.assertEqual([item["node_run_id"] for item in self.store.read_json(ref)["merged_candidates"]], ["a", "b"])
        self.assertTrue((self.repo / "a.txt").is_file())
        self.assertTrue((self.repo / "b.txt").is_file())

    def test_integration_conflict_is_an_ownership_defect(self) -> None:
        sha_a = self._candidate("conflict-a", "base.txt", "from a\n")
        sha_b = self._candidate("conflict-b", "base.txt", "from b\n")
        self._git("checkout", "-q", "--detach", self.base)

        with self.assertRaises(IntegrationOwnershipDefect):
            IntegrationService(self.store).integrate_candidates(
                integration_worktree=self.repo,
                ordered_candidates=[
                    {"node_run_id": "a", "candidate_digest": sha_a},
                    {"node_run_id": "b", "candidate_digest": sha_b},
                ],
                architecture_manifest_sha="manifest",
            )

        self.assertEqual(
            self._git("rev-parse", "HEAD^{tree}").strip(),
            self._git("rev-parse", f"{sha_a}^{{tree}}").strip(),
        )
        self.assertEqual((self.repo / "base.txt").read_text(encoding="utf-8"), "from a\n")

    def _candidate(self, branch: str, filename: str, content: str) -> str:
        self._git("checkout", "-q", "-B", branch, self.base)
        (self.repo / filename).write_text(content, encoding="utf-8")
        self._git("add", filename)
        self._git("commit", "-qm", branch)
        return self._git("rev-parse", "HEAD").strip()

    def _git(self, *args: str) -> str:
        return subprocess.check_output(["git", *args], cwd=self.repo, text=True)


if __name__ == "__main__":
    unittest.main()
