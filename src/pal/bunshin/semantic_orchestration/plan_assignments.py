from __future__ import annotations
from dataclasses import dataclass
from pal.bunshin.unit_of_work import BunshinUnitOfWork
from typing import Any, Mapping
from pal.bunshin.cycle_protocol import AssignmentKind, CycleSlot
from pal.bunshin.workflow_runtime import WorkflowCoordinator
from pal.bunshin.repository import BunshinRepository


@dataclass
class PlanAssignments:
    repository: BunshinRepository

    def start_plan_cycle_assignment(
        self,
        *,
        workflow_id: str,
        effect: Mapping[str, Any],
        slot: CycleSlot,
        unit_of_work: BunshinUnitOfWork | None = None,
    ) -> None:
        coordinator = WorkflowCoordinator(self.repository)
        cycle = coordinator.ensure_plan_cycle(
            workflow_id=workflow_id,
            unit_of_work=unit_of_work,
        )
        coordinator.start_plan_assignment(
            workflow_id=workflow_id,
            slot=slot,
            kind=(
                AssignmentKind.REVISION
                if cycle.generation > 1
                else AssignmentKind.INITIAL
            ),
            input_fingerprint=str(effect["effect_key"]),
            unit_of_work=unit_of_work,
        )
