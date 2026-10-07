from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from pal.bunshin.harnesses import BunshinHarnessRegistryGeneration, pal_harness_spec
from pal.bunshin import ActionEnvelope, AggregateType, build_default_transition_engine
from pal.bunshin.workflow_capabilities import BunshinV2PublicProvider
from pal.bunshin.contracts import AggregateSnapshot, StaleFencingToken
from pal.bunshin.cycle_protocol import AssignmentKind, CycleSlot, NodeCycleState
from pal.bunshin.graph_protocol import GraphIR, NodeSpec, RoleBinding
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.role_protocol import stable_hash
from pal.bunshin.semantic_orchestration.attempt_assignment_reuse import AssignmentReuse
from pal.bunshin.semantic_orchestration.attempt_harness_binding import HarnessBinding
from pal.bunshin.semantic_orchestration.attempt_models import (
    AttemptReplay, PreparedRolePrompt, PreparedVerifierContext, RoleAttemptRequest,
)
from pal.bunshin.semantic_orchestration.attempt_prompt_construction import PromptConstruction
from pal.bunshin.semantic_orchestration.attempt_reference_binding import ReferenceBinding
from pal.bunshin.semantic_orchestration.attempt_role_session import RoleSession
from pal.bunshin.semantic_orchestration.orchestrator import SemanticOrchestrator
from pal.bunshin.semantic_orchestration.role_inputs import _node_role_session_id
from pal.bunshin.service import BunshinV2WorkflowService
from pal.bunshin.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
from pal.bunshin.verification import VerificationService, VerificationStatus
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.execution.contracts import CapabilityCall
from pal.shared import RuntimeStatus


VERIFIER = RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE)
SNAPSHOT_FIELDS = (
    "pending_verification_ref", "process_group_reaped", "exclusive_workspace_lock",
    "workspace_fingerprint", "workspace_lock_path",
)


class BlockingUnknownRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="pal-unknown-recovery-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service = BunshinV2WorkflowService(self.root)
        self.repository = self.service.repository
        self.worker = SemanticOrchestrator(self.service)
        self.coordinator = WorkflowCoordinator(self.repository)
        self.candidate = self.service.artifacts.put_json(
            {"candidate": "unchanged"}, artifact_type="CandidateArtifact"
        )
        self.pending = self.service.artifacts.put_json(
            {"submission": {"outcome": "UNKNOWN"}}, artifact_type="PendingVerificationArtifact"
        )
        self.report = self.service.artifacts.put_json(
            {"status": "UNKNOWN", "reason": "required LSP unavailable"},
            artifact_type="VerificationArtifact",
        )
        binding = RoleBinding("profile", "worker")
        graph = GraphIR(
            graph_id="wf-unknown", generation=1,
            nodes={"router": NodeSpec(
                name="router", responsibility="route", satellite_data={"test": True},
                producer_binding=binding, checker_binding=binding,
                execution_adapter="software_git.v2", workspace_policy={},
                output_contract=("router-output",), is_sink=True,
            )},
            edges=(), sink="router", source_ref="architect.yaml", source_map_ref="map",
        )
        self.coordinator.install_graph(workflow_id="wf-unknown", graph=graph)
        self.coordinator.start_assignment(
            workflow_id="wf-unknown", node_name="router", slot=CycleSlot.PRODUCER,
            kind=AssignmentKind.INITIAL, input_fingerprint="producer",
        )
        self.coordinator.producer_submitted(
            workflow_id="wf-unknown", node_name="router", product_ref=self.candidate.sha256,
        )
        self.coordinator.start_assignment(
            workflow_id="wf-unknown", node_name="router", slot=CycleSlot.CHECKER,
            kind=AssignmentKind.INITIAL, input_fingerprint="first-review",
        )
        for action, payload in (
            ("CREATE_NODE_RUN", {
                "unit_contract_ref": self.candidate.to_dict(), "epoch_id": "epoch",
                "module_name": "router", "role_session_generation": 3,
                "graph_generation": 1,
                "execution_adapter": "software_git.v2",
                "architecture_review_generation": 17, "candidate_cycle": 2,
                "failure_history": [{"finding_fingerprint": "historical"}],
            }),
            ("DEPENDENCIES_ACCEPTED", {"accepted_producer_dependency_node_ids": [], "epoch_frozen": False}),
            ("START_PRODUCING", {"fencing_token": 1}),
            ("SUBMIT_CANDIDATE", {"fencing_token": 1}),
            ("QUIESCE_COMPLETED", {"fencing_token": 1, "process_group_reaped": True,
                                    "exclusive_workspace_lock": True, "workspace_fingerprint": "candidate-tree"}),
            ("CANDIDATE_SNAPSHOTTED", {"candidate_ref": self.candidate.to_dict(),
                                      "candidate_digest": "candidate-digest", "workspace_fingerprint": "candidate-tree"}),
            ("VERIFICATION_DEPENDENCIES_ACCEPTED", {"accepted_dependency_node_ids": [], "epoch_frozen": False}),
        ):
            self.dispatch(action, payload)
        # Role setup belongs to a live admitted verifier, before its submitted
        # receipt moves the aggregate through quiescing and snapshotting.
        owner = _node_role_session_id(self.node(), VERIFIER)
        lease = self.repository.leases.claim_lease("node:node-router:review", owner, ttl_seconds=120)
        self.dispatch("START_REVIEW", {"active_worker_id": owner, "lease_resource_key": lease.resource_key,
                                       "fencing_token": lease.fencing_token})

    def node(self):
        return self.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, "node-router")

    def dispatch(self, action, payload):
        current = self.node()
        return self.repository.transitions.dispatch(ActionEnvelope(
            action_type=action, workflow_id="wf-unknown", aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id="node-router", actor="test", expected_version=current.version if current else 0,
            idempotency_key=f"test:{action}:{current.version if current else 0}", payload=payload,
        ))

    def effect(self, key="fresh-review"):
        return {"effect_id": key, "effect_key": key, "effect_type": "run_verifier_role",
                "aggregate_type": AggregateType.DAG_NODE_RUN.value, "aggregate_id": "node-router"}

    def block_unknown(self):
        self.finish_review()
        self.coordinator.require_node_triage(workflow_id="wf-unknown", node_name="router")
        return VerificationService(self.repository, self.service.artifacts).submit_verdict(
            node=self.node(), verification_ref=self.report, status=VerificationStatus.UNKNOWN,
            actor="verifier",
        )

    def finish_review(self, receipt_binding=None):
        if self.node().state == "REVIEWING":
            self.dispatch("SUBMIT_SEMANTIC_VERIFICATION", {
                "pending_verification_ref": self.pending.to_dict(), **(receipt_binding or {}),
            })
        if self.node().state == "REVIEW_QUIESCING":
            self.dispatch("VERIFIER_QUIESCED", {
                "fencing_token": self.node().payload["fencing_token"], "process_group_reaped": True,
                "exclusive_workspace_lock": True, "workspace_fingerprint": "review-tree",
                "workspace_lock_path": "/old/review.lock",
            })

    def resolve(self):
        return self.service.resolve_triage(
            workflow_id="wf-unknown", actor="operator", source_channel="test",
            resolution="Fixed the required LSP policy", subject="module:router",
        )

    async def prepare(self, node, key):
        resource = str(node.payload.get("lease_resource_key") or "node:node-router:review")
        lease = self.repository.leases.read_lease(resource)
        self.assertIsNotNone(lease)
        command = RoleAttemptRequest(
            effect=self.effect(key), snapshot=node, invocation_id=_node_role_session_id(node, VERIFIER),
            lease_resource=resource, fencing_token=int(node.payload.get("fencing_token") or lease["fencing_token"]),
            profile="software_engineering.v2_verifier", activation=VERIFIER,
            instruction="Verify this candidate", reference_refs={}, workspace_override=None,
            prepare_workspace=False,
        )
        spec = pal_harness_spec()
        workspace = SimpleNamespace(
            bound_input_entries=[], bound_reference_refs={"candidate_diff": self.candidate},
            contract_authoring=False, workspace={"output_policy": {"primary_artifact": "verification.json"}}, role="verifier", mode="module",
            binding={}, binding_ref={"sha256": "family"}, llm_policy={},
            harness_generation=BunshinHarnessRegistryGeneration("harness", (spec,)),
            preferred_harness=spec,
        )
        references = await ReferenceBinding(
            self.repository, self.service.task_ledger, self.worker.components.workflow_facts,
        ).execute(command, workspace)
        # Family validation is unrelated to evaluation identity; leave all
        # fingerprint, receipt selection, session and draft machinery real.
        with patch("pal.bunshin.semantic_orchestration.attempt_prompt_construction.validate_family_binding_payload",
                   return_value={"verifier": {"role_profile": {"canonical_profile_id": command.profile}}}):
            prompt = await PromptConstruction(self.worker.components.workflow_facts).execute(
                command, references, PreparedVerifierContext({"policy": "unchanged"}), workspace,
            )
        reuse = await AssignmentReuse(
            self.service.artifacts, self.worker.components.assignment_identity,
            self.worker.components.assignment_retries, self.worker.background,
            self.repository, self.worker.components.role_checkpoints,
        ).execute(command, references, PreparedRolePrompt(prompt.pack), workspace)
        return command, workspace, references, prompt, reuse

    async def assignment(self, prepared):
        command, workspace, references, prompt, reuse = prepared
        self.assertNotIsInstance(reuse, AttemptReplay)
        return await RoleSession(self.worker.components.assignment_retries, self.repository).execute(
            command, reuse, prompt, references, workspace,
        )

    def start_attempt(self, prepared, session):
        command, _, _, prompt, _ = prepared
        assignment_id = session.assignment["assignment_id"]
        attempt = self.repository.role_assignments.claim_role_assignment(assignment_id)
        attempt_id = attempt["attempt_id"]
        lease = self.repository.leases.claim_lease("verifier-attempt", attempt_id, ttl_seconds=60)
        prompt_ref = self.service.artifacts.put_json(prompt.pack.to_dict(), artifact_type="RolePromptPackArtifact")
        self.repository.role_attempts.start_role_attempt(
            assignment_id=assignment_id, attempt_id_value=attempt_id,
            lease_resource_key=lease.resource_key, fencing_token=lease.fencing_token,
            prompt_pack_ref=prompt_ref.to_dict(),
        )
        draft = SubmissionDraftContext(
            workflow_id="wf-unknown", invocation_id=attempt_id, lease_resource_key=lease.resource_key,
            fencing_token=lease.fencing_token, role="verifier", mode="module", draft_kind="verification",
            input_fingerprint=prompt.input_fingerprint,
        )
        return attempt_id, lease, draft, prompt_ref

    def test_public_unknown_resolution_starts_fresh_cycle_and_preserves_history(self):
        async def scenario():
            before = self.node()
            old_prepared = await self.prepare(before, "old-review")
            old_session = await self.assignment(old_prepared)
            attempt, lease, old_context, _ = self.start_attempt(old_prepared, old_session)
            drafts = SubmissionDraftStore(self.root)
            evidence = {"cases": {"required-lsp": {"status": "UNKNOWN"}}}
            drafts.mutate(old_context, operation_key="old-evidence", request={},
                          reducer=lambda payload: ({**payload, "evidence": evidence}, {"recorded": True}))
            access_token = self.repository.role_access.issue_role_attempt_access_token(
                assignment_id=old_session.assignment["assignment_id"], attempt_id_value=attempt,
                fencing_token=lease.fencing_token,
            )
            receipt = self.service.artifacts.put_json(
                {"outcome": "UNKNOWN"}, artifact_type="VerifierRoleSubmissionArtifact",
            )
            self.repository.role_submissions.record_role_submission(
                assignment_id=old_session.assignment["assignment_id"], attempt_id_value=attempt,
                fencing_token=lease.fencing_token, artifact_ref=receipt.to_dict(), payload_hash=stable_hash({"outcome": "UNKNOWN"}),
                settlement_action={"action_type": "SUBMIT_SEMANTIC_VERIFICATION"},
            )
            self.repository.role_submissions.settle_role_assignment(
                assignment_id=old_session.assignment["assignment_id"], submission_payload_hash=stable_hash({"outcome": "UNKNOWN"}),
            )
            self.assertIsNone(self.repository.leases.read_lease(lease.resource_key))
            result = self.block_unknown()
            self.assertEqual(result.snapshot.state, "TRIAGE_REQUIRED")
            with self.repository.database.read_connection() as connection:
                effects = [connection.execute(
                    "SELECT effect_type FROM bunshin_v2_outbox WHERE effect_id = ?", (effect_id,),
                ).fetchone()[0] for effect_id in result.outbox_effect_ids]
            self.assertEqual(effects, ["quiesce_role_for_triage"])
            with self.repository.database.read_connection() as connection:
                cleanup_effect = dict(connection.execute(
                    "SELECT * FROM bunshin_v2_outbox WHERE effect_id = ?", (result.outbox_effect_ids[0],),
                ).fetchone())
            cleanup_effect["payload"] = json.loads(cleanup_effect.pop("payload_json"))
            await self.worker.execute_semantic_effect(cleanup_effect)
            self.assertEqual(await self.worker.components.node_control.resume_node(self.effect("no-auto-retry")), {})
            self.assertNotIn("verifier_evaluation_generation", self.node().payload)
            # The old receipt really is reusable until the operator starts a
            # new evaluation, even with another effect key and unchanged policy.
            replay = await self.prepare(self.node(), "equivalent-old-review")
            self.assertIsInstance(replay[-1], AttemptReplay)
            with self.repository.database.read_connection() as connection:
                prior_events = connection.execute("SELECT * FROM bunshin_v2_domain_events").fetchall()
                prior_drafts = connection.execute("SELECT * FROM bunshin_v2_submission_drafts").fetchall()
            provider = BunshinV2PublicProvider(runtime_root=self.root, wake_manager=lambda: None)
            provider.service = self.service
            with patch.object(self.service, "resolve_task_workflow_selector", return_value=("task", "wf-unknown")):
                resolved = provider.resolve_triage(CapabilityCall(
                    name="resolve_bunshin_triage", meta={"actor_id": "operator"},
                    args={"task": "task", "subject": "module:router", "resolution": "Fixed required LSP policy"},
                ))
            self.assertEqual(resolved.status, RuntimeStatus.OK)
            fresh = self.node()
            self.assertEqual(fresh.state, "REVIEW_QUEUED")
            self.assertEqual(fresh.payload["verifier_evaluation_generation"], 1)
            self.assertEqual(self.coordinator.execution(workflow_id="wf-unknown").cycles["router"].state,
                             NodeCycleState.CHECKER_READY)
            for field in (*SNAPSHOT_FIELDS, "unknown_blocking", "blocker", "active_worker_id", "fencing_token", "lease_resource_key"):
                self.assertNotIn(field, fresh.payload)
            for field in ("candidate_ref", "candidate_digest", "candidate_cycle", "role_session_generation",
                          "architecture_review_generation", "failure_history"):
                self.assertEqual(fresh.payload[field], before.payload[field])
            self.assertEqual(fresh.payload["verification_artifact_ref"], self.report.to_dict())
            self.assertEqual(self.service.artifacts.read_json(self.report)["status"], "UNKNOWN")
            self.assertEqual(self.service.artifacts.read_json(self.pending)["submission"]["outcome"], "UNKNOWN")
            admitted = await self.worker.components.node_control.reconcile_node(self.effect("operator-retry"))
            self.assertEqual(admitted["provider_request_id"], old_prepared[0].invocation_id)
            self.assertEqual(self.node().state, "REVIEWING")
            self.assertEqual(self.coordinator.execution(workflow_id="wf-unknown").cycles["router"].state,
                             NodeCycleState.CHECKING)
            new_prepared = await self.prepare(self.node(), "operator-retry")
            self.assertNotIsInstance(new_prepared[-1], AttemptReplay)
            self.assertNotEqual(old_prepared[3].input_fingerprint, new_prepared[3].input_fingerprint)
            new_session = await self.assignment(new_prepared)
            self.assertNotEqual(old_session.assignment["assignment_id"], new_session.assignment["assignment_id"])
            self.assertEqual(old_session.role_session["session_id"], new_session.role_session["session_id"])
            self.assertEqual(new_session.assignment["execution_spec"]["evaluation_generation"], 1)
            _, _, new_context, _ = self.start_attempt(new_prepared, new_session)
            fresh_draft = drafts.read(new_context)
            self.assertEqual(fresh_draft.payload, {})
            self.assertEqual(fresh_draft.source_draft_key, "")
            with self.assertRaises((StaleFencingToken, ValueError)):
                self.repository.role_access.authenticate_role_attempt(access_token)
            with self.assertRaisesRegex(ValueError, "stale fencing token"):
                drafts.mutate(old_context, operation_key="stale-evidence", request={},
                              reducer=lambda payload: (payload, {}))
            with self.repository.database.read_connection() as connection:
                for prior in prior_events:
                    row = connection.execute("SELECT * FROM bunshin_v2_domain_events WHERE event_id = ?", (prior["event_id"],)).fetchone()
                    self.assertEqual(dict(row), dict(prior))
                for prior in prior_drafts:
                    row = connection.execute("SELECT * FROM bunshin_v2_submission_drafts WHERE draft_key = ?", (prior["draft_key"],)).fetchone()
                    self.assertEqual(dict(row), dict(prior))
            self.assertEqual(self.repository.role_assignments.read_role_assignment(old_session.assignment["assignment_id"])["state"], "settled")
        asyncio.run(scenario())

    def test_interrupted_snapshot_resolution_replays_same_pending_submission(self):
        async def settled_pending():
            prepared = await self.prepare(self.node(), "interrupted-review")
            session = await self.assignment(prepared)
            attempt, lease, _, _ = self.start_attempt(prepared, session)
            submission = {"outcome": "UNKNOWN"}
            receipt = self.service.artifacts.put_json(
                submission, artifact_type="VerifierRoleSubmissionArtifact",
            )
            payload_hash = stable_hash(submission)
            self.repository.role_submissions.record_role_submission(
                assignment_id=session.assignment["assignment_id"], attempt_id_value=attempt,
                fencing_token=lease.fencing_token, artifact_ref=receipt.to_dict(),
                payload_hash=payload_hash,
                settlement_action={"action_type": "SUBMIT_SEMANTIC_VERIFICATION"},
            )
            self.repository.role_submissions.settle_role_assignment(
                assignment_id=session.assignment["assignment_id"], submission_payload_hash=payload_hash,
            )
            return self.service.artifacts.put_json({
                "submission": submission, "submission_ref": receipt.to_dict(),
                "candidate_ref": self.candidate.to_dict(), "candidate_digest": "candidate-digest",
                "role_assignment_id": session.assignment["assignment_id"],
                "role_submission_payload_hash": payload_hash,
                "invocation_id": prepared[0].invocation_id,
            }, artifact_type="PendingSemanticVerificationArtifact")

        self.pending = asyncio.run(settled_pending())
        pending = self.service.artifacts.read_json(self.pending)
        receipt_binding = {
            "role_assignment_id": pending["role_assignment_id"],
            "role_submission_payload_hash": pending["role_submission_payload_hash"],
        }
        self.finish_review(receipt_binding)
        original = replace(self.node(), payload={
            **receipt_binding,
            **self.node().payload, "pending_verification_ref": self.pending.to_dict(),
            "graph_generation": 1,
        })
        self.dispatch("ENTER_TRIAGE", {
            "blocker": {"kind": "effect_failed"}, "pending_verification_ref": self.pending.to_dict(),
            "graph_generation": 1, **receipt_binding,
        })
        self.coordinator.require_node_triage(workflow_id="wf-unknown", node_name="router")
        self.resolve()
        resumed = self.node()
        self.assertEqual(resumed.state, "REVIEW_SNAPSHOTTING")
        for field in SNAPSHOT_FIELDS:
            self.assertEqual(resumed.payload[field], original.payload[field])
        self.assertNotIn("verifier_evaluation_generation", resumed.payload)
        with patch.object(self.worker.components.verification_snapshot, "snapshot_semantic_verification", return_value={"replayed": True}) as snapshot:
            self.assertEqual(asyncio.run(self.worker.components.node_control.reconcile_node(self.effect())), {"replayed": True})
            snapshot.assert_called_once()

    def test_special_resolution_requires_exact_terminal_unknown_and_only_affects_nodes(self):
        base = replace(self.node(), state="TRIAGE_REQUIRED", payload={
            **self.node().payload, "triage_resume_state": "REVIEW_SNAPSHOTTING",
            "blocker": {"kind": "blocking_unknown"}, "verification_artifact_ref": self.report.to_dict(),
            "verifier_evaluation_generation": 4,
        })
        engine = build_default_transition_engine()
        for source, blocker, report, expected in (
            ("REVIEW_SNAPSHOTTING", "blocking_unknown", self.report.to_dict(), "REVIEW_QUEUED"),
            ("REVIEW_SNAPSHOTTING", "effect_failed", self.report.to_dict(), "REVIEW_SNAPSHOTTING"),
            ("REVIEW_SNAPSHOTTING", "blocking_unknown", {}, "REVIEW_SNAPSHOTTING"),
            ("REVIEW_QUIESCING", "blocking_unknown", self.report.to_dict(), "REVIEW_QUIESCING"),
        ):
            with self.subTest(source=source, blocker=blocker, report=bool(report)):
                node = replace(base, payload={**base.payload, "triage_resume_state": source,
                                             "blocker": {"kind": blocker}, "verification_artifact_ref": report})
                result = engine.transition(node, ActionEnvelope(
                    action_type="RESOLVE_TRIAGE", workflow_id=node.workflow_id,
                    aggregate_type=node.aggregate_type, aggregate_id=node.aggregate_id, actor="operator",
                    expected_version=node.version,
                )).snapshot
                self.assertEqual(result.state, expected)
                self.assertEqual(result.payload["verifier_evaluation_generation"],
                                 5 if expected == "REVIEW_QUEUED" else 4)
                self.assertEqual(result.payload["architecture_review_generation"], 17)
        architecture = replace(base, aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                               payload={**base.payload, "triage_resume_state": "REVIEW_QUEUED"})
        result = engine.transition(architecture, ActionEnvelope(
            action_type="RESOLVE_TRIAGE", workflow_id=architecture.workflow_id,
            aggregate_type=architecture.aggregate_type, aggregate_id=architecture.aggregate_id, actor="operator",
            expected_version=architecture.version,
        )).snapshot
        self.assertEqual(result.payload["architecture_review_generation"], 17)
        self.assertEqual(result.payload["verifier_evaluation_generation"], 4)

    def test_reference_generation_is_scoped_to_verifier_module(self):
        async def scenario():
            prepared = await self.prepare(replace(self.node(), payload={
                **self.node().payload, "verifier_evaluation_generation": 5,
            }), "scope")
            command, workspace, _, _, _ = prepared
            for activation, expected in (
                (VERIFIER, 5),
                (RoleActivation(OrchestrationRole.REVIEWER, RoleMode.ARCHITECTURE), 17),
                (RoleActivation(OrchestrationRole.REVIEWER, RoleMode.STANDALONE), 0),
                (RoleActivation(OrchestrationRole.IMPLEMENTATION, RoleMode.PRODUCE), 0),
                (RoleActivation(OrchestrationRole.ARCHITECT, RoleMode.AUTHOR), 0),
            ):
                result = await ReferenceBinding(self.repository, self.service.task_ledger, self.worker.components.workflow_facts).execute(
                    replace(command, activation=activation), workspace,
                )
                self.assertEqual(result.evaluation_generation, expected)
        asyncio.run(scenario())

    def test_same_effect_retry_keeps_original_generation_prompt_and_contract(self):
        async def scenario():
            prepared = await self.prepare(self.node(), "same-effect")
            session = await self.assignment(prepared)
            attempt, lease, _, prompt_ref = self.start_attempt(prepared, session)
            self.worker.components.assignment_retries.queue_active_assignment_retry(
                self.repository.role_assignments.read_role_assignment(session.assignment["assignment_id"]),
                error_kind="manager_shutdown", error_text="interrupted",
            )
            # A process restart may recompile current inputs; the durable effect
            # still owns its original assignment and policy, even after drift.
            changed = replace(self.node(), payload={**self.node().payload, "verifier_evaluation_generation": 9})
            retry = await self.prepare(changed, "same-effect")
            self.assertEqual(retry[-1].assignment["assignment_id"], session.assignment["assignment_id"])
            self.assertEqual(retry[-1].assignment["execution_spec"]["evaluation_generation"], 0)
            retry[-1].pack.metadata["bunshin_v2"]["verification_tool_contract"] = {"policy": "recompiled"}
            retry_session = await self.assignment(retry)
            bound = await HarnessBinding(self.service.artifacts, self.repository, self.worker.components.role_checkpoints).execute(
                retry[0], retry[-1], retry_session, retry[1],
            )
            self.assertTrue(bound.durable_prompt_reused)
            self.assertEqual(bound.input_fingerprint, prepared[3].input_fingerprint)
            self.assertEqual(bound.pack.metadata["bunshin_v2"]["verification_tool_contract"], {"policy": "unchanged"})
            self.assertEqual(bound.pack.metadata["bunshin_v2"]["authoring_input_fingerprint"], prepared[3].input_fingerprint)
            self.assertEqual(self.service.artifacts.read_json(prompt_ref)["metadata"]["bunshin_v2"]["verification_tool_contract"], {"policy": "unchanged"})
        asyncio.run(scenario())
