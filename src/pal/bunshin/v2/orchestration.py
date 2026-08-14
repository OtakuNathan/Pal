from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Protocol

from pal.bunshin.v2.artifacts import ArtifactRef
from pal.bunshin.v2.contracts import (
    ActionEnvelope,
    AggregateSnapshot,
    AggregateType,
    DeferredEffectError,
    PermanentEffectError,
)
from pal.bunshin.v2.execution import DagScheduler, ExecutionCompiler
from pal.bunshin.v2.machine_dsl import ControlDisposition, ControlIntent
from pal.bunshin.v2.human_review import (
    HUMAN_REVIEW_RENDER_VERSION,
    human_review_card_is_current,
)
from pal.bunshin.v2.machines import machine_spec_for
from pal.bunshin.v2.paths import cleanup_workflow_worktrees
from pal.bunshin.v2.replan import collect_architecture_finding_batch
from pal.bunshin.v2.service import BunshinV2WorkflowService, workflow_request_from_snapshot
from pal.bunshin.v2.sessions import (
    architecture_reviewer_session_id,
    architect_session_id_for_revision,
)
from pal.bunshin.v2.verification import DefectPropagationService
from pal.bunshin.v2.workflow_runtime import WorkflowCoordinator


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
    "propagate_pause",
    "propagate_resume",
    "propagate_cancel",
    "freeze_workflow_children",
    "pause_epoch_nodes",
    "cancel_epoch_nodes",
    "freeze_epoch_nodes",
    "suspend_stale_node_assignments",
    "reopen_dependency_and_stale_descendants",
    "request_epoch_replan",
    "freeze_epoch_for_replan",
    "create_replan_revision",
    "submit_workflow_completion",
    "cleanup_terminal_worktrees",
    "submit_standalone_completion",
    "start_review_repair_execution",
    "submit_workflow_rejection",
    "start_replacement_workflow_from_architecture",
    "reconcile_execution_epoch",
    "reconcile_workflow",
})

CONTROL_RECONCILIATION_EFFECT_TYPES = frozenset(
    {
        "reconcile_workflow",
        "reconcile_execution_epoch",
        "reconcile_semantic_state",
    }
)

CANCELLATION_EFFECT_TYPES = CONTROL_RECONCILIATION_EFFECT_TYPES | frozenset(
    {
        "propagate_cancel",
        "cancel_epoch_nodes",
        "cancel_role",
    }
)

PAUSE_EFFECT_TYPES = CONTROL_RECONCILIATION_EFFECT_TYPES | frozenset(
    {
        "propagate_pause",
        "pause_epoch_nodes",
        "pause_role",
    }
)

TRIAGE_FREEZE_EFFECT_TYPES = frozenset(
    {
        "freeze_workflow_children",
        "freeze_epoch_nodes",
        "quiesce_role_for_triage",
    }
)


