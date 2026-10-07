from __future__ import annotations
from typing import Any, Mapping
from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.contract_protocol import CONTRACT_ARTIFACT
from pal.bunshin.repository import BunshinV2Repository


def _artifact_is_contract(
    record: Mapping[str, Any] | None,
) -> bool:
    return (
        str(dict(record or {}).get("artifact_type") or "")
        == CONTRACT_ARTIFACT
    )


def _same_module_identity(
    source_node: AggregateSnapshot,
    target_node: AggregateSnapshot,
) -> bool:
    source = str(source_node.payload.get("module_responsibility") or "")
    target = str(target_node.payload.get("module_responsibility") or "")
    # Existing runtime rows predate responsibility identity. Preserve them
    # once; all newly compiled nodes carry the explicit identity field.
    return not source or _normalized_responsibility(source) == _normalized_responsibility(target)


def _module_role_session_generations(
    repository: BunshinV2Repository,
    *,
    workflow_id: str,
    source_epoch_id: str,
    target_subjects: set[str],
    replaced_subjects: set[str] | None = None,
) -> dict[str, int]:
    """Preserve a Module identity, or allocate a fresh generation after removal."""

    source_epoch = str(source_epoch_id or "").strip()
    source_generations: dict[str, int] = {}
    historical_generations: dict[str, int] = {}
    for snapshot in repository.queries.list_workflow_snapshots(workflow_id):
        if snapshot.aggregate_type != AggregateType.DAG_NODE_RUN:
            continue
        subject = str(
            snapshot.payload.get("module_name")
            or snapshot.payload.get("unit_id")
            or ""
        ).strip()
        if not subject:
            continue
        generation = max(
            0, int(snapshot.payload.get("role_session_generation") or 0)
        )
        historical_generations[subject] = max(
            historical_generations.get(subject, 0),
            generation,
        )
        if source_epoch and str(snapshot.payload.get("epoch_id") or "") == source_epoch:
            source_generations[subject] = max(
                source_generations.get(subject, 0),
                generation,
            )
    replaced = set(replaced_subjects or set())
    return {
        subject: (
            source_generations[subject] + 1
            if subject in source_generations and subject in replaced
            else source_generations[subject]
            if subject in source_generations
            else historical_generations[subject] + 1
            if subject in historical_generations
            else 0
        )
        for subject in target_subjects
    }


def _module_identity_delta(
    repository: BunshinV2Repository,
    *,
    workflow_id: str,
    source_epoch_id: str,
    target_module_responsibilities: Mapping[str, str],
) -> dict[str, list[str]]:
    source_epoch = str(source_epoch_id or "").strip()
    source_modules = {
        str(snapshot.payload.get("module_name") or snapshot.payload.get("unit_id") or ""): str(
            snapshot.payload.get("module_responsibility") or ""
        )
        for snapshot in repository.queries.list_workflow_snapshots(workflow_id)
        if source_epoch
        and snapshot.aggregate_type == AggregateType.DAG_NODE_RUN
        and str(snapshot.payload.get("epoch_id") or "") == source_epoch
        and str(snapshot.payload.get("node_kind") or "") == "unit"
    }
    source_modules.pop("", None)
    target_modules = {
        str(name): str(responsibility)
        for name, responsibility in target_module_responsibilities.items()
    }
    common = set(source_modules) & set(target_modules)
    replaced = {
        name
        for name in common
        if source_modules[name]
        and _normalized_responsibility(source_modules[name])
        != _normalized_responsibility(target_modules[name])
    }
    return {
        "preserved": sorted(common - replaced),
        "replaced": sorted(replaced),
        "added": sorted(set(target_modules) - set(source_modules)),
        "deleted": sorted(set(source_modules) - set(target_modules)),
    }


def _normalized_responsibility(value: str) -> str:
    return " ".join(str(value).casefold().split())
