from __future__ import annotations
from pal.bunshin.semantic_orchestration.role_inputs import _semantic_role_input_refs
from dataclasses import dataclass
from typing import Any, Mapping
from pal.bunshin.contracts import AggregateType, SubmissionInvariantError
from pal.bunshin.background_assignments import BackgroundAssignments
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.machines import machine_spec_for
from pal.bunshin.role_contracts import RoleActivation, RoleMode
from pal.bunshin.role_gateway import role_submission_artifact_type
from pal.bunshin.role_protocol import RoleAssignmentState
from pal.bunshin.semantic_orchestration.effect_reads import EffectReads
from pal.bunshin.semantic_orchestration.routes import SEMANTIC_EFFECT_ROUTES


@dataclass
class AssignmentIdentity:
    effect_reads: EffectReads
    background: BackgroundAssignments
    repository: BunshinRepository

    def role_assignment_disposition(
        self,
        effect: Mapping[str, Any],
        assignment: Mapping[str, Any],
    ) -> str:
        assignment_state = str(assignment.get("state") or "")
        if assignment_state == RoleAssignmentState.CANCELLED.value:
            return "cancelled"
        route = SEMANTIC_EFFECT_ROUTES.get(str(effect.get("effect_type") or ""))
        aggregate_type_value = str(assignment.get("aggregate_type") or "")
        expected_states: set[str] = set()
        if route is not None and route.role is not None:
            modes = route.modes
            effect_mode = self.effect_reads.effect_role_mode(effect)
            if effect_mode:
                modes = frozenset({RoleMode(effect_mode)})
            try:
                machine = machine_spec_for(AggregateType(aggregate_type_value))
            except ValueError:
                machine = None
            if machine is not None:
                for mode in modes:
                    expected_states.update(
                        machine.states_for_activation(RoleActivation(route.role, mode))
                    )
        if expected_states:
            try:
                aggregate_type = AggregateType(str(assignment.get("aggregate_type") or ""))
                snapshot = self.repository.snapshots.read_snapshot(
                    aggregate_type,
                    str(assignment.get("aggregate_id") or ""),
                )
            except (KeyError, ValueError):
                return "superseded"
            if snapshot is None:
                return "superseded"
            if snapshot.state not in expected_states:
                if snapshot.state in {"PAUSE_REQUESTED", "PAUSED"}:
                    return "suspended"
                if snapshot.state in {"CANCEL_REQUESTED", "CANCELLED", "STALE"}:
                    return "cancelled"
                return "settled" if assignment_state == RoleAssignmentState.SETTLED.value else "superseded"
        elif assignment_state == RoleAssignmentState.SETTLED.value:
            return "settled"
        reusable = self.reusable_role_assignment(
            workflow_id=str(assignment.get("workflow_id") or ""),
            aggregate_type=str(assignment.get("aggregate_type") or ""),
            aggregate_id=str(assignment.get("aggregate_id") or ""),
            role=str(assignment.get("role") or ""),
            mode=str(assignment.get("mode") or ""),
            submission_kind=str(assignment.get("submission_kind") or ""),
            input_refs=dict(assignment.get("input_refs") or {}),
            evaluation_generation=int(
                dict(assignment.get("execution_spec") or {}).get(
                    "evaluation_generation"
                )
                or 0
            ),
            exclude_assignment_id=str(assignment.get("assignment_id") or ""),
        )
        if reusable is not None:
            return "superseded by an equivalent durable submission"
        return ""

    def reusable_role_assignment(
        self,
        *,
        workflow_id: str,
        aggregate_type: str,
        aggregate_id: str,
        role: str,
        mode: str,
        submission_kind: str,
        input_refs: Mapping[str, Mapping[str, Any]],
        evaluation_generation: int = 0,
        exclude_assignment_id: str = "",
    ) -> dict[str, Any] | None:
        semantic_inputs = self.semantic_role_input_identity(
            input_refs,
            role=role,
            mode=mode,
        )
        expected_artifact_type = role_submission_artifact_type(submission_kind)
        if not expected_artifact_type:
            return None
        candidates = self.repository.role_assignments.list_role_assignments(workflow_id=workflow_id)
        for candidate in reversed(candidates):
            if str(candidate.get("assignment_id") or "") == exclude_assignment_id:
                continue
            if str(candidate.get("aggregate_type") or "") != aggregate_type:
                continue
            if str(candidate.get("aggregate_id") or "") != aggregate_id:
                continue
            if str(candidate.get("role") or "") != role:
                continue
            if str(candidate.get("mode") or "") != mode:
                continue
            if str(candidate.get("submission_kind") or "") != submission_kind:
                continue
            if int(
                dict(candidate.get("execution_spec") or {}).get(
                    "evaluation_generation"
                )
                or 0
            ) != int(evaluation_generation):
                continue
            if str(candidate.get("state") or "") not in {
                RoleAssignmentState.RESULT_RECORDED.value,
                RoleAssignmentState.SETTLED.value,
            }:
                continue
            artifact_ref = dict(candidate.get("submission_artifact_ref") or {})
            if str(artifact_ref.get("artifact_type") or "") != expected_artifact_type:
                continue
            if (
                self.semantic_role_input_identity(
                    dict(candidate.get("input_refs") or {}),
                    role=role,
                    mode=mode,
                )
                == semantic_inputs
            ):
                return dict(candidate)
        return None

    def semantic_role_input_identity(
        self,
        input_refs: Mapping[str, Mapping[str, Any]],
        *,
        role: str,
        mode: str,
    ) -> dict[str, dict[str, Any]]:
        """Return the exact immutable semantic inputs for receipt reconciliation."""

        return _semantic_role_input_refs(
            input_refs,
            role=role,
            mode=mode,
        )

    def role_submission_settlement(
        self,
        effect: Mapping[str, Any],
        *,
        assignment_id: str = "",
        required: bool = True,
    ) -> dict[str, str]:
        effect_key = str(effect.get("effect_key") or effect.get("effect_id") or "")
        resolved_assignment_id = str(assignment_id).strip()
        if not resolved_assignment_id:
            resolved_assignment_id = self.background.assignment_id(effect_key, "")
        if not resolved_assignment_id:
            return {}
        assignment = self.repository.role_assignments.read_role_assignment(resolved_assignment_id)
        if assignment is None:
            raise SubmissionInvariantError("role assignment disappeared before settlement")
        if assignment["state"] not in {
            RoleAssignmentState.RESULT_RECORDED.value,
            RoleAssignmentState.SETTLED.value,
        }:
            if not required:
                return {}
            raise SubmissionInvariantError(
                "role business action requires a durable submission receipt"
            )
        payload_hash = str(assignment.get("submission_payload_hash") or "")
        if not payload_hash:
            raise SubmissionInvariantError(
                "role assignment submission receipt has no payload hash"
            )
        return {
            "role_assignment_id": resolved_assignment_id,
            "role_submission_payload_hash": payload_hash,
        }

    @staticmethod
    def terminal_role_assignment_id(terminal: Mapping[str, Any]) -> str:
        assignment_id = str(
            dict(terminal.get("payload") or {}).get("role_assignment_id") or ""
        ).strip()
        if not assignment_id:
            raise SubmissionInvariantError(
                "role terminal has no durable assignment identity"
            )
        return assignment_id
