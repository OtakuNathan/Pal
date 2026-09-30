from __future__ import annotations
from pal.bunshin.v2.contract_runtime import ContractArtifactAccess
from pal.bunshin.v2.adapters import ARTIFACT_BUNDLE_ADAPTER, SOFTWARE_GIT_ADAPTER
from pal.bunshin.v2.artifacts import ArtifactRef
from pal.bunshin.v2.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.graph_executor import GraphDiff, diff_graphs
from pal.bunshin.v2.graph_protocol import GraphIR
from pal.bunshin.v2.module_identity_facts import _artifact_is_contract
from pal.bunshin.v2.artifact_replan import _reconcile_data_contract_module_identities
from pal.bunshin.v2.git_replan import _reconcile_skeleton_module_identities
from pal.bunshin.v2.graph_installation import _workflow_execution_adapter


def reconcile_module_identities(
    *,
    repository: BunshinV2Repository,
    contracts: ContractArtifactAccess,
    workflow_id: str,
    source_epoch_id: str,
    target_epoch_id: str,
    target_manifest_ref: ArtifactRef,
    target_graph: GraphIR,
    installed_graph_diff: GraphDiff | None,
    actor: str,
) -> tuple[str, ...]:
    source_epoch = repository.snapshots.read_snapshot(
        AggregateType.EXECUTION_EPOCH, source_epoch_id
    )
    if source_epoch is None:
        return ()
    source_manifest_ref = ArtifactRef.from_mapping(
        dict(source_epoch.payload.get("architecture_manifest_ref") or {})
    )
    source_record = repository.artifacts.read_artifact_record(
        source_manifest_ref.sha256
    )
    target_record = repository.artifacts.read_artifact_record(
        target_manifest_ref.sha256
    )
    if not (
        _artifact_is_contract(source_record)
        and _artifact_is_contract(target_record)
    ):
        return ()
    graph_diff = _replan_graph_diff(
        repository=repository,
        workflow_id=workflow_id,
        source_epoch=source_epoch,
        target_graph=target_graph,
        installed_graph_diff=installed_graph_diff,
    )
    execution_adapter = _workflow_execution_adapter(
        repository,
        contracts,
        workflow_id,
    )
    if execution_adapter == SOFTWARE_GIT_ADAPTER:
        return _reconcile_skeleton_module_identities(
            repository=repository,
            contracts=contracts,
            workflow_id=workflow_id,
            source_epoch_id=source_epoch_id,
            target_epoch_id=target_epoch_id,
            source_manifest_ref=source_manifest_ref,
            target_manifest_ref=target_manifest_ref,
            target_graph=target_graph,
            graph_diff=graph_diff,
            actor=actor,
        )
    if execution_adapter == ARTIFACT_BUNDLE_ADAPTER:
        return _reconcile_data_contract_module_identities(
            repository=repository,
            contracts=contracts,
            workflow_id=workflow_id,
            source_epoch_id=source_epoch_id,
            target_epoch_id=target_epoch_id,
            target_graph=target_graph,
            graph_diff=graph_diff,
            actor=actor,
        )
    raise ValueError(
        "workflow selected an unsupported execution adapter: "
        + execution_adapter
    )


def _replan_graph_diff(
    *,
    repository: BunshinV2Repository,
    workflow_id: str,
    source_epoch: AggregateSnapshot,
    target_graph: GraphIR,
    installed_graph_diff: GraphDiff | None,
) -> GraphDiff:
    """Resolve the one graph-level reuse decision for a compiled replan.

    GraphExecution is authoritative for which accepted products may cross a
    generation boundary.  Aggregate node rows are only a durable projection,
    so they must consume this decision rather than independently deciding
    which products to carry forward.
    """

    source_generation = int(source_epoch.payload.get("graph_generation") or 0)
    if source_generation < 1:
        raise RuntimeError(
            "replan source execution epoch has no GraphIR generation"
        )
    source_graph = repository.cycles.read_graph_generation(
        graph_id=workflow_id,
        generation=source_generation,
    )
    if source_graph is None:
        raise RuntimeError(
            "replan source GraphIR generation is unavailable"
        )
    if source_graph.graph_id != target_graph.graph_id:
        raise RuntimeError("replan GraphIR identity changed across one workflow")
    if (
        installed_graph_diff is not None
        and installed_graph_diff.source_generation == source_generation
        and installed_graph_diff.target_generation == target_graph.generation
    ):
        return installed_graph_diff
    return diff_graphs(source_graph, target_graph)
