"""Import an actual external Git product into the active node cycle."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import Any, Mapping

from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.candidate_snapshots import _validate_skeleton_candidate_paths
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.bunshin.cycle_protocol import CycleAction
from pal.bunshin.direct_contract import deliverable_paths
from pal.bunshin.execution_values import workspace_content_fingerprint
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.bunshin.workspace_git import _git, _git_bytes, _git_is_ancestor
from pal.bunshin.workspace_paths import repository_path_targets_control_plane
from pal.bunshin.workspace_resources import exclusive_workspace_lock


def submit_external_product(repository: BunshinRepository, artifacts: ContentAddressedArtifactStore,
                            data: Mapping[str, Any]) -> dict[str, Any]:
    workflow_id = str(data["workflow_id"])
    node_id = str(data["node_run_id"])
    identity = str(data["submission_id"])
    candidate = str(data.get("commit_sha") or "")
    if not identity:
        raise ValueError("external submission needs an identity")
    workflow = repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
    node = repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
    if workflow is None or node is None or node.workflow_id != workflow_id:
        raise ValueError("unknown workflow or node")
    if workflow.payload.get("owner") != str(data.get("actor") or "pal"):
        raise ValueError("only the workflow owner may import a product")
    from pal.bunshin.role_protocol import stable_hash
    source = Path(str(data.get("repository") or "")).expanduser()
    bundle_fingerprint = ""
    if node.payload.get("execution_adapter") == "artifact_bundle.v2":
        from pal.bunshin.adapters import artifact_tree_fingerprint
        if not data.get("repository") or not source.is_dir():
            raise ValueError("external bundle requires an explicit directory")
        bundle_fingerprint = artifact_tree_fingerprint(source)
    submission_fingerprint = stable_hash({"request": dict(data), "bundle": bundle_fingerprint})
    previous = dict(node.payload.get("external_submission") or {})
    if previous.get("id") == identity:
        if previous.get("fingerprint") != submission_fingerprint:
            raise ValueError("external submission id reused with different product")
        return {"status": "imported", "submission_id": identity, "node_run_id": node_id, "candidate_digest": str(previous.get("digest") or previous.get("commit") or candidate)}
    if (workflow.state != "ACTIVE" or node.state != "WAITING_PRODUCT"
            or int(data["generation"]) != int(node.payload.get("graph_generation") or 0)):
        raise ValueError("node is not waiting for this generation's external Git product")
    if node.payload.get("execution_adapter") == "artifact_bundle.v2":
        return _import_bundle(repository, artifacts, node, data, submission_fingerprint, bundle_fingerprint)
    if node.payload.get("execution_adapter") != "software_git.v2" or not re.fullmatch(r"[0-9a-f]{40}", candidate):
        raise ValueError("external Git submission requires a full commit SHA")
    worktree = Path(str(node.payload["workspace_path"]))
    source = Path(str(data["repository"])).expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ValueError("external Git source must be a repository directory")
    with exclusive_workspace_lock(worktree):
        current = repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, node_id)
        if current is None or current.version != node.version:
            raise ValueError("external submission lost node authority")
        execution = WorkflowCoordinator(repository).execution(workflow_id=workflow_id)
        name = str(node.payload.get("module_name") or node.payload["unit_id"])
        if execution.graph.nodes[name].producer_binding.participant != "null":
            raise ValueError("external submission requires a null producer")
        if _git(worktree, "status", "--porcelain", "--untracked-files=all").strip():
            raise ValueError("external product cannot overwrite a dirty node workspace")
        base = str(node.payload["base_sha"])
        _git(worktree, "fetch", "--no-tags", str(source), candidate)
        if not _git_is_ancestor(worktree, base, candidate):
            raise ValueError("external product does not preserve the node baseline")
        changed = sorted(x for x in _git(worktree, "diff", "--name-only", "--no-renames", "-z", base, candidate).split("\0") if x)
        if any(repository_path_targets_control_plane(path) for path in changed):
            raise ValueError("external product modifies control-plane paths")
        _validate_skeleton_candidate_paths(changed, dict(node.payload["path_policy"]))
        for entry in _git(worktree, "ls-tree", "-r", candidate).splitlines():
            if entry.split(None, 1)[0] not in {"100644", "100755"}:
                raise ValueError("external products must contain regular files")
        manifest = artifacts.read_json(node.payload["architecture_manifest_ref"])
        report = {"status": "candidate_ready", "files_changed": changed, "work_items": []}
        if manifest.get("domain") == "general":
            if not isinstance(data.get("result_text"), str) or not str(data["result_text"]).strip():
                raise ValueError("general external result requires result_text")
            paths = deliverable_paths({"deliverable_paths": [*manifest.get("deliverable_paths", []),
                                                             *list(data.get("deliverable_paths") or [])]})
            for path in paths:
                if not _git(worktree, "ls-tree", candidate, "--", f":(literal){path}").strip():
                    raise ValueError("external deliverable file is missing")
            report.update(result_text=data["result_text"], next_steps=list(data.get("next_steps") or []), deliverable_paths=paths)
        candidate_key = hashlib.sha256((node_id + identity).encode()).hexdigest()
        _git(worktree, "update-ref", "refs/pal/checkpoints/" + candidate_key, candidate)
        _git(worktree, "reset", "--hard", candidate)
        fingerprint = workspace_content_fingerprint(worktree)
        candidate_ref = artifacts.put_json({
            "schema_version": "3", "node_run_id": node_id, "candidate_digest": candidate,
            "base_sha": base, "architecture_base_sha": base, "previous_head_sha": base,
            "baseline_tree_sha": _git(worktree, "rev-parse", base + "^{tree}").strip(),
            "candidate_tree_sha": _git(worktree, "rev-parse", candidate + "^{tree}").strip(),
            "delta_patch_sha": hashlib.sha256(_git_bytes(worktree, "diff", "--binary", base, candidate)).hexdigest(),
            "unit_contract_hash": node.payload["unit_contract_ref"]["sha256"],
            "dependency_output_hashes": dict(node.payload.get("dependency_output_hashes") or {}),
            "environment_fingerprint": node.payload.get("environment_fingerprint", "default"),
            "repair_bill_ref": dict(node.payload.get("repair_bill_ref") or {}),
            "workspace_fingerprint": fingerprint, "changed_paths": changed,
            "candidate_key": candidate_key,
        }, artifact_type="GitCheckpointArtifact", provenance={"producer": "external"})
        report_ref = artifacts.put_json(report, artifact_type="ProducerReportArtifact")
        with repository.transaction() as unit:
            WorkflowCoordinator(repository).external_product_transition(node=node, action=CycleAction.IMPORT_PRODUCT,
                unit_of_work=unit, product_ref=candidate_ref.sha256)
            unit.transitions.dispatch(ActionEnvelope(action_type="IMPORT_EXTERNAL_PRODUCT", workflow_id=workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node_id, expected_version=node.version,
                actor=str(data.get("actor") or "pal"), idempotency_key=f"external:{node_id}:{identity}",
                payload={"candidate_ref": candidate_ref.to_dict(), "candidate_digest": candidate,
                         "producer_report_ref": report_ref.to_dict(), "workspace_fingerprint": fingerprint,
                         "external_submission": {"id": identity, "commit": candidate, "digest": candidate, "fingerprint": submission_fingerprint}}))
    return {"status": "imported", "submission_id": identity, "node_run_id": node_id, "candidate_digest": candidate}


def _import_bundle(repository: BunshinRepository, artifacts: ContentAddressedArtifactStore,
                   node: AggregateSnapshot, data: Mapping[str, Any], submission_fingerprint: str,
                   bundle_fingerprint: str) -> dict[str, Any]:
    from pal.bunshin.adapters import ArtifactBundleAdapter, artifact_tree_fingerprint
    source = Path(str(data["repository"])).expanduser().resolve(strict=True)
    if not source.is_dir():
        raise ValueError("artifact product must be a directory")
    before = artifact_tree_fingerprint(source)
    contract = artifacts.read_json(node.payload["unit_contract_ref"])
    candidate_ref, digest = ArtifactBundleAdapter(artifacts.runtime_root, artifacts).snapshot_candidate(
        workspace=source, reference_only_paths=contract.get("reference_only_paths", []),
        unit_contract_hash=node.payload["unit_contract_ref"]["sha256"],
        dependency_output_hashes=dict(node.payload.get("dependency_output_hashes") or {}),
        environment_fingerprint=str(node.payload.get("environment_fingerprint") or "default"),
        parent_candidate_digest=str(node.payload.get("candidate_digest") or ""),
        repair_bill_ref=dict(node.payload.get("repair_bill_ref") or {}),
    )
    product = artifacts.read_json(candidate_ref)
    if before != bundle_fingerprint or before != artifact_tree_fingerprint(source) or not product.get("files"):
        raise ValueError("external bundle must be a stable, nonempty actual product")
    report = artifacts.put_json({"status": "candidate_ready", "work_items": [],
                                 "files_changed": [item["path"] for item in product["files"]]},
                                artifact_type="ProducerReportArtifact")
    with repository.transaction() as unit:
        WorkflowCoordinator(repository).external_product_transition(node=node, action=CycleAction.IMPORT_PRODUCT,
            unit_of_work=unit, product_ref=candidate_ref.sha256)
        unit.transitions.dispatch(ActionEnvelope(action_type="IMPORT_EXTERNAL_PRODUCT", workflow_id=node.workflow_id,
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id, expected_version=node.version,
            actor=str(data.get("actor") or "pal"), idempotency_key=f"external:{node.aggregate_id}:{data['submission_id']}",
            payload={"candidate_ref": candidate_ref.to_dict(), "candidate_digest": digest,
                     "producer_report_ref": report.to_dict(), "workspace_fingerprint": before,
                     "external_submission": {"id": str(data["submission_id"]), "commit": "", "digest": digest, "fingerprint": submission_fingerprint}}))
    return {"status": "imported", "submission_id": str(data["submission_id"]), "node_run_id": node.aggregate_id, "candidate_digest": digest}
