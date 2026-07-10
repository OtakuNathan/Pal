from __future__ import annotations

import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.minion.v2.contracts import ActionEnvelope, AggregateType
from pal.minion.v2.execution import worktree_has_live_processes
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
            worktree = Path(str(metadata.get("worktree_path") or "")) if metadata.get("worktree_path") else None
            reaped = self._kill_and_reap(process_group)
            clean_worktree = worktree is None or not worktree_has_live_processes(worktree)
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
                    payload={"blocker": {"kind": "orphaned_workflow", "reason": "no lease, outbox, queued work, or explicit wait"}},
                )
            )
            triaged.append(aggregate_id)
        return {
            "recovered_lease_count": len(recovered_leases),
            "recovered_leases": recovered_leases,
            "rebuilt_projection_count": rebuilt,
            "triaged_aggregate_ids": sorted(set(triaged)),
        }

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
