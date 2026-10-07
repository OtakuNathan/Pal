from __future__ import annotations
import pal.bunshin.execution_values as _dependency_execution_values
from pal.bunshin.semantic_orchestration.review_results import _ref_from_mapping
from pal.bunshin.semantic_orchestration.workspace_safety import _raise_if_workspace_held
from pal.bunshin.semantic_orchestration.workspace_safety import _lease_is_live
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping
from pal.bunshin.artifacts import ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, LeaseConflict, StaleFencingToken, SubmissionInvariantError
from pal.bunshin.execution_values import workspace_content_fingerprint
from pal.bunshin.workspace_resources import WorkspaceLockRegistry
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.role_cleanup import RoleCleanup
from pal.bunshin.semantic_orchestration.verification_settlement import VerificationSettlement


@dataclass
class VerificationSnapshot:
    effect_reads: EffectReads
    role_cleanup: RoleCleanup
    verification_settlement: VerificationSettlement
    artifacts: ContentAddressedArtifactStore
    repository: BunshinV2Repository
    workspace_locks: WorkspaceLockRegistry

    def verification_input_snapshot(self, effect: Mapping[str, Any]) -> AggregateSnapshot:
        node = self.effect_reads.effect_snapshot(effect)
        causal = self.effect_reads.effect_causal_context(effect)
        pending_ref = dict(causal.get("pending_verification_ref") or {})
        if not pending_ref.get("sha256"):
            pending_ref = self.repository.queries.read_effect_pending_verification_ref(str(effect.get("event_id") or ""))
        if not pending_ref.get("sha256"):
            raise SubmissionInvariantError("verification effect has no causal pending submission")
        bound = replace(node, payload={**node.payload, "pending_verification_ref": pending_ref})
        if self.verification_settlement.settled_verification_result(bound) is not None:
            return bound
        if pending_ref != dict(node.payload.get("pending_verification_ref") or {}):
            raise SubmissionInvariantError("verification effect belongs to a superseded pending submission")
        return node

    async def quiesce_verifier_role(
        self,
        effect: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        node = self.verification_input_snapshot(effect)
        settled = self.verification_settlement.settled_verification_result(node)
        if settled is not None:
            return settled
        prepared = self.verification_settlement.prepared_dependency_result(node)
        if prepared is not None:
            return prepared
        pending_ref = _ref_from_mapping(node.payload.get("pending_verification_ref"))
        pending = dict(self.artifacts.read_json(pending_ref))
        invocation_id = str(pending.get("invocation_id") or "")
        lease_resource = str(pending.get("lease_resource_key") or "")
        fencing_token = int(pending.get("fencing_token") or 0)
        claimed_rebind = False

        def release_rebind() -> None:
            if not claimed_rebind:
                return
            try:
                self.repository.leases.release_lease(
                    lease_resource,
                    invocation_id,
                    fencing_token,
                )
            except (LeaseConflict, StaleFencingToken):
                pass

        try:
            self.repository.leases.assert_fencing_token(
                lease_resource,
                invocation_id,
                fencing_token,
            )
        except (LeaseConflict, StaleFencingToken):
            lease = self.repository.leases.read_lease(lease_resource)
            if lease is not None and _lease_is_live(lease):
                raise
            rebound = self.repository.leases.claim_lease(
                lease_resource,
                invocation_id,
                ttl_seconds=120,
                metadata={
                    "workflow_id": node.workflow_id,
                    "aggregate_type": AggregateType.DAG_NODE_RUN.value,
                    "aggregate_id": node.aggregate_id,
                    "role": "verifier_snapshot",
                    "workspace_path": str(pending.get("review_workspace") or ""),
                },
            )
            fencing_token = rebound.fencing_token
            claimed_rebind = True
        review_workspace = Path(str(pending.get("review_workspace") or ""))
        try:
            await self.role_cleanup.close_owned_process(
                invocation_id,
            )
            await self.role_cleanup.release_managed_lsp_workspace(review_workspace)
            _raise_if_workspace_held(
                review_workspace,
                "a live process still holds the verifier worktree",
            )
        except BaseException:
            release_rebind()
            raise
        lock_key = f"verification:{node.aggregate_id}"
        self.workspace_locks.release(lock_key)
        lock_path = self.workspace_locks.acquire(lock_key, review_workspace)
        try:
            _raise_if_workspace_held(
                review_workspace,
                "a process reached the verifier worktree during quiescing",
                manager_snapshot_lock=lock_path,
            )
            fingerprint = _dependency_execution_values.workspace_content_fingerprint(review_workspace)
            submitted_fingerprint = str(
                pending.get("submitted_workspace_fingerprint") or ""
            )
            if submitted_fingerprint and fingerprint != submitted_fingerprint:
                raise RuntimeError(
                    "verifier worktree changed after semantic submission"
                )
        except BaseException:
            self.workspace_locks.release(lock_key)
            release_rebind()
            raise
        current = self.repository.snapshots.read_snapshot(
            AggregateType.DAG_NODE_RUN,
            node.aggregate_id,
        )
        if current is None:
            self.workspace_locks.release(lock_key)
            release_rebind()
            raise SubmissionInvariantError("verification node disappeared while quiescing")
        try:
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="VERIFIER_QUIESCED",
                    workflow_id=current.workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN,
                    aggregate_id=current.aggregate_id,
                    actor="bunshin-v2-manager",
                    expected_version=current.version,
                    idempotency_key=f"effect:{effect['effect_key']}:quiesced",
                    payload={
                        "fencing_token": fencing_token,
                        "process_group_reaped": True,
                        "exclusive_workspace_lock": True,
                        "workspace_fingerprint": fingerprint,
                        "workspace_lock_path": str(lock_path),
                    },
                )
            )
        except BaseException:
            self.workspace_locks.release(lock_key)
            release_rebind()
            raise
        return {}

    def snapshot_semantic_verification(
        self,
        effect: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        node = self.verification_input_snapshot(effect)
        settled = self.verification_settlement.settled_verification_result(node)
        if settled is not None:
            return settled
        prepared = self.verification_settlement.prepared_dependency_result(node)
        if prepared is not None:
            return prepared
        pending_ref = _ref_from_mapping(node.payload.get("pending_verification_ref"))
        pending = dict(self.artifacts.read_json(pending_ref))
        review_workspace = Path(str(pending.get("review_workspace") or ""))
        review_scratch = Path(str(pending.get("review_scratch") or ""))
        candidate_ref = _ref_from_mapping(pending.get("candidate_ref"))
        candidate_digest = str(pending.get("candidate_digest") or "")
        candidate = dict(self.artifacts.read_json(candidate_ref))
        submission = dict(pending.get("submission") or {})
        lock_key = f"verification:{node.aggregate_id}"
        expected_fingerprint = str(node.payload.get("workspace_fingerprint") or "")
        if not self.workspace_locks.is_held(lock_key):
            _raise_if_workspace_held(
                review_workspace,
                "a live process still holds the verifier worktree",
            )
            if _dependency_execution_values.workspace_content_fingerprint(review_workspace) != expected_fingerprint:
                raise RuntimeError("verifier worktree changed after quiescing")
            self.workspace_locks.acquire(lock_key, review_workspace)
        try:
            if _dependency_execution_values.workspace_content_fingerprint(review_workspace) != expected_fingerprint:
                raise RuntimeError("verifier worktree changed while snapshotting")
            return self.verification_settlement.finalize_semantic_verification(
                node=node,
                pending=pending,
                submission=submission,
                candidate_ref=candidate_ref,
                candidate_digest=candidate_digest,
                candidate=candidate,
                review_workspace=review_workspace,
                review_scratch=review_scratch,
                execution_adapter=str(pending.get("execution_adapter") or ""),
            )
        finally:
            self.workspace_locks.release(lock_key)

    async def handle_snapshot_semantic_verification(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.snapshot_semantic_verification(effect)
