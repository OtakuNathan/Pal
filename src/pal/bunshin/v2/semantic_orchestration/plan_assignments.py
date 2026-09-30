from __future__ import annotations
from dataclasses import dataclass
from pal.bunshin.v2.unit_of_work import BunshinUnitOfWork
from typing import Any, Mapping
from pal.bunshin.v2.cycle_protocol import AssignmentKind, CycleSlot
from pal.bunshin.v2.workflow_runtime import WorkflowCoordinator
from pal.bunshin.v2.repository import BunshinV2Repository


@dataclass
class PlanAssignments:
    repository: BunshinV2Repository

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
