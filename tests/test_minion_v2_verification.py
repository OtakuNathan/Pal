from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

from pal.llm.contracts import CanonicalToolCall, CanonicalToolResult
from pal.minion.v2 import ActionEnvelope, AggregateType, ContentAddressedArtifactStore, MinionV2Repository
from pal.minion.v2.contracts import AggregateSnapshot
from pal.minion.v2.integration import IntegrationOwnershipDefect, IntegrationService
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.service import MinionV2WorkflowService
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
    verification_builder_tool_result,
)
from pal.minion.v2.candidate_builder import candidate_builder_tool_result
from pal.minion.v2.submission_drafts import AUTHORING_CONTRACT_VERSION
from pal.shared import RuntimeStatus
from pal.minion.v2.workers import (
    _compile_standalone_review_markdown,
    _recorded_verification_case_results,
    _reject_manager_identity_fields,
    _routable_verification_findings,
    _resolve_verification_defect_targets,
    _seed_durable_verification_scratch,
    _validate_skeleton_coder_report,
    _validate_semantic_verification_plan_shape,
    _validate_verifier_requirement_refs,
    _verification_case_specs,
    _verification_findings,
)


class _FakeExecutionAdapter:
    def __init__(self) -> None:
        self.calls: list[CanonicalToolCall] = []

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
                structured={"status": "ok", "diagnostics": [], "diagnostics_state": "fresh"},
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

    @staticmethod
    def _verification_submission() -> dict[str, object]:
        return {
            "cases": [
                {
                    "name": "released resource rejects use",
                    "case_kind": "contract_adversarial",
                    "command": ["python", "-m", "pytest", "tests/test_resource.py", "-q"],
                    "expected_exit_codes": [0],
                    "requirements": [
                        {
                            "section": "Lifecycle",
                            "requirement": "Released resources reject further use.",
                        }
                    ],
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
    ) -> dict[str, object]:
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
        return asyncio.run(
            verification_builder_tool_result(
                CanonicalToolCall(
                    name=name,
                    args=args or {},
                    call_id=f"verification-call-{self.call_index}",
                ),
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
                original_adapter=self.adapter,
            )
        )

    def _record_lifecycle_case(
        self,
        workspace: dict[str, object],
        *,
        requirement: str = "Released resources reject further use.",
        command: str = "printf verified",
    ):
        return self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            {
                "name": "released resource rejects use",
                "command": command,
                "description": "Exercise the public operation after release.",
                "requirement_section": "Lifecycle",
                "requirement": requirement,
                "path": "src/resource.py",
                "symbol": "use",
                "invariants": ["Released is terminal."],
            },
        )

    def test_verifier_submit_schema_is_the_only_completion_shape(self) -> None:
        schema = VERIFICATION_BUILDER_TOOL_SPECS["op_minion_verification_submit"][
            "parameters_schema"
        ]
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

    def test_case_binding_rejects_unknown_requirement_section_before_execution(self) -> None:
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
        self.assertFalse(result.ok)
        self.assertIn("Requirement section 'Lifecycle' is not bound", result.llm_text)
        self.assertIn("Validation", result.llm_text)
        self.assertEqual(self.adapter.calls, [])
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
                    "test_scopes": [{"kind": "file", "path": "tests/test_font.cpp"}],
                }
            ),
            encoding="utf-8",
        )
        workspace = self._bind_workspace({
            "artifact_dir": str(artifact_dir),
            "artifact_stage_dir": str(stage_dir),
            "reference_paths": [{"name": "unit_work_view", "path": str(work_view)}],
        }, role="producer")
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

    def test_candidate_submit_matches_report_to_live_git_delta(self) -> None:
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
                    "implementation_scopes": [{"kind": "directory", "path": "src/font"}],
                    "test_scopes": [],
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
        }, role="producer")
        checked = self._candidate_call(
            workspace,
            "op_minion_developer_test",
            {"name": "focused render check", "command": "test -f src/font/backend.cpp"},
        )
        self.assertTrue(checked.ok, checked.text)
        accepted = self._candidate_call(workspace, "op_minion_candidate_submit")
        self.assertTrue(accepted.ok)
        report = json.loads((stage_dir / "coder_report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["files_changed"], ["src/font/backend.cpp"])
        self.assertEqual(report["tests_run"], ["focused render check: PASS"])

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
                    "test_scopes": [],
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
            role="producer",
        )
        checked = self._candidate_call(
            workspace,
            "op_minion_developer_test",
            {"name": "focused render check", "command": "test -f src/font/backend.cpp"},
        )
        self.assertTrue(checked.ok, checked.text)
        produced: list[dict[str, object]] = []
        with (
            patch(
                "pal.minion.v2.candidate_builder.SubmissionDraftStore.uses_worker_gateway",
                new_callable=PropertyMock,
                return_value=True,
            ),
            patch(
                "pal.minion.v2.candidate_builder.SubmissionDraftStore.mark_submitted",
                side_effect=ValueError("worker submission is missing required input reads: repair_bill"),
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
            role="producer",
        )
        produced = [
            {
                "path": str(product_path),
                "relative_path": "checkin.json",
                "role": "primary",
            }
        ]
        checked = self._candidate_call(
            workspace,
            "op_minion_developer_test",
            {"name": "validate check-in", "command": "test -f checkin.json"},
            produced,
        )
        self.assertTrue(checked.ok, checked.text)
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
        self.assertEqual(report["tests_run"], ["validate check-in: PASS"])

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
            role="producer",
        )
        checked = self._candidate_call(
            workspace,
            "op_minion_developer_test",
            {"name": "workspace is empty", "command": "test ! -e product.json"},
        )
        self.assertTrue(checked.ok, checked.text)
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
            role="producer",
        )
        checked = self._candidate_call(
            workspace,
            "op_minion_developer_test",
            {"name": "pollution exists", "command": "test -f producer_report.json"},
        )
        self.assertTrue(checked.ok, checked.text)
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
        self.assertTrue(
            self._verification_call(
                workspace,
                "op_minion_verification_scratch_write",
                {"path": "priority_probe.py", "content": "print('first')\n"},
            ).ok
        )
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
        self.assertTrue(
            self._verification_call(
                workspace,
                "op_minion_verification_scratch_write",
                {"path": "priority_probe.py", "content": "print('second')\n"},
            ).ok
        )
        third = self._verification_call(
            workspace, "op_minion_verification_run_adversarial_case", args
        )
        self.assertFalse(bool(third.structured.get("reused")))

    def test_invocation_tool_contract_is_stable_and_structural(self) -> None:
        work_view = {
            "module_name": "rule_router",
            "requirements": {
                "sections": {
                    "Validation": [
                        "Priority must be an integer.",
                        "Priority must be finite.",
                    ]
                }
            },
            "contract_paths": ["src/rule_router/protocol.py"],
            "construction_dependencies": ["rule_model"],
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
        self.assertEqual(
            first["requirements"]["Validation"],
            ["Priority must be an integer.", "Priority must be finite."],
        )
        run_description = first["description_overrides"][
            "op_minion_verification_run_adversarial_case"
        ]
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
            {"case": "runtime case", "defect_kind": "module_defect"},
            {"case": "contract case", "defect_kind": "contract_defect"},
            {"case": "dependency case", "defect_kind": "dependency_defect"},
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
        for tool, case in (
            ("op_minion_verification_report_module_defect", "runtime shape"),
            ("op_minion_verification_report_contract_defect", "contract shape"),
        ):
            finding = self._verification_call(
                workspace,
                tool,
                {
                    "case": case,
                    "finding_section": "interface",
                    "summary": f"{case} failed",
                    "failure_reason": "The reproduced probe exited nonzero.",
                    "severity": "major",
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
            {item["defect_kind"] for item in compiled["findings"]},
            {"module_defect", "contract_defect"},
        )

    def test_unique_requirement_section_binds_canonical_text_before_execution(self) -> None:
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
        recorded = self._record_lifecycle_case(
            workspace,
            requirement="Released resources may reject use.",
        )
        self.assertTrue(recorded.ok, recorded.text)
        self.assertEqual(
            recorded.structured["case"]["requirements"],
            [
                {
                    "section": "Lifecycle",
                    "requirement": "Released resources reject further use.",
                }
            ],
        )
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
        self.assertEqual(
            compiled["cases"][0]["requirements"][0]["requirement"],
            "Released resources reject further use.",
        )

    def test_ambiguous_requirement_section_returns_candidates_before_execution(self) -> None:
        work_view = self.runtime_root / "ambiguous-work-view.json"
        work_view.write_text(
            json.dumps(
                {
                    "requirements": {
                        "sections": {
                            "Validation": [
                                "Reject fractional priorities.",
                                "Reject non-finite priorities.",
                            ]
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        workspace = self._bind_workspace(
            {
                "repo_path": str(self.runtime_root),
                "reference_paths": [{"name": "module_work_view", "path": str(work_view)}],
            },
            role="verifier",
        )
        result = self._verification_call(
            workspace,
            "op_minion_verification_run_adversarial_case",
            {
                "name": "fractional priority",
                "command": "exit 7",
                "requirement_section": "Validation",
                "requirement": "Reject bad priorities.",
            },
        )
        self.assertFalse(result.ok)
        self.assertIn("ambiguous", result.llm_text)
        self.assertIn("Reject fractional priorities.", result.llm_text)
        self.assertEqual(self.adapter.calls, [])

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
            "op_minion_verification_report_module_defect",
            {
                "case": "priority shape",
                "finding_section": "implementation",
                "summary": "Fractional priority is accepted.",
                "failure_reason": "The public constructor returned success.",
                "severity": "major",
            },
        )
        self.assertTrue(module_finding.ok, module_finding.text)
        contract_finding = self._verification_call(
            workspace,
            "op_minion_verification_report_contract_defect",
            {
                "case": "priority shape",
                "finding_section": "interface",
                "summary": "Priority shape is underspecified.",
                "failure_reason": "The frozen contract does not constrain numeric shape.",
                "severity": "major",
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
            {item["defect_kind"] for item in status.structured["findings"]},
            {"module_defect", "contract_defect"},
        )
        duplicate = self._verification_call(
            workspace,
            "op_minion_verification_report_module_defect",
            {
                "case": "priority shape",
                "finding_section": "implementation",
                "summary": "Fractional priority is accepted.",
                "failure_reason": "The public constructor returned success.",
                "severity": "major",
            },
        )
        self.assertTrue(duplicate.ok, duplicate.text)
        self.assertTrue(duplicate.structured["deduplicated"])
        removed = self._verification_call(
            workspace,
            "op_minion_verification_remove_finding",
            {
                "case": "priority shape",
                "summary": "Priority shape is underspecified.",
                "reason": "The finding was classified against the wrong contract layer.",
            },
        )
        self.assertTrue(removed.ok, removed.text)
        after_removal = self._verification_call(
            workspace, "op_minion_verification_draft_status"
        )
        self.assertEqual(
            [item["summary"] for item in after_removal.structured["findings"]],
            ["Fractional priority is accepted."],
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
        self.assertEqual(final.structured["findings"], [])

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
            "op_minion_verification_report_module_defect",
            {
                "case": "released resource rejects use",
                "finding_section": "implementation",
                "summary": "Released resource remains usable.",
                "failure_reason": "The adversarial probe exited with failure.",
                "severity": "major",
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
            draft_kind="verification",
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, VerificationStatus.PASS)

    def test_rerun_keeps_finding_until_case_passes(self) -> None:
        workspace = self._bind_workspace(
            {"repo_path": str(self.runtime_root)},
            role="verifier",
        )
        failed = self._record_lifecycle_case(workspace, command="exit 7")
        self.assertTrue(failed.ok, failed.text)
        finding = self._verification_call(
            workspace,
            "op_minion_verification_report_module_defect",
            {
                "case": "released resource rejects use",
                "finding_section": "implementation",
                "summary": "Released resource remains usable.",
                "failure_reason": "The adversarial probe exited with failure.",
                "severity": "major",
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
        self.assertEqual(passed_status.structured["findings"], [])

    def test_withdrawing_finding_requires_audit_reason(self) -> None:
        workspace = self._bind_workspace(
            {"repo_path": str(self.runtime_root)},
            role="verifier",
        )
        result = self._verification_call(
            workspace,
            "op_minion_verification_remove_finding",
            {"case": "missing", "summary": "missing finding"},
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

    def test_finding_rejects_passing_case(self) -> None:
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
            "op_minion_verification_report_module_defect",
            {
                "case": "passing probe",
                "finding_section": "implementation",
                "summary": "This must not be accepted.",
                "failure_reason": "The cited probe passed.",
                "severity": "major",
            },
        )
        self.assertFalse(finding.ok)
        self.assertIn("FAIL or UNKNOWN", finding.llm_text)

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
                    "requirements": [
                        {
                            "section": "Lifecycle",
                            "requirement": "Released resources reject further use.",
                        }
                    ],
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
        self.assertEqual(findings[0]["case_name"], cases[0].case_name)
        _validate_verifier_requirement_refs(
            work_view={
                "requirements": {
                    "sections": {"Lifecycle": ["Released resources reject further use."]}
                }
            },
            cases=cases,
            findings=findings,
        )
        with self.assertRaisesRegex(ValueError, "Manager-owned identity"):
            _reject_manager_identity_fields({**plan, "finding_id": "F-1"}, owner="test verifier")
        with self.assertRaisesRegex(ValueError, "outside its ModuleWorkView"):
            _validate_verifier_requirement_refs(
                work_view={"requirements": {"sections": {"Lifecycle": ["Different text."]}}},
                cases=cases,
                findings=findings,
            )

    def test_verifier_can_propose_semantic_requirement_patch_but_not_manager_metadata(self) -> None:
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
            "op_minion_verification_report_contract_defect",
            {
                "case": "released resource rejects use",
                "finding_section": "lifecycle",
                "summary": "Reset semantics are missing from the product contract.",
                "failure_reason": "The reproduced consumer observes reordered routes.",
                "severity": "major",
                "target_module": "router",
                "requirement_section": "Lifecycle",
                "requirement": "Released resources reject further use.",
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
        self.assertTrue(proposal.ok, proposal.text)
        schema = VERIFICATION_BUILDER_TOOL_SPECS[
            "op_minion_verification_propose_requirement_patch"
        ]["parameters_schema"]
        self.assertNotIn("observed_at", schema["properties"])
        self.assertNotIn("artifact_ref", schema["properties"])

    def test_standalone_review_markdown_exposes_semantics_not_manager_identity(self) -> None:
        markdown = _compile_standalone_review_markdown(
            {
                "status": "FAIL",
                "reviewer_summary": "The released-state contract is violated.",
                "report_ref": {"sha256": "hidden-report-digest"},
                "findings": [
                    {
                        "case": "released resource rejects use",
                        "finding_section": "lifecycle",
                        "summary": "Use after release succeeds.",
                        "failure_reason": "The method returns success after release.",
                        "requirements": [
                            {
                                "section": "Lifecycle",
                                "requirement": "Released resources reject further use.",
                            }
                        ],
                        "locations": [{"path": "src/resource.py", "symbol": "use"}],
                        "severity": "high",
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
        self.assertIn("Lifecycle - Released resources reject further use.", markdown)
        self.assertIn("src/resource.py::use", markdown)
        self.assertIn("released resource rejects use", markdown)
        self.assertNotIn("hidden-report-digest", markdown)
        self.assertNotIn("hidden-stdout-digest", markdown)
        self.assertNotIn("sha256", markdown)

    def test_coder_defect_report_is_bound_to_module_and_requirement_text(self) -> None:
        report = {
            "current_micro_plan": [],
            "completed_checklist": [],
            "files_inspected": ["include/ohos_font.h"],
            "files_changed": [],
            "tests_run": [],
            "open_questions": [],
            "known_failures": [],
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
        with self.assertRaisesRegex(ValueError, "outside its bound work view"):
            _validate_skeleton_coder_report(
                {
                    **report,
                    "requirements": [
                        {"section": "Font rendering", "requirement": "A fabricated requirement."}
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
                reproducer_hash=output.sha256,
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
            ("unknown_blocked", VerificationStatus.UNKNOWN, None, repair_ref, "REPAIR_QUEUED"),
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

    def test_contract_defect_carries_requirement_patch_into_replan_state(self) -> None:
        node = self._reviewing_node("node_requirement_patch")
        verification_ref = self.store.put_json(
            {"status": "FAIL"}, artifact_type="VerificationArtifact"
        )
        repair_ref = self.store.put_json(
            {"finding": "contract"}, artifact_type="RepairBillArtifact"
        )
        patch_ref = self.store.put_json(
            {"requirement": "Reset preserves precedence."},
            artifact_type="RequirementPatchArtifact",
        )
        revised_ref = self.store.put_json(
            {"requirements": [{"statement": "Reset preserves precedence."}]},
            artifact_type="RequirementsArtifact",
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
            requirement_patch_ref=patch_ref,
            revised_requirements_ref=revised_ref,
        )
        self.assertEqual(result.snapshot.state, "STALE")
        self.assertEqual(result.snapshot.payload["requirement_patch_ref"], patch_ref.to_dict())
        self.assertEqual(result.snapshot.payload["revised_requirements_ref"], revised_ref.to_dict())

    def test_requirement_patch_replan_uses_revised_requirements_and_returns_to_human_review_path(self) -> None:
        service = MinionV2WorkflowService(self.runtime_root)
        base_requirements_ref = service.architecture.publish_requirements(
            {
                "requirements": [
                    {
                        "section": "Routing",
                        "statement": "Route matching must be deterministic.",
                        "strength": "hard",
                    }
                ]
            }
        )
        repair_ref = self.store.put_json(
            {"summary": "Reset changes route precedence."}, artifact_type="RepairBillArtifact"
        )
        patch_ref, revised_ref = service.architecture.publish_requirement_patch(
            base_requirements_ref=base_requirements_ref,
            proposal={
                "patch_kind": "derived_constraint",
                "section": "Reset semantics",
                "requirement": "Reset must preserve configured route precedence.",
                "strength": "hard",
                "reason": "A reproduced consumer observes a changed route after reset.",
                "affected_modules": ["router"],
                "affected_contracts": [],
            },
            source={"role": "verifier", "stage": "module_verification"},
            source_artifact_ref=repair_ref,
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
        workflow_id = "wf_requirement_patch_replan"
        epoch_id = "epoch_requirement_patch_replan"
        node_id = "node_requirement_patch_replan"
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
                "CONTRACT_DEFECT",
                {
                    "repair_bill_ref": repair_ref.to_dict(),
                    "requirement_patch_ref": patch_ref.to_dict(),
                    "revised_requirements_ref": revised_ref.to_dict(),
                },
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
                "effect_key": "requirement-patch-replan",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": node_id,
            }
        )
        processor._freeze_epoch_for_replan(
            {
                "effect_key": "requirement-patch-freeze",
                "aggregate_type": AggregateType.EXECUTION_EPOCH.value,
                "aggregate_id": epoch_id,
            }
        )
        processor._create_replan_revision(
            {
                "effect_key": "requirement-patch-create-revision",
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
        self.assertEqual(revision.payload["requirements_ref"], revised_ref.to_dict())
        self.assertEqual(revision.payload["requirement_patch_refs"], [patch_ref.to_dict()])
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
