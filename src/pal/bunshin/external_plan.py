"""External Planner handoff; the existing PlanCycle remains the single cursor."""
from __future__ import annotations

from typing import Any, Mapping, TYPE_CHECKING

from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.bunshin.cycle_protocol import CycleAction, PlanCycleState
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.workflow_runtime import WorkflowCoordinator

if TYPE_CHECKING:
    from pal.bunshin.service import BunshinWorkflowService


def wait_external_plan(repository: BunshinRepository, revision: AggregateSnapshot) -> dict[str, Any]:
    if revision.state == "WAITING_PRODUCT":
        return {"status": "waiting_product"}
    with repository.transaction() as unit:
        WorkflowCoordinator(repository).transition_plan(
            workflow_id=revision.workflow_id, action=CycleAction.WAIT_PRODUCT, unit_of_work=unit)
        unit.transitions.dispatch(ActionEnvelope(
            action_type="WAIT_EXTERNAL_PLAN", workflow_id=revision.workflow_id,
            aggregate_type=AggregateType.ARCHITECTURE_REVISION, aggregate_id=revision.aggregate_id,
            expected_version=revision.version, actor="bunshin-manager",
            idempotency_key=f"wait-plan:{revision.aggregate_id}:{revision.version}", payload={}))
    return {"status": "waiting_product"}


def submit_external_plan(service: BunshinWorkflowService, data: Mapping[str, Any]) -> dict[str, Any]:
    repository, artifacts = service.repository, service.artifacts
    workflow_id = str(data["workflow_id"])
    revision_id = str(data["node_run_id"])
    identity = str(data["submission_id"])
    ref = dict(data["artifact_ref"])
    if not identity:
        raise ValueError("plan submission identity required")
    workflow = repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    revision = repository.snapshots.read_snapshot(AggregateType.ARCHITECTURE_REVISION, revision_id)
    if workflow is None or revision is None or revision.workflow_id != workflow_id:
        raise ValueError("unknown workflow plan")
    if workflow.payload.get("owner") != str(data.get("actor") or "pal"):
        raise ValueError("only the workflow owner may import a plan")
    previous = revision.payload.get("external_plan_submission")
    if previous == {"id": identity, "sha256": ref.get("sha256")}:
        return {"status": "imported", "submission_id": identity, "revision_id": revision_id}
    if (workflow.state != "ACTIVE" or revision.state != "WAITING_PRODUCT"
            or workflow.payload.get("architecture_revision_id") != revision_id):
        raise ValueError("workflow is not waiting for this plan")
    record = repository.artifacts.read_artifact_record(str(ref.get("sha256") or ""))
    if not record or not record.get("durable") or record.get("artifact_type") != "ContractArtifact":
        raise ValueError("external plan must be an actual durable ContractArtifact")
    product = artifacts.read_json(ref)
    if product.get("requirements_ref") != revision.payload.get("requirements_ref"):
        raise ValueError("external plan requirements do not match the current task")
    binding = artifacts.read_json(workflow.payload["family_binding_ref"])
    if binding["role_bindings"]["architect"]["participant"] != "null":
        raise ValueError("Planner is not external")
    service._validate_external_architecture_ref(ref, trusted_required=False, family_binding_ref=workflow.payload["family_binding_ref"])
    compiled_ref = _compile_external_plan(service, workflow_id, product, binding, ref)
    with repository.transaction() as unit:
        current_workflow = unit.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if current_workflow is None or current_workflow.state != "ACTIVE" or current_workflow.payload.get("architecture_revision_id") != revision_id:
            raise ValueError("external plan lost its workflow authority")
        coordinator = WorkflowCoordinator(repository)
        cycle = coordinator.ensure_plan_cycle(workflow_id=workflow_id, unit_of_work=unit)
        if cycle.state != PlanCycleState.WAITING_PRODUCT or cycle.generation != int(data["generation"]):
            raise ValueError("external plan belongs to another generation")
        coordinator.transition_plan(workflow_id=workflow_id, action=CycleAction.IMPORT_PRODUCT,
                                    product_ref=compiled_ref.sha256, unit_of_work=unit)
        unit.transitions.dispatch(ActionEnvelope(
            action_type="IMPORT_EXTERNAL_PLAN", workflow_id=workflow_id,
            aggregate_type=AggregateType.ARCHITECTURE_REVISION, aggregate_id=revision_id,
            actor=str(data.get("actor") or "pal"), expected_version=revision.version,
            idempotency_key=f"external-plan:{revision_id}:{identity}",
            payload={"architecture_manifest_ref": compiled_ref.to_dict(), "requirements_ref": revision.payload["requirements_ref"],
                     "external_plan_submission": {"id": identity, "sha256": ref["sha256"]}}))
    return {"status": "imported", "submission_id": identity, "revision_id": revision_id}


def _compile_external_plan(service: BunshinWorkflowService, workflow_id: str, product: Mapping[str, Any],
                           binding: Mapping[str, Any], input_ref: Mapping[str, Any]) -> ArtifactRef:
    """Compile the authored contract using the current workflow's pinned roles."""
    from pal.bunshin.architect_submission import _graph_role_binding
    from pal.bunshin.architecture_compilation import compiled_architecture_definition_from_mapping
    from pal.bunshin.contract_protocol import validate_contract_payload
    from pal.bunshin.graph_compiler import GraphCompiler, GraphCompileBindings
    from pal.bunshin.graph_satellites import FamilyGraphSatelliteProjector
    artifacts = service.artifacts
    architecture = dict(binding["architecture_definition"])
    definition = compiled_architecture_definition_from_mapping({
        **architecture,
        "schema": artifacts.read_json(architecture["schema_ref"]),
        "template": artifacts.read_json(architecture["template_ref"])["template"],
        "graph_satellite_template": artifacts.read_json(architecture["satellite_template_ref"])["template"],
        "example": {},
    })
    document = validate_contract_payload(product["contract"], definition=definition)
    previous = service.repository.cycles.read_graph_execution(workflow_id=workflow_id)
    generation = previous.graph.generation + 1 if previous is not None else 1
    roles = binding["role_bindings"]
    source = str(input_ref["sha256"])
    source_map = artifacts.put_json({"source_ref": source, "locations": {}}, artifact_type="GraphSourceMapArtifact",
                                    child_refs=((source, "external_plan"),))
    graph = GraphCompiler().compile(document, graph_id=workflow_id, generation=generation,
        bindings=GraphCompileBindings(producer=_graph_role_binding(roles["implementation"]),
                                     checker=_graph_role_binding(roles["verifier"]),
                                     execution_adapter=str(binding["execution_adapter"])),
        satellite_projector=FamilyGraphSatelliteProjector(specialization_id=definition.specialization_id,
                                                         template=definition.graph_satellite_template),
        source_ref=source, source_map_ref=source_map.sha256,
        workspace_authority_rules=definition.workspace_authority_rules)
    children = [(source, "external_plan"), (source_map.sha256, "source_map"),
                (str(product["requirements_ref"]["sha256"]), "requirements")]
    for field in ("git_bundle_ref", "workspace_snapshot_ref"):
        if product.get(field):
            children.append((str(product[field]["sha256"]), field))
    return artifacts.put_json({**product, "graph_ir": graph.to_dict()}, artifact_type="ContractArtifact",
                              child_refs=tuple(children), provenance={"owner": "manager", "source": "external_plan"})
