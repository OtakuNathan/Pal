from __future__ import annotations

import sqlite3
import shutil
import tempfile
import unittest
import time
from pathlib import Path

from pal.minion.v2 import (
    ActionEnvelope,
    AggregateSnapshot,
    AggregateType,
    ContentAddressedArtifactStore,
    MinionV2Repository,
    TransitionError,
    build_default_transition_engine,
)
from pal.minion.v2.contracts import (
    AggregateVersionConflict,
    ArchitectureRevisionState,
    DagNodeRunState,
    LeaseConflict,
    StaleFencingToken,
    TransitionGuardError,
    UnknownTransitionError,
    WorkflowState,
    ExecutionEpochState,
    StandaloneReviewState,
    TaskState,
)
from pal.minion.v2.recovery import MinionV2Recovery
from pal.minion.v2.sessions import architect_session_id, coder_session_id
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.machines import all_transition_specs
from pal.minion.v2.orchestration import MECHANICAL_EFFECT_TYPES
from pal.minion.v2.workers import SEMANTIC_EFFECT_TYPES


class MinionV2TransitionKernelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.engine = build_default_transition_engine()

    def action(
        self,
        action_type: str,
        aggregate_type: AggregateType,
        aggregate_id: str,
        *,
        payload: dict | None = None,
        expected_version: int | None = None,
    ) -> ActionEnvelope:
        return ActionEnvelope(
            action_type=action_type,
            workflow_id="wf_test",
            aggregate_type=aggregate_type,
            aggregate_id=aggregate_id,
            actor="test",
            payload=payload or {},
            expected_version=expected_version,
        )

    def test_workflow_transition_matrix_rejects_unknown_action(self) -> None:
        created = self.engine.transition(
            None,
            self.action("CREATE_WORKFLOW", AggregateType.WORKFLOW, "wf_test"),
        ).snapshot
        self.assertEqual(created.state, WorkflowState.CREATED)
        self.assertEqual(
            self.engine.legal_actions(AggregateType.WORKFLOW, created.state),
            ("ENTER_TRIAGE", "REQUEST_CANCEL", "REQUEST_PAUSE", "START_WORKFLOW"),
        )
        with self.assertRaises(UnknownTransitionError):
            self.engine.transition(
                created,
                self.action("MARK_COMPLETED", AggregateType.WORKFLOW, "wf_test", expected_version=1),
            )

    def test_task_family_is_bound_and_task_archival_is_terminal(self) -> None:
        created = self.engine.transition(
            None,
            self.action(
                "CREATE_TASK",
                AggregateType.TASK,
                "task_test",
                payload={"family_id": "software_engineering", "task_revision_ref": {"sha256": "a"}},
            ),
        ).snapshot
        self.assertEqual(created.state, TaskState.ACTIVE)
        archived = self.engine.transition(
            created,
            self.action("ARCHIVE_TASK", AggregateType.TASK, "task_test", expected_version=1),
        ).snapshot
        self.assertEqual(archived.state, TaskState.ARCHIVED)
        self.assertEqual(self.engine.legal_actions(AggregateType.TASK, archived.state), ())

    def test_expected_version_is_checked_before_reducer(self) -> None:
        created = self.engine.transition(
            None,
            self.action("CREATE_WORKFLOW", AggregateType.WORKFLOW, "wf_test"),
        ).snapshot
        with self.assertRaises(AggregateVersionConflict):
            self.engine.transition(
                created,
                self.action("START_WORKFLOW", AggregateType.WORKFLOW, "wf_test", expected_version=0),
            )

    def test_worker_invocation_terminal_status_is_persisted_under_fencing(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal_v2_worker_status_"))
        self.addCleanup(shutil.rmtree, root, True)
        repository = MinionV2Repository(root)
        store = ContentAddressedArtifactStore(root, repository)
        prompt_ref = store.put_json({"prompt": "bounded"}, artifact_type="WorkerPromptPackArtifact")
        lease = repository.claim_lease("architecture:arch-status:requirements", "inv-status", ttl_seconds=60)
        repository.record_worker_invocation(
            invocation_id="inv-status",
            workflow_id="wf-status",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-status",
            lease_resource_key=lease.resource_key,
            fencing_token=lease.fencing_token,
            role="requirements",
            prompt_pack_ref=prompt_ref.to_dict(),
        )

        repository.finish_worker_invocation(
            invocation_id="inv-status",
            fencing_token=lease.fencing_token,
            status="completed",
        )

        with repository._connect() as connection:
            row = connection.execute(
                "SELECT status FROM minion_v2_worker_invocations WHERE invocation_id = 'inv-status'"
            ).fetchone()
        self.assertEqual(row["status"], "completed")

    def test_worker_session_suspends_and_resumes_with_same_continuation(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal_v2_worker_session_"))
        self.addCleanup(shutil.rmtree, root, True)
        repository = MinionV2Repository(root)
        store = ContentAddressedArtifactStore(root, repository)
        prompt = store.put_json({"prompt": "initial"}, artifact_type="WorkerPromptPackArtifact")
        continuation = store.put_json(
            {"session_id": "inv-session", "llm_round_count": 7},
            artifact_type="AgentSessionContinuationArtifact",
        )
        lease = repository.claim_lease("architecture:arch-session:architect", "inv-session", ttl_seconds=60)
        repository.record_worker_invocation(
            invocation_id="inv-session",
            workflow_id="wf-session",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-session",
            lease_resource_key=lease.resource_key,
            fencing_token=lease.fencing_token,
            role="v2_architect",
            prompt_pack_ref=prompt.to_dict(),
        )
        repository.suspend_worker_invocation(
            invocation_id="inv-session",
            fencing_token=lease.fencing_token,
            continuation_ref=continuation.to_dict(),
        )
        repository.release_lease(lease.resource_key, lease.owner_id, lease.fencing_token)

        resumed_lease = repository.claim_lease(lease.resource_key, "inv-session", ttl_seconds=60)
        resumed_prompt = store.put_json({"prompt": "review response"}, artifact_type="WorkerPromptPackArtifact")
        repository.record_worker_invocation(
            invocation_id="inv-session",
            workflow_id="wf-session",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-session-revision",
            lease_resource_key=resumed_lease.resource_key,
            fencing_token=resumed_lease.fencing_token,
            role="v2_architect",
            prompt_pack_ref=resumed_prompt.to_dict(),
        )

        invocation = repository.read_worker_invocation("inv-session")
        self.assertEqual(invocation["status"], "running")
        self.assertEqual(invocation["aggregate_id"], "arch-session-revision")
        self.assertEqual(invocation["continuation_ref"]["sha256"], continuation.sha256)
        self.assertEqual(invocation["prompt_pack_ref"]["sha256"], resumed_prompt.sha256)

    def test_role_session_ids_are_stable_across_response_effects(self) -> None:
        self.assertEqual(architect_session_id("wf-1"), architect_session_id("wf-1"))
        self.assertNotEqual(architect_session_id("wf-1"), architect_session_id("wf-2"))
        self.assertEqual(coder_session_id("node-1"), coder_session_id("node-1"))
        self.assertNotEqual(coder_session_id("node-1"), coder_session_id("node-2"))

    def test_running_worker_states_have_fenced_rebind_self_transitions(self) -> None:
        node_cases = {
            DagNodeRunState.PRODUCING: "REBIND_PRODUCER",
            DagNodeRunState.QUIESCING: "REBIND_QUIESCER",
            DagNodeRunState.SNAPSHOTTING: "REBIND_SNAPSHOTTER",
            DagNodeRunState.REVIEWING: "REBIND_REVIEWER",
            DagNodeRunState.REPAIRING: "REBIND_REPAIRER",
        }
        for state, action_type in node_cases.items():
            with self.subTest(state=state):
                snapshot = AggregateSnapshot(
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id="node-rebind",
                    workflow_id="wf_test",
                    state=state,
                    version=3,
                    payload={"fencing_token": 1},
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
                result = self.engine.transition(
                    snapshot,
                    self.action(
                        action_type,
                        AggregateType.DAG_NODE_RUN,
                        "node-rebind",
                        payload={"fencing_token": 2, "active_worker_id": "worker-2"},
                        expected_version=3,
                    ),
                )
                self.assertEqual(result.snapshot.state, state)
                self.assertEqual(result.snapshot.payload["fencing_token"], 2)

        review = AggregateSnapshot(
            aggregate_type=AggregateType.STANDALONE_REVIEW,
            aggregate_id="review-rebind",
            workflow_id="wf_test",
            state=StandaloneReviewState.REVIEWING,
            version=4,
            payload={"fencing_token": 1},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        rebound = self.engine.transition(
            review,
            self.action(
                "REBIND_REVIEWER",
                AggregateType.STANDALONE_REVIEW,
                "review-rebind",
                payload={"fencing_token": 2},
                expected_version=4,
            ),
        )
        self.assertEqual(rebound.snapshot.state, StandaloneReviewState.REVIEWING)

    def test_architecture_typed_findings_route_to_owning_stage(self) -> None:
        snapshot = AggregateSnapshot(
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch_1",
            workflow_id="wf_test",
            state=ArchitectureRevisionState.REVIEWING,
            version=8,
            payload={},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        cases = {
            "REQUIREMENTS_DEFECT": ArchitectureRevisionState.ARCHITECT_QUEUED,
            "CONTRACT_DEFECT": ArchitectureRevisionState.ARCHITECT_QUEUED,
            "ARCHITECTURE_DEFECT": ArchitectureRevisionState.ARCHITECT_QUEUED,
        }
        for action_type, expected_state in cases.items():
            with self.subTest(action_type=action_type):
                result = self.engine.transition(
                    snapshot,
                    self.action(
                        action_type,
                        AggregateType.ARCHITECTURE_REVISION,
                        "arch_1",
                        payload={"finding_artifact_ref": "artifact:test"},
                        expected_version=8,
                    ),
                )
                self.assertEqual(result.snapshot.state, expected_state)
                self.assertEqual(result.effects[0].effect_type, "enqueue_architecture_stage")

    def test_node_candidate_requires_quiescing_before_snapshot(self) -> None:
        coding = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node_1",
            workflow_id="wf_test",
            state=DagNodeRunState.PRODUCING,
            version=3,
            payload={},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        quiescing = self.engine.transition(
            coding,
            self.action(
                "SUBMIT_CANDIDATE",
                AggregateType.DAG_NODE_RUN,
                "node_1",
                payload={"fencing_token": 4},
                expected_version=3,
            ),
        ).snapshot
        self.assertEqual(quiescing.state, DagNodeRunState.QUIESCING)
        with self.assertRaises(TransitionGuardError):
            self.engine.transition(
                quiescing,
                self.action(
                    "QUIESCE_COMPLETED",
                    AggregateType.DAG_NODE_RUN,
                    "node_1",
                    payload={"fencing_token": 4, "workspace_fingerprint": "abc"},
                    expected_version=4,
                ),
            )
        snapshotting = self.engine.transition(
            quiescing,
            self.action(
                "QUIESCE_COMPLETED",
                AggregateType.DAG_NODE_RUN,
                "node_1",
                payload={
                    "fencing_token": 4,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "abc",
                },
                expected_version=4,
            ),
        ).snapshot
        self.assertEqual(snapshotting.state, DagNodeRunState.SNAPSHOTTING)
        with self.assertRaises(TransitionGuardError):
            self.engine.transition(
                snapshotting,
                self.action(
                    "CANDIDATE_SNAPSHOTTED",
                    AggregateType.DAG_NODE_RUN,
                    "node_1",
                    payload={
                        "candidate_ref": "artifact:candidate",
                        "candidate_digest": "deadbeef",
                        "workspace_fingerprint": "changed",
                    },
                    expected_version=5,
                ),
            )

    def test_cancel_wins_over_late_quiesce_completion(self) -> None:
        quiescing = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node_cancel_race",
            workflow_id="wf_test",
            state=DagNodeRunState.QUIESCING,
            version=4,
            payload={"fencing_token": 7},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        cancel_requested = self.engine.transition(
            quiescing,
            self.action(
                "REQUEST_CANCEL",
                AggregateType.DAG_NODE_RUN,
                "node_cancel_race",
                expected_version=4,
            ),
        ).snapshot
        self.assertEqual(cancel_requested.state, DagNodeRunState.CANCEL_REQUESTED)
        with self.assertRaises(UnknownTransitionError):
            self.engine.transition(
                cancel_requested,
                self.action(
                    "QUIESCE_COMPLETED",
                    AggregateType.DAG_NODE_RUN,
                    "node_cancel_race",
                    payload={
                        "fencing_token": 7,
                        "process_group_reaped": True,
                        "exclusive_workspace_lock": True,
                        "workspace_fingerprint": "late",
                    },
                    expected_version=5,
                ),
            )

    def test_pause_resume_returns_to_recorded_architecture_state(self) -> None:
        running = AggregateSnapshot(
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch_1",
            workflow_id="wf_test",
            state=ArchitectureRevisionState.ARCHITECT_RUNNING,
            version=4,
            payload={
                "blocker": {"kind": "old_failure"},
                "active_worker_id": "inv_old",
                "fencing_token": 4,
                "lease_resource_key": "architecture:arch_1:architect",
            },
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        requested = self.engine.transition(
            running,
            self.action("REQUEST_PAUSE", AggregateType.ARCHITECTURE_REVISION, "arch_1", expected_version=4),
        ).snapshot
        paused = self.engine.transition(
            requested,
            self.action("PAUSE_CONFIRMED", AggregateType.ARCHITECTURE_REVISION, "arch_1", expected_version=5),
        ).snapshot
        resumed = self.engine.transition(
            paused,
            self.action("RESUME", AggregateType.ARCHITECTURE_REVISION, "arch_1", expected_version=6),
        ).snapshot
        self.assertEqual(resumed.state, ArchitectureRevisionState.ARCHITECT_QUEUED)
        self.assertNotIn("blocker", resumed.payload)
        self.assertNotIn("active_worker_id", resumed.payload)
        self.assertNotIn("fencing_token", resumed.payload)
        self.assertNotIn("lease_resource_key", resumed.payload)

    def test_transition_table_uses_only_declared_aggregate_states(self) -> None:
        declared = {
            AggregateType.TASK: {str(item) for item in TaskState},
            AggregateType.WORKFLOW: {str(item) for item in WorkflowState},
            AggregateType.ARCHITECTURE_REVISION: {str(item) for item in ArchitectureRevisionState},
            AggregateType.EXECUTION_EPOCH: {str(item) for item in ExecutionEpochState},
            AggregateType.DAG_NODE_RUN: {str(item) for item in DagNodeRunState},
            AggregateType.STANDALONE_REVIEW: {str(item) for item in StandaloneReviewState},
        }
        for aggregate_type, source_state, _action_type in self.engine.registered_keys():
            if source_state is not None:
                self.assertIn(str(source_state), declared[aggregate_type])
        terminal_expectations = {
            (AggregateType.TASK, str(TaskState.ARCHIVED)): (),
            (AggregateType.WORKFLOW, str(WorkflowState.COMPLETED)): ("ARCHIVE",),
            (AggregateType.WORKFLOW, str(WorkflowState.REJECTED)): ("ARCHIVE",),
            (AggregateType.WORKFLOW, str(WorkflowState.CANCELLED)): ("ARCHIVE",),
            (AggregateType.ARCHITECTURE_REVISION, str(ArchitectureRevisionState.ACCEPTED)): (),
            (AggregateType.EXECUTION_EPOCH, str(ExecutionEpochState.COMPLETED)): (),
            (AggregateType.DAG_NODE_RUN, str(DagNodeRunState.ACCEPTED)): (
                "MARK_STALE",
                "MEMORY_CANDIDATE_PUBLISHED",
                "REOPEN_DEPENDENCY",
            ),
            (AggregateType.STANDALONE_REVIEW, str(StandaloneReviewState.COMPLETED)): (),
        }
        for (aggregate_type, state), expected in terminal_expectations.items():
            self.assertEqual(self.engine.legal_actions(aggregate_type, state), expected)

    def test_every_declared_effect_has_exactly_one_execution_owner(self) -> None:
        declared: set[str] = set()
        for spec in all_transition_specs():
            action = self.action(spec.action_type, spec.aggregate_type, "aggregate")
            declared.update(
                effect.effect_type
                for effect in spec.effect_builder({}, action, str(spec.target_state))
            )
        self.assertFalse(MECHANICAL_EFFECT_TYPES & SEMANTIC_EFFECT_TYPES)
        self.assertEqual(declared, MECHANICAL_EFFECT_TYPES | SEMANTIC_EFFECT_TYPES)


class MinionV2PersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_minion_v2_"))
        self.repository = MinionV2Repository(self.runtime_root)
        self.artifacts = ContentAddressedArtifactStore(self.runtime_root, self.repository)

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def action(self, action_type: str, *, version: int | None = None, key: str = "", payload: dict | None = None) -> ActionEnvelope:
        return ActionEnvelope(
            action_type=action_type,
            workflow_id="wf_1",
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id="wf_1",
            actor="test",
            payload=payload or {},
            expected_version=version,
            idempotency_key=key,
        )

    def test_dispatch_is_atomic_and_idempotent(self) -> None:
        first = self.repository.dispatch(self.action("CREATE_WORKFLOW", version=0, key="create"))
        duplicate = self.repository.dispatch(self.action("CREATE_WORKFLOW", version=0, key="create"))
        self.assertFalse(first.duplicate)
        self.assertTrue(duplicate.duplicate)
        self.assertEqual(first.snapshot.version, 1)
        self.assertEqual(first.events[0].event_id, duplicate.events[0].event_id)
        self.assertEqual(first.outbox_effect_ids, duplicate.outbox_effect_ids)
        projection = self.repository.read_workflow_projection("wf_1")
        self.assertEqual(projection["current_phase"], "created")
        self.assertEqual(projection["liveness"], "outbox")

    def test_bare_queued_state_without_outbox_is_orphaned(self) -> None:
        self.repository.dispatch(self.action("CREATE_WORKFLOW", version=0, key="queued-create"))
        self.repository.dispatch(self.action("START_WORKFLOW", version=1, key="queued-start"))
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id="wf_1",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="arch_queued",
                actor="test",
                expected_version=0,
                idempotency_key="queued-architecture",
            )
        )
        with sqlite3.connect(str(self.repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_outbox SET status = 'completed' WHERE workflow_id = ?",
                ("wf_1",),
            )
        self.repository.rebuild_workflow_projections()

        projection = self.repository.read_workflow_projection("wf_1")
        self.assertEqual(projection["current_phase"], "architecture")
        self.assertEqual(projection["liveness"], "orphaned")

    def test_idempotency_key_cannot_hide_a_different_request(self) -> None:
        self.repository.dispatch(self.action("CREATE_WORKFLOW", version=0, key="same"))
        with self.assertRaises(ValueError):
            self.repository.dispatch(self.action("CREATE_WORKFLOW", version=None, key="same", payload={"different": True}))

    def test_idempotent_action_replay_ignores_later_cas_version(self) -> None:
        first = self.repository.dispatch(self.action("CREATE_WORKFLOW", version=0, key="replay"))
        replay = self.repository.dispatch(self.action("CREATE_WORKFLOW", version=99, key="replay"))
        self.assertTrue(replay.duplicate)
        self.assertEqual(replay.snapshot.version, first.snapshot.version)

    def test_outbox_receipt_prevents_duplicate_completion(self) -> None:
        result = self.repository.dispatch(self.action("CREATE_WORKFLOW", version=0, key="create"))
        claimed = self.repository.claim_outbox("outbox_worker", limit=1)
        self.assertEqual(claimed[0]["effect_id"], result.outbox_effect_ids[0])
        self.assertTrue(self.repository.complete_outbox_effect(claimed[0]["effect_id"], worker_id="outbox_worker"))
        self.assertFalse(self.repository.complete_outbox_effect(claimed[0]["effect_id"], worker_id="outbox_worker"))
        self.assertEqual(self.repository.claim_outbox("other_worker", limit=10), ())

    def test_worker_timing_metrics_are_persisted_in_workflow_status_projection(self) -> None:
        self.repository.dispatch(self.action("CREATE_WORKFLOW", version=0, key="metrics-create"))
        self.repository.dispatch(self.action("START_WORKFLOW", version=1, key="metrics-start"))
        prompt = self.artifacts.put_json({"prompt": "test"}, artifact_type="WorkerPromptPackArtifact")
        response = self.artifacts.put_json({"response": "test"}, artifact_type="WorkerTerminalArtifact")
        summary = self.artifacts.put_json({"tools": []}, artifact_type="WorkerToolSummaryArtifact")
        lease = self.repository.claim_lease(
            "worker:metrics",
            "inv_metrics",
            ttl_seconds=60,
            metadata={"workflow_id": "wf_1"},
        )
        self.repository.record_worker_invocation(
            invocation_id="inv_metrics",
            workflow_id="wf_1",
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id="wf_1",
            lease_resource_key=lease.resource_key,
            fencing_token=lease.fencing_token,
            role="v2_architecture_reviewer",
            prompt_pack_ref=prompt.to_dict(),
        )
        self.repository.record_worker_turn(
            invocation_id="inv_metrics",
            fencing_token=lease.fencing_token,
            turn_index=1,
            llm_request_ref=prompt.to_dict(),
            llm_response_ref=response.to_dict(),
            tool_summary_ref=summary.to_dict(),
            latency_ms=120,
            tool_latency_ms=35,
            wall_latency_ms=180,
        )

        metrics = self.repository.read_workflow_projection("wf_1")["metrics"]
        self.assertEqual(metrics["llm_time_ms"], 120)
        self.assertEqual(metrics["tool_time_ms"], 35)
        self.assertEqual(metrics["worker_time_ms"], 180)
        self.assertEqual(metrics["review_time_ms"], 120)

    def test_worker_progress_events_advance_durable_round_ledger(self) -> None:
        prompt = self.artifacts.put_json({"prompt": "test"}, artifact_type="WorkerPromptPackArtifact")
        lease = self.repository.claim_lease("worker:events", "inv_events", ttl_seconds=60)
        self.repository.record_worker_invocation(
            invocation_id="inv_events",
            workflow_id="wf_1",
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id="wf_1",
            lease_resource_key=lease.resource_key,
            fencing_token=lease.fencing_token,
            role="architect",
            prompt_pack_ref=prompt.to_dict(),
        )
        self.repository.record_worker_event(
            {
                "invocation_id": "inv_events",
                "event_kind": "progress",
                "created_at": "2026-01-01T00:00:01+00:00",
                "payload": {"phase": "llm_round_completed", "round": 7, "tool_call_count": 19},
            }
        )
        with self.repository._connect() as connection:
            invocation = connection.execute(
                "SELECT last_completed_turn FROM minion_v2_worker_invocations WHERE invocation_id = 'inv_events'"
            ).fetchone()
            event = connection.execute(
                "SELECT phase, round_index, tool_call_count FROM minion_v2_worker_events WHERE invocation_id = 'inv_events'"
            ).fetchone()
        self.assertEqual(invocation["last_completed_turn"], 7)
        self.assertEqual((event["phase"], event["round_index"], event["tool_call_count"]), ("llm_round_completed", 7, 19))

    def test_lease_fencing_rejects_zombie_worker(self) -> None:
        first = self.repository.claim_lease("worktree:node_1", "worker_1", ttl_seconds=60)
        with self.assertRaises(LeaseConflict):
            self.repository.claim_lease("worktree:node_1", "worker_2", ttl_seconds=60)
        self.repository.release_lease(first.resource_key, first.owner_id, first.fencing_token)
        second = self.repository.claim_lease("worktree:node_1", "worker_2", ttl_seconds=60)
        self.assertGreater(second.fencing_token, first.fencing_token)
        with self.assertRaises(StaleFencingToken):
            self.repository.assert_fencing_token(first.resource_key, first.owner_id, first.fencing_token)

    def test_artifact_is_published_before_action_can_reference_it(self) -> None:
        ref = self.artifacts.put_json(
            {"requirements": [{"id": "R-1", "text": "do the thing"}]},
            artifact_type="RequirementsArtifact",
        )
        self.assertTrue(self.repository.artifact_is_durable(ref.sha256))
        self.assertEqual(self.artifacts.read_json(ref)["requirements"][0]["id"], "R-1")

        missing = {**ref.to_dict(), "sha256": "0" * 64}
        with self.assertRaises(ValueError):
            self.repository.dispatch(
                self.action(
                    "CREATE_WORKFLOW",
                    version=0,
                    key="missing-artifact",
                    payload={"requirements_ref": missing},
                )
            )

    def test_artifact_manifest_requires_durable_children(self) -> None:
        requirements = self.artifacts.put_json(
            {"requirements": []},
            artifact_type="RequirementsArtifact",
        )
        manifest = self.artifacts.put_json(
            {"requirements_ref": requirements.to_dict()},
            artifact_type="ArchitectureContractArtifact",
            child_refs=((requirements.sha256, "requirements"),),
        )
        self.assertTrue(self.repository.artifact_is_durable(manifest.sha256))
        with self.assertRaises(ValueError):
            self.artifacts.put_json(
                {"requirements_ref": "sha256:" + "1" * 64},
                artifact_type="ArchitectureContractArtifact",
                child_refs=(("1" * 64, "requirements"),),
            )

    def test_identical_bytes_have_distinct_typed_artifact_addresses(self) -> None:
        constraints = self.artifacts.put_json([], artifact_type="GlobalConstraintsArtifact")
        decisions = self.artifacts.put_json([], artifact_type="DesignDecisionsArtifact")
        self.assertNotEqual(constraints.sha256, decisions.sha256)
        self.assertEqual(self.artifacts.read_json(constraints), [])
        self.assertEqual(self.artifacts.read_json(decisions), [])

    def test_startup_recovery_reaps_expired_lease_before_new_fencing_token(self) -> None:
        first = self.repository.claim_lease(
            "worktree:recover",
            "dead_worker",
            ttl_seconds=1,
            metadata={"workflow_id": "wf_recover", "process_group_id": 99999999},
        )
        time.sleep(1.05)
        result = MinionV2Recovery(MinionV2WorkflowService(self.runtime_root)).recover()
        self.assertIn("worktree:recover", result["recovered_leases"])
        second = self.repository.claim_lease("worktree:recover", "new_worker", ttl_seconds=60)
        self.assertGreater(second.fencing_token, first.fencing_token)


if __name__ == "__main__":
    unittest.main()