@dataclass
class BunshinV2OutboxProcessor:
    service: BunshinV2WorkflowService
    semantic_effects: SemanticEffectPort = field(default_factory=RejectingSemanticEffectPort)
    publish_workflow_event: Callable[[Mapping[str, Any]], None] | None = None
    worker_id: str = "bunshin-v2-outbox"
    effect_lease_seconds: int = 120
    _background_tasks: set[asyncio.Task[str]] = field(default_factory=set, init=False, repr=False)

    @property
    def repository(self):
        return self.service.repository

    @property
    def active_background_count(self) -> int:
        return sum(not task.done() for task in self._background_tasks)

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
                name=f"bunshin-v2-effect-{str(effect['effect_id'])[-12:]}",
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
            self._reconcile_replan_collections(str(effect.get("workflow_id") or ""))
            self._publish_terminal_workflow_if_any(
                str(effect.get("workflow_id") or "")
            )
            result_ref = dict(result.get("result_artifact_ref") or {}) if isinstance(result, Mapping) else {}
            provider_request_id = str(result.get("provider_request_id") or "") if isinstance(result, Mapping) else ""
            self.repository.complete_outbox_effect(
                effect_id,
                worker_id=self.worker_id,
                provider_request_id=provider_request_id,
                result_artifact_ref=result_ref,
            )
            return "completed"
        except DeferredEffectError as exc:
            self.repository.defer_outbox_effect(
                effect_id,
                worker_id=self.worker_id,
                reason=str(exc) or "effect deferred at a durable restart safe point",
                attempt_was_incremented=bool(effect.get("claim_incremented_attempt", True)),
            )
            return "deferred"
        except Exception as exc:
            snapshot = self._effect_snapshot(effect)
            effect_type = str(effect.get("effect_type") or "")
            superseded = (
                snapshot.state in {"PAUSED", "CANCELLED", "STALE"}
                or (
                    snapshot.state == "PAUSE_REQUESTED"
                    and effect_type not in PAUSE_EFFECT_TYPES
                )
                or (
                    snapshot.state == "CANCEL_REQUESTED"
                    and effect_type not in CANCELLATION_EFFECT_TYPES
                )
                or (
                    snapshot.state == "TRIAGE_REQUIRED"
                    and effect_type not in TRIAGE_FREEZE_EFFECT_TYPES
                )
            )
            if superseded:
                self.repository.complete_outbox_effect(effect_id, worker_id=self.worker_id)
                self._reconcile_control_requests(str(effect.get("workflow_id") or ""))
                self._reconcile_replan_collections(str(effect.get("workflow_id") or ""))
                self._publish_terminal_workflow_if_any(
                    str(effect.get("workflow_id") or "")
                )
                return "completed"
            error = f"{exc.__class__.__name__}: {exc}"
            if isinstance(exc, PermanentEffectError):
                triage_action = self._failed_effect_triage_action(effect, exc)
                with self.repository.transaction() as connection:
                    self.repository.fail_outbox_effect(
                        effect_id,
                        worker_id=self.worker_id,
                        error=error,
                        triage_action=triage_action,
                        _connection=connection,
                    )
                    self._sync_cycle_triage(
                        triage_action,
                        _connection=connection,
                    )
            else:
                triage_action = None
                if int(effect.get("attempt_count") or 0) >= int(effect.get("max_attempts") or 1):
                    triage_action = self._failed_effect_triage_action(effect, exc)
                with self.repository.transaction() as connection:
                    self.repository.retry_outbox_effect(
                        effect_id,
                        worker_id=self.worker_id,
                        error=error,
                        retry_after_seconds=5,
                        triage_action=triage_action,
                        _connection=connection,
                    )
                    self._sync_cycle_triage(
                        triage_action,
                        _connection=connection,
                    )
            self._reconcile_control_requests(str(effect.get("workflow_id") or ""))
            self._reconcile_replan_collections(str(effect.get("workflow_id") or ""))
            self._publish_terminal_workflow_if_any(
                str(effect.get("workflow_id") or "")
            )
            return "failed"
        finally:
            heartbeat.cancel()
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass

    def _sync_cycle_triage(
        self,
        action: ActionEnvelope | None,
        *,
        _connection=None,
    ) -> None:
        if action is None:
            return
        coordinator = WorkflowCoordinator(self.repository)
        if action.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
            coordinator.require_plan_triage(
                workflow_id=action.workflow_id,
                _connection=_connection,
            )
            return
        if action.aggregate_type != AggregateType.DAG_NODE_RUN:
            return
        node = self.repository.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            action.aggregate_id,
            _connection=_connection,
        )
        if node is None:
            return
        coordinator.require_node_triage(
            workflow_id=action.workflow_id,
            node_name=str(
                node.payload.get("module_name")
                or node.payload.get("unit_id")
                or ""
            ),
            _connection=_connection,
        )

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
        if effect_type == "schedule_ready_nodes":
            snapshot = self._effect_snapshot(effect)
            epoch_id = snapshot.aggregate_id if snapshot.aggregate_type == AggregateType.EXECUTION_EPOCH else str(snapshot.payload.get("epoch_id") or "")
            DagScheduler(self.repository).schedule_ready_nodes(
                workflow_id=snapshot.workflow_id,
                epoch_id=epoch_id,
            )
            return {}
        if effect_type == "notify_node_accepted":
            return self._node_accepted(effect)
        if effect_type == "publish_accepted_memory_candidate":
            return self._publish_accepted_memory_candidate(effect)
        if effect_type in {
            "propagate_pause",
            "propagate_resume",
            "propagate_cancel",
            "freeze_workflow_children",
        }:
            return self._propagate_workflow_control(effect_type, effect)
        if effect_type in {
            "pause_epoch_nodes",
            "cancel_epoch_nodes",
            "freeze_epoch_nodes",
        }:
            return self._control_epoch_nodes(effect_type, effect)
        if effect_type == "suspend_stale_node_assignments":
            node = self._effect_snapshot(effect)
            self.repository.cancel_role_assignments(
                workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN,
                aggregate_id=node.aggregate_id,
                reason="node became stale before its queued activation started",
            )
            return {}
        if effect_type == "reopen_dependency_and_stale_descendants":
            node = self._effect_snapshot(effect)
            repair_ref = ArtifactRef.from_mapping(dict(node.payload.get("repair_bill_ref") or {}))
            targets = tuple(
                dict.fromkeys(
                    str(item)
                    for item in (
                        *list(node.payload.get("repair_target_node_ids") or []),
                        node.payload.get("repair_target_node_id") or "",
                    )
                    if str(item)
                )
            )
            if not targets:
                raise RuntimeError("repair propagation effect has no explicit target nodes")
            for target in targets:
                DefectPropagationService(self.repository).propagate_dependency_defect(
                    workflow_id=node.workflow_id,
                    epoch_id=str(node.payload.get("epoch_id") or ""),
                    dependency_node_id=target,
                    repair_bill_ref=repair_ref,
                    reopen_action="REOPEN_DEPENDENCY",
                )
            return {}
        if effect_type == "request_epoch_replan":
            return self._request_epoch_replan(effect)
        if effect_type == "freeze_epoch_for_replan":
            return self._freeze_epoch_for_replan(effect)
        if effect_type == "create_replan_revision":
            return self._create_replan_revision(effect)
        if effect_type == "submit_workflow_completion":
            epoch = self._effect_snapshot(effect)
            workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, epoch.workflow_id)
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="MARK_COMPLETED",
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=epoch.workflow_id,
                    actor="bunshin-v2-manager",
                    expected_version=workflow.version,
                    idempotency_key=f"effect:{effect['effect_key']}:workflow-complete",
                    payload={"result_artifact_ref": epoch.payload.get("published_deliverable_ref")},
                )
            )
            self.repository.complete_workflow_role_sessions(epoch.workflow_id)
            return {}
        if effect_type == "cleanup_terminal_worktrees":
            epoch = self._effect_snapshot(effect)
            manifest_ref = ArtifactRef.from_mapping(
                dict(epoch.payload.get("architecture_manifest_ref") or {})
            )
            manifest = dict(self.service.artifacts.read_json(manifest_ref))
            repository_layout = dict(manifest.get("repository_layout") or {})
            if repository_layout:
                cleanup_workflow_worktrees(
                    self.service.runtime_root,
                    repository_layout=repository_layout,
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
                    actor="bunshin-v2-manager",
                    expected_version=workflow.version,
                    idempotency_key=f"effect:{effect['effect_key']}:workflow-complete",
                    payload={"result_artifact_ref": review.payload.get("verification_artifact_ref")},
                )
            )
            self.repository.complete_workflow_role_sessions(review.workflow_id)
            return {}
        if effect_type == "start_review_repair_execution":
            review = self._effect_snapshot(effect)
            return self._compile_execution(
                workflow_id=review.workflow_id,
                manifest_ref=dict(review.payload.get("architecture_manifest_ref") or {}),
                causation_key=str(effect["effect_key"]),
                initial_repair_bill_ref=dict(review.payload.get("repair_bill_ref") or {}) or None,
            )
        if effect_type == "start_replacement_workflow_from_architecture":
            return self._start_replacement_workflow_from_architecture(effect)
        if effect_type == "submit_workflow_rejection":
            revision = self._effect_snapshot(effect)
            self._complete_architecture_cycle_sessions(revision)
            workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
            if workflow is not None and workflow.state == "ACTIVE":
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="REJECT_WORKFLOW",
                        workflow_id=workflow.workflow_id,
                        aggregate_type=AggregateType.WORKFLOW,
                        aggregate_id=workflow.aggregate_id,
                        actor="bunshin-v2-manager",
                        expected_version=workflow.version,
                        idempotency_key=f"effect:{effect['effect_key']}:reject-workflow",
                    )
                )
                self.repository.complete_workflow_role_sessions(revision.workflow_id)
            return {}
        if effect_type == "reconcile_execution_epoch":
            return await self._reconcile_execution_epoch(effect)
        if effect_type == "reconcile_workflow":
            return self._reconcile_workflow(effect)
        raise ValueError(f"unsupported mechanical V2 effect: {effect_type}")

    async def _reconcile_execution_epoch(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        epoch = self._effect_snapshot(effect)
        if epoch.state == "NOT_STARTED":
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="START_EXECUTION",
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch.aggregate_id,
                    actor="bunshin-v2-recovery",
                    expected_version=epoch.version,
                    idempotency_key=f"effect:{effect['effect_key']}:start-execution",
                )
            )
            return {}
        if epoch.state == "PAUSE_REQUESTED":
            return self._control_epoch_nodes("pause_epoch_nodes", effect)
        if epoch.state == "CANCEL_REQUESTED":
            return self._control_epoch_nodes("cancel_epoch_nodes", effect)
        if epoch.state == "REPLAN_COLLECTING":
            self._reconcile_replan_collection(epoch)
            return {}
        if epoch.state == "REPLAN_REQUIRED":
            if not str(epoch.payload.get("active_replan_revision_id") or ""):
                return self._create_replan_revision(effect)
            return {}
        if epoch.state == "RUNNING":
            self._control_epoch_nodes("resume_epoch_nodes", effect)
            DagScheduler(self.repository).schedule_ready_nodes(
                workflow_id=epoch.workflow_id,
                epoch_id=epoch.aggregate_id,
            )
            nodes = self._epoch_nodes(epoch)
            graph_execution = WorkflowCoordinator(self.repository).execution(
                workflow_id=epoch.workflow_id,
                generation=int(epoch.payload.get("graph_generation") or 0) or None,
            )
            if nodes and graph_execution.state.value == "COMPLETED":
                accepted = next(
                    (
                        item
                        for item in nodes
                        if bool(item.payload.get("graph_sink"))
                    ),
                    sorted(nodes, key=lambda item: item.aggregate_id)[-1],
                )
                return self._node_accepted(
                    {
                        **dict(effect),
                        "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                        "aggregate_id": accepted.aggregate_id,
                        "effect_key": f"{effect['effect_key']}:accepted-node-reconcile",
                    }
                )
            return {}
        if epoch.state == "FINALIZING":
            return await self.semantic_effects.execute_semantic_effect(
                {**dict(effect), "effect_type": "publish_final_deliverable"}
            )
        if epoch.state == "STARTING":
            self._control_epoch_nodes("resume_epoch_nodes", effect)
            nodes = [
                item
                for item in self.repository.list_workflow_snapshots(epoch.workflow_id)
                if item.aggregate_type == AggregateType.DAG_NODE_RUN
                and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
            ]
            sinks = [item for item in nodes if bool(item.payload.get("graph_sink"))]
            if not nodes or len(sinks) != 1:
                raise RuntimeError("execution compilation is incomplete and requires operator triage")
            payload = {"node_ids": [item.aggregate_id for item in nodes]}
            payload["implementation_node_ids"] = [item.aggregate_id for item in nodes]
            payload["sink_node_id"] = sinks[0].aggregate_id
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="NODES_COMPILED",
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch.aggregate_id,
                    actor="bunshin-v2-recovery",
                    expected_version=epoch.version,
                    idempotency_key=f"effect:{effect['effect_key']}:nodes-compiled",
                    payload=payload,
                )
            )
        return {}

    def _reconcile_workflow(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        workflow = self._effect_snapshot(effect)
        if workflow.state == "CREATED":
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="START_WORKFLOW",
                    workflow_id=workflow.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=workflow.aggregate_id,
                    actor="bunshin-v2-recovery",
                    expected_version=workflow.version,
                    idempotency_key=f"effect:{effect['effect_key']}:start-workflow",
                )
            )
            return {}
        if workflow.state == "PAUSE_REQUESTED":
            return self._propagate_workflow_control("propagate_pause", effect)
        if workflow.state == "CANCEL_REQUESTED":
            return self._propagate_workflow_control("propagate_cancel", effect)
        if workflow.state != "ACTIVE":
            return {}

        self._propagate_workflow_control("propagate_resume", effect)
        snapshots = list(self.repository.list_workflow_snapshots(workflow.workflow_id))
        epoch_id = str(workflow.payload.get("execution_epoch_id") or "")
        review_id = str(workflow.payload.get("standalone_review_id") or "")
        revision_id = str(workflow.payload.get("architecture_revision_id") or "")
        epoch = next(
            (
                item
                for item in snapshots
                if item.aggregate_type == AggregateType.EXECUTION_EPOCH
                and item.aggregate_id == epoch_id
            ),
            None,
        ) if epoch_id else None
        revision = next(
            (
                item
                for item in snapshots
                if item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
                and item.aggregate_id == revision_id
            ),
            None,
        ) if revision_id else None

        if revision_id and revision is None:
            raise RuntimeError("workflow references a missing architecture revision")
        if revision is not None:
            revision_manifest_sha = str(
                dict(revision.payload.get("architecture_manifest_ref") or {}).get("sha256")
                or ""
            )
            epoch_manifest_sha = str(
                dict((epoch.payload if epoch is not None else {}).get("architecture_manifest_ref") or {}).get("sha256")
                or ""
            )
            revision_precedes_epoch = (
                revision.state != "ACCEPTED"
                or epoch is None
                or not revision_manifest_sha
                or revision_manifest_sha != epoch_manifest_sha
            )
            if revision_precedes_epoch:
                return self._reconcile_linked_revision(
                    workflow,
                    revision,
                    snapshots,
                    effect,
                )

        if epoch_id:
            if epoch is None:
                raise RuntimeError("workflow references a missing execution epoch")
            if epoch.state == "COMPLETED":
                result_ref = dict(epoch.payload.get("published_deliverable_ref") or {})
                if not result_ref:
                    raise RuntimeError("completed execution epoch has no published deliverable")
                self._complete_active_workflow(workflow, result_ref, str(effect["effect_key"]))
            return {}

        if review_id:
            review = next(
                (
                    item
                    for item in snapshots
                    if item.aggregate_type == AggregateType.STANDALONE_REVIEW
                    and item.aggregate_id == review_id
                ),
                None,
            )
            if review is None:
                raise RuntimeError("workflow references a missing standalone review")
            if review.state == "COMPLETED":
                result_ref = dict(review.payload.get("verification_artifact_ref") or {})
                if not result_ref:
                    raise RuntimeError("completed standalone review has no verification artifact")
                self._complete_active_workflow(workflow, result_ref, str(effect["effect_key"]))
            return {}

        if revision is not None:
            return self._reconcile_linked_revision(workflow, revision, snapshots, effect)

        unlinked_children = _active_control_children(snapshots, workflow)
        if len(unlinked_children) > 1:
            raise RuntimeError("workflow has multiple unlinked active child aggregates")
        if unlinked_children:
            child = unlinked_children[0]
            action_type, payload_field = {
                AggregateType.ARCHITECTURE_REVISION: (
                    "LINK_ARCHITECTURE_REVISION",
                    "architecture_revision_id",
                ),
                AggregateType.EXECUTION_EPOCH: (
                    "LINK_EXECUTION_EPOCH",
                    "execution_epoch_id",
                ),
                AggregateType.STANDALONE_REVIEW: (
                    "LINK_STANDALONE_REVIEW",
                    "standalone_review_id",
                ),
            }[child.aggregate_type]
            self._link_workflow(
                workflow.workflow_id,
                action_type,
                {payload_field: child.aggregate_id},
                f"{effect['effect_key']}:relink-child",
            )
            return {}
        return self._route_workflow(effect)

    def _reconcile_linked_revision(
        self,
        workflow: AggregateSnapshot,
        revision: AggregateSnapshot,
        snapshots: list[AggregateSnapshot],
        effect: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        if revision.state == "HUMAN_REVIEW":
            manifest_ref = dict(revision.payload.get("architecture_manifest_ref") or {})
            card_ref = dict(revision.payload.get("human_review_card_ref") or {})
            if card_ref:
                card_payload: dict[str, Any] = {}
                record = self.repository.read_artifact_record(
                    str(card_ref.get("sha256") or "")
                )
                if record and str(record.get("artifact_type") or "") == "HumanReviewCardArtifact":
                    card_payload = dict(self.service.artifacts.read_json(card_ref))
                if not human_review_card_is_current(
                    card_payload,
                    manifest_sha=str(manifest_ref.get("sha256") or ""),
                ):
                    self.repository.dispatch(
                        ActionEnvelope(
                            action_type="REFRESH_HUMAN_REVIEW_CARD",
                            workflow_id=revision.workflow_id,
                            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                            aggregate_id=revision.aggregate_id,
                            actor="bunshin-v2-recovery",
                            expected_version=revision.version,
                            idempotency_key=(
                                f"effect:{effect['effect_key']}:refresh-human-review:"
                                f"v{HUMAN_REVIEW_RENDER_VERSION}"
                            ),
                            payload={"architecture_manifest_ref": manifest_ref},
                        )
                    )
            return {}
        if revision.state == "ACCEPTED":
            return self._reconcile_accepted_revision(workflow, revision, snapshots, effect)
        if revision.state == "SUPERSEDED":
            successor = next(
                (
                    item
                    for item in snapshots
                    if item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
                    and str(item.payload.get("parent_revision_id") or "") == revision.aggregate_id
                ),
                None,
            )
            if successor is not None:
                self._link_workflow(
                    workflow.workflow_id,
                    "LINK_ARCHITECTURE_REVISION",
                    {"architecture_revision_id": successor.aggregate_id},
                    f"{effect['effect_key']}:successor",
                )
                return {}
            return self._create_revision(
                {
                    **dict(effect),
                    "aggregate_type": AggregateType.ARCHITECTURE_REVISION.value,
                    "aggregate_id": revision.aggregate_id,
                    "effect_key": f"{effect['effect_key']}:recreate-revision",
                }
            )
        if revision.state == "REJECTED":
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="REJECT_WORKFLOW",
                    workflow_id=workflow.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=workflow.aggregate_id,
                    actor="bunshin-v2-recovery",
                    expected_version=workflow.version,
                    idempotency_key=f"effect:{effect['effect_key']}:reject-workflow",
                )
            )
        elif revision.state == "CANCELLED":
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="REQUEST_CANCEL",
                    workflow_id=workflow.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=workflow.aggregate_id,
                    actor="bunshin-v2-recovery",
                    expected_version=workflow.version,
                    idempotency_key=f"effect:{effect['effect_key']}:cancel-workflow",
                )
            )
        return {}

    def _reconcile_accepted_revision(
        self,
        workflow: AggregateSnapshot,
        revision: AggregateSnapshot,
        snapshots: list[AggregateSnapshot],
        effect: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        manifest_ref = dict(revision.payload.get("architecture_manifest_ref") or {})
        if not manifest_ref:
            raise RuntimeError("accepted architecture revision has no manifest")
        existing = next(
            (
                item
                for item in snapshots
                if item.aggregate_type == AggregateType.EXECUTION_EPOCH
                and dict(item.payload.get("architecture_manifest_ref") or {}).get("sha256")
                == manifest_ref.get("sha256")
                and item.state not in {"SUPERSEDED", "CANCELLED"}
            ),
            None,
        )
        if existing is not None:
            self._link_workflow(
                workflow.workflow_id,
                "LINK_EXECUTION_EPOCH",
                {"execution_epoch_id": existing.aggregate_id},
                f"{effect['effect_key']}:existing-epoch",
            )
            if existing.state == "COMPLETED":
                result_ref = dict(existing.payload.get("published_deliverable_ref") or {})
                if not result_ref:
                    raise RuntimeError("completed execution epoch has no published deliverable")
                latest_workflow = self.repository.read_snapshot(
                    AggregateType.WORKFLOW,
                    workflow.aggregate_id,
                )
                self._complete_active_workflow(
                    latest_workflow,
                    result_ref,
                    f"{effect['effect_key']}:existing-epoch",
                )
            return {}
        return self._compile_execution(
            workflow_id=workflow.workflow_id,
            manifest_ref=manifest_ref,
            causation_key=f"reconcile:{revision.aggregate_id}:{manifest_ref.get('sha256')}",
            source_epoch_id=str(revision.payload.get("source_execution_epoch_id") or ""),
        )

    def _complete_active_workflow(
        self,
        workflow: AggregateSnapshot,
        result_ref: Mapping[str, Any],
        effect_key: str,
    ) -> None:
        self.repository.dispatch(
            ActionEnvelope(
                action_type="MARK_COMPLETED",
                workflow_id=workflow.workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow.aggregate_id,
                actor="bunshin-v2-recovery",
                expected_version=workflow.version,
                idempotency_key=f"effect:{effect_key}:complete-workflow",
                payload={"result_artifact_ref": dict(result_ref)},
            )
        )
        self.repository.complete_workflow_role_sessions(workflow.workflow_id)

    def _publish_terminal_workflow_if_any(self, workflow_id: str) -> None:
        normalized_workflow_id = str(workflow_id or "").strip()
        if self.publish_workflow_event is None or not normalized_workflow_id:
            return
        workflow = self.repository.read_snapshot(
            AggregateType.WORKFLOW,
            normalized_workflow_id,
        )
        if workflow is None or workflow.state not in {
            "COMPLETED",
            "REJECTED",
            "CANCELLED",
        }:
            return
        normalized_status = workflow.state.lower()
        revision_id = str(workflow.payload.get("architecture_revision_id") or "").strip()
        resolved_interactions = (
            [
                {
                    "interaction_id": f"bunshin_v2_architecture_{revision_id}",
                    "interaction_kind": "bunshin_v2_architecture_review",
                }
            ]
            if revision_id
            else []
        )
        self.publish_workflow_event(
            {
                "workflow_id": normalized_workflow_id,
                "status": normalized_status,
                "summary": f"Bunshin workflow {normalized_status}.",
                "terminal_at": workflow.updated_at,
                "resolved_interactions": resolved_interactions,
                "result_artifact_ref": dict(
                    workflow.payload.get("result_artifact_ref") or {}
                ),
            }
        )

    def _submit_effect_action(self, effect: Mapping[str, Any], payload: Mapping[str, Any]) -> Mapping[str, Any]:
        action_type = str(payload.get("action_type") or "")
        snapshot = self._effect_snapshot(effect)
        if action_type == "START_EXECUTION" and snapshot.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
            manifest_ref = dict(snapshot.payload.get("architecture_manifest_ref") or {})
            if not manifest_ref:
                raise ValueError("accepted architecture revision has no manifest")
            self._complete_architecture_cycle_sessions(snapshot)
            return self._compile_execution(
                workflow_id=snapshot.workflow_id,
                manifest_ref=manifest_ref,
                causation_key=str(effect["effect_key"]),
                source_epoch_id=str(snapshot.payload.get("source_execution_epoch_id") or ""),
            )
        self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=snapshot.workflow_id,
                aggregate_type=snapshot.aggregate_type,
                aggregate_id=snapshot.aggregate_id,
                actor="bunshin-v2-outbox",
                # Effects are replayed from the same causative domain event.
                # Pin the request to that event's resulting version so a crash
                # retry hashes as the same business action after the first
                # attempt has already advanced the snapshot.
                expected_version=(
                    self.repository.read_domain_event_aggregate_version(
                        str(effect.get("event_id") or "")
                    )
                    or snapshot.version
                ),
                idempotency_key=f"effect:{effect['effect_key']}",
                causation_id=str(effect["event_id"]),
                payload=dict(payload.get("action_payload") or {}),
            )
        )
        return {}

    def _complete_architecture_cycle_sessions(
        self,
        revision: AggregateSnapshot,
        *,
        status: str = "completed",
    ) -> None:
        for session_id in (
            architect_session_id_for_revision(
                revision.workflow_id,
                revision.aggregate_id,
                revision.payload,
            ),
            architecture_reviewer_session_id(
                revision.workflow_id,
                revision.aggregate_id,
                revision.payload,
            ),
        ):
            self.repository.complete_role_session(session_id, status=status)

    def _route_workflow(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        workflow = self._effect_snapshot(effect)
        request = workflow_request_from_snapshot(self.service, workflow)
        operation = str(request.get("operation") or "new_requirement")
        if operation == "new_requirement":
            revision_id = _derived_id("arch", str(effect["effect_key"]))
            with self.repository.transaction() as connection:
                WorkflowCoordinator(self.repository).ensure_plan_cycle(
                    workflow_id=workflow.workflow_id,
                    _connection=connection,
                )
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="CREATE_ARCHITECTURE_REVISION",
                        workflow_id=workflow.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision_id,
                        actor="bunshin-v2-router",
                        expected_version=0,
                        idempotency_key=f"effect:{effect['effect_key']}:revision",
                        payload={
                            "request_ref": dict(workflow.payload["request_ref"]),
                            "requirements_ref": dict(request["requirements_ref"]),
                            "architecture_cycle_id": revision_id,
                            "research_mode": str(request.get("research_mode") or "local_only"),
                            "revision_number": 1,
                        },
                    ),
                    _connection=connection,
                )
                self._link_workflow(
                    workflow.workflow_id,
                    "LINK_ARCHITECTURE_REVISION",
                    {"architecture_revision_id": revision_id},
                    str(effect["effect_key"]),
                    _connection=connection,
                )
            return {}
        artifact_ref = dict(request.get("input_artifact_ref") or {})
        if operation == "execute_trusted":
            return self._compile_execution(workflow_id=workflow.workflow_id, manifest_ref=artifact_ref, causation_key=str(effect["effect_key"]))
        if operation == "review_then_execute":
            revision_id = _derived_id("arch", str(effect["effect_key"]))
            with self.repository.transaction() as connection:
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="IMPORT_ARCHITECTURE_REVISION",
                        workflow_id=workflow.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision_id,
                        actor="bunshin-v2-router",
                        expected_version=0,
                        idempotency_key=f"effect:{effect['effect_key']}:import",
                        payload={
                            "architecture_manifest_ref": artifact_ref,
                            "requirements_ref": dict(request.get("requirements_ref") or {}),
                            "architecture_cycle_id": revision_id,
                            "revision_number": 1,
                        },
                    ),
                    _connection=connection,
                )
                self._link_workflow(
                    workflow.workflow_id,
                    "LINK_ARCHITECTURE_REVISION",
                    {"architecture_revision_id": revision_id},
                    str(effect["effect_key"]),
                    _connection=connection,
                )
            return {}
        review_id = _derived_id("review", str(effect["effect_key"]))
        with self.repository.transaction() as connection:
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="CREATE_STANDALONE_REVIEW",
                    workflow_id=workflow.workflow_id,
                    aggregate_type=AggregateType.STANDALONE_REVIEW,
                    aggregate_id=review_id,
                    actor="bunshin-v2-router",
                    expected_version=0,
                    idempotency_key=f"effect:{effect['effect_key']}:review",
                    payload={"review_request_ref": artifact_ref, "review_mode": operation},
                ),
                _connection=connection,
            )
            review = self.repository.read_snapshot(
                AggregateType.STANDALONE_REVIEW,
                review_id,
                _connection=connection,
            )
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="QUEUE_REVIEW",
                    workflow_id=workflow.workflow_id,
                    aggregate_type=AggregateType.STANDALONE_REVIEW,
                    aggregate_id=review_id,
                    actor="bunshin-v2-router",
                    expected_version=review.version,
                    idempotency_key=f"effect:{effect['effect_key']}:queue-review",
                ),
                _connection=connection,
            )
            self._link_workflow(
                workflow.workflow_id,
                "LINK_STANDALONE_REVIEW",
                {"standalone_review_id": review_id},
                str(effect["effect_key"]),
                _connection=connection,
            )
        return {}

    def _create_revision(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        previous = self._effect_snapshot(effect)
        revision_id = _derived_id("arch", str(effect["effect_key"]))
        source_epoch_id = str(previous.payload.get("source_execution_epoch_id") or "")
        with self.repository.transaction() as connection:
            WorkflowCoordinator(self.repository).begin_plan_revision(
                workflow_id=previous.workflow_id,
                _connection=connection,
            )
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="CREATE_ARCHITECTURE_REVISION",
                    workflow_id=previous.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision_id,
                    actor="bunshin-v2-router",
                    expected_version=0,
                    idempotency_key=f"effect:{effect['effect_key']}:revision",
                    payload={
                        "request_ref": previous.payload.get("request_ref"),
                        "requirements_ref": previous.payload.get("requirements_ref"),
                        "parent_revision_id": previous.aggregate_id,
                        "architecture_cycle_id": str(
                            previous.payload.get("architecture_cycle_id")
                            or previous.payload.get("root_architecture_revision_id")
                            or previous.aggregate_id
                        ),
                        "revision_number": int(previous.payload.get("revision_number") or 1) + 1,
                        "edit_instruction_ref": previous.payload.get("edit_instruction_ref"),
                        "base_architecture_manifest_ref": previous.payload.get("architecture_manifest_ref"),
                        "research_mode": previous.payload.get("research_mode", "local_only"),
                        **(
                            {
                                "source_execution_epoch_id": source_epoch_id,
                                "replan_generation": previous.payload.get("replan_generation"),
                                "replan_finding_batch_ref": previous.payload.get(
                                    "replan_finding_batch_ref"
                                ),
                            }
                            if source_epoch_id
                            else {}
                        ),
                    },
                ),
                _connection=connection,
            )
            if source_epoch_id:
                epoch = self.repository.read_snapshot(
                    AggregateType.EXECUTION_EPOCH,
                    source_epoch_id,
                    _connection=connection,
                )
                if epoch is not None and epoch.state == "REPLAN_REQUIRED":
                    self.repository.dispatch(
                        ActionEnvelope(
                            action_type="REPLAN_REVISION_LINKED",
                            workflow_id=previous.workflow_id,
                            aggregate_type=AggregateType.EXECUTION_EPOCH,
                            aggregate_id=source_epoch_id,
                            actor="bunshin-v2-router",
                            expected_version=epoch.version,
                            idempotency_key=(
                                f"effect:{effect['effect_key']}:replan-revision-linked"
                            ),
                            payload={"active_replan_revision_id": revision_id},
                        ),
                        _connection=connection,
                    )
            self._link_workflow(
                previous.workflow_id,
                "LINK_ARCHITECTURE_REVISION",
                {"architecture_revision_id": revision_id},
                str(effect["effect_key"]),
                _connection=connection,
            )
        return {}

    def _request_epoch_replan(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        source = self._effect_snapshot(effect)
        if source.aggregate_type != AggregateType.DAG_NODE_RUN:
            raise ValueError("request_epoch_replan must originate from a DAG node")
        epoch_id = str(source.payload.get("epoch_id") or "")
        epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
        if epoch is None or epoch.state in {"SUPERSEDED", "COMPLETED", "CANCELLED"}:
            return {}
        finding_ref = dict(
            source.payload.get("repair_bill_ref") or source.payload.get("finding_artifact_ref") or {}
        )
        if not finding_ref:
            raise ValueError("architecture replan requires a persisted finding")
        finding_payload = dict(self.service.artifacts.read_json(finding_ref))
        fingerprint = str(finding_payload.get("finding_fingerprint") or finding_ref.get("sha256") or "")
        if epoch.state == "REPLAN_REQUIRED":
            known = set(str(item) for item in list(epoch.payload.get("replan_finding_fingerprints") or []))
            if fingerprint in known:
                return {}
            failure_ref = self.service.artifacts.put_json(
                {
                    "reason": "a new architecture finding arrived after the replan batch was frozen",
                    "finding_artifact_ref": finding_ref,
                    "finding_fingerprint": fingerprint,
                    "source_node": str(source.payload.get("module_name") or source.payload.get("unit_id") or ""),
                },
                artifact_type="LateReplanFindingArtifact",
                child_refs=((str(finding_ref["sha256"]), "late_finding"),),
            )
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="ENTER_TRIAGE",
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch.aggregate_id,
                    actor="bunshin-v2-manager",
                    expected_version=epoch.version,
                    idempotency_key=f"late-replan-finding:{epoch.aggregate_id}:{fingerprint}",
                    payload={
                        "failure_artifact_ref": failure_ref.to_dict(),
                        "blocker": {
                            "kind": "late_replan_finding",
                            "finding_artifact_ref": finding_ref,
                        },
                    },
                )
            )
            return {"result_artifact_ref": failure_ref.to_dict()}
        if epoch.state not in {"RUNNING", "FINALIZING", "REPLAN_COLLECTING"}:
            return {}
        self.repository.dispatch(
            ActionEnvelope(
                action_type="REGISTER_REPLAN_FINDING",
                workflow_id=source.workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id=epoch_id,
                actor="bunshin-v2-manager",
                expected_version=epoch.version,
                idempotency_key=f"effect:{effect['effect_key']}:register-replan-finding",
                payload={
                    "finding_artifact_ref": finding_ref,
                    "finding_fingerprint": fingerprint,
                    "source_node": str(source.payload.get("module_name") or source.payload.get("unit_id") or ""),
                },
            )
        )
        return {}

    def _freeze_epoch_for_replan(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        epoch = self._effect_snapshot(effect)
        if epoch.aggregate_type != AggregateType.EXECUTION_EPOCH:
            raise ValueError("freeze_epoch_for_replan requires an execution epoch")
        self._stale_non_drain_nodes(epoch)
        self._reconcile_replan_collection(epoch)
        return {}

    def _stale_non_drain_nodes(self, epoch: AggregateSnapshot) -> None:
        pending = [dict(item or {}) for item in list(epoch.payload.get("pending_replan_findings") or [])]
        finding_ref = dict(pending[0].get("finding_artifact_ref") or {}) if pending else {}
        if not finding_ref:
            return
        for node in self._epoch_nodes(epoch):
            if node.state in {
                "ACCEPTED",
                "STALE",
                "CANCELLED",
                "TRIAGE_REQUIRED",
                "REVIEWING",
                "REVIEW_QUIESCING",
                "REVIEW_SNAPSHOTTING",
            }:
                continue
            legal = self.repository.engine.legal_actions(AggregateType.DAG_NODE_RUN, node.state)
            action_type = (
                "MARK_STALE"
                if "MARK_STALE" in legal
                else "REQUEST_STALE"
                if "REQUEST_STALE" in legal
                else ""
            )
            if not action_type:
                continue
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="bunshin-v2-replan",
                    expected_version=node.version,
                    idempotency_key=f"replan-freeze:{epoch.aggregate_id}:{node.aggregate_id}:{node.version}",
                    payload={"stale_reason_ref": finding_ref},
                )
            )

    def _reconcile_replan_collections(self, workflow_id: str) -> None:
        if not workflow_id:
            return
        for epoch in self.repository.list_workflow_snapshots(workflow_id):
            if epoch.aggregate_type == AggregateType.EXECUTION_EPOCH and epoch.state == "REPLAN_COLLECTING":
                self._reconcile_replan_collection(epoch)

    def _reconcile_replan_collection(self, epoch: AggregateSnapshot) -> None:
        current = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch.aggregate_id)
        if current is None or current.state != "REPLAN_COLLECTING":
            return
        self._stale_non_drain_nodes(current)
        current = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch.aggregate_id)
        nodes = self._epoch_nodes(current)
        unsettled = {
            "PRODUCING",
            "REPAIRING",
            "QUIESCING",
            "SNAPSHOTTING",
            "REVIEWING",
            "REVIEW_QUIESCING",
            "REVIEW_SNAPSHOTTING",
            "PAUSE_REQUESTED",
            "CANCEL_REQUESTED",
        }
        if any(node.state in unsettled for node in nodes):
            return
        batch = collect_architecture_finding_batch(
            self.service.artifacts,
            epoch=current,
            nodes=nodes,
        )
        manifest_ref = dict(current.payload.get("architecture_manifest_ref") or {})
        manifest = dict(self.service.artifacts.read_json(manifest_ref)) if manifest_ref else {}
        requirements_ref = dict(manifest.get("requirements_ref") or {})
        latest = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, current.aggregate_id)
        if latest is None or latest.state != "REPLAN_COLLECTING":
            return
        self.repository.dispatch(
            ActionEnvelope(
                action_type="REPLAN_BATCH_READY",
                workflow_id=latest.workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id=latest.aggregate_id,
                actor="bunshin-v2-manager",
                expected_version=latest.version,
                idempotency_key=f"replan-batch-ready:{latest.aggregate_id}:{batch.artifact_ref.sha256}",
                payload={
                    "replan_finding_batch_ref": batch.artifact_ref.to_dict(),
                    "replan_finding_fingerprints": list(batch.finding_fingerprints),
                    "requirements_ref": requirements_ref,
                },
            )
        )

    def _create_replan_revision(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        epoch = self._effect_snapshot(effect)
        if epoch.aggregate_type != AggregateType.EXECUTION_EPOCH or epoch.state != "REPLAN_REQUIRED":
            return {}
        workflow = self.repository.read_snapshot(AggregateType.WORKFLOW, epoch.workflow_id)
        if workflow is None:
            raise ValueError("replan epoch has no workflow")
        generation = int(epoch.payload.get("replan_generation") or 0)
        revision_id = _derived_id("arch", f"replan:{epoch.aggregate_id}:{generation}")
        batch_ref = dict(epoch.payload.get("replan_finding_batch_ref") or {})
        with self.repository.transaction() as connection:
            plan_cycle = WorkflowCoordinator(self.repository).begin_plan_revision(
                workflow_id=epoch.workflow_id,
                _connection=connection,
            )
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="CREATE_ARCHITECTURE_REVISION",
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision_id,
                    actor="bunshin-v2-manager",
                    expected_version=0,
                    idempotency_key=f"replan-revision:{epoch.aggregate_id}:{generation}",
                    payload={
                        "request_ref": workflow.payload.get("request_ref"),
                        "requirements_ref": dict(epoch.payload.get("requirements_ref") or {}),
                        "base_architecture_manifest_ref": dict(epoch.payload.get("architecture_manifest_ref") or {}),
                        "replan_finding_batch_ref": batch_ref,
                        "architecture_cycle_id": revision_id,
                        "source_execution_epoch_id": epoch.aggregate_id,
                        "replan_generation": generation,
                        "research_mode": "local_only",
                        "revision_number": plan_cycle.generation,
                    },
                ),
                _connection=connection,
            )
            latest = self.repository.read_snapshot(
                AggregateType.EXECUTION_EPOCH,
                epoch.aggregate_id,
                _connection=connection,
            )
            if latest is not None and latest.state == "REPLAN_REQUIRED":
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="REPLAN_REVISION_LINKED",
                        workflow_id=epoch.workflow_id,
                        aggregate_type=AggregateType.EXECUTION_EPOCH,
                        aggregate_id=epoch.aggregate_id,
                        actor="bunshin-v2-manager",
                        expected_version=latest.version,
                        idempotency_key=f"replan-revision-linked:{epoch.aggregate_id}:{generation}",
                        payload={"active_replan_revision_id": revision_id},
                    ),
                    _connection=connection,
                )
            self._link_workflow(
                epoch.workflow_id,
                "LINK_ARCHITECTURE_REVISION",
                {"architecture_revision_id": revision_id},
                f"replan:{epoch.aggregate_id}:{generation}",
                _connection=connection,
            )
        return {"architecture_revision_id": revision_id, "result_artifact_ref": batch_ref}

    def _epoch_nodes(self, epoch: AggregateSnapshot) -> list[AggregateSnapshot]:
        return [
            item
            for item in self.repository.list_workflow_snapshots(epoch.workflow_id)
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
        ]

    def _compile_execution(
        self,
        *,
        workflow_id: str,
        manifest_ref: Mapping[str, Any],
        causation_key: str,
        source_epoch_id: str = "",
        initial_repair_bill_ref: Mapping[str, Any] | None = None,
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
        epoch_id = _execution_epoch_id(
            workflow_id=workflow_id,
            manifest_sha=ref.sha256,
            source_epoch_id=source_epoch_id,
            repair_bill_sha=str(
                dict(initial_repair_bill_ref or {}).get("sha256") or ""
            ),
        )
        compilation = ExecutionCompiler(self.repository, self.service.contracts).compile_epoch(
            workflow_id=workflow_id,
            epoch_id=epoch_id,
            manifest_ref=ref,
            source_epoch_id=source_epoch_id,
            initial_repair_bill_ref=initial_repair_bill_ref,
        )
        self._link_workflow(workflow_id, "LINK_EXECUTION_EPOCH", {"execution_epoch_id": epoch_id}, causation_key)
        if source_epoch_id:
            previous = self.repository.read_snapshot(
                AggregateType.EXECUTION_EPOCH,
                source_epoch_id,
            )
            if previous is not None and previous.state == "REPLAN_REQUIRED":
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="SUCCESSOR_EPOCH_STARTED",
                        workflow_id=workflow_id,
                        aggregate_type=AggregateType.EXECUTION_EPOCH,
                        aggregate_id=source_epoch_id,
                        actor="bunshin-v2-manager",
                        expected_version=previous.version,
                        idempotency_key=f"successor-epoch:{source_epoch_id}:{epoch_id}",
                        payload={"successor_execution_epoch_id": epoch_id},
                    )
                )
        return {"epoch_id": epoch_id, "node_ids": list(compilation.node_run_ids)}

    def _start_replacement_workflow_from_architecture(
        self,
        effect: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        source = self._effect_snapshot(effect)
        restart_request = dict(source.payload.get("restart_execution_request") or {})
        if not restart_request:
            return {}
        existing_replacement_id = str(source.payload.get("replacement_workflow_id") or "")
        if source.state == "CANCELLED" and existing_replacement_id:
            replacement = self.repository.read_snapshot(
                AggregateType.WORKFLOW,
                existing_replacement_id,
            )
            if replacement is None:
                raise RuntimeError("cancelled source workflow references a missing replacement")
            return {"replacement_workflow_id": existing_replacement_id}
        if source.aggregate_type != AggregateType.WORKFLOW or source.state != "RESTARTING":
            raise RuntimeError(
                "replacement workflow creation requires the source workflow to be RESTARTING"
            )
        request = restart_request
        manifest_ref = dict(request.get("architecture_manifest_ref") or {})
        requirements_ref = dict(request.get("requirements_ref") or {})
        task_id = str(request.get("task_id") or "")
        if not task_id or not manifest_ref or not requirements_ref:
            raise PermanentEffectError(
                "execution restart request is missing Task, architecture, or Requirements binding"
            )
        replacement_id = _derived_id("wf", str(effect["effect_key"]))
        replacement = self.repository.read_snapshot(
            AggregateType.WORKFLOW,
            replacement_id,
        )
        if source.payload.get("restart_cancel_requested") and replacement is None:
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="REPLACEMENT_WORKFLOW_ABORTED",
                    workflow_id=source.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=source.aggregate_id,
                    actor="bunshin-v2-manager",
                    expected_version=source.version,
                    idempotency_key=f"effect:{effect['effect_key']}:replacement-aborted",
                )
            )
            return {"status": "replacement_cancelled"}
        if replacement is None:
            self.service.start_workflow(
                {
                    "task_id": task_id,
                    "workflow_id": replacement_id,
                    "operation": "review_then_execute",
                    "artifact_ref": manifest_ref,
                    "requirements_ref": requirements_ref,
                    "goal": str(request.get("goal") or ""),
                    "research_mode": str(request.get("research_mode") or "none"),
                    "actor": str(request.get("actor") or "pal"),
                    "source_channel": "bunshin-v2-manager",
                }
            )
            replacement = self.repository.read_snapshot(
                AggregateType.WORKFLOW,
                replacement_id,
            )
        latest = self.repository.read_snapshot(AggregateType.WORKFLOW, source.aggregate_id)
        if latest is None:
            raise RuntimeError("source workflow disappeared while creating replacement")
        if latest.state == "RESTARTING":
            if latest.payload.get("restart_cancel_requested"):
                if replacement is None:
                    raise RuntimeError("replacement workflow disappeared before cancellation")
                if "REQUEST_CANCEL" in self.repository.engine.legal_actions(
                    AggregateType.WORKFLOW,
                    replacement.state,
                ):
                    self.service.control_workflow(
                        workflow_id=replacement.aggregate_id,
                        command="cancel",
                        actor="bunshin-v2-manager",
                        source_channel="bunshin-v2-manager",
                        reason="execution restart was cancelled before replacement activation",
                    )
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="REPLACEMENT_WORKFLOW_STARTED",
                    workflow_id=latest.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=latest.aggregate_id,
                    actor="bunshin-v2-manager",
                    expected_version=latest.version,
                    idempotency_key=f"effect:{effect['effect_key']}:replacement-started",
                    payload={"replacement_workflow_id": replacement_id},
                )
            )
        elif not (
            latest.state == "CANCELLED"
            and str(latest.payload.get("replacement_workflow_id") or "") == replacement_id
        ):
            raise RuntimeError(
                f"source workflow left RESTARTING while creating replacement: {latest.state}"
            )
        return {"replacement_workflow_id": replacement_id}

    def _node_accepted(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        epoch_id = str(node.payload.get("epoch_id") or "")
        DagScheduler(self.repository).schedule_ready_nodes(
            workflow_id=node.workflow_id,
            epoch_id=epoch_id,
        )
        nodes = [
            item
            for item in self.repository.list_workflow_snapshots(node.workflow_id)
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == epoch_id
        ]
        sinks = [item for item in nodes if bool(item.payload.get("graph_sink"))]
        if nodes and len(sinks) == 1:
            graph_execution = WorkflowCoordinator(self.repository).execution(
                workflow_id=node.workflow_id,
                generation=int(node.payload.get("graph_generation") or 0) or None,
            )
        else:
            graph_execution = None
        if graph_execution is not None and graph_execution.state.value == "COMPLETED":
            sink = sinks[0]
            if (
                node.aggregate_id == sink.aggregate_id
                and str(dict(sink.payload.get("candidate_ref") or {}).get("sha256") or "")
                == graph_execution.published_sink_ref
            ):
                epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
                if epoch is not None and epoch.state == "RUNNING":
                    self.repository.dispatch(
                        ActionEnvelope(
                            action_type="ALL_REQUIRED_NODES_ACCEPTED",
                            workflow_id=node.workflow_id,
                            aggregate_type=AggregateType.EXECUTION_EPOCH,
                            aggregate_id=epoch_id,
                            actor="bunshin-v2-outbox",
                            expected_version=epoch.version,
                            idempotency_key=f"effect:{effect['effect_key']}:all-required-accepted",
                            payload={
                                "accepted_candidate_refs": [dict(item.payload["candidate_ref"]) for item in nodes],
                                "verification_artifact_refs": [
                                    dict(item.payload["verification_artifact_ref"])
                                    for item in nodes
                                    if item.payload.get("verification_artifact_ref")
                                ],
                                "sink_candidate_ref": dict(
                                    sink.payload.get("candidate_ref") or {}
                                ),
                            },
                        )
                    )
        return {}

    def _publish_accepted_memory_candidate(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        if node.state != "ACCEPTED" or str(node.payload.get("node_kind") or "unit") != "unit":
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
                    actor="bunshin-v2-outbox",
                    expected_version=current.version,
                    idempotency_key=f"effect:{effect['effect_key']}:memory-candidate",
                    payload={"memory_candidate_ref": memory_candidate_ref.to_dict()},
                )
            )
        return {"result_artifact_ref": memory_candidate_ref.to_dict()}

    def _propagate_workflow_control(self, effect_type: str, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        workflow = self._effect_snapshot(effect)
        snapshots = self.repository.list_workflow_snapshots(workflow.workflow_id)
        children = _active_control_children(snapshots, workflow)
        if effect_type == "propagate_resume":
            for child in children:
                if "RESUME" not in self.repository.engine.legal_actions(
                    child.aggregate_type,
                    child.state,
                ):
                    continue
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="RESUME",
                        workflow_id=workflow.workflow_id,
                        aggregate_type=child.aggregate_type,
                        aggregate_id=child.aggregate_id,
                        actor="bunshin-v2-control",
                        expected_version=child.version,
                        idempotency_key=f"effect:{effect['effect_key']}:{child.aggregate_id}",
                    )
                )
            return {}

        intent = (
            ControlIntent.CANCEL
            if effect_type == "propagate_cancel"
            else ControlIntent.PAUSE
        )
        for child in children:
            machine = machine_spec_for(child.aggregate_type)
            if machine.control_disposition(intent, child.state) != ControlDisposition.REQUEST:
                continue
            action_type = machine.control_policies[intent].request_action
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=workflow.workflow_id,
                    aggregate_type=child.aggregate_type,
                    aggregate_id=child.aggregate_id,
                    actor="bunshin-v2-control",
                    expected_version=child.version,
                    idempotency_key=f"effect:{effect['effect_key']}:{child.aggregate_id}",
                )
            )

        if effect_type in {"propagate_pause", "propagate_cancel"}:
            latest_snapshots = self.repository.list_workflow_snapshots(workflow.workflow_id)
            latest_workflow = self.repository.read_snapshot(
                AggregateType.WORKFLOW,
                workflow.workflow_id,
            )
            if latest_workflow is None:
                raise RuntimeError("workflow disappeared during control propagation")
            latest_children = _active_control_children(latest_snapshots, latest_workflow)
            all_settled = all(
                machine_spec_for(child.aggregate_type).control_disposition(
                    intent,
                    child.state,
                )
                == ControlDisposition.SETTLED
                for child in latest_children
            )
        else:
            all_settled = False
        if all_settled:
            confirmation = "CHILDREN_PAUSED" if effect_type == "propagate_pause" else "CHILDREN_CANCELLED"
            latest = self.repository.read_snapshot(
                AggregateType.WORKFLOW,
                workflow.workflow_id,
            )
            if confirmation in self.repository.engine.legal_actions(
                AggregateType.WORKFLOW,
                latest.state,
            ):
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type=confirmation,
                        workflow_id=workflow.workflow_id,
                        aggregate_type=AggregateType.WORKFLOW,
                        aggregate_id=workflow.workflow_id,
                        actor="bunshin-v2-control",
                        expected_version=latest.version,
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
        node_machine = machine_spec_for(AggregateType.DAG_NODE_RUN)
        intent = (
            ControlIntent.CANCEL
            if effect_type == "cancel_epoch_nodes"
            else ControlIntent.PAUSE
        )
        for node in nodes:
            if effect_type == "resume_epoch_nodes":
                action_type = "RESUME"
                if action_type not in self.repository.engine.legal_actions(
                    AggregateType.DAG_NODE_RUN,
                    node.state,
                ):
                    continue
            else:
                if (
                    node_machine.control_disposition(intent, node.state)
                    != ControlDisposition.REQUEST
                ):
                    continue
                action_type = node_machine.control_policies[intent].request_action
            self.repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="bunshin-v2-control",
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
        *,
        _connection=None,
    ) -> None:
        workflow = self.repository.read_snapshot(
            AggregateType.WORKFLOW,
            workflow_id,
            _connection=_connection,
        )
        if workflow is None:
            raise ValueError("workflow does not exist")
        self.repository.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="bunshin-v2-router",
                expected_version=workflow.version,
                idempotency_key=f"link:{causation_key}:{action_type}",
                payload=dict(payload),
            ),
            _connection=_connection,
        )

    def _effect_snapshot(self, effect: Mapping[str, Any]) -> AggregateSnapshot:
        aggregate_type = AggregateType(str(effect["aggregate_type"]))
        snapshot = self.repository.read_snapshot(aggregate_type, str(effect["aggregate_id"]))
        if snapshot is None:
            raise ValueError("effect aggregate no longer exists")
        return snapshot

    def _failed_effect_triage_action(
        self,
        effect: Mapping[str, Any],
        exc: Exception,
    ) -> ActionEnvelope | None:
        source = self._effect_snapshot(effect)
        target = source
        if "ENTER_TRIAGE" not in self.repository.engine.legal_actions(
            source.aggregate_type,
            source.state,
        ):
            workflow = self.repository.read_snapshot(
                AggregateType.WORKFLOW,
                source.workflow_id,
            )
            if workflow is None or "ENTER_TRIAGE" not in self.repository.engine.legal_actions(
                AggregateType.WORKFLOW,
                workflow.state,
            ):
                return None
            target = workflow
        failure_ref = self.service.artifacts.put_json(
            {
                "effect_id": effect.get("effect_id"),
                "effect_type": effect.get("effect_type"),
                "attempt_count": effect.get("attempt_count"),
                "error": f"{exc.__class__.__name__}: {exc}",
                "source_aggregate_type": source.aggregate_type.value,
                "source_aggregate_id": source.aggregate_id,
                "source_aggregate_state": source.state,
            },
            artifact_type="EffectFailureArtifact",
        )
        return ActionEnvelope(
            action_type="ENTER_TRIAGE",
            workflow_id=target.workflow_id,
            aggregate_type=target.aggregate_type,
            aggregate_id=target.aggregate_id,
            actor="bunshin-v2-outbox",
            idempotency_key=(
                f"effect-failed:{effect['effect_key']}:"
                f"{target.aggregate_type.value}:{target.aggregate_id}"
            ),
            payload={
                "failure_artifact_ref": failure_ref.to_dict(),
                "blocker": {
                    "kind": "effect_failed",
                    "effect_type": effect.get("effect_type"),
                    "failure_artifact_ref": failure_ref.to_dict(),
                    "source_aggregate_type": source.aggregate_type.value,
                    "source_aggregate_id": source.aggregate_id,
                    "source_aggregate_state": source.state,
                },
            },
        )

    def _reconcile_control_requests(self, workflow_id: str) -> None:
        reconcile_control_requests(self.repository, workflow_id)


