from __future__ import annotations
from pal.bunshin.v2.workspace_git import _git as _git
from pal.bunshin.v2.workspace_git import _git_bytes as _git_bytes
from pal.bunshin.v2.workspace_git import _git_is_ancestor as _git_is_ancestor
from pal.bunshin.v2.workspace_git import _git_ref_exists as _git_ref_exists
from pal.bunshin.v2.workspace_git import _abort_cherry_pick as _abort_cherry_pick
from pal.bunshin.v2.workspace_git import git_changed_paths as git_changed_paths
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.v2.adapters import ARTIFACT_BUNDLE_ADAPTER, SOFTWARE_GIT_ADAPTER, artifact_tree_fingerprint
from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.contracts import AggregateSnapshot
from pal.bunshin.v2.execution_models import DependencyIntegrationConflict
from pal.bunshin.v2.execution_graph_facts import _ordered_dependency_closure
from pal.bunshin.v2.execution_graph_facts import dependency_fingerprint
from pal.bunshin.v2.execution_values import workspace_content_fingerprint


def prepare_node_dependency_baseline(
    node: AggregateSnapshot,
    node_by_id: Mapping[str, AggregateSnapshot],
    *,
    apply_candidates: bool = True,
) -> dict[str, Any]:
    workspace = Path(str(node.payload.get("workspace_path") or ""))
    if not workspace.is_dir():
        raise ValueError(f"node workspace does not exist: {workspace}")
    adapter = str(node.payload.get("execution_adapter") or "").strip()
    if adapter not in {SOFTWARE_GIT_ADAPTER, ARTIFACT_BUNDLE_ADAPTER}:
        raise ValueError(
            "node dependency baseline has no supported bound execution adapter"
        )
    if (
        adapter == SOFTWARE_GIT_ADAPTER
        and apply_candidates
        and node.state == "BLOCKED_BY_DEPS"
        and not bool(node.payload.get("module_replan_prepared"))
    ):
        declared_base = str(node.payload.get("base_sha") or "")
        if not declared_base:
            raise ValueError("blocked node has no declared Git baseline")
        _abort_cherry_pick(workspace)
        _git(workspace, "reset", "--hard", declared_base)
        _git(workspace, "clean", "-fd")
    starting_head = (
        _git(workspace, "rev-parse", "HEAD").strip()
        if adapter == SOFTWARE_GIT_ADAPTER and apply_candidates
        else ""
    )
    recorded_applied_digests = _recorded_applied_dependency_digests(
        node,
        workspace=workspace,
        starting_head=starting_head,
    )
    accepted_digests: list[str] = []
    output_hashes: dict[str, str] = {}
    dependency_outputs: dict[str, Any] = {}
    try:
        for dependency in _ordered_dependency_closure(node, node_by_id):
            dependency_id = dependency.aggregate_id
            if dependency.state != "ACCEPTED":
                raise ValueError(f"dependency is not accepted: {dependency_id}")
            candidate_digest = str(dependency.payload.get("candidate_digest") or "")
            if not candidate_digest:
                raise ValueError(f"accepted dependency has no candidate digest: {dependency_id}")
            if adapter == SOFTWARE_GIT_ADAPTER:
                if (
                    apply_candidates
                    and recorded_applied_digests.get(dependency_id)
                    != candidate_digest
                ):
                    _apply_dependency_candidate_delta(workspace, dependency)
            elif adapter != ARTIFACT_BUNDLE_ADAPTER:
                raise ValueError(f"unsupported execution adapter: {adapter}")
            accepted_digests.append(candidate_digest)
            dependency_outputs[dependency_id] = {
                "candidate_ref": dict(dependency.payload.get("candidate_ref") or {}),
                "candidate_digest": candidate_digest,
                "output_hashes": dict(dependency.payload.get("output_hashes") or {}),
            }
            output_hashes[dependency_id] = hashlib.sha256(
                json.dumps(dict(dependency.payload.get("output_hashes") or {}), sort_keys=True).encode("utf-8")
            ).hexdigest()
    except BaseException:
        if starting_head:
            _abort_cherry_pick(workspace)
            _git(workspace, "reset", "--hard", starting_head)
            _git(workspace, "clean", "-fd")
        raise
    base_digest = (
        _git(workspace, "rev-parse", "HEAD").strip()
        if adapter == SOFTWARE_GIT_ADAPTER
        else artifact_tree_fingerprint(workspace)
    )
    return {
        "base_digest": base_digest,
        "base_sha": base_digest if adapter == SOFTWARE_GIT_ADAPTER else "",
        "accepted_dependency_candidate_digests": accepted_digests,
        "dependency_output_hashes": output_hashes,
        "dependency_outputs": dependency_outputs,
        "dependency_fingerprint": dependency_fingerprint(node, node_by_id),
    }


