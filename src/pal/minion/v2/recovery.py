from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.lsp.ipc import LspManagerClient
from pal.minion.v2.contracts import ActionEnvelope, AggregateType
from pal.minion.v2.execution import workspace_process_holders
from pal.minion.v2.orchestration import reconcile_control_requests
from pal.minion.v2.replan import architecture_revision_finding_value
from pal.minion.v2.service import MinionV2WorkflowService


@dataclass
class MinionV2Recovery:
    service: MinionV2WorkflowService

    @property
    def repository(self):
        return self.service.repository

    def recover(self) -> dict[str, Any]:
        recovered_leases: list[str] = []
        triaged: list[str] = []
        for lease in self.repository.expired_leases():
            metadata = dict(lease.get("metadata") or {})
            process_group = int(metadata.get("process_group_id") or 0)
            if process_group <= 0 and str(lease.get("resource_key") or "").startswith(
                "assignment:"
            ):
                attempt = self.repository.read_worker_attempt(
                    str(lease.get("owner_id") or "")
                )
                process_group = int(dict(attempt or {}).get("process_group_id") or 0)
            worktree = Path(str(metadata.get("workspace_path") or "")) if metadata.get("workspace_path") else None
            reaped = self._kill_and_reap(process_group)
            if reaped and worktree is not None:
                try:
                    LspManagerClient(
                        self.service.runtime_root,
                        request_timeout_seconds=15.0,
                    ).release_workspace_sync(worktree)
                except Exception:
                    pass
            holders = workspace_process_holders(worktree) if worktree is not None else ()
            clean_worktree = not holders
            if reaped and clean_worktree:
                if self.repository.clear_expired_lease(
                    str(lease["resource_key"]),
                    int(lease["fencing_token"]),
                ):
                    recovered_leases.append(str(lease["resource_key"]))
                continue
            aggregate_type_text = str(metadata.get("aggregate_type") or "")
            aggregate_id = str(metadata.get("aggregate_id") or "")
            if aggregate_type_text and aggregate_id:
                aggregate_type = AggregateType(aggregate_type_text)
                snapshot = self.repository.read_snapshot(aggregate_type, aggregate_id)
                if snapshot is not None and "ENTER_TRIAGE" in self.repository.engine.legal_actions(aggregate_type, snapshot.state):
                    failure_ref = self.service.artifacts.put_json(
                        {
                            "reason": "expired worker could not be safely reaped",
                            "lease": lease,
                            "process_group_reaped": reaped,
                            "worktree_clean": clean_worktree,
                            "workspace_holders": [item.to_dict() for item in holders],
                        },
                        artifact_type="RecoveryFailureArtifact",
                    )
                    self.repository.dispatch(
                        ActionEnvelope(
                            action_type="ENTER_TRIAGE",
                            workflow_id=snapshot.workflow_id,
                            aggregate_type=aggregate_type,
                            aggregate_id=aggregate_id,
                            actor="minion-v2-recovery",
                            expected_version=snapshot.version,
                            idempotency_key=f"recovery:{lease['resource_key']}:{lease['fencing_token']}",
                            payload={
                                "failure_artifact_ref": failure_ref.to_dict(),
                                "blocker": {"kind": "zombie_worker", "failure_artifact_ref": failure_ref.to_dict()},
                            },
                        )
                    )
                    triaged.append(aggregate_id)
        reconciled_replans = self._recover_duplicate_replans()
        reconciled_replans.extend(self._resume_replan_collections())
        reconciled_controls: list[str] = []
        for workflow_id in self.repository.workflow_ids():
            before = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
            reconcile_control_requests(self.repository, workflow_id)
            after = self.repository.read_snapshot(AggregateType.WORKFLOW, workflow_id)
            if before is not None and after is not None and after.version != before.version:
                reconciled_controls.append(workflow_id)
            triaged.extend(
                item["aggregate_id"]
                for item in self.service.triage_orphaned_work_aggregates(
                    workflow_id=workflow_id,
                    actor="minion-v2-recovery",
                )
            )
        rebuilt = self.repository.rebuild_workflow_projections()
        orphaned = self.repository.orphaned_workflow_ids()
        for workflow_id in orphaned:
            projection = self.repository.read_workflow_projection(workflow_id) or {}
            aggregate_type_text = str(projection.get("active_aggregate_type") or AggregateType.WORKFLOW.value)
            aggregate_id = str(projection.get("active_aggregate_id") or workflow_id)
            aggregate_type = AggregateType(aggregate_type_text)
            snapshot = self.repository.read_snapshot(aggregate_type, aggregate_id)
            if snapshot is None or "ENTER_TRIAGE" not in self.repository.engine.legal_actions(aggregate_type, snapshot.state):
                continue
            self.repository.dispatch(
                ActionEnvelope(
                    action_type="ENTER_TRIAGE",
                    workflow_id=workflow_id,
                    aggregate_type=aggregate_type,
                    aggregate_id=aggregate_id,
                    actor="minion-v2-recovery",
                    expected_version=snapshot.version,
                    idempotency_key=f"orphan-triage:{aggregate_type.value}:{aggregate_id}:{snapshot.version}",
                    payload={"blocker": {"kind": "orphaned_workflow", "reason": "no live lease, pending outbox effect, or explicit wait"}},
                )
            )
            triaged.append(aggregate_id)
        return {
            "recovered_lease_count": len(recovered_leases),
            "recovered_leases": recovered_leases,
            "rebuilt_projection_count": rebuilt,
            "reconciled_replan_epochs": sorted(set(reconciled_replans)),
            "reconciled_control_workflows": sorted(set(reconciled_controls)),
            "triaged_aggregate_ids": sorted(set(triaged)),
        }

    def _recover_duplicate_replans(self) -> list[str]:
        reconciled: list[str] = []
        terminal_revision_states = {"ACCEPTED", "REJECTED", "CANCELLED"}
        for workflow_id in self.repository.workflow_ids():
            snapshots = list(self.repository.list_workflow_snapshots(workflow_id))
            revisions = [
                item
                for item in snapshots
                if item.aggregate_type == AggregateType.ARCHITECTURE_REVISION
                and str(item.payload.get("source_execution_epoch_id") or "")
            ]
            grouped: dict[tuple[str, str], list[Any]] = {}
            for revision in revisions:
                epoch_id = str(revision.payload.get("source_execution_epoch_id") or "")
                base_sha = str(
                    dict(revision.payload.get("base_architecture_manifest_ref") or {}).get("sha256")
                    or ""
                )
                grouped.setdefault((epoch_id, base_sha), []).append(revision)
            for (epoch_id, _base_sha), group in grouped.items():
                active = [item for item in group if item.state not in terminal_revision_states]
                if len(active) <= 1:
                    continue
                if any(item.state == "ACCEPTED" for item in group):
                    epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
                    if epoch is not None and "ENTER_TRIAGE" in self.repository.engine.legal_actions(
                        AggregateType.EXECUTION_EPOCH,
                        epoch.state,
                    ):
                        failure_ref = self.service.artifacts.put_json(
                            {
                                "reason": "duplicate replan revisions include an accepted revision",
                                "source_execution_epoch_id": epoch_id,
                                "revision_states": {
                                    item.aggregate_id: item.state for item in group
                                },
                            },
                            artifact_type="DuplicateAcceptedReplanArtifact",
                        )
                        self.repository.dispatch(
                            ActionEnvelope(
                                action_type="ENTER_TRIAGE",
                                workflow_id=workflow_id,
                                aggregate_type=AggregateType.EXECUTION_EPOCH,
                                aggregate_id=epoch.aggregate_id,
                                actor="minion-v2-recovery",
                                expected_version=epoch.version,
                                idempotency_key=f"duplicate-accepted-replan:{epoch.aggregate_id}:{epoch.version}",
                                payload={
                                    "failure_artifact_ref": failure_ref.to_dict(),
                                    "blocker": {"kind": "duplicate_accepted_replan"},
                                },
                            )
                        )
                    continue
                finding_entries = self._replan_recovery_entries(active)
                if not finding_entries:
                    continue
                for revision in active:
                    self.repository.expire_human_decisions_for_revision(
                        workflow_id=workflow_id,
                        architecture_revision_id=revision.aggregate_id,
                    )
                    if "REQUEST_CANCEL" not in self.repository.engine.legal_actions(
                        AggregateType.ARCHITECTURE_REVISION,
                        revision.state,
                    ):
                        continue
                    self.repository.dispatch(
                        ActionEnvelope(
                            action_type="REQUEST_CANCEL",
                            workflow_id=workflow_id,
                            aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                            aggregate_id=revision.aggregate_id,
                            actor="minion-v2-recovery",
                            expected_version=revision.version,
                            idempotency_key=f"recover-duplicate-replan:{revision.aggregate_id}:{revision.version}",
                        )
                    )
                epoch = self.repository.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
                if epoch is None or epoch.state != "REPLAN_REQUIRED":
                    continue
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="REOPEN_REPLAN_COLLECTION",
                        workflow_id=workflow_id,
                        aggregate_type=AggregateType.EXECUTION_EPOCH,
                        aggregate_id=epoch.aggregate_id,
                        actor="minion-v2-recovery",
                        expected_version=epoch.version,
                        idempotency_key=f"recover-duplicate-replan:{epoch.aggregate_id}:{epoch.version}",
                        payload={"finding_entries": finding_entries},
                    )
                )
                reconciled.append(epoch.aggregate_id)
        return sorted(set(reconciled))

    def _resume_replan_collections(self) -> list[str]:
        resumed: list[str] = []
        for workflow_id in self.repository.workflow_ids():
            for snapshot in self.repository.list_workflow_snapshots(workflow_id):
                if (
                    snapshot.aggregate_type != AggregateType.EXECUTION_EPOCH
                    or snapshot.state != "REPLAN_COLLECTING"
                ):
                    continue
                self.repository.dispatch(
                    ActionEnvelope(
                        action_type="RECONCILE_REPLAN_COLLECTION",
                        workflow_id=workflow_id,
                        aggregate_type=AggregateType.EXECUTION_EPOCH,
                        aggregate_id=snapshot.aggregate_id,
                        actor="minion-v2-recovery",
                        expected_version=snapshot.version,
                        idempotency_key=f"recover-replan-collection:{snapshot.aggregate_id}:{snapshot.version}",
                    )
                )
                resumed.append(snapshot.aggregate_id)
        return resumed

    def _replan_recovery_entries(self, revisions: list[Any]) -> list[dict[str, Any]]:
        entries: dict[str, dict[str, Any]] = {}
        for revision in revisions:
            finding_value = architecture_revision_finding_value(revision.payload)
            if not isinstance(finding_value, dict) or not finding_value.get("sha256"):
                continue
            finding_ref = dict(finding_value)
            record = self.repository.read_artifact_record(str(finding_ref["sha256"]))
            refs = [finding_ref]
            if record and str(record.get("artifact_type") or "") == "ArchitectureFindingBatchArtifact":
                payload = dict(self.service.artifacts.read_json(finding_ref))
                refs = [
                    dict(item)
                    for group in list(payload.get("finding_groups") or [])
                    for item in list(dict(group or {}).get("repair_bill_refs") or [])
                    if isinstance(item, dict) and item.get("sha256")
                ]
            for ref in refs:
                digest = str(ref.get("sha256") or "")
                entries.setdefault(
                    digest,
                    {
                        "finding_artifact_ref": ref,
                        "source_node": "recovered_replan",
                        **(
                            {"requirement_patch_ref": dict(revision.payload["requirement_patch_ref"])}
                            if revision.payload.get("requirement_patch_ref")
                            else {}
                        ),
                        **(
                            {"revised_requirements_ref": dict(revision.payload["requirements_ref"])}
                            if revision.payload.get("requirement_patch_ref")
                            and revision.payload.get("requirements_ref")
                            else {}
                        ),
                    },
                )
        return [entries[digest] for digest in sorted(entries)]

    @staticmethod
    def _kill_and_reap(process_group: int, *, timeout_seconds: float = 2.0) -> bool:
        if process_group <= 0:
            return True
        try:
            os.killpg(process_group, signal.SIGTERM)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        try:
            os.killpg(process_group, signal.SIGKILL)
        except ProcessLookupError:
            return True
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            try:
                os.killpg(process_group, 0)
            except ProcessLookupError:
                return True
            time.sleep(0.05)
        return False
