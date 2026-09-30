from __future__ import annotations
from typing import Any, Mapping
from pal.bunshin.v2.contract_runtime import ContractArtifactAccess
from pal.bunshin.v2.contracts import AggregateType
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.role_contracts import family_execution_adapter, validate_family_binding_payload
from pal.bunshin.v2.graph_protocol import graph_ir_from_mapping
from pal.bunshin.v2.workflow_runtime import InstalledGraph, WorkflowCoordinator


def _workflow_execution_adapter(
    repository: BunshinV2Repository,
    contracts: ContractArtifactAccess,
    workflow_id: str,
) -> str:
    workflow = repository.snapshots.read_snapshot(
        AggregateType.WORKFLOW,
        workflow_id,
    )
    if workflow is None:
        raise ValueError(
            f"workflow is unavailable while resolving execution strategy: "
            f"{workflow_id}"
        )
    binding_ref = dict(workflow.payload.get("family_binding_ref") or {})
    if not binding_ref.get("sha256"):
        raise ValueError("workflow has no pinned FamilyBindingArtifact")
    binding = dict(contracts.artifacts.read_json(binding_ref))
    validate_family_binding_payload(binding)
    return family_execution_adapter(binding.get("execution_adapter"))


def _install_execution_graph(
    artifact: Mapping[str, Any],
    *,
    workflow_id: str,
    repository: BunshinV2Repository,
) -> InstalledGraph:
    raw = artifact.get("graph_ir")
    if not isinstance(raw, Mapping) or not raw:
        raise ValueError(
            "ContractArtifact has no Manager-compiled GraphIR; fresh v29 "
            "contracts must be re-authored"
        )
    graph = graph_ir_from_mapping(raw)
    if graph.graph_id != workflow_id:
        raise ValueError("ContractArtifact GraphIR belongs to another workflow")
    return WorkflowCoordinator(repository).install_graph(
        workflow_id=workflow_id,
        graph=graph,
    )
