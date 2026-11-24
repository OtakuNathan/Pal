from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.adapters import SOFTWARE_GIT_ADAPTER
from pal.bunshin.architecture_compilation import compiled_architecture_definition_from_mapping
from pal.bunshin.contracts import AggregateType
from pal.bunshin.contract_protocol import ARCHITECT_FILENAME, software_contract_projection, validate_contract_payload
from pal.bunshin.graph_compiler import GraphCompileBindings, GraphCompiler, build_yaml_source_map
from pal.bunshin.graph_satellites import FamilyGraphSatelliteProjector
from pal.bunshin.graph_protocol import GraphSourceMap, RoleBinding
from pal.bunshin.role_contracts import family_execution_adapter, validate_family_binding_payload
from pal.bunshin.service import BunshinWorkflowService
from pal.bunshin.skeleton import preflight_architecture_workspace_submission
from pal.bunshin.task_ledger import validate_task_ledger
from pal.bunshin.work_items import assert_work_items_complete, submission_work_items
from pal.bunshin.submission_errors import SubmissionValidationError, submission_validation


def _graph_role_binding(value: Mapping[str, Any]) -> RoleBinding:
    binding = dict(value or {})
    participant = str(binding.get("participant") or "")
    profile = dict(binding.get("role_profile") or {})
    return RoleBinding(
        participant=participant,
        profile_id=(
            str(
                profile.get("canonical_profile_id")
                or profile.get("bunshin_profile")
                or ""
            ).strip()
            if participant == "profile"
            else ""
        ),
        reason=(
            str(binding.get("reason") or "").strip()
            if participant == "null"
            else ""
        ),
    )



