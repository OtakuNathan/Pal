from __future__ import annotations
from pal.bunshin.v2.semantic_orchestration.workflow_requests import WorkflowRequests
from pal.bunshin.v2.semantic_orchestration.review_results import _ref_from_mapping
from pal.bunshin.v2.semantic_orchestration.workspace_safety import _git_output
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping
from pal.bunshin.v2.adapters import ARTIFACT_BUNDLE_ADAPTER, SOFTWARE_GIT_ADAPTER, ArtifactBundleAdapter
from pal.bunshin.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.bunshin.v2.delivery import DeliveryService
from pal.bunshin.v2.workflow_runtime import WorkflowCoordinator
from pal.bunshin.v2.repository import BunshinV2Repository
from pal.bunshin.v2.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.v2.semantic_orchestration.workflow_facts import WorkflowFacts


@dataclass
class FinalDelivery:
    effect_reads: EffectReads
    workflow_facts: WorkflowFacts
    artifacts: ContentAddressedArtifactStore
    repository: BunshinV2Repository
    requests: WorkflowRequests
    runtime_root: Path

    async def publish_final_deliverable(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        epoch = self.effect_reads.effect_snapshot(effect)
        snapshots = self.repository.queries.list_workflow_snapshots(epoch.workflow_id)
        epoch_nodes = [
            item
            for item in snapshots
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
        ]
        sink = next(
            (
                item for item in epoch_nodes
                if bool(item.payload.get("graph_sink"))
                and item.state == "ACCEPTED"
            ),
            None,
        )
        if sink is None or any(item.state != "ACCEPTED" for item in epoch_nodes):
            raise ValueError(
                "final delivery requires the declared sink and every executable node ACCEPTED"
            )
        published_sink_ref = WorkflowCoordinator(
            self.repository
        ).published_sink_ref(workflow_id=epoch.workflow_id)
        if (
            str(dict(sink.payload.get("candidate_ref") or {}).get("sha256") or "")
            != published_sink_ref
        ):
            raise ValueError(
                "delivery sink Candidate disagrees with GraphExecution publication"
            )
        verification_ref = _ref_from_mapping(
            sink.payload.get("verification_artifact_ref")
        )
        adapter = self.workflow_facts.execution_adapter(sink)
        if adapter == SOFTWARE_GIT_ADAPTER:
            repository = Path(str(sink.payload.get("workspace_path") or ""))
            deliverable_ref = self.publish_verified_git_delivery(
                epoch=epoch,
                delivery_node=sink,
                repository=repository,
                verification_ref=verification_ref,
            )
        elif adapter == ARTIFACT_BUNDLE_ADAPTER:
            deliverable_ref = ArtifactBundleAdapter(
                self.runtime_root,
                self.artifacts,
            ).publish_deliverable(
                workflow_id=epoch.workflow_id,
                candidate_ref=dict(sink.payload.get("candidate_ref") or {}),
                verification_ref=verification_ref,
            )
        else:
            raise ValueError(f"unsupported publisher adapter: {adapter}")
        current = self.repository.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch.aggregate_id)
        self.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="FINAL_DELIVERABLE_PUBLISHED",
                workflow_id=epoch.workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id=epoch.aggregate_id,
                actor="bunshin-v2-manager",
                expected_version=current.version,
                idempotency_key=f"publish:{deliverable_ref.sha256}",
                payload={"published_deliverable_ref": deliverable_ref.to_dict()},
            )
        )
        return {"result_artifact_ref": deliverable_ref.to_dict()}

    def publish_verified_git_delivery(
        self,
        *,
        epoch: AggregateSnapshot,
        delivery_node: AggregateSnapshot,
        repository: Path,
        verification_ref: ArtifactRef,
    ) -> ArtifactRef:
        if not repository.is_dir():
            raise ValueError("delivery requires the accepted sink module worktree")
        commit_sha = _git_output(repository, "rev-parse", "HEAD")
        manifest_ref = _ref_from_mapping(epoch.payload.get("architecture_manifest_ref"))
        manifest = dict(self.artifacts.read_json(manifest_ref))
        snapshot_value = manifest.get("workspace_snapshot_ref")
        source_snapshot: dict[str, Any]
        if isinstance(snapshot_value, Mapping) and snapshot_value.get("sha256"):
            source_snapshot = dict(
                self.artifacts.read_json(_ref_from_mapping(snapshot_value))
            )
        else:
            raise ValueError("patch delivery requires workspace snapshot metadata")
        workflow = self.repository.snapshots.read_snapshot(
            AggregateType.WORKFLOW, epoch.workflow_id
        )
        if workflow is None:
            raise ValueError("delivery workflow is unavailable")
        request = self.requests.read(workflow)
        repository_layout = dict(manifest.get("repository_layout") or {})
        workflow_key = str(
            delivery_node.payload.get("workflow_key")
            or repository_layout.get("workflow_key")
            or epoch.workflow_id
        )
        task_title = str(
            request.get("title")
            or request.get("goal")
            or request.get("objective")
            or "Bunshin delivery"
        )
        return DeliveryService(
            self.runtime_root,
            self.artifacts,
        ).publish(
            workflow_id=epoch.workflow_id,
            workflow_key=workflow_key,
            task_title=task_title,
            repository=repository,
            commit_sha=commit_sha,
            source_snapshot=source_snapshot,
            verification_ref=verification_ref,
        )
