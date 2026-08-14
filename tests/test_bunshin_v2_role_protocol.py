from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from pal.bunshin.checkpoint import LogicalCoroutineCheckpointStore
from pal.bunshin.v2.artifacts import ContentAddressedArtifactStore
from pal.bunshin.v2.contracts import ActionEnvelope, AggregateType
from pal.bunshin.v2.orchestration import BunshinV2OutboxProcessor
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.service import BunshinV2WorkflowService
from pal.bunshin.v2.semantic_orchestration import SemanticOrchestrator
from pal.bunshin.v2.sessions import architecture_reviewer_session_id
from pal.bunshin.v2.role_protocol import (
    RoleAssignmentAction,
    RoleAssignmentRequest,
    RoleAssignmentState,
    RoleSessionAction,
    RoleSessionState,
    stable_hash,
    role_assignment_target,
    role_session_target,
)


class BunshinV2RoleProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal-v2-role-protocol-"))
        self.repository = BunshinV2Repository(self.runtime_root)
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
            artifact_type="RolePromptPackArtifact",
        )
        self.submission_ref = self.artifacts.put_json(
            {"status": "candidate_ready"},
            artifact_type="CandidateRoleSubmissionArtifact",
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="workflow-router",
                actor="test",
                expected_version=0,
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="workflow-router",
                actor="test",
                expected_version=1,
            )
        )
        self.repository.dispatch(
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
        self.repository.ensure_role_session(
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

    def request(self, *, key: str = "router-cycle-1") -> RoleAssignmentRequest:
        return RoleAssignmentRequest(
            assignment_key=key,
            session_id="session-router",
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN.value,
            aggregate_id="node-router",
            role="implementation",
            mode="produce",
            role_profile_id="software_engineering.v2_coder",
            family_binding_sha="binding",
            input_fingerprint="input-fingerprint",
            required_inputs=(),
            input_refs={"module_work_view": self.input_ref.to_dict()},
            execution_spec={"effect_type": "run_implementation_role"},
            submission_kind="candidate",
        )

    def test_checkpoint_reconciliation_keeps_only_resumable_role_sessions(self) -> None:
        store = LogicalCoroutineCheckpointStore(self.runtime_root)
        for session_id in ("session-router", "orphan-session"):
            path = store.current_path(session_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")

        retired = self.repository.reconcile_role_session_checkpoints()

        self.assertEqual(retired, ("orphan-session",))
        self.assertTrue(store.current_path("session-router").is_file())
        self.assertFalse(store.current_path("orphan-session").exists())

    def test_architecture_reviewer_cannot_retire_before_cycle_is_terminal(self) -> None:
        revision_id = "arch-reviewer-root"
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="test",
                expected_version=0,
                payload={
                    "architecture_cycle_id": revision_id,
                    "requirements_ref": self.input_ref.to_dict(),
                },
            )
        )
        session_id = architecture_reviewer_session_id(
            "workflow-router",
            revision_id,
            {"architecture_cycle_id": revision_id},
        )
        self.repository.ensure_role_session(
            session_id=session_id,
            workflow_id="workflow-router",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id=revision_id,
            role="reviewer",
            mode="architecture",
            role_profile_id="software_engineering.v2_reviewer",
            family_binding_sha="binding",
            scope_kind="architecture_cycle",
            subject_key=revision_id,
        )

        with self.assertRaisesRegex(
            ValueError,
            "architecture role session cannot complete while its correction cycle is open",
        ):
            self.repository.complete_role_session(session_id)

        self.assertEqual(
            self.repository.read_role_session(session_id)["status"],
            RoleSessionState.ACTIVE.value,
        )

    def start_attempt(self, assignment_id: str) -> tuple[dict, int]:
        attempt = self.repository.claim_role_assignment(assignment_id)
        lease = self.repository.claim_lease(
            f"assignment:{assignment_id}",
            attempt["attempt_id"],
            ttl_seconds=120,
            metadata={"workflow_id": "workflow-router"},
        )
        started = self.repository.start_role_attempt(
            assignment_id=assignment_id,
            attempt_id_value=attempt["attempt_id"],
            lease_resource_key=f"assignment:{assignment_id}",
            fencing_token=lease.fencing_token,
            prompt_pack_ref=self.prompt_ref.to_dict(),
        )
        return started, lease.fencing_token

    def test_assignment_submission_is_durable_and_idempotent(self) -> None:
        first = self.repository.create_role_assignment(self.request())
        replay = self.repository.create_role_assignment(self.request())
        self.assertEqual(replay["assignment_id"], first["assignment_id"])

        attempt, fencing_token = self.start_attempt(first["assignment_id"])
        action = {
            "action_type": "SUBMIT_CANDIDATE",
            "payload": {"producer_report_ref": self.submission_ref.to_dict()},
        }
        payload_hash = stable_hash({"status": "candidate_ready"})
        receipt = self.repository.record_role_submission(
            assignment_id=first["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=fencing_token,
            artifact_ref=self.submission_ref.to_dict(),
            payload_hash=payload_hash,
            settlement_action=action,
        )
        replayed = self.repository.record_role_submission(
            assignment_id=first["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=fencing_token,
            artifact_ref=self.submission_ref.to_dict(),
            payload_hash=payload_hash,
            settlement_action=action,
        )
        self.assertEqual(replayed.to_dict(), receipt.to_dict())

        settled = self.repository.settle_role_assignment(
            assignment_id=first["assignment_id"],
            submission_payload_hash=payload_hash,
        )
        self.assertEqual(settled["state"], "settled")
        completed = self.repository.read_latest_completed_role_harness_attempt(
            session_id="session-router",
            harness_id="pal",
        )
        self.assertIsNotNone(completed)
        self.assertEqual(completed["attempt_id"], attempt["attempt_id"])
        self.assertEqual(
            self.repository.settle_role_assignment(
                assignment_id=first["assignment_id"],
                submission_payload_hash=payload_hash,
            )["state"],
            "settled",
        )

    def test_submission_does_not_require_input_read_receipts(self) -> None:
        assignment = self.repository.create_role_assignment(self.request())
        attempt, fencing_token = self.start_attempt(assignment["assignment_id"])
        receipt = self.repository.record_role_submission(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=fencing_token,
            artifact_ref=self.submission_ref.to_dict(),
            payload_hash=stable_hash({"status": "candidate_ready"}),
            settlement_action={"action_type": "SUBMIT_CANDIDATE", "payload": {}},
        )
        self.assertEqual(receipt.assignment_id, assignment["assignment_id"])

    def test_retry_creates_a_new_attempt_without_replacing_the_session(self) -> None:
        assignment = self.repository.create_role_assignment(self.request())
        first, _fencing_token = self.start_attempt(assignment["assignment_id"])
        failed = self.repository.queue_role_attempt_retry(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=first["attempt_id"],
            error_kind="process_lost",
            error_text="runner exited before submission",
        )
        self.assertEqual(failed["state"], "retry_queued")
        second = self.repository.claim_role_assignment(assignment["assignment_id"])
        self.assertNotEqual(second["attempt_id"], first["attempt_id"])
        self.assertEqual(second["attempt_index"], 2)
        self.assertEqual(
            self.repository.read_role_assignment(assignment["assignment_id"])["session_id"],
            "session-router",
        )

    def test_claim_pins_the_harness_that_actually_runs_the_session(self) -> None:
        assignment = self.repository.create_role_assignment(self.request())

        attempt = self.repository.claim_role_assignment(
            assignment["assignment_id"],
            harness_id="pal",
            harness_generation="registry-generation-2",
        )

        self.assertEqual(attempt["harness_id"], "pal")
        session = self.repository.read_role_session("session-router")
        self.assertIsNotNone(session)
        self.assertEqual(session["preferred_harness_id"], "pal")
        self.assertEqual(
            session["preferred_harness_generation"],
            "registry-generation-2",
        )

    def test_assignment_machine_has_no_business_triage_state(self) -> None:
        self.assertNotIn("triage_required", {state.value for state in RoleAssignmentState})
        self.assertEqual(
            role_assignment_target(
                RoleAssignmentState.RUNNING,
                RoleAssignmentAction.RECORD_RESULT,
            ),
            RoleAssignmentState.RESULT_RECORDED,
        )
        with self.assertRaisesRegex(ValueError, "illegal role assignment transition"):
            role_assignment_target(
                RoleAssignmentState.RESULT_RECORDED,
                RoleAssignmentAction.QUEUE_RETRY,
            )
        with self.assertRaisesRegex(ValueError, "illegal role assignment transition"):
            role_assignment_target(
                RoleAssignmentState.RESULT_RECORDED,
                RoleAssignmentAction.CANCEL,
            )

    def test_role_session_machine_is_explicit_and_terminal(self) -> None:
        self.assertEqual(
            role_session_target(
                RoleSessionState.ACTIVE,
                RoleSessionAction.SUSPEND,
            ),
            RoleSessionState.SUSPENDED,
        )
        self.assertEqual(
            role_session_target(
                RoleSessionState.SUSPENDED,
                RoleSessionAction.ACTIVATE,
            ),
            RoleSessionState.ACTIVE,
        )
        self.assertEqual(
            role_session_target(
                RoleSessionState.SUSPENDED,
                RoleSessionAction.COMPLETE,
            ),
            RoleSessionState.COMPLETED,
        )
        with self.assertRaisesRegex(ValueError, "illegal role session transition"):
            role_session_target(
                RoleSessionState.COMPLETED,
                RoleSessionAction.ACTIVATE,
            )

    def test_module_verifier_session_suspends_until_module_or_workflow_terminal(self) -> None:
        session_id = "session-router-candidate-a"
        self.repository.ensure_role_session(
            session_id=session_id,
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            role="verifier",
            mode="module",
            role_profile_id="software_engineering.v2_verifier",
            family_binding_sha="binding",
            scope_kind="module",
            subject_key="router",
        )
        request = RoleAssignmentRequest(
            assignment_key="router-review-candidate-a",
            session_id=session_id,
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN.value,
            aggregate_id="node-router",
            role="verifier",
            mode="module",
            role_profile_id="software_engineering.v2_verifier",
            family_binding_sha="binding",
            input_fingerprint="candidate-a",
            required_inputs=(),
            input_refs={"module_work_view": self.input_ref.to_dict()},
            execution_spec={"effect_type": "run_verifier_role"},
            submission_kind="verification",
        )
        assignment = self.repository.create_role_assignment(request)
        attempt, fencing_token = self.start_attempt(assignment["assignment_id"])
        payload_hash = stable_hash({"verdict": "FAIL"})
        self.repository.record_role_submission(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=fencing_token,
            artifact_ref=self.submission_ref.to_dict(),
            payload_hash=payload_hash,
            settlement_action={"action_type": "REVIEW_FAILED", "payload": {}},
        )

        with self.assertRaisesRegex(ValueError, "non-terminal assignment"):
            self.repository.complete_role_session(session_id)

        self.repository.settle_role_assignment(
            assignment_id=assignment["assignment_id"],
            submission_payload_hash=payload_hash,
        )
        with self.assertRaisesRegex(ValueError, "lives for its Module identity"):
            self.repository.complete_role_session(session_id)
        self.assertEqual(
            self.repository.read_role_session(session_id)["status"],
            RoleSessionState.SUSPENDED.value,
        )
        workflow = self.repository.read_snapshot(
            AggregateType.WORKFLOW,
            "workflow-router",
        )
        assert workflow is not None
        self.repository.dispatch(
            ActionEnvelope(
                action_type="MARK_COMPLETED",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="workflow-router",
                actor="test",
                expected_version=workflow.version,
                payload={"result_artifact_ref": self.submission_ref.to_dict()},
            )
        )
        completed_sessions = self.repository.complete_workflow_role_sessions(
            "workflow-router"
        )
        self.assertIn(session_id, completed_sessions)
        self.assertEqual(
            self.repository.read_role_session(session_id)["status"],
            RoleSessionState.COMPLETED.value,
        )

    def test_deleted_module_can_retire_its_logical_session(self) -> None:
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_EXECUTION_EPOCH",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id="epoch-after-router",
                actor="test",
                expected_version=0,
                payload={
                    "architecture_manifest_ref": {"sha256": "manifest-next"},
                    "topology_ref": {"sha256": "topology-next"},
                },
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="START_EXECUTION",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id="epoch-after-router",
                actor="test",
                expected_version=1,
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_NODE_RUN",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-other",
                actor="test",
                expected_version=0,
                payload={
                    "epoch_id": "epoch-after-router",
                    "module_name": "other",
                    "unit_contract_ref": {"sha256": "contract-other"},
                },
            )
        )

        self.assertTrue(
            self.repository.complete_role_session(
                "session-router",
                status="cancelled",
            )
        )
        self.assertEqual(
            self.repository.read_role_session("session-router")["status"],
            RoleSessionState.CANCELLED.value,
        )

    def test_role_session_scope_is_immutable(self) -> None:
        session = self.repository.ensure_role_session(
            session_id="session-router",
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            role="implementation",
            mode="repair",
            role_profile_id="software_engineering.v2_coder",
            family_binding_sha="binding",
            scope_kind="module",
            subject_key="router",
        )
        self.assertEqual(session["scope_kind"], "module")
        self.assertEqual(session["subject_key"], "router")
        with self.assertRaisesRegex(ValueError, "identity is immutable"):
            self.repository.ensure_role_session(
                session_id="session-router",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                role="implementation",
                mode="produce",
                role_profile_id="software_engineering.v2_coder",
                family_binding_sha="binding",
                scope_kind="module",
                subject_key="other_module",
            )

    def test_failure_result_and_parent_triage_settle_atomically(self) -> None:
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
        assignment = self.repository.create_role_assignment(self.request())
        attempt, _fencing_token = self.start_attempt(assignment["assignment_id"])
        failure_payload = {
            "kind": "role_assignment_failed",
            "error": "worker process exited",
        }
        failure_ref = self.artifacts.put_json(
            failure_payload,
            artifact_type="RoleAssignmentFailureArtifact",
        )
        receipt = self.repository.record_role_failure_result(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            error_kind="worker_process_failed",
            error_text="worker process exited",
            failure_artifact_ref=failure_ref.to_dict(),
            payload_hash=stable_hash(failure_payload),
            settlement_action={"action_type": "ROLE_FAILED"},
        )
        self.assertEqual(
            self.repository.read_role_assignment(assignment["assignment_id"])["state"],
            "result_recorded",
        )
        self.assertIsNone(
            self.repository.read_lease(f"assignment:{assignment['assignment_id']}")
        )

        outcome = self.repository.dispatch(
            ActionEnvelope(
                action_type="ROLE_FAILED",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="manager",
                expected_version=3,
                idempotency_key="settle-worker-failure",
                payload={
                    "failure_artifact_ref": failure_ref.to_dict(),
                    "blocker": {
                        "kind": "role_failure",
                        "summary": "worker process exited",
                    },
                },
            ),
            role_assignment_id=assignment["assignment_id"],
            role_submission_payload_hash=receipt.payload_hash,
        )

        self.assertEqual(outcome.snapshot.state, "TRIAGE_REQUIRED")
        self.assertEqual(
            self.repository.read_role_assignment(assignment["assignment_id"])["state"],
            "settled",
        )
        self.assertEqual(
            self.repository.read_role_attempt(attempt["attempt_id"])["status"],
            "failed",
        )
        with self.assertRaisesRegex(ValueError, "lives for its Module identity"):
            self.repository.complete_role_session("session-router")

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
        self.repository.dispatch(
            ActionEnvelope(
                action_type="REJECT_WORKFLOW",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="workflow-router",
                actor="test",
                expected_version=2,
            )
        )
        self.assertTrue(
            self.repository.complete_role_session(
                "session-router",
                status="cancelled",
            )
        )
        self.assertEqual(
            self.repository.read_role_session("session-router")["status"],
            "cancelled",
        )

    def test_settled_submission_reconciliation_failure_can_triage_parent(self) -> None:
        for action in (
            ActionEnvelope(
                action_type="DEPENDENCIES_ACCEPTED",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="test",
                expected_version=1,
                payload={"accepted_dependency_node_ids": []},
            ),
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
            ),
        ):
            self.repository.dispatch(action)
        assignment = self.repository.create_role_assignment(self.request())
        attempt, fencing_token = self.start_attempt(assignment["assignment_id"])
        payload_hash = stable_hash({"status": "candidate_ready"})
        self.repository.record_role_submission(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=fencing_token,
            artifact_ref=self.submission_ref.to_dict(),
            payload_hash=payload_hash,
            settlement_action={"action_type": "SUBMIT_CANDIDATE"},
        )
        self.repository.settle_role_assignment(
            assignment_id=assignment["assignment_id"],
            submission_payload_hash=payload_hash,
        )

        result = SemanticOrchestrator(
            BunshinV2WorkflowService(self.runtime_root)
        )._settle_background_role_failure(
            {
                "effect_type": "run_implementation_role",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": "node-router",
            },
            self.repository.read_role_assignment(assignment["assignment_id"]),
            RuntimeError("submission could not be applied"),
            exhausted=True,
        )

        self.assertEqual(result["status"], "triage_required")
        self.assertEqual(
            self.repository.read_snapshot(
                AggregateType.DAG_NODE_RUN,
                "node-router",
            ).state,
            "TRIAGE_REQUIRED",
        )
        self.assertEqual(
            self.repository.read_role_assignment(assignment["assignment_id"])["state"],
            "settled",
        )

        triaged = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            "node-router",
        )
        resumed = self.repository.dispatch(
            ActionEnvelope(
                action_type="RESOLVE_TRIAGE",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="operator",
                expected_version=triaged.version,
                payload={"triage_resolution": "manager defect repaired"},
            )
        ).snapshot
        self.assertEqual(resumed.state, "QUEUED")
        producing = self.repository.dispatch(
            ActionEnvelope(
                action_type="START_PRODUCING",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id="node-router",
                actor="test",
                expected_version=resumed.version,
                payload={
                    "fencing_token": 2,
                    "active_worker_id": "session-router",
                    "lease_resource_key": "node:node-router:writer",
                },
            )
        ).snapshot
        self.assertEqual(producing.state, "PRODUCING")

        replayed = SemanticOrchestrator(
            BunshinV2WorkflowService(self.runtime_root)
        )._settle_background_role_failure(
            {
                "effect_type": "run_implementation_role",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": "node-router",
            },
            self.repository.read_role_assignment(assignment["assignment_id"]),
            RuntimeError("submission still cannot be applied"),
            exhausted=True,
        )

        self.assertEqual(replayed["status"], "triage_required")
        self.assertEqual(
            self.repository.read_snapshot(
                AggregateType.DAG_NODE_RUN,
                "node-router",
            ).state,
            "TRIAGE_REQUIRED",
        )

    def test_assignment_key_cannot_be_reused_for_different_inputs(self) -> None:
        self.repository.create_role_assignment(self.request())
        changed = RoleAssignmentRequest(
            **{
                **self.request().to_payload(),
                "required_inputs": ("module_work_view",),
                "input_refs": {"module_work_view": self.input_ref.to_dict()},
                "input_fingerprint": "different",
            }
        )
        with self.assertRaisesRegex(ValueError, "different inputs"):
            self.repository.create_role_assignment(changed)

    def test_session_cannot_cross_aggregate_ownership(self) -> None:
        with self.assertRaisesRegex(ValueError, "identity is immutable"):
            self.repository.ensure_role_session(
                session_id="session-router",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="revision-after-finding",
                role="implementation",
                mode="produce",
                role_profile_id="software_engineering.v2_coder",
                family_binding_sha="binding",
                scope_kind="architecture_cycle",
                subject_key="revision-after-finding",
            )
        request = RoleAssignmentRequest(
            **{
                **self.request(key="router-repair").to_payload(),
                "aggregate_id": "node-router-repair",
            }
        )
        with self.assertRaisesRegex(ValueError, "outside its workflow"):
            self.repository.create_role_assignment(request)

    def test_produce_and_repair_are_modes_of_one_implementation_session(self) -> None:
        produce = self.repository.create_role_assignment(
            self.request(key="router-produce")
        )
        self.repository.cancel_role_assignments(
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            reason="produce assignment settled before repair",
        )
        self.repository.ensure_role_session(
            session_id="session-router",
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            role="implementation",
            mode="repair",
            role_profile_id="software_engineering.v2_coder",
            family_binding_sha="binding",
            scope_kind="module",
            subject_key="router",
        )
        repair = self.repository.create_role_assignment(
            RoleAssignmentRequest(
                **{
                    **self.request(key="router-repair").to_payload(),
                    "mode": "repair",
                }
            )
        )
        self.assertEqual(produce["session_id"], repair["session_id"])
        self.assertEqual(
            self.repository.read_role_session("session-router")["role"],
            "implementation",
        )

    def test_stale_node_preserves_its_role_session_for_requeue(self) -> None:
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
        assignment = self.repository.create_role_assignment(
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
            BunshinV2OutboxProcessor(
                BunshinV2WorkflowService(self.runtime_root)
            )._execute_mechanical(stale_effect)
        )

        self.assertEqual(
            self.repository.read_role_assignment(assignment["assignment_id"])["state"],
            "cancelled",
        )
        self.assertEqual(
            self.repository.read_role_session("session-router")["status"],
            "suspended",
        )
        with self.assertRaisesRegex(ValueError, "lives for its Module identity"):
            self.repository.complete_role_session("session-router")

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
        resumed = self.repository.create_role_assignment(
            self.request(key="router-after-stale")
        )
        self.assertEqual(resumed["session_id"], "session-router")

    def test_attempt_access_token_is_scoped_and_fenced(self) -> None:
        assignment = self.repository.create_role_assignment(self.request())
        attempt, fencing_token = self.start_attempt(assignment["assignment_id"])
        token = self.repository.issue_role_attempt_access_token(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=fencing_token,
        )
        authenticated = self.repository.authenticate_role_attempt(token)
        self.assertEqual(authenticated["assignment"]["assignment_id"], assignment["assignment_id"])
        self.assertEqual(authenticated["attempt_id"], attempt["attempt_id"])
        with self.assertRaisesRegex(ValueError, "invalid"):
            self.repository.authenticate_role_attempt("not-the-token")

    def test_aggregate_control_cancels_assignment_without_resurrection(self) -> None:
        assignment = self.repository.create_role_assignment(self.request())
        attempt, fencing_token = self.start_attempt(assignment["assignment_id"])
        token = self.repository.issue_role_attempt_access_token(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=fencing_token,
        )

        cancelled = self.repository.cancel_role_assignments(
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            reason="node paused",
        )

        self.assertEqual(len(cancelled), 1)
        self.assertEqual(cancelled[0]["state"], "cancelled")
        self.assertEqual(
            self.repository.read_role_attempt(attempt["attempt_id"])["status"],
            "cancelled",
        )
        self.assertIsNone(
            self.repository.read_lease(f"assignment:{assignment['assignment_id']}")
        )
        with self.assertRaisesRegex(ValueError, "invalid"):
            self.repository.authenticate_role_attempt(token)

        repeated_failure = self.repository.queue_role_attempt_retry(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            error_kind="worker_process_failed",
            error_text="process observed cancellation",
        )
        self.assertEqual(repeated_failure["state"], "cancelled")

    def test_aggregate_control_settles_an_already_recorded_result(self) -> None:
        assignment = self.repository.create_role_assignment(self.request())
        attempt, fencing_token = self.start_attempt(assignment["assignment_id"])
        self.repository.record_role_submission(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=fencing_token,
            artifact_ref=self.submission_ref.to_dict(),
            payload_hash=stable_hash({"status": "candidate_ready"}),
            settlement_action={"action_type": "SUBMIT_CANDIDATE"},
        )

        settled = self.repository.cancel_role_assignments(
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            reason="parent state superseded this result",
        )

        self.assertEqual(settled[0]["state"], "settled")
        self.assertEqual(
            self.repository.read_role_attempt(attempt["attempt_id"])["status"],
            "completed",
        )
        self.assertEqual(
            self.repository.read_role_session("session-router")["status"],
            "suspended",
        )
        self.assertEqual(
            settled[0]["last_error"],
            "parent state superseded this result",
        )

    def test_role_session_rejects_a_second_open_assignment(self) -> None:
        preserved = self.repository.create_role_assignment(self.request(key="preserved"))
        with self.assertRaisesRegex(ValueError, "already has an open assignment"):
            self.repository.create_role_assignment(self.request(key="other"))
        self.assertEqual(
            self.repository.read_role_assignment(preserved["assignment_id"])["state"],
            "queued",
        )

    def test_business_action_and_submission_settlement_commit_atomically(self) -> None:
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id="workflow-settlement",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="workflow-settlement",
                actor="test",
                expected_version=0,
            )
        )
        self.repository.ensure_role_session(
            session_id="session-settlement",
            workflow_id="workflow-settlement",
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id="workflow-settlement",
            role="implementation",
            mode="produce",
            role_profile_id="software_engineering.v2_coder",
            family_binding_sha="binding",
            scope_kind=AggregateType.WORKFLOW.value,
            subject_key="workflow-settlement",
        )
        assignment = self.repository.create_role_assignment(
            RoleAssignmentRequest(
                assignment_key="atomic-settlement",
                session_id="session-settlement",
                workflow_id="workflow-settlement",
                aggregate_type=AggregateType.WORKFLOW.value,
                aggregate_id="workflow-settlement",
                role="implementation",
            mode="produce",
            role_profile_id="software_engineering.v2_coder",
            family_binding_sha="binding",
                input_fingerprint="atomic-input",
                required_inputs=(),
                input_refs={},
                execution_spec={"effect_type": "run_implementation_role"},
                submission_kind="candidate",
            )
        )
        attempt = self.repository.claim_role_assignment(assignment["assignment_id"])
        lease = self.repository.claim_lease(
            f"assignment:{assignment['assignment_id']}",
            attempt["attempt_id"],
            ttl_seconds=120,
            metadata={"workflow_id": "workflow-settlement"},
        )
        self.repository.start_role_attempt(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            lease_resource_key=f"assignment:{assignment['assignment_id']}",
            fencing_token=lease.fencing_token,
            prompt_pack_ref=self.prompt_ref.to_dict(),
        )
        payload_hash = stable_hash({"status": "candidate_ready"})
        self.repository.record_role_submission(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=lease.fencing_token,
            artifact_ref=self.submission_ref.to_dict(),
            payload_hash=payload_hash,
            settlement_action={"action_type": "SETTLE_WORKER_SUBMISSION"},
        )
        action = ActionEnvelope(
            action_type="START_WORKFLOW",
            workflow_id="workflow-settlement",
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id="workflow-settlement",
            actor="worker-supervisor",
            expected_version=1,
            idempotency_key="start-from-worker",
        )

        self.repository.dispatch(action)
        self.assertEqual(
            self.repository.read_role_assignment(assignment["assignment_id"])["state"],
            "result_recorded",
        )
        replay = self.repository.dispatch(
            action,
            role_assignment_id=assignment["assignment_id"],
            role_submission_payload_hash=payload_hash,
        )

        self.assertTrue(replay.duplicate)
        self.assertEqual(
            self.repository.read_role_assignment(assignment["assignment_id"])["state"],
            "settled",
        )


if __name__ == "__main__":
    unittest.main()
