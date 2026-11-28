from __future__ import annotations
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
import yaml
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.adapters import ArtifactBundleAdapter
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateType
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.role_contracts import OrchestrationRole
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts


@dataclass
class NullExecution:
    effect_reads: EffectReads
    workflow_facts: WorkflowFacts
    artifacts: ContentAddressedArtifactStore
    repository: BunshinRepository
    runtime_root: Path

    def admit_missing_producer(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self.effect_reads.effect_snapshot(effect)
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, node.workflow_id)
        if workflow is None:
            raise ValueError("missing producer requires a workflow")
        binding = self.artifacts.read_json(workflow.payload["family_binding_ref"])
        roles = binding["role_bindings"]
        if (binding.get("family_id") == "lifestyle"
                and node.payload.get("execution_mode") != "direct"
                and roles["implementation"].get("reason") != "external_product"
                and roles["verifier"]["participant"] == "null"):
            return self.accept_null_execution(effect)
        return self.wait_for_product(effect)

    def wait_for_product(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        from pal.bunshin.cycle_protocol import CycleAction
        node = self.effect_reads.effect_snapshot(effect)
        if node.state == "WAITING_PRODUCT":
            return {"status": "waiting_product"}
        with self.repository.transaction() as connection:
            WorkflowCoordinator(self.repository).external_product_transition(
                node=node, action=CycleAction.WAIT_PRODUCT, unit_of_work=connection)
            connection.transitions.dispatch(ActionEnvelope(
                action_type="WAIT_EXTERNAL_PRODUCT", workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id,
                expected_version=node.version, actor="bunshin-manager",
                idempotency_key=f"external-wait:{node.aggregate_id}:{node.version}", payload={},
            ))
        return {"status": "waiting_product"}

    def skip_checker(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self.effect_reads.effect_snapshot(effect)
        if node.state == "ACCEPTED" and node.payload.get("verification_status") == "not_required":
            return {"status": "accepted", "node_run_id": node.aggregate_id}
        if node.state != "REVIEW_QUEUED":
            raise ValueError("checker skip requires a queued checker")
        candidate = _ref_from_mapping(node.payload.get("candidate_ref"))
        receipt = self.artifacts.put_json(
            {"status": "not_required", "candidate_ref": candidate.to_dict(),
             "candidate_digest": node.payload["candidate_digest"]},
            artifact_type="NullVerificationReceiptArtifact",
            child_refs=((candidate.sha256, "candidate"),),
        )
        with self.repository.transaction() as connection:
            WorkflowCoordinator(self.repository).skip_node_checker(
                workflow_id=node.workflow_id,
                node_name=str(node.payload.get("module_name") or node.payload["unit_id"]),
                unit_of_work=connection,
            )
            connection.transitions.dispatch(ActionEnvelope(
                action_type="SKIP_NODE_CHECKER", workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id,
                expected_version=node.version, actor="bunshin-manager",
                idempotency_key=f"skip-checker:{node.aggregate_id}:{candidate.sha256}",
                payload={"verification_artifact_ref": receipt.to_dict(),
                         "verification_status": "not_required"},
            ))
        return {"status": "accepted", "node_run_id": node.aggregate_id}

    def accept_null_execution(
        self,
        effect: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Settle an explicitly external/human role without spawning a worker."""

        node = self.effect_reads.effect_snapshot(effect)
        if self.workflow_facts.role_participant_kind(
            node.workflow_id,
            OrchestrationRole.VERIFIER.value,
        ) != "null":
            raise ValueError(
                "null implementation requires a null verifier binding"
            )
        contract_ref = _ref_from_mapping(
            node.payload.get("unit_contract_ref")
        )
        role_binding = self.workflow_facts.role_binding(
            node.workflow_id,
            OrchestrationRole.IMPLEMENTATION.value,
        )
        receipt = {
            "schema_version": "1",
            "status": "not_applicable",
            "module_name": str(
                node.payload.get("module_name")
                or node.payload.get("unit_id")
                or ""
            ),
            "reason": str(
                role_binding.get("reason")
                or "external_human_execution"
            ),
            "contract_ref": contract_ref.to_dict(),
        }
        workspace = Path(str(node.payload.get("workspace_path") or ""))
        workspace.mkdir(parents=True, exist_ok=True)
        source_payload = dict(
            self.artifacts.read_json(
                (
                    node.payload.get("architecture_manifest_ref")
                    if bool(node.payload.get("graph_sink"))
                    else contract_ref
                )
            )
        )
        deliverable_contract = dict(
            source_payload.get("contract") or source_payload
        )
        (workspace / "architect.yaml").write_text(
            yaml.safe_dump(
                deliverable_contract,
                allow_unicode=True,
                default_flow_style=False,
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        candidate_ref, candidate_digest = ArtifactBundleAdapter(
            self.runtime_root,
            self.artifacts,
        ).snapshot_candidate(
            workspace=workspace,
            reference_only_paths=(),
            unit_contract_hash=contract_ref.sha256,
            dependency_output_hashes={},
            environment_fingerprint="null-executor.v1",
        )
        verification_ref = self.artifacts.put_json(
            {
                **receipt,
                "verification_status": "not_applicable",
            },
            artifact_type="NullVerificationReceiptArtifact",
            provenance={"owner": "manager", "participant": "null"},
            child_refs=((candidate_ref.sha256, "candidate"),),
        )
        graph_contract_hash = str(
            node.payload.get("graph_contract_hash") or ""
        )
        if not graph_contract_hash:
            raise ValueError(
                "null execution node has no GraphIR contract hash"
            )
        module_name = str(
            node.payload.get("module_name")
            or node.payload.get("unit_id")
            or ""
        )
        coordinator = WorkflowCoordinator(self.repository)
        with self.repository.transaction() as connection:
            accepted = (connection or self.repository).transitions.dispatch(
                ActionEnvelope(
                    action_type="ACCEPT_NULL_EXECUTION",
                    workflow_id=node.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="bunshin-manager",
                    expected_version=node.version,
                    idempotency_key=(
                        f"effect:{effect['effect_key']}:null-execution"
                    ),
                    payload={
                        "candidate_ref": candidate_ref.to_dict(),
                        "candidate_digest": candidate_digest,
                        "verification_artifact_ref": (
                            verification_ref.to_dict()
                        ),
                        "graph_contract_hash": graph_contract_hash,
                        "output_hashes": {},
                        "null_execution": True,
                    },
                ),
            ).snapshot
            coordinator.accept_null_node(
                workflow_id=node.workflow_id,
                node_name=module_name,
                product_ref=candidate_ref.sha256,
                input_fingerprint=str(effect["effect_key"]),
                unit_of_work=connection,
            )
        return {
            "status": "accepted",
            "node_run_id": accepted.aggregate_id,
            "result_artifact_ref": candidate_ref.to_dict(),
        }
