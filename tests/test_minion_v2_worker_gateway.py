from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pal.minion.v2.contracts import AggregateType
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.submission_drafts import AUTHORING_CONTRACT_VERSION
from pal.minion.v2.worker_gateway import WorkerAssignmentGateway
from pal.minion.v2.worker_protocol import WorkerAssignmentRequest


class MinionV2WorkerGatewayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal-v2-worker-gateway-"))
        self.service = MinionV2WorkflowService(self.runtime_root)
        self.gateway = WorkerAssignmentGateway(self.service)
        self.input_ref = self.service.artifacts.put_json(
            {"module": "router", "contract": "route deterministically"},
            artifact_type="ModuleWorkViewArtifact",
        )
        self.prompt_ref = self.service.artifacts.put_json(
            {"instruction": "implement router"},
            artifact_type="WorkerPromptPackArtifact",
        )
        self.service.repository.ensure_worker_session(
            session_id="session-router",
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            role="producer",
        )
        assignment = self.service.repository.create_worker_assignment(
            WorkerAssignmentRequest(
                assignment_key="router-cycle-1",
                session_id="session-router",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN.value,
                aggregate_id="node-router",
                role="producer",
                input_fingerprint="router-input",
                required_inputs=("module_work_view",),
                input_refs={"module_work_view": self.input_ref.to_dict()},
                execution_spec={"effect_type": "spawn_producer_worker"},
                submission_kind="candidate",
            )
        )
        self.assignment_id = assignment["assignment_id"]
        attempt = self.service.repository.claim_worker_assignment(self.assignment_id)
        self.attempt_id = attempt["attempt_id"]
        self.lease_resource = f"assignment:{self.assignment_id}"
        lease = self.service.repository.claim_lease(
            self.lease_resource,
            self.attempt_id,
            ttl_seconds=120,
        )
        self.fencing_token = lease.fencing_token
        self.service.repository.start_worker_attempt(
            assignment_id=self.assignment_id,
            attempt_id_value=self.attempt_id,
            lease_resource_key=self.lease_resource,
            fencing_token=self.fencing_token,
            prompt_pack_ref=self.prompt_ref.to_dict(),
        )
        self.access_token = self.service.repository.issue_worker_attempt_access_token(
            assignment_id=self.assignment_id,
            attempt_id_value=self.attempt_id,
            fencing_token=self.fencing_token,
        )
        self.context = {
            "workflow_id": "workflow-router",
            "invocation_id": self.attempt_id,
            "lease_resource_key": self.lease_resource,
            "fencing_token": self.fencing_token,
            "role": "producer",
            "draft_kind": "candidate",
            "input_fingerprint": "router-input",
            "authoring_contract_version": AUTHORING_CONTRACT_VERSION,
        }

    def call(self, method: str, **params):
        return self.gateway.call(
            method,
            {"access_token": self.access_token, **params},
        )

    def test_gateway_reads_only_bound_inputs_and_records_receipt(self) -> None:
        result = self.call("bound_input_read", name="module_work_view")
        self.assertIn('"module":"router"', result["content"])
        assignment = self.service.repository.read_worker_assignment(self.assignment_id)
        self.assertEqual(assignment["state"], "running")
        with self.assertRaisesRegex(ValueError, "unknown bound input"):
            self.call("bound_input_read", name="another_workflow")

    def test_gateway_owns_draft_cas_artifact_publish_and_submission_receipt(self) -> None:
        self.call("bound_input_read", name="module_work_view")
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
        self.assertTrue(
            self.call(
                "draft_submit",
                context=self.context,
                expected_version=1,
                submission={"status": "candidate_ready", "checks": ["unit"]},
            )["submitted"]
        )
        assignment = self.service.repository.read_worker_assignment(self.assignment_id)
        self.assertEqual(assignment["state"], "submission_recorded")
        self.assertTrue(assignment["submission_artifact_ref"]["sha256"])

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


if __name__ == "__main__":
    unittest.main()
