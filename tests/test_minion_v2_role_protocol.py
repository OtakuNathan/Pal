from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pal.minion.v2.artifacts import ContentAddressedArtifactStore
from pal.minion.v2.contracts import ActionEnvelope, AggregateType
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.repository import MinionV2Repository
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.semantic_orchestration import SemanticOrchestrator
from pal.minion.v2.role_protocol import (
    RoleAssignmentAction,
    RoleAssignmentRequest,
    RoleAssignmentState,
    RoleSessionAction,
    RoleSessionState,
    stable_hash,
    role_assignment_target,
    role_session_target,
)


class MinionV2RoleProtocolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal-v2-role-protocol-"))
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
            executor_profile_id="software_engineering.v2_coder",
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
            executor_profile_id="software_engineering.v2_coder",
            family_binding_sha="binding",
            input_fingerprint="input-fingerprint",
            required_inputs=(),
            input_refs={"module_work_view": self.input_ref.to_dict()},
            execution_spec={"effect_type": "run_implementation_role"},
            submission_kind="candidate",
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

    def test_module_verifier_session_lives_until_workflow_terminal(self) -> None:
        session_id = "session-router-candidate-a"
        self.repository.ensure_role_session(
            session_id=session_id,
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            role="verifier",
            mode="module",
            executor_profile_id="software_engineering.v2_verifier",
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
            executor_profile_id="software_engineering.v2_verifier",
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
        with self.assertRaisesRegex(ValueError, "lives for the workflow"):
            self.repository.complete_role_session(session_id)
        workflow = self.repository.read_snapshot(
            AggregateType.WORKFLOW,
            "workflow-router",
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="REJECT_WORKFLOW",
                workflow_id="workflow-router",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="workflow-router",
                actor="test",
                expected_version=workflow.version,
            )
        )
        self.assertTrue(self.repository.complete_role_session(session_id))
        self.assertEqual(
            self.repository.read_role_session(session_id)["status"],
            RoleSessionState.COMPLETED.value,
        )

    def test_role_session_scope_is_immutable(self) -> None:
        session = self.repository.ensure_role_session(
            session_id="session-router",
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            role="implementation",
            mode="repair",
            executor_profile_id="software_engineering.v2_coder",
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
                executor_profile_id="software_engineering.v2_coder",
                family_binding_sha="binding",
                scope_kind="module",
                subject_key="other_module",
            )

    def test_v19_migrates_legacy_roles_and_quiesces_active_session(self) -> None:
        assignment = self.repository.create_role_assignment(self.request())
        with sqlite3.connect(str(self.repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_schema_meta SET schema_value = '14' "
                "WHERE schema_key = 'schema_version'"
            )
            connection.execute(
                "UPDATE minion_v2_role_sessions SET role = 'repair' "
                "WHERE session_id = 'session-router'"
            )
            connection.execute(
                "UPDATE minion_v2_role_assignments SET state = 'triage_required' "
                "WHERE assignment_id = ?",
                (assignment["assignment_id"],),
            )

        self.repository.ensure_schema()

        session = self.repository.read_role_session("session-router")
        migrated = self.repository.read_role_assignment(assignment["assignment_id"])
        self.assertEqual(session["role"], "implementation")
        self.assertEqual(session["mode"], "produce")
        self.assertEqual(session["status"], "cancelled")
        self.assertEqual(migrated["state"], "cancelled")
        self.repository.ensure_role_session(
            session_id="session-router",
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            role="implementation",
            mode="repair",
            executor_profile_id="software_engineering.v2_coder",
            family_binding_sha="binding",
            scope_kind="module",
            subject_key="router",
        )

    def test_v19_cutover_triages_only_active_work_and_preserves_completed_history(self) -> None:
        completed_ref = self.artifacts.put_json(
            {"status": "complete"},
            artifact_type="PublishedBranchArtifact",
        )
        for action in (
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id="workflow-completed",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="workflow-completed",
                actor="test",
                expected_version=0,
                payload={"history_marker": "keep"},
            ),
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id="workflow-completed",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="workflow-completed",
                actor="test",
                expected_version=1,
            ),
            ActionEnvelope(
                action_type="MARK_COMPLETED",
                workflow_id="workflow-completed",
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id="workflow-completed",
                actor="test",
                expected_version=2,
                payload={"result_artifact_ref": completed_ref.to_dict()},
            ),
        ):
            self.repository.dispatch(action)
        with sqlite3.connect(str(self.repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_schema_meta SET schema_value = '18' "
                "WHERE schema_key = 'schema_version'"
            )

        self.repository.ensure_schema()

        active = self.repository.read_snapshot(AggregateType.WORKFLOW, "workflow-router")
        node = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, "node-router")
        completed = self.repository.read_snapshot(
            AggregateType.WORKFLOW,
            "workflow-completed",
        )
        assert active is not None and node is not None and completed is not None
        self.assertEqual(active.state, "TRIAGE_REQUIRED")
        self.assertEqual(active.payload["blocker"]["kind"], "orchestration_contract_changed")
        self.assertEqual(node.state, "TRIAGE_REQUIRED")
        self.assertEqual(
            self.repository.read_role_session("session-router")["status"],
            "cancelled",
        )
        self.assertEqual(completed.state, "COMPLETED")
        self.assertEqual(completed.payload["history_marker"], "keep")
        self.assertNotIn("blocker", completed.payload)

    def test_v18_renames_legacy_protocol_tables_without_a_compatibility_view(self) -> None:
        with sqlite3.connect(str(self.repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_schema_meta SET schema_value = '17' "
                "WHERE schema_key = 'schema_version'"
            )
            for current, legacy in (
                ("minion_v2_role_invocations", "minion_v2_worker_invocations"),
                ("minion_v2_role_sessions", "minion_v2_worker_sessions"),
                ("minion_v2_role_assignments", "minion_v2_worker_assignments"),
                ("minion_v2_role_attempts", "minion_v2_worker_attempts"),
                ("minion_v2_role_turns", "minion_v2_worker_turns"),
            ):
                connection.execute(f"ALTER TABLE {current} RENAME TO {legacy}")

        self.repository.ensure_schema()

        with sqlite3.connect(str(self.repository.db_path)) as connection:
            names = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        for name in (
            "invocations",
            "sessions",
            "assignments",
            "attempts",
            "turns",
        ):
            self.assertIn(f"minion_v2_role_{name}", names)
            self.assertNotIn(f"minion_v2_worker_{name}", names)

    def test_v18_migrates_profile_named_legacy_invocations(self) -> None:
        legacy_roles = (
            ("v2_architect", "architect", "author", "software_engineering.v2_architect"),
            (
                "v2_architecture_reviewer",
                "reviewer",
                "architecture",
                "software_engineering.v2_reviewer",
            ),
            ("v2_reviewer", "reviewer", "standalone", "software_engineering.v2_reviewer"),
            ("v2_coder", "implementation", "produce", "software_engineering.v2_coder"),
            ("v2_verifier", "verifier", "module", "software_engineering.v2_verifier"),
        )
        now = "2026-07-21T00:00:00+00:00"
        with sqlite3.connect(str(self.repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_schema_meta SET schema_value = '17' "
                "WHERE schema_key = 'schema_version'"
            )
            for index, (legacy_role, _, _, _) in enumerate(legacy_roles):
                connection.execute(
                    """
                    INSERT INTO minion_v2_role_invocations(
                        invocation_id, workflow_id, aggregate_type, aggregate_id,
                        lease_resource_key, fencing_token, role, prompt_pack_ref_json,
                        status, created_at, updated_at
                    ) VALUES (?, 'legacy-workflow', 'dag_node_run', 'legacy-node',
                              ?, 1, ?, '{}', 'completed', ?, ?)
                    """,
                    (f"legacy-invocation-{index}", f"legacy-lease-{index}", legacy_role, now, now),
                )

        self.repository.ensure_schema()

        with sqlite3.connect(str(self.repository.db_path)) as connection:
            migrated = connection.execute(
                """
                SELECT role, mode, executor_profile_id
                FROM minion_v2_role_invocations
                WHERE workflow_id = 'legacy-workflow'
                ORDER BY invocation_id
                """
            ).fetchall()
        self.assertEqual(
            migrated,
            [(role, mode, profile) for _, role, mode, profile in legacy_roles],
        )

    def test_v18_pins_legacy_task_to_its_latest_workflow_family_binding(self) -> None:
        binding_ref = self.artifacts.put_json(
            {"schema_version": "3", "family_id": "software_engineering"},
            artifact_type="FamilyBindingArtifact",
            schema_version="3",
        ).to_dict()
        now = "2026-07-21T00:00:00+00:00"
        with sqlite3.connect(str(self.repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_schema_meta SET schema_value = '17' "
                "WHERE schema_key = 'schema_version'"
            )
            connection.execute(
                """
                INSERT INTO minion_v2_aggregate_snapshots(
                    aggregate_type, aggregate_id, workflow_id, state, version,
                    payload_json, created_at, updated_at
                ) VALUES ('task', 'legacy-task', '', 'ACTIVE', 1, ?, ?, ?)
                """,
                (
                    json.dumps({"family_id": "software_engineering"}),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO minion_v2_aggregate_snapshots(
                    aggregate_type, aggregate_id, workflow_id, state, version,
                    payload_json, created_at, updated_at
                ) VALUES ('workflow', 'legacy-workflow', 'legacy-workflow',
                          'ACTIVE', 1, ?, ?, ?)
                """,
                (
                    json.dumps(
                        {
                            "task_id": "legacy-task",
                            "family_binding_ref": binding_ref,
                        }
                    ),
                    now,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO minion_v2_task_projection(
                    task_id, state, title, objective, profile_id, family_id,
                    workspace_key, task_revision_sha, owner, updated_at
                ) VALUES ('legacy-task', 'ACTIVE', 'Legacy', 'Migrate', '',
                          'software_engineering', '', '', 'pal', ?)
                """,
                (now,),
            )

        self.repository.ensure_schema()

        task = self.repository.read_snapshot(AggregateType.TASK, "legacy-task")
        self.assertEqual(
            task.payload["primary_profile_id"],
            "software_engineering.v2_coder",
        )
        self.assertEqual(task.payload["family_binding_ref"], binding_ref)
        projected = self.repository.search_tasks(
            task_id="legacy-task", include_archived=True
        )[0]
        self.assertEqual(projected["profile_id"], "software_engineering.v2_coder")

    def test_v16_closes_legacy_revision_pending_snapshots(self) -> None:
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id="workflow-edit",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="arch-edit",
                actor="test",
                expected_version=0,
                idempotency_key="create-edit-revision",
            )
        )
        with sqlite3.connect(str(self.repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_schema_meta SET schema_value = '15' "
                "WHERE schema_key = 'schema_version'"
            )
            connection.execute(
                "UPDATE minion_v2_aggregate_snapshots SET state = 'REVISION_PENDING' "
                "WHERE aggregate_type = 'architecture_revision' AND aggregate_id = 'arch-edit'"
            )

        self.repository.ensure_schema()

        snapshot = self.repository.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            "arch-edit",
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.state, "SUPERSEDED")

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
        with self.assertRaisesRegex(ValueError, "lives for the workflow"):
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
            MinionV2WorkflowService(self.runtime_root)
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
                executor_profile_id="software_engineering.v2_coder",
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
        self.repository.ensure_role_session(
            session_id="session-router",
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            role="implementation",
            mode="repair",
            executor_profile_id="software_engineering.v2_coder",
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
            MinionV2OutboxProcessor(
                MinionV2WorkflowService(self.runtime_root)
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
        with self.assertRaisesRegex(ValueError, "lives for the workflow"):
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

    def test_assignment_cancellation_can_preserve_reused_submission(self) -> None:
        preserved = self.repository.create_role_assignment(self.request(key="preserved"))
        other = self.repository.create_role_assignment(self.request(key="other"))

        cancelled = self.repository.cancel_role_assignments(
            workflow_id="workflow-router",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router",
            reason="superseded by preserved receipt",
            exclude_assignment_id=preserved["assignment_id"],
        )

        self.assertEqual([item["assignment_id"] for item in cancelled], [other["assignment_id"]])
        self.assertEqual(
            self.repository.read_role_assignment(preserved["assignment_id"])["state"],
            "queued",
        )
        self.assertEqual(
            self.repository.read_role_assignment(other["assignment_id"])["state"],
            "cancelled",
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
            executor_profile_id="software_engineering.v2_coder",
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
            executor_profile_id="software_engineering.v2_coder",
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
