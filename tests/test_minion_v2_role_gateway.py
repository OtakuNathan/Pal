from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import subprocess

from pal.minion.v2.architecture_templates import ArchitectureTemplateCompiler
from pal.minion.v2.contracts import ActionEnvelope, AggregateType
from pal.minion.v2.graph_protocol import graph_ir_from_mapping
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.submission_drafts import AUTHORING_CONTRACT_VERSION
from pal.minion.v2.role_gateway import (
    RoleAssignmentGateway,
    RoleGatewayArtifactStore,
)
from pal.minion.v2.role_protocol import RoleAssignmentRequest
from pal.minion.v2.workflow_runtime import WorkflowCoordinator


class MinionV2RoleGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal-v2-worker-gateway-"))
        self.service = MinionV2WorkflowService(self.runtime_root)
        self.gateway = RoleAssignmentGateway(self.service)
        self.workspace = self.runtime_root / "workspace"
        self.workspace.mkdir()
        subprocess.run(
            ["git", "init"],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            check=True,
        )
        (self.workspace / "README.md").write_text("gateway\n", encoding="utf-8")
        self.input_ref = self.service.artifacts.put_json(
            {"module": "router", "contract": "route deterministically"},
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
        self.service.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="workflow-router",
                actor="test",
                expected_version=0,
            )
        )
        self.service.repository.dispatch(
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
        self.service.repository.ensure_role_session(
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
        assignment = self.service.repository.create_role_assignment(
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
        attempt = self.service.repository.claim_role_assignment(self.assignment_id)
        self.attempt_id = attempt["attempt_id"]
        self.lease_resource = f"assignment:{self.assignment_id}"
        lease = self.service.repository.claim_lease(
            self.lease_resource,
            self.attempt_id,
            ttl_seconds=120,
        )
        self.fencing_token = lease.fencing_token
        self.service.repository.start_role_attempt(
            assignment_id=self.assignment_id,
            attempt_id_value=self.attempt_id,
            lease_resource_key=self.lease_resource,
            fencing_token=self.fencing_token,
            prompt_pack_ref=self.prompt_ref.to_dict(),
        )
        self.access_token = self.service.repository.issue_role_attempt_access_token(
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
            "execution_reconcile_projection",
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
        assignment = self.service.repository.read_role_assignment(self.assignment_id)
        self.assertEqual(assignment["state"], "result_recorded")
        self.assertTrue(assignment["submission_artifact_ref"]["sha256"])
        self.assertTrue(self.call("submission_status")["recorded"])

    def test_manager_compiles_architect_yaml_against_pinned_family_generation(
        self,
    ) -> None:
        binding_ref = self.service.catalog.publish_family_binding(
            "general.generic"
        )
        assignment, attempt, token, fence, resource = (
            self._create_architect_assignment(binding_ref.sha256)
        )
        definition = ArchitectureTemplateCompiler().compile("general.v1")
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
        self.assertEqual(stored["contract_schema"], "general.v1")
        self.assertEqual(stored["source"], "architect.yaml")
        self.assertNotIn("contract_schema", stored["contract"])
        # The authored architecture is revision 2 because revision 1 was
        # superseded before any graph was installed.  The first accepted graph
        # must nevertheless occupy GraphIR generation 1.
        self.assertEqual(stored["graph_ir"]["generation"], 1)
        graph = graph_ir_from_mapping(stored["graph_ir"])
        WorkflowCoordinator(self.service.repository).install_graph(
            workflow_id="workflow-router",
            graph=graph,
        )
        self.assertEqual(
            self.service.repository.read_graph_generation(
                graph_id="workflow-router"
            ).generation,
            1,
        )
        self.assertEqual(
            self.service.repository.read_role_assignment(
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
        self.assertEqual(result["stdout"].strip(), "?? README.md")
        self.assertEqual(result["classification"]["operation_kind"], "read")

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

    def _create_architect_assignment(
        self,
        family_binding_sha: str,
    ) -> tuple[str, str, str, int, str]:
        architecture_revision_id = "architecture-router-revision-2"
        self.service.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=architecture_revision_id,
                actor="test",
                expected_version=0,
                payload={"revision_number": 2},
            )
        )
        session_id = "session-architect"
        self.service.repository.ensure_role_session(
            session_id=session_id,
            workflow_id="workflow-router",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id=architecture_revision_id,
            role="architect",
            mode="author",
            role_profile_id="general.architect",
            family_binding_sha=family_binding_sha,
            scope_kind=AggregateType.ARCHITECTURE_REVISION.value,
            subject_key=architecture_revision_id,
        )
        assignment = self.service.repository.create_role_assignment(
            RoleAssignmentRequest(
                assignment_key="architecture-cycle-1",
                session_id=session_id,
                workflow_id="workflow-router",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION.value,
                aggregate_id=architecture_revision_id,
                role="architect",
                mode="author",
                role_profile_id="general.architect",
                family_binding_sha=family_binding_sha,
                input_fingerprint="architecture-input",
                required_inputs=(),
                input_refs={},
                execution_spec={"effect_type": "run_architecture_role"},
                submission_kind="contract",
            )
        )
        assignment_id = str(assignment["assignment_id"])
        attempt = self.service.repository.claim_role_assignment(
            assignment_id
        )
        attempt_id = str(attempt["attempt_id"])
        resource = f"assignment:{assignment_id}"
        fence = self.service.repository.claim_lease(
            resource,
            attempt_id,
            ttl_seconds=120,
        ).fencing_token
        prompt_ref = self.service.artifacts.put_json(
            {
                "workspace": {"repo_path": str(self.workspace)},
                "metadata": {
                    "minion_v2": {
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
        self.service.repository.start_role_attempt(
            assignment_id=assignment_id,
            attempt_id_value=attempt_id,
            lease_resource_key=resource,
            fencing_token=fence,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        token = self.service.repository.issue_role_attempt_access_token(
            assignment_id=assignment_id,
            attempt_id_value=attempt_id,
            fencing_token=fence,
        )
        return assignment_id, attempt_id, token, fence, resource

if __name__ == "__main__":
    unittest.main()