def prepare_node_verification_baseline(
    node: AggregateSnapshot,
    node_by_id: Mapping[str, AggregateSnapshot],
    *,
    artifacts: ContentAddressedArtifactStore,
) -> dict[str, Any]:
    """Assemble accepted dependency products after the producer submits.

    Software producers work from contracts in parallel.  The Manager applies
    accepted dependency deltas only at the checker boundary and publishes a
    new immutable checkpoint describing the assembled worktree.  The original
    implementation checkpoint remains linked for module-local diff review.
    """

    dependency_ids = {
        str(item) for item in list(node.payload.get("dependency_node_ids") or [])
    }
    candidate_ref = ArtifactRef.from_mapping(
        dict(node.payload.get("candidate_ref") or {})
    )
    candidate_digest = str(node.payload.get("candidate_digest") or "")
    if not candidate_digest:
        raise ValueError("verification baseline requires a Candidate digest")
    adapter = str(node.payload.get("execution_adapter") or "").strip()
    baseline = prepare_node_dependency_baseline(
        node,
        node_by_id,
        apply_candidates=True,
    )
    verification_digest = str(
        baseline.pop("base_sha", "") or baseline.pop("base_digest", "")
    )
    baseline.pop("base_digest", None)
    result = {
        **baseline,
        "verification_base_sha": verification_digest,
    }
    if adapter != SOFTWARE_GIT_ADAPTER or not dependency_ids:
        return result
    if verification_digest == candidate_digest:
        return result
    workspace = Path(str(node.payload.get("workspace_path") or ""))
    try:
        implementation_candidate = dict(artifacts.read_json(candidate_ref))
        review_base_sha = str(
            implementation_candidate.get("base_sha")
            or implementation_candidate.get("previous_head_sha")
            or ""
        )
        if not review_base_sha:
            raise ValueError("implementation Candidate has no Git review base")
        if _git(workspace, "rev-parse", "HEAD").strip() != verification_digest:
            raise RuntimeError("assembled verification worktree moved unexpectedly")
        candidate_tree_sha = _git(
            workspace, "rev-parse", f"{verification_digest}^{{tree}}"
        ).strip()
        baseline_tree_sha = _git(
            workspace, "rev-parse", f"{review_base_sha}^{{tree}}"
        ).strip()
        delta_patch = _git_bytes(
            workspace,
            "diff",
            "--binary",
            review_base_sha,
            verification_digest,
            "--",
        )
        assembly_key = hashlib.sha256(
            json.dumps(
                {
                    "node_run_id": node.aggregate_id,
                    "implementation_candidate_digest": candidate_digest,
                    "dependency_candidate_digests": baseline[
                        "accepted_dependency_candidate_digests"
                    ],
                },
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        assembled_candidate = {
            **implementation_candidate,
            "candidate_digest": verification_digest,
            "base_sha": review_base_sha,
            "previous_head_sha": review_base_sha,
            "baseline_tree_sha": baseline_tree_sha,
            "candidate_tree_sha": candidate_tree_sha,
            "delta_patch_sha": hashlib.sha256(delta_patch).hexdigest(),
            "workspace_fingerprint": workspace_content_fingerprint(workspace),
            "changed_paths": git_changed_paths(workspace, review_base_sha),
            "candidate_key": assembly_key,
            "implementation_candidate_ref": candidate_ref.to_dict(),
            "implementation_candidate_digest": candidate_digest,
            "accepted_dependency_candidate_digests": baseline[
                "accepted_dependency_candidate_digests"
            ],
            "dependency_output_hashes": baseline["dependency_output_hashes"],
            "assembly_boundary": "verification",
        }
        child_refs = [(candidate_ref.sha256, "implementation_candidate")]
        child_refs.extend(
            (
                str(
                    dict(
                        node_by_id[node_id].payload.get("candidate_ref") or {}
                    )["sha256"]
                ),
                "accepted_dependency_candidate",
            )
            for node_id in sorted(dependency_ids)
        )
        assembled_ref = artifacts.put_json(
            assembled_candidate,
            artifact_type="GitCheckpointArtifact",
            provenance={"owner": "manager", "purpose": "verification_assembly"},
            child_refs=tuple(child_refs),
        )
    except BaseException:
        _abort_cherry_pick(workspace)
        _git(workspace, "reset", "--hard", candidate_digest)
        _git(workspace, "clean", "-fd")
        raise
    result.update(
        {
            "implementation_candidate_ref": candidate_ref.to_dict(),
            "implementation_candidate_digest": candidate_digest,
            "candidate_ref": assembled_ref.to_dict(),
            "candidate_digest": verification_digest,
            "workspace_fingerprint": assembled_candidate[
                "workspace_fingerprint"
            ],
        }
    )
    return result


def _apply_dependency_candidate_delta(
    workspace: Path,
    dependency: AggregateSnapshot,
) -> None:
    candidate_digest = str(dependency.payload.get("candidate_digest") or "")
    candidate_base = str(dependency.payload.get("base_sha") or "")
    if not candidate_base:
        raise ValueError(
            f"accepted dependency has no Candidate baseline: {dependency.aggregate_id}"
        )
    if not _git_is_ancestor(workspace, candidate_base, candidate_digest):
        raise ValueError(
            f"accepted dependency Candidate is not based on its declared baseline: {dependency.aggregate_id}"
        )
    commits = [
        line.strip()
        for line in _git(
            workspace,
            "rev-list",
            "--reverse",
            "--topo-order",
            candidate_digest,
            f"^{candidate_base}",
        ).splitlines()
        if line.strip()
    ]
    if not commits:
        raise ValueError(
            f"accepted dependency Candidate contains no delta: {dependency.aggregate_id}"
        )
    patch_equivalence = {
        parts[1]: parts[0]
        for line in _git(
            workspace,
            "cherry",
            "HEAD",
            candidate_digest,
            candidate_base,
        ).splitlines()
        if len(parts := line.split()) >= 2 and parts[0] in {"+", "-"}
    }
    for commit in commits:
        if _git_is_ancestor(workspace, commit, "HEAD"):
            continue
        if patch_equivalence.get(commit) == "-":
            continue
        try:
            _git(workspace, "cherry-pick", commit)
        except subprocess.CalledProcessError as exc:
            if _git_ref_exists(workspace, "CHERRY_PICK_HEAD"):
                raise DependencyIntegrationConflict(
                    "accepted dependency Candidate conflicts with the current "
                    f"workspace: {dependency.aggregate_id}"
                ) from exc
            raise


def _recorded_applied_dependency_digests(
    node: AggregateSnapshot,
    *,
    workspace: Path,
    starting_head: str,
) -> dict[str, str]:
    """Return durable dependency applications inherited by the current Candidate.

    A repair Candidate starts from the previous assembled verification checkpoint.
    The source dependency SHAs are not ancestors after cherry-pick, so replay must
    use the Manager-owned dependency provenance rather than raw SHA ancestry.
    """

    verification_base = str(node.payload.get("verification_base_sha") or "")
    if (
        not starting_head
        or not verification_base
        or not _git_is_ancestor(workspace, verification_base, starting_head)
    ):
        return {}
    accepted = {
        str(item)
        for item in list(
            node.payload.get("accepted_dependency_candidate_digests") or []
        )
        if str(item)
    }
    result: dict[str, str] = {}
    for dependency_id, raw_output in dict(
        node.payload.get("dependency_outputs") or {}
    ).items():
        output = dict(raw_output) if isinstance(raw_output, Mapping) else {}
        digest = str(output.get("candidate_digest") or "")
        if digest and (not accepted or digest in accepted):
            result[str(dependency_id)] = digest
    return result
