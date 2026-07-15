from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.contracts import AggregateType
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.worker_protocol import WorkerAssignmentRequest, stable_hash


class MinionV2WorkerProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal-v2-worker-protocol-"))
        self.repository = MinionV2Repository(self.runtime_root)
        self.repository.ensure_schema()
        self.artifacts = ContentAddressedArtifactStore(
            self.runtime_root,
            self.repository,
        )
        self.input_ref = self.artifacts.put_json(
            {"module": "router"},
            artifact_type="ModuleWorkViewArtifact",
        )
        self.prompt_ref = self.artifacts.put_json(
            {"instruction": "implement router"},
            artifact_type="WorkerPromptPackArtifact",
        )
        self.submission_ref = self.artifacts.put_json(
            {"status": "candidate_ready"},
            artifact_type="ProducerSubmissionArtifact",
        )
        self.repository.ensure_worker_session(
            session_id="session-router",
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            role="producer",
        )

    def request(self, *, key: str = "router-cycle-1") -> WorkerAssignmentRequest:
        return WorkerAssignmentRequest(
            assignment_key=key,
            session_id="session-router",
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN.value,
            aggregate_id="node-router",
            role="producer",
            input_fingerprint="input-fingerprint",
            required_inputs=("module_work_view",),
            input_refs={"module_work_view": self.input_ref.to_dict()},
            submission_kind="candidate",
        )

    def start_attempt(self, assignment_id: str) -> tuple[dict, int]:
        attempt = self.repository.claim_worker_assignment(assignment_id)
        lease = self.repository.claim_lease(
            f"assignment:{assignment_id}",
            attempt["attempt_id"],
            ttl_seconds=120,
            metadata={"workflow_id": "workflow-router"},
        )
        started = self.repository.start_worker_attempt(
            assignment_id=assignment_id,
            attempt_id_value=attempt["attempt_id"],
            lease_resource_key=f"assignment:{assignment_id}",
            fencing_token=lease.fencing_token,
            prompt_pack_ref=self.prompt_ref.to_dict(),
        )
        return started, lease.fencing_token

    def test_assignment_submission_is_durable_and_idempotent(self) -> None:
        first = self.repository.create_worker_assignment(self.request())
        replay = self.repository.create_worker_assignment(self.request())
        self.assertEqual(replay["assignment_id"], first["assignment_id"])

        attempt, fencing_token = self.start_attempt(first["assignment_id"])
        self.repository.record_worker_input_read(
            assignment_id=first["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            input_name="module_work_view",
            artifact_sha256=self.input_ref.sha256,
            fencing_token=fencing_token,
        )
        action = {
            "action_type": "SUBMIT_CANDIDATE",
            "payload": {"producer_report_ref": self.submission_ref.to_dict()},
        }
        payload_hash = stable_hash({"status": "candidate_ready"})
        receipt = self.repository.record_worker_submission(
            assignment_id=first["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=fencing_token,
            artifact_ref=self.submission_ref.to_dict(),
            payload_hash=payload_hash,
            settlement_action=action,
        )
        replayed = self.repository.record_worker_submission(
            assignment_id=first["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=fencing_token,
            artifact_ref=self.submission_ref.to_dict(),
            payload_hash=payload_hash,
            settlement_action=action,
        )
        self.assertEqual(replayed.to_dict(), receipt.to_dict())

        settled = self.repository.settle_worker_assignment(
            assignment_id=first["assignment_id"],
            submission_payload_hash=payload_hash,
        )
        self.assertEqual(settled["state"], "settled")
        self.assertEqual(
            self.repository.settle_worker_assignment(
                assignment_id=first["assignment_id"],
                submission_payload_hash=payload_hash,
            )["state"],
            "settled",
        )

    def test_submission_requires_every_mandatory_input_read(self) -> None:
        assignment = self.repository.create_worker_assignment(self.request())
        attempt, fencing_token = self.start_attempt(assignment["assignment_id"])
        with self.assertRaisesRegex(ValueError, "missing required input reads"):
            self.repository.record_worker_submission(
                assignment_id=assignment["assignment_id"],
                attempt_id_value=attempt["attempt_id"],
                fencing_token=fencing_token,
                artifact_ref=self.submission_ref.to_dict(),
                payload_hash=stable_hash({"status": "candidate_ready"}),
                settlement_action={"action_type": "SUBMIT_CANDIDATE", "payload": {}},
            )

    def test_retry_creates_a_new_attempt_without_replacing_the_session(self) -> None:
        assignment = self.repository.create_worker_assignment(self.request())
        first, _fencing_token = self.start_attempt(assignment["assignment_id"])
        failed = self.repository.fail_worker_attempt(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=first["attempt_id"],
            error_kind="process_lost",
            error_text="runner exited before submission",
            retryable=True,
        )
        self.assertEqual(failed["state"], "retryable")
        second = self.repository.claim_worker_assignment(assignment["assignment_id"])
        self.assertNotEqual(second["attempt_id"], first["attempt_id"])
        self.assertEqual(second["attempt_index"], 2)
        self.assertEqual(
            self.repository.read_worker_assignment(assignment["assignment_id"])["session_id"],
            "session-router",
        )

    def test_assignment_key_cannot_be_reused_for_different_inputs(self) -> None:
        self.repository.create_worker_assignment(self.request())
        changed = WorkerAssignmentRequest(
            **{
                **self.request().to_payload(),
                "required_inputs": ("module_work_view",),
                "input_refs": {"module_work_view": self.input_ref.to_dict()},
                "input_fingerprint": "different",
            }
        )
        with self.assertRaisesRegex(ValueError, "different inputs"):
            self.repository.create_worker_assignment(changed)


if __name__ == "__main__":
    unittest.main()
