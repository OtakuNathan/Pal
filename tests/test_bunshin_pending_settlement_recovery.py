"""Public triage recovery of an already-submitted verifier settlement.

These regressions exercise real persisted resume effects, graph transitions,
settled role receipts, canonical workspace fingerprints and snapshot locks.
No verifier process or live provider is invoked.
"""
from __future__ import annotations

import asyncio
import copy
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from tests import test_bunshin_verifier_scope_recovery as scope_fixture
from pal.bunshin.workflow_catalog import BunshinWorkflowCatalog
from pal.bunshin.workflow_capabilities import BunshinPublicProvider
from pal.bunshin.contracts import ActionEnvelope, AggregateType, SubmissionInvariantError
from pal.bunshin.cycle_protocol import AssignmentKind, CycleAction, CycleSlot, CycleTransitionError, NodeCycleState
from pal.bunshin.execution_values import workspace_content_fingerprint
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.role_protocol import RoleAssignmentRequest, stable_hash
from pal.bunshin.semantic_orchestration.role_inputs import _node_role_session_id, _verifier_reference_refs
from pal.bunshin.verification import VerificationService, VerificationStatus
from pal.bunshin.review_findings import structured_findings
from pal.execution.contracts import CapabilityCall
from pal.shared import RuntimeStatus


VERIFIER = RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE)


class PendingSettlementRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.fx = scope_fixture.VerifierScopeRecoveryTests()
        self.fx.setUp()
        self.addCleanup(self.fx.doCleanups)
        self.service = self.fx.service
        self.repository = self.fx.repository
        self.artifacts = self.fx.artifacts
        self.worker = self.fx.worker
        self.coordinator = self.fx.coordinator
        self.binding_ref = BunshinWorkflowCatalog(self.fx.root, self.artifacts).publish_family_binding("software_engineering.v2_coder")
        self.binding = dict(self.artifacts.read_json(self.binding_ref))
        self.fx._accept_provider()

    def _node(self):
        return self.fx._node()

    def _graph_cycle(self):
        return self.coordinator.execution(workflow_id=self.fx.workflow_id).cycles["archive_verify"]

    def _dispatch(self, action, payload, **settlement):
        node = self._node()
        self.fx.sequence += 1
        return self.repository.transitions.dispatch(ActionEnvelope(
            action_type=action, workflow_id=self.fx.workflow_id,
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id,
            actor="regression", expected_version=node.version,
            idempotency_key=f"pending-recovery:{self.fx.sequence}:{action}", payload=payload,
        ), **settlement).snapshot

    def _prepare_pending(self, *, quiesced=True, pending_patch=None, settle=True, generation=1, evaluation_generation=0, submission=None, current_receipt_patch=None, edit_corpus=True):
        for action, payload in (
            ("DEPENDENCIES_ACCEPTED", {"accepted_producer_dependency_node_ids": [], "epoch_frozen": False, "graph_generation": generation, "verifier_evaluation_generation": evaluation_generation}),
            ("START_PRODUCING", {"fencing_token": 1}),
            ("SUBMIT_CANDIDATE", {"fencing_token": 1}),
            ("QUIESCE_COMPLETED", {"fencing_token": 1, "process_group_reaped": True, "exclusive_workspace_lock": True, "workspace_fingerprint": "candidate-tree"}),
            ("CANDIDATE_SNAPSHOTTED", {"candidate_ref": self.fx.candidate_ref.to_dict(), "candidate_digest": self.fx.digest, "workspace_fingerprint": "candidate-tree"}),
            ("VERIFICATION_DEPENDENCIES_ACCEPTED", {"accepted_dependency_node_ids": [], "epoch_frozen": False}),
        ):
            self._dispatch(action, payload)
        for slot in (CycleSlot.PRODUCER, CycleSlot.CHECKER):
            self.coordinator.start_assignment(workflow_id=self.fx.workflow_id, node_name="archive_verify", slot=slot, kind=AssignmentKind.INITIAL, input_fingerprint="original-" + slot.value)
            if slot == CycleSlot.PRODUCER:
                self.coordinator.producer_submitted(workflow_id=self.fx.workflow_id, node_name="archive_verify", product_ref=self.fx.candidate_ref.sha256)
        self.session_id = _node_role_session_id(self._node(), VERIFIER)
        self.repository.role_sessions.ensure_role_session(
            session_id=self.session_id, workflow_id=self.fx.workflow_id,
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=self._node().aggregate_id,
            role="verifier", mode="module", role_profile_id="software_engineering.v2_verifier",
            family_binding_sha=self.binding_ref.sha256, scope_kind="module", subject_key="archive_verify",
        )
        self.assignment = self.repository.role_assignments.create_role_assignment(RoleAssignmentRequest(
            assignment_key="original-archive-verifier", session_id=self.session_id,
            workflow_id=self.fx.workflow_id, aggregate_type=AggregateType.DAG_NODE_RUN.value,
            aggregate_id=self._node().aggregate_id, role="verifier", mode="module",
            role_profile_id="software_engineering.v2_verifier", family_binding_sha=self.binding_ref.sha256,
            input_fingerprint="original-verifier-input", required_inputs=(),
            input_refs={"candidate_diff": self.fx.candidate_ref.to_dict()},
            execution_spec={"effect_type": "run_verifier_role", "effect_key": "original-verifier", "evaluation_generation": 0},
            submission_kind="verification",
        ))
        attempt = self.repository.role_assignments.claim_role_assignment(self.assignment["assignment_id"])
        attempt_id = attempt["attempt_id"]
        attempt_lease = self.repository.leases.claim_lease("original-attempt", attempt_id, ttl_seconds=120)
        node_lease = self.repository.leases.claim_lease("original-node-review", self.session_id, ttl_seconds=120)
        self._dispatch("START_REVIEW", {"active_worker_id": self.session_id, "lease_resource_key": node_lease.resource_key, "fencing_token": node_lease.fencing_token})
        corpus = self.fx.repo / "tests/archive_verify/verifier/test_contract.py"
        if edit_corpus:
            corpus.write_text("def test_fifo_is_rejected_before_open():\n    assert True\n", encoding="utf-8")
        prompt_ref = self.artifacts.put_json({"workspace": {"repo_path": str(self.fx.repo)}}, artifact_type="RolePromptPackArtifact")
        self.repository.role_attempts.start_role_attempt(
            assignment_id=self.assignment["assignment_id"], attempt_id_value=attempt_id,
            lease_resource_key=attempt_lease.resource_key, fencing_token=attempt_lease.fencing_token,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        self.submission = copy.deepcopy(submission or self.fx._submission())
        self.submission_ref = self.artifacts.put_json(self.submission, artifact_type="SemanticVerificationSubmissionArtifact")
        self.submission_hash = stable_hash(self.submission)
        self.repository.role_submissions.record_role_submission(
            assignment_id=self.assignment["assignment_id"], attempt_id_value=attempt_id,
            fencing_token=attempt_lease.fencing_token, artifact_ref=self.submission_ref.to_dict(),
            payload_hash=self.submission_hash, settlement_action={"action_type": "SUBMIT_SEMANTIC_VERIFICATION"},
        )
        self.fingerprint = workspace_content_fingerprint(self.fx.repo)
        pending = {
            "schema_version": "1", "submission": self.submission,
            "candidate_ref": self.fx.candidate_ref.to_dict(), "candidate_digest": self.fx.digest,
            "candidate_git_base": self.fx.digest, "implementation_candidate_ref": self.fx.candidate_ref.to_dict(),
            "review_workspace": str(self.fx.repo), "review_scratch": str(self.fx.root / "scratch"),
            "execution_adapter": "software_git.v2", "submitted_workspace_fingerprint": self.fingerprint,
            "invocation_id": self.session_id, "lease_resource_key": node_lease.resource_key,
            "fencing_token": node_lease.fencing_token,
            "role_assignment_id": self.assignment["assignment_id"], "role_submission_payload_hash": self.submission_hash,
            "submission_ref": self.submission_ref.to_dict(),
        }
        pending.update(pending_patch or {})
        self.pending_ref = self.artifacts.put_json(pending, artifact_type="PendingSemanticVerificationArtifact")
        self._dispatch("SUBMIT_SEMANTIC_VERIFICATION", {
            "pending_verification_ref": self.pending_ref.to_dict(),
            "role_assignment_id": self.assignment["assignment_id"], "role_submission_payload_hash": self.submission_hash,
            **(current_receipt_patch or {}),
        }, **({
            "role_assignment_id": self.assignment["assignment_id"], "role_submission_payload_hash": self.submission_hash,
        } if settle else {}))
        if quiesced:
            self._dispatch("VERIFIER_QUIESCED", {"fencing_token": node_lease.fencing_token, "process_group_reaped": True, "exclusive_workspace_lock": True, "workspace_fingerprint": self.fingerprint})
        return self._node()

    def _enter_triage(self, *, blocker=None, extra_payload=None):
        node = self._node()
        failure = self.artifacts.put_json({"error": "deterministic snapshot route failure"}, artifact_type="EffectFailureArtifact")
        with self.repository.transaction() as connection:
            connection.transitions.dispatch(ActionEnvelope(
                action_type="ENTER_TRIAGE", workflow_id=node.workflow_id, aggregate_type=node.aggregate_type,
                aggregate_id=node.aggregate_id, actor="manager", expected_version=node.version,
                idempotency_key=f"public-pending-triage:{node.version}",
                payload={"failure_artifact_ref": failure.to_dict(), "blocker": blocker or {"kind": "effect_failed", "effect_type": "snapshot_verifier_result"}, **(extra_payload or {})},
            ))
            self.coordinator.require_node_triage(workflow_id=node.workflow_id, node_name="archive_verify", unit_of_work=connection)
        return self._node()

    def _resolve(self):
        return self.service.resolve_triage(workflow_id=self.fx.workflow_id, actor="operator", source_channel="test", subject="module:archive_verify", resolution="Reevaluate the preserved submission and keep the FIFO finding.")

    def _stored(self, effect_type):
        return self.fx._stored_effect(self._node(), effect_type=effect_type)

    def _role_rows(self):
        with self.repository.database.read_connection() as connection:
            return {table: [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()] for table in (
                "bunshin_v2_role_assignments", "bunshin_v2_role_sessions", "bunshin_v2_role_attempts",
            )}

    def _counts(self):
        with self.repository.database.read_connection() as connection:
            return tuple(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in ("bunshin_v2_domain_events", "bunshin_v2_outbox"))

    def test_public_resolve_restores_pending_checker_then_real_resume_effect_corrects(self):
        original = self._prepare_pending()
        original_snapshot_effect = self._stored("snapshot_verifier_result")
        provider_before = self.fx._node("manifest_model")
        self.assertEqual(self.repository.role_assignments.read_role_assignment(self.assignment["assignment_id"])["state"], "settled")
        self._enter_triage()
        self.assertEqual(self._graph_cycle().state, NodeCycleState.TRIAGE_REQUIRED)
        self.assertEqual(self._graph_cycle().resume_state, NodeCycleState.CHECKER_READY)
        self.assertIsNone(self._graph_cycle().active_assignment)
        roles_before = self._role_rows()
        provider = BunshinPublicProvider(runtime_root=self.fx.root, wake_manager=lambda: None)
        provider.service = self.service
        with patch.object(self.service, "resolve_task_workflow_selector", return_value=("task", self.fx.workflow_id)):
            result = provider.resolve_triage(CapabilityCall(name="resolve_bunshin_triage", meta={"actor_id": "operator"}, args={"task": "task", "subject": "module:archive_verify", "resolution": "Reevaluate the preserved FIFO and stub findings."}))
        self.assertEqual(result.status, RuntimeStatus.OK, result)
        resumed = self._node()
        self.assertEqual(resumed.state, "REVIEW_SNAPSHOTTING")
        self.assertEqual(resumed.payload["pending_verification_ref"], self.pending_ref.to_dict())
        self.assertEqual(resumed.payload["candidate_digest"], original.payload["candidate_digest"])
        self.assertEqual(self._graph_cycle().state, NodeCycleState.CHECKING)
        self.assertEqual(self._graph_cycle().active_assignment.kind, AssignmentKind.RESUME)
        self.assertEqual(self._graph_cycle().active_assignment.input_fingerprint, "pending-checker-settlement:" + self.pending_ref.sha256)
        self.assertEqual(self._role_rows(), roles_before)
        resume_effect = self._stored("reconcile_semantic_state")
        corrected = asyncio.run(self.worker.execute_semantic_effect(resume_effect))
        after = self._node()
        self.assertEqual(after.state, "REVIEW_QUEUED")
        self.assertEqual(self._graph_cycle().state, NodeCycleState.CHECKER_READY)
        self.assertFalse(self._graph_cycle().last_verdict.accepted)
        self.assertEqual(self._role_rows(), roles_before)
        self.assertEqual(self.fx._node("manifest_model"), provider_before)
        packet = self.artifacts.read_json(after.payload["repair_bill_ref"])
        self.assertEqual(packet["route"], "verification_correction")
        self.assertEqual(packet["findings"], structured_findings(self.submission))
        self.assertEqual(packet["source_pending_verification_ref"], self.pending_ref.to_dict())
        self.assertEqual(self.artifacts.read_json(packet["tool_receipts_ref"])["receipts"], self.submission["tool_receipts"])
        self.assertEqual(self.artifacts.read_json(self.submission_ref), self.submission)
        checkpoint = self.artifacts.read_json(after.payload["candidate_ref"])
        self.assertEqual(checkpoint["verifier_test_paths"], ["tests/archive_verify/verifier/test_contract.py"])
        replay = self.worker.components.verification_snapshot.snapshot_semantic_verification(original_snapshot_effect)
        self.assertEqual(replay, corrected)
        repeated_effect = asyncio.run(self.worker.execute_semantic_effect(resume_effect))
        self.assertEqual(repeated_effect["status"], "superseded")
        self.assertEqual(self._node(), after)
        self.assertEqual(self._role_rows(), roles_before)
        admitted = asyncio.run(self.worker.execute_semantic_effect(self._stored("admit_verifier_role")))
        self.assertEqual(admitted["provider_request_id"], self.session_id)
        self.assertEqual(self._node().state, "REVIEWING")
        self.assertEqual(self._graph_cycle().state, NodeCycleState.CHECKING)
        fresh = asyncio.run(self._new_verifier_assignment())
        self.assertNotEqual(fresh["assignment_id"], self.assignment["assignment_id"])
        self.assertEqual(fresh["session_id"], self.session_id)
        self.assertEqual(fresh["state"], "queued")
        self.assertIn("repair_bill", fresh["input_refs"])
        self.assertEqual(self.repository.role_assignments.read_role_assignment(self.assignment["assignment_id"])["state"], "settled")

    async def _new_verifier_assignment(self):
        from pal.bunshin.harnesses import BunshinHarnessRegistryGeneration, pal_harness_spec
        from pal.bunshin.semantic_orchestration.attempt_assignment_reuse import AssignmentReuse
        from pal.bunshin.semantic_orchestration.attempt_models import AttemptReplay, PreparedRolePrompt, RoleAttemptRequest
        from pal.bunshin.semantic_orchestration.attempt_prompt_construction import PromptConstruction
        from pal.bunshin.semantic_orchestration.attempt_reference_binding import ReferenceBinding
        from pal.bunshin.semantic_orchestration.attempt_role_session import RoleSession
        from pal.bunshin.semantic_orchestration.attempt_verifier_context import VerifierContext
        from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts

        node = self._node()
        refs = _verifier_reference_refs(
            artifacts=self.artifacts, node_payload=node.payload,
            module_work_view_ref=self.artifacts.put_json({"module_name": "archive_verify"}, artifact_type="ModuleWorkViewArtifact"),
            candidate_diff_ref=self.fx.candidate_ref,
        )
        spec = pal_harness_spec()
        workspace = SimpleNamespace(
            binding=self.binding, binding_ref=self.binding_ref.to_dict(), bound_input_entries=[], bound_reference_refs=refs,
            contract_authoring=False, family_policies=dict(self.binding.get("policies") or {}), llm_policy={}, mode="module", role="verifier",
            workspace={"runtime_root": str(self.fx.root), "repo_path": str(self.fx.repo)},
            harness_generation=BunshinHarnessRegistryGeneration("test-harness", (spec,)), preferred_harness=spec,
        )
        command = RoleAttemptRequest(
            effect=self._stored("run_verifier_role"), snapshot=node, invocation_id=self.session_id,
            lease_resource=str(node.payload["lease_resource_key"]), fencing_token=int(node.payload["fencing_token"]),
            profile="software_engineering.v2_verifier", activation=VERIFIER,
            instruction="Reevaluate every preserved finding.", reference_refs=refs, workspace_override=None, prepare_workspace=False,
        )
        facts = WorkflowFacts(self.artifacts, self.repository)
        verifier = await VerifierContext(self.artifacts).execute(command, workspace)
        references = await ReferenceBinding(self.repository, self.service.task_ledger, facts).execute(command, workspace)
        prompt = await PromptConstruction(facts).execute(command, references, verifier, workspace)
        reuse = await AssignmentReuse(self.artifacts, self.worker.components.assignment_identity, self.worker.components.assignment_retries, self.worker.background, self.repository, self.worker.components.role_checkpoints).execute(command, references, PreparedRolePrompt(prompt.pack), workspace)
        self.assertNotIsInstance(reuse, AttemptReplay)
        result = await RoleSession(self.worker.components.assignment_retries, self.repository).execute(command, reuse, prompt, references, workspace)
        return result.assignment

    def test_public_resolve_of_pending_quiesce_uses_original_submission_without_worker(self):
        self._prepare_pending(quiesced=False)
        self._enter_triage()
        roles_before = self._role_rows()
        self.assertEqual(self._resolve()["state"], "REVIEW_QUIESCING")
        self.assertEqual(self._graph_cycle().state, NodeCycleState.CHECKING)
        asyncio.run(self.worker.execute_semantic_effect(self._stored("reconcile_semantic_state")))
        self.assertEqual(self._node().state, "REVIEW_SNAPSHOTTING")
        result = asyncio.run(self.worker.execute_semantic_effect(self._stored("snapshot_verifier_result")))
        self.assertTrue(result["result_artifact_ref"])
        self.assertEqual(self._node().state, "REVIEW_QUEUED")
        self.assertEqual(self._graph_cycle().state, NodeCycleState.CHECKER_READY)
        self.assertEqual(self._role_rows(), roles_before)

    def _assert_restore_rolls_back(self, *, triage_payload=None, expected_error=".", **prepare):
        self._prepare_pending(**prepare)
        node = self._enter_triage(extra_payload=triage_payload)
        graph = self.coordinator.execution(workflow_id=self.fx.workflow_id)
        rows = self._role_rows()
        counts = self._counts()
        with self.assertRaisesRegex((SubmissionInvariantError, ValueError), expected_error):
            self._resolve()
        self.assertEqual(self._node(), node)
        self.assertEqual(self.coordinator.execution(workflow_id=self.fx.workflow_id), graph)
        self.assertEqual(self._role_rows(), rows)
        self.assertEqual(self._counts(), counts)

    def test_stale_candidate_binding_rolls_back_public_resolution(self):
        self._assert_restore_rolls_back(pending_patch={"candidate_digest": "different-candidate"})

    def test_mismatched_submission_receipt_rolls_back_public_resolution(self):
        self._assert_restore_rolls_back(pending_patch={"role_submission_payload_hash": "different-receipt"})

    def test_unsettled_role_submission_cannot_restore_logical_checker(self):
        self._assert_restore_rolls_back(settle=False)

    def test_wrong_graph_generation_rolls_back_public_resolution(self):
        self._assert_restore_rolls_back(generation=2)

    def test_pending_semantics_must_equal_the_original_role_receipt(self):
        self._assert_restore_rolls_back(pending_patch={"submission": {"outcome": "pass", "findings": []}})

    def test_terminal_unknown_resolution_stays_checker_ready(self):
        self._prepare_pending()
        report = self.artifacts.put_json({"status": "UNKNOWN"}, artifact_type="VerificationArtifact")
        self.coordinator.require_node_triage(workflow_id=self.fx.workflow_id, node_name="archive_verify")
        VerificationService(self.repository, self.artifacts).submit_verdict(node=self._node(), verification_ref=report, status=VerificationStatus.UNKNOWN, actor="verifier")
        roles = self._role_rows()
        self.assertEqual(self._resolve()["state"], "REVIEW_QUEUED")
        self.assertEqual(self._graph_cycle().state, NodeCycleState.CHECKER_READY)
        self.assertIsNone(self._graph_cycle().active_assignment)
        self.assertNotIn("pending_verification_ref", self._node().payload)
        self.assertEqual(self._role_rows(), roles)

    def test_terminal_correction_exhaustion_resolution_stays_checker_ready(self):
        self._prepare_pending()
        report = self.artifacts.put_json({"status": "FAIL"}, artifact_type="VerificationArtifact")
        self._dispatch("ENTER_TRIAGE", {"verification_artifact_ref": report.to_dict(), "blocker": {"kind": "invalid_verifier_submission", "attempt_count": 3}})
        self.coordinator.require_node_triage(workflow_id=self.fx.workflow_id, node_name="archive_verify")
        self.assertEqual(self._resolve()["state"], "REVIEW_QUEUED")
        self.assertEqual(self._graph_cycle().state, NodeCycleState.CHECKER_READY)
        self.assertIsNone(self._graph_cycle().active_assignment)
        self.assertNotIn("pending_verification_ref", self._node().payload)

    def test_ordinary_checker_retry_from_ready_remains_invalid(self):
        cycle = self._graph_cycle()
        # The targeted restore must not relax the underlying checker protocol.
        ready = replace(cycle, state=NodeCycleState.CHECKER_READY, active_assignment=None)
        with self.assertRaisesRegex(CycleTransitionError, "checker_retry from CHECKER_READY"):
            ready.transition(CycleAction.CHECKER_RETRY)

    def test_observed_public_resume_settles_pending_before_a_new_checker_is_admitted(self):
        self._prepare_pending()
        self._enter_triage()
        roles = self._role_rows()
        with self.repository.database.read_connection() as connection:
            leases = [dict(row) for row in connection.execute("SELECT * FROM bunshin_v2_leases ORDER BY resource_key").fetchall()]
        self.assertEqual(self._resolve()["state"], "REVIEW_SNAPSHOTTING")
        self.assertEqual(self._role_rows(), roles)
        with self.repository.database.read_connection() as connection:
            self.assertEqual([dict(row) for row in connection.execute("SELECT * FROM bunshin_v2_leases ORDER BY resource_key").fetchall()], leases)
        # Intentionally exercise the persisted resume dispatch before asserting
        # graph state: pre-fix this reproduces the exact live CHECKER_RETRY
        # from CHECKER_READY exception rather than an earlier helper assertion.
        result = asyncio.run(self.worker.execute_semantic_effect(self._stored("reconcile_semantic_state")))
        self.assertTrue(result["result_artifact_ref"])
        self.assertEqual(self._node().state, "REVIEW_QUEUED")
        self.assertEqual(self._graph_cycle().state, NodeCycleState.CHECKER_READY)
        self.assertFalse(self._graph_cycle().last_verdict.accepted)
        self.assertEqual(self._role_rows(), roles)
        with self.repository.database.read_connection() as connection:
            self.assertTrue({row["resource_key"] for row in connection.execute("SELECT * FROM bunshin_v2_leases").fetchall()} <= {row["resource_key"] for row in leases})
        self.assertEqual(self.repository.role_assignments.read_role_assignment(self.assignment["assignment_id"])["state"], "settled")

    def test_stale_verifier_evaluation_rolls_back_public_resolution(self):
        self._assert_restore_rolls_back(evaluation_generation=1)

    def test_claimed_outbox_reconciliation_commits_one_correction_receipt(self):
        from pal.bunshin.orchestration import BunshinOutboxProcessor
        import json

        self._prepare_pending()
        self._enter_triage()
        self._resolve()
        persisted = self._stored("reconcile_semantic_state")
        roles = self._role_rows()
        processor = BunshinOutboxProcessor(self.service, semantic_effects=self.worker, worker_id="pending-recovery-outbox")
        claimed = self.repository.outbox_claims.claim_outbox(processor.worker_id, limit=1000, lease_seconds=120)
        effect = next(item for item in claimed if item["effect_id"] == persisted["effect_id"])
        self.assertEqual(effect["status"], "inflight")
        self.assertEqual(asyncio.run(processor._process_effect(effect)), "completed")
        corrected = self._node()
        self.assertEqual(corrected.state, "REVIEW_QUEUED")
        self.assertEqual(self._graph_cycle().state, NodeCycleState.CHECKER_READY)
        self.assertEqual(self._role_rows(), roles)
        with self.repository.database.read_connection() as connection:
            outbox = dict(connection.execute("SELECT * FROM bunshin_v2_outbox WHERE effect_id = ?", (effect["effect_id"],)).fetchone())
            receipts = [dict(row) for row in connection.execute("SELECT * FROM bunshin_v2_effect_receipts WHERE effect_key = ?", (effect["effect_key"],)).fetchall()]
        self.assertEqual(outbox["status"], "completed")
        self.assertEqual(outbox["attempt_count"], 1)
        self.assertEqual(len(receipts), 1)
        self.assertEqual(json.loads(receipts[0]["result_artifact_ref_json"]), corrected.payload["verification_artifact_ref"])
        attempts = self.repository.outbox_claims.list_effect_attempts(effect["effect_id"])
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]["status"], "completed")
        self.assertEqual(asyncio.run(processor._process_effect(effect)), "completed")
        self.assertEqual(self._node(), corrected)
        with self.repository.database.read_connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM bunshin_v2_effect_receipts WHERE effect_key = ?", (effect["effect_key"],)).fetchone()[0], 1)

    def test_missing_settled_assignment_binding_rolls_back_public_resolution(self):
        self._assert_restore_rolls_back(pending_patch={"role_assignment_id": ""})

    def test_wrong_current_assignment_rolls_back_public_resolution(self):
        self._assert_restore_rolls_back(current_receipt_patch={"role_assignment_id": "different-current-assignment"})

    def test_wrong_current_receipt_hash_rolls_back_public_resolution(self):
        self._assert_restore_rolls_back(current_receipt_patch={"role_submission_payload_hash": "different-current-receipt"})

    def _settle_no_progress(self):
        import hashlib
        import json

        self._prepare_pending(submission=self.fx._submission([self.fx.fifo]), edit_corpus=False)
        # Seed the two prior identical observations, then let the real snapshot
        # and verdict path record the third observation as terminal no_progress.
        self.fx._git("add", "-A")
        tree = self.fx._git("write-tree").strip()
        fingerprint = hashlib.sha256(json.dumps({
            "outcome": self.submission["outcome"],
            "findings": structured_findings(self.submission),
            "changed_test_paths": [],
            "receipt_hashes": [str(item.get("output_sha256") or "") for item in self.submission["tool_receipts"]],
            "candidate_tree": tree,
        }, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        self._enter_triage(extra_payload={"failure_history": [
            {"finding_fingerprint": fingerprint, "candidate_tree_hash": tree},
            {"finding_fingerprint": fingerprint, "candidate_tree_hash": tree},
        ]})
        self._resolve()
        result = asyncio.run(self.worker.execute_semantic_effect(self._stored("reconcile_semantic_state")))
        terminal = self._node()
        self.assertEqual(terminal.state, "TRIAGE_REQUIRED")
        self.assertEqual(terminal.payload["blocker"], {"kind": "no_progress", "rounds": 3})
        self.assertEqual(self._graph_cycle().state, NodeCycleState.TRIAGE_REQUIRED)
        self.assertEqual(self.repository.queries.read_verification_settlement_ref(terminal.aggregate_id, self.pending_ref.sha256), result["result_artifact_ref"])
        self.assertEqual(terminal.payload["candidate_ref"], self.fx.candidate_ref.to_dict())
        self.assertEqual(terminal.payload["candidate_digest"], self.fx.digest)
        return terminal

    def _assert_committed_verdict_resolution_is_unchanged(self):
        terminal = self._node()
        graph = self.coordinator.execution(workflow_id=self.fx.workflow_id)
        roles = self._role_rows()
        counts = self._counts()
        with self.repository.database.read_connection() as connection:
            leases = [dict(row) for row in connection.execute("SELECT * FROM bunshin_v2_leases ORDER BY resource_key").fetchall()]
        with self.assertRaisesRegex(SubmissionInvariantError, "already has a committed verdict"):
            self._resolve()
        self.assertEqual(self._node(), terminal)
        self.assertEqual(self.coordinator.execution(workflow_id=self.fx.workflow_id), graph)
        self.assertEqual(self._role_rows(), roles)
        self.assertEqual(self._counts(), counts)
        with self.repository.database.read_connection() as connection:
            self.assertEqual([dict(row) for row in connection.execute("SELECT * FROM bunshin_v2_leases ORDER BY resource_key").fetchall()], leases)

    def test_absent_pending_submission_rolls_back_public_resolution(self):
        self._assert_restore_rolls_back(
            triage_payload={"pending_verification_ref": {}},
            expected_error="no durable pending submission",
        )

    def test_committed_no_progress_verdict_cannot_restore_pending_checker(self):
        self._settle_no_progress()
        self._assert_committed_verdict_resolution_is_unchanged()

    def test_indexed_committed_verdict_rejects_generic_effect_failed_recovery(self):
        self._settle_no_progress()
        self._dispatch("ENTER_TRIAGE", {"blocker": {"kind": "effect_failed"}})
        self.assertTrue(self.repository.queries.read_verification_settlement_ref(self._node().aggregate_id, self.pending_ref.sha256))
        self._assert_committed_verdict_resolution_is_unchanged()

    def test_legacy_no_progress_verdict_without_pending_index_stays_triaged(self):
        self._prepare_pending(submission=self.fx._submission([self.fx.fifo]), edit_corpus=False)
        report = self.artifacts.put_json({"status": "FAIL", "findings": structured_findings(self.submission)}, artifact_type="VerificationArtifact")
        # Persist the terminal action shape written before source-pending
        # receipt indexing existed. No newer event/index is synthesized.
        self._dispatch("ENTER_TRIAGE", {
            "verification_artifact_ref": report.to_dict(),
            "blocker": {"kind": "no_progress", "rounds": 3},
        })
        self.coordinator.require_node_triage(workflow_id=self.fx.workflow_id, node_name="archive_verify")
        self.assertEqual(self.repository.queries.read_verification_settlement_ref(self._node().aggregate_id, self.pending_ref.sha256), {})
        self._assert_committed_verdict_resolution_is_unchanged()
