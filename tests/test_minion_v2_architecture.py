from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.minion.v2 import (
    ActionEnvelope,
    AggregateType,
    ArtifactRef,
    ContentAddressedArtifactStore,
    MinionV2Repository,
)
from pal.minion.v2.architecture import (
    ArchitectureArtifactService,
    ComplexityBudgetPolicy,
    HumanReviewCard,
    review_architecture_contract,
    validate_unit_contract,
)
from pal.minion.v2.contracts import UnknownTransitionError
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.orchestration import MinionV2OutboxProcessor
from pal.minion.v2.task_ledger import TaskLedgerService
from pal.minion.v2.semantic_orchestration import apply_v2_research_capability_policy
from pal.shared import MinionInvocationPack


def _complexity_budget(**overrides: int) -> dict[str, int]:
    result = {
        "target_file_count": 4,
        "estimated_context_tokens": 12000,
        "public_interface_count": 4,
        "cross_unit_contract_count": 1,
        "stateful_resource_count": 0,
        "expected_candidate_cycles": 2,
        "platform_dependency_level": 1,
    }
    result.update(overrides)
    return result


def _unit_contract() -> dict:
    return {
        "unit_id": "foundation",
        "unit_behavior_kind": "stateless",
        "responsibility": "Define the stable value types used by consumers.",
        "owned_area": ["src/foundation/**"],
        "reference_only_paths": ["references/**"],
        "provided_interfaces": [{"name": "Geometry", "lifetime": "value"}],
        "consumed_interfaces": [],
        "ownership": {"rule": "Each value is owned by its caller."},
        "lifecycle": "N/A: pure value definitions",
        "state_model": "stateless",
        "invariants": ["value layout is stable"],
        "error_behavior": ["Invalid values fail deterministic validation."],
        "compatibility": ["C ABI"],
        "dependency_constraints": [],
        "requirement_ids": ["R-1"],
        "verification_obligations": [{"kind": "consumer_compile"}],
        "complexity_budget": _complexity_budget(),
        "split_conditions": [],
    }


