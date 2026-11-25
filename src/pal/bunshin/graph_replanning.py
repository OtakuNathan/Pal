"""Freeze an execution epoch and collect its complete batch of architecture findings."""
from __future__ import annotations

from typing import Any, Mapping

from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.bunshin.replan import collect_architecture_finding_batch
from pal.bunshin.workflow_effect_context import WorkflowEffectContext, _derived_id
from pal.bunshin.workflow_runtime import WorkflowCoordinator


class GraphReplanning(WorkflowEffectContext):
    def request_epoch_replan(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        source = self._effect_snapshot(effect)
        if source.aggregate_type != AggregateType.DAG_NODE_RUN:
            raise ValueError("request_epoch_replan must originate from a DAG node")
        epoch_id = str(source.payload.get("epoch_id") or "")
        epoch = self.repository.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
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
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="ENTER_TRIAGE",
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.EXECUTION_EPOCH,
                    aggregate_id=epoch.aggregate_id,
                    actor="bunshin-manager",
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
        self.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="REGISTER_REPLAN_FINDING",
                workflow_id=source.workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id=epoch_id,
                actor="bunshin-manager",
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

    def freeze_epoch_for_replan(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        epoch = self._effect_snapshot(effect)
        if epoch.aggregate_type != AggregateType.EXECUTION_EPOCH:
            raise ValueError("freeze_epoch_for_replan requires an execution epoch")
        # The reconciler checks the current REPLAN_COLLECTING state before
        # touching nodes. A delayed freeze must not mutate a paused epoch.
        self.reconcile_replan_collection(epoch)
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
            legal = self.repository.transitions.legal_actions(AggregateType.DAG_NODE_RUN, node.state)
            action_type = (
                "MARK_STALE"
                if "MARK_STALE" in legal
                else "REQUEST_STALE"
                if "REQUEST_STALE" in legal
                else ""
            )
            if not action_type:
                continue
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="bunshin-replan",
                    expected_version=node.version,
                    idempotency_key=f"replan-freeze:{epoch.aggregate_id}:{node.aggregate_id}:{node.version}",
                    payload={"stale_reason_ref": finding_ref},
                )
            )

    def reconcile_replan_collections(self, workflow_id: str) -> None:
        if not workflow_id:
            return
        for epoch in self.repository.queries.collecting_replan_epochs(workflow_id):
            self.reconcile_replan_collection(epoch)

    def reconcile_replan_collection(self, epoch: AggregateSnapshot) -> None:
        current = self.repository.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch.aggregate_id)
        if current is None or current.state != "REPLAN_COLLECTING":
            return
        self._stale_non_drain_nodes(current)
        current = self.repository.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch.aggregate_id)
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
        latest = self.repository.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, current.aggregate_id)
        if latest is None or latest.state != "REPLAN_COLLECTING":
            return
        self.repository.transitions.dispatch(
            ActionEnvelope(
                action_type="REPLAN_BATCH_READY",
                workflow_id=latest.workflow_id,
                aggregate_type=AggregateType.EXECUTION_EPOCH,
                aggregate_id=latest.aggregate_id,
                actor="bunshin-manager",
                expected_version=latest.version,
                idempotency_key=f"replan-batch-ready:{latest.aggregate_id}:{batch.artifact_ref.sha256}",
                payload={
                    "replan_finding_batch_ref": batch.artifact_ref.to_dict(),
                    "replan_finding_fingerprints": list(batch.finding_fingerprints),
                    "requirements_ref": requirements_ref,
                },
            )
        )

    def create_replan_revision(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        epoch = self._effect_snapshot(effect)
        if epoch.aggregate_type != AggregateType.EXECUTION_EPOCH or epoch.state != "REPLAN_REQUIRED":
            return {}
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, epoch.workflow_id)
        if workflow is None:
            raise ValueError("replan epoch has no workflow")
        generation = int(epoch.payload.get("replan_generation") or 0)
        revision_id = _derived_id("arch", f"replan:{epoch.aggregate_id}:{generation}")
        batch_ref = dict(epoch.payload.get("replan_finding_batch_ref") or {})
        with self.repository.transaction() as connection:
            plan_cycle = WorkflowCoordinator(self.repository).begin_plan_revision(
                workflow_id=epoch.workflow_id,
                unit_of_work=connection,
            )
            (connection or self.repository).transitions.dispatch(
                ActionEnvelope(
                    action_type="CREATE_ARCHITECTURE_REVISION",
                    workflow_id=epoch.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision_id,
                    actor="bunshin-manager",
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
            )
            latest = (connection or self.repository).snapshots.read_snapshot(
                AggregateType.EXECUTION_EPOCH,
                epoch.aggregate_id,
            )
            if latest is not None and latest.state == "REPLAN_REQUIRED":
                (connection or self.repository).transitions.dispatch(
                    ActionEnvelope(
                        action_type="REPLAN_REVISION_LINKED",
                        workflow_id=epoch.workflow_id,
                        aggregate_type=AggregateType.EXECUTION_EPOCH,
                        aggregate_id=epoch.aggregate_id,
                        actor="bunshin-manager",
                        expected_version=latest.version,
                        idempotency_key=f"replan-revision-linked:{epoch.aggregate_id}:{generation}",
                        payload={"active_replan_revision_id": revision_id},
                    ),
                )
            self._link_workflow(
                epoch.workflow_id,
                "LINK_ARCHITECTURE_REVISION",
                {"architecture_revision_id": revision_id},
                f"replan:{epoch.aggregate_id}:{generation}",
                unit_of_work=connection,
            )
        return {"architecture_revision_id": revision_id, "result_artifact_ref": batch_ref}

    def _epoch_nodes(self, epoch: AggregateSnapshot) -> list[AggregateSnapshot]:
        return [
            item
            for item in self.repository.queries.list_workflow_snapshots(epoch.workflow_id)
            if item.aggregate_type == AggregateType.DAG_NODE_RUN
            and str(item.payload.get("epoch_id") or "") == epoch.aggregate_id
        ]
