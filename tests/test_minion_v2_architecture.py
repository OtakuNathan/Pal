from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from pal.minion.v2 import ActionEnvelope, AggregateType, ContentAddressedArtifactStore, MinionV2Repository
from pal.minion.v2.architecture import (
    ArchitectureArtifactService,
    ArchitectureFindingKind,
    ComplexityBudgetPolicy,
    HumanReviewCard,
    ResearchMode,
    review_architecture_contract,
    validate_unit_contract,
)
from pal.minion.v2.service import MinionV2WorkflowService
from pal.minion.v2.skeleton import requirements_semantic_view
from pal.minion.v2.workers import apply_v2_research_capability_policy
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
        "ownership": {"values": "caller-owned"},
        "lifecycle": "N/A: pure value definitions",
        "state_model": "stateless",
        "invariants": ["value layout is stable"],
        "error_behavior": [],
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

    def test_evidence_publish_requires_all_requirements_to_be_linked(self) -> None:
        requirements = self.service.publish_requirements(
            {
                "requirements": [
                    {"requirement_id": "R-1", "statement": "First", "strength": "hard"},
                    {"requirement_id": "R-2", "statement": "Second", "strength": "hard"},
                ]
            }
        )

        with self.assertRaisesRegex(ValueError, "requirements lack supporting evidence: R-2"):
            self.service.publish_evidence_catalog(
                {
                    "evidence": [
                        {
                            "evidence_id": "E-1",
                            "source_kind": "local",
                            "location": "reference.patch:1-2",
                            "line_start": 1,
                            "line_end": 2,
                            "summary": "Only the first requirement is linked.",
                            "supports_requirement_ids": ["R-1"],
                        }
                    ]
                },
                requirements_ref=requirements,
                research_mode=ResearchMode.LOCAL_ONLY,
            )

    def test_requirement_patch_is_manager_timestamped_and_creates_an_immutable_revision(self) -> None:
        base_ref = self.service.publish_requirements(
            {
                "title": "Router",
                "requirements": [
                    {
                        "section": "Routing",
                        "statement": "Route matching must be deterministic.",
                        "strength": "hard",
                    }
                ],
            }
        )
        finding_ref = self.store.put_json(
            {"summary": "Reset order is externally observable."},
            artifact_type="RepairBillArtifact",
        )
        patch_ref, revised_ref = self.service.publish_requirement_patch(
            base_requirements_ref=base_ref,
            proposal={
                "patch_kind": "derived_constraint",
                "section": "Reset semantics",
                "requirement": "Reset must preserve the configured route precedence order.",
                "strength": "hard",
                "reason": "The public reset operation otherwise changes observable routing behavior.",
                "affected_modules": ["router"],
                "affected_contracts": [
                    {"module": "router", "path": "include/router.h", "symbol": "reset"}
                ],
            },
            source={
                "role": "verifier",
                "stage": "scenario_verification",
                "case": "reset preserves route precedence",
                "finding_summary": "Reset reverses equal-priority routes.",
            },
            source_artifact_ref=finding_ref,
        )

        base = self.store.read_json(base_ref)
        patch = self.store.read_json(patch_ref)
        revised = self.store.read_json(revised_ref)
        self.assertEqual(len(base["requirements"]), 1)
        self.assertEqual(len(revised["requirements"]), 2)
        self.assertEqual(patch["observed_at"], patch["proposed_at"])
        self.assertEqual(patch["source"]["role"], "verifier")
        self.assertEqual(revised["requirement_patch_refs"], [patch_ref.to_dict()])
        semantic = requirements_semantic_view(revised)
        encoded = json.dumps(semantic, sort_keys=True)
        self.assertIn("Reset must preserve the configured route precedence order.", encoded)
        self.assertIn("reset preserves route precedence", encoded)
        self.assertNotIn(patch_ref.sha256, encoded)
        self.assertNotIn("source_artifact_ref", encoded)

        with self.assertRaisesRegex(ValueError, "add new product semantics"):
            self.service.publish_requirement_patch(
                base_requirements_ref=revised_ref,
                proposal={
                    "patch_kind": "derived_constraint",
                    "section": "Reset semantics",
                    "requirement": "Reset must preserve the configured route precedence order.",
                    "strength": "hard",
                    "reason": "Duplicate proposal.",
                    "affected_modules": ["router"],
                    "affected_contracts": [],
                },
                source={"role": "verifier", "stage": "module_verification"},
            )

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
        return requirements, evidence, manifest

    def test_stateless_module_does_not_need_a_fake_state_machine(self) -> None:
        validated = validate_unit_contract(_unit_contract(), complexity_policy=ComplexityBudgetPolicy())
        self.assertEqual(validated["state_model"], "stateless")
        stateful = {**_unit_contract(), "unit_behavior_kind": "resource_owner", "state_model": "", "lifecycle": ""}
        with self.assertRaisesRegex(ValueError, "explicit lifecycle"):
            validate_unit_contract(stateful, complexity_policy=ComplexityBudgetPolicy())

    def test_unit_contract_rejects_milestones_and_unbounded_complexity(self) -> None:
        with self.assertRaisesRegex(ValueError, "implementation-level"):
            validate_unit_contract(
                {**_unit_contract(), "milestones": [{"title": "write it"}]},
                complexity_policy=ComplexityBudgetPolicy(),
            )
        too_large = {
            **_unit_contract(),
            "complexity_budget": _complexity_budget(target_file_count=40),
        }
        with self.assertRaisesRegex(ValueError, "without split_conditions"):
            validate_unit_contract(too_large, complexity_policy=ComplexityBudgetPolicy())

    def test_manifest_review_checks_evidence_coverage_and_topology(self) -> None:
        _requirements, _evidence, manifest = self._publish_contract()
        review = self.service.review_manifest(manifest)
        self.assertEqual(review.verdict, "PASS")
        markdown = self.service.compile_human_review_markdown(manifest)
        self.assertIn("## Module Topology", markdown)
        self.assertIn("### foundation", markdown)
        self.assertNotIn("milestone", markdown.lower())

    def test_mechanical_review_rejects_unwaived_complexity_and_missing_handoff(self) -> None:
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

        self.assertEqual(review.verdict, "FAIL")
        self.assertTrue(all(item.revision_targets for item in review.findings))
        self.assertTrue(any("complexity budget" in item.summary for item in review.findings))
        handoff = next(item for item in review.findings if "without a directional cross-unit contract" in item.summary)
        self.assertEqual(handoff.finding_kind, ArchitectureFindingKind.CONTRACT_DEFECT)
        self.assertEqual(handoff.revision_targets[0].section, "cross_unit_contract")
        self.assertEqual(handoff.revision_targets[0].operation, "create")

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
                "workflow_id": workflow_id,
                "decision": "clarify",
                "clarification_response": "The checked-in OHOS ABI headers are authoritative.",
                "actor": "nathan",
                "source_channel": "socket:test",
            }
        )
        self.assertEqual(result["state"], "ARCHITECT_QUEUED")
        self.assertEqual(self.repository.inspect_human_decision_token(token)["status"], "expired")
        with self.assertRaisesRegex(ValueError, "no pending human decision"):
            service.submit_human_decision(
                {
                    "workflow_id": workflow_id,
                    "decision": "clarify",
                    "clarification_response": "duplicate",
                    "actor": "nathan",
                    "source_channel": "socket:test",
                }
            )

    def test_local_research_policy_removes_web_capabilities(self) -> None:
        pack = MinionInvocationPack(
            invocation_id="research",
            profile_group="software_engineering",
            profile_name="v2_architect",
            minion_profile="software_engineering.v2_architect",
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
            ("START_ARCHITECT", {"fencing_token": 1}, 1),
            ("ARCHITECT_COMPLETED", {"requirements_ref": requirements_ref, "architecture_manifest_ref": manifest_ref}, 2),
            ("START_ARCHITECTURE_REVIEW", {"fencing_token": 2}, 3),
            (
                "ARCHITECTURE_REVIEW_PASSED",
                {"review_artifact_ref": manifest_ref, "architecture_manifest_ref": manifest_ref},
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


if __name__ == "__main__":
    unittest.main()
