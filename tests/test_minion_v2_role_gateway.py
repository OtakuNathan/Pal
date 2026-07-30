from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import subprocess

from pal.minion.v2.contracts import ActionEnvelope, AggregateType
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.submission_drafts import AUTHORING_CONTRACT_VERSION
from pal.minion.v2.role_gateway import (
    RoleAssignmentGateway,
    RoleGatewayArtifactStore,
)
from pal.minion.v2.role_protocol import RoleAssignmentRequest


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
            executor_profile_id="software_engineering.v2_coder",
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

    def test_gateway_persists_execution_clock_and_pager_by_role_session(
        self,
    ) -> None:
        first = self.call(
            "execution_begin_input",
            logical_session_id="session-router",
            input_id="assignment-router",
            retention_user_turns=5,
        )
        replay = self.call(
            "execution_begin_input",
            logical_session_id="session-router",
            input_id="assignment-router",
            retention_user_turns=5,
        )
        self.assertEqual(first["context"]["current_user_turn"], 1)
        self.assertEqual(replay["context"]["current_user_turn"], 1)

        stored = self.call(
            "execution_store_pager",
            manifest={
                "result_ref": "result-router",
                "logical_session_id": "session-router",
                "tool_name": "read_file",
                "status": "ok",
                "ok": True,
                "page_size": 256,
                "original_size": 11,
                "page_count": 1,
                "created_user_turn": 999,
                "expires_at_user_turn": 999,
                "output_json": '{"value":1}',
                "rendered": "hello world",
            },
        )
        self.assertEqual(stored["manifest"]["created_user_turn"], 1)
        self.assertEqual(stored["manifest"]["expires_at_user_turn"], 6)
        page = self.call(
            "execution_read_pager",
            logical_session_id="session-router",
            result_ref="result-router",
            page=1,
            anchor="head",
        )
        self.assertEqual(page["state"], "ok")
        self.assertEqual(page["content"], "hello world")

        session = self.service.repository.read_role_session("session-router")
        self.assertIn("result-router", session["execution_state"]["handles"])
        payload_ref = session["execution_state"]["handles"]["result-router"][
            "payload_ref"
        ]
        self.assertEqual(
            self.service.artifacts.read_json(payload_ref)["rendered"],
            "hello world",
        )
        for index in range(2, 7):
            self.call(
                "execution_begin_input",
                logical_session_id="session-router",
                input_id=f"assignment-router-{index}",
                retention_user_turns=5,
            )
        expired = self.call(
            "execution_read_pager",
            logical_session_id="session-router",
            result_ref="result-router",
            page=1,
            anchor="head",
        )
        self.assertEqual(expired["state"], "expired_handle")

    def test_gateway_rejects_cross_session_execution_state_access(self) -> None:
        with self.assertRaisesRegex(ValueError, "authenticated role session"):
            self.call(
                "execution_context",
                logical_session_id="some-other-session",
            )

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

if __name__ == "__main__":
    unittest.main()
