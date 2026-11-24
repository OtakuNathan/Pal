"""Shared aggregate access for durable workflow effects."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from pal.bunshin.contracts import ActionEnvelope, AggregateSnapshot, AggregateType
from pal.bunshin.repository import BunshinRepository
from pal.bunshin.service import BunshinWorkflowService
from pal.bunshin.unit_of_work import BunshinUnitOfWork


def _derived_id(prefix: str, effect_key: str) -> str:
    return f"{prefix}_{hashlib.sha256(effect_key.encode('utf-8')).hexdigest()[:24]}"


@dataclass
class WorkflowEffectContext:
    service: BunshinWorkflowService

    @property
    def repository(self) -> BunshinRepository:
        return self.service.repository

    def _link_workflow(
        self,
        workflow_id: str,
        action_type: str,
        payload: Mapping[str, Any],
        causation_key: str,
        *,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        workflow = (unit_of_work or self.repository).snapshots.read_snapshot(
            AggregateType.WORKFLOW,
            workflow_id,
        )
        if workflow is None:
            raise ValueError("workflow does not exist")
        (unit_of_work or self.repository).transitions.dispatch(
            ActionEnvelope(
                action_type=action_type,
                workflow_id=workflow_id,
                aggregate_type=AggregateType.WORKFLOW,
                aggregate_id=workflow_id,
                actor="bunshin-router",
                expected_version=workflow.version,
                idempotency_key=f"link:{causation_key}:{action_type}",
                payload=dict(payload),
            ),
        )

    def _effect_snapshot(self, effect: Mapping[str, Any]) -> AggregateSnapshot:
        aggregate_type = AggregateType(str(effect["aggregate_type"]))
        snapshot = self.repository.snapshots.read_snapshot(aggregate_type, str(effect["aggregate_id"]))
        if snapshot is None:
            raise ValueError("effect aggregate no longer exists")
        return snapshot
