from __future__ import annotations
from pal.foundation.diagnostics import exception_report
from pal.bunshin.effect_dispatcher import WorkflowEffectDispatcher
from pal.bunshin.unit_of_work import BunshinUnitOfWork

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from pal.bunshin.artifacts import ArtifactRef
from pal.bunshin.contract_protocol import CONTRACT_ARTIFACT
from pal.bunshin.direct_contract import DIRECT_EXECUTION_ARTIFACT
from pal.bunshin.contracts import (
    ActionEnvelope,
    AggregateSnapshot,
    AggregateType,
    DeferredEffectError,
    PermanentEffectError,
)
from pal.bunshin.workflow_publication import WorkflowPublication
from pal.bunshin.dag_scheduling import DagScheduler
from pal.bunshin.epoch_compilation import ExecutionCompiler
from pal.bunshin.machine_dsl import ControlDisposition, ControlIntent
from pal.bunshin.human_review import (
    HUMAN_REVIEW_RENDER_VERSION,
    human_review_card_is_current,
)
from pal.bunshin.machines import machine_spec_for
from pal.bunshin.paths import (
    cleanup_workflow_runtime,
    cleanup_workflow_worktrees,
    resolve_project_git_layout,
)
from pal.bunshin.graph_replanning import GraphReplanning
from pal.bunshin.workflow_effect_context import WorkflowEffectContext, _derived_id
from pal.bunshin.service import workflow_request_from_snapshot
from pal.bunshin.sessions import (
    architecture_reviewer_session_id,
    architect_session_id_for_revision,
)
from pal.bunshin.verification import DefectPropagationService
from pal.bunshin.workflow_control import (
    _active_control_children, _reconcile_cycle_control_projection,
    _workflow_pauses_cycles, control_effect_is_current, reconcile_control_requests,
)
from pal.bunshin.workflow_runtime import WorkflowCoordinator, role_pause_must_drain


