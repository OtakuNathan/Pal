from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
import subprocess

from pal.bunshin.architecture_compilation import ArchitectureTemplateCompiler
from pal.bunshin.contract_protocol import software_contract_projection
from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.graph_protocol import graph_ir_from_mapping
from pal.bunshin.git_scope import scoped_role_git_read_plan
from pal.execution.git_tool import GitTool, _run_git, classify_git_command
from pal.bunshin.service import BunshinV2WorkflowService
from pal.bunshin.submission_drafts import (
    AUTHORING_CONTRACT_VERSION,
    SubmissionDraftContext,
    SubmissionDraftStore,
)
from pal.bunshin.role_gateway import (
    RoleAssignmentGateway,
    RoleGatewayArtifactStore,
)
from pal.bunshin.role_protocol import RoleAssignmentRequest
from pal.bunshin.submission_errors import SubmissionValidationError
from pal.bunshin.workflow_runtime import WorkflowCoordinator


class BunshinV2RoleGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal-v2-worker-gateway-"))
        self.service = BunshinV2WorkflowService(self.runtime_root)
        self.gateway = RoleAssignmentGateway(self.service)
        self.workspace = self.runtime_root / "workspace"
        self.workspace.mkdir()
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        (self.workspace / "README.md").write_text("gateway\n", encoding="utf-8")
        (self.workspace / "src" / "router").mkdir(parents=True)
        (self.workspace / "src" / "router" / "router.py").write_text(
            "VALUE = 1\n",
            encoding="utf-8",
        )
        self.input_ref = self.service.artifacts.put_json(
            {
                "module": "router",
                "contract": "route deterministically",
                "implementation_scopes": ["src/router"],
                "contract_paths": ["include/router.hpp"],
                "dependency_contracts": {
                    "codec": {"contract_paths": ["include/codec.hpp"]}
                },
            },
            artifact_type="ModuleWorkViewArtifact",
        )
        self.bound_input_path = self.runtime_root / "input.json"
        self.bound_input_path.write_text(
            '{"module":"router","contract":"route deterministically"}\n',
            encoding="utf-8",
        )
        self.prompt_ref = self.service.artifacts.put_json(
            {
                "instruction": "implement router",
                "workspace": {"repo_path": str(self.workspace)},
                "metadata": {
                    "requirements_brief": {
                        "references": [
                            {
                                "name": "module_work_view",
                                "path": str(self.bound_input_path),
                            }
                        ]
                    }
                },
            },
            artifact_type="RolePromptPackArtifact",
        )
        self.service.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="workflow-router",
                actor="test",
                expected_version=0,
            )
        )
        self.service.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="CREATE_NODE_RUN",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="test",
                expected_version=0,
                payload={
                    "epoch_id": "epoch-router",
                    "module_name": "router",
                    "unit_contract_ref": {"sha256": "contract-router"},
                },
            )
        )
        self.service.repository.role_sessions.ensure_role_session(
            session_id="session-router",
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            role="implementation",
            mode="produce",
            role_profile_id="software_engineering.v2_coder",
            family_binding_sha="binding",
            scope_kind="module",
            subject_key="router",
        )
        assignment = self.service.repository.role_assignments.create_role_assignment(
            RoleAssignmentRequest(
                assignment_key="router-cycle-1",
                session_id="session-router",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN.value,
                aggregate_id="node-router",
                role="implementation",
                mode="produce",
                role_profile_id="software_engineering.v2_coder",
                family_binding_sha="binding",
                input_fingerprint="router-input",
                required_inputs=(),
                input_refs={"module_work_view": self.input_ref.to_dict()},
                execution_spec={"effect_type": "run_implementation_role"},
                submission_kind="candidate",
            )
        )
        self.assignment_id = assignment["assignment_id"]
        attempt = self.service.repository.role_assignments.claim_role_assignment(self.assignment_id)
        self.attempt_id = attempt["attempt_id"]
        self.lease_resource = f"assignment:{self.assignment_id}"
        lease = self.service.repository.leases.claim_lease(
            self.lease_resource,
            self.attempt_id,
            ttl_seconds=120,
        )
        self.fencing_token = lease.fencing_token
        self.service.repository.role_attempts.start_role_attempt(
            assignment_id=self.assignment_id,
            attempt_id_value=self.attempt_id,
            lease_resource_key=self.lease_resource,
            fencing_token=self.fencing_token,
            prompt_pack_ref=self.prompt_ref.to_dict(),
        )
        self.access_token = self.service.repository.role_access.issue_role_attempt_access_token(
            assignment_id=self.assignment_id,
            attempt_id_value=self.attempt_id,
            fencing_token=self.fencing_token,
        )
        self.context = {
            "workflow_id": "workflow-router",
            "invocation_id": self.attempt_id,
            "lease_resource_key": self.lease_resource,
            "fencing_token": self.fencing_token,
            "role": "implementation",
            "mode": "produce",
            "draft_kind": "candidate",
            "input_fingerprint": "router-input",
            "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
        }

    def call(self, method: str, **params):
        return self.gateway.call(
            method,
            {"access_token": self.access_token, **params},
        )

    def test_gateway_rejects_legacy_bound_input_receipt_method(self) -> None:
        with self.assertRaisesRegex(ValueError, "not allowed"):
            self.call("bound_input_read", name="module_work_view")

    def test_gateway_reads_authenticated_bound_input_json(self) -> None:
        # The normal worker prompt stores a projected /pal path, while the
        # Manager keeps the source path in its authenticated prompt metadata.
        value = self.call("bound_input_json", name="module_work_view")
        self.assertEqual(value["value"]["module"], "router")

    def test_gateway_persists_harness_continuation_by_logical_session(self) -> None:
        written = self.call(
            "harness_state_write",
            state={"thread_id": "thread-architect-1"},
        )
        self.assertEqual(
            written["state"],
            {"thread_id": "thread-architect-1"},
        )
        restored = self.call("harness_state_read")
        self.assertEqual(restored["harness_id"], "pal")
        self.assertEqual(restored["harness_generation"], "")
        self.assertEqual(
            restored["state"],
            {"thread_id": "thread-architect-1"},
        )

        with self.assertRaisesRegex(ValueError, "64 KiB"):
            self.call(
                "harness_state_write",
                state={"value": "x" * (65 * 1024)},
            )

    def test_gateway_rejects_worker_owned_execution_state_methods(
        self,
    ) -> None:
        for method in (
            "execution_context",
            "execution_begin_input",
            "execution_record_delivery",
            "execution_store_pager",
            "execution_read_pager",
            "execution_file_grant",
            "execution_file_snapshot",
            "execution_set_file_snapshot",
            "execution_invalidate_file",
            "execution_retire",
        ):
            with self.subTest(method=method), self.assertRaisesRegex(
                ValueError,
                "not allowed",
            ):
                self.call(method)
    def test_gateway_owns_draft_cas_artifact_publish_and_submission_receipt(self) -> None:
        first = self.call("draft_read", context=self.context, seed={"checks": []})
        self.assertEqual(first["snapshot"]["version"], 0)
        mutation = self.call(
            "draft_mutate",
            context=self.context,
            operation_key="record-check",
            request={"check": "unit"},
            expected_version=0,
            next_payload={"checks": ["unit"]},
            result={"recorded": True},
            seed={"checks": []},
        )
        self.assertEqual(mutation["result"]["draft_version"], 1)
        receipt = self.call(
            "draft_submit",
            context=self.context,
            expected_version=1,
            submission={"status": "candidate_ready", "checks": ["unit"]},
        )
        self.assertTrue(receipt["submitted"])
        self.assertTrue(receipt["submission_artifact_ref"]["sha256"])
        self.assertTrue(receipt["submission_payload_hash"])
        assignment = self.service.repository.role_assignments.read_role_assignment(self.assignment_id)
        self.assertEqual(assignment["state"], "result_recorded")
        self.assertTrue(assignment["submission_artifact_ref"]["sha256"])
        status = self.call("submission_status")
        self.assertTrue(status["recorded"])
        self.assertEqual(status["submission_artifact_ref"], receipt["submission_artifact_ref"])
        self.assertEqual(status["submission_payload_hash"], receipt["submission_payload_hash"])

    def test_gateway_receipt_blocks_draft_race_before_projection(self) -> None:
        first = self.call("draft_read", context=self.context, seed={"checks": []})
        self.assertEqual(first["snapshot"]["version"], 0)
        original = self.service.repository.role_submissions.record_role_submission

        def record_then_race(**kwargs):
            receipt = original(**kwargs)
            store = SubmissionDraftStore(self.runtime_root)
            context = SubmissionDraftContext.from_mapping(self.context)
            with self.assertRaisesRegex(ValueError, "frozen"):
                store.mutate_precomputed(
                    context,
                    operation_key="late-draft-race",
                    request={"check": "late"},
                    expected_version=0,
                    next_payload={"checks": ["late"]},
                    result={"recorded": True},
                    seed={"checks": []},
                )
            return receipt

        self.service.repository.role_submissions.record_role_submission = record_then_race
        receipt = self.call(
            "draft_submit",
            context=self.context,
            expected_version=0,
            submission={"status": "candidate_ready", "checks": []},
        )

        self.assertTrue(receipt["submitted"])
        draft = self.call("draft_read", context=self.context, seed={})
        self.assertEqual(draft["snapshot"]["status"], "submitted")
        self.assertEqual(draft["snapshot"]["version"], 0)
        with self.assertRaisesRegex(ValueError, "receipt already froze authoring"):
            self.call(
                "draft_mutate",
                context=self.context,
                operation_key="too-late",
                request={},
                expected_version=1,
                next_payload={},
                result={},
                seed={},
            )

    def test_verifier_rejection_before_receipt_keeps_same_attempt_retryable(self) -> None:
        corpus = self.workspace / "tests" / "router" / "verifier"
        corpus.mkdir(parents=True)
        (corpus / "test_router.py").write_text(
            "def test_router():\n    assert True\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "add", "-A"],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Pal Tests",
                "-c",
                "user.email=pal-tests@example.invalid",
                "commit",
                "-m",
                "candidate",
            ],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        candidate_digest = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        node_id = "node-router-verification"
        self.service.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="CREATE_NODE_RUN",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node_id,
                actor="test",
                expected_version=0,
                payload={
                    "epoch_id": "epoch-router",
                    "module_name": "router",
                    "unit_contract_ref": {"sha256": "contract-router"},
                    "unit_work_view_ref": self.input_ref.to_dict(),
                    "candidate_digest": candidate_digest,
                    "path_policy": {
                        "verification_corpus": {
                            "kind": "directory",
                            "path": "tests/router/verifier",
                        }
                    },
                },
            )
        )
        session_id = "session-router-verification"
        self.service.repository.role_sessions.ensure_role_session(
            session_id=session_id,
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id=node_id,
            role="verifier",
            mode="module",
            role_profile_id="software_engineering.v2_verifier",
            family_binding_sha="binding",
            scope_kind="module",
            subject_key="router",
        )
        assignment = self.service.repository.role_assignments.create_role_assignment(
            RoleAssignmentRequest(
                assignment_key="router-verification-cycle-1",
                session_id=session_id,
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN.value,
                aggregate_id=node_id,
                role="verifier",
                mode="module",
                role_profile_id="software_engineering.v2_verifier",
                family_binding_sha="binding",
                input_fingerprint="router-verification-input",
                required_inputs=(),
                input_refs={"module_work_view": self.input_ref.to_dict()},
                execution_spec={"effect_type": "run_verifier_role"},
                submission_kind="verification",
            )
        )
        assignment_id = str(assignment["assignment_id"])
        attempt = self.service.repository.role_assignments.claim_role_assignment(assignment_id)
        attempt_id = str(attempt["attempt_id"])
        lease_resource = f"assignment:{assignment_id}"
        fence = self.service.repository.leases.claim_lease(
            lease_resource,
            attempt_id,
            ttl_seconds=120,
        ).fencing_token
        review_scratch = self.runtime_root / "verifier-scratch"
        review_scratch.mkdir()
        receipt_workspace = {
            "repo_path": str(self.workspace), "review_scratch_dir": str(review_scratch),
            "verification_scratch_only": False,
            "write_path_scopes": [{"kind": "directory", "path": "tests/router/verifier"}],
            "bunshin_v2": {"authoring_input_fingerprint": "router-verification-input"},
            "review_tool_evidence_refs": [],
        }
        prompt_ref = self.service.artifacts.put_json(
            {"workspace": receipt_workspace},
            artifact_type="RolePromptPackArtifact",
        )
        self.service.repository.role_attempts.start_role_attempt(
            assignment_id=assignment_id,
            attempt_id_value=attempt_id,
            lease_resource_key=lease_resource,
            fencing_token=fence,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        token = self.service.repository.role_access.issue_role_attempt_access_token(
            assignment_id=assignment_id,
            attempt_id_value=attempt_id,
            fencing_token=fence,
        )
        context = {
            "workflow_id": "workflow-router",
            "invocation_id": attempt_id,
            "lease_resource_key": lease_resource,
            "fencing_token": fence,
            "role": "verifier",
            "mode": "module",
            "draft_kind": "verification",
            "input_fingerprint": "router-verification-input",
            "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
        }
        submission = {
            "schema_version": "4",
            "outcome": "pass",
            "findings": [],
            "advisories": [],
            "recorded_results": [
                {
                    "name": "candidate diff risk",
                    "case_kind": "diff_risk",
                    "obligation_tags": ["candidate_delta_review"],
                }
            ],
            "tool_receipts": [
                {"kind": "command", "ok": True, "output_sha256": "ok"}
            ],
        }

        self.gateway.call("draft_mutate", {"access_token": token, "context": context,
            "operation_key": "record-current-case", "request": {"case": "candidate diff risk"},
            "expected_version": 0, "next_payload": {"evidence": {"cases": {
                "candidate diff risk": submission["recorded_results"][0]}}}, "result": {}})
        receipt_workspace["verification_case_revision"] = 1

        build_file = self.workspace / "build" / "CMakeCache.txt"
        build_file.parent.mkdir()
        build_file.write_text("transient\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside the bound module corpus"):
            self.gateway.call(
                "draft_submit",
                {
                    "access_token": token,
                    "context": context,
                    "expected_version": 1,
                    "expected_work_item_version": 0,
                    "submission": submission,
                },
            )

        rejected_assignment = self.service.repository.role_assignments.read_role_assignment(
            assignment_id
        )
        self.assertEqual(rejected_assignment["state"], "running")
        self.assertFalse(rejected_assignment["submission_artifact_ref"])
        draft = self.gateway.call(
            "draft_read",
            {"access_token": token, "context": context, "seed": {}},
        )
        self.assertEqual(draft["snapshot"]["status"], "active")

        build_file.unlink()
        build_file.parent.rmdir()
        with self.assertRaisesRegex(SubmissionValidationError, "fresh validation required"):
            self.gateway.call("draft_submit", {
                "access_token": token, "context": context, "expected_version": 1,
                "expected_work_item_version": 0,
                "submission": submission,
            })
        # A legacy checkpoint stays readable, but requires an actual fresh run.
        import asyncio
        import sys
        from tests.test_bunshin_v2_verification import _FakeExecutionAdapter
        from pal.shared.tool_protocol import new_tool_call
        from pal.bunshin.verification_readiness import record_verification_execution, verification_corpus_snapshot
        call = new_tool_call(name="op_exec_shell", args={
            "cmd": f"{sys.executable} -B -m pytest -p no:cacheprovider tests/router/verifier/test_router.py",
            "cwd": str(self.workspace),
        })
        from pal.bunshin.semantic_evidence import run_shell_evidence, recorded_cases
        receipt_workspace["runtime_root"] = str(self.runtime_root)
        receipt_workspace["bunshin_v2"].update(context)
        receipt_workspace.pop("verification_case_revision", None)
        result = asyncio.run(run_shell_evidence(new_tool_call(
            name="op_bunshin_verification_run_diff_risk", call_id="fresh-semantic-case",
            args={"name": "candidate diff risk", "command": call.args["cmd"]}),
            workspace=receipt_workspace, original_adapter=_FakeExecutionAdapter(), draft_kind="verification",
            case_kind="diff_risk", obligation_tag="candidate_delta_review"))
        self.assertTrue(result.ok, result.llm_text)
        fresh = SubmissionDraftStore(self.runtime_root).read(SubmissionDraftContext.from_mapping(context))
        submission["recorded_results"] = recorded_cases(fresh.payload)
        submission["tool_receipts"] = receipt_workspace["review_tool_evidence_refs"]
        receipt = self.gateway.call(
            "draft_submit",
            {
                "access_token": token,
                "context": context,
                "expected_version": fresh.version,
                "expected_work_item_version": 0,
                "submission": submission,
            },
        )
        self.assertTrue(receipt["submitted"])
        accepted_assignment = self.service.repository.role_assignments.read_role_assignment(
            assignment_id
        )
        self.assertEqual(accepted_assignment["state"], "result_recorded")

    def test_manager_compiles_software_architecture_against_pinned_generation(
        self,
    ) -> None:
        self._assert_manager_compiles_architecture_generation(
            family_profile="software_engineering.v2_architect",
            specialization_id="software_engineering.v1",
            expect_build_authority=True,
        )

    def test_manager_compiles_generic_architecture_against_pinned_generation(
        self,
    ) -> None:
        self._assert_manager_compiles_architecture_generation(
            family_profile="general.generic",
            specialization_id="general.v1",
            expect_build_authority=False,
        )

    def test_software_contract_submit_rejects_workspace_errors_before_receipt(
        self,
    ) -> None:
        binding_ref = self.service.catalog.publish_family_binding(
            "software_engineering.v2_architect"
        )
        assignment, attempt, token, fence, resource = (
            self._create_architect_assignment(binding_ref.sha256)
        )
        definition = ArchitectureTemplateCompiler().compile(
            "software_engineering.v1"
        )
        context = {
            "workflow_id": "workflow-router",
            "invocation_id": attempt,
            "lease_resource_key": resource,
            "fencing_token": fence,
            "role": "architect",
            "mode": "author",
            "draft_kind": "contract",
            "input_fingerprint": "architecture-input",
            "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
        }

        def architect_call(method: str, **params):
            return self.gateway.call(
                method,
                {"access_token": token, **params},
            )

        architect_call("draft_read", context=context, seed={})
        submission = {
            "source": "architect.yaml",
            "architecture": definition.example,
        }
        projected = software_contract_projection(definition.example)
        contract_path = str(
            next(iter(projected["modules"].values()))["paths"][
                "contract_paths"
            ][0]
        )
        contract_file = self.workspace / contract_path
        original_contract = contract_file.read_text(encoding="utf-8")
        contract_file.unlink()

        with self.assertRaisesRegex(
            SubmissionValidationError,
            "contract (entrypoint|path) does not exist",
        ):
            architect_call(
                "draft_submit",
                context=context,
                expected_version=0,
                submission=submission,
            )
        self.assertEqual(
            self.service.repository.role_assignments.read_role_assignment(assignment)["state"],
            "running",
        )
        self.assertEqual(
            architect_call("draft_read", context=context, seed={})["snapshot"][
                "status"
            ],
            "active",
        )

        contract_file.write_text(original_contract, encoding="utf-8")
        duplicate_owner = copy.deepcopy(definition.example)
        duplicate_owner["modules"]["delivery"]["definition"]["paths"][
            "contract_paths"
        ] = [contract_path]
        with self.assertRaisesRegex(
            SubmissionValidationError,
            "contract path .* is owned by both",
        ):
            architect_call(
                "draft_submit",
                context=context,
                expected_version=0,
                submission={
                    "source": "architect.yaml",
                    "architecture": duplicate_owner,
                },
            )

        private_file = self.workspace / "src" / "architect_private.py"
        private_file.write_text("PRIVATE = True\n", encoding="utf-8")
        with self.assertRaisesRegex(
            SubmissionValidationError,
            "outside declared contract or implementation scopes",
        ):
            architect_call(
                "draft_submit",
                context=context,
                expected_version=0,
                submission=submission,
            )
        self.assertEqual(
            self.service.repository.role_assignments.read_role_assignment(assignment)["state"],
            "running",
        )
        self.assertEqual(
            architect_call("draft_read", context=context, seed={})["snapshot"][
                "status"
            ],
            "active",
        )

    def _assert_manager_compiles_architecture_generation(
        self,
        *,
        family_profile: str,
        specialization_id: str,
        expect_build_authority: bool,
    ) -> None:
        binding_ref = self.service.catalog.publish_family_binding(
            family_profile
        )
        assignment, attempt, token, fence, resource = (
            self._create_architect_assignment(binding_ref.sha256)
        )
        definition = ArchitectureTemplateCompiler().compile(specialization_id)
        contract_context = {
            "workflow_id": "workflow-router",
            "invocation_id": attempt,
            "lease_resource_key": resource,
            "fencing_token": fence,
            "role": "architect",
            "mode": "author",
            "draft_kind": "contract",
            "input_fingerprint": "architecture-input",
            "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
        }
        work_context = {**contract_context, "draft_kind": "work_items"}

        def architect_call(method: str, **params):
            return self.gateway.call(
                method,
                {"access_token": token, **params},
            )

        architect_call(
            "draft_mutate",
            context=work_context,
            operation_key="complete-design",
            request={"step": "design"},
            expected_version=0,
            next_payload={
                "items": [
                    {
                        "item_id": "phase:design",
                        "kind": "phase",
                        "status": "completed",
                        "summary": "design",
                        "ordinal": 0,
                        "origin": "role_playbook",
                        "required": True,
                    }
                ]
            },
            result={"updated": True},
            seed={
                "items": [
                    {
                        "item_id": "phase:design",
                        "kind": "phase",
                        "status": "pending",
                        "summary": "design",
                        "ordinal": 0,
                        "origin": "role_playbook",
                        "required": True,
                    }
                ]
            },
        )
        architect_call(
            "draft_read",
            context=contract_context,
            seed={},
        )
        with self.assertRaisesRegex(
            ValueError,
            "Manager-compiled family schema",
        ):
            architect_call(
                "draft_submit",
                context=contract_context,
                expected_version=0,
                submission={
                    "source": "architect.yaml",
                    "architecture": {
                        **definition.example,
                        "unexpected": True,
                    },
                },
            )

        receipt = architect_call(
            "draft_submit",
            context=contract_context,
            expected_version=0,
            submission={
                "source": "architect.yaml",
                "architecture": definition.example,
            },
        )
        stored = dict(
            self.service.artifacts.read_json(
                receipt["submission_artifact_ref"]
            )
        )
        self.assertEqual(stored["contract_schema"], specialization_id)
        self.assertEqual(stored["source"], "architect.yaml")
        self.assertNotIn("contract_schema", stored["contract"])
        # The authored architecture is revision 2 because revision 1 was
        # superseded before any graph was installed.  The first accepted graph
        # must nevertheless occupy GraphIR generation 1.
        self.assertEqual(stored["graph_ir"]["generation"], 1)
        graph = graph_ir_from_mapping(stored["graph_ir"])
        if expect_build_authority:
            self.assertIn(
                {"kind": "file", "path": "CMakeLists.txt"},
                graph.nodes["delivery"].workspace_policy[
                    "implementation_scopes"
                ],
            )
            self.assertNotIn(
                {"kind": "file", "path": "CMakeLists.txt"},
                graph.nodes["decoder"].workspace_policy[
                    "implementation_scopes"
                ],
            )
        WorkflowCoordinator(self.service.repository).install_graph(
            workflow_id="workflow-router",
            graph=graph,
        )
        self.assertEqual(
            self.service.repository.cycles.read_graph_generation(
                graph_id="workflow-router"
            ).generation,
            1,
        )
        self.assertEqual(
            self.service.repository.role_assignments.read_role_assignment(
                assignment
            )["state"],
            "result_recorded",
        )

    def test_role_artifact_store_round_trips_gateway_artifact_ref(self) -> None:
        owner = self

        class BoundGatewayClient:
            def request_sync(self, method: str, params: dict):
                return owner.gateway.call(
                    method,
                    {"access_token": owner.access_token, **params},
                )

        store = RoleGatewayArtifactStore(BoundGatewayClient())
        ref = store.put_bytes(
            b"compiler output\n",
            artifact_type="VerificationStdoutArtifact",
            media_type="text/plain",
        )

        self.assertEqual(
            self.service.artifacts.read_bytes(ref),
            b"compiler output\n",
        )
        self.assertEqual(ref.artifact_type, "VerificationStdoutArtifact")
        self.assertTrue(ref.durable)

    def test_submission_does_not_require_input_read_receipts(self) -> None:
        first = self.call("draft_read", context=self.context, seed={"checks": []})
        self.assertEqual(first["snapshot"]["status"], "active")

        accepted = self.call(
            "draft_submit",
            context=self.context,
            expected_version=0,
            submission={"status": "candidate_ready", "checks": []},
        )
        self.assertTrue(accepted["submitted"])
        self.assertTrue(self.call("submission_status")["recorded"])

    def test_gateway_rejects_context_from_another_attempt(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.call(
                "draft_read",
                context={**self.context, "invocation_id": "attempt-another"},
                seed={},
            )

    def test_gateway_has_a_closed_method_allowlist(self) -> None:
        for method in (
            "v2_task_status",
            "task_revision_authority_record",
            "task_revision_append",
        ):
            with self.subTest(method=method), self.assertRaisesRegex(
                ValueError,
                "not allowed",
            ):
                self.call(method)

    def test_git_read_delegates_to_real_git_within_the_assigned_workspace(self) -> None:
        result = self.call(
            "git_read",
            cmd="status --short",
            cwd=str(self.workspace),
        )

        self.assertEqual(result["returncode"], 0)
        self.assertIn("src/router", result["stdout"])
        self.assertNotIn("README.md", result["stdout"])
        self.assertEqual(result["classification"]["operation_kind"], "read")

    def _bind_git_scope_workspace(self, **values) -> None:
        prompt = self.service.artifacts.read_json(self.prompt_ref.to_dict())
        prompt["workspace"].update(values)
        self.prompt_ref = self.service.artifacts.put_json(
            prompt, artifact_type="RolePromptPackArtifact"
        )
        with self.service.repository.database.write_connection() as connection:
            connection.execute(
                "UPDATE bunshin_v2_role_attempts SET prompt_pack_ref_json = ? WHERE attempt_id = ?",
                (json.dumps(self.prompt_ref.to_dict()), self.attempt_id),
            )

    def _seed_git_files(self, paths: list[str]) -> None:
        for path in paths:
            target = self.workspace / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("original\n", encoding="utf-8")
        for args in (
            ["add", "-A"],
            ["-c", "user.name=Pal Tests", "-c", "user.email=pal-tests@example.invalid",
             "commit", "-qm", "seed scope"],
        ):
            subprocess.run(["git", *args], cwd=self.workspace, check=True, capture_output=True)

    def test_git_read_keeps_semicolon_overlay_as_one_literal_operand(self) -> None:
        overlay = "inputs/INPUTREFERENCE;touch SCOPED_SENTINEL"
        target = f"{overlay}/reference.txt"
        self._bind_git_scope_workspace(read_only_overlay_paths=[overlay])
        self._seed_git_files([target])
        (self.workspace / target).write_text("changed\n", encoding="utf-8")
        for command in ("status --short", "diff --name-only HEAD", "ls-files"):
            with self.subTest(command=command), patch(
                "pal.execution.git_tool.subprocess.run", wraps=subprocess.run
            ) as run:
                result = self.call("git_read", cmd=command)
                self.assertEqual(result["returncode"], 0)
                self.assertIn("INPUTREFERENCE;touch SCOPED_SENTINEL", result["stdout"])
                self.assertNotIn("README.md", result["stdout"])
                self.assertEqual(result["classification"]["raw"], command)
                argv = run.call_args.args[0]
                expected = list(classify_git_command(command).tokens)
                if expected[0] == "diff":
                    expected[1:1] = ["--no-ext-diff", "--no-textconv"]
                self.assertEqual(argv, [
                    "git", "--no-pager", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                    "--git-dir", str(self.workspace / ".git"), "--work-tree", str(self.workspace),
                    *expected, "--", ":(top,literal)include/codec.hpp",
                    ":(top,literal)include/router.hpp", f":(top,literal){overlay}",
                    ":(top,literal)src/router",
                ])
                self.assertEqual(argv.count("--"), 1)
                self.assertFalse(run.call_args.kwargs.get("shell", False))
        self.assertFalse((self.workspace / "SCOPED_SENTINEL").exists())

    def test_git_read_preserves_literal_overlay_names_and_authorized_globs(self) -> None:
        overlays = ["literal*.txt", "literal?.txt", "literal[ab].txt", ":(glob)**",
                    "quote'\" spaces.txt", " whitespace "]
        glob_files = ["patterns/a.txt", "patterns/deep/b.txt"]
        canaries = ["literalX.txt", "literala.txt", "outside_canary.txt"]
        self._bind_git_scope_workspace(
            read_only_overlay_paths=overlays, write_path_scopes=["patterns/*.txt"]
        )
        self._seed_git_files([*overlays, *glob_files, *canaries])
        result = self.call("git_read", cmd="ls-files -z")
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(set(result["stdout"].split("\0")) - {""},
                         {*overlays, *glob_files, "src/router/router.py"})
        for command in ("ls-files -- literal*.txt", "ls-files -- literal?.txt",
                        "ls-files -- literal[ab].txt", "ls-files -- ':(glob)**'"):
            with self.subTest(command=command), patch("pal.execution.git_tool._run_git") as run:
                with self.assertRaisesRegex(ValueError, "outside"):
                    self.call("git_read", cmd=command)
                run.assert_not_called()

    def test_git_read_option_shaped_manager_filename_never_becomes_an_option(self) -> None:
        self._bind_git_scope_workspace(read_only_overlay_paths=["--output"])
        self._seed_git_files(["--output"])
        with patch("pal.execution.git_tool.subprocess.run", wraps=subprocess.run) as run:
            result = self.call("git_read", cmd="ls-files -z --")
            argv = run.call_args.args[0]
            self.assertEqual(argv.count("--"), 1)
            self.assertIn(":(top,literal)--output", argv[argv.index("--") + 1:])
        self.assertIn("--output", result["stdout"].split("\0"))
        for command in ("diff --output=SCOPED_SENTINEL", "diff --output SCOPED_SENTINEL",
                        "ls-files -- --output", "status --short; touch SCOPED_SENTINEL",
                        "status --short && touch SCOPED_SENTINEL", "ls-files 'semi;colon'",
                        "ls-files `touch SCOPED_SENTINEL`", "ls-files $(touch SCOPED_SENTINEL)"):
            with self.subTest(command=command), patch("pal.execution.git_tool._run_git") as run:
                with self.assertRaisesRegex(ValueError, "only classified read-only"):
                    self.call("git_read", cmd=command)
                result = GitTool().invoke({"cmd": command, "cwd": str(self.workspace)})
                self.assertEqual(result.structured["error_code"], "GIT_COMMAND_BLOCKED")
                run.assert_not_called()
        self.assertFalse((self.workspace / "SCOPED_SENTINEL").exists())

    def test_git_read_anchors_manager_paths_from_descendant_cwd(self) -> None:
        self._bind_git_scope_workspace(read_only_overlay_paths=["inputs/literal*.txt"])
        self._seed_git_files([
            "inputs/literal*.txt", "nested/inputs/literal*.txt",
            "nested/src/router/canary.txt",
        ])
        nested = self.workspace / "nested"
        result = self.call("git_read", cmd="ls-files --full-name -z", cwd=str(nested))
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(set(result["stdout"].split("\0")) - {""},
                         {"inputs/literal*.txt", "src/router/router.py"})
        for command in ("ls-files -- src/router", "ls-files src/router"):
            with self.subTest(command=command), patch("pal.execution.git_tool._run_git") as run:
                with self.assertRaisesRegex(ValueError, "outside"):
                    self.call("git_read", cmd=command, cwd=str(nested))
                run.assert_not_called()
        allowed = self.call("git_read", cmd="ls-files -- router.py", cwd=str(self.workspace / "src/router"))
        self.assertEqual(allowed["stdout"].strip(), "router.py")
        identity = self.call("git_read", cmd="rev-parse --show-prefix", cwd=str(nested))
        self.assertEqual(identity["stdout"].strip(), "nested/")

    def test_git_read_preserves_explicit_filters_and_empty_delimiter(self) -> None:
        self._seed_git_files(["include/codec.hpp"])
        (self.workspace / "src/router/router.py").write_text("changed\n", encoding="utf-8")
        (self.workspace / "include/codec.hpp").write_text("changed\n", encoding="utf-8")
        for command in ("ls-files src/router/router.py", "ls-files -- src/router/router.py",
                        "status --short src/router/router.py", "status --short -- src/router/router.py"):
            with self.subTest(command=command), patch("pal.execution.git_tool._run_git", wraps=_run_git) as run:
                result = self.call("git_read", cmd=command)
                self.assertEqual(result["returncode"], 0)
                self.assertIn("src/router/router.py", result["stdout"])
                self.assertNotIn("codec.hpp", result["stdout"])
                self.assertEqual(run.call_args.args[0], classify_git_command(command).tokens)
        for command in ("ls-files --", "status --short --", "diff --name-only HEAD --"):
            with self.subTest(command=command), patch("pal.execution.git_tool.subprocess.run", wraps=subprocess.run) as run:
                result = self.call("git_read", cmd=command)
                self.assertEqual(result["returncode"], 0)
                self.assertEqual(run.call_args.args[0].count("--"), 1)
                self.assertIn("codec.hpp", result["stdout"])

    def test_scoped_git_read_preserves_hardened_process_environment(self) -> None:
        with patch.dict(os.environ, {"GIT_DIR": "/untrusted", "GIT_CONFIG_COUNT": "1",
                                     "GIT_CONFIG_KEY_0": "core.fsmonitor", "GIT_CONFIG_VALUE_0": "untrusted"}), patch(
            "pal.execution.git_tool.subprocess.run", wraps=subprocess.run
        ) as run:
            self.assertEqual(self.call("git_read", cmd="status --short")["returncode"], 0)
        env = run.call_args.kwargs["env"]
        for name in ("GIT_DIR", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"):
            self.assertNotIn(name, env)
        self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        self.assertEqual(env["GIT_CONFIG_SYSTEM"], os.devnull)
        self.assertEqual(env["GIT_OPTIONAL_LOCKS"], "0")

    def test_git_read_rejects_invalid_manager_operands_before_spawning(self) -> None:
        for path in ("", "/outside", "../README.md", "./src/router", "src//router",
                     "src/router/", "src/../README.md", "src\\router", "C:/outside",
                     "C:relative", "input\x00reference"):
            with self.subTest(path=path), patch("pal.execution.git_tool._run_git") as run:
                self._bind_git_scope_workspace(read_only_overlay_paths=[path])
                with self.assertRaisesRegex(ValueError, "invalid relative path"):
                    self.call("git_read", cmd="ls-files")
                run.assert_not_called()

    def test_git_read_scope_and_revision_rejections_do_not_spawn(self) -> None:
        for command in ("ls-files -- README.md", "ls-files -- ':(top)README.md'",
                        "ls-files -- ':(exclude)src/router'", "show HEAD:README.md",
                        "show main", "blame HEAD --", "blame --",
                        "blame HEAD -- src/router/router.py include/router.hpp"):
            with self.subTest(command=command), patch("pal.execution.git_tool._run_git") as run:
                with self.assertRaises(ValueError):
                    self.call("git_read", cmd=command)
                run.assert_not_called()
        with patch("pal.execution.git_tool._run_git") as run:
            with self.assertRaisesRegex(ValueError, "outside the assigned repository"):
                self.call("git_read", cmd="ls-files", cwd=str(self.runtime_root))
            run.assert_not_called()

    def test_git_read_complete_repo_scope_works_from_root_and_descendant(self) -> None:
        self._bind_git_scope_workspace(workspace_policy={"mode": "read_only_repo"})
        self._seed_git_files(["nested/reviewer.txt"])
        for cwd in (self.workspace, self.workspace / "nested"):
            with self.subTest(cwd=cwd):
                result = self.call("git_read", cmd="ls-files --full-name -z", cwd=str(cwd))
                self.assertEqual(result["returncode"], 0)
                self.assertEqual(set(result["stdout"].split("\0")) - {""},
                                 {"README.md", "src/router/router.py", "nested/reviewer.txt"})

    def test_git_read_pins_outer_repository_even_inside_nested_repository(self) -> None:
        self._seed_git_files([])
        nested = self.workspace / "nested"
        (nested / "src/router").mkdir(parents=True)
        (nested / "src/router/canary.txt").write_text("OUTSIDE_CANARY\n", encoding="utf-8")
        for args in (["init", "-b", "nested-canary"], ["add", "-A"]):
            subprocess.run(["git", *args], cwd=nested, check=True, capture_output=True)
        result = self.call("git_read", cmd="ls-files --full-name -z", cwd=str(nested))
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"], "src/router/router.py\0")
        for command, expected in (("rev-parse --show-toplevel", str(self.workspace)),
                                  ("rev-parse --show-prefix", "nested/"),
                                  ("branch --show-current", "main")):
            self.assertEqual(self.call("git_read", cmd=command, cwd=str(nested))["stdout"].strip(), expected)

    def test_git_read_pins_linked_worktree_identity(self) -> None:
        self._seed_git_files([])
        linked = self.runtime_root / "linked"
        subprocess.run(["git", "worktree", "add", "-b", "linked-branch", str(linked)],
                       cwd=self.workspace, check=True, capture_output=True)
        self.assertTrue((linked / ".git").is_file())
        self._bind_git_scope_workspace(repo_path=str(linked))
        cwd = linked / "src/router"
        result = self.call("git_read", cmd="ls-files --full-name -z", cwd=str(cwd))
        self.assertEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"], "src/router/router.py\0")
        self.assertEqual(self.call("git_read", cmd="branch --show-current", cwd=str(cwd))["stdout"].strip(), "linked-branch")
        self.assertEqual(self.call("git_read", cmd="rev-parse --show-prefix", cwd=str(cwd))["stdout"].strip(), "src/router/")

    def test_git_read_missing_backing_identity_does_not_fall_back_to_another_repo(self) -> None:
        self._seed_git_files([])
        missing = self.workspace / "unbound"
        nested = missing / "nested"
        (nested / "src/router").mkdir(parents=True)
        (nested / "src/router/canary.txt").write_text("OUTSIDE_CANARY\n", encoding="utf-8")
        for args in (["init", "-b", "nested-canary"], ["add", "-A"]):
            subprocess.run(["git", *args], cwd=nested, check=True, capture_output=True)
        self._bind_git_scope_workspace(repo_path=str(missing))
        for cwd in (missing, nested):
            result = self.call("git_read", cmd="ls-files", cwd=str(cwd))
            self.assertNotEqual(result["returncode"], 0)
            self.assertEqual(result["stdout"], "")
        (missing / ".git").write_text("gitdir: missing-git-directory\n", encoding="utf-8")
        result = self.call("git_read", cmd="branch --show-current", cwd=str(nested))
        self.assertNotEqual(result["returncode"], 0)
        self.assertEqual(result["stdout"], "")

    def test_git_read_explicit_globs_require_proven_scope_containment(self) -> None:
        self._bind_git_scope_workspace(write_path_scopes=["data/?.txt", "brackets/[ab].txt"])
        self._seed_git_files(["data/a.txt", "data/long.txt", "brackets/a.txt", "brackets/other.txt"])
        for command, cwd in (("ls-files -- data/*.txt", self.workspace),
                             ("ls-files -- '*.txt'", self.workspace / "data")):
            with self.subTest(command=command, cwd=cwd), patch("pal.execution.git_tool._run_git") as run:
                with self.assertRaisesRegex(ValueError, "outside"):
                    self.call("git_read", cmd=command, cwd=str(cwd))
                run.assert_not_called()
        for command, expected in (("ls-files -- data/?.txt", "data/a.txt"),
                                  ("ls-files -- data/a.txt", "data/a.txt"),
                                  ("ls-files -- brackets/[ab].txt", "brackets/a.txt"),
                                  ("ls-files -- src/router/*.py", "src/router/router.py")):
            result = self.call("git_read", cmd=command)
            self.assertEqual(result["returncode"], 0)
            self.assertEqual(result["stdout"].strip(), expected)
        result = self.call("git_read", cmd="ls-files -- '?.txt'", cwd=str(self.workspace / "data"))
        self.assertEqual(result["stdout"].strip(), "a.txt")

    def test_git_read_grep_requires_worker_pattern_before_scope_operands(self) -> None:
        self._seed_git_files(["outside.txt", "src/router/patterns.txt"])
        (self.workspace / "src/router/patterns.txt").write_text("VALUE\n", encoding="utf-8")
        (self.workspace / "outside.txt").write_text(":(top,literal)src/router OUTSIDE_CANARY\n", encoding="utf-8")
        for command in ("grep", "grep --", "grep -n --", "grep -e", "grep --regexp", "grep --file"):
            with self.subTest(command=command), patch("pal.execution.git_tool._run_git") as run:
                with self.assertRaisesRegex(ValueError, "requires"):
                    self.call("git_read", cmd=command)
                run.assert_not_called()
        for command in ("grep VALUE", "grep -e VALUE --", "grep -e ''",
                        "grep -fsrc/router/patterns.txt"):
            result = self.call("git_read", cmd=command)
            self.assertEqual(result["returncode"], 0)
            self.assertIn("src/router/router.py", result["stdout"])
            self.assertNotIn("outside.txt", result["stdout"])
        for command in ("grep --file src/router/patterns.txt", "grep --regexp="):
            result = self.call("git_read", cmd=command)
            native = subprocess.run(["git", *classify_git_command(command).tokens, "--", "src/router"],
                                    cwd=self.workspace, text=True, capture_output=True)
            self.assertEqual(result["returncode"], native.returncode)
            self.assertNotIn("outside.txt", result["stdout"])
        for command in ("grep --file outside.txt", "grep -foutside.txt",
                        "ls-files -X /outside", "ls-files -X/outside"):
            with self.subTest(command=command), patch("pal.execution.git_tool._run_git") as run:
                with self.assertRaisesRegex(ValueError, "outside"):
                    self.call("git_read", cmd=command)
                run.assert_not_called()
        for command in ("grep -nfoutside.txt -e VALUE", "ls-files -zXoutside.txt",
                        "blame -nSoutside.txt src/router/router.py"):
            with self.subTest(command=command), patch("pal.execution.git_tool._run_git") as run:
                with self.assertRaisesRegex(ValueError, "clustered"):
                    self.call("git_read", cmd=command)
                run.assert_not_called()
        self.assertEqual(self.call("git_read", cmd="grep -in VALUE")["returncode"], 0)
        # A regex value beginning with a file-option spelling is still data.
        for pattern in ("-foutside.txt", "-nfoutside"):
            self.assertEqual(self.call("git_read", cmd=f"grep -e '{pattern}'")["returncode"], 1)

    def test_git_read_option_values_equal_to_delimiter_keep_their_grammar(self) -> None:
        self._seed_git_files(["outside.txt"])
        (self.workspace / "src/router/router.py").write_text("--\n", encoding="utf-8")
        (self.workspace / "outside.txt").write_text("-- OUTSIDE_CANARY\n", encoding="utf-8")
        for command in ("grep -e '--'", "grep -e '--' --", "diff -S '--' HEAD", "diff -S '--' HEAD --"):
            with self.subTest(command=command), patch("pal.execution.git_tool._run_git", wraps=_run_git) as run:
                result = self.call("git_read", cmd=command)
                native = subprocess.run(["git", *classify_git_command(command).tokens],
                                        cwd=self.workspace, text=True, capture_output=True)
                self.assertEqual(result["returncode"], native.returncode)
                if result["returncode"] == 0:
                    self.assertIn("src/router/router.py", result["stdout"])
                self.assertNotIn("outside.txt", result["stdout"])
                tokens = run.call_args.args[0]
                self.assertEqual(tokens.count("--"), 2)
                self.assertEqual(tokens[tokens.index("--") - 1], "-e" if command.startswith("grep") else "-S")
        self.assertEqual(self.call("git_read", cmd="diff -S-- HEAD")["returncode"], 0)
        self.assertEqual(self.call("git_read", cmd="log --pretty --")["returncode"], 0)
        for command in ("grep -e VALUE -A", "ls-files --exclude-from", "diff -S", "log --max-count"):
            with self.subTest(command=command), patch("pal.execution.git_tool._run_git") as run:
                with self.assertRaisesRegex(ValueError, "requires a value"):
                    self.call("git_read", cmd=command)
                run.assert_not_called()

    def test_git_read_does_not_confuse_python_and_git_glob_grammars(self) -> None:
        self._bind_git_scope_workspace(write_path_scopes=["digits/[[:digit:]].txt", "negation/[^a].txt", "unicode/?.txt"])
        self._seed_git_files(["digits/1.txt", "digits/d].txt", "negation/a.txt", "negation/b.txt",
                              "unicode/a.txt", "unicode/é.txt", "unicode/😀.txt"])
        result = self.call("git_read", cmd="ls-files -z")
        self.assertEqual(set(result["stdout"].split("\0")) - {""},
                         {"src/router/router.py", "digits/1.txt", "negation/b.txt", "unicode/a.txt"})
        for path in ("digits/d].txt", "negation/a.txt", "unicode/é.txt", "unicode/😀.txt"):
            with self.subTest(path=path), patch("pal.execution.git_tool._run_git") as run:
                with self.assertRaisesRegex(ValueError, "outside"):
                    self.call("git_read", cmd=f"ls-files -- {path}")
                run.assert_not_called()
        for pattern, expected in (("digits/[[:digit:]].txt", "digits/1.txt"),
                                  ("negation/[^a].txt", "negation/b.txt")):
            result = self.call("git_read", cmd=f"ls-files -- {pattern}")
            self.assertEqual(result["stdout"].strip(), expected)

    def test_git_read_accepts_safe_option_values_without_treating_them_as_revisions(self) -> None:
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Pal Tests",
                "-c",
                "user.email=pal-tests@example.invalid",
                "add",
                "src/router/router.py",
            ],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Pal Tests",
                "-c",
                "user.email=pal-tests@example.invalid",
                "commit",
                "-m",
                "seed router",
            ],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        blame = self.call(
            "git_read",
            cmd="blame -L 1,1 src/router/router.py",
            cwd=str(self.workspace),
        )
        blame_with_separator = self.call(
            "git_read",
            cmd="blame HEAD -- src/router/router.py",
            cwd=str(self.workspace),
        )
        grep = self.call(
            "git_read",
            cmd="grep -e router -e definitely_missing_pattern",
            cwd=str(self.workspace),
        )

        self.assertEqual(blame["returncode"], 0)
        self.assertEqual(blame_with_separator["returncode"], 0)
        self.assertIn(grep["returncode"], {0, 1})

    def test_git_read_allows_only_current_branch_identity(self) -> None:
        current = self.call(
            "git_read",
            cmd="branch --show-current",
            cwd=str(self.workspace),
        )

        self.assertEqual(current["returncode"], 0)
        self.assertEqual(current["stdout"].strip(), "main")
        self.assertEqual(current["classification"]["operation_kind"], "read")
        for command in ("branch", "branch --all", "branch --list"):
            with self.subTest(command=command), self.assertRaisesRegex(
                ValueError,
                "current workspace identity",
            ):
                self.call("git_read", cmd=command, cwd=str(self.workspace))

    def test_git_read_is_limited_to_candidate_range_and_module_paths(self) -> None:
        for command in (
            "show main",
            "diff -- README.md",
            "show HEAD:README.md",
            "blame -S /etc/passwd src/router/router.py",
            "blame --ignore-revs-file=/etc/passwd src/router/router.py",
            "ls-files --exclude-from /etc/passwd",
            "ls-files --exclude-per-directory .gitignore",
        ):
            with self.subTest(command=command), self.assertRaisesRegex(
                ValueError,
                "outside|enumeration|archaeology",
            ):
                self.call("git_read", cmd=command, cwd=str(self.workspace))

        result = self.call(
            "git_read",
            cmd="diff -- src/router/router.py",
            cwd=str(self.workspace),
        )
        self.assertEqual(result["classification"]["operation_kind"], "read")

        log = self.call(
            "git_read",
            cmd="log --all --oneline -5",
            cwd=str(self.workspace),
        )
        self.assertEqual(log["classification"]["operation_kind"], "read")
        rev_list = self.call(
            "git_read",
            cmd="rev-list --all",
            cwd=str(self.workspace),
        )
        self.assertEqual(rev_list["classification"]["operation_kind"], "read")

    def test_git_read_rejects_mutations_unknown_commands_and_out_of_scope_cwd(self) -> None:
        for command in ("restore -- README.md", "commit -am nope", "frobnicate"):
            with self.subTest(command=command), self.assertRaisesRegex(
                ValueError,
                "only classified read-only Git commands",
            ):
                self.call("git_read", cmd=command, cwd=str(self.workspace))

        outside = self.runtime_root / "outside"
        outside.mkdir()
        with self.assertRaisesRegex(ValueError, "outside the assigned repository"):
            self.call("git_read", cmd="status --short", cwd=str(outside))

    def test_git_read_fails_closed_without_authenticated_paths_or_with_bad_artifact(self) -> None:
        policy = classify_git_command("status --short")
        with self.assertRaisesRegex(ValueError, "no Manager-authenticated"):
            scoped_role_git_read_plan(
                prompt_pack={"workspace": {"repo_path": str(self.workspace)}},
                assignment={"input_refs": {}},
                artifact_reader=lambda _ref: {},
                policy=policy,
                repository_root=self.workspace,
            )

        with self.assertRaisesRegex(ValueError, "unavailable or invalid"):
            scoped_role_git_read_plan(
                prompt_pack={"workspace": {"repo_path": str(self.workspace)}},
                assignment={
                    "input_refs": {
                        "module_work_view": {"sha256": "missing" * 9 + "x"}
                    }
                },
                artifact_reader=lambda _ref: (_ for _ in ()).throw(KeyError("missing")),
                policy=policy,
                repository_root=self.workspace,
            )

    def test_git_read_uses_complete_candidate_for_read_only_repo_reviewer(self) -> None:
        policy = classify_git_command("status --short")
        command = scoped_role_git_read_plan(
            prompt_pack={
                "workspace": {
                    "repo_path": str(self.workspace),
                    "workspace_policy": {"mode": "read_only_repo"},
                }
            },
            assignment={"input_refs": {}},
            artifact_reader=lambda _ref: {},
            policy=policy,
            repository_root=self.workspace,
        )

        self.assertEqual(command.tokens, ("status", "--short", "--", ":(top,literal)"))

    def _create_architect_assignment(
        self,
        family_binding_sha: str,
    ) -> tuple[str, str, str, int, str]:
        # Mirror the real Manager hand-off: a software Architect starts from a
        # pinned Git base plus immutable workspace/task snapshot references.
        software_example = software_contract_projection(
            ArchitectureTemplateCompiler().compile(
                "software_engineering.v1"
            ).example
        )
        for module in dict(software_example.get("modules") or {}).values():
            paths = dict(dict(module).get("paths") or {})
            for relative in [
                *list(paths.get("contract_paths") or []),
                *list(paths.get("reference_only") or []),
            ]:
                target = self.workspace / str(relative)
                target.parent.mkdir(parents=True, exist_ok=True)
                if not target.exists():
                    target.write_text("// architecture contract\n", encoding="utf-8")
        subprocess.run(
            ["git", "config", "user.name", "Test"],
            cwd=self.workspace,
            check=True,
        )
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"],
            cwd=self.workspace,
            check=True,
        )
        subprocess.run(["git", "add", "-A"], cwd=self.workspace, check=True)
        subprocess.run(
            ["git", "commit", "-qm", "architecture base"],
            cwd=self.workspace,
            check=True,
        )
        base_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        requirements_ref = self.service.task_ledger.publish(
            title="Gateway architecture",
            task_spec={"objective": "Compile the architecture"},
            actor="test",
            source_channel="test",
        )
        workspace_snapshot_ref = self.service.artifacts.put_json(
            {"original_head": base_sha},
            artifact_type="WorkspaceSnapshotArtifact",
        )
        architecture_revision_id = "architecture-router-revision-2"
        self.service.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=architecture_revision_id,
                actor="test",
                expected_version=0,
                payload={
                    "revision_number": 2,
                    "architecture_workspace_path": str(self.workspace),
                    "architecture_base_sha": base_sha,
                    "requirements_ref": requirements_ref.to_dict(),
                    "workspace_snapshot_ref": workspace_snapshot_ref.to_dict(),
                },
            )
        )
        session_id = "session-architect"
        self.service.repository.role_sessions.ensure_role_session(
            session_id=session_id,
            workflow_id="workflow-router",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id=architecture_revision_id,
            role="architect",
            mode="author",
            role_profile_id="software_engineering.v2_architect",
            family_binding_sha=family_binding_sha,
            scope_kind=AggregateType.ARCHITECTURE_REVISION.value,
            subject_key=architecture_revision_id,
        )
        assignment = self.service.repository.role_assignments.create_role_assignment(
            RoleAssignmentRequest(
                assignment_key="architecture-cycle-1",
                session_id=session_id,
                workflow_id="workflow-router",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION.value,
                aggregate_id=architecture_revision_id,
                role="architect",
                mode="author",
                role_profile_id="software_engineering.v2_architect",
                family_binding_sha=family_binding_sha,
                input_fingerprint="architecture-input",
                required_inputs=(),
                input_refs={},
                execution_spec={"effect_type": "run_architecture_role"},
                submission_kind="contract",
            )
        )
        assignment_id = str(assignment["assignment_id"])
        attempt = self.service.repository.role_assignments.claim_role_assignment(
            assignment_id
        )
        attempt_id = str(attempt["attempt_id"])
        resource = f"assignment:{assignment_id}"
        fence = self.service.repository.leases.claim_lease(
            resource,
            attempt_id,
            ttl_seconds=120,
        ).fencing_token
        prompt_ref = self.service.artifacts.put_json(
            {
                "workspace": {"repo_path": str(self.workspace)},
                "metadata": {
                    "bunshin_v2": {
                        "workflow_id": "workflow-router",
                        "invocation_id": attempt_id,
                        "lease_resource_key": resource,
                        "fencing_token": fence,
                        "role": "architect",
                        "mode": "author",
                        "authoring_input_fingerprint": "architecture-input",
                        "authoring_contract_version": (
                            AUTHORING_CONTRACT_VERSION
                        ),
                        "work_item_seed": [
                            {
                                "kind": "phase",
                                "summary": "design",
                                "status": "pending",
                                "required": True,
                            }
                        ],
                    }
                },
            },
            artifact_type="RolePromptPackArtifact",
        )
        self.service.repository.role_attempts.start_role_attempt(
            assignment_id=assignment_id,
            attempt_id_value=attempt_id,
            lease_resource_key=resource,
            fencing_token=fence,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        token = self.service.repository.role_access.issue_role_attempt_access_token(
            assignment_id=assignment_id,
            attempt_id_value=attempt_id,
            fencing_token=fence,
        )
        return assignment_id, attempt_id, token, fence, resource

if __name__ == "__main__":
    unittest.main()
