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
                    actor="bunshin-v2-manager",
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