def compile_architect_submission(
    service: BunshinWorkflowService,
    load_prompt_pack: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    authenticated: Mapping[str, Any],
    submission: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one authored projection against its pinned Family binding.

    The sandbox sends only the parsed ``architect.yaml`` instance. Raw JSON
    Schema and compiler state stay behind this Manager gateway.
    """

    payload = dict(submission)
    if set(payload) != {"source", "architecture"}:
        raise SubmissionValidationError(
            "architect submission requires only source and architecture"
        )
    if str(payload.get("source") or "") != ARCHITECT_FILENAME:
        raise SubmissionValidationError(
            f"architect submission source must be {ARCHITECT_FILENAME}"
        )
    architecture = payload.get("architecture")
    if not isinstance(architecture, Mapping):
        raise SubmissionValidationError("architect submission architecture must be an object")

    assignment = dict(authenticated["assignment"])
    binding_sha = str(assignment.get("family_binding_sha") or "").strip()
    binding_record = service.repository.artifacts.read_artifact_record(binding_sha)
    if binding_record is None:
        raise ValueError("pinned FamilyBindingArtifact is unavailable")
    if str(binding_record.get("artifact_type") or "") != "FamilyBindingArtifact":
        raise ValueError("pinned family binding has the wrong artifact type")
    binding = dict(service.artifacts.read_json(binding_record))
    validate_family_binding_payload(binding)
    architecture_binding = dict(
        binding.get("architecture_definition") or {}
    )
    schema_payload = service.artifacts.read_json(
        dict(architecture_binding.get("schema_ref") or {})
    )
    template_payload = service.artifacts.read_json(
        dict(architecture_binding.get("template_ref") or {})
    )
    satellite_template_payload = service.artifacts.read_json(
        dict(architecture_binding.get("satellite_template_ref") or {})
    )
    if not isinstance(schema_payload, Mapping):
        raise ValueError("pinned architecture schema artifact is malformed")
    if not isinstance(template_payload, Mapping):
        raise ValueError("pinned architect template artifact is malformed")
    if not isinstance(satellite_template_payload, Mapping):
        raise ValueError("pinned graph satellite template artifact is malformed")
    if (
        str(satellite_template_payload.get("specialization_id") or "")
        != str(architecture_binding.get("specialization_id") or "")
        or str(satellite_template_payload.get("generation_hash") or "")
        != str(architecture_binding.get("generation_hash") or "")
    ):
        raise ValueError(
            "pinned graph satellite template does not match its Family binding"
        )
    definition = compiled_architecture_definition_from_mapping(
        {
            "specialization_id": architecture_binding["specialization_id"],
            "family_id": architecture_binding["family_id"],
            "generation_hash": architecture_binding["generation_hash"],
            "schema": dict(schema_payload),
            "template": str(template_payload.get("template") or ""),
            "graph_satellite_template": str(
                satellite_template_payload.get("template") or ""
            ),
            "example": {},
        }
    )
    with submission_validation():
        document = validate_contract_payload(
            dict(architecture),
            definition=definition,
        )

    if (
        family_execution_adapter(binding.get("execution_adapter"))
        == SOFTWARE_GIT_ADAPTER
    ):
        revision = service.repository.snapshots.read_snapshot(
            AggregateType.ARCHITECTURE_REVISION,
            str(assignment["aggregate_id"]),
        )
        if revision is None:
            raise ValueError("architecture revision is unavailable")
        workspace_root = Path(
            str(revision.payload.get("architecture_workspace_path") or "")
        )
        base_sha = str(
            revision.payload.get("architecture_base_sha") or ""
        ).strip()
        requirements_ref = dict(revision.payload.get("requirements_ref") or {})
        workspace_snapshot_ref = dict(
            revision.payload.get("workspace_snapshot_ref") or {}
        )
        if (
            not workspace_root.is_dir()
            or not base_sha
            or not requirements_ref.get("sha256")
            or not workspace_snapshot_ref.get("sha256")
        ):
            raise ValueError(
                "architecture workspace preflight state is incomplete"
            )
        requirements = validate_task_ledger(
            service.artifacts.read_json(requirements_ref)
        )
        workspace_snapshot = service.artifacts.read_json(
            workspace_snapshot_ref
        )
        validation, _ = (
            preflight_architecture_workspace_submission(
                software_contract_projection(
                    document.model_dump(mode="python")
                ),
                requirements_payload=requirements,
                workspace_root=workspace_root,
                base_sha=base_sha,
                original_head=str(
                    dict(workspace_snapshot).get("original_head") or ""
                ),
            )
        )
        with submission_validation():
            validation.raise_for_errors()

    prompt_pack = load_prompt_pack(authenticated)
    workspace = {
        **dict(prompt_pack.get("workspace") or {}),
        "runtime_root": str(service.runtime_root),
        "bunshin": dict(
            dict(prompt_pack.get("metadata") or {}).get("bunshin") or {}
        ),
    }
    # Architecture revisions and installed GraphIR generations are
    # independent sequences.  A human edit may supersede any number of
    # authored revisions before one is accepted, and those discarded
    # revisions must not consume GraphIR generations.  Compile the
    # candidate against the next append-only GraphIR slot; installation
    # still performs the transactional gap/identity check.
    previous_graph = service.repository.cycles.read_graph_generation(
        graph_id=str(assignment["workflow_id"]),
    )
    graph_generation = (
        int(previous_graph.generation) + 1
        if previous_graph is not None
        else 1
    )
    source_map = (
        build_yaml_source_map(
            Path(str(workspace["architect_path"]))
        )
        if str(workspace.get("architect_path") or "").strip()
        else GraphSourceMap(
            source_ref=ARCHITECT_FILENAME,
            locations={},
        )
    )
    source_map_ref = service.artifacts.put_json(
        source_map.to_dict(),
        artifact_type="GraphSourceMapArtifact",
        provenance={
            "workflow_id": str(assignment["workflow_id"]),
            "architecture_assignment_id": str(
                assignment["assignment_id"]
            ),
        },
    )
    with submission_validation():
        graph_ir = GraphCompiler().compile(
            document,
            graph_id=str(assignment["workflow_id"]),
            generation=graph_generation,
            bindings=GraphCompileBindings(
                producer=_graph_role_binding(
                    dict(binding["role_bindings"])["implementation"]
                ),
                checker=_graph_role_binding(
                    dict(binding["role_bindings"])["verifier"]
                ),
                execution_adapter=str(binding["execution_adapter"]),
            ),
            satellite_projector=FamilyGraphSatelliteProjector(
                specialization_id=definition.specialization_id,
                template=definition.graph_satellite_template,
            ),
            source_ref=ARCHITECT_FILENAME,
            workspace_authority_rules=definition.workspace_authority_rules,
            source_map=source_map,
            source_map_ref=source_map_ref.sha256,
        )
    work_items = assert_work_items_complete(workspace)
    return {
        "schema_version": "2",
        "contract_schema": definition.specialization_id,
        "contract": document.model_dump(mode="python"),
        "graph_ir": graph_ir.to_dict(),
        "graph_source_map_ref": source_map_ref.to_dict(),
        "source": ARCHITECT_FILENAME,
        "work_items": submission_work_items(work_items.get("items")),
    }