def reconcile_control_requests(repository: Any, workflow_id: str) -> None:
    if not workflow_id:
        return
    snapshots = list(repository.list_workflow_snapshots(workflow_id))
    workflow = next(
        (item for item in snapshots if item.aggregate_type == AggregateType.WORKFLOW),
        None,
    )
    if workflow is None:
        return
    _reconcile_cycle_control_projection(repository, workflow, snapshots)
    requested_intent = {
        "PAUSE_REQUESTED": ControlIntent.PAUSE,
        "CANCEL_REQUESTED": ControlIntent.CANCEL,
    }.get(workflow.state)
    if requested_intent is not None:
        for child in _active_control_children(snapshots, workflow):
            machine = machine_spec_for(child.aggregate_type)
            if (
                machine.control_disposition(requested_intent, child.state)
                != ControlDisposition.REQUEST
            ):
                continue
            action_type = machine.control_policies[requested_intent].request_action
            repository.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=workflow_id,
                    aggregate_type=child.aggregate_type,
                    aggregate_id=child.aggregate_id,
                    actor="bunshin-v2-control",
                    expected_version=child.version,
                    idempotency_key=(
                        f"reconcile:{workflow_id}:{action_type.lower()}:"
                        f"{child.aggregate_id}:{child.version}"
                    ),
                )
            )
    elif workflow.state == "ACTIVE":
        for child in _active_control_children(snapshots, workflow):
            if child.state != "PAUSED" or "RESUME" not in repository.engine.legal_actions(
                child.aggregate_type,
                child.state,
            ):
                continue
            repository.dispatch(
                ActionEnvelope(
                    action_type="RESUME",
                    workflow_id=workflow_id,
                    aggregate_type=child.aggregate_type,
                    aggregate_id=child.aggregate_id,
                    actor="bunshin-v2-control",
                    expected_version=child.version,
                    idempotency_key=(
                        f"reconcile:{workflow_id}:resume:"
                        f"{child.aggregate_id}:{child.version}"
                    ),
                )
            )

    snapshots = list(repository.list_workflow_snapshots(workflow_id))
    for epoch in [
        item
        for item in snapshots
        if item.aggregate_type == AggregateType.EXECUTION_EPOCH
    ]:
        nodes = [
            item
            for item in snapshots
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
        ]
        node_machine = machine_spec_for(AggregateType.DAG_NODE_RUN)
        epoch_intent = {
            "PAUSE_REQUESTED": ControlIntent.PAUSE,
            "CANCEL_REQUESTED": ControlIntent.CANCEL,
        }.get(epoch.state)
        if epoch_intent is not None:
            for node in nodes:
                if (
                    node_machine.control_disposition(epoch_intent, node.state)
                    != ControlDisposition.REQUEST
                ):
                    continue
                action_type = node_machine.control_policies[epoch_intent].request_action
                repository.dispatch(
                    ActionEnvelope(
                        action_type=action_type,
                        workflow_id=workflow_id,
                        aggregate_type=AggregateType.DAG_NODE_RUN,
                        aggregate_id=node.aggregate_id,
                        actor="bunshin-v2-control",
                        expected_version=node.version,
                        idempotency_key=(
                            f"reconcile:{epoch.aggregate_id}:{action_type.lower()}:"
                            f"{node.aggregate_id}:{node.version}"
                        ),
                    )
                )
            snapshots = list(repository.list_workflow_snapshots(workflow_id))
            nodes = [
                item
                for item in snapshots
                if item.aggregate_type == AggregateType.DAG_NODE_RUN
                and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
            ]
            epoch = next(
                item
                for item in snapshots
                if item.aggregate_type == AggregateType.EXECUTION_EPOCH
                and item.aggregate_id == epoch.aggregate_id
            )
        if epoch.state == "PAUSE_REQUESTED" and all(
            node_machine.control_disposition(ControlIntent.PAUSE, item.state)
            == ControlDisposition.SETTLED
            for item in nodes
        ):
            repository.dispatch(
                ActionEnvelope(
                    action_type="NODES_PAUSED",
                    workflow_id=workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch.aggregate_id,
                    actor="bunshin-v2-control",
                    expected_version=epoch.version,
                    idempotency_key=f"reconcile:{epoch.aggregate_id}:{epoch.version}:paused",
                )
            )
        elif epoch.state == "CANCEL_REQUESTED" and all(
            node_machine.control_disposition(ControlIntent.CANCEL, item.state)
            == ControlDisposition.SETTLED
            for item in nodes
        ):
            repository.dispatch(
                ActionEnvelope(
                    action_type="NODES_CANCELLED",
                    workflow_id=workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch.aggregate_id,
                    actor="bunshin-v2-control",
                    expected_version=epoch.version,
                    idempotency_key=f"reconcile:{epoch.aggregate_id}:{epoch.version}:cancelled",
                )
            )
        elif epoch.state in {"STARTING", "RUNNING", "FINALIZING"}:
            for node in nodes:
                if node.state != "PAUSED" or "RESUME" not in repository.engine.legal_actions(
                    AggregateType.DAG_NODE_RUN,
                    node.state,
                ):
                    continue
                repository.dispatch(
                    ActionEnvelope(
                        action_type="RESUME",
                        workflow_id=workflow_id,
                        aggregate_type=AggregateType.DAG_NODE_RUN,
                        aggregate_id=node.aggregate_id,
                        actor="bunshin-v2-control",
                        expected_version=node.version,
                        idempotency_key=(
                            f"reconcile:{epoch.aggregate_id}:resume:"
                            f"{node.aggregate_id}:{node.version}"
                        ),
                    )
                )

    snapshots = list(repository.list_workflow_snapshots(workflow_id))
    workflow = next(
        (item for item in snapshots if item.aggregate_type == AggregateType.WORKFLOW),
        None,
    )
    if workflow is None:
        return
    children = _active_control_children(snapshots, workflow)
    if workflow.state == "PAUSE_REQUESTED" and all(
        machine_spec_for(item.aggregate_type).control_disposition(
            ControlIntent.PAUSE,
            item.state,
        )
        == ControlDisposition.SETTLED
        for item in children
    ):
        repository.dispatch(
            ActionEnvelope(
                action_type="CHILDREN_PAUSED",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="bunshin-v2-control",
                expected_version=workflow.version,
                idempotency_key=f"reconcile:{workflow_id}:{workflow.version}:paused",
            )
        )
    elif workflow.state == "CANCEL_REQUESTED" and all(
        machine_spec_for(item.aggregate_type).control_disposition(
            ControlIntent.CANCEL,
            item.state,
        )
        == ControlDisposition.SETTLED
        for item in children
    ):
        repository.dispatch(
            ActionEnvelope(
                action_type="CHILDREN_CANCELLED",
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="bunshin-v2-control",
                expected_version=workflow.version,
                idempotency_key=f"reconcile:{workflow_id}:{workflow.version}:cancelled",
            )
        )

    refreshed = list(repository.list_workflow_snapshots(workflow_id))
    refreshed_workflow = next(
        (
            item
            for item in refreshed
            if item.aggregate_type == AggregateType.WORKFLOW
        ),
        None,
    )
    if refreshed_workflow is not None:
        _reconcile_cycle_control_projection(
            repository,
            refreshed_workflow,
            refreshed,
        )


