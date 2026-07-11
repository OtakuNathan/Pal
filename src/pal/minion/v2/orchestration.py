from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from pal.minion.v2.artifacts import ArtifactRef
from pal.minion.v2.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.minion.v2.execution import DagScheduler, ExecutionCompiler
from pal.minion.v2.service import MinionV2WorkflowService, workflow_request_from_snapshot
from pal.minion.v2.verification import DefectPropagationService


class SemanticEffectPort(Protocol):
    async def execute_semantic_effect(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


@dataclass
class RejectingSemanticEffectPort:
    async def execute_semantic_effect(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError(f"no V2 semantic worker is configured for effect {effect.get('effect_type')}")


MECHANICAL_EFFECT_TYPES = frozenset({
    "submit_action",
    "route_workflow",
    "create_architecture_revision",
    "schedule_ready_nodes",
    "notify_node_accepted",
    "publish_accepted_memory_candidate",
    "queue_integration_node",
    "propagate_pause",
    "propagate_resume",
    "propagate_cancel",
    "pause_epoch_nodes",
    "resume_epoch_nodes",
    "cancel_epoch_nodes",
    "reopen_dependency_and_stale_descendants",
    "freeze_epoch_and_create_revision",
    "submit_workflow_completion",
    "submit_standalone_completion",
    "start_review_repair_execution",
    "submit_workflow_rejection",
    "reconcile_execution_epoch",
    "reconcile_workflow",
})


@dataclass
class MinionV2OutboxProcessor:
    service: MinionV2WorkflowService
    semantic_effects: SemanticEffectPort = field(default_factory=RejectingSemanticEffectPort)
    worker_id: str = "minion-v2-outbox"
    effect_lease_seconds: int = 120
    max_parallel_nodes: int = 5
    _background_tasks: set[asyncio.Task[str]] = field(default_factory=set, init=False, repr=False)

    @property
    def repository(self):
        return self.service.repository

    async def process_once(self, *, limit: int = 10) -> dict[str, Any]:
        claimed = self.repository.claim_outbox(
            self.worker_id,
            limit=max(1, int(limit)),
            lease_seconds=self.effect_lease_seconds,
        )
        if not claimed:
            return {"status": "idle", "claimed": 0, "completed": 0, "failed": 0}
        results = await asyncio.gather(*(self._process_effect(effect) for effect in claimed))
        return {
            "status": "processed",
            "claimed": len(claimed),
            "completed": sum(item == "completed" for item in results),
            "failed": sum(item != "completed" for item in results),
        }

    def start_available(self, *, max_concurrency: int) -> int:
        self._background_tasks = {task for task in self._background_tasks if not task.done()}
        available = max(0, int(max_concurrency) - len(self._background_tasks))
        if available == 0:
            return 0
        claimed = self.repository.claim_outbox(
            self.worker_id,
            limit=available,
            lease_seconds=self.effect_lease_seconds,
        )
        for effect in claimed:
            task = asyncio.create_task(
                self._process_effect(effect),
                name=f"minion-v2-effect-{str(effect['effect_id'])[-12:]}",
            )
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        return len(claimed)

    async def stop_background(self) -> None:
        tasks = tuple(self._background_tasks)
        self._background_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _process_effect(self, effect: Mapping[str, Any]) -> str:
        effect_id = str(effect["effect_id"])
        heartbeat = asyncio.create_task(self._heartbeat(effect_id))
        try:
            if str(effect.get("effect_type") or "") in MECHANICAL_EFFECT_TYPES:
                result = await self._execute_mechanical(effect)
            else:
                result = await self.semantic_effects.execute_semantic_effect(effect)
            self._reconcile_control_requests(str(effect.get("workflow_id") or ""))
            result_ref = dict(result.get("result_artifact_ref") or {}) if isinstance(result, Mapping) else {}
            provider_request_id = str(result.get("provider_request_id") or "") if isinstance(result, Mapping) else ""
            self.repository.complete_outbox_effect(
                effect_id,
                worker_id=self.worker_id,
                provider_request_id=provider_request_id,
                result_artifact_ref=result_ref,
            )
            return "completed"
        except Exception as exc:
            snapshot = self._effect_snapshot(effect)
            if snapshot.state in {"PAUSED", "CANCEL_REQUESTED", "CANCELLED", "STALE"}:
                self.repository.complete_outbox_effect(effect_id, worker_id=self.worker_id)
                return "completed"
            retry_status = self.repository.retry_outbox_effect(
                effect_id,
                worker_id=self.worker_id,
                error=f"{exc.__class__.__name__}: {exc}",
                retry_after_seconds=5,
            )
            if retry_status == "failed":
                self._triage_failed_effect(effect, exc)
            return "failed"
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    async def _heartbeat(self, effect_id: str) -> None:
        interval = max(1.0, self.effect_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            self.repository.renew_outbox_claim(
                effect_id,
                worker_id=self.worker_id,
                lease_seconds=self.effect_lease_seconds,
            )

    async def _execute_mechanical(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        effect_type = str(effect.get("effect_type") or "")
        payload = dict(effect.get("payload") or {})
        if effect_type == "submit_action":
            return self._submit_effect_action(effect, payload)
        if effect_type == "route_workflow":
            return self._route_workflow(effect)
        if effect_type == "create_architecture_revision":
            return self._create_revision(effect)
        if effect_type in {"schedule_ready_nodes", "queue_integration_node"}:
            snapshot = self._effect_snapshot(effect)
            epoch_id = snapshot.aggregate_id if snapshot.aggregate_type == AggregateType.EXECUTION_EPOCH else str(snapshot.payload.get("epoch_id") or "")
            DagScheduler(self.repository).schedule_ready_nodes(
                workflow_id=snapshot.workflow_id,
                epoch_id=epoch_id,
                max_new_nodes=self.max_parallel_nodes,
            )
            return {}
        if effect_type == "notify_node_accepted":
            return self._node_accepted(effect)
        if effect_type == "publish_accepted_memory_candidate":
            return self._publish_accepted_memory_candidate(effect)
        if effect_type in {"propagate_pause", "propagate_resume", "propagate_cancel"}:
            return self._propagate_workflow_control(effect_type, effect)
        if effect_type in {"pause_epoch_nodes", "resume_epoch_nodes", "cancel_epoch_nodes"}:
            return self._control_epoch_nodes(effect_type, effect)
        if effect_type == "reopen_dependency_and_stale_descendants":
            node = self._effect_snapshot(effect)
            repair_ref = ArtifactRef.from_mapping(dict(node.payload.get("repair_bill_ref") or {}))
            DefectPropagationService(self.repository).propagate_dependency_defect(
                workflow_id=node.workflow_id,
                epoch_id=str(node.payload.get("epoch_id") or ""),
                dependency_node_id=str(node.payload.get("dependency_node_id") or ""),
                repair_bill_ref=repair_ref,
            )
            return {}
        if effect_type == "freeze_epoch_and_create_revision":
            return self._freeze_epoch_and_replan(effect)
        if effect_type == "submit_workflow_completion":
            epoch = self._effect_snapshot(effect)
            workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, epoch.workflow_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="MARK_COMPLETED",
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=epoch.workflow_id,
                    actor="minion-v2-manager",
                    expected_version=workflow.version,
                    idempotency_key=f"effect:{effect['effect_key']}:workflow-complete",
                    payload={"result_artifact_ref": epoch.payload.get("published_deliverable_ref")},
                )
            )
            return {}
        if effect_type == "submit_standalone_completion":
            review = self._effect_snapshot(effect)
            workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, review.workflow_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="MARK_COMPLETED",
                    workflow_id=review.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=review.workflow_id,
                    actor="minion-v2-manager",
                    expected_version=workflow.version,
                    idempotency_key=f"effect:{effect['effect_key']}:workflow-complete",
                    payload={"result_artifact_ref": review.payload.get("verification_artifact_ref")},
                )
            )
            return {}
        if effect_type == "start_review_repair_execution":
            review = self._effect_snapshot(effect)
            return self._compile_execution(
                workflow_id=review.workflow_id,
                manifest_ref=dict(review.payload.get("architecture_manifest_ref") or {}),
                causation_key=str(effect["effect_key"]),
            )
        if effect_type == "submit_workflow_rejection":
            revision = self._effect_snapshot(effect)
            workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
            if workflow is not None and workflow.state == "ACTIVE":
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="REJECT_WORKFLOW",
                        workflow_id=workflow.workflow_id,
                        aggregate_type=AggregateType.WORKFLOW,
                        aggregate_id=workflow.aggregate_id,
                        actor="minion-v2-manager",
                        expected_version=workflow.version,
                        idempotency_key=f"effect:{effect['effect_key']}:reject-workflow",
                    )
                )
            return {}
        if effect_type == "reconcile_execution_epoch":
            return await self._reconcile_execution_epoch(effect)
        if effect_type == "reconcile_workflow":
            self._reconcile_control_requests(str(effect.get("workflow_id") or ""))
            return {}
        raise ValueError(f"unsupported mechanical V2 effect: {effect_type}")

    async def _reconcile_execution_epoch(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        epoch = self._effect_snapshot(effect)
        if epoch.state == "RUNNING":
            DagScheduler(self.repository).schedule_ready_nodes(
                workflow_id=epoch.workflow_id,
                epoch_id=epoch.aggregate_id,
                max_new_nodes=self.max_parallel_nodes,
            )
            return {}
        if epoch.state == "FINALIZING":
            return await self.semantic_effects.execute_semantic_effect(
                {**dict(effect), "effect_type": "publish_final_deliverable"}
            )
        if epoch.state == "STARTING":
            nodes = [
                item
                for item in self.repository.list_workflow_snapshots(epoch.workflow_id)
                if item.aggregate_type == AggregateType.DAG_NODE_RUN
                and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
            ]
            integration = [item for item in nodes if str(item.payload.get("node_kind") or "") == "integration"]
            if not nodes or len(integration) != 1:
                raise RuntimeError("execution compilation is incomplete and requires operator triage")
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="NODES_COMPILED",
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch.aggregate_id,
                    actor="minion-v2-recovery",
                    expected_version=epoch.version,
                    idempotency_key=f"effect:{effect['effect_key']}:nodes-compiled",
                    payload={
                        "node_ids": [item.aggregate_id for item in nodes],
                        "integration_node_id": integration[0].aggregate_id,
                    },
                )
            )
        return {}

    def _submit_effect_action(self, effect: Mapping[str, Any], payload: Mapping[str, Any]) -> Mapping[str, Any]:
        action_type = str(payload.get("action_type") or "")
        snapshot = self._effect_snapshot(effect)
        if action_type == "START_EXECUTION" and snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
            manifest_ref = dict(snapshot.payload.get("architecture_manifest_ref") or {})
            if not manifest_ref:
                raise ValueError("accepted architecture revision has no manifest")
            return self._compile_execution(
                workflow_id=snapshot.workflow_id,
                manifest_ref=manifest_ref,
                causation_key=str(effect["effect_key"]),
                reuse_from_epoch_id=str(snapshot.payload.get("source_execution_epoch_id") or ""),
            )
        self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=snapshot.workflow_id,
                aggregate_type=snapshot.aggregate_type,
                aggregate_id=snapshot.aggregate_id,
                actor="minion-v2-outbox",
                expected_version=snapshot.version,
                idempotency_key=f"effect:{effect['effect_key']}",
                causation_id=str(effect["event_id"]),
                payload=dict(payload.get("action_payload") or {}),
            )
        )
        return {}

    def _route_workflow(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        workflow = self._effect_snapshot(effect)
        request = workflow_request_from_snapshot(self.service, workflow)
        operation = str(request.get("operation") or "new_requirement")
        if operation == "new_requirement":
            revision_id = _derived_id("arch", str(effect["effect_key"]))
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="CREATE_ARCHITECTURE_REVISION",
                    workflow_id=workflow.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision_id,
                    actor="minion-v2-router",
                    expected_version=0,
                    idempotency_key=f"effect:{effect['effect_key']}:revision",
                    payload={
                        "request_ref": dict(workflow.payload["request_ref"]),
                        "research_mode": str(request.get("research_mode") or "local_only"),
                        "revision_number": 1,
                    },
                )
            )
            self._link_workflow(workflow.workflow_id, "LINK_ARCHITECTURE_REVISION", {"architecture_revision_id": revision_id}, str(effect["effect_key"]))
            return {}
        artifact_ref = dict(request.get("input_artifact_ref") or {})
        if operation == "execute_trusted":
            return self._compile_execution(workflow_id=workflow.workflow_id, manifest_ref=artifact_ref, causation_key=str(effect["effect_key"]))
        if operation == "review_then_execute":
            revision_id = _derived_id("arch", str(effect["effect_key"]))
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="IMPORT_ARCHITECTURE_REVISION",
                    workflow_id=workflow.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision_id,
                    actor="minion-v2-router",
                    expected_version=0,
                    idempotency_key=f"effect:{effect['effect_key']}:import",
                    payload={"architecture_manifest_ref": artifact_ref, "revision_number": 1},
                )
            )
            self._link_workflow(workflow.workflow_id, "LINK_ARCHITECTURE_REVISION", {"architecture_revision_id": revision_id}, str(effect["effect_key"]))
            return {}
        review_id = _derived_id("review", str(effect["effect_key"]))
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_STANDALONE_REVIEW",
                workflow_id=workflow.workflow_id,
                aggregate_type=AggregateType.STANDALONE_REVIEW,
                aggregate_id=review_id,
                actor="minion-v2-router",
                expected_version=0,
                idempotency_key=f"effect:{effect['effect_key']}:review",
                payload={"review_request_ref": artifact_ref, "review_mode": operation},
            )
        )
        review = self.repository.read_snapshot(AggregateType.STANDALONE_REVIEW, review_id)
        self.repository.dispatch(
            ActionEnvelope(
                action_type="QUEUE_REVIEW",
                workflow_id=workflow.workflow_id,
                aggregate_type=AggregateType.STANDALONE_REVIEW,
                aggregate_id=review_id,
                actor="minion-v2-router",
                expected_version=review.version,
                idempotency_key=f"effect:{effect['effect_key']}:queue-review",
            )
        )
        self._link_workflow(workflow.workflow_id, "LINK_STANDALONE_REVIEW", {"standalone_review_id": review_id}, str(effect["effect_key"]))
        return {}

    def _create_revision(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        previous = self._effect_snapshot(effect)
        revision_id = _derived_id("arch", str(effect["effect_key"]))
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id=previous.workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="minion-v2-router",
                expected_version=0,
                idempotency_key=f"effect:{effect['effect_key']}:revision",
                payload={
                    "request_ref": previous.payload.get("request_ref"),
                    "parent_revision_id": previous.aggregate_id,
                    "revision_number": int(previous.payload.get("revision_number") or 1) + 1,
                    "edit_instruction_ref": previous.payload.get("edit_instruction_ref"),
                    "base_architecture_manifest_ref": previous.payload.get("architecture_manifest_ref"),
                    "research_mode": previous.payload.get("research_mode", "local_only"),
                },
            )
        )
        self._link_workflow(previous.workflow_id, "LINK_ARCHITECTURE_REVISION", {"architecture_revision_id": revision_id}, str(effect["effect_key"]))
        return {}

    def _freeze_epoch_and_replan(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        source = self._effect_snapshot(effect)
        epoch_id = str(source.payload.get("epoch_id") or "")
        epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
        finding_ref = source.payload.get("repair_bill_ref") or source.payload.get("finding_artifact_ref")
        if epoch is not None and epoch.state == "RUNNING":
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="REPLAN_REQUESTED",
                    workflow_id=source.workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch_id,
                    actor="minion-v2-manager",
                    expected_version=epoch.version,
                    idempotency_key=f"effect:{effect['effect_key']}:freeze",
                    payload={"finding_artifact_ref": finding_ref},
                )
            )
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, source.workflow_id)
        revision_id = _derived_id("arch", str(effect["effect_key"]))
        self.repository.dispatch(
            ActionEnvelope(
                action_type="CREATE_ARCHITECTURE_REVISION",
                workflow_id=source.workflow_id,
                aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                aggregate_id=revision_id,
                actor="minion-v2-manager",
                expected_version=0,
                idempotency_key=f"effect:{effect['effect_key']}:replan",
                payload={
                    "request_ref": workflow.payload.get("request_ref"),
                    "base_architecture_manifest_ref": source.payload.get("architecture_manifest_ref"),
                    "replan_finding_ref": finding_ref,
                    "source_execution_epoch_id": epoch_id,
                    "research_mode": "local_only",
                    "revision_number": 1,
                },
            )
        )
        self._link_workflow(source.workflow_id, "LINK_ARCHITECTURE_REVISION", {"architecture_revision_id": revision_id}, str(effect["effect_key"]))
        return {}

    def _compile_execution(
        self,
        *,
        workflow_id: str,
        manifest_ref: Mapping[str, Any],
        causation_key: str,
        reuse_from_epoch_id: str = "",
    ) -> Mapping[str, Any]:
        record = self.repository.read_artifact_record(str(manifest_ref.get("sha256") or ""))
        if record is None:
            raise ValueError("architecture manifest is missing")
        ref = ArtifactRef(
            sha256=str(record["sha256"]),
            artifact_type=str(record["artifact_type"]),
            schema_version=str(record["schema_version"]),
            media_type=str(record["media_type"]),
            byte_size=int(record["byte_size"]),
            durable=True,
        )
        epoch_id = _derived_id("epoch", causation_key)
        compilation = ExecutionCompiler(self.repository, self.service.architecture).compile_epoch(
            workflow_id=workflow_id,
            epoch_id=epoch_id,
            manifest_ref=ref,
            reuse_from_epoch_id=reuse_from_epoch_id,
        )
        self._link_workflow(workflow_id, "LINK_EXECUTION_EPOCH", {"execution_epoch_id": epoch_id}, causation_key)
        return {"epoch_id": epoch_id, "node_ids": list(compilation.node_run_ids)}

    def _node_accepted(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        epoch_id = str(node.payload.get("epoch_id") or "")
        if str(node.payload.get("node_kind") or "") == "integration":
            epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
            if epoch is not None and epoch.state == "RUNNING":
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="INTEGRATION_ACCEPTED",
                        workflow_id=node.workflow_id,
                        aggregate_type=AggregateType.EXECUTION_EPOCH,
                        aggregate_id=epoch_id,
                        actor="minion-v2-outbox",
                        expected_version=epoch.version,
                        idempotency_key=f"effect:{effect['effect_key']}:integration",
                        payload={"integration_candidate_ref": node.payload.get("candidate_ref")},
                    )
                )
            return {}
        DagScheduler(self.repository).schedule_ready_nodes(
            workflow_id=node.workflow_id,
            epoch_id=epoch_id,
            max_new_nodes=self.max_parallel_nodes,
        )
        return {}

    def _publish_accepted_memory_candidate(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        if node.state != "ACCEPTED" or str(node.payload.get("node_kind") or "") == "integration":
            return {}
        if node.payload.get("memory_candidate_ref"):
            return {"result_artifact_ref": dict(node.payload["memory_candidate_ref"])}
        epoch_id = str(node.payload.get("epoch_id") or "")
        memory_candidate_ref = self.service.artifacts.put_json(
            {
                "schema_version": "1",
                "candidate_kind": "accepted_unit_learning",
                "review_status": "pending_human_review",
                "workflow_id": node.workflow_id,
                "epoch_id": epoch_id,
                "node_run_id": node.aggregate_id,
                "unit_id": str(node.payload.get("unit_id") or ""),
                "unit_contract_ref": dict(node.payload.get("unit_contract_ref") or {}),
                "candidate_ref": dict(node.payload.get("candidate_ref") or {}),
                "verification_artifact_ref": dict(node.payload.get("verification_artifact_ref") or {}),
                "historical_repair_bill_refs": [
                    dict(item)
                    for item in list(node.payload.get("historical_repair_bill_refs") or [])
                    if isinstance(item, Mapping)
                ],
                "architecture_manifest_ref": dict(node.payload.get("architecture_manifest_ref") or {}),
                "publication_rule": "Pal or the user must review this candidate before long-term memory publication.",
                "validity_rule": "Publish only while this node remains ACCEPTED in the active execution epoch; STALE or superseded nodes invalidate the candidate.",
            },
            artifact_type="AcceptedModuleMemoryCandidateArtifact",
            child_refs=tuple(
                [
                    (str(ref.get("sha256")), relation)
                    for relation, ref in (
                        ("unit_contract", dict(node.payload.get("unit_contract_ref") or {})),
                        ("candidate", dict(node.payload.get("candidate_ref") or {})),
                        ("verification", dict(node.payload.get("verification_artifact_ref") or {})),
                        ("architecture_manifest", dict(node.payload.get("architecture_manifest_ref") or {})),
                    )
                    if ref.get("sha256")
                ]
                + [
                    (str(ref.get("sha256")), "repair_bill")
                    for ref in list(node.payload.get("historical_repair_bill_refs") or [])
                    if isinstance(ref, Mapping) and ref.get("sha256")
                ]
            ),
            provenance={"workflow_id": node.workflow_id, "node_run_id": node.aggregate_id},
        )
        current = self.repository.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
        if current is not None and not current.payload.get("memory_candidate_ref"):
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="MEMORY_CANDIDATE_PUBLISHED",
                    workflow_id=node.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="minion-v2-outbox",
                    expected_version=current.version,
                    idempotency_key=f"effect:{effect['effect_key']}:memory-candidate",
                    payload={"memory_candidate_ref": memory_candidate_ref.to_dict()},
                )
            )
        return {"result_artifact_ref": memory_candidate_ref.to_dict()}

    def _propagate_workflow_control(self, effect_type: str, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        workflow = self._effect_snapshot(effect)
        snapshots = self.repository.list_workflow_snapshots(workflow.workflow_id)
        active = [
            item
            for item in snapshots
            if item.aggregate_type != AggregateType.WORKFLOW
            and item.state not in {"ACCEPTED", "REJECTED", "COMPLETED", "CANCELLED", "STALE"}
        ]
        action_type = {
            "propagate_pause": "REQUEST_PAUSE",
            "propagate_resume": "RESUME",
            "propagate_cancel": "REQUEST_CANCEL",
        }[effect_type]
        for child in active:
            if action_type not in self.repository.engine.legal_actions(child.aggregate_type, child.state):
                continue
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=workflow.workflow_id,
                    aggregate_type=child.aggregate_type,
                    aggregate_id=child.aggregate_id,
                    actor="minion-v2-control",
                    expected_version=child.version,
                    idempotency_key=f"effect:{effect['effect_key']}:{child.aggregate_id}",
                )
            )
        if not active:
            confirmation = "CHILDREN_PAUSED" if effect_type == "propagate_pause" else "CHILDREN_CANCELLED"
            if effect_type != "propagate_resume":
                latest = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow.workflow_id)
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type=confirmation,
                        workflow_id=workflow.workflow_id,
                        aggregate_type=AggregateType.WORKFLOW,
                        aggregate_id=workflow.workflow_id,
                        actor="minion-v2-control",
                        expected_version=latest.version,
                        idempotency_key=f"effect:{effect['effect_key']}:confirm",
                    )
                )
        return {}

    def _confirm_simple_control(self, effect_type: str, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        snapshot = self._effect_snapshot(effect)
        if effect_type == "pause_aggregate_work":
            action_type = "PAUSE_CONFIRMED"
        elif effect_type == "cancel_aggregate_work":
            action_type = "CANCEL_CONFIRMED"
        else:
            return {}
        self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=snapshot.workflow_id,
                aggregate_type=snapshot.aggregate_type,
                aggregate_id=snapshot.aggregate_id,
                actor="minion-v2-control",
                expected_version=snapshot.version,
                idempotency_key=f"effect:{effect['effect_key']}:confirm",
            )
        )
        return {}

    def _control_epoch_nodes(self, effect_type: str, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        epoch = self._effect_snapshot(effect)
        snapshots = self.repository.list_workflow_snapshots(epoch.workflow_id)
        nodes = [
            item
            for item in snapshots
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
        ]
        requested_action = {
            "pause_epoch_nodes": "REQUEST_PAUSE",
            "resume_epoch_nodes": "RESUME",
            "cancel_epoch_nodes": "REQUEST_CANCEL",
        }[effect_type]
        for node in nodes:
            if requested_action not in self.repository.engine.legal_actions(AggregateType.DAG_NODE_RUN, node.state):
                continue
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=requested_action,
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="minion-v2-control",
                    expected_version=node.version,
                    idempotency_key=f"effect:{effect['effect_key']}:{node.aggregate_id}",
                )
            )
        return {}

    def _link_workflow(
        self,
        workflow_id: str,
        action_type: str,
        payload: Mapping[str, Any],
        causation_key: str,
    ) -> None:
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            raise ValueError("workflow does not exist")
        self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="minion-v2-router",
                expected_version=workflow.version,
                idempotency_key=f"link:{causation_key}:{action_type}",
                payload=dict(payload),
            )
        )

    def _effect_snapshot(self, effect: Mapping[str, Any]) -> AggregateSnapshot:
        aggregate_type = AggregateType(str(effect["aggregate_type"]))
        snapshot = self.repository.read_snapshot(aggregate_type, str(effect["aggregate_id"]))
        if snapshot is None:
            raise ValueError("effect aggregate no longer exists")
        return snapshot

    def _triage_failed_effect(self, effect: Mapping[str, Any], exc: Exception) -> None:
        snapshot = self._effect_snapshot(effect)
        if "ENTER_TRIAGE" not in self.repository.engine.legal_actions(snapshot.aggregate_type, snapshot.state):
            return
        failure_ref = self.service.artifacts.put_json(
            {
                "effect_id": effect.get("effect_id"),
                "effect_type": effect.get("effect_type"),
                "attempt_count": effect.get("attempt_count"),
                "error": f"{exc.__class__.__name__}: {exc}",
            },
            artifact_type="EffectFailureArtifact",
        )
        self.repository.dispatch(
            ActionEnvelope(
                action_type="ENTER_TRIAGE",
                workflow_id=snapshot.workflow_id,
                aggregate_type=snapshot.aggregate_type,
                aggregate_id=snapshot.aggregate_id,
                actor="minion-v2-outbox",
                expected_version=snapshot.version,
                idempotency_key=f"effect-failed:{effect['effect_key']}",
                payload={
                    "failure_artifact_ref": failure_ref.to_dict(),
                    "blocker": {
                        "kind": "effect_failed",
                        "effect_type": effect.get("effect_type"),
                        "failure_artifact_ref": failure_ref.to_dict(),
                    },
                },
            )
        )

    def _reconcile_control_requests(self, workflow_id: str) -> None:
        if not workflow_id:
            return
        snapshots = list(self.repository.list_workflow_snapshots(workflow_id))
        for epoch in [item for item in snapshots if item.aggregate_type == AggregateType.EXECUTION_EPOCH]:
            nodes = [
                item
                for item in snapshots
                if item.aggregate_type == AggregateType.DAG_NODE_RUN
                and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
            ]
            if epoch.state == "PAUSE_REQUESTED" and all(
                item.state in {"PAUSED", "ACCEPTED", "STALE", "CANCELLED", "TRIAGE_REQUIRED"} for item in nodes
            ):
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="NODES_PAUSED",
                        workflow_id=workflow_id,
                        aggregate_type=AggregateType.EXECUTION_EPOCH,
                        aggregate_id=epoch.aggregate_id,
                        actor="minion-v2-control",
                        expected_version=epoch.version,
                        idempotency_key=f"reconcile:{epoch.aggregate_id}:{epoch.version}:paused",
                    )
                )
            elif epoch.state == "CANCEL_REQUESTED" and all(
                item.state in {"CANCELLED", "ACCEPTED", "STALE", "TRIAGE_REQUIRED"} for item in nodes
            ):
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="NODES_CANCELLED",
                        workflow_id=workflow_id,
                        aggregate_type=AggregateType.EXECUTION_EPOCH,
                        aggregate_id=epoch.aggregate_id,
                        actor="minion-v2-control",
                        expected_version=epoch.version,
                        idempotency_key=f"reconcile:{epoch.aggregate_id}:{epoch.version}:cancelled",
                    )
                )
        snapshots = list(self.repository.list_workflow_snapshots(workflow_id))
        workflow = next((item for item in snapshots if item.aggregate_type == AggregateType.WORKFLOW), None)
        if workflow is None:
            return
        children = [item for item in snapshots if item.aggregate_type != AggregateType.WORKFLOW]
        if workflow.state == "PAUSE_REQUESTED" and all(
            item.state in {"PAUSED", "ACCEPTED", "REJECTED", "COMPLETED", "STALE", "CANCELLED", "TRIAGE_REQUIRED"}
            for item in children
        ):
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="CHILDREN_PAUSED",
                    workflow_id=workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=workflow_id,
                    actor="minion-v2-control",
                    expected_version=workflow.version,
                    idempotency_key=f"reconcile:{workflow_id}:{workflow.version}:paused",
                )
            )
        elif workflow.state == "CANCEL_REQUESTED" and all(
            item.state in {"CANCELLED", "ACCEPTED", "REJECTED", "COMPLETED", "STALE", "TRIAGE_REQUIRED"}
            for item in children
        ):
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="CHILDREN_CANCELLED",
                    workflow_id=workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=workflow_id,
                    actor="minion-v2-control",
                    expected_version=workflow.version,
                    idempotency_key=f"reconcile:{workflow_id}:{workflow.version}:cancelled",
                )
            )


def _derived_id(prefix: str, effect_key: str) -> str:
    return f"{prefix}_{hashlib.sha256(effect_key.encode('utf-8')).hexdigest()[:24]}"
