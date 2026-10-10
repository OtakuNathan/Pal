"""Manager-owned task binding for a single repository producer/checker cycle.

The task ledger is the semantic authority. The generated graph is execution
metadata, not an authored architecture or evidence of an architecture review.
"""
from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from pal.bunshin.artifacts import ArtifactRef
from pal.bunshin.contracts import AggregateSnapshot
from pal.bunshin.graph_protocol import GraphIR, NodeSpec, RoleBinding
from pal.bunshin.workspace_git import _git
from pal.bunshin.direct_contract import DIRECT_EXECUTION_ARTIFACT, DIRECT_MODULE, deliverable_paths, validate_direct_binding


if TYPE_CHECKING:
    from pal.bunshin.service import BunshinWorkflowService


def prepare_direct_execution(
    service: BunshinWorkflowService,
    workflow_id: str,
    request: Mapping[str, Any],
    *,
    base_artifact: Mapping[str, Any] | None = None,
) -> ArtifactRef:
    artifacts = service.artifacts
    task_ref = ArtifactRef.from_mapping(request["requirements_ref"])
    task = artifacts.read_json(task_ref)
    outputs = deliverable_paths(dict(task.get("original") or {}))
    binding = artifacts.read_json(request["family_binding_ref"])
    validate_direct_binding(binding)
    workspace = service.skeleton.provision_architecture_workspace(
        workflow_id=workflow_id, workflow_name=str(request.get("workflow_name") or workflow_id),
        revision_name="direct-input", workspace=dict(request.get("workspace") or {}),
        requirements_ref=task_ref, base_artifact=base_artifact,
    )
    # This worktree only captures the source baseline; no architect runs and
    # no declaration files or artificial architecture commit are produced.
    with tempfile.TemporaryDirectory(prefix="pal-direct-bundle-") as temporary:
        bundle = Path(temporary) / "source.bundle"
        _git(workspace.worktree, "bundle", "create", str(bundle), workspace.architecture_branch)
        bundle_ref = artifacts.put_bytes(bundle.read_bytes(), artifact_type="DirectSourceBundleArtifact",
                                        media_type="application/x-git-bundle")
    policy = {
        "contract_mode": "review_guarded", "contract_paths": [],
        "implementation_scopes": [{"kind": "repository", "path": "."}],
        "reference_only": [],
    }
    source_map = artifacts.put_json({"source_ref": task_ref.sha256, "locations": {}},
                                    artifact_type="GraphSourceMapArtifact",
                                    child_refs=((task_ref.sha256, "task"),))
    roles = binding["role_bindings"]
    node = NodeSpec(
        name=DIRECT_MODULE, responsibility="Complete the bound task in the repository",
        satellite_data={"execution_mode": "direct", "requirements_ref": task_ref.to_dict(),
                        "deliverable_paths": outputs},
        producer_binding=RoleBinding("profile", roles["implementation"]["role_profile"]["canonical_profile_id"]),
        checker_binding=RoleBinding("profile", roles["verifier"]["role_profile"]["canonical_profile_id"]),
        execution_adapter="software_git.v2", workspace_policy=policy,
        output_contract=tuple(outputs) or ("task_result",), is_sink=True,
    )
    graph = GraphIR(graph_id=workflow_id, generation=1, nodes={DIRECT_MODULE: node},
                    edges=(), sink=DIRECT_MODULE, source_ref=task_ref.sha256, source_map_ref=source_map.sha256)
    payload = {
        "schema_version": "1", "execution_mode": "direct", "requirements_ref": task_ref.to_dict(),
        "input_binding_ref": dict(request.get("input_binding_ref") or {}),
        "direct_reference_refs": dict(request.get("direct_reference_refs") or {}),
        "deliverable_paths": outputs, "graph_ir": graph.to_dict(),
        "submission": {"context": {"language": dict(request.get("workspace") or {}).get("primary_language", "")},
                       "modules": {DIRECT_MODULE: {"responsibility": node.responsibility,
                                                   "dependencies": {}, "paths": policy}},
                       "requirements": {}, "scenarios": {}},
        "git_bundle_ref": bundle_ref.to_dict(), "workspace_snapshot_ref": workspace.workspace_snapshot_ref.to_dict(),
        "base_commit_sha": workspace.base_sha, "base_tree_sha": workspace.base_tree_sha,
        # Shared Git execution metadata; these names remain stable for existing
        # candidates and recovery records. Here they identify the source snapshot.
        "skeleton_commit_sha": workspace.base_sha, "skeleton_tree_sha": workspace.base_tree_sha,
        "contract_file_hashes": {}, "original_workspace_head": workspace.original_head,
        "source_fingerprint": workspace.source_fingerprint,
        "repository_layout": {"project_name": workspace.project_name, "project_key": workspace.project_key,
                              "workflow_name": workspace.workflow_name, "workflow_key": workspace.workflow_key,
                              "workflow_branch": workspace.workflow_branch},
    }
    if base_artifact:
        for key in ("workspace_snapshot_ref", "base_commit_sha", "base_tree_sha", "original_workspace_head", "source_fingerprint"):
            payload[key] = base_artifact[key]
    return artifacts.put_json(payload, artifact_type=DIRECT_EXECUTION_ARTIFACT,
                              provenance={"owner": "manager", "source": "direct_task"},
                              child_refs=((task_ref.sha256, "task"), (bundle_ref.sha256, "source_bundle"),
                                          (payload["workspace_snapshot_ref"]["sha256"], "workspace_snapshot"),
                                          (source_map.sha256, "source_map"),
                                          *((ref["sha256"], "reference") for ref in dict(request.get("direct_reference_refs") or {}).values()),
                                          *(((request["input_binding_ref"]["sha256"], "inputs"),)
                                            if request.get("input_binding_ref") else ())))