def _reconcile_cycle_control_projection(
    repository: Any,
    workflow: AggregateSnapshot,
    snapshots: list[AggregateSnapshot],
) -> None:
    """Repair the crash window between aggregate control and cycle state.

    Aggregate machines project process/effect progress. PlanCycle and
    GraphExecution remain the semantic owners, so this reconciliation is
    one-way and idempotent: committed aggregate intent may repair a missing
    cycle write, never the reverse.
    """

    coordinator = WorkflowCoordinator(repository)
    if workflow.state in {"PAUSE_REQUESTED", "PAUSED"}:
        coordinator.request_workflow_pause(workflow_id=workflow.workflow_id)
    elif workflow.state in {"CANCEL_REQUESTED", "CANCELLED"}:
        coordinator.request_workflow_cancel(workflow_id=workflow.workflow_id)
    elif workflow.state == "ACTIVE":
        coordinator.resume_workflow(workflow_id=workflow.workflow_id)

    active_revision_id = str(
        workflow.payload.get("architecture_revision_id") or ""
    )
    for item in snapshots:
        if (
            item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
            and item.aggregate_id == active_revision_id
            and item.state in {"PAUSED", "CANCELLED"}
        ):
            coordinator.confirm_plan_control(
                workflow_id=workflow.workflow_id,
                cancel=item.state == "CANCELLED",
            )
        if (
            item.aggregate_type == AggregateType.DAG_NODE_RUN
            and item.state in {"PAUSED", "CANCELLED"}
        ):
            coordinator.confirm_node_control(
                workflow_id=workflow.workflow_id,
                node_name=str(
                    item.payload.get("module_name")
                    or item.payload.get("unit_id")
                    or ""
                ),
                cancel=item.state == "CANCELLED",
            )


