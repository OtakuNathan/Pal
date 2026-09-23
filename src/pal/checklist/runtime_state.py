"""Checklist recovery uses the existing encrypted runtime checkpoint."""
from dataclasses import dataclass
from typing import Any, Mapping

from pal.checklist.service import ChecklistItem, ChecklistService


@dataclass
class ChecklistRuntimeStatePort:
    service: ChecklistService
    module_id: str = "checklist"
    schema_version: str = "1"
    state_order: int = 150

    def snapshot_state(self) -> Mapping[str, Any]:
        snapshot = self.service.show()
        return {"plan": [dict(item) for item in snapshot.plan] if snapshot is not None else None}

    def prepare_restore_state(self, payload: Mapping[str, Any]) -> tuple[ChecklistItem, ...] | None:
        if set(payload) != {"plan"}:
            raise ValueError("checklist snapshot must contain only plan")
        plan = payload["plan"]
        if plan is None:
            return None
        if not isinstance(plan, list) or not plan:
            raise ValueError("checklist snapshot plan must be a non-empty array or null")
        for item in plan:
            if (not isinstance(item, dict) or set(item) != {"step", "status"}
                    or not isinstance(item["step"], str) or not isinstance(item["status"], str)):
                raise ValueError("checklist snapshot has an invalid item")
        # Use the same size, status and non-empty-step checks as live edits.
        validated = ChecklistService().upsert(plan)
        return tuple(ChecklistItem(item["step"], item["status"]) for item in validated.plan)

    def install_prepared_state(self, prepared: tuple[ChecklistItem, ...] | None) -> None:
        self.service.install_restored_items(prepared)

    def reset_state(self, reason: str) -> None:
        self.service.clear()