def resolve_direct_task(
    service: BunshinWorkflowService,
    node: AggregateSnapshot,
    *,
    answer: str,
    actor: str,
    source_channel: str,
) -> dict[str, Any]:
    """Atomically advance task authority and requeue the existing repository sandbox."""
    from dataclasses import replace
    from datetime import UTC, datetime
    import json
    from pal.bunshin.contracts import ActionEnvelope, AggregateType
    from pal.bunshin.workflow_runtime import WorkflowCoordinator

    repository = service.repository
    old_ref = dict(node.payload["architecture_manifest_ref"])
    old = dict(service.artifacts.read_json(old_ref))
    blocker = dict(node.payload["blocker"])
    finding = service.artifacts.read_json(blocker["finding_ref"])
    task_ref = service.task_ledger.append_revision(
        base_ref=old["requirements_ref"], actor=actor, source_channel=source_channel,
        authority={"title": "Direct task clarification", "question": str(blocker.get("question") or json.dumps(finding, ensure_ascii=False)),
                   "answer": answer, "origin": "direct_user_clarification",
                   "observed_at": datetime.now(UTC).isoformat()},
    )
    previous = repository.cycles.read_graph_execution(workflow_id=node.workflow_id)
    graph = previous.graph
    specification = graph.nodes[DIRECT_MODULE]
    updated = replace(specification, satellite_data={**specification.satellite_data,
                                                     "requirements_ref": task_ref.to_dict()})
    source_map = service.artifacts.put_json({"source_ref": task_ref.sha256, "locations": {}},
        artifact_type="GraphSourceMapArtifact", child_refs=((task_ref.sha256, "task"),))
    graph = replace(graph, generation=graph.generation + 1, source_ref=task_ref.sha256,
                    source_map_ref=source_map.sha256, nodes={DIRECT_MODULE: updated})
    manifest_ref = service.artifacts.put_json(
        {**old, "requirements_ref": task_ref.to_dict(), "graph_ir": graph.to_dict()},
        artifact_type=DIRECT_EXECUTION_ARTIFACT,
        child_refs=((old_ref["sha256"], "previous_binding"), (task_ref.sha256, "task"), (source_map.sha256, "source_map")),
    )
    old_contract = service.artifacts.read_json(node.payload["unit_contract_ref"])
    contract_ref = service.artifacts.put_json({**old_contract, "requirements_ref": task_ref.to_dict()},
        artifact_type=ArtifactRef.from_mapping(node.payload["unit_contract_ref"]).artifact_type,
        child_refs=((manifest_ref.sha256, "task_binding"),))
    epoch = repository.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, node.payload["epoch_id"])
    workflow = repository.snapshots.read_snapshot(AggregateType.WORKFLOW, node.workflow_id)
    with repository.transaction() as connection:
        WorkflowCoordinator(repository).rebind_direct_task(workflow_id=node.workflow_id, graph=graph,
                                                          unit_of_work=connection)
        for snapshot, action, payload in (
            (workflow, "BIND_DIRECT_EXECUTION", {"direct_execution_ref": manifest_ref.to_dict()}),
            (epoch, "REBIND_DIRECT_TASK", {"architecture_manifest_ref": manifest_ref.to_dict(), "architecture_manifest_sha": manifest_ref.sha256, "graph_generation": graph.generation}),
            (node, "RESOLVE_TRIAGE", {"architecture_manifest_ref": manifest_ref.to_dict(),
                "graph_generation": graph.generation, "graph_contract_hash": updated.contract_hash,
                "unit_contract_ref": contract_ref.to_dict(), "direct_task_rebound": True,
                "triage_resolution": answer, "triage_resolution_kind": "task_clarification"}),
        ):
            result = connection.transitions.dispatch(ActionEnvelope(
                action_type=action, workflow_id=node.workflow_id, aggregate_type=snapshot.aggregate_type,
                aggregate_id=snapshot.aggregate_id, expected_version=snapshot.version,
                actor=actor, source_channel=source_channel,
                idempotency_key=f"direct-clarification:{snapshot.aggregate_id}:{task_ref.sha256}", payload=payload))
    return {"status": "triage_resolved", "workflow_id": node.workflow_id,
            "subject": DIRECT_MODULE, "state": result.snapshot.state, "resolution": answer}
