"""Execute the reviewed capture/close/apply refinement of dependency repair.

The graph ledger owns admission and the finite frontier. Domain actions own
aggregate projections. Neither logical CHECKING nor a stopped subprocess alone
is a closure proof: setup, receipt cut, corpus, workspace and both lease fences
must all be accounted for before the graph can release any repair.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from pal.bunshin.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, DeferredEffectError, SubmissionInvariantError
from pal.bunshin.dependency_repair_protocol import DependencyRepairDeferred, RepairClosure, RepairIncarnation, RepairIntent
from pal.bunshin.graph_executor import GraphExecution, GraphExecutionState
from pal.bunshin.repository import BunshinV2Repository
from pal.bunshin.semantic_orchestration.dependency_repair_capture import DependencyRepairCapture
from pal.bunshin.semantic_orchestration.dependency_repair_facts import append_refs, current_attempt_id, freeze_incarnation, frozen_node, node_name, role_for_incarnation
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.role_cleanup import RoleCleanup
from pal.bunshin.semantic_orchestration.role_leases import RoleLeases
from pal.bunshin.semantic_orchestration.workspace_safety import _lease_is_live, _raise_if_workspace_held
from pal.bunshin.unit_of_work import BunshinUnitOfWork
from pal.bunshin.workspace_resources import WorkspaceLockRegistry


@dataclass
class DependencyRepairRuntime:
    artifacts: ContentAddressedArtifactStore
    repository: BunshinV2Repository
    effect_reads: EffectReads
    cleanup: RoleCleanup
    leases: RoleLeases
    workspace_locks: WorkspaceLockRegistry
    collector: DependencyRepairCapture

    def execution(self, workflow_id: str) -> GraphExecution:
        execution = self.repository.cycles.read_graph_execution(workflow_id=workflow_id)
        if execution is None:
            raise SubmissionInvariantError("dependency repair requires the current executable graph")
        return execution

    def owns(self, node: AggregateSnapshot) -> bool:
        execution = self.repository.cycles.read_graph_execution(workflow_id=node.workflow_id)
        pending = execution.dependency_repairs.pending if execution else None
        return bool(pending and any(item.aggregate_id == node.aggregate_id for item in pending.frontier.values()))

    async def control_cleanup(self, node: AggregateSnapshot, *, confirm: bool) -> Mapping[str, Any]:
        from pal.bunshin.semantic_orchestration.dependency_repair_control import retire_repair_control
        await retire_repair_control(node=node, execution=self.execution(node.workflow_id),
            repository=self.repository, artifacts=self.artifacts, cleanup=self.cleanup,
            leases=self.leases, workspace_locks=self.workspace_locks, confirm=confirm)
        return {"status": "control_quiesced"}

    def register(self, node: AggregateSnapshot, capture_ref: ArtifactRef) -> Mapping[str, Any]:
        """Persist preparation even when an independent current cohort defers it."""
        capture = dict(self.artifacts.read_json(capture_ref))
        with self.repository.transaction() as work:
            current = work.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, node.aggregate_id)
            if current is None or current.version != node.version or current.state != "REVIEW_SNAPSHOTTING":
                raise SubmissionInvariantError("dependency preparation no longer owns its snapshot")
            execution = work.cycles.read_graph_execution(workflow_id=node.workflow_id)
            if execution is None:
                raise SubmissionInvariantError("dependency preparation has no executable graph")
            store = ContentAddressedArtifactStore(self.artifacts.runtime_root, work.transitions.artifacts)
            incarnation, frozen_ref = freeze_incarnation(self.repository, store, execution, node)
            capture_ref = store.put_json({
                **capture, "incarnation_key": incarnation.key,
                "invocation_id": incarnation.lease_owner,
                "lease_resource_key": incarnation.lease_resource,
                "fencing_token": incarnation.fencing_token,
                "snapshot_lease_resource": incarnation.snapshot_lease_resource,
                "snapshot_lease_owner": incarnation.snapshot_lease_owner,
                "snapshot_fencing_token": incarnation.snapshot_fencing_token,
            },
                                                  artifact_type="DependencyRepairCaptureArtifact",
                                                  child_refs=((capture_ref.sha256, "prepared_evidence"),))
            capture = dict(self.artifacts.read_json(capture_ref))
            try:
                execution = self._join(work, execution, node, incarnation, frozen_ref, capture, capture_ref)
            except DependencyRepairDeferred:
                # This reporter was outside the original actual closure. Its
                # immutable report remains ready for a subsequent cohort.
                pass
            work.transitions.dispatch(ActionEnvelope(
                action_type="REGISTER_DEPENDENCY_REPAIR", workflow_id=node.workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id,
                actor="bunshin-v2-manager", expected_version=current.version,
                idempotency_key=f"dependency-repair:prepare:{capture_ref.sha256}",
                payload={"dependency_repair_capture_ref": capture_ref.to_dict(),
                         "dependency_repair_incarnation_ref": frozen_ref.to_dict(),
                         "dependency_repair_source_pending_ref": dict(capture.get("source_pending_ref") or {})},
            ))
            work.cycles.store_graph_execution(workflow_id=node.workflow_id, execution=execution)
        return {"provider_request_id": str(capture.get("invocation_id") or ""),
                "result_artifact_ref": dict(capture["report_ref"])}

    def _nodes(self, execution: GraphExecution, workflow_id: str) -> dict[str, AggregateSnapshot]:
        return {node_name(node): node for node in self.repository.queries.list_workflow_snapshots(workflow_id)
                if node.aggregate_type == AggregateType.DAG_NODE_RUN
                and int(node.payload.get("graph_generation") or 0) == execution.graph.generation
                and node_name(node) in execution.graph.nodes}

    def _intent(self, execution: GraphExecution, node: AggregateSnapshot, incarnation: RepairIncarnation,
                capture: Mapping[str, Any]) -> RepairIntent:
        if (capture.get("routing_errors") or capture.get("defect_kind") != "dependency_defect"
                or capture.get("status") != "FAIL"):
            raise SubmissionInvariantError("only validated dependency failures may register a repair intent")
        return RepairIntent(
            graph_id=execution.graph.graph_id, generation=execution.graph.generation,
            generation_hash=execution.graph.generation_hash, source_node=incarnation.node_name,
            source_cycle_id=incarnation.cycle_id, source_aggregate_id=node.aggregate_id,
            source_assignment_id=str(capture.get("source_assignment_id") or ""),
            source_input_fingerprint=incarnation.input_fingerprint,
            source_product_ref=execution.cycles[incarnation.node_name].product_ref,
            source_fencing_token=incarnation.fencing_token,
            candidate_ref=dict(capture["source_candidate_ref"]), packet_ref=dict(capture["repair_packet_ref"]),
            candidate_digest=str(capture["source_candidate_digest"]),
            submission_payload_hash=str(capture["source_payload_hash"]),
            provider_nodes=tuple(str(name) for name in capture.get("target_modules") or ()),
            pending_verification_ref=dict(capture.get("source_pending_ref") or capture.get("pending_ref") or {}),
        )

    def _join(self, work: BunshinUnitOfWork, execution: GraphExecution, node: AggregateSnapshot,
              incarnation: RepairIncarnation, frozen_ref: ArtifactRef, capture: Mapping[str, Any],
              capture_ref: ArtifactRef) -> GraphExecution:
        intent = self._intent(execution, node, incarnation, capture)
        if execution.dependency_repairs.find_intent(intent.key) is not None:
            return execution
        scope = set(intent.provider_nodes)
        for provider in intent.provider_nodes:
            scope.update(execution._dependency_consumers(intent.source_node, provider))
        previous = execution.dependency_repairs.pending
        old_members = dict(previous.frontier) if previous else {}
        by_node = {member.node_name: member for member in old_members.values()}
        nodes = self._nodes(execution, node.workflow_id)
        frozen = {incarnation.key: frozen_ref}
        frontier = []
        for name in sorted(scope):
            cycle = execution.cycles[name]
            if name in by_node or cycle.active_assignment is None:
                continue
            selected = nodes.get(name)
            if selected is None:
                raise SubmissionInvariantError(f"dependency repair lost the aggregate for {name}")
            member, reference = ((incarnation, frozen_ref) if name == incarnation.node_name
                                 else freeze_incarnation(self.repository,
                                    ContentAddressedArtifactStore(self.artifacts.runtime_root, work.transitions.artifacts),
                                    execution, selected))
            frontier.append(member)
            frozen[member.key] = reference
        joined = execution.register_dependency_repair(intent, frontier=tuple(frontier))
        pending = joined.dependency_repairs.pending
        assert pending is not None
        for member in frontier:
            if member.node_name == incarnation.node_name:
                continue
            selected = work.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, member.aggregate_id)
            if selected is None:
                raise SubmissionInvariantError("dependency repair aggregate disappeared during registration")
            self._request_stale(work, selected, frozen[member.key], capture_ref)
        # Capturing references does not close the admitted source. Cleanup must
        # still prove its task, workspace, submission cut and original lease.
        joined = joined.capture_dependency_repair(incarnation.key,
                    receipt_refs=(capture_ref.to_dict(), dict(capture["repair_packet_ref"])))
        return joined

    def _request_stale(self, work: BunshinUnitOfWork, node: AggregateSnapshot,
                       frozen_ref: ArtifactRef, reason_ref: ArtifactRef) -> None:
        if node.state == "STALE":
            return
        if node.state == "CANCEL_REQUESTED":
            if node.payload.get("cancel_target") != "STALE":
                raise SubmissionInvariantError("terminal cancellation supersedes dependency repair")
            return
        work.transitions.dispatch(ActionEnvelope(
            action_type="REQUEST_STALE", workflow_id=node.workflow_id,
            aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=node.aggregate_id,
            actor="bunshin-v2-manager", expected_version=node.version,
            idempotency_key=f"dependency-repair:freeze:{frozen_ref.sha256}",
            payload={"stale_reason_ref": reason_ref.to_dict(),
                     "dependency_repair_incarnation_ref": frozen_ref.to_dict()},
        ))

    def _effect_is_current(self, effect: Mapping[str, Any], trigger: AggregateSnapshot) -> bool:
        if effect.get("effect_type") != "reconcile_dependency_repairs":
            return self.owns(trigger)
        payload = dict(effect.get("payload") or {})
        capture_value = dict(payload.get("dependency_repair_capture_ref") or {})
        frozen_value = dict(payload.get("dependency_repair_incarnation_ref") or {})
        if not capture_value or not frozen_value:
            raise SubmissionInvariantError("repair reconcile effect has no immutable preparation binding")
        capture = self.artifacts.read_json(capture_value)
        original = frozen_node(self.artifacts, frozen_value)
        incarnation = RepairIncarnation.from_mapping(self.artifacts.read_json(frozen_value)["incarnation"])
        if (original.aggregate_id != trigger.aggregate_id or original.workflow_id != trigger.workflow_id
                or capture.get("incarnation_key") != incarnation.key
                or self.repository.queries.read_dependency_repair_capture_ref(trigger.aggregate_id,
                    str(dict(capture.get("source_pending_ref") or {}).get("sha256") or "")) != capture_value):
            raise SubmissionInvariantError("repair reconcile effect is not the source's prepared receipt")
        execution = self.execution(trigger.workflow_id)
        if int(payload.get("graph_generation") or 0) != incarnation.generation:
            raise SubmissionInvariantError("repair reconcile effect changed its graph generation")
        if execution.graph.generation != incarnation.generation or execution.state != GraphExecutionState.RUNNING:
            return False
        intent = self._intent(execution, original, incarnation, capture)
        for cohort in execution.dependency_repairs.history:
            if intent.key in cohort.intents:
                return False
        pending = execution.dependency_repairs.pending
        return bool((pending and intent.key in pending.intents) or
                    (trigger.state == "REVIEW_SNAPSHOTTING"
                     and dict(trigger.payload.get("dependency_repair_capture_ref") or {}) == capture_value))

    def _control_blocked(self, workflow_id: str, execution: GraphExecution) -> bool:
        workflow = self.repository.snapshots.read_snapshot(AggregateType.WORKFLOW, workflow_id)
        if workflow is not None and workflow.state in {"PAUSE_REQUESTED", "PAUSED", "TRIAGE_REQUIRED"}:
            return True
        return any(node.state in {"PAUSE_REQUESTED", "PAUSED", "TRIAGE_REQUIRED"}
                   for name, node in self._nodes(execution, workflow_id).items() if name in execution.pending_scope)

    async def reconcile(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        trigger = self.effect_reads.effect_snapshot(effect)
        workflow_id = trigger.workflow_id
        if not self._effect_is_current(effect, trigger):
            return {"status": "superseded"}
        # A finite frontier may expand only through a captured valid report.
        # Every loop either closes a frozen member or atomically applies it.
        while True:
            execution = self.execution(workflow_id)
            if execution.state != GraphExecutionState.RUNNING:
                return {"status": "superseded", "graph_state": execution.state.value}
            if self._control_blocked(workflow_id, execution):
                raise DeferredEffectError("dependency repair waits for explicit control resolution")
            pending = execution.dependency_repairs.pending
            if pending is None:
                if not self._register_waiting(workflow_id, trigger.aggregate_id):
                    return {"status": "reconciled"}
                continue
            member = next((item for key, item in pending.frontier.items() if key not in pending.closures), None)
            if member is not None:
                await self._retire(workflow_id, member)
                continue
            await self._check_inactive_scope(workflow_id, execution)
            from pal.bunshin.semantic_orchestration.dependency_repair_apply import apply_dependency_repair_cohort
            capture_refs = {key: next(dict(ref) for ref in refs if ref.get("artifact_type") == "DependencyRepairCaptureArtifact")
                            for key, refs in pending.captures.items()}
            apply_dependency_repair_cohort(repository=self.repository, artifacts=self.artifacts,
                graph_execution=execution, cohort_key=pending.key, capture_refs=capture_refs,
                command_id=f"dependency-repair:apply:{pending.key}")

    def _register_waiting(self, workflow_id: str, aggregate_id: str) -> bool:
        with self.repository.transaction() as work:
            execution = work.cycles.read_graph_execution(workflow_id=workflow_id)
            if execution is None or execution.state != GraphExecutionState.RUNNING:
                return False
            for node in self._nodes(execution, workflow_id).values():
                capture_value = dict(node.payload.get("dependency_repair_capture_ref") or {})
                frozen_value = dict(node.payload.get("dependency_repair_incarnation_ref") or {})
                if node.aggregate_id != aggregate_id or node.state != "REVIEW_SNAPSHOTTING" or not capture_value or not frozen_value:
                    continue
                frozen = self.artifacts.read_json(frozen_value)
                incarnation = RepairIncarnation.from_mapping(frozen["incarnation"])
                capture = self.artifacts.read_json(capture_value)
                original = frozen_node(self.artifacts, frozen_value)
                intent = self._intent(execution, original, incarnation, capture)
                if execution.dependency_repairs.find_intent(intent.key) is not None:
                    continue
                execution = self._join(work, execution, original, incarnation,
                    ArtifactRef.from_mapping(frozen_value), capture, ArtifactRef.from_mapping(capture_value))
                work.cycles.store_graph_execution(workflow_id=workflow_id, execution=execution)
                return True
        return False

    async def _retire(self, workflow_id: str, incarnation: RepairIncarnation) -> None:
        current = self.repository.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, incarnation.aggregate_id)
        if current is None:
            raise SubmissionInvariantError("repair frontier aggregate disappeared")
        frozen_ref = dict(current.payload.get("dependency_repair_incarnation_ref") or {})
        if not frozen_ref.get("sha256"):
            raise SubmissionInvariantError("repair frontier has no immutable aggregate binding")
        original = frozen_node(self.artifacts, frozen_ref)
        known = role_for_incarnation(self.repository, incarnation)
        if known:
            incarnation = incarnation.bind(replace(incarnation,
                role_assignment_id=str(known["assignment_id"]), attempt_id=incarnation.attempt_id or current_attempt_id(known)))
        assignment_id = await self.cleanup.retire_incarnation(
            effect_key=incarnation.effect_key, invocation_id=incarnation.lease_owner,
            lease_resource_key=incarnation.lease_resource, fencing_token=incarnation.fencing_token,
            assignment_id=incarnation.role_assignment_id, attempt_id=incarnation.attempt_id,
        )
        known = role_for_incarnation(self.repository, incarnation)
        if known:
            incarnation = incarnation.bind(replace(incarnation,
                role_assignment_id=str(known["assignment_id"]), attempt_id=incarnation.attempt_id or current_attempt_id(known)))
            assignment_id = incarnation.role_assignment_id
        self.repository.role_cancellation.cancel_role_assignments(
            workflow_id=workflow_id, aggregate_type=AggregateType.DAG_NODE_RUN,
            aggregate_id=incarnation.aggregate_id, reason="captured dependency repair invalidation",
            assignment_ids=(assignment_id,) if assignment_id else (),
        )
        workspace = Path(str(original.payload.get("workspace_path") or ""))
        if not workspace.is_dir():
            raise SubmissionInvariantError("repair frontier workspace disappeared")
        await self.cleanup.release_managed_lsp_workspace(workspace)
        # Quiescing may have left a manager-owned snapshot lock in this same
        # process. Exact task closure makes its release safe; another Manager's
        # OS lock still prevents concurrent snapshotting below.
        self.workspace_locks.release(incarnation.aggregate_id)
        self.workspace_locks.release(f"verification:{incarnation.aggregate_id}")
        lock_owner = f"dependency-repair:{incarnation.key}"
        try:
            lock_path = self.workspace_locks.acquire(lock_owner, workspace)
        except BlockingIOError as exc:
            raise DeferredEffectError("dependency repair waits for the original workspace lock") from exc
        try:
            _raise_if_workspace_held(workspace, "dependency repair workspace remains held", manager_snapshot_lock=lock_path)
            capture_value = dict(current.payload.get("dependency_repair_capture_ref") or {})
            if capture_value:
                capture = self.artifacts.read_json(capture_value)
                if capture.get("incarnation_key") != incarnation.key:
                    capture_value = {}
            if not capture_value:
                capture_value = self.collector.capture(node=original, incarnation=incarnation,
                    role_assignment_id=assignment_id,
                    pending_ref=dict(self.artifacts.read_json(frozen_ref).get("pending_verification_ref") or {}) if incarnation.slot == "checker" else None).to_dict()
            capture = dict(self.artifacts.read_json(capture_value))
        finally:
            self.workspace_locks.release(lock_owner)
        self.leases.release_business_lease(resource_key=incarnation.lease_resource,
            owner_id=incarnation.lease_owner, fencing_token=incarnation.fencing_token)
        if incarnation.snapshot_fencing_token:
            self.leases.release_business_lease(resource_key=incarnation.snapshot_lease_resource,
                owner_id=incarnation.snapshot_lease_owner, fencing_token=incarnation.snapshot_fencing_token)
        with self.repository.transaction() as work:
            execution = work.cycles.read_graph_execution(workflow_id=workflow_id)
            if execution is None or execution.dependency_repairs.pending is None:
                return
            pending = execution.dependency_repairs.pending
            if incarnation.key not in pending.frontier:
                raise SubmissionInvariantError("closed incarnation does not belong to the current frontier")
            if incarnation.key in pending.closures:
                return
            execution = execution.update_dependency_repair_frontier((incarnation,))
            capture_ref = ArtifactRef.from_mapping(capture_value)
            refs = tuple(append_refs([], capture_value, dict(capture.get("repair_packet_ref") or {})))
            execution = execution.capture_dependency_repair(incarnation.key, receipt_refs=refs)
            current = work.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, incarnation.aggregate_id)
            if current is None:
                raise SubmissionInvariantError("repair frontier disappeared before close")
            work.transitions.dispatch(ActionEnvelope(
                action_type="CAPTURE_DEPENDENCY_REPAIR", workflow_id=workflow_id,
                aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=current.aggregate_id,
                actor="bunshin-v2-manager", expected_version=current.version,
                idempotency_key=f"dependency-repair:capture:{incarnation.key}",
                payload={"dependency_repair_capture_ref": capture_value,
                         "historical_repair_bill_refs": append_refs(current.payload.get("historical_repair_bill_refs"),
                             dict(current.payload.get("repair_bill_ref") or {}), dict(capture.get("repair_packet_ref") or {})),
                         **({"candidate_ref": capture["candidate_ref"], "candidate_digest": capture["candidate_digest"]}
                            if capture.get("candidate_ref") and incarnation.slot == "checker" else {})},
            ))
            current = work.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, incarnation.aggregate_id)
            assert current is not None
            from pal.bunshin.semantic_orchestration.dependency_repair_budget import triage_captured_no_progress
            if triage_captured_no_progress(artifacts=self.artifacts, work=work,
                    execution=execution, node=current, capture=capture):
                return
            if capture.get("defect_kind") == "dependency_defect" and capture.get("status") == "FAIL" and not capture.get("routing_errors"):
                execution = self._join(work, execution, original, incarnation, ArtifactRef.from_mapping(frozen_ref), capture, capture_ref)
            self._request_stale(work, current, ArtifactRef.from_mapping(frozen_ref), capture_ref)
            current = work.snapshots.read_snapshot(AggregateType.DAG_NODE_RUN, incarnation.aggregate_id)
            assert current is not None
            execution = execution.close_dependency_repair(RepairClosure(incarnation,
                task_closed=True, process_reaped=True, workspace_released=True,
                lease_released=True, submission_cut=True, receipt_refs=refs))
            if current.state == "CANCEL_REQUESTED":
                work.transitions.dispatch(ActionEnvelope(
                    action_type="CANCEL_CONFIRMED", workflow_id=workflow_id,
                    aggregate_type=AggregateType.DAG_NODE_RUN, aggregate_id=current.aggregate_id,
                    actor="bunshin-v2-manager", expected_version=current.version,
                    idempotency_key=f"dependency-repair:closed:{incarnation.key}"))
            elif current.state != "STALE":
                raise SubmissionInvariantError("dependency closure cannot acknowledge another control state")
            work.cycles.store_graph_execution(workflow_id=workflow_id, execution=execution)

    async def _check_inactive_scope(self, workflow_id: str, execution: GraphExecution) -> None:
        pending = execution.dependency_repairs.pending
        assert pending is not None
        frontier = {member.node_name for member in pending.frontier.values()}
        for name, node in self._nodes(execution, workflow_id).items():
            if name not in pending.scope or name in frontier:
                continue
            resource = str(node.payload.get("lease_resource_key") or "")
            lease = self.repository.leases.read_lease(resource) if resource else None
            if lease is not None and _lease_is_live(lease):
                raise DeferredEffectError("dependency repair waits for accepted provider cleanup")
            text = str(node.payload.get("workspace_path") or "")
            if text:
                workspace = Path(text)
                await self.cleanup.release_managed_lsp_workspace(workspace)
                _raise_if_workspace_held(workspace, "inactive dependency repair workspace remains held")
