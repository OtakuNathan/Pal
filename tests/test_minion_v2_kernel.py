from __future__ import annotations

import json
import sqlite3
import shutil
import tempfile
import unittest
import time
from pathlib import Path
from unittest.mock import patch

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
from pal.minion.v2.sessions import (
    architect_session_id,
    architect_session_id_for_revision,
    coder_session_id,
    node_role_generation,
    verifier_session_id,
)
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.submission_drafts import AUTHORING_CONTRACT_VERSION
from pal.minion.v2.machine_dsl import ControlDisposition, ControlIntent
from pal.minion.v2.machines import (
    LIVENESS_REQUIRED_STATES,
    all_machine_specs,
    all_transition_specs,
)
from pal.minion.v2.orchestration import MECHANICAL_EFFECT_TYPES
from pal.minion.v2.formal import (
    STATE_CLASSIFICATIONS,
    STATE_ENUMS,
    StateClass,
    render_implementation_topology,
    transition_topology,
)
from pal.minion.v2.semantic_orchestration import SemanticOrchestrator, SEMANTIC_EFFECT_TYPES
from pal.minion.v2.role_protocol import RoleAssignmentRequest


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

    def test_workflow_execution_restart_settles_before_replacement(self) -> None:
        created = self.engine.transition(
            None,
            self.action("CREATE_WORKFLOW", AggregateType.WORKFLOW, "wf_test"),
        ).snapshot
        active = self.engine.transition(
            created,
            self.action(
                "START_WORKFLOW",
                AggregateType.WORKFLOW,
                "wf_test",
                expected_version=1,
            ),
        ).snapshot
        requested = self.engine.transition(
            active,
            self.action(
                "REQUEST_EXECUTION_RESTART",
                AggregateType.WORKFLOW,
                "wf_test",
                expected_version=2,
                payload={
                    "restart_execution_request": {
                        "task_id": "task_test",
                        "architecture_manifest_ref": {"sha256": "architecture"},
                        "requirements_ref": {"sha256": "requirements"},
                    }
                },
            ),
        )
        restarting = self.engine.transition(
            requested.snapshot,
            self.action(
                "CHILDREN_CANCELLED",
                AggregateType.WORKFLOW,
                "wf_test",
                expected_version=3,
            ),
        )
        cancelled = self.engine.transition(
            restarting.snapshot,
            self.action(
                "REPLACEMENT_WORKFLOW_STARTED",
                AggregateType.WORKFLOW,
                "wf_test",
                expected_version=4,
                payload={"replacement_workflow_id": "wf_replacement"},
            ),
        )

        self.assertEqual(requested.snapshot.state, WorkflowState.CANCEL_REQUESTED)
        self.assertEqual(requested.effects[0].effect_type, "propagate_cancel")
        self.assertEqual(restarting.snapshot.state, WorkflowState.RESTARTING)
        self.assertEqual(
            restarting.effects[0].effect_type,
            "start_replacement_workflow_from_architecture",
        )
        self.assertEqual(cancelled.snapshot.state, WorkflowState.CANCELLED)
        self.assertEqual(
            cancelled.snapshot.payload["replacement_workflow_id"],
            "wf_replacement",
        )

    def test_ordinary_workflow_cancel_does_not_enter_restarting(self) -> None:
        created = self.engine.transition(
            None,
            self.action("CREATE_WORKFLOW", AggregateType.WORKFLOW, "wf_test"),
        ).snapshot
        requested = self.engine.transition(
            created,
            self.action(
                "REQUEST_CANCEL",
                AggregateType.WORKFLOW,
                "wf_test",
                expected_version=1,
            ),
        ).snapshot
        cancelled = self.engine.transition(
            requested,
            self.action(
                "CHILDREN_CANCELLED",
                AggregateType.WORKFLOW,
                "wf_test",
                expected_version=2,
            ),
        )

        self.assertEqual(cancelled.snapshot.state, WorkflowState.CANCELLED)
        self.assertNotIn("restart_execution_request", cancelled.snapshot.payload)
        self.assertEqual(cancelled.effects, ())

    def test_workflow_restart_cancel_waits_for_replacement_effect_receipt(self) -> None:
        created = self.engine.transition(
            None,
            self.action("CREATE_WORKFLOW", AggregateType.WORKFLOW, "wf_test"),
        ).snapshot
        active = self.engine.transition(
            created,
            self.action(
                "START_WORKFLOW",
                AggregateType.WORKFLOW,
                "wf_test",
                expected_version=1,
            ),
        ).snapshot
        requested = self.engine.transition(
            active,
            self.action(
                "REQUEST_EXECUTION_RESTART",
                AggregateType.WORKFLOW,
                "wf_test",
                expected_version=2,
                payload={"restart_execution_request": {"task_id": "task_test"}},
            ),
        ).snapshot
        restarting = self.engine.transition(
            requested,
            self.action(
                "CHILDREN_CANCELLED",
                AggregateType.WORKFLOW,
                "wf_test",
                expected_version=3,
            ),
        ).snapshot
        cancel_requested = self.engine.transition(
            restarting,
            self.action(
                "REQUEST_CANCEL",
                AggregateType.WORKFLOW,
                "wf_test",
                expected_version=4,
            ),
        )
        cancelled = self.engine.transition(
            cancel_requested.snapshot,
            self.action(
                "REPLACEMENT_WORKFLOW_ABORTED",
                AggregateType.WORKFLOW,
                "wf_test",
                expected_version=5,
            ),
        )

        self.assertEqual(cancel_requested.snapshot.state, WorkflowState.RESTARTING)
        self.assertTrue(cancel_requested.snapshot.payload["restart_cancel_requested"])
        self.assertEqual(cancel_requested.effects, ())
        self.assertEqual(cancelled.snapshot.state, WorkflowState.CANCELLED)

    def test_task_family_is_bound_and_task_archival_is_terminal(self) -> None:
        created = self.engine.transition(
            None,
            self.action(
                "CREATE_TASK",
                AggregateType.TASK,
                "task_test",
                payload={
                    "primary_profile_id": "software_engineering.v2_coder",
                    "family_id": "software_engineering",
                    "family_binding_ref": {"sha256": "binding"},
                    "task_revision_ref": {"sha256": "a"},
                },
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

    def test_role_invocation_terminal_status_is_persisted_under_fencing(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal_v2_worker_status_"))
        self.addCleanup(shutil.rmtree, root, True)
        repository = MinionV2Repository(root)
        store = ContentAddressedArtifactStore(root, repository)
        prompt_ref = store.put_json({"prompt": "bounded"}, artifact_type="RolePromptPackArtifact")
        lease = repository.claim_lease("architecture:arch-status:requirements", "inv-status", ttl_seconds=60)
        repository.record_role_invocation(
            invocation_id="inv-status",
            workflow_id="wf-status",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-status",
            lease_resource_key=lease.resource_key,
            fencing_token=lease.fencing_token,
            role="architect",
            mode="author",
            executor_profile_id="software_engineering.v2_architect",
            family_binding_sha="binding",
            authoring_contract_version=AUTHORING_CONTRACT_VERSION,
            prompt_pack_ref=prompt_ref.to_dict(),
        )

        repository.finish_role_invocation(
            invocation_id="inv-status",
            fencing_token=lease.fencing_token,
            status="completed",
        )

        with repository._connect() as connection:
            row = connection.execute(
                "SELECT status FROM minion_v2_role_invocations WHERE invocation_id = 'inv-status'"
            ).fetchone()
        self.assertEqual(row["status"], "completed")

    def test_role_session_suspends_and_resumes_with_same_continuation(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal_v2_role_session_"))
        self.addCleanup(shutil.rmtree, root, True)
        repository = MinionV2Repository(root)
        store = ContentAddressedArtifactStore(root, repository)
        prompt = store.put_json({"prompt": "initial"}, artifact_type="RolePromptPackArtifact")
        continuation = store.put_json(
            {"session_id": "inv-session", "llm_round_count": 7},
            artifact_type="AgentSessionContinuationArtifact",
        )
        repository.ensure_role_session(
            session_id="inv-session",
            workflow_id="wf-session",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-session",
            role="architect",
            mode="author",
            executor_profile_id="software_engineering.v2_architect",
            family_binding_sha="binding",
            scope_kind="architecture_revision",
            subject_key="arch-session",
        )
        lease = repository.claim_lease("architecture:arch-session:architect", "inv-session", ttl_seconds=60)
        repository.record_role_invocation(
            invocation_id="inv-session",
            workflow_id="wf-session",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-session",
            lease_resource_key=lease.resource_key,
            fencing_token=lease.fencing_token,
            role="architect",
            mode="author",
            executor_profile_id="software_engineering.v2_architect",
            family_binding_sha="binding",
            authoring_contract_version=AUTHORING_CONTRACT_VERSION,
            prompt_pack_ref=prompt.to_dict(),
        )
        repository.suspend_role_invocation(
            invocation_id="inv-session",
            fencing_token=lease.fencing_token,
            continuation_ref=continuation.to_dict(),
        )
        repository.release_lease(lease.resource_key, lease.owner_id, lease.fencing_token)

        resumed_lease = repository.claim_lease(lease.resource_key, "inv-session", ttl_seconds=60)
        resumed_prompt = store.put_json({"prompt": "review response"}, artifact_type="RolePromptPackArtifact")
        repository.record_role_invocation(
            invocation_id="inv-session",
            workflow_id="wf-session",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch-session-revision",
            lease_resource_key=resumed_lease.resource_key,
            fencing_token=resumed_lease.fencing_token,
            role="architect",
            mode="revision",
            executor_profile_id="software_engineering.v2_architect",
            family_binding_sha="binding",
            authoring_contract_version=AUTHORING_CONTRACT_VERSION,
            prompt_pack_ref=resumed_prompt.to_dict(),
        )

        invocation = repository.read_role_invocation("inv-session")
        self.assertEqual(invocation["status"], "running")
        self.assertEqual(invocation["aggregate_id"], "arch-session-revision")
        self.assertEqual(invocation["continuation_ref"]["sha256"], continuation.sha256)
        self.assertEqual(invocation["prompt_pack_ref"]["sha256"], resumed_prompt.sha256)

        worker = SemanticOrchestrator(MinionV2WorkflowService(root))
        restore_path, checkpoint_path = worker._prepare_agent_session_attempt(
            session_id="inv-session",
            attempt_id="attempt-after-local-loss",
        )
        self.assertIsNotNone(restore_path)
        restored = json.loads(Path(restore_path).read_text(encoding="utf-8"))
        self.assertEqual(restored["schema_version"], "2")
        self.assertEqual(restored["scope_kind"], "architecture_revision")
        self.assertEqual(restored["subject_key"], "arch-session")
        self.assertFalse(checkpoint_path.exists())

        checkpoint_payload = {
            **restored,
            "fencing_token": resumed_lease.fencing_token,
        }
        checkpoint_path.write_text(
            json.dumps(checkpoint_payload),
            encoding="utf-8",
        )
        published = worker._publish_agent_session_checkpoint(
            "inv-session",
            resumed_lease.fencing_token,
            checkpoint_path,
        )
        self.assertIsNotNone(published)
        self.assertEqual(
            store.read_json(published)["subject_key"],
            "arch-session",
        )

        checkpoint_path.write_text(
            json.dumps({**checkpoint_payload, "fencing_token": resumed_lease.fencing_token - 1}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "stale fencing token"):
            worker._publish_agent_session_checkpoint(
                "inv-session",
                resumed_lease.fencing_token,
                checkpoint_path,
            )

        checkpoint_path.write_text(
            json.dumps({**checkpoint_payload, "subject_key": "different-subject"}),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(RuntimeError, "wrong subject"):
            worker._publish_agent_session_checkpoint(
                "inv-session",
                resumed_lease.fencing_token,
                checkpoint_path,
            )

    def test_role_session_ids_are_stable_across_response_effects(self) -> None:
        self.assertEqual(
            architect_session_id("wf-1", "arch-1"),
            architect_session_id("wf-1", "arch-1"),
        )
        self.assertNotEqual(
            architect_session_id("wf-1", "arch-1"),
            architect_session_id("wf-1", "arch-2"),
        )
        self.assertNotEqual(
            architect_session_id("wf-1", "arch-1"),
            architect_session_id("wf-2", "arch-1"),
        )
        self.assertEqual(
            architect_session_id_for_revision(
                "wf-1",
                "arch-1",
                {"finding_artifact_ref": {"sha256": "finding-a"}},
            ),
            architect_session_id_for_revision(
                "wf-1",
                "arch-1",
                {"finding_artifact_ref": {"sha256": "finding-a"}},
            ),
        )
        self.assertNotEqual(
            architect_session_id_for_revision(
                "wf-1",
                "arch-1",
                {"finding_artifact_ref": {"sha256": "finding-a"}},
            ),
            architect_session_id_for_revision(
                "wf-1",
                "arch-1",
                {"finding_artifact_ref": {"sha256": "finding-b"}},
            ),
        )
        self.assertNotEqual(
            architect_session_id_for_revision(
                "wf-1",
                "arch-1",
                {
                    "finding_artifact_ref": {"sha256": "finding-a"},
                    "architecture_repair_baseline_ref": {"sha256": "candidate-a"},
                },
            ),
            architect_session_id_for_revision(
                "wf-1",
                "arch-1",
                {
                    "finding_artifact_ref": {"sha256": "finding-a"},
                    "architecture_repair_baseline_ref": {"sha256": "candidate-b"},
                },
            ),
        )
        self.assertNotEqual(
            architect_session_id_for_revision(
                "wf-1",
                "arch-1",
                {
                    "architecture_repair_baseline_ref": {"sha256": "candidate-a"},
                    "architect_session_generation": 1,
                },
            ),
            architect_session_id_for_revision(
                "wf-1",
                "arch-1",
                {
                    "architecture_repair_baseline_ref": {"sha256": "candidate-a"},
                    "architect_session_generation": 2,
                },
            ),
        )
        self.assertEqual(coder_session_id("node-1"), coder_session_id("node-1"))
        self.assertNotEqual(coder_session_id("node-1"), coder_session_id("node-2"))
        self.assertEqual(
            verifier_session_id("node-1", "candidate:a"),
            verifier_session_id("node-1", "candidate:a"),
        )
        self.assertNotEqual(
            verifier_session_id("node-1", "candidate:a"),
            verifier_session_id("node-1", "candidate:b"),
        )
        self.assertNotEqual(
            verifier_session_id("node-1", "candidate:a"),
            verifier_session_id("node-1", "candidate:a", 1),
        )
        self.assertEqual(node_role_generation({}), 0)
        self.assertEqual(node_role_generation({"role_session_generation": 2}), 2)

    def test_role_failure_is_owned_by_parent_aggregate_state_machine(self) -> None:
        cases = (
            (AggregateType.DAG_NODE_RUN, DagNodeRunState.PRODUCING),
            (AggregateType.DAG_NODE_RUN, DagNodeRunState.REVIEWING),
            (AggregateType.DAG_NODE_RUN, DagNodeRunState.REPAIRING),
            (AggregateType.DAG_NODE_RUN, DagNodeRunState.VERIFYING),
            (
                AggregateType.ARCHITECTURE_REVISION,
                ArchitectureRevisionState.ARCHITECT_RUNNING,
            ),
            (
                AggregateType.ARCHITECTURE_REVISION,
                ArchitectureRevisionState.REVIEWING,
            ),
            (AggregateType.STANDALONE_REVIEW, StandaloneReviewState.REVIEWING),
        )
        for aggregate_type, source_state in cases:
            with self.subTest(aggregate_type=aggregate_type, source_state=source_state):
                snapshot = AggregateSnapshot(
                    aggregate_type=aggregate_type,
                    aggregate_id=f"failed-{aggregate_type.value}",
                    workflow_id="wf_test",
                    state=source_state,
                    version=4,
                    payload={"active_worker_id": "worker"},
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
                result = self.engine.transition(
                    snapshot,
                    self.action(
                        "ROLE_FAILED",
                        aggregate_type,
                        snapshot.aggregate_id,
                        expected_version=4,
                        payload={
                            "failure_artifact_ref": {"sha256": "failure"},
                            "blocker": {
                                "kind": "role_failure",
                                "summary": "worker exited",
                            },
                        },
                    ),
                )
                self.assertEqual(result.snapshot.state, "TRIAGE_REQUIRED")
                self.assertEqual(
                    result.snapshot.payload["triage_resume_state"],
                    str(source_state),
                )
                self.assertIsInstance(result.snapshot.payload["blocker"], dict)

        malformed = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="malformed-worker-failure",
            workflow_id="wf_test",
            state=DagNodeRunState.PRODUCING,
            version=1,
            payload={},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        with self.assertRaisesRegex(TransitionGuardError, "structured mapping"):
            self.engine.transition(
                malformed,
                self.action(
                    "ROLE_FAILED",
                    AggregateType.DAG_NODE_RUN,
                    malformed.aggregate_id,
                    expected_version=1,
                    payload={
                        "failure_artifact_ref": {"sha256": "failure"},
                        "blocker": "worker exited",
                    },
                ),
            )

    def test_every_liveness_required_state_has_an_explicit_triage_transition(self) -> None:
        for aggregate_type, states in LIVENESS_REQUIRED_STATES.items():
            for state in states:
                with self.subTest(aggregate_type=aggregate_type, state=state):
                    self.assertIn(
                        "ENTER_TRIAGE",
                        self.engine.legal_actions(aggregate_type, state),
                    )

    def test_state_classification_is_exhaustive_and_matches_worker_liveness(self) -> None:
        for aggregate_type, state_enum in STATE_ENUMS.items():
            with self.subTest(aggregate_type=aggregate_type):
                self.assertEqual(
                    set(STATE_CLASSIFICATIONS[aggregate_type]),
                    {state.value for state in state_enum},
                )
        classified_worker_liveness = {
            aggregate_type: frozenset(
                state
                for state, state_class in states.items()
                if state_class == StateClass.WORKER_LIVENESS
            )
            for aggregate_type, states in STATE_CLASSIFICATIONS.items()
            if any(
                state_class == StateClass.WORKER_LIVENESS
                for state_class in states.values()
            )
        }
        self.assertEqual(classified_worker_liveness, LIVENESS_REQUIRED_STATES)

    def test_machine_runtime_metadata_owns_every_liveness_state(self) -> None:
        expected_ownership: set[tuple[str, str, str, str, str]] = set()
        for machine in all_machine_specs():
            liveness = {
                state
                for state, state_class in machine.state_classes.items()
                if state_class == StateClass.WORKER_LIVENESS
            }
            self.assertEqual(set(machine.runtime_states), liveness)
            for state, runtime in machine.runtime_states.items():
                self.assertTrue(runtime.activations)
                expected_ownership.update(
                    {
                        (
                            machine.aggregate_type.value,
                            state,
                            activation.role.value,
                            activation.mode.value,
                            runtime.reconciliation.value,
                        )
                        for activation in runtime.activations
                    }
                )
        self.assertEqual(
            set(transition_topology()["role_ownership"]),
            expected_ownership,
        )

    def test_machine_spec_control_states_form_exact_partitions(self) -> None:
        for machine in all_machine_specs():
            for intent in machine.control_policies:
                with self.subTest(
                    aggregate_type=machine.aggregate_type,
                    intent=intent,
                ):
                    partitions = {
                        disposition: machine.control_states(intent, disposition)
                        for disposition in ControlDisposition
                    }
                    self.assertEqual(
                        set().union(*partitions.values()),
                        set(machine.states),
                    )
                    for left in ControlDisposition:
                        for right in ControlDisposition:
                            if left == right:
                                continue
                            self.assertFalse(partitions[left] & partitions[right])
                    request_action = machine.control_policies[intent].request_action
                    self.assertEqual(
                        partitions[ControlDisposition.REQUEST],
                        frozenset(
                            state
                            for state in machine.states
                            if request_action in machine.legal_actions(state)
                        ),
                    )

    def test_dependency_blocked_node_uses_the_declared_pause_cycle(self) -> None:
        created = self.engine.transition(
            None,
            self.action(
                "CREATE_NODE_RUN",
                AggregateType.DAG_NODE_RUN,
                "node_blocked_pause",
                expected_version=0,
                payload={
                    "unit_contract_ref": {"sha256": "contract"},
                    "epoch_id": "epoch_blocked_pause",
                },
            ),
        )
        requested = self.engine.transition(
            created.snapshot,
            self.action(
                "REQUEST_PAUSE",
                AggregateType.DAG_NODE_RUN,
                "node_blocked_pause",
                expected_version=1,
            ),
        )
        paused = self.engine.transition(
            requested.snapshot,
            self.action(
                "PAUSE_CONFIRMED",
                AggregateType.DAG_NODE_RUN,
                "node_blocked_pause",
                expected_version=2,
            ),
        )
        resumed = self.engine.transition(
            paused.snapshot,
            self.action(
                "RESUME",
                AggregateType.DAG_NODE_RUN,
                "node_blocked_pause",
                expected_version=3,
            ),
        )

        self.assertEqual(requested.snapshot.state, "PAUSE_REQUESTED")
        self.assertEqual(paused.snapshot.state, "PAUSED")
        self.assertEqual(resumed.snapshot.state, "BLOCKED_BY_DEPS")
        node_machine = next(
            machine
            for machine in all_machine_specs()
            if machine.aggregate_type == AggregateType.DAG_NODE_RUN
        )
        self.assertEqual(
            node_machine.control_disposition(
                ControlIntent.PAUSE,
                "BLOCKED_BY_DEPS",
            ),
            ControlDisposition.REQUEST,
        )

    def test_generated_transition_topology_is_current(self) -> None:
        generated = Path("spec/minion_v2/ImplementationTopology.tla").read_text(
            encoding="utf-8"
        )
        self.assertEqual(generated, render_implementation_topology())

    def test_generated_dynamic_edges_exactly_match_runtime_declarations(self) -> None:
        topology = transition_topology()
        resolved = set(topology["resolved_transitions"])
        for machine in all_machine_specs():
            for transition in machine.transitions:
                if not callable(transition.target_state):
                    continue
                source = str(transition.source_state)
                actual = {
                    target
                    for aggregate, edge_source, action, target in resolved
                    if aggregate == machine.aggregate_type.value
                    and edge_source == source
                    and action == transition.action_type
                }
                self.assertEqual(
                    actual,
                    set(machine.transition_targets(transition)),
                )

    def test_triage_resolution_preserves_pending_control_intent(self) -> None:
        cases = (
            (AggregateType.WORKFLOW, "PAUSE_REQUESTED", "reconcile_workflow"),
            (
                AggregateType.ARCHITECTURE_REVISION,
                "CANCEL_REQUESTED",
                "reconcile_semantic_state",
            ),
            (
                AggregateType.EXECUTION_EPOCH,
                "PAUSE_REQUESTED",
                "reconcile_execution_epoch",
            ),
            (AggregateType.DAG_NODE_RUN, "CANCEL_REQUESTED", "reconcile_semantic_state"),
            (
                AggregateType.STANDALONE_REVIEW,
                "PAUSE_REQUESTED",
                "reconcile_semantic_state",
            ),
        )
        for aggregate_type, resume_state, expected_effect in cases:
            with self.subTest(aggregate_type=aggregate_type, resume_state=resume_state):
                snapshot = AggregateSnapshot(
                    aggregate_type=aggregate_type,
                    aggregate_id=f"control-triage-{aggregate_type.value}",
                    workflow_id="wf_test",
                    state="TRIAGE_REQUIRED",
                    version=4,
                    payload={"triage_resume_state": resume_state},
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
                result = self.engine.transition(
                    snapshot,
                    self.action(
                        "RESOLVE_TRIAGE",
                        aggregate_type,
                        snapshot.aggregate_id,
                        expected_version=4,
                    ),
                )
                self.assertEqual(result.snapshot.state, resume_state)
                self.assertEqual(
                    [effect.effect_type for effect in result.effects],
                    [expected_effect],
                )

    def test_node_snapshot_failures_record_a_recoverable_resume_state(self) -> None:
        for state, action_type in (
            (DagNodeRunState.QUIESCING, "QUIESCE_FAILED"),
            (DagNodeRunState.SNAPSHOTTING, "SNAPSHOT_FAILED"),
        ):
            with self.subTest(state=state):
                snapshot = AggregateSnapshot(
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=f"snapshot-failure-{state.value}",
                    workflow_id="wf_test",
                    state=state,
                    version=2,
                    payload={},
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
                failed = self.engine.transition(
                    snapshot,
                    self.action(
                        action_type,
                        AggregateType.DAG_NODE_RUN,
                        snapshot.aggregate_id,
                        payload={"failure_artifact_ref": {"sha256": "failure"}},
                        expected_version=2,
                    ),
                )
                self.assertEqual(failed.snapshot.state, DagNodeRunState.TRIAGE_REQUIRED)
                self.assertEqual(failed.snapshot.payload["triage_resume_state"], state.value)
                self.assertEqual(
                    [effect.effect_type for effect in failed.effects],
                    ["quiesce_role_for_triage"],
                )
                resolved = self.engine.transition(
                    failed.snapshot,
                    self.action(
                        "RESOLVE_TRIAGE",
                        AggregateType.DAG_NODE_RUN,
                        snapshot.aggregate_id,
                        expected_version=3,
                    ),
                )
                self.assertEqual(resolved.snapshot.state, state)

    def test_triage_can_record_a_later_failure_without_losing_resume_state(self) -> None:
        snapshot = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="triage-refresh",
            workflow_id="wf_test",
            state=DagNodeRunState.TRIAGE_REQUIRED,
            version=3,
            payload={
                "triage_resume_state": DagNodeRunState.REVIEWING.value,
                "blocker": {"kind": "worker_failed"},
            },
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        refreshed = self.engine.transition(
            snapshot,
            self.action(
                "ENTER_TRIAGE",
                AggregateType.DAG_NODE_RUN,
                snapshot.aggregate_id,
                expected_version=3,
                payload={"blocker": {"kind": "triage_quiesce_failed"}},
            ),
        ).snapshot
        self.assertEqual(refreshed.state, DagNodeRunState.TRIAGE_REQUIRED)
        self.assertEqual(
            refreshed.payload["triage_resume_state"],
            DagNodeRunState.REVIEWING.value,
        )
        self.assertEqual(refreshed.payload["blocker"]["kind"], "triage_quiesce_failed")

    def test_reopened_terminal_node_gets_a_new_role_session_generation(self) -> None:
        accepted = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-reopened",
            workflow_id="wf_test",
            state=DagNodeRunState.ACCEPTED,
            version=9,
            payload={"role_session_generation": 2},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        reopened = self.engine.transition(
            accepted,
            self.action(
                "REOPEN_DEPENDENCY",
                AggregateType.DAG_NODE_RUN,
                accepted.aggregate_id,
                expected_version=9,
                payload={"repair_bill_ref": {"sha256": "repair"}},
            ),
        ).snapshot
        self.assertEqual(reopened.state, DagNodeRunState.REPAIR_QUEUED)
        self.assertEqual(reopened.payload["role_session_generation"], 3)
        self.assertNotEqual(
            coder_session_id(accepted.aggregate_id, 2),
            coder_session_id(reopened.aggregate_id, 3),
        )

    def test_direct_stale_transition_durably_suspends_queued_assignments(self) -> None:
        queued = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-queued-stale",
            workflow_id="wf_test",
            state=DagNodeRunState.QUEUED,
            version=3,
            payload={"role_session_generation": 0},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        result = self.engine.transition(
            queued,
            self.action(
                "MARK_STALE",
                AggregateType.DAG_NODE_RUN,
                queued.aggregate_id,
                expected_version=3,
                payload={"stale_reason_ref": {"sha256": "dependency-change"}},
            ),
        )

        self.assertEqual(result.snapshot.state, DagNodeRunState.STALE)
        self.assertEqual(
            [effect.effect_type for effect in result.effects],
            ["suspend_stale_node_assignments"],
        )
        self.assertEqual(node_role_generation(result.snapshot.payload), 0)

    def test_running_worker_states_have_fenced_rebind_self_transitions(self) -> None:
        node_cases = {
            DagNodeRunState.PRODUCING: "REBIND_PRODUCER",
            DagNodeRunState.QUIESCING: "REBIND_QUIESCER",
            DagNodeRunState.SNAPSHOTTING: "REBIND_SNAPSHOTTER",
            DagNodeRunState.REVIEWING: "REBIND_REVIEWER",
            DagNodeRunState.REPAIRING: "REBIND_REPAIRER",
            DagNodeRunState.VERIFYING: "REBIND_SCENARIO_VERIFIER",
        }
        for state, action_type in node_cases.items():
            with self.subTest(state=state):
                snapshot = AggregateSnapshot(
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id="node-rebind",
                    workflow_id="wf_test",
                    state=state,
                    version=3,
                    payload={
                        "fencing_token": 1,
                        "blocker": {"kind": "effect_failed"},
                        "failure_artifact_ref": {"sha256": "stale"},
                    },
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
                self.assertNotIn("blocker", result.snapshot.payload)
                self.assertNotIn("failure_artifact_ref", result.snapshot.payload)

        review = AggregateSnapshot(
            aggregate_type=AggregateType.STANDALONE_REVIEW,
            aggregate_id="review-rebind",
            workflow_id="wf_test",
            state=StandaloneReviewState.REVIEWING,
            version=4,
            payload={
                "fencing_token": 1,
                "blocker": {"kind": "effect_failed"},
                "failure_artifact_ref": {"sha256": "stale"},
            },
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
        self.assertNotIn("blocker", rebound.snapshot.payload)
        self.assertNotIn("failure_artifact_ref", rebound.snapshot.payload)

    def test_semantic_verifier_has_explicit_quiesce_and_snapshot_boundaries(self) -> None:
        reviewing = AggregateSnapshot(
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-semantic-review",
            workflow_id="wf_test",
            state=DagNodeRunState.REVIEWING,
            version=2,
            payload={"fencing_token": 3, "active_worker_id": "verifier"},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        submitted = self.engine.transition(
            reviewing,
            self.action(
                "SUBMIT_SEMANTIC_VERIFICATION",
                AggregateType.DAG_NODE_RUN,
                reviewing.aggregate_id,
                expected_version=2,
                payload={"pending_verification_ref": {"sha256": "pending"}},
            ),
        )
        self.assertEqual(submitted.snapshot.state, DagNodeRunState.REVIEW_QUIESCING)
        self.assertEqual([item.effect_type for item in submitted.effects], ["quiesce_verifier_role"])

        quiesced = self.engine.transition(
            submitted.snapshot,
            self.action(
                "VERIFIER_QUIESCED",
                AggregateType.DAG_NODE_RUN,
                reviewing.aggregate_id,
                expected_version=3,
                payload={
                    "fencing_token": 3,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "stable",
                },
            ),
        )
        self.assertEqual(
            quiesced.snapshot.state,
            DagNodeRunState.REVIEW_SNAPSHOTTING,
        )
        self.assertEqual(
            [item.effect_type for item in quiesced.effects],
            ["snapshot_verifier_result"],
        )

        triaged = self.engine.transition(
            quiesced.snapshot,
            self.action(
                "ENTER_TRIAGE",
                AggregateType.DAG_NODE_RUN,
                reviewing.aggregate_id,
                expected_version=4,
            ),
        )
        resumed = self.engine.transition(
            triaged.snapshot,
            self.action(
                "RESOLVE_TRIAGE",
                AggregateType.DAG_NODE_RUN,
                reviewing.aggregate_id,
                expected_version=5,
            ),
        )
        self.assertEqual(
            resumed.snapshot.state,
            DagNodeRunState.REVIEW_SNAPSHOTTING,
        )
        self.assertEqual(resumed.snapshot.payload["workspace_fingerprint"], "stable")

    def test_resolve_triage_clears_stale_worker_and_blocker_fields(self) -> None:
        cases = (
            (AggregateType.WORKFLOW, WorkflowState.ACTIVE, WorkflowState.ACTIVE),
            (AggregateType.EXECUTION_EPOCH, ExecutionEpochState.RUNNING, ExecutionEpochState.RUNNING),
            (AggregateType.DAG_NODE_RUN, DagNodeRunState.REVIEWING, DagNodeRunState.REVIEW_QUEUED),
            (AggregateType.STANDALONE_REVIEW, StandaloneReviewState.REVIEWING, StandaloneReviewState.REVIEW_QUEUED),
        )
        for aggregate_type, resume_state, expected_state in cases:
            with self.subTest(aggregate_type=aggregate_type):
                snapshot = AggregateSnapshot(
                    aggregate_type=aggregate_type,
                    aggregate_id=f"triage-{aggregate_type.value}",
                    workflow_id="wf_test",
                    state="TRIAGE_REQUIRED",
                    version=4,
                    payload={
                        "triage_resume_state": str(resume_state),
                        "blocker": {"kind": "effect_failed"},
                        "active_worker_id": "stale-worker",
                        "fencing_token": 7,
                        "lease_resource_key": "stale-lease",
                    },
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
                result = self.engine.transition(
                    snapshot,
                    self.action(
                        "RESOLVE_TRIAGE",
                        aggregate_type,
                        snapshot.aggregate_id,
                        expected_version=4,
                    ),
                )
                self.assertEqual(result.snapshot.state, expected_state)
                for field in ("blocker", "active_worker_id", "fencing_token", "lease_resource_key"):
                    self.assertNotIn(field, result.snapshot.payload)

    def test_worker_completion_transitions_clear_active_lease_fields(self) -> None:
        node_cases = (
            (
                DagNodeRunState.REVIEW_SNAPSHOTTING,
                "REVIEW_PASSED",
                DagNodeRunState.ACCEPTED,
                {"verification_artifact_ref": "artifact:verification"},
            ),
            (
                DagNodeRunState.REVIEW_SNAPSHOTTING,
                "REVIEW_FAILED",
                DagNodeRunState.REPAIR_QUEUED,
                {
                    "verification_artifact_ref": "artifact:verification",
                    "repair_bill_ref": "artifact:repair",
                    "finding_fingerprint": "finding",
                },
            ),
            (
                DagNodeRunState.REVIEW_SNAPSHOTTING,
                "DEPENDENCY_DEFECT",
                DagNodeRunState.STALE,
                {
                    "repair_bill_ref": "artifact:repair",
                    "repair_target_node_id": "dependency",
                },
            ),
            (
                DagNodeRunState.VERIFY_SNAPSHOTTING,
                "VERIFICATION_PASSED",
                DagNodeRunState.ACCEPTED,
                {
                    "verification_artifact_ref": "artifact:verification",
                    "scenario_fingerprint": "scenario",
                },
            ),
            (
                DagNodeRunState.VERIFY_SNAPSHOTTING,
                "MODULE_DEFECT",
                DagNodeRunState.STALE,
                {
                    "repair_bill_ref": "artifact:repair",
                    "repair_target_node_id": "module",
                },
            ),
            (
                DagNodeRunState.PRODUCING,
                "PRODUCER_ARCHITECTURE_DEFECT",
                DagNodeRunState.STALE,
                {"finding_artifact_ref": "artifact:finding"},
            ),
        )
        for source_state, action_type, target_state, action_payload in node_cases:
            with self.subTest(action_type=action_type):
                snapshot = AggregateSnapshot(
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id="node-finished",
                    workflow_id="wf_test",
                    state=source_state,
                    version=6,
                    payload={
                        "node_kind": "verification"
                        if source_state == DagNodeRunState.VERIFY_SNAPSHOTTING
                        else "unit",
                        "active_worker_id": "finished-worker",
                        "fencing_token": 7,
                        "lease_resource_key": "finished-lease",
                    },
                    created_at="2026-01-01T00:00:00+00:00",
                    updated_at="2026-01-01T00:00:00+00:00",
                )
                result = self.engine.transition(
                    snapshot,
                    self.action(
                        action_type,
                        AggregateType.DAG_NODE_RUN,
                        snapshot.aggregate_id,
                        payload=action_payload,
                        expected_version=6,
                    ),
                )
                self.assertEqual(result.snapshot.state, target_state)
                for field in ("active_worker_id", "fencing_token", "lease_resource_key"):
                    self.assertNotIn(field, result.snapshot.payload)

        standalone = AggregateSnapshot(
            aggregate_type=AggregateType.STANDALONE_REVIEW,
            aggregate_id="standalone-finished",
            workflow_id="wf_test",
            state=StandaloneReviewState.REVIEWING,
            version=2,
            payload={
                "active_worker_id": "finished-worker",
                "fencing_token": 3,
                "lease_resource_key": "finished-lease",
            },
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        result = self.engine.transition(
            standalone,
            self.action(
                "REPORT_PRODUCED",
                AggregateType.STANDALONE_REVIEW,
                standalone.aggregate_id,
                payload={"verification_artifact_ref": "artifact:verification"},
                expected_version=2,
            ),
        )
        self.assertEqual(result.snapshot.state, StandaloneReviewState.REPORT_READY)
        for field in ("active_worker_id", "fencing_token", "lease_resource_key"):
            self.assertNotIn(field, result.snapshot.payload)

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
                self.assertEqual(result.effects[0].effect_type, "admit_architect_role")

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

    def test_architect_submission_quiesces_before_manager_snapshot_and_review(self) -> None:
        running = AggregateSnapshot(
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch_skeleton",
            workflow_id="wf_test",
            state=ArchitectureRevisionState.ARCHITECT_RUNNING,
            version=3,
            payload={"fencing_token": 4},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        quiescing_result = self.engine.transition(
            running,
            self.action(
                "ARCHITECT_SUBMITTED",
                AggregateType.ARCHITECTURE_REVISION,
                "arch_skeleton",
                payload={
                    "requirements_ref": {"sha256": "requirements"},
                    "pending_architecture_submission_ref": {"sha256": "submission"},
                    "architecture_workspace_path": "/tmp/architecture",
                    "fencing_token": 4,
                },
                expected_version=3,
            ),
        )
        self.assertEqual(quiescing_result.snapshot.state, ArchitectureRevisionState.ARCHITECT_QUIESCING)
        self.assertEqual(quiescing_result.effects[0].effect_type, "quiesce_architect_role")

        with self.assertRaises(TransitionGuardError):
            self.engine.transition(
                quiescing_result.snapshot,
                self.action(
                    "ARCHITECT_QUIESCED",
                    AggregateType.ARCHITECTURE_REVISION,
                    "arch_skeleton",
                    payload={"fencing_token": 4, "workspace_fingerprint": "tree"},
                    expected_version=4,
                ),
            )
        snapshotting_result = self.engine.transition(
            quiescing_result.snapshot,
            self.action(
                "ARCHITECT_QUIESCED",
                AggregateType.ARCHITECTURE_REVISION,
                "arch_skeleton",
                payload={
                    "fencing_token": 4,
                    "process_group_reaped": True,
                    "exclusive_workspace_lock": True,
                    "workspace_fingerprint": "tree",
                },
                expected_version=4,
            ),
        )
        self.assertEqual(snapshotting_result.snapshot.state, ArchitectureRevisionState.ARCHITECT_SNAPSHOTTING)
        self.assertEqual(snapshotting_result.effects[0].effect_type, "snapshot_architect_result")

        rejected = self.engine.transition(
            snapshotting_result.snapshot,
            self.action(
                "ARCHITECTURE_SNAPSHOT_REJECTED",
                AggregateType.ARCHITECTURE_REVISION,
                "arch_skeleton",
                payload={
                    "finding_artifact_ref": {"sha256": "preflight-finding"},
                    "architecture_repair_baseline_ref": {"sha256": "repair-baseline"},
                },
                expected_version=5,
            ),
        )
        self.assertEqual(rejected.snapshot.state, ArchitectureRevisionState.ARCHITECT_QUEUED)
        self.assertEqual(rejected.effects[0].effect_type, "admit_architect_role")

        reviewed = self.engine.transition(
            snapshotting_result.snapshot,
            self.action(
                "ARCHITECTURE_SNAPSHOTTED",
                AggregateType.ARCHITECTURE_REVISION,
                "arch_skeleton",
                payload={
                    "requirements_ref": {"sha256": "requirements"},
                    "architecture_manifest_ref": {"sha256": "skeleton"},
                },
                expected_version=5,
            ),
        )
        self.assertEqual(reviewed.snapshot.state, ArchitectureRevisionState.REVIEW_QUEUED)
        self.assertEqual(reviewed.effects[0].effect_type, "run_reviewer_role")

    def test_start_architect_clears_stale_quiesce_state(self) -> None:
        queued = AggregateSnapshot(
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch_restart",
            workflow_id="wf_test",
            state=ArchitectureRevisionState.ARCHITECT_QUEUED,
            version=7,
            payload={
                "architecture_manifest_ref": {"sha256": "baseline"},
                "finding_artifact_ref": {"sha256": "finding"},
                "process_group_reaped": True,
                "exclusive_workspace_lock": True,
                "workspace_fingerprint": "old-tree",
                "workspace_lock_path": "/tmp/old.lock",
                "pending_architecture_submission_ref": {"sha256": "old-submission"},
                "failure_artifact_ref": {"sha256": "old-failure"},
                "blocker": {"kind": "old_failure"},
            },
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

        running = self.engine.transition(
            queued,
            self.action(
                "START_ARCHITECT",
                AggregateType.ARCHITECTURE_REVISION,
                "arch_restart",
                payload={
                    "fencing_token": 8,
                    "active_worker_id": "inv_new",
                    "lease_resource_key": "architecture:arch_restart:writer",
                },
                expected_version=7,
            ),
        ).snapshot

        self.assertEqual(running.state, ArchitectureRevisionState.ARCHITECT_RUNNING)
        self.assertEqual(running.payload["architecture_manifest_ref"], {"sha256": "baseline"})
        self.assertEqual(running.payload["finding_artifact_ref"], {"sha256": "finding"})
        self.assertEqual(running.payload["active_worker_id"], "inv_new")
        for field in (
            "process_group_reaped",
            "exclusive_workspace_lock",
            "workspace_fingerprint",
            "workspace_lock_path",
            "pending_architecture_submission_ref",
            "failure_artifact_ref",
            "blocker",
        ):
            self.assertNotIn(field, running.payload)

    def test_resolve_architect_quiesce_triage_preserves_submission_only(self) -> None:
        triage = AggregateSnapshot(
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch_quiesce_triage",
            workflow_id="wf_test",
            state=ArchitectureRevisionState.TRIAGE_REQUIRED,
            version=11,
            payload={
                "triage_resume_state": "ARCHITECT_QUIESCING",
                "pending_architecture_submission_ref": {"sha256": "submission"},
                "process_group_reaped": True,
                "exclusive_workspace_lock": True,
                "workspace_fingerprint": "stale-tree",
                "workspace_lock_path": "/tmp/stale.lock",
                "failure_artifact_ref": {"sha256": "failure"},
                "blocker": {"kind": "effect_failed"},
            },
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

        resumed = self.engine.transition(
            triage,
            self.action(
                "RESOLVE_TRIAGE",
                AggregateType.ARCHITECTURE_REVISION,
                "arch_quiesce_triage",
                expected_version=11,
            ),
        ).snapshot

        self.assertEqual(resumed.state, ArchitectureRevisionState.ARCHITECT_QUIESCING)
        self.assertEqual(resumed.payload["architect_session_generation"], 1)
        self.assertEqual(
            resumed.payload["pending_architecture_submission_ref"],
            {"sha256": "submission"},
        )
        for field in (
            "process_group_reaped",
            "exclusive_workspace_lock",
            "workspace_fingerprint",
            "workspace_lock_path",
            "failure_artifact_ref",
            "blocker",
        ):
            self.assertNotIn(field, resumed.payload)

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

    def test_replan_required_epoch_can_pause_and_resume(self) -> None:
        replan_required = AggregateSnapshot(
            aggregate_type=AggregateType.EXECUTION_EPOCH,
            aggregate_id="epoch_replan_pause",
            workflow_id="wf_test",
            state=ExecutionEpochState.REPLAN_REQUIRED,
            version=7,
            payload={},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

        requested = self.engine.transition(
            replan_required,
            self.action(
                "REQUEST_PAUSE",
                AggregateType.EXECUTION_EPOCH,
                "epoch_replan_pause",
                expected_version=7,
            ),
        ).snapshot
        paused = self.engine.transition(
            requested,
            self.action(
                "NODES_PAUSED",
                AggregateType.EXECUTION_EPOCH,
                "epoch_replan_pause",
                expected_version=8,
            ),
        ).snapshot
        resumed = self.engine.transition(
            paused,
            self.action(
                "RESUME",
                AggregateType.EXECUTION_EPOCH,
                "epoch_replan_pause",
                expected_version=9,
            ),
        ).snapshot

        self.assertEqual(requested.state, ExecutionEpochState.PAUSE_REQUESTED)
        self.assertEqual(paused.state, ExecutionEpochState.PAUSED)
        self.assertEqual(resumed.state, ExecutionEpochState.REPLAN_REQUIRED)

    def test_architecture_review_handoff_clears_worker_and_persists_card(self) -> None:
        reviewing = AggregateSnapshot(
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch_human_review",
            workflow_id="wf_test",
            state=ArchitectureRevisionState.REVIEWING,
            version=4,
            payload={
                "active_worker_id": "inv_reviewer",
                "fencing_token": 9,
                "lease_resource_key": "architecture:arch_human_review:review",
            },
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )
        waiting = self.engine.transition(
            reviewing,
            self.action(
                "ARCHITECTURE_REVIEW_PASSED",
                AggregateType.ARCHITECTURE_REVISION,
                "arch_human_review",
                expected_version=4,
                payload={
                    "review_artifact_ref": {"sha256": "review"},
                    "architecture_manifest_ref": {"sha256": "manifest"},
                },
            ),
        ).snapshot
        self.assertEqual(waiting.state, ArchitectureRevisionState.HUMAN_REVIEW)
        self.assertNotIn("active_worker_id", waiting.payload)
        self.assertNotIn("fencing_token", waiting.payload)
        self.assertNotIn("lease_resource_key", waiting.payload)

        published = self.engine.transition(
            waiting,
            self.action(
                "HUMAN_REVIEW_PUBLISHED",
                AggregateType.ARCHITECTURE_REVISION,
                "arch_human_review",
                expected_version=5,
                payload={"human_review_card_ref": {"sha256": "card"}},
            ),
        ).snapshot
        self.assertEqual(published.state, ArchitectureRevisionState.HUMAN_REVIEW)
        self.assertEqual(published.payload["human_review_card_ref"], {"sha256": "card"})

        reopened = self.engine.transition(
            published,
            self.action(
                "REOPEN_ARCHITECTURE_REVIEW",
                AggregateType.ARCHITECTURE_REVISION,
                "arch_human_review",
                expected_version=6,
                payload={"reason": "the previous reviewer input omitted Verification Topology"},
            ),
        )
        self.assertEqual(reopened.snapshot.state, ArchitectureRevisionState.REVIEW_QUEUED)
        self.assertEqual(reopened.snapshot.payload["architecture_review_generation"], 1)
        self.assertNotIn("review_artifact_ref", reopened.snapshot.payload)
        self.assertNotIn("human_review_card_ref", reopened.snapshot.payload)
        self.assertEqual([effect.effect_type for effect in reopened.effects], ["run_reviewer_role"])

    def test_human_edit_supersedes_the_old_revision_before_creating_the_next(self) -> None:
        waiting = AggregateSnapshot(
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch_edit",
            workflow_id="wf_test",
            state=ArchitectureRevisionState.HUMAN_REVIEW,
            version=3,
            payload={"architecture_manifest_ref": {"sha256": "manifest"}},
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
        )

        edited = self.engine.transition(
            waiting,
            self.action(
                "HUMAN_EDIT",
                AggregateType.ARCHITECTURE_REVISION,
                "arch_edit",
                expected_version=3,
                payload={
                    "decision_token": "decision-edit",
                    "edit_instruction_ref": {"sha256": "edit"},
                },
            ),
        )

        self.assertEqual(edited.snapshot.state, ArchitectureRevisionState.SUPERSEDED)
        self.assertEqual(
            [effect.effect_type for effect in edited.effects],
            ["materialize_plan_revision", "create_architecture_revision"],
        )

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
            (AggregateType.ARCHITECTURE_REVISION, str(ArchitectureRevisionState.SUPERSEDED)): (),
            (AggregateType.EXECUTION_EPOCH, str(ExecutionEpochState.COMPLETED)): (),
            (AggregateType.DAG_NODE_RUN, str(DagNodeRunState.ACCEPTED)): (
                "MARK_STALE",
                "MEMORY_CANDIDATE_PUBLISHED",
                "REOPEN_DEPENDENCY",
                "REOPEN_VERIFICATION",
            ),
            (AggregateType.STANDALONE_REVIEW, str(StandaloneReviewState.COMPLETED)): (),
        }
        for (aggregate_type, state), expected in terminal_expectations.items():
            self.assertEqual(self.engine.legal_actions(aggregate_type, state), expected)

    def test_every_declared_effect_has_exactly_one_execution_owner(self) -> None:
        declared: set[str] = set()
        for spec in all_transition_specs():
            action = self.action(spec.action_type, spec.aggregate_type, "aggregate")
            targets = tuple(getattr(spec.target_state, "target_states", ())) or (
                str(spec.target_state),
            )
            for target in targets:
                declared.update(
                    effect.effect_type
                    for effect in spec.effect_builder({}, action, str(target))
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

    def test_durable_role_assignment_is_a_liveness_source(self) -> None:
        self.repository.dispatch(self.action("CREATE_WORKFLOW", version=0, key="worker-create"))
        self.repository.dispatch(self.action("START_WORKFLOW", version=1, key="worker-start"))
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id="wf_1",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id="arch_worker",
                actor="test",
                expected_version=0,
                idempotency_key="worker-architecture",
            )
        )
        self.repository.ensure_role_session(
            session_id="session-worker",
            workflow_id="wf_1",
            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
            aggregate_id="arch_worker",
            role="architect",
            mode="author",
            executor_profile_id="software_engineering.v2_architect",
            family_binding_sha="binding",
        )
        self.repository.create_role_assignment(
            RoleAssignmentRequest(
                assignment_key="worker-liveness",
                session_id="session-worker",
                workflow_id="wf_1",
                aggregate_type=AggregateType.ARCHITECTURE_REVISION.value,
                aggregate_id="arch_worker",
                role="architect",
                mode="author",
                executor_profile_id="software_engineering.v2_architect",
                family_binding_sha="binding",
                input_fingerprint="worker-input",
                required_inputs=(),
                input_refs={},
                execution_spec={"effect_type": "admit_architect_role"},
                submission_kind="architecture",
            )
        )
        with sqlite3.connect(str(self.repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_outbox SET status = 'completed' WHERE workflow_id = ?",
                ("wf_1",),
            )
        self.repository.rebuild_workflow_projections()

        projection = self.repository.read_workflow_projection("wf_1")
        self.assertEqual(projection["liveness"], "role_assignment")

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
        attempts = self.repository.list_effect_attempts(claimed[0]["effect_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "completed")
        self.assertEqual(attempts[0]["worker_id"], "outbox_worker")

    def test_deferred_outbox_effect_returns_to_queue_without_spending_attempt(self) -> None:
        result = self.repository.dispatch(self.action("CREATE_WORKFLOW", version=0, key="defer"))
        effect_id = result.outbox_effect_ids[0]
        claimed = self.repository.claim_outbox("draining_manager", limit=1)
        self.assertEqual(claimed[0]["attempt_count"], 1)

        self.repository.defer_outbox_effect(
            effect_id,
            worker_id="draining_manager",
            reason="manager restart safe point",
        )

        replay = self.repository.claim_outbox("fresh_manager", limit=1)
        self.assertEqual(replay[0]["effect_id"], effect_id)
        self.assertEqual(replay[0]["attempt_count"], 1)

    def test_deferring_reclaimed_inflight_effect_does_not_erase_prior_attempt(self) -> None:
        result = self.repository.dispatch(self.action("CREATE_WORKFLOW", version=0, key="defer-replay"))
        effect_id = result.outbox_effect_ids[0]
        self.repository.claim_outbox("crashed_manager", limit=1)
        with sqlite3.connect(str(self.repository.db_path)) as connection:
            connection.execute(
                "UPDATE minion_v2_outbox SET locked_until = '2000-01-01T00:00:00+00:00' WHERE effect_id = ?",
                (effect_id,),
            )
        reclaimed = self.repository.claim_outbox("draining_manager", limit=1)
        self.assertFalse(reclaimed[0]["claim_incremented_attempt"])

        self.repository.defer_outbox_effect(
            effect_id,
            worker_id="draining_manager",
            reason="manager restart safe point",
            attempt_was_incremented=False,
        )

        replay = self.repository.claim_outbox("fresh_manager", limit=1)
        self.assertEqual(replay[0]["attempt_count"], 2)

    def test_expired_final_outbox_attempt_is_reclaimed_without_consuming_another_attempt(self) -> None:
        result = self.repository.dispatch(self.action("CREATE_WORKFLOW", version=0, key="crash-window"))
        effect_id = result.outbox_effect_ids[0]
        first_claim = self.repository.claim_outbox("crashed_worker", limit=1)
        self.assertEqual(first_claim[0]["attempt_count"], 1)

        with sqlite3.connect(str(self.repository.db_path)) as connection:
            connection.execute(
                """
                UPDATE minion_v2_outbox
                SET max_attempts = 1, locked_until = '2000-01-01T00:00:00+00:00'
                WHERE effect_id = ?
                """,
                (effect_id,),
            )

        replay = self.repository.claim_outbox("recovery_worker", limit=1)
        self.assertEqual(len(replay), 1)
        self.assertEqual(replay[0]["effect_id"], effect_id)
        self.assertEqual(replay[0]["attempt_count"], 1)
        self.assertEqual(
            self.repository.retry_outbox_effect(
                effect_id,
                worker_id="recovery_worker",
                error="replayed attempt failed",
                retry_after_seconds=0,
            ),
            "failed",
        )
        self.assertEqual(self.repository.claim_outbox("third_worker", limit=1), ())
        attempts = self.repository.list_effect_attempts(effect_id)
        self.assertEqual(
            [(item["worker_id"], item["status"]) for item in attempts],
            [("crashed_worker", "lost"), ("recovery_worker", "failed")],
        )

    def test_deferred_effect_records_claim_history_without_spending_retry_budget(self) -> None:
        result = self.repository.dispatch(self.action("CREATE_WORKFLOW", version=0, key="defer-history"))
        effect_id = result.outbox_effect_ids[0]
        first = self.repository.claim_outbox("draining-manager", limit=1)[0]
        self.repository.defer_outbox_effect(
            effect_id,
            worker_id="draining-manager",
            reason="shutdown safe point",
            attempt_was_incremented=first["claim_incremented_attempt"],
        )
        second = self.repository.claim_outbox("new-manager", limit=1)[0]
        self.assertEqual(second["attempt_count"], 1)
        attempts = self.repository.list_effect_attempts(effect_id)
        self.assertEqual(
            [(item["worker_id"], item["status"]) for item in attempts],
            [("draining-manager", "deferred"), ("new-manager", "running")],
        )

    def test_worker_timing_metrics_are_persisted_in_workflow_status_projection(self) -> None:
        self.repository.dispatch(self.action("CREATE_WORKFLOW", version=0, key="metrics-create"))
        self.repository.dispatch(self.action("START_WORKFLOW", version=1, key="metrics-start"))
        prompt = self.artifacts.put_json({"prompt": "test"}, artifact_type="RolePromptPackArtifact")
        response = self.artifacts.put_json({"response": "test"}, artifact_type="RoleTerminalArtifact")
        summary = self.artifacts.put_json({"tools": []}, artifact_type="RoleToolSummaryArtifact")
        lease = self.repository.claim_lease(
            "worker:metrics",
            "inv_metrics",
            ttl_seconds=60,
            metadata={"workflow_id": "wf_1"},
        )
        self.repository.record_role_invocation(
            invocation_id="inv_metrics",
            workflow_id="wf_1",
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id="wf_1",
            lease_resource_key=lease.resource_key,
            fencing_token=lease.fencing_token,
            role="reviewer",
            mode="architecture",
            executor_profile_id="software_engineering.v2_reviewer",
            family_binding_sha="binding",
            authoring_contract_version=AUTHORING_CONTRACT_VERSION,
            prompt_pack_ref=prompt.to_dict(),
        )
        self.repository.record_role_turn(
            invocation_id="inv_metrics",
            fencing_token=lease.fencing_token,
            turn_index=1,
            llm_request_ref=prompt.to_dict(),
            llm_response_ref=response.to_dict(),
            tool_summary_ref=summary.to_dict(),
            input_tokens=321,
            output_tokens=87,
            cost=0.42,
            latency_ms=120,
            tool_latency_ms=35,
            wall_latency_ms=180,
        )

        metrics = self.repository.read_workflow_projection("wf_1")["metrics"]
        self.assertEqual(metrics["llm_time_ms"], 120)
        self.assertEqual(metrics["tool_time_ms"], 35)
        self.assertEqual(metrics["worker_time_ms"], 180)
        self.assertEqual(metrics["review_time_ms"], 120)
        self.assertEqual(metrics["input_tokens"], 321)
        self.assertEqual(metrics["output_tokens"], 87)
        self.assertEqual(metrics["cost"], 0.42)

    def test_worker_progress_events_advance_durable_round_ledger(self) -> None:
        prompt = self.artifacts.put_json({"prompt": "test"}, artifact_type="RolePromptPackArtifact")
        lease = self.repository.claim_lease("worker:events", "inv_events", ttl_seconds=60)
        self.repository.record_role_invocation(
            invocation_id="inv_events",
            workflow_id="wf_1",
            aggregate_type=AggregateType.WORKFLOW,
            aggregate_id="wf_1",
            lease_resource_key=lease.resource_key,
            fencing_token=lease.fencing_token,
            role="architect",
            mode="author",
            executor_profile_id="software_engineering.v2_architect",
            family_binding_sha="binding",
            authoring_contract_version=AUTHORING_CONTRACT_VERSION,
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
                "SELECT last_completed_turn FROM minion_v2_role_invocations WHERE invocation_id = 'inv_events'"
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
            {
                "schema_version": "1",
                "title": "Do the thing",
                "original": {"objective": "do the thing"},
                "revisions": [],
            },
            artifact_type="TaskLedgerArtifact",
        )
        self.assertTrue(self.repository.artifact_is_durable(ref.sha256))
        self.assertEqual(self.artifacts.read_json(ref)["original"]["objective"], "do the thing")

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
            {
                "schema_version": "1",
                "title": "Empty work",
                "original": {"objective": "nothing"},
                "revisions": [],
            },
            artifact_type="TaskLedgerArtifact",
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
        gates = self.artifacts.put_json([], artifact_type="ArchitectureGateChecksArtifact")
        self.assertNotEqual(constraints.sha256, gates.sha256)
        self.assertEqual(self.artifacts.read_json(constraints), [])
        self.assertEqual(self.artifacts.read_json(gates), [])

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

    def test_recovery_uses_attempt_process_group_when_lease_metadata_is_incomplete(self) -> None:
        prompt_ref = self.artifacts.put_json(
            {"instruction": "recover process"},
            artifact_type="RolePromptPackArtifact",
        )
        self.repository.ensure_role_session(
            session_id="session-process-recovery",
            workflow_id="workflow-process-recovery",
            aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-process-recovery",
            role="implementation",
            mode="produce",
            executor_profile_id="software_engineering.v2_coder",
            family_binding_sha="binding",
        )
        assignment = self.repository.create_role_assignment(
            RoleAssignmentRequest(
                assignment_key="process-recovery",
                session_id="session-process-recovery",
                workflow_id="workflow-process-recovery",
                aggregate_type=AggregateType.DAG_NODE_RUN.value,
                aggregate_id="node-process-recovery",
                role="implementation",
                mode="produce",
                executor_profile_id="software_engineering.v2_coder",
                family_binding_sha="binding",
                input_fingerprint="process-input",
                required_inputs=(),
                input_refs={},
                execution_spec={"effect_type": "run_implementation_role"},
                submission_kind="candidate",
            )
        )
        attempt = self.repository.claim_role_assignment(assignment["assignment_id"])
        resource = f"assignment:{assignment['assignment_id']}"
        lease = self.repository.claim_lease(
            resource,
            attempt["attempt_id"],
            ttl_seconds=1,
            metadata={"workflow_id": "workflow-process-recovery"},
        )
        self.repository.start_role_attempt(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            lease_resource_key=resource,
            fencing_token=lease.fencing_token,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        self.repository.update_role_attempt_process_group(
            assignment_id=assignment["assignment_id"],
            attempt_id_value=attempt["attempt_id"],
            fencing_token=lease.fencing_token,
            process_group_id=777777,
        )
        time.sleep(1.05)

        with patch.object(MinionV2Recovery, "_kill_and_reap", return_value=True) as reap:
            result = MinionV2Recovery(MinionV2WorkflowService(self.runtime_root)).recover()

        reap.assert_any_call(777777)
        self.assertIn(resource, result["recovered_leases"])


if __name__ == "__main__":
    unittest.main()
