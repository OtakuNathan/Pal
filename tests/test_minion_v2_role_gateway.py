from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import subprocess

from pal.minion.v2.contracts import ActionEnvelope, AggregateType
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.submission_drafts import AUTHORING_CONTRACT_VERSION
from pal.minion.v2.role_gateway import RoleAssignmentGateway
from pal.minion.v2.role_protocol import RoleAssignmentRequest
from pal.minion.v2.task_ledger import TaskLedgerService, effective_task


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
        self.prompt_ref = self.service.artifacts.put_json(
            {
                "instruction": "implement router",
                "workspace": {"repo_path": str(self.workspace)},
            },
            artifact_type="RolePromptPackArtifact",
        )
        self.service.repository.ensure_role_session(
            session_id="session-router",
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            role="implementation",
            mode="produce",
            executor_profile_id="software_engineering.v2_coder",
            family_binding_sha="binding",
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
                executor_profile_id="software_engineering.v2_coder",
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

    def test_gateway_rejects_legacy_bound_input_receipt_methods(self) -> None:
        for method in ("bound_input_read", "bound_input_json"):
            with self.subTest(method=method), self.assertRaisesRegex(ValueError, "not allowed"):
                self.call(method, name="module_work_view")

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
        with self.assertRaisesRegex(ValueError, "not allowed"):
            self.call("v2_workflow_status")

    def test_architect_appends_one_authorized_task_revision_before_submission(self) -> None:
        workflow_id = "workflow-task-revision"
        revision_id = "architecture-task-revision"
        ledger = TaskLedgerService(self.runtime_root, self.service.artifacts)
        requirements_ref = ledger.publish(
            title="Router",
            task_spec={"objective": "Route quickly", "compatibility": "preserve v1"},
            actor="foreground",
            source_channel="test",
        )
        self.service.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="test",
                expected_version=0,
                idempotency_key="task-revision:create",
                payload={"requirements_ref": requirements_ref.to_dict()},
            )
        )
        self.service.repository.dispatch(
            ActionEnvelope(
                action_type="START_ARCHITECT",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="test",
                expected_version=1,
                idempotency_key="task-revision:start",
                payload={"fencing_token": 1},
            )
        )
        self.service.repository.ensure_role_session(
            session_id="session-task-revision",
            workflow_id=workflow_id,
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id=revision_id,
            role="architect",
            mode="author",
            executor_profile_id="software_engineering.v2_architect",
            family_binding_sha="binding",
        )
        assignment = self.service.repository.create_role_assignment(
            RoleAssignmentRequest(
                assignment_key="task-revision-cycle-1",
                session_id="session-task-revision",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION.value,
                aggregate_id=revision_id,
                role="architect",
                mode="author",
                executor_profile_id="software_engineering.v2_architect",
                family_binding_sha="binding",
                input_fingerprint="task-revision-input",
                required_inputs=(),
                input_refs={"requirements": requirements_ref.to_dict()},
                execution_spec={"effect_type": "run_architect_role"},
                submission_kind="architecture",
            )
        )
        assignment_id = str(assignment["assignment_id"])
        attempt = self.service.repository.claim_role_assignment(assignment_id)
        attempt_id = str(attempt["attempt_id"])
        lease_resource = f"assignment:{assignment_id}"
        lease = self.service.repository.claim_lease(
            lease_resource,
            attempt_id,
            ttl_seconds=120,
        )
        self.service.repository.start_role_attempt(
            assignment_id=assignment_id,
            attempt_id_value=attempt_id,
            lease_resource_key=lease_resource,
            fencing_token=lease.fencing_token,
            prompt_pack_ref=self.prompt_ref.to_dict(),
        )
        access_token = self.service.repository.issue_role_attempt_access_token(
            assignment_id=assignment_id,
            attempt_id_value=attempt_id,
            fencing_token=lease.fencing_token,
        )
        context = {
            "workflow_id": workflow_id,
            "invocation_id": attempt_id,
            "lease_resource_key": lease_resource,
            "fencing_token": lease.fencing_token,
            "role": "architect",
            "mode": "author",
            "draft_kind": "architecture",
            "input_fingerprint": "task-revision-input",
            "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
        }
        authority_ref = ledger.publish_authority(
            title="Routing objective",
            question="Should routing optimize speed or determinism?",
            answer="It must be deterministic; speed is secondary.",
            origin="architect_user_clarification",
            actor="user",
            source_channel="test",
        )

        recorded = self.gateway.call(
            "task_revision_authority_record",
            {
                "access_token": access_token,
                "task_revision_authority_ref": authority_ref.to_dict(),
            },
        )
        self.assertTrue(recorded["recorded"])
        with self.assertRaisesRegex(ValueError, "pending task revision"):
            self.gateway.call(
                "draft_submit",
                {
                    "access_token": access_token,
                    "context": context,
                    "expected_version": 0,
                    "submission": {"status": "ready"},
                },
            )

        revision = {
            "schema_version": "1",
            "summary": "Make deterministic routing binding and speed secondary.",
            "changes": [
                {
                    "op": "replace",
                    "path": "/objective",
                    "value": "Route deterministically; speed is secondary",
                }
            ],
        }
        appended = self.gateway.call(
            "task_revision_append",
            {
                "access_token": access_token,
                "revision": revision,
            },
        )
        self.assertTrue(appended["appended"])
        self.assertEqual(appended["generation"], 1)
        current = self.service.repository.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            revision_id,
        )
        assert current is not None
        self.assertEqual(current.state, "ARCHITECT_RUNNING")
        self.assertNotIn("pending_task_revision_authority_ref", current.payload)
        self.assertEqual(current.payload["requirements_ref"], appended["requirements_ref"])
        next_ledger = self.service.artifacts.read_json(appended["requirements_ref"])
        self.assertEqual(len(next_ledger["revisions"]), 1)
        self.assertEqual(
            next_ledger["revisions"][0]["authority"]["answer"],
            "It must be deterministic; speed is secondary.",
        )
        self.assertEqual(
            effective_task(next_ledger)["objective"],
            "Route deterministically; speed is secondary",
        )

        duplicate = self.gateway.call(
            "task_revision_append",
            {
                "access_token": access_token,
                "revision": revision,
            },
        )
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["requirements_ref"], appended["requirements_ref"])

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

if __name__ == "__main__":
    unittest.main()
