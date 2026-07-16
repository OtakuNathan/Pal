from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.contracts import ActionEnvelope, AggregateType
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.worker_protocol import (
    WorkerAssignmentAction,
    WorkerAssignmentRequest,
    WorkerAssignmentState,
    stable_hash,
    worker_assignment_target,
)


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
            execution_spec={"effect_type": "spawn_producer_worker"},
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
        failed = self.repository.queue_worker_attempt_retry(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=first["attempt_id"],
            error_kind="process_lost",
            error_text="runner exited before submission",
        )
        self.assertEqual(failed["state"], "retry_queued")
        second = self.repository.claim_worker_assignment(assignment["assignment_id"])
        self.assertNotEqual(second["attempt_id"], first["attempt_id"])
        self.assertEqual(second["attempt_index"], 2)
        self.assertEqual(
            self.repository.read_worker_assignment(assignment["assignment_id"])["session_id"],
            "session-router",
        )

    def test_assignment_machine_has_no_business_triage_state(self) -> None:
        self.assertNotIn("triage_required", {state.value for state in WorkerAssignmentState})
        self.assertEqual(
            worker_assignment_target(
                WorkerAssignmentState.RUNNING,
                WorkerAssignmentAction.RECORD_RESULT,
            ),
            WorkerAssignmentState.RESULT_RECORDED,
        )
        with self.assertRaisesRegex(ValueError, "illegal worker assignment transition"):
            worker_assignment_target(
                WorkerAssignmentState.RESULT_RECORDED,
                WorkerAssignmentAction.QUEUE_RETRY,
            )
        with self.assertRaisesRegex(ValueError, "illegal worker assignment transition"):
            worker_assignment_target(
                WorkerAssignmentState.RESULT_RECORDED,
                WorkerAssignmentAction.CANCEL,
            )

    def test_v15_migrates_activation_roles_and_obsolete_assignment_states(self) -> None:
        assignment = self.repository.create_worker_assignment(self.request())
        with sqlite3.connect(str(self.repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_schema_meta SET schema_value = '14' "
                "WHERE schema_key = 'schema_version'"
            )
            connection.execute(
                "UPDATE minion_v2_worker_sessions SET role = 'repair' "
                "WHERE session_id = 'session-router'"
            )
            connection.execute(
                "UPDATE minion_v2_worker_assignments SET state = 'triage_required' "
                "WHERE assignment_id = ?",
                (assignment["assignment_id"],),
            )

        self.repository.ensure_schema()

        session = self.repository.read_worker_session("session-router")
        migrated = self.repository.read_worker_assignment(assignment["assignment_id"])
        self.assertEqual(session["role"], "coder")
        self.assertEqual(session["status"], "suspended")
        self.assertEqual(migrated["state"], "cancelled")
        self.repository.ensure_worker_session(
            session_id="session-router",
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            role="repair",
        )

    def test_failure_result_and_parent_triage_settle_atomically(self) -> None:
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_NODE_RUN",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="test",
                expected_version=0,
                payload={
                    "unit_contract_ref": self.input_ref.to_dict(),
                    "epoch_id": "epoch-router",
                    "dependency_node_ids": [],
                },
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="DEPENDENCIES_ACCEPTED",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="test",
                expected_version=1,
                payload={"accepted_dependency_node_ids": []},
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="START_PRODUCING",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="test",
                expected_version=2,
                payload={
                    "fencing_token": 1,
                    "active_worker_id": "session-router",
                    "lease_resource_key": "node:node-router:writer",
                },
            )
        )
        assignment = self.repository.create_worker_assignment(self.request())
        attempt, _fencing_token = self.start_attempt(assignment["assignment_id"])
        failure_payload = {
            "kind": "worker_assignment_failed",
            "error": "worker process exited",
        }
        failure_ref = self.artifacts.put_json(
            failure_payload,
            artifact_type="WorkerAssignmentFailureArtifact",
        )
        receipt = self.repository.record_worker_failure_result(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            error_kind="worker_process_failed",
            error_text="worker process exited",
            failure_artifact_ref=failure_ref.to_dict(),
            payload_hash=stable_hash(failure_payload),
            settlement_action={"action_type": "WORKER_FAILED"},
        )
        self.assertEqual(
            self.repository.read_worker_assignment(assignment["assignment_id"])["state"],
            "result_recorded",
        )
        self.assertIsNone(
            self.repository.read_lease(f"assignment:{assignment['assignment_id']}")
        )

        outcome = self.repository.dispatch(
            ActionEnvelope(
                action_type="WORKER_FAILED",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="manager",
                expected_version=3,
                idempotency_key="settle-worker-failure",
                payload={
                    "failure_artifact_ref": failure_ref.to_dict(),
                    "blocker": {
                        "kind": "worker_failure",
                        "summary": "worker process exited",
                    },
                },
            ),
            worker_assignment_id=assignment["assignment_id"],
            worker_submission_payload_hash=receipt.payload_hash,
        )

        self.assertEqual(outcome.snapshot.state, "TRIAGE_REQUIRED")
        self.assertEqual(
            self.repository.read_worker_assignment(assignment["assignment_id"])["state"],
            "settled",
        )
        self.assertEqual(
            self.repository.read_worker_attempt(attempt["attempt_id"])["status"],
            "failed",
        )
        with self.assertRaisesRegex(ValueError, "terminal outcome"):
            self.repository.complete_worker_session("session-router")

        self.repository.dispatch(
            ActionEnvelope(
                action_type="REQUEST_CANCEL",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="test",
                expected_version=4,
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CANCEL_CONFIRMED",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="test",
                expected_version=5,
            )
        )
        self.assertTrue(
            self.repository.complete_worker_session(
                "session-router",
                status="cancelled",
            )
        )
        self.assertEqual(
            self.repository.read_worker_session("session-router")["status"],
            "cancelled",
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

    def test_session_cannot_cross_aggregate_ownership(self) -> None:
        with self.assertRaisesRegex(ValueError, "identity is immutable"):
            self.repository.ensure_worker_session(
                session_id="session-router",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="revision-after-finding",
                role="producer",
            )
        request = WorkerAssignmentRequest(
            **{
                **self.request(key="router-repair").to_payload(),
                "aggregate_id": "node-router-repair",
            }
        )
        with self.assertRaisesRegex(ValueError, "session identity"):
            self.repository.create_worker_assignment(request)

    def test_producer_and_repair_are_activations_of_one_coder_session(self) -> None:
        producer = self.repository.create_worker_assignment(
            self.request(key="router-producer")
        )
        repair = self.repository.create_worker_assignment(
            WorkerAssignmentRequest(
                **{
                    **self.request(key="router-repair").to_payload(),
                    "role": "repair",
                }
            )
        )
        self.assertEqual(producer["session_id"], repair["session_id"])
        self.assertEqual(
            self.repository.read_worker_session("session-router")["role"],
            "coder",
        )

    def test_stale_node_preserves_its_role_session_for_requeue(self) -> None:
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_NODE_RUN",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="test",
                expected_version=0,
                payload={
                    "unit_contract_ref": self.input_ref.to_dict(),
                    "epoch_id": "epoch-router",
                    "dependency_node_ids": [],
                },
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="DEPENDENCIES_ACCEPTED",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="test",
                expected_version=1,
                payload={"accepted_dependency_node_ids": []},
            )
        )
        assignment = self.repository.create_worker_assignment(
            self.request(key="router-before-stale")
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="MARK_STALE",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="test",
                expected_version=2,
                payload={"stale_reason_ref": self.input_ref.to_dict()},
            )
        )
        effects = self.repository.claim_outbox("stale-test", limit=10)
        stale_effect = next(
            effect
            for effect in effects
            if effect["effect_type"] == "suspend_stale_node_assignments"
        )
        asyncio.run(
            MinionV2OutboxProcessor(
                MinionV2WorkflowService(self.runtime_root)
            )._execute_mechanical(stale_effect)
        )

        self.assertEqual(
            self.repository.read_worker_assignment(assignment["assignment_id"])["state"],
            "cancelled",
        )
        self.assertEqual(
            self.repository.read_worker_session("session-router")["status"],
            "suspended",
        )
        with self.assertRaisesRegex(ValueError, "terminal outcome"):
            self.repository.complete_worker_session("session-router")

        self.repository.dispatch(
            ActionEnvelope(
                action_type="REQUEUE_STALE",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="test",
                expected_version=3,
                payload={
                    "unit_contract_ref": self.input_ref.to_dict(),
                    "dependency_fingerprint": "deps-v2",
                    "accepted_dependency_node_ids": [],
                },
            )
        )
        resumed = self.repository.create_worker_assignment(
            self.request(key="router-after-stale")
        )
        self.assertEqual(resumed["session_id"], "session-router")

    def test_attempt_access_token_is_scoped_and_fenced(self) -> None:
        assignment = self.repository.create_worker_assignment(self.request())
        attempt, fencing_token = self.start_attempt(assignment["assignment_id"])
        token = self.repository.issue_worker_attempt_access_token(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=fencing_token,
        )
        authenticated = self.repository.authenticate_worker_attempt(token)
        self.assertEqual(authenticated["assignment"]["assignment_id"], assignment["assignment_id"])
        self.assertEqual(authenticated["attempt_id"], attempt["attempt_id"])
        with self.assertRaisesRegex(ValueError, "invalid"):
            self.repository.authenticate_worker_attempt("not-the-token")

    def test_aggregate_control_cancels_assignment_without_resurrection(self) -> None:
        assignment = self.repository.create_worker_assignment(self.request())
        attempt, fencing_token = self.start_attempt(assignment["assignment_id"])
        token = self.repository.issue_worker_attempt_access_token(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=fencing_token,
        )

        cancelled = self.repository.cancel_worker_assignments(
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            reason="node paused",
        )

        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["state"], "cancelled")
        self.assertEqual(
            self.repository.read_worker_attempt(attempt["attempt_id"])["status"],
            "cancelled",
        )
        self.assertIsNone(
            self.repository.read_lease(f"assignment:{assignment['assignment_id']}")
        )
        with self.assertRaisesRegex(ValueError, "invalid"):
            self.repository.authenticate_worker_attempt(token)

        repeated_failure = self.repository.queue_worker_attempt_retry(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            error_kind="worker_process_failed",
            error_text="process observed cancellation",
        )
        self.assertEqual(repeated_failure["state"], "cancelled")

    def test_aggregate_control_settles_an_already_recorded_result(self) -> None:
        assignment = self.repository.create_worker_assignment(self.request())
        attempt, fencing_token = self.start_attempt(assignment["assignment_id"])
        self.repository.record_worker_input_read(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            input_name="module_work_view",
            artifact_sha256=self.input_ref.sha256,
            fencing_token=fencing_token,
        )
        self.repository.record_worker_submission(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=fencing_token,
            artifact_ref=self.submission_ref.to_dict(),
            payload_hash=stable_hash({"status": "candidate_ready"}),
            settlement_action={"action_type": "SUBMIT_CANDIDATE"},
        )

        settled = self.repository.cancel_worker_assignments(
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            reason="parent state superseded this result",
        )

        self.assertEqual(settled[0]["state"], "settled")
        self.assertEqual(
            self.repository.read_worker_attempt(attempt["attempt_id"])["status"],
            "completed",
        )
        self.assertEqual(
            self.repository.read_worker_session("session-router")["status"],
            "suspended",
        )
        self.assertEqual(
            settled[0]["last_error"],
            "parent state superseded this result",
        )

    def test_business_action_and_submission_settlement_commit_atomically(self) -> None:
        self.repository.ensure_worker_session(
            session_id="session-settlement",
            workflow_id="workflow-settlement",
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id="workflow-settlement",
            role="producer",
        )
        assignment = self.repository.create_worker_assignment(
            WorkerAssignmentRequest(
                assignment_key="atomic-settlement",
                session_id="session-settlement",
                workflow_id="workflow-settlement",
                aggregate_type=AggregateType.WORKFLOW.value,
                aggregate_id="workflow-settlement",
                role="producer",
                input_fingerprint="atomic-input",
                required_inputs=(),
                input_refs={},
                execution_spec={"effect_type": "spawn_producer_worker"},
                submission_kind="candidate",
            )
        )
        attempt = self.repository.claim_worker_assignment(assignment["assignment_id"])
        lease = self.repository.claim_lease(
            f"assignment:{assignment['assignment_id']}",
            attempt["attempt_id"],
            ttl_seconds=120,
            metadata={"workflow_id": "workflow-settlement"},
        )
        self.repository.start_worker_attempt(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            lease_resource_key=f"assignment:{assignment['assignment_id']}",
            fencing_token=lease.fencing_token,
            prompt_pack_ref=self.prompt_ref.to_dict(),
        )
        payload_hash = stable_hash({"status": "candidate_ready"})
        self.repository.record_worker_submission(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=lease.fencing_token,
            artifact_ref=self.submission_ref.to_dict(),
            payload_hash=payload_hash,
            settlement_action={"action_type": "SETTLE_WORKER_SUBMISSION"},
        )
        action = ActionEnvelope(
            action_type="CREATE_WORKFLOW",
            workflow_id="workflow-settlement",
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id="workflow-settlement",
            actor="worker-supervisor",
            expected_version=0,
            idempotency_key="create-from-worker",
        )

        self.repository.dispatch(action)
        self.assertEqual(
            self.repository.read_worker_assignment(assignment["assignment_id"])["state"],
            "result_recorded",
        )
        replay = self.repository.dispatch(
            action,
            worker_assignment_id=assignment["assignment_id"],
            worker_submission_payload_hash=payload_hash,
        )

        self.assertTrue(replay.duplicate)
        self.assertEqual(
            self.repository.read_worker_assignment(assignment["assignment_id"])["state"],
            "settled",
        )


if __name__ == "__main__":
    unittest.main()
