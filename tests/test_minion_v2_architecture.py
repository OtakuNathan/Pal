from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from pal.minion.v2 import ActionEnvelope, AggregateType, ContentAddressedArtifactStore, MinionV2Repository
from pal.minion.v2.architecture import (
    ArchitectureArtifactService,
    ComplexityBudgetPolicy,
    HumanReviewCard,
    ResearchMode,
    validate_module_contract,
)
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.workers import apply_v2_research_capability_policy
from pal.shared import TaskContextPack


def _complexity_budget(**overrides: int) -> dict[str, int]:
    result = {
        "target_file_count": 4,
        "estimated_context_tokens": 12000,
        "public_interface_count": 4,
        "cross_module_contract_count": 1,
        "stateful_resource_count": 0,
        "expected_candidate_cycles": 2,
        "platform_dependency_level": 1,
    }
    result.update(overrides)
    return result


def _module_contract() -> dict:
    return {
        "module_id": "foundation",
        "module_behavior_kind": "stateless",
        "responsibility": "Define the stable value types used by consumers.",
        "owned_area": ["src/foundation/**"],
        "reference_only_paths": ["references/**"],
        "provided_interfaces": [{"name": "Geometry", "lifetime": "value"}],
        "consumed_interfaces": [],
        "ownership": {"values": "caller-owned"},
        "lifecycle": "N/A: pure value definitions",
        "state_model": "stateless",
        "invariants": ["value layout is stable"],
        "error_behavior": [],
        "compatibility": ["C ABI"],
        "dependency_constraints": [],
        "requirement_ids": ["R-1"],
        "evidence_ids": ["E-1"],
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
        requirements = self.service.publish_requirements(
            {
                "requirements": [
                    {
                        "requirement_id": "R-1",
                        "statement": "Expose stable geometry value types.",
                        "strength": "hard",
                        "source_refs": ["user:turn"],
                    }
                ]
            }
        )
        evidence = self.service.publish_evidence_catalog(
            {
                "evidence": [
                    {
                        "evidence_id": "E-1",
                        "source_kind": "local",
                        "location": "reference.patch:10-40",
                        "line_start": 10,
                        "line_end": 40,
                        "summary": "Reference value layout and mapping.",
                        "supports_requirement_ids": ["R-1"],
                    }
                ]
            },
            requirements_ref=requirements,
            research_mode=ResearchMode.LOCAL_ONLY,
        )
        module = self.service.publish_module_contract(_module_contract())
        constraints = self.service.publish_fragment([], artifact_type="GlobalConstraintsArtifact")
        decisions = self.service.publish_fragment([], artifact_type="DesignDecisionsArtifact")
        cross = self.service.publish_fragment(
            {"contract_id": "X-1", "provider": "foundation", "consumer": "integration"},
            artifact_type="CrossModuleContractArtifact",
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
                "evidence_catalog_ref": evidence.to_dict(),
                "global_constraints_ref": constraints.to_dict(),
                "design_decisions_ref": decisions.to_dict(),
                "module_contract_refs": [module.to_dict()],
                "cross_module_contract_refs": [cross.to_dict()],
                "topology_ref": topology.to_dict(),
                "integration_contract_ref": integration.to_dict(),
                "assumption_ledger_ref": assumptions.to_dict(),
                "risk_ledger_ref": risks.to_dict(),
            }
        )
        return requirements, evidence, manifest

    def test_stateless_module_does_not_need_a_fake_state_machine(self) -> None:
        validated = validate_module_contract(_module_contract(), complexity_policy=ComplexityBudgetPolicy())
        self.assertEqual(validated["state_model"], "stateless")
        stateful = {**_module_contract(), "module_behavior_kind": "resource_owner", "state_model": "", "lifecycle": ""}
        with self.assertRaisesRegex(ValueError, "explicit lifecycle"):
            validate_module_contract(stateful, complexity_policy=ComplexityBudgetPolicy())

    def test_module_contract_rejects_milestones_and_unbounded_complexity(self) -> None:
        with self.assertRaisesRegex(ValueError, "implementation-level"):
            validate_module_contract(
                {**_module_contract(), "milestones": [{"title": "write it"}]},
                complexity_policy=ComplexityBudgetPolicy(),
            )
        too_large = {
            **_module_contract(),
            "complexity_budget": _complexity_budget(target_file_count=40),
        }
        with self.assertRaisesRegex(ValueError, "without split_conditions"):
            validate_module_contract(too_large, complexity_policy=ComplexityBudgetPolicy())

    def test_manifest_review_checks_evidence_coverage_and_topology(self) -> None:
        _requirements, _evidence, manifest = self._publish_contract()
        review = self.service.review_manifest(manifest)
        self.assertEqual(review.verdict, "PASS")
        markdown = self.service.compile_human_review_markdown(manifest)
        self.assertIn("## Module Topology", markdown)
        self.assertIn("### foundation", markdown)
        self.assertNotIn("milestone", markdown.lower())

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

    def test_clarification_token_resumes_requirements_queue_once(self) -> None:
        workflow_id = "wf_clarify"
        revision_id = "arch_clarify"
        clarification = self.store.put_json(
            {"questions": [{"question": "Which public ABI is authoritative?"}]},
            artifact_type="ClarificationRequestArtifact",
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
                action_type="START_REQUIREMENTS",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="worker",
                expected_version=1,
                idempotency_key="clarify:start",
                payload={"fencing_token": 1},
            )
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CLARIFICATION_REQUIRED",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="worker",
                expected_version=2,
                idempotency_key="clarify:required",
                payload={"clarification_ref": clarification.to_dict()},
            )
        )
        token = self.repository.issue_human_decision_token(
            workflow_id=workflow_id,
            architecture_revision_id=revision_id,
            manifest_sha=clarification.sha256,
            actor_id="nathan",
            active_channel_id="socket:test",
        )
        service = MinionV2WorkflowService(self.runtime_root)
        result = service.submit_human_decision(
            {
                "decision_token": token,
                "decision": "clarify",
                "clarification_response": "The checked-in OHOS ABI headers are authoritative.",
                "actor": "nathan",
                "source_channel": "socket:test",
            }
        )
        self.assertEqual(result["state"], "REQUIREMENTS_QUEUED")
        self.assertEqual(self.repository.inspect_human_decision_token(token)["status"], "consumed")
        with self.assertRaisesRegex(ValueError, "stale or already consumed"):
            service.submit_human_decision(
                {
                    "decision_token": token,
                    "decision": "clarify",
                    "clarification_response": "duplicate",
                    "actor": "nathan",
                    "source_channel": "socket:test",
                }
            )

    def test_local_research_policy_removes_web_capabilities(self) -> None:
        pack = TaskContextPack(
            work_order_id="research",
            profile_group="software_engineering",
            profile_name="v2_researcher",
            minion_profile="software_engineering.v2_researcher",
            allowed_capabilities=["op_tree", "op_web_search", "op_web_read"],
        )
        local = apply_v2_research_capability_policy(pack, research_mode="local_only")
        external = apply_v2_research_capability_policy(pack, research_mode="external_allowed")
        self.assertEqual(local.allowed_capabilities, ["op_tree"])
        self.assertIn("op_web_search", external.allowed_capabilities)

    def _drive_revision_to_human_review(
        self,
        *,
        workflow_id: str,
        revision_id: str,
        requirements_ref: dict,
        evidence_ref: dict,
        manifest_ref: dict,
    ) -> None:
        actions = [
            ("CREATE_ARCHITECTURE_REVISION", {}, 0),
            ("START_REQUIREMENTS", {"fencing_token": 1}, 1),
            ("REQUIREMENTS_COMPLETED", {"requirements_ref": requirements_ref}, 2),
            ("START_RESEARCH", {"fencing_token": 2}, 3),
            ("RESEARCH_COMPLETED", {"evidence_catalog_ref": evidence_ref}, 4),
            ("START_PLANNING", {"fencing_token": 3}, 5),
            ("PLANNING_COMPLETED", {"architecture_manifest_ref": manifest_ref}, 6),
            ("START_ARCHITECTURE_REVIEW", {"fencing_token": 4}, 7),
            (
                "ARCHITECTURE_REVIEW_PASSED",
                {"review_artifact_ref": manifest_ref, "architecture_manifest_ref": manifest_ref},
                8,
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


if __name__ == "__main__":
    unittest.main()
