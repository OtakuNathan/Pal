from __future__ import annotations
import pal.bunshin.execution_values as _dependency_execution_values
from pal.bunshin.semantic_orchestration.role_inputs import _node_role_session_id
from pal.bunshin.semantic_orchestration.workspace_safety import _raise_if_workspace_held
from pal.bunshin.semantic_orchestration.workspace_safety import _lease_is_live
import asyncio
import hashlib
from dataclasses import dataclass, field
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, DeferredEffectError, LeaseConflict, StaleFencingToken
from pal.bunshin.execution_values import workspace_content_fingerprint
from pal.bunshin.workspace_resources import WorkspaceLockRegistry
from pal.bunshin.worker_processes import WorkerProcesses
from pal.bunshin.sessions import architect_session_id_for_revision
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.role_cleanup import RoleCleanup
from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts


@dataclass
class RoleLeases:
    effect_reads: EffectReads
    role_cleanup: RoleCleanup
    workflow_facts: WorkflowFacts
    processes: WorkerProcesses
    repository: BunshinRepository
    workspace_locks: WorkspaceLockRegistry

    _background_lease: ContextVar[tuple[str, str, str, int] | None] = field(
        default_factory=lambda: ContextVar("bunshin_background_business_lease", default=None),
        init=False, repr=False,
    )

    def bind_background_business_lease(
        self, effect: Mapping[str, Any], *, owner_id: str, resource_key: str, fencing_token: int,
    ) -> None:
        # A task-local binding survives awaits/finally without ever borrowing a
        # replacement task's latest aggregate ownership.
        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "")
        self._background_lease.set((effect_key, resource_key, owner_id, fencing_token))

    def release_background_business_lease(self, effect: Mapping[str, Any]) -> None:
        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "")
        bound = self._background_lease.get()
        if bound is not None and bound[0] == effect_key:
            _key, resource, owner, token = bound
        else:
            causal = self.effect_reads.effect_causal_context(effect)
            resource = str(causal.get("lease_resource_key") or "")
            owner = str(causal.get("active_worker_id") or "")
            token = int(causal.get("fencing_token") or 0)
        if not resource or not owner or not token:
            # Missing causal ownership never grants authority over latest.
            return
        snapshot = self.effect_reads.effect_snapshot(effect)
        same_owner = (
            str(snapshot.payload.get("lease_resource_key") or "") == resource
            and str(snapshot.payload.get("active_worker_id") or "") == owner
            and int(snapshot.payload.get("fencing_token") or 0) == token
        )
        if same_owner and snapshot.state in {"REVIEW_QUIESCING", "REVIEW_SNAPSHOTTING"}:
            # The same submitted verifier still needs its snapshot lease.
            return
        self.release_business_lease(
            resource_key=resource, owner_id=owner, fencing_token=token,
        )

    def release_business_lease(self, *, resource_key: str, owner_id: str, fencing_token: int) -> None:
        """Release and verify only this causal lease; do not suppress failure."""
        if not resource_key or not owner_id or fencing_token <= 0:
            raise ValueError("business lease release requires exact causal ownership")
        previous = self.repository.leases.read_lease(resource_key)
        if previous is None or (
            str(previous.get("owner_id") or "") != owner_id
            or int(previous.get("fencing_token") or 0) != fencing_token
        ):
            return
        try:
            self.repository.leases.release_lease(resource_key, owner_id, fencing_token)
        except (LeaseConflict, StaleFencingToken):
            # A concurrent exact release/replacement may win. Re-read rather
            # than acknowledge an unverified exception.
            pass
        current = self.repository.leases.read_lease(resource_key)
        if current is not None and (
            str(current.get("owner_id") or "") == owner_id
            and int(current.get("fencing_token") or 0) == fencing_token
        ):
            raise RuntimeError("retired incarnation still holds its business lease")

    async def ensure_node_effect_lease(
        self,
        node: AggregateSnapshot,
        *,
        action_type: str,
        activation: RoleActivation,
    ) -> AggregateSnapshot:
        if node.state in {"CANCEL_REQUESTED", "CANCELLED", "STALE", "PAUSE_REQUESTED", "PAUSED"}:
            raise DeferredEffectError("node effect incarnation is frozen")
        invocation_id = str(node.payload.get("active_worker_id") or "")
        lease_resource = str(node.payload.get("lease_resource_key") or "")
        fencing_token = int(node.payload.get("fencing_token") or 0)
        writer_role = activation.role == OrchestrationRole.IMPLEMENTATION
        expected_invocation_id = _node_role_session_id(node, activation)
        if invocation_id and invocation_id != expected_invocation_id:
            previous = self.repository.leases.read_lease(lease_resource)
            if previous is not None and _lease_is_live(previous):
                raise DeferredEffectError(
                    "node worker is still fenced by an obsolete logical session"
                )
            invocation_id = ""
            fencing_token = 0
        if invocation_id and lease_resource and fencing_token:
            if await self.reuse_or_retire_effect_lease(
                resource_key=lease_resource,
                owner_id=invocation_id,
                fencing_token=fencing_token,
                worker_label=f"node worker {node.aggregate_id}",
            ):
                await self.ensure_node_snapshot_lock(node)
                return node

        if writer_role:
            invocation_id = expected_invocation_id
            lease_resource = f"node:{node.aggregate_id}:writer"
        else:
            invocation_id = invocation_id or expected_invocation_id
            lease_resource = f"node:{node.aggregate_id}:review"

        previous = self.repository.leases.read_lease(lease_resource)
        if previous is not None and str(previous.get("owner_id") or "") and _lease_is_live(previous):
            raise LeaseConflict(f"node effect lease is active under {previous.get('owner_id')}")
        workspace = Path(str(node.payload.get("workspace_path") or ""))
        if writer_role:
            await self.role_cleanup.release_managed_lsp_workspace(workspace)
            _raise_if_workspace_held(workspace, "expired node worker still holds its worktree")

        lease = self.repository.leases.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": node.workflow_id,
                "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                "aggregate_id": node.aggregate_id,
                "node_run_id": node.aggregate_id,
                "role": activation.role.value,
                "mode": activation.mode.value,
                "workspace_path": str(workspace),
            },
        )
        try:
            rebound = self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=node.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=node.aggregate_id,
                    actor="bunshin-v2-recovery",
                    expected_version=node.version,
                    idempotency_key=f"rebind:{node.aggregate_id}:{action_type}:{lease.fencing_token}",
                    payload={
                        "fencing_token": lease.fencing_token,
                        "active_worker_id": invocation_id,
                        "lease_resource_key": lease_resource,
                        "active_role": activation.role.value,
                        "active_role_mode": activation.mode.value,
                    },
                )
            ).snapshot
            await self.ensure_node_snapshot_lock(rebound)
            return rebound
        except BaseException:
            try:
                self.repository.leases.release_lease(
                    lease_resource,
                    invocation_id,
                    lease.fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
            raise

    async def ensure_node_snapshot_lock(self, node: AggregateSnapshot) -> None:
        if node.state != "SNAPSHOTTING" or self.workspace_locks.is_held(node.aggregate_id):
            return
        workspace = Path(str(node.payload.get("workspace_path") or ""))
        await self.role_cleanup.release_managed_lsp_workspace(workspace)
        _raise_if_workspace_held(
            workspace,
            "a live process still holds the candidate worktree",
        )
        expected = str(node.payload.get("workspace_fingerprint") or "")
        current = self.workflow_facts.workspace_fingerprint(node, workspace)
        if not expected or current != expected:
            raise RuntimeError("candidate worktree changed while snapshot worker was unavailable")
        self.workspace_locks.acquire(node.aggregate_id, workspace)

    async def ensure_standalone_review_lease(self, review: AggregateSnapshot) -> AggregateSnapshot:
        invocation_id = str(review.payload.get("active_worker_id") or "")
        lease_resource = str(review.payload.get("lease_resource_key") or f"standalone-review:{review.aggregate_id}")
        fencing_token = int(review.payload.get("fencing_token") or 0)
        if invocation_id and fencing_token:
            if await self.reuse_or_retire_effect_lease(
                resource_key=lease_resource,
                owner_id=invocation_id,
                fencing_token=fencing_token,
                worker_label=f"standalone reviewer {review.aggregate_id}",
            ):
                return review
        invocation_id = invocation_id or f"inv_{hashlib.sha256(f'{review.aggregate_id}:review'.encode()).hexdigest()[:24]}"
        previous = self.repository.leases.read_lease(lease_resource)
        if previous is not None and str(previous.get("owner_id") or "") and _lease_is_live(previous):
            raise LeaseConflict(f"standalone review lease is active under {previous.get('owner_id')}")
        lease = self.repository.leases.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": review.workflow_id,
                "aggregate_type": AggregateType.STANDALONE_REVIEW.value,
                "aggregate_id": review.aggregate_id,
                "role": "reviewer",
            },
        )
        try:
            return self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="REBIND_REVIEWER",
                    workflow_id=review.workflow_id,
                    aggregate_type=AggregateType.STANDALONE_REVIEW,
                    aggregate_id=review.aggregate_id,
                    actor="bunshin-v2-recovery",
                    expected_version=review.version,
                    idempotency_key=f"rebind:{review.aggregate_id}:reviewer:{lease.fencing_token}",
                    payload={
                        "fencing_token": lease.fencing_token,
                        "active_worker_id": invocation_id,
                        "lease_resource_key": lease_resource,
                        "active_role": OrchestrationRole.REVIEWER.value,
                        "active_role_mode": RoleMode.STANDALONE.value,
                    },
                )
            ).snapshot
        except BaseException:
            try:
                self.repository.leases.release_lease(
                    lease_resource,
                    invocation_id,
                    lease.fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
            raise

    async def ensure_architecture_effect_lease(
        self,
        revision: AggregateSnapshot,
        *,
        action_type: str,
    ) -> AggregateSnapshot:
        invocation_id = architect_session_id_for_revision(
            revision.workflow_id,
            revision.aggregate_id,
            revision.payload,
        )
        lease_resource = f"architecture:{revision.aggregate_id}:writer"
        fencing_token = int(revision.payload.get("fencing_token") or 0)
        active_worker = str(revision.payload.get("active_worker_id") or invocation_id)
        if fencing_token:
            if await self.reuse_or_retire_effect_lease(
                resource_key=lease_resource,
                owner_id=active_worker,
                fencing_token=fencing_token,
                worker_label=f"architecture worker {revision.aggregate_id}",
            ):
                if revision.state == "ARCHITECT_SNAPSHOTTING" and not self.workspace_locks.is_held(
                    revision.aggregate_id
                ):
                    workspace = Path(str(revision.payload.get("architecture_workspace_path") or ""))
                    await self.role_cleanup.release_managed_lsp_workspace(workspace)
                    _raise_if_workspace_held(
                        workspace,
                        "a live process still holds the architecture worktree",
                    )
                    expected = str(revision.payload.get("workspace_fingerprint") or "")
                    current = _dependency_execution_values.workspace_content_fingerprint(workspace)
                    if not expected or current != expected:
                        raise RuntimeError("architecture worktree changed while snapshot worker was unavailable")
                    self.workspace_locks.acquire(revision.aggregate_id, workspace)
                return revision
        previous = self.repository.leases.read_lease(lease_resource)
        if previous is not None and str(previous.get("owner_id") or "") and _lease_is_live(previous):
            raise LeaseConflict(f"architecture effect lease is active under {previous.get('owner_id')}")
        workspace = Path(str(revision.payload.get("architecture_workspace_path") or ""))
        await self.role_cleanup.release_managed_lsp_workspace(workspace)
        _raise_if_workspace_held(workspace, "expired architect still holds the architecture worktree")
        lease = self.repository.leases.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": revision.workflow_id,
                "aggregate_id": revision.aggregate_id,
                "stage": "architecture_snapshot",
                "workspace_path": str(workspace),
            },
        )
        try:
            rebound = self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type=action_type,
                    workflow_id=revision.workflow_id,
                    aggregate_type=AggregateType.ARCHITECTURE_REVISION,
                    aggregate_id=revision.aggregate_id,
                    actor="bunshin-v2-recovery",
                    expected_version=revision.version,
                    idempotency_key=f"architecture-rebind:{revision.aggregate_id}:{action_type}:{lease.fencing_token}",
                    payload={
                        "fencing_token": lease.fencing_token,
                        "active_worker_id": invocation_id,
                        "lease_resource_key": lease_resource,
                        "active_role": OrchestrationRole.ARCHITECT.value,
                        "active_role_mode": (
                            RoleMode.REVISION.value
                            if self.workflow_facts.revision_input_base_manifest_ref(revision) is not None
                            else RoleMode.AUTHOR.value
                        ),
                    },
                )
            ).snapshot
            if rebound.state == "ARCHITECT_SNAPSHOTTING" and not self.workspace_locks.is_held(revision.aggregate_id):
                expected = str(rebound.payload.get("workspace_fingerprint") or "")
                current = _dependency_execution_values.workspace_content_fingerprint(workspace)
                if not expected or current != expected:
                    raise RuntimeError("architecture worktree changed while snapshot worker was unavailable")
                self.workspace_locks.acquire(revision.aggregate_id, workspace)
            return rebound
        except BaseException:
            try:
                self.repository.leases.release_lease(
                    lease_resource,
                    invocation_id,
                    lease.fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass
            raise

    async def reuse_or_retire_effect_lease(
        self,
        *,
        resource_key: str,
        owner_id: str,
        fencing_token: int,
        worker_label: str,
    ) -> bool:
        """Reuse a fresh admission lease, or fence an unmanaged prior worker."""

        try:
            self.repository.leases.assert_fencing_token(resource_key, owner_id, fencing_token)
            # Ownership ends when the in-memory owner unregisters, never from
            # durable numeric process metadata.
            if self.processes.contains(owner_id):
                raise LeaseConflict(f"{worker_label} is already active in this manager")
            # Admission and process spawn are separate effects. A manager that
            # has no in-memory owner has no process authority to recover. Keep
            # the logical lease and let workspace-holder checks fence effects.
            self.repository.leases.renew_lease(
                resource_key,
                owner_id,
                fencing_token,
                ttl_seconds=120,
            )
            return True
        except StaleFencingToken:
            return False

    async def lease_heartbeat(self, resource_key: str, owner_id: str, fencing_token: int) -> None:
        while True:
            await asyncio.sleep(30)
            self.repository.leases.renew_lease(
                resource_key,
                owner_id,
                fencing_token,
                ttl_seconds=120,
            )