class MinionV2ArchitectureContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.runtime_root = Path(tempfile.mkdtemp(prefix="pal_minion_v2_arch_"))
        self.repository = MinionV2Repository(self.runtime_root)
        self.store = ContentAddressedArtifactStore(self.runtime_root, self.repository)
        self.service = ArchitectureArtifactService(self.store, self.repository)

    def tearDown(self) -> None:
        shutil.rmtree(self.runtime_root, ignore_errors=True)

    def _publish_contract(self):
        requirements = TaskLedgerService(self.runtime_root, self.store).publish(
            title="Geometry foundation",
            task_spec={"objective": "Expose stable geometry value types."},
            actor="test",
            source_channel="test",
        )
        module = self.service.publish_unit_contract(_unit_contract())
        constraints = self.service.publish_fragment([], artifact_type="GlobalConstraintsArtifact")
        decisions = self.service.publish_fragment([], artifact_type="DesignDecisionsArtifact")
        gates = self.service.publish_fragment([], artifact_type="ArchitectureGateChecksArtifact")
        cross = self.service.publish_fragment(
            {"contract_id": "X-1", "provider": "foundation", "consumer": "integration"},
            artifact_type="CrossUnitContractArtifact",
        )
        topology = self.service.publish_fragment(
            {"depends_on": {"foundation": []}},
            artifact_type="TopologyArtifact",
        )
        integration = self.service.publish_fragment(
            {"node_kind": "integration", "depends_on": ["foundation"]},
            artifact_type="IntegrationContractArtifact",
        )
        assumptions = self.service.publish_fragment(
            {"assumptions": []},
            artifact_type="AssumptionLedgerArtifact",
        )
        risks = self.service.publish_fragment({"risks": []}, artifact_type="RiskLedgerArtifact")
        manifest = self.service.publish_manifest(
            {
                "requirements_ref": requirements.to_dict(),
                "global_constraints_ref": constraints.to_dict(),
                "design_decisions_ref": decisions.to_dict(),
                "gate_checks_ref": gates.to_dict(),
                "unit_contract_refs": [module.to_dict()],
                "cross_unit_contract_refs": [cross.to_dict()],
                "topology_ref": topology.to_dict(),
                "integration_contract_ref": integration.to_dict(),
                "assumption_ledger_ref": assumptions.to_dict(),
                "risk_ledger_ref": risks.to_dict(),
            }
        )
        return requirements, requirements, manifest

    def test_manager_does_not_grade_lifecycle_semantics(self) -> None:
        validated = validate_unit_contract(_unit_contract(), complexity_policy=ComplexityBudgetPolicy())
        self.assertEqual(validated["state_model"], "stateless")
        stateful = {**_unit_contract(), "unit_behavior_kind": "resource_owner", "state_model": "", "lifecycle": ""}
        validated_stateful = validate_unit_contract(stateful, complexity_policy=ComplexityBudgetPolicy())
        self.assertEqual(validated_stateful["state_model"], "")

    def test_unit_contract_rejects_implementation_checklists_but_not_semantic_budget_claims(self) -> None:
        with self.assertRaisesRegex(ValueError, "implementation-level"):
            validate_unit_contract(
                {**_unit_contract(), "milestones": [{"title": "write it"}]},
                complexity_policy=ComplexityBudgetPolicy(),
            )
        too_large = {
            **_unit_contract(),
            "complexity_budget": _complexity_budget(target_file_count=40),
        }
        validated = validate_unit_contract(too_large, complexity_policy=ComplexityBudgetPolicy())
        self.assertNotIn("complexity_budget", validated)

    def test_manifest_review_checks_evidence_coverage_and_topology(self) -> None:
        _requirements, _evidence, manifest = self._publish_contract()
        review = self.service.review_manifest(manifest)
        self.assertEqual(review.verdict, "PASS")
        markdown = self.service.compile_human_review_markdown(manifest)
        self.assertIn("## Module Topology", markdown)
        self.assertIn("### foundation", markdown)
        self.assertNotIn("milestone", markdown.lower())

    def test_mechanical_review_checks_topology_not_contract_semantics(self) -> None:
        producer = _unit_contract()
        producer["unit_id"] = "producer"
        producer["provided_interfaces"] = [{"name": "Published value"}]
        producer["complexity_budget"] = _complexity_budget(public_interface_count=99)
        producer["split_conditions"] = ["Split if it becomes too large."]
        consumer = _unit_contract()
        consumer["unit_id"] = "consumer"
        consumer["consumed_interfaces"] = [{"name": "Published value", "ownership": "producer"}]
        review = review_architecture_contract(
            {},
            {
                "requirements": {"requirements": [{"requirement_id": "R-1", "statement": "One", "strength": "hard"}]},
                "unit_contract": [producer, consumer],
                "cross_unit_contract": [],
                "topology": {"depends_on": {"producer": [], "consumer": ["producer"]}},
            },
            complexity_policy=ComplexityBudgetPolicy(),
        )

        self.assertEqual(review.verdict, "PASS")
        self.assertEqual(review.findings, ())

    def test_human_decision_token_is_bound_and_single_use(self) -> None:
        requirements, evidence, manifest = self._publish_contract()
        workflow_id = "wf_human"
        revision_id = "arch_human"
        self._drive_revision_to_human_review(
            workflow_id=workflow_id,
            revision_id=revision_id,
            requirements_ref=requirements.to_dict(),
            evidence_ref=evidence.to_dict(),
            manifest_ref=manifest.to_dict(),
        )
        card = self.service.create_human_review_card(
            workflow_id=workflow_id,
            architecture_revision_id=revision_id,
            manifest_ref=manifest,
            actor_id="nathan",
            active_channel_id="telegram:chat-1",
        )
        result = self.service.submit_human_decision(card, decision="accept")
        self.assertEqual(result.snapshot.state, "ACCEPTED")
        with self.assertRaisesRegex(ValueError, "stale or already consumed"):
            self.service.submit_human_decision(card, decision="accept")

    def test_human_card_cannot_be_replayed_by_another_channel(self) -> None:
        requirements, evidence, manifest = self._publish_contract()
        workflow_id = "wf_stale"
        revision_id = "arch_stale"
        self._drive_revision_to_human_review(
            workflow_id=workflow_id,
            revision_id=revision_id,
            requirements_ref=requirements.to_dict(),
            evidence_ref=evidence.to_dict(),
            manifest_ref=manifest.to_dict(),
        )
        card = self.service.create_human_review_card(
            workflow_id=workflow_id,
            architecture_revision_id=revision_id,
            manifest_ref=manifest,
            actor_id="nathan",
            active_channel_id="telegram:chat-1",
        )
        wrong_channel = HumanReviewCard(**{**card.__dict__, "active_channel_id": "socket:other"})
        with self.assertRaisesRegex(ValueError, "active_channel_id"):
            self.service.submit_human_decision(wrong_channel, decision="reject")
        self.assertEqual(self.repository.inspect_human_decision_token(card.decision_token)["status"], "issued")

    def test_human_architecture_edit_reuses_immutable_requirements(self) -> None:
        requirements, _evidence, manifest = self._publish_contract()
        workflow_id = "wf_architecture_edit"
        revision_id = "arch_architecture_edit"
        card = self._prepare_workflow_human_review(
            workflow_id=workflow_id,
            revision_id=revision_id,
            requirements_ref=requirements.to_dict(),
            manifest_ref=manifest.to_dict(),
        )
        service = MinionV2WorkflowService(self.runtime_root)

        result = service.submit_human_decision(
            {
                "workflow_id": workflow_id,
                "decision_token": card.decision_token,
                "decision": "edit",
                "edit_scope": "architecture",
                "edit_instruction": "Split ownership from runtime wiring.",
                "actor": "nathan",
                "source_channel": "socket:test",
            }
        )

        self.assertEqual(result["edit_scope"], "architecture")
        superseded = self.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision_id)
        assert superseded is not None
        self.assertNotIn("task_revision_authority_ref", superseded.payload)
        MinionV2OutboxProcessor(service)._create_revision(
            {
                "effect_key": "architecture-edit-child",
                "aggregate_type": AggregateType.ARCHITECTURE_REVISION.value,
                "aggregate_id": revision_id,
            }
        )
        child = next(
            item
            for item in self.repository.list_workflow_snapshots(workflow_id)
            if item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
            and item.payload.get("parent_revision_id") == revision_id
        )
        self.assertEqual(child.payload["requirements_ref"], requirements.to_dict())

    def test_human_requirements_edit_queues_authority_for_architect_revision(self) -> None:
        requirements, _evidence, manifest = self._publish_contract()
        workflow_id = "wf_requirements_edit"
        revision_id = "arch_requirements_edit"
        card = self._prepare_workflow_human_review(
            workflow_id=workflow_id,
            revision_id=revision_id,
            requirements_ref=requirements.to_dict(),
            manifest_ref=manifest.to_dict(),
        )
        service = MinionV2WorkflowService(self.runtime_root)

        result = service.submit_human_decision(
            {
                "workflow_id": workflow_id,
                "decision_token": card.decision_token,
                "decision": "edit",
                "edit_scope": "requirements",
                "amendment": (
                    "Expose stable geometry values and deterministic validation.\n"
                    "Preserve the existing public C ABI.\n"
                ),
                "actor": "nathan",
                "source_channel": "socket:test",
            }
        )

        authority_ref = result["task_revision_authority_ref"]
        authority = self.store.read_json(authority_ref)
        self.assertEqual(
            authority["answer"],
            "Expose stable geometry values and deterministic validation.\n"
            "Preserve the existing public C ABI.\n",
        )
        self.assertEqual(
            authority["question"],
            "What requirement change should supersede the reviewed task specification?",
        )
        self.assertEqual(authority["origin"], "human_review_edit")
        superseded = self.repository.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision_id)
        assert superseded is not None
        self.assertEqual(superseded.payload["task_revision_authority_ref"], authority_ref)
        edit_instruction = self.store.read_json(superseded.payload["edit_instruction_ref"])
        self.assertNotIn("task_revision_authority_ref", edit_instruction)
        MinionV2OutboxProcessor(service)._create_revision(
            {
                "effect_key": "requirements-edit-child",
                "aggregate_type": AggregateType.ARCHITECTURE_REVISION.value,
                "aggregate_id": revision_id,
            }
        )
        child = next(
            item
            for item in self.repository.list_workflow_snapshots(workflow_id)
            if item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
            and item.payload.get("parent_revision_id") == revision_id
        )
        self.assertEqual(child.payload["requirements_ref"], requirements.to_dict())
        self.assertEqual(
            child.payload["pending_task_revision_authority_ref"],
            authority_ref,
        )
        self.assertEqual(child.payload["edit_scope"], "requirements")

    def test_invalid_requirements_edit_does_not_consume_human_decision(self) -> None:
        requirements, _evidence, manifest = self._publish_contract()
        workflow_id = "wf_invalid_requirements_edit"
        revision_id = "arch_invalid_requirements_edit"
        card = self._prepare_workflow_human_review(
            workflow_id=workflow_id,
            revision_id=revision_id,
            requirements_ref=requirements.to_dict(),
            manifest_ref=manifest.to_dict(),
        )

        with self.assertRaisesRegex(ValueError, "amendment prose"):
            MinionV2WorkflowService(self.runtime_root).submit_human_decision(
                {
                    "workflow_id": workflow_id,
                    "decision_token": card.decision_token,
                    "decision": "edit",
                    "edit_scope": "requirements",
                    "actor": "nathan",
                    "source_channel": "socket:test",
                }
            )
        self.assertEqual(
            self.repository.inspect_human_decision_token(card.decision_token)["status"],
            "issued",
        )

    def test_manual_human_decision_rebinds_pending_card_to_current_channel(self) -> None:
        requirements, evidence, manifest = self._publish_contract()
        workflow_id = "wf_channel_rebind"
        revision_id = "arch_channel_rebind"
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="nathan",
                expected_version=0,
                idempotency_key="create-channel-rebind",
                payload={
                    "owner": "nathan",
                    "active_channel": "socket:old-session",
                    "control_route": {"reply_target": {"session_id": "old"}},
                },
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="nathan",
                expected_version=1,
                idempotency_key="start-channel-rebind",
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="LINK_ARCHITECTURE_REVISION",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="nathan",
                expected_version=2,
                idempotency_key="link-channel-rebind",
                payload={"architecture_revision_id": revision_id},
            )
        )
        review_ref = self.store.put_json(
            {"verdict": "PASS", "findings": []},
            artifact_type="ArchitectureReviewArtifact",
        )
        self._drive_revision_to_human_review(
            workflow_id=workflow_id,
            revision_id=revision_id,
            requirements_ref=requirements.to_dict(),
            evidence_ref=evidence.to_dict(),
            manifest_ref=manifest.to_dict(),
            review_ref=review_ref.to_dict(),
        )
        old_card = self.service.create_human_review_card(
            workflow_id=workflow_id,
            architecture_revision_id=revision_id,
            manifest_ref=manifest,
            actor_id="nathan",
            active_channel_id="socket:old-session",
        )

        result = MinionV2WorkflowService(self.runtime_root).submit_human_decision(
            {
                "workflow_id": workflow_id,
                "decision": "accept",
                "actor": "nathan",
                "source_channel": "socket:new-session",
                "control_route": {"reply_target": {"session_id": "new"}},
            }
        )

        self.assertEqual(result["state"], "ACCEPTED")
        self.assertEqual(self.repository.inspect_human_decision_token(old_card.decision_token)["status"], "expired")
        rebound = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        self.assertEqual(rebound.payload["active_channel"], "socket:new-session")
        self.assertEqual(rebound.payload["control_route"], {"reply_target": {"session_id": "new"}})

    def test_workflow_status_can_render_durable_pending_human_review(self) -> None:
        requirements, evidence, manifest = self._publish_contract()
        workflow_id = "wf_review_status"
        revision_id = "arch_review_status"
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="nathan",
                expected_version=0,
                idempotency_key="create-review-status",
                payload={"owner": "nathan", "active_channel": "socket:test"},
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="START_WORKFLOW",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="nathan",
                expected_version=1,
                idempotency_key="start-review-status",
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="LINK_ARCHITECTURE_REVISION",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="nathan",
                expected_version=2,
                idempotency_key="link-review-status",
                payload={"architecture_revision_id": revision_id},
            )
        )
        review_ref = self.store.put_json(
            {"verdict": "PASS", "findings": []},
            artifact_type="ArchitectureReviewArtifact",
        )
        self._drive_revision_to_human_review(
            workflow_id=workflow_id,
            revision_id=revision_id,
            requirements_ref=requirements.to_dict(),
            evidence_ref=evidence.to_dict(),
            manifest_ref=manifest.to_dict(),
            review_ref=review_ref.to_dict(),
        )
        card_ref = self.store.put_json(
            {
                "workflow_id": workflow_id,
                "architecture_revision_id": revision_id,
                "manifest_sha": manifest.sha256,
                "actor_id": "nathan",
                "active_channel_id": "socket:expired-session",
                "decision_token": "secret-token",
                "markdown": "# Durable architecture review",
                "actions": ["accept", "edit", "reject"],
            },
            artifact_type="HumanReviewCardArtifact",
            child_refs=((manifest.sha256, "architecture_manifest"),),
        )
        claimed = self.repository.claim_outbox("legacy-card-worker", limit=20)
        review_effect = next(item for item in claimed if item["effect_type"] == "publish_architecture_review_request")
        for item in claimed:
            self.repository.complete_outbox_effect(
                item["effect_id"],
                worker_id="legacy-card-worker",
                result_artifact_ref=(
                    card_ref.to_dict() if item["effect_id"] == review_effect["effect_id"] else None
                ),
            )

        status = MinionV2WorkflowService(self.runtime_root).workflow_status(
            workflow_id,
            view="human_review",
        )

        self.assertTrue(status["waiting_for_user"])
        self.assertTrue(status["human_review_available"])
        self.assertEqual(status["active_worker"], "")
        self.assertEqual(status["active_worker_role"], "")
        self.assertEqual(status["human_review"]["markdown"], "# Durable architecture review")
        self.assertEqual(status["human_review"]["review_verdict"], "PASS")
        self.assertNotIn("decision_token", status["human_review"])

    def test_human_waiver_is_invalidated_by_manifest_or_fragment_change(self) -> None:
        _requirements, evidence, manifest = self._publish_contract()
        fragment_hashes = {"evidence_catalog": evidence.sha256}
        waiver = self.service.publish_human_waiver(
            actor="nathan",
            workflow_id="wf_waiver",
            architecture_revision_id="arch_waiver",
            manifest_ref=manifest,
            finding_refs=["finding:platform"],
            reason="Device CI is unavailable for this run.",
            allowed_impact="Platform-only verification remains unknown.",
            scope="E-1",
            fragment_hashes=fragment_hashes,
        )

        self.assertTrue(
            self.service.validate_human_waiver(
                waiver,
                manifest_ref=manifest,
                fragment_hashes=fragment_hashes,
            )
        )
        changed_manifest = self.store.put_json(
            {"changed": True},
            artifact_type="ArchitectureContractArtifact",
        )
        self.assertFalse(
            self.service.validate_human_waiver(
                waiver,
                manifest_ref=changed_manifest,
                fragment_hashes=fragment_hashes,
            )
        )
        self.assertFalse(
            self.service.validate_human_waiver(
                waiver,
                manifest_ref=manifest,
                fragment_hashes={"evidence_catalog": "changed"},
            )
        )

    def test_architect_clarification_is_not_a_hidden_aggregate_state(self) -> None:
        workflow_id = "wf_clarify"
        revision_id = "arch_clarify"
        clarification = TaskLedgerService(
            self.runtime_root,
            self.store,
        ).publish_authority(
            title="Public ABI",
            question="Which public ABI is authoritative?",
            answer="Preserve the C ABI.",
            origin="architect_user_clarification",
            actor="user",
            source_channel="test",
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="test",
                expected_version=0,
                idempotency_key="clarify:create",
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="START_ARCHITECT",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="worker",
                expected_version=1,
                idempotency_key="clarify:start",
                payload={"fencing_token": 1},
            )
        )
        recorded = self.repository.dispatch(
            ActionEnvelope(
                action_type="TASK_REVISION_AUTHORITY_RECORDED",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="worker",
                expected_version=2,
                idempotency_key="clarify:authority",
                payload={
                    "task_revision_authority_ref": clarification.to_dict(),
                    "fencing_token": 1,
                },
            )
        )
        self.assertEqual(recorded.snapshot.state, "ARCHITECT_RUNNING")
        with self.assertRaises(UnknownTransitionError):
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="CLARIFICATION_REQUIRED",
                    workflow_id=workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision_id,
                    actor="worker",
                    expected_version=3,
                    idempotency_key="clarify:required",
                    payload={"clarification_ref": clarification.to_dict()},
                )
            )
        current = self.repository.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            revision_id,
        )
        self.assertEqual(current.state, "ARCHITECT_RUNNING")
        self.assertEqual(
            current.payload["pending_task_revision_authority_ref"],
            clarification.to_dict(),
        )
        self.assertNotIn(
            "CLARIFICATION_REQUIRED",
            self.repository.engine.legal_actions(
                AggregateType.ARCHITECTURE_REVISION,
                current.state,
            ),
        )

    def test_new_human_review_card_invalidates_prior_revision_token(self) -> None:
        manifest = self.store.put_json({"version": 1}, artifact_type="ArchitectureSkeletonArtifact")
        first = self.repository.issue_human_decision_token(
            workflow_id="wf_review_reopened",
            architecture_revision_id="arch_review_reopened",
            manifest_sha=manifest.sha256,
            actor_id="nathan",
            active_channel_id="telegram:main",
        )
        second = self.repository.issue_human_decision_token(
            workflow_id="wf_review_reopened",
            architecture_revision_id="arch_review_reopened",
            manifest_sha=manifest.sha256,
            actor_id="nathan",
            active_channel_id="telegram:main",
        )

        self.assertEqual(self.repository.inspect_human_decision_token(first)["status"], "expired")
        self.assertEqual(self.repository.inspect_human_decision_token(second)["status"], "issued")

    def test_local_research_policy_removes_web_capabilities(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="research",
            profile_group="software_engineering",
            profile_name="v2_architect",
            minion_profile="software_engineering.v2_architect",
            allowed_capabilities=["op_exec_shell", "op_web_search", "op_web_read"],
        )
        local = apply_v2_research_capability_policy(pack, research_mode="local_only")
        external = apply_v2_research_capability_policy(pack, research_mode="external_allowed")
        self.assertEqual(local.allowed_capabilities, ["op_exec_shell"])
        self.assertIn("op_web_search", external.allowed_capabilities)

    def _drive_revision_to_human_review(
        self,
        *,
        workflow_id: str,
        revision_id: str,
        requirements_ref: dict,
        evidence_ref: dict,
        manifest_ref: dict,
        review_ref: dict | None = None,
    ) -> None:
        actions = [
            ("CREATE_ARCHITECTURE_REVISION", {}, 0),
            ("START_ARCHITECT", {"fencing_token": 1}, 1),
            ("DATA_ARCHITECT_COMPLETED", {"requirements_ref": requirements_ref, "architecture_manifest_ref": manifest_ref}, 2),
            ("START_ARCHITECTURE_REVIEW", {"fencing_token": 2}, 3),
            (
                "ARCHITECTURE_REVIEW_PASSED",
                {"review_artifact_ref": review_ref or manifest_ref, "architecture_manifest_ref": manifest_ref},
                4,
            ),
        ]
        for action_type, payload, version in actions:
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision_id,
                    actor="test",
                    payload=payload,
                    expected_version=version,
                    idempotency_key=f"{revision_id}:{action_type}",
                )
            )

    def _prepare_workflow_human_review(
        self,
        *,
        workflow_id: str,
        revision_id: str,
        requirements_ref: dict,
        manifest_ref: dict,
    ) -> HumanReviewCard:
        workspace = self.runtime_root / f"workspace-{workflow_id}"
        workspace.mkdir()
        request_ref = self.store.put_json(
            {
                "workspace": {
                    "kind": "existing_repo",
                    "repo_path": str(workspace),
                }
            },
            artifact_type="WorkflowRequestArtifact",
        )
        for action_type, version, payload in (
            (
                "CREATE_WORKFLOW",
                0,
                {
                    "owner": "nathan",
                    "active_channel": "socket:test",
                    "request_ref": request_ref.to_dict(),
                },
            ),
            ("START_WORKFLOW", 1, {}),
            ("LINK_ARCHITECTURE_REVISION", 2, {"architecture_revision_id": revision_id}),
        ):
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=workflow_id,
                    actor="nathan",
                    expected_version=version,
                    idempotency_key=f"{workflow_id}:{action_type}",
                    payload=payload,
                )
            )
        self._drive_revision_to_human_review(
            workflow_id=workflow_id,
            revision_id=revision_id,
            requirements_ref=requirements_ref,
            evidence_ref={},
            manifest_ref=manifest_ref,
        )
        return self.service.create_human_review_card(
            workflow_id=workflow_id,
            architecture_revision_id=revision_id,
            manifest_ref=ArtifactRef.from_mapping(manifest_ref),
            actor_id="nathan",
            active_channel_id="socket:test",
        )


if __name__ == "__main__":
    unittest.main()
