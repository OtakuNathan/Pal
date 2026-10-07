from __future__ import annotations
from pal.bunshin.semantic_orchestration.workflow_requests import WorkflowRequests
from pal.bunshin.semantic_orchestration.verification_workspace import _verification_scratch_paths
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
import base64
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import AggregateSnapshot, AggregateType
from pal.bunshin.paths import resolve_project_git_layout
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.replan import ARCHITECTURE_FINDING_BATCH_VIEW_ARTIFACT, architecture_finding_semantic_view


@dataclass
class RoleReports:
    artifacts: ContentAddressedArtifactStore
    repository: BunshinV2Repository
    requests: WorkflowRequests
    runtime_root: Path

    def record_role_turn(
        self,
        *,
        terminal: Mapping[str, Any],
        **kwargs: Any,
    ) -> None:
        if bool(dict(terminal.get("payload") or {}).get("durable_receipt_replay")):
            return
        self.repository.role_events.record_role_turn(**kwargs)

    def architecture_artifact_with_runtime_layout(
        self,
        revision: AggregateSnapshot,
        artifact: Mapping[str, Any],
    ) -> dict[str, Any]:
        result = dict(artifact)
        if dict(result.get("repository_layout") or {}):
            return result
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
        if workflow is None:
            return result
        request = self.requests.read(workflow)
        layout = resolve_project_git_layout(
            self.runtime_root,
            workspace=dict(request.get("workspace") or {}),
            workflow_id=revision.workflow_id,
            workflow_name=str(
                request.get("workflow_name") or request.get("goal") or revision.workflow_id
            ),
        )
        result["repository_layout"] = layout.to_artifact_dict()
        return result

    def publish_architecture_revision_scope(
        self,
        revision: AggregateSnapshot,
        *,
        base_manifest_ref: ArtifactRef,
        finding_value: Any,
    ) -> ArtifactRef:
        """Publish a small semantic repair view while retaining identities internally."""

        finding_ref = _ref_from_mapping(finding_value) if finding_value else None
        finding_payload = self.artifacts.read_json(finding_ref) if finding_ref else {}
        base_artifact = dict(
            self.artifacts.read_json(base_manifest_ref)
        )
        if not isinstance(base_artifact.get("contract"), Mapping):
            raise ValueError(
                "architecture revision baseline is not a ContractArtifact"
            )
        scope = {
            "schema_version": "1",
            "findings": [dict(item) for item in list(dict(finding_payload).get("findings") or [])],
            "instruction": (
                "Revise the preseeded architect.yaml in place. Resolve every "
                "finding while preserving unrelated accepted semantics."
            ),
        }
        child_refs = [(base_manifest_ref.sha256, "base_manifest")]
        if finding_ref is not None:
            child_refs.append((finding_ref.sha256, "review_finding"))
        return self.artifacts.put_json(
            scope,
            artifact_type="ArchitectureRevisionScopeArtifact",
            provenance={"architecture_revision_id": revision.aggregate_id},
            child_refs=tuple(child_refs),
        )

    def publish_architecture_finding_view(
        self,
        finding_value: Any,
        *,
        audience: str,
    ) -> ArtifactRef:
        finding_ref = _ref_from_mapping(finding_value)
        payload = dict(self.artifacts.read_json(finding_ref))
        return self.artifacts.put_json(
            architecture_finding_semantic_view(payload),
            artifact_type=ARCHITECTURE_FINDING_BATCH_VIEW_ARTIFACT,
            provenance={"owner": "manager", "audience": audience},
            child_refs=((finding_ref.sha256, "architecture_findings"),),
        )

    def write_node_journal(
        self,
        node: AggregateSnapshot,
        *,
        owner_id: str,
        lease_resource: str,
        fencing_token: int,
        updates: Mapping[str, Any],
    ) -> None:
        current = self.repository.role_events.read_node_journal(node.aggregate_id) or {}
        journal = dict(current.get("journal") or {})
        journal.update(dict(updates))
        self.repository.role_events.update_node_journal(
            node_run_id=node.aggregate_id,
            workflow_id=node.workflow_id,
            lease_resource_key=lease_resource,
            owner_id=owner_id,
            fencing_token=fencing_token,
            expected_generation=int(current.get("generation") or 0),
            journal=journal,
        )

    def publish_verification_evidence(
        self,
        *,
        review_scratch: Path,
        candidate_identity: str,
    ) -> ArtifactRef:
        files: list[dict[str, Any]] = []
        total_bytes = 0
        for root in (review_scratch,):
            for path in sorted(item for item in root.rglob("*") if item.is_file() and not item.is_symlink()):
                raw = path.read_bytes()
                total_bytes += len(raw)
                if total_bytes > 5 * 1024 * 1024:
                    raise ValueError("verification scratch artifact exceeds 5 MiB")
                files.append(
                    {
                        "path": str(path.relative_to(root)),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "content_base64": base64.b64encode(raw).decode("ascii"),
                    }
                )
        return self.artifacts.put_json(
            {
                "schema_version": "2",
                "candidate_identity": candidate_identity,
                "changed_paths": _verification_scratch_paths(review_scratch),
                "files": files,
            },
            artifact_type="VerificationWorkspaceEvidenceArtifact",
        )