def _active_control_children(
    snapshots: list[AggregateSnapshot],
    workflow: AggregateSnapshot,
) -> list[AggregateSnapshot]:
    active_ids = {
        str(workflow.payload.get(field) or "")
        for field in (
            "architecture_revision_id",
            "execution_epoch_id",
            "standalone_review_id",
        )
        if str(workflow.payload.get(field) or "")
    }
    children = [
        item
        for item in snapshots
        if item.aggregate_type
        in {
            AggregateType.ARCHITECTURE_REVISION,
            AggregateType.EXECUTION_EPOCH,
            AggregateType.STANDALONE_REVIEW,
        }
        and item.aggregate_id in active_ids
    ]
    if active_ids:
        return children
    return [
        item
        for item in snapshots
        if item.aggregate_type
        in {
            AggregateType.ARCHITECTURE_REVISION,
            AggregateType.EXECUTION_EPOCH,
            AggregateType.STANDALONE_REVIEW,
        }
        and item.state
        not in {
            "ACCEPTED",
            "REJECTED",
            "COMPLETED",
            "CANCELLED",
            "STALE",
            "SUPERSEDED",
        }
    ]


def _derived_id(prefix: str, effect_key: str) -> str:
    return f"{prefix}_{hashlib.sha256(effect_key.encode('utf-8')).hexdigest()[:24]}"


def _execution_epoch_id(
    *,
    workflow_id: str,
    manifest_sha: str,
    source_epoch_id: str = "",
    repair_bill_sha: str = "",
) -> str:
    semantic_identity = json.dumps(
        {
            "manifest_sha": str(manifest_sha),
            "repair_bill_sha": str(repair_bill_sha),
            "source_epoch_id": str(source_epoch_id),
            "workflow_id": str(workflow_id),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _derived_id("epoch", semantic_identity)
