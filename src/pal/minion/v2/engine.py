from __future__ import annotations

from dataclasses import replace
from typing import Iterable

from pal.minion.v2.contracts import (
    ActionEnvelope,
    AggregateSnapshot,
    AggregateType,
    AggregateVersionConflict,
    TransitionOutcome,
    TransitionSpec,
    UnknownTransitionError,
)


class TransitionEngine:
    """Pure, table-driven aggregate transition engine."""

    def __init__(self, transitions: Iterable[TransitionSpec] = ()) -> None:
        self._table: dict[tuple[AggregateType, str | None, str], TransitionSpec] = {}
        for transition in transitions:
            self.register(transition)

    def register(self, transition: TransitionSpec) -> None:
        key = (transition.aggregate_type, transition.source_state, transition.action_type)
        if key in self._table:
            raise ValueError(f"duplicate transition registration: {key}")
        self._table[key] = transition

    def transition(
        self,
        snapshot: AggregateSnapshot | None,
        action: ActionEnvelope,
    ) -> TransitionOutcome:
        current_state = snapshot.state if snapshot is not None else None
        key = (action.aggregate_type, current_state, action.action_type)
        spec = self._table.get(key)
        if spec is None:
            raise UnknownTransitionError(
                f"invalid transition: {action.aggregate_type.value}:{current_state} + {action.action_type}"
            )
        if snapshot is not None:
            if snapshot.aggregate_type != action.aggregate_type or snapshot.aggregate_id != action.aggregate_id:
                raise UnknownTransitionError("action aggregate identity does not match snapshot")
            if snapshot.workflow_id != action.workflow_id:
                raise UnknownTransitionError("action workflow identity does not match snapshot")
        current_version = snapshot.version if snapshot is not None else 0
        if action.expected_version is not None and action.expected_version != current_version:
            raise AggregateVersionConflict(
                f"expected aggregate version {action.expected_version}, found {current_version}"
            )
        current_payload = dict(snapshot.payload) if snapshot is not None else {}
        spec.guard(current_payload, action)
        next_payload = dict(spec.reducer(current_payload, action))
        target_state = (
            spec.target_state(next_payload, action)
            if callable(spec.target_state)
            else str(spec.target_state)
        )
        next_version = current_version + 1
        next_snapshot = AggregateSnapshot(
            aggregate_type=action.aggregate_type,
            aggregate_id=action.aggregate_id,
            workflow_id=action.workflow_id,
            state=target_state,
            version=next_version,
            payload=next_payload,
            created_at=snapshot.created_at if snapshot is not None else action.created_at,
            updated_at=action.created_at,
        )
        events = spec.event_builder(next_payload, action, target_state)
        effects = spec.effect_builder(next_payload, action, target_state)
        return TransitionOutcome(snapshot=next_snapshot, events=events, effects=effects)

    def registered_keys(self) -> frozenset[tuple[AggregateType, str | None, str]]:
        return frozenset(self._table)

    def legal_actions(self, aggregate_type: AggregateType, state: str | None) -> tuple[str, ...]:
        return tuple(
            sorted(
                action_type
                for current_type, current_state, action_type in self._table
                if current_type == aggregate_type and current_state == state
            )
        )

    def with_transition(self, transition: TransitionSpec) -> "TransitionEngine":
        clone = TransitionEngine(self._table.values())
        clone.register(replace(transition))
        return clone
