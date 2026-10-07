from __future__ import annotations
from pal.bunshin.unit_of_work import BunshinUnitOfWork
from pal.bunshin.semantic_orchestration.role_inputs import _node_role_session_id
import hashlib
from dataclasses import dataclass
from typing import Any, Mapping
from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType, DeferredEffectError, LeaseConflict, StaleFencingToken
from pal.bunshin.cycle_protocol import AssignmentKind, CycleSlot
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.null_execution import NullExecution
from pal.bunshin.semantic_orchestration.verifier_tests import VerifierTests
from pal.bunshin.semantic_orchestration.workflow_facts import WorkflowFacts


@dataclass
class NodeAdmission:
    effect_reads: EffectReads
    null_execution: NullExecution
    verifier_tests: VerifierTests
    workflow_facts: WorkflowFacts
    repository: BunshinRepository

    def admit_implementation_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        mode = RoleMode(self.effect_reads.effect_role_mode(effect))
        if (
            mode == RoleMode.PRODUCE
            and self.workflow_facts.role_participant_kind(
                self.effect_reads.effect_snapshot(effect).workflow_id,
                OrchestrationRole.IMPLEMENTATION.value,
            )
            == "null"
        ):
            return self.null_execution.accept_null_execution(effect)
        if mode == RoleMode.PRODUCE:
            return self.admit_node_worker(
                effect,
                action_type="START_PRODUCING",
                activation=RoleActivation(
                    OrchestrationRole.IMPLEMENTATION,
                    RoleMode.PRODUCE,
                ),
            )
        if mode == RoleMode.REPAIR:
            node = self.effect_reads.effect_snapshot(effect)
            repair_inputs = self.verifier_tests.install_verifier_tests_for_repair(node)
            return self.admit_node_worker(
                effect,
                action_type="START_REPAIR",
                activation=RoleActivation(
                    OrchestrationRole.IMPLEMENTATION,
                    RoleMode.REPAIR,
                ),
                extra_payload=repair_inputs,
            )
        raise ValueError(f"unsupported implementation mode: {mode.value}")

    async def handle_admit_implementation_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.admit_implementation_role(effect)

    def admit_verifier_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        mode = RoleMode(self.effect_reads.effect_role_mode(effect))
        if mode != RoleMode.MODULE:
            raise ValueError(f"unsupported verifier mode: {mode.value}")
        return self.admit_node_worker(
            effect,
            action_type="START_REVIEW",
            activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE),
        )

    async def handle_admit_verifier_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.admit_verifier_role(effect)

    def admit_reviewer_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        if RoleMode(self.effect_reads.effect_role_mode(effect)) != RoleMode.STANDALONE:
            raise ValueError("only standalone review has a separate admission step")
        return self.admit_standalone_review(effect)

    async def handle_admit_reviewer_role(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.admit_reviewer_role(effect)

    def admit_node_worker(
        self,
        effect: Mapping[str, Any],
        *,
        action_type: str,
        activation: RoleActivation,
        extra_payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        node = self.effect_reads.effect_snapshot(effect)
        epoch_id = str(node.payload.get("epoch_id") or "")
        epoch = self.repository.snapshots.read_snapshot(AggregateType.EXECUTION_EPOCH, epoch_id)
        if epoch is not None and epoch.state in {
            "REPLAN_COLLECTING",
            "REPLAN_REQUIRED",
            "SUPERSEDED",
        }:
            legal = self.repository.transitions.legal_actions(AggregateType.DAG_NODE_RUN, node.state)
            finding_ref = dict(epoch.payload.get("replan_finding_batch_ref") or {})
            if not finding_ref:
                pending = list(epoch.payload.get("pending_replan_findings") or [])
                if pending:
                    finding_ref = dict(dict(pending[0] or {}).get("finding_artifact_ref") or {})
            action = "MARK_STALE" if "MARK_STALE" in legal else "REQUEST_STALE" if "REQUEST_STALE" in legal else ""
            if action and finding_ref:
                self.repository.transitions.dispatch(
                    ActionEnvelope(
                        action_type=action,
                        workflow_id=node.workflow_id,
                        aggregate_type=AggregateType.DAG_NODE_RUN,
                        aggregate_id=node.aggregate_id,
                        actor="bunshin-v2-replan",
                        expected_version=node.version,
                        idempotency_key=f"replan-suppress-admission:{node.aggregate_id}:{node.version}",
                        payload={"stale_reason_ref": finding_ref},
                    )
                )
            return {"status": "suppressed_by_replan"}
        execution = self.repository.cycles.read_graph_execution(workflow_id=node.workflow_id)
        name = str(node.payload.get("module_name") or node.payload.get("unit_id") or "")
        if execution is not None and name in execution.pending_scope:
            raise DeferredEffectError("pending dependency repair fences role admission")
        implementation = activation.role == OrchestrationRole.IMPLEMENTATION
        target_state = {
            "START_PRODUCING": "PRODUCING",
            "START_REVIEW": "REVIEWING",
            "START_REPAIR": "REPAIRING",
        }[action_type]
        if node.state == target_state and node.payload.get("active_worker_id"):
            self.start_graph_cycle_assignment(
                node=node,
                effect=effect,
                action_type=action_type,
                implementation=implementation,
            )
            return {"provider_request_id": str(node.payload.get("active_worker_id"))}
        cycle = int(node.payload.get("candidate_cycle") or 0) + (1 if implementation else 0)
        invocation_id = _node_role_session_id(node, activation)
        lease_resource = f"node:{node.aggregate_id}:{'writer' if implementation else 'review'}"
        lease = self.repository.leases.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": node.workflow_id,
                "node_run_id": node.aggregate_id,
                **activation.to_dict(),
            },
        )
        payload = {
            "fencing_token": lease.fencing_token,
            "active_worker_id": invocation_id,
            "lease_resource_key": lease_resource,
            "active_role": activation.role.value,
            "active_role_mode": activation.mode.value,
            **dict(extra_payload or {}),
        }
        if implementation:
            payload["candidate_cycle"] = cycle
        try:
            with self.repository.transaction() as connection:
                (connection or self.repository).transitions.dispatch(
                    ActionEnvelope(
                        action_type=action_type,
                        workflow_id=node.workflow_id,
                        aggregate_type=AggregateType.DAG_NODE_RUN,
                        aggregate_id=node.aggregate_id,
                        actor="bunshin-v2-scheduler",
                        expected_version=node.version,
                        idempotency_key=f"effect:{effect['effect_key']}:admit",
                        payload=payload,
                    ),
                )
                self.start_graph_cycle_assignment(
                    node=node,
                    effect=effect,
                    action_type=action_type,
                    implementation=implementation,
                    unit_of_work=connection,
                )
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
        return {"provider_request_id": invocation_id}

    def start_graph_cycle_assignment(
        self,
        *,
        node: AggregateSnapshot,
        effect: Mapping[str, Any],
        action_type: str,
        implementation: bool,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        module_name = str(
            node.payload.get("module_name")
            or node.payload.get("unit_id")
            or ""
        )
        coordinator = WorkflowCoordinator(self.repository)
        execution = coordinator.execution(workflow_id=node.workflow_id, unit_of_work=unit_of_work)
        if module_name in execution.pending_scope:
            raise DeferredEffectError("pending dependency repair fences role admission")
        cycle = execution.cycles[module_name]
        coordinator.start_assignment(
            workflow_id=node.workflow_id,
            node_name=module_name,
            slot=(
                CycleSlot.PRODUCER
                if implementation
                else CycleSlot.CHECKER
            ),
            kind=(
                AssignmentKind.REPAIR
                if action_type == "START_REPAIR"
                else AssignmentKind.RECHECK
                if action_type == "START_REVIEW"
                and cycle.last_verdict is not None
                else AssignmentKind.INITIAL
            ),
            input_fingerprint=str(effect["effect_key"]),
            unit_of_work=unit_of_work,
        )

    def admit_standalone_review(self, effect: Mapping[str, Any]) -> Mapping[str, Any]:
        review = self.effect_reads.effect_snapshot(effect)
        invocation_id = f"inv_{hashlib.sha256(f'{review.aggregate_id}:review'.encode()).hexdigest()[:24]}"
        lease_resource = f"standalone-review:{review.aggregate_id}"
        lease = self.repository.leases.claim_lease(
            lease_resource,
            invocation_id,
            ttl_seconds=120,
            metadata={
                "workflow_id": review.workflow_id,
                "review_id": review.aggregate_id,
                "role": OrchestrationRole.REVIEWER.value,
                "mode": RoleMode.STANDALONE.value,
            },
        )
        try:
            self.repository.transitions.dispatch(
                ActionEnvelope(
                    action_type="START_REVIEW",
                    workflow_id=review.workflow_id,
                    aggregate_type=AggregateType.STANDALONE_REVIEW,
                    aggregate_id=review.aggregate_id,
                    actor="bunshin-v2-scheduler",
                    expected_version=review.version,
                    idempotency_key=f"effect:{effect['effect_key']}:start",
                    payload={
                        "fencing_token": lease.fencing_token,
                        "active_worker_id": invocation_id,
                        "lease_resource_key": lease_resource,
                        "active_role": OrchestrationRole.REVIEWER.value,
                        "active_role_mode": RoleMode.STANDALONE.value,
                    },
                )
            )
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
        return {"provider_request_id": invocation_id}