class SemanticEffectPort(Protocol):
    async def execute_semantic_effect(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        ...


@dataclass
class RejectingSemanticEffectPort:
    async def execute_semantic_effect(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError(f"no Bunshin semantic worker is configured for effect {effect.get('effect_type')}")


LEGACY_MECHANICAL_EFFECT_TYPES = frozenset({"cleanup_terminal_worktrees"})


MECHANICAL_EFFECT_TYPES = LEGACY_MECHANICAL_EFFECT_TYPES | frozenset({
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
    "cleanup_terminal_runtime",
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
class WorkflowEffects(WorkflowEffectContext):
    semantic_effects: SemanticEffectPort = field(default_factory=RejectingSemanticEffectPort)
    publish_workflow_event: Callable[[Mapping[str, Any]], None] | None = None

    replanning: GraphReplanning = field(init=False)
    publication: WorkflowPublication = field(init=False)

    def __post_init__(self) -> None:
        self.replanning = GraphReplanning(self.service)
        self.publication = WorkflowPublication(self.service, self.publish_workflow_event)

    def reconcile_after_effect(self, effect: Mapping[str, Any]) -> None:
        workflow_id = str(effect.get("workflow_id") or "")
        if not workflow_id:
            return
        effect_type = str(effect.get("effect_type") or "")
        if (effect_type.startswith(("resume_", "reconcile_"))
                or self.repository.queries.control_reconciliation_needed(workflow_id)):
            self._reconcile_control_requests(workflow_id)
        self.replanning.reconcile_replan_collections(workflow_id)
        self.publication.publish_terminal(workflow_id)

    async def execute_effect(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if str(effect.get("effect_type") or "") in MECHANICAL_EFFECT_TYPES:
            result = await self._execute_mechanical(effect)
        else:
            result = await self.semantic_effects.execute_semantic_effect(effect)
        self.reconcile_after_effect(effect)
        return result

    def settle_failed_effect(self, effect: Mapping[str, Any], exc: Exception, worker_id: str) -> str:
        effect_id = str(effect["effect_id"])
        snapshot = self._effect_snapshot(effect)
        effect_type = str(effect.get("effect_type") or "")
        superseded = effect_type != "reconcile_dependency_repairs" and (
            # A cohort effect remains authoritative after its source has
            # closed to STALE; only its exact receipt/generation handler
            # may decide supersession. Errors must remain retryable/triaged.
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
            failure_ref = self.service.artifacts.put_json(
                {
                    "effect_id": effect_id,
                    "effect_type": effect_type,
                    "status": "superseded_after_failure",
                    "aggregate_state": snapshot.state,
                    "error": exception_report(exc),
                    "summary": "Effect failed and was retired because its aggregate changed state; execution success is not confirmed.",
                },
                artifact_type="EffectFailureArtifact",
            )
            self.repository.outbox_results.complete_outbox_effect(
                effect_id, worker_id=worker_id, result_artifact_ref=failure_ref.to_dict(),
            )
            self.reconcile_after_effect(effect)
            return "completed"
        error = exception_report(exc)
        if isinstance(exc, PermanentEffectError):
            triage_action = self._failed_effect_triage_action(effect, exc)
            with self.repository.transaction() as connection:
                (connection or self.repository).outbox_results.fail_outbox_effect(
                    effect_id,
                    worker_id=worker_id,
                    error=error,
                    triage_action=triage_action,
                )
                self._sync_cycle_triage(
                    triage_action,
                    unit_of_work=connection,
                )
        else:
            triage_action = None
            if int(effect.get("attempt_count") or 0) >= int(effect.get("max_attempts") or 1):
                triage_action = self._failed_effect_triage_action(effect, exc)
            with self.repository.transaction() as connection:
                (connection or self.repository).outbox_results.retry_outbox_effect(
                    effect_id,
                    worker_id=worker_id,
                    error=error,
                    retry_after_seconds=5,
                    triage_action=triage_action,
                )
                self._sync_cycle_triage(
                    triage_action,
                    unit_of_work=connection,
                )
        self.reconcile_after_effect(effect)
        return "failed"

    def _sync_cycle_triage(
        self,
        action: ActionEnvelope | None,
        *,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        if action is None:
            return
        coordinator = WorkflowCoordinator(self.repository)
        if action.aggregate_type == AggregateType.WORKFLOW:
            workflow = (unit_of_work or self.repository).snapshots.read_snapshot(
                AggregateType.WORKFLOW, action.aggregate_id,
            )
            if workflow is not None and _workflow_pauses_cycles(workflow):
                coordinator.request_workflow_pause(
                    workflow_id=action.workflow_id,
                    unit_of_work=unit_of_work,
                )
            return
        if action.aggregate_type == AggregateType.ARCHITECTURE_REVISION:
            coordinator.require_plan_triage(
                workflow_id=action.workflow_id,
                unit_of_work=unit_of_work,
            )
            return
        if action.aggregate_type != AggregateType.DAG_NODE_RUN:
            return
        node = (unit_of_work or self.repository).snapshots.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            action.aggregate_id,
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
            unit_of_work=unit_of_work,
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
            self._advance_graph(
                workflow_id=snapshot.workflow_id,
                epoch_id=epoch_id,
                generation=int(snapshot.payload.get("graph_generation") or 0),
            )
            return {}
        if effect_type == "notify_node_accepted":
            return self._node_accepted(effect)
        if effect_type == "publish_accepted_memory_candidate":
            return self.publication.publish_memory_candidate(effect)
        if effect_type == "propagate_resume":
            # Resuming also restores routing when pause consumed the initial
            # start effect before any child aggregate existed.
            return self._reconcile_workflow(effect)
        if effect_type in {
            "propagate_pause",
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
            with self.repository.transaction() as work:
                node = work.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, str(effect["aggregate_id"]))
                if node is None or not control_effect_is_current(effect_type, node):
                    return {"status": "superseded"}
                work.role_cancellation.cancel_role_assignments(
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
            DefectPropagationService(self.repository).propagate_dependency_defects(
                workflow_id=node.workflow_id,
                epoch_id=str(node.payload.get("epoch_id") or ""),
                dependency_node_ids=targets,
                repair_bill_ref=repair_ref,
                reopen_action="REOPEN_DEPENDENCY",
            )
            return {}
        if effect_type == "request_epoch_replan":
            return self.replanning.request_epoch_replan(effect)
        if effect_type == "freeze_epoch_for_replan":
            return self.replanning.freeze_epoch_for_replan(effect)
        if effect_type == "create_replan_revision":
            return self.replanning.create_replan_revision(effect)
        if effect_type == "submit_workflow_completion":
            epoch = self._effect_snapshot(effect)
            workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, epoch.workflow_id)
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="MARK_COMPLETED",
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=epoch.workflow_id,
                    actor="bunshin-manager",
                    expected_version=workflow.version,
                    idempotency_key=f"effect:{effect['effect_key']}:workflow-complete",
                    payload={"result_artifact_ref": epoch.payload.get("published_deliverable_ref")},
                )
            )
            self.repository.role_maintenance.complete_workflow_role_sessions(epoch.workflow_id)
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
            cleanup_workflow_runtime(
                self.service.runtime_root,
                workflow_id=epoch.workflow_id,
            )
            return {}
        if effect_type == "cleanup_terminal_runtime":
            workflow = self._effect_snapshot(effect)
            if (
                workflow.aggregate_type != AggregateType.WORKFLOW
                or workflow.state not in {"COMPLETED", "REJECTED", "CANCELLED"}
            ):
                raise RuntimeError(
                    "terminal runtime cleanup requires a terminal workflow"
                )
            self.repository.role_maintenance.complete_workflow_role_sessions(
                workflow.workflow_id,
                status="cancelled" if workflow.state == "CANCELLED" else "completed",
            )
            self.repository.role_maintenance.reconcile_role_session_checkpoints()
            self.repository.role_maintenance.reconcile_role_runtime_spool()
            repository_layout = self._terminal_repository_layout(workflow)
            if repository_layout:
                cleanup_workflow_worktrees(
                    self.service.runtime_root,
                    repository_layout=repository_layout,
                )
            cleanup_workflow_runtime(
                self.service.runtime_root,
                workflow_id=workflow.workflow_id,
            )
            return {}
        if effect_type == "submit_standalone_completion":
            review = self._effect_snapshot(effect)
            workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, review.workflow_id)
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="MARK_COMPLETED",
                    workflow_id=review.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=review.workflow_id,
                    actor="bunshin-manager",
                    expected_version=workflow.version,
                    idempotency_key=f"effect:{effect['effect_key']}:workflow-complete",
                    payload={"result_artifact_ref": review.payload.get("verification_artifact_ref")},
                )
            )
            self.repository.role_maintenance.complete_workflow_role_sessions(review.workflow_id)
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
            workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, revision.workflow_id)
            if workflow is not None and workflow.state == "ACTIVE":
                self.repository.transitions.dispatch(
                    ActionEnvelope(
                        action_type="REJECT_WORKFLOW",
                        workflow_id=workflow.workflow_id,
                        aggregate_type=AggregateType.WORKFLOW,
                        aggregate_id=workflow.aggregate_id,
                        actor="bunshin-manager",
                        expected_version=workflow.version,
                        idempotency_key=f"effect:{effect['effect_key']}:reject-workflow",
                    )
                )
                self.repository.role_maintenance.complete_workflow_role_sessions(revision.workflow_id)
            return {}
        if effect_type == "reconcile_execution_epoch":
            return await self._reconcile_execution_epoch(effect)
        if effect_type == "reconcile_workflow":
            result = self._reconcile_workflow(effect)
            await self._resume_pending_dependency_repair(effect)
            return result
        raise ValueError(f"unsupported mechanical Bunshin effect: {effect_type}")

    def _terminal_repository_layout(
        self,
        workflow: AggregateSnapshot,
    ) -> dict[str, Any]:
        for snapshot in reversed(
            self.repository.queries.list_workflow_snapshots(workflow.workflow_id)
        ):
            manifest_ref = dict(
                snapshot.payload.get("architecture_manifest_ref") or {}
            )
            if not manifest_ref:
                continue
            manifest = dict(self.service.artifacts.read_json(manifest_ref))
            repository_layout = dict(manifest.get("repository_layout") or {})
            if repository_layout:
                return repository_layout
        if not dict(workflow.payload.get("request_ref") or {}):
            return {}
        request = workflow_request_from_snapshot(self.service, workflow)
        workspace = dict(request.get("workspace") or {})
        if not str(workspace.get("repo_path") or workspace.get("cwd") or "").strip():
            return {}
        return resolve_project_git_layout(
            self.service.runtime_root,
            workspace=workspace,
            workflow_id=workflow.workflow_id,
            workflow_name=str(
                request.get("workflow_name")
                or workflow.payload.get("workflow_name")
                or workflow.workflow_id
            ),
        ).to_artifact_dict()

    async def _reconcile_execution_epoch(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        epoch = self._effect_snapshot(effect)
        if epoch.state == "NOT_STARTED":
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="START_EXECUTION",
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch.aggregate_id,
                    actor="bunshin-recovery",
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
            self.replanning.reconcile_replan_collection(epoch)
            return {}
        if epoch.state == "REPLAN_REQUIRED":
            if not str(epoch.payload.get("active_replan_revision_id") or ""):
                return self.replanning.create_replan_revision(effect)
            return {}
        if epoch.state == "RUNNING":
            self._control_epoch_nodes("resume_epoch_nodes", effect)
            self._advance_graph(
                workflow_id=epoch.workflow_id, epoch_id=epoch.aggregate_id,
                generation=int(epoch.payload.get("graph_generation") or 0),
            )
            return {}
        if epoch.state == "FINALIZING":
            return await self.semantic_effects.execute_semantic_effect(
                {**dict(effect), "effect_type": "publish_final_deliverable"}
            )
        if epoch.state == "STARTING":
            manifest_ref = dict(epoch.payload.get("architecture_manifest_ref") or {})
            record = self.repository.artifacts.read_artifact_record(str(manifest_ref.get("sha256") or ""))
            if record is not None and record["artifact_type"] in {CONTRACT_ARTIFACT, DIRECT_EXECUTION_ARTIFACT}:
                source_epoch_id = str(epoch.payload.get("source_execution_epoch_id") or "")
                if "source_execution_epoch_id" not in epoch.payload and int(epoch.payload.get("graph_generation") or 0) > 1:
                    # Historical preparations did not persist the source.
                    # The still-linked predecessor is the only admissible
                    # recovery authority; never guess from node membership.
                    workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, epoch.workflow_id)
                    source_epoch_id = str(workflow.payload.get("execution_epoch_id") or "") if workflow else ""
                    if not source_epoch_id or source_epoch_id == epoch.aggregate_id:
                        raise PermanentEffectError("legacy replan preparation lost its source epoch")
                return self._compile_execution(
                    workflow_id=epoch.workflow_id, manifest_ref=manifest_ref,
                    causation_key=str(effect["effect_key"]), source_epoch_id=source_epoch_id,
                    initial_repair_bill_ref=dict(epoch.payload.get("initial_repair_bill_ref") or {}) or None,
                    epoch_id=epoch.aggregate_id,
                )
            WorkflowCoordinator(self.repository).activate_graph(
                workflow_id=epoch.workflow_id, epoch_id=epoch.aggregate_id,
                actor="bunshin-recovery",
            )
        return {}

    async def _resume_pending_dependency_repair(self, effect: Mapping[str, Any]) -> None:
        """A fresh public workflow recovery resumes the durable cohort receipt.

        The original node may already be STALE, and its bounded outbox attempt
        may be exhausted. Reusing its immutable authority here starts no worker
        and does not reset or hide that old failed effect's retry history.
        """
        workflow = self._effect_snapshot(effect)
        if workflow.state != "ACTIVE":
            return
        execution = self.repository.cycles.read_graph_execution(workflow_id=workflow.workflow_id)
        cohort = execution.dependency_repairs.pending if execution is not None else None
        if cohort is None:
            return
        for refs in cohort.captures.values():
            for ref in refs:
                if ref.get("artifact_type") != "DependencyRepairCaptureArtifact":
                    continue
                prepared = self.repository.queries.read_dependency_repair_effect(workflow.workflow_id, str(ref["sha256"]))
                if prepared:
                    await self.semantic_effects.execute_semantic_effect(prepared)
                    return
        raise PermanentEffectError("pending dependency repair lost its immutable registration wake")

    def _reconcile_workflow(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        workflow = self._effect_snapshot(effect)
        if workflow.state == "CREATED":
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="START_WORKFLOW",
                    workflow_id=workflow.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=workflow.aggregate_id,
                    actor="bunshin-recovery",
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
        snapshots = list(self.repository.queries.list_workflow_snapshots(workflow.workflow_id))
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
                record = self.repository.artifacts.read_artifact_record(
                    str(card_ref.get("sha256") or "")
                )
                if record and str(record.get("artifact_type") or "") == "HumanReviewCardArtifact":
                    card_payload = dict(self.service.artifacts.read_json(card_ref))
                if not human_review_card_is_current(
                    card_payload,
                    manifest_sha=str(manifest_ref.get("sha256") or ""),
                ):
                    self.repository.transitions.dispatch(
                        ActionEnvelope(
                            action_type="REFRESH_HUMAN_REVIEW_CARD",
                            workflow_id=revision.workflow_id,
                            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                            aggregate_id=revision.aggregate_id,
                            actor="bunshin-recovery",
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
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="REJECT_WORKFLOW",
                    workflow_id=workflow.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=workflow.aggregate_id,
                    actor="bunshin-recovery",
                    expected_version=workflow.version,
                    idempotency_key=f"effect:{effect['effect_key']}:reject-workflow",
                )
            )
        elif revision.state == "CANCELLED":
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="REQUEST_CANCEL",
                    workflow_id=workflow.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=workflow.aggregate_id,
                    actor="bunshin-recovery",
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
                latest_workflow = self.repository.snapshots.read_snapshot(
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
        self.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="MARK_COMPLETED",
                workflow_id=workflow.workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow.aggregate_id,
                actor="bunshin-recovery",
                expected_version=workflow.version,
                idempotency_key=f"effect:{effect_key}:complete-workflow",
                payload={"result_artifact_ref": dict(result_ref)},
            )
        )
        self.repository.role_maintenance.complete_workflow_role_sessions(workflow.workflow_id)

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
        self.repository.transitions.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=snapshot.workflow_id,
                aggregate_type=snapshot.aggregate_type,
                aggregate_id=snapshot.aggregate_id,
                actor="bunshin-outbox",
                # Effects are replayed from the same causative domain event.
                # Pin the request to that event's resulting version so a crash
                # retry hashes as the same business action after the first
                # attempt has already advanced the snapshot.
                expected_version=(
                    self.repository.queries.read_domain_event_aggregate_version(
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
            self.repository.role_sessions.complete_role_session(session_id, status=status)

    def _route_workflow(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        workflow = self._effect_snapshot(effect)
        if workflow.state != "ACTIVE":
            # A queued route can arrive after pause/cancel. Do not create a
            # partial child tree; resume reconciliation restores this work.
            return {}
        request = workflow_request_from_snapshot(self.service, workflow)
        operation = str(request.get("operation") or "new_requirement")
        if operation == "new_requirement" and request.get("execution_mode") == "direct":
            from pal.bunshin.direct_execution import prepare_direct_execution
            existing_epoch = str(workflow.payload.get("execution_epoch_id") or "")
            if existing_epoch:
                return {"epoch_id": existing_epoch, "node_ids": [
                    item.aggregate_id for item in self.repository.queries.list_workflow_snapshots(workflow.workflow_id)
                    if item.aggregate_type == AggregateType.DAG_NODE_RUN and item.payload.get("epoch_id") == existing_epoch]}
            manifest_ref = dict(workflow.payload.get("direct_execution_ref") or {})
            if not manifest_ref:
                base_ref = dict(request.get("input_artifact_ref") or {})
                base = self.service.artifacts.read_json(base_ref) if base_ref else None
                if base is not None and base.get("execution_mode") != "direct":
                    raise ValueError("direct restart source must be a direct execution artifact")
                manifest_ref = prepare_direct_execution(self.service, workflow.workflow_id, request, base_artifact=base).to_dict()
                self._link_workflow(workflow.workflow_id, "BIND_DIRECT_EXECUTION",
                                    {"direct_execution_ref": manifest_ref}, str(effect["effect_key"]))
            return self._compile_execution(workflow_id=workflow.workflow_id, manifest_ref=manifest_ref,
                                           causation_key=str(effect["effect_key"]))
        if operation == "new_requirement":
            if workflow.payload.get("architecture_revision_id"):
                # The original route may arrive after resume restored it.
                return {}
            revision_id = _derived_id("arch", str(effect["effect_key"]))
            with self.repository.transaction() as connection:
                WorkflowCoordinator(self.repository).ensure_plan_cycle(
                    workflow_id=workflow.workflow_id,
                    unit_of_work=connection,
                )
                (connection or self.repository).transitions.dispatch(
                    ActionEnvelope(
                        action_type="CREATE_ARCHITECTURE_REVISION",
                        workflow_id=workflow.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision_id,
                        actor="bunshin-router",
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
                )
                self._link_workflow(
                    workflow.workflow_id,
                    "LINK_ARCHITECTURE_REVISION",
                    {"architecture_revision_id": revision_id},
                    str(effect["effect_key"]),
                    unit_of_work=connection,
                )
            return {}
        artifact_ref = dict(request.get("input_artifact_ref") or {})
        if operation == "execute_trusted":
            return self._compile_execution(workflow_id=workflow.workflow_id, manifest_ref=artifact_ref, causation_key=str(effect["effect_key"]))
        if operation == "review_then_execute":
            revision_id = _derived_id("arch", str(effect["effect_key"]))
            with self.repository.transaction() as connection:
                imported = (connection or self.repository).transitions.dispatch(
                    ActionEnvelope(
                        action_type="IMPORT_ARCHITECTURE_REVISION",
                        workflow_id=workflow.workflow_id,
                        aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                        aggregate_id=revision_id,
                        actor="bunshin-router",
                        expected_version=0,
                        idempotency_key=f"effect:{effect['effect_key']}:import",
                        payload={
                            "architecture_manifest_ref": artifact_ref,
                            "requirements_ref": dict(request.get("requirements_ref") or {}),
                            "architecture_cycle_id": revision_id,
                            "revision_number": 1,
                        },
                    ),
                )
                if imported.duplicate:
                    return {"status": "import_replayed"}
                self._link_workflow(
                    workflow.workflow_id,
                    "LINK_ARCHITECTURE_REVISION",
                    {"architecture_revision_id": revision_id},
                    str(effect["effect_key"]),
                    unit_of_work=connection,
                )
                from pal.bunshin.imported_plan import bind_imported_plan_product
                bind_imported_plan_product(repository=self.repository, artifacts=self.service.artifacts,
                    revision=imported.snapshot, unit_of_work=connection)
            return {}
        review_id = _derived_id("review", str(effect["effect_key"]))
        with self.repository.transaction() as connection:
            (connection or self.repository).transitions.dispatch(
                ActionEnvelope(
                    action_type="CREATE_STANDALONE_REVIEW",
                    workflow_id=workflow.workflow_id,
                    aggregate_type=AggregateType.STANDALONE_REVIEW,
                    aggregate_id=review_id,
                    actor="bunshin-router",
                    expected_version=0,
                    idempotency_key=f"effect:{effect['effect_key']}:review",
                    payload={"review_request_ref": artifact_ref, "review_mode": operation},
                ),
            )
            review = (connection or self.repository).snapshots.read_snapshot(
                AggregateType.STANDALONE_REVIEW,
                review_id,
            )
            (connection or self.repository).transitions.dispatch(
                ActionEnvelope(
                    action_type="QUEUE_REVIEW",
                    workflow_id=workflow.workflow_id,
                    aggregate_type=AggregateType.STANDALONE_REVIEW,
                    aggregate_id=review_id,
                    actor="bunshin-router",
                    expected_version=review.version,
                    idempotency_key=f"effect:{effect['effect_key']}:queue-review",
                ),
            )
            self._link_workflow(
                workflow.workflow_id,
                "LINK_STANDALONE_REVIEW",
                {"standalone_review_id": review_id},
                str(effect["effect_key"]),
                unit_of_work=connection,
            )
        return {}

    def _create_revision(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        previous = self._effect_snapshot(effect)
        revision_id = _derived_id("arch", str(effect["effect_key"]))
        source_epoch_id = str(previous.payload.get("source_execution_epoch_id") or "")
        with self.repository.transaction() as connection:
            WorkflowCoordinator(self.repository).begin_plan_revision(
                workflow_id=previous.workflow_id,
                unit_of_work=connection,
            )
            (connection or self.repository).transitions.dispatch(
                ActionEnvelope(
                    action_type="CREATE_ARCHITECTURE_REVISION",
                    workflow_id=previous.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision_id,
                    actor="bunshin-router",
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
            )
            if source_epoch_id:
                epoch = (connection or self.repository).snapshots.read_snapshot(
                    AggregateType.EXECUTION_EPOCH,
                    source_epoch_id,
                )
                if epoch is not None and epoch.state == "REPLAN_REQUIRED":
                    (connection or self.repository).transitions.dispatch(
                        ActionEnvelope(
                            action_type="REPLAN_REVISION_LINKED",
                            workflow_id=previous.workflow_id,
                            aggregate_type=AggregateType.EXECUTION_EPOCH,
                            aggregate_id=source_epoch_id,
                            actor="bunshin-router",
                            expected_version=epoch.version,
                            idempotency_key=(
                                f"effect:{effect['effect_key']}:replan-revision-linked"
                            ),
                            payload={"active_replan_revision_id": revision_id},
                        ),
                    )
            self._link_workflow(
                previous.workflow_id,
                "LINK_ARCHITECTURE_REVISION",
                {"architecture_revision_id": revision_id},
                str(effect["effect_key"]),
                unit_of_work=connection,
            )
        return {}

    def _compile_execution(
        self,
        *,
        workflow_id: str,
        manifest_ref: Mapping[str, Any],
        causation_key: str,
        source_epoch_id: str = "",
        initial_repair_bill_ref: Mapping[str, Any] | None = None,
        epoch_id: str = "",
    ) -> Mapping[str, Any]:
        record = self.repository.artifacts.read_artifact_record(str(manifest_ref.get("sha256") or ""))
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
        epoch_id = epoch_id or _execution_epoch_id(
            workflow_id=workflow_id,
            manifest_sha=ref.sha256,
            source_epoch_id=source_epoch_id,
            repair_bill_sha=str(
                dict(initial_repair_bill_ref or {}).get("sha256") or ""
            ),
        )
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is None:
            raise PermanentEffectError("execution preparation has no parent workflow")
        epoch = self.repository.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
        linked = str(workflow.payload.get("execution_epoch_id") or "")
        if epoch is not None and (epoch.workflow_id != workflow_id
                or epoch.payload.get("architecture_manifest_ref") != ref.to_dict()
                or epoch.payload.get("source_execution_epoch_id", source_epoch_id) != source_epoch_id):
            raise PermanentEffectError("execution epoch belongs to another compilation request")
        # A committed activation can be acknowledged without reopening a
        # paused or cancelled generation. Otherwise a parent control intent
        # fences the original compile effect before any materialization.
        if (linked == epoch_id and epoch is not None and epoch.state not in {"STARTING", "NOT_STARTED"}
                and epoch.payload.get("node_ids")):
            # The compiler's existing-epoch path is read-only and checks the
            # exact persisted graph membership before acknowledging replay.
            compiled = ExecutionCompiler(self.repository, self.service.contracts).compile_epoch(
                workflow_id=workflow_id, epoch_id=epoch_id, manifest_ref=ref,
                source_epoch_id=source_epoch_id, initial_repair_bill_ref=initial_repair_bill_ref,
            )
            return {"epoch_id": epoch_id, "node_ids": list(compiled.node_run_ids)}
        if (workflow.state in {"CANCEL_REQUESTED", "CANCELLED", "RESTARTING", "COMPLETED", "REJECTED"}
                or (linked and linked not in {source_epoch_id, epoch_id})):
            return {"epoch_id": epoch_id, "status": "superseded"}
        if epoch is not None and epoch.state in {"CANCELLED", "SUPERSEDED"}:
            return {"epoch_id": epoch_id, "status": "superseded"}
        if (workflow.state != "ACTIVE" or (epoch is not None and epoch.state in {
                "PAUSE_REQUESTED", "PAUSED", "TRIAGE_REQUIRED", "CANCEL_REQUESTED",
        })):
            raise DeferredEffectError("workflow control fences execution preparation")
        compilation = ExecutionCompiler(self.repository, self.service.contracts).compile_epoch(
            workflow_id=workflow_id,
            epoch_id=epoch_id,
            manifest_ref=ref,
            source_epoch_id=source_epoch_id,
            initial_repair_bill_ref=initial_repair_bill_ref,
        )
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is not None and workflow.payload.get("execution_epoch_id") != epoch_id:
            # Only historical compiled-but-unlinked epochs need this path.
            WorkflowCoordinator(self.repository).reconcile_activated_graph_link(
                workflow_id=workflow_id, epoch_id=epoch_id, source_epoch_id=source_epoch_id, actor="bunshin-recovery",
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
            replacement = self.repository.snapshots.read_snapshot(
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
        replacement = self.repository.snapshots.read_snapshot(
            AggregateType.WORKFLOW,
            replacement_id,
        )
        if source.payload.get("restart_cancel_requested") and replacement is None:
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="REPLACEMENT_WORKFLOW_ABORTED",
                    workflow_id=source.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=source.aggregate_id,
                    actor="bunshin-manager",
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
                    "operation": str(request.get("operation") or "review_then_execute"),
                    "execution_mode": str(request.get("execution_mode") or "planned"),
                    "references": list(request.get("references") or []),
                    "artifact_ref": manifest_ref,
                    "requirements_ref": requirements_ref,
                    "goal": str(request.get("goal") or ""),
                    "research_mode": str(request.get("research_mode") or "none"),
                    "actor": str(request.get("actor") or "pal"),
                    "source_channel": "bunshin-manager",
                },
                replacing_workflow_id=source.workflow_id,
            )
            replacement = self.repository.snapshots.read_snapshot(
                AggregateType.WORKFLOW,
                replacement_id,
            )
        latest = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, source.aggregate_id)
        if latest is None:
            raise RuntimeError("source workflow disappeared while creating replacement")
        if latest.state == "RESTARTING":
            if latest.payload.get("restart_cancel_requested"):
                if replacement is None:
                    raise RuntimeError("replacement workflow disappeared before cancellation")
                if "REQUEST_CANCEL" in self.repository.transitions.legal_actions(
                    AggregateType.WORKFLOW,
                    replacement.state,
                ):
                    self.service.control_workflow(
                        workflow_id=replacement.aggregate_id,
                        command="cancel",
                        actor="bunshin-manager",
                        source_channel="bunshin-manager",
                        reason="execution restart was cancelled before replacement activation",
                    )
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="REPLACEMENT_WORKFLOW_STARTED",
                    workflow_id=latest.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW,
                    aggregate_id=latest.aggregate_id,
                    actor="bunshin-manager",
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

    def _advance_graph(self, *, workflow_id: str, epoch_id: str, generation: int) -> None:
        if generation:
            installed = self.repository.cycles.read_graph_execution(workflow_id=workflow_id)
            if installed is None or installed.graph.generation != generation:
                return
        DagScheduler(self.repository).schedule_ready_nodes(workflow_id=workflow_id, epoch_id=epoch_id)
        WorkflowCoordinator(self.repository).complete_graph(
            workflow_id=workflow_id, epoch_id=epoch_id, generation=generation,
        )

    def _node_accepted(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        node = self._effect_snapshot(effect)
        self._advance_graph(
            workflow_id=node.workflow_id, epoch_id=str(node.payload.get("epoch_id") or ""),
            generation=int(node.payload.get("graph_generation") or 0),
        )
        return {}

    def _propagate_workflow_control(self, effect_type: str, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        with self.repository.transaction() as work:
            # Reconcile the current control episode, never the one in an old
            # delivery. Check its authority in the same write transaction as
            # all child requests and the acknowledgement of their settlement.
            workflow = work.snapshots.read_snapshot(AggregateType.WORKFLOW, str(effect["aggregate_id"]))
            if workflow is None or not control_effect_is_current(effect_type, workflow):
                return {"status": "superseded"}
            children = _active_control_children(work.queries.list_workflow_snapshots(workflow.workflow_id), workflow)
            intent = {
                "propagate_resume": None,
                "propagate_cancel": ControlIntent.CANCEL,
                "propagate_pause": ControlIntent.PAUSE,
                "freeze_workflow_children": ControlIntent.PAUSE,
            }[effect_type]
            for child in children:
                machine = machine_spec_for(child.aggregate_type)
                if intent is None:
                    action_type = "RESUME"
                    if action_type not in work.transitions.legal_actions(child.aggregate_type, child.state):
                        continue
                else:
                    if intent == ControlIntent.PAUSE and role_pause_must_drain(child, work.queries):
                        continue
                    if machine.control_disposition(intent, child.state) != ControlDisposition.REQUEST:
                        continue
                    action_type = machine.control_policies[intent].request_action
                work.transitions.dispatch(ActionEnvelope(
                    action_type=action_type, workflow_id=workflow.workflow_id,
                    aggregate_type=child.aggregate_type, aggregate_id=child.aggregate_id,
                    actor="bunshin-control", expected_version=child.version,
                    idempotency_key=f"control:{workflow.aggregate_id}:{workflow.version}:{child.aggregate_id}:{child.version}:{action_type}",
                ))
            if effect_type not in {"propagate_pause", "propagate_cancel"} or intent is None:
                return {}
            latest_children = _active_control_children(work.queries.list_workflow_snapshots(workflow.workflow_id), workflow)
            if all(
                machine_spec_for(child.aggregate_type).control_disposition(intent, child.state) == ControlDisposition.SETTLED
                for child in latest_children
            ):
                confirmation = "CHILDREN_PAUSED" if intent == ControlIntent.PAUSE else "CHILDREN_CANCELLED"
                work.transitions.dispatch(ActionEnvelope(
                    action_type=confirmation, workflow_id=workflow.workflow_id,
                    aggregate_type=AggregateType.WORKFLOW, aggregate_id=workflow.aggregate_id,
                    actor="bunshin-control", expected_version=workflow.version,
                    idempotency_key=f"control:{workflow.aggregate_id}:{workflow.version}:{confirmation}",
                ))
        return {}

    def _control_epoch_nodes(self, effect_type: str, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        intent = {"cancel_epoch_nodes": ControlIntent.CANCEL, "pause_epoch_nodes": ControlIntent.PAUSE,
                  "freeze_epoch_nodes": ControlIntent.PAUSE, "resume_epoch_nodes": None}[effect_type]
        with self.repository.transaction() as work:
            epoch = work.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, str(effect["aggregate_id"]))
            if epoch is None or not control_effect_is_current(effect_type, epoch):
                return {"status": "superseded"}
            if intent is None:
                workflow = work.snapshots.read_snapshot(AggregateType.WORKFLOW, epoch.workflow_id)
                if workflow is not None and workflow.state not in {"CREATED", "ACTIVE"}:
                    return {"status": "superseded"}
            DagScheduler(self.repository).control_nodes(intent, epoch, unit_of_work=work)
        return {}

    def _failed_effect_triage_action(
        self,
        effect: Mapping[str, Any],
        exc: Exception,
    ) -> ActionEnvelope | None:
        source = self._effect_snapshot(effect)
        target = source
        if "ENTER_TRIAGE" not in self.repository.transitions.legal_actions(
            source.aggregate_type,
            source.state,
        ):
            workflow = self.repository.snapshots.read_snapshot(
                AggregateType.WORKFLOW,
                source.workflow_id,
            )
            if workflow is None or "ENTER_TRIAGE" not in self.repository.transitions.legal_actions(
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
                "error": exception_report(exc),
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
            actor="bunshin-outbox",
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


@dataclass
class BunshinOutboxProcessor(WorkflowEffects):
    """Compatibility entry point for callers that constructed the combined processor."""
    worker_id: str = "bunshin-outbox"
    effect_lease_seconds: int = 120
    dispatcher: WorkflowEffectDispatcher = field(init=False)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.dispatcher = WorkflowEffectDispatcher(self, self.worker_id, self.effect_lease_seconds)

    async def _process_effect(self, effect: Mapping[str, Any]) -> str:
        return await self.dispatcher._process_effect(effect)

    async def process_once(self, *, limit: int = 10) -> dict[str, Any]:
        return await self.dispatcher.process_once(limit=limit)

    def start_available(self, *, max_concurrency: int) -> int:
        return self.dispatcher.start_available(max_concurrency=max_concurrency)

    async def stop_background(self) -> None:
        await self.dispatcher.stop_background()

    @property
    def active_background_count(self) -> int:
        return self.dispatcher.active_background_count


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
