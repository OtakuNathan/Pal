from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from pal.foundation import utc_now
from pal.proactive.contracts import ProactiveDefinition
from pal.proactive.models import ProactiveDefinitionModel, ProactiveRunModel
from pal.shared import ProactiveTriggerEvent

class ProactiveRepositoryPort(Protocol):
    def upsert_definition(self, definition: ProactiveDefinition, *, next_due_at_utc: str | None = None) -> object:
        ...

    def list_definitions(self) -> list[object]:
        ...

    def update_schedule_state(
        self,
        proactive_id: str,
        *,
        next_due_at_utc: str | None = None,
        last_run_at_utc: str | None = None,
    ) -> object | None:
        ...

    def begin_run(self, trigger: ProactiveTriggerEvent) -> str:
        ...

    def complete_run(self, proactive_run_id: str, *, turn_id: str, final_reply: str) -> None:
        ...

    def fail_run(self, proactive_run_id: str, *, error_text: str) -> None:
        ...

    def delete_definition(self, proactive_id: str) -> bool:
        ...


@dataclass(frozen=True)
class StoredProactiveDefinition:
    definition: ProactiveDefinition
    next_due_at_utc: str | None = None
    last_run_at_utc: str | None = None


@dataclass(frozen=True)
class StoredProactiveRun:
    proactive_run_id: str
    proactive_id: str
    trigger_kind: str
    status: str
    trigger_metadata: dict[str, object]
    turn_id: str | None = None
    output_summary: str | None = None
    error_text: str | None = None
    started_at: str = ""
    completed_at: str | None = None


class ProactiveRepository(ProactiveRepositoryPort):
    def upsert_definition(self, definition: ProactiveDefinition, *, next_due_at_utc: str | None = None) -> ProactiveDefinitionModel:
        instance = ProactiveDefinitionModel.get_or_none(ProactiveDefinitionModel.proactive_id == definition.proactive_id)
        now = utc_now()
        payload = {
            "goal": definition.goal,
            "method": definition.method,
            "skill_refs_blob": list(definition.skill_refs),
            "out_channel_id": definition.out_channel_id,
            "schedule_blob": dict(definition.schedule),
            "out_reply_target_blob": dict(definition.out_reply_target or {}),
            "enabled": bool(definition.enabled),
            "next_due_at_utc": next_due_at_utc,
        }
        if instance is None:
            return ProactiveDefinitionModel.create(
                proactive_id=definition.proactive_id,
                created_at=now,
                updated_at=now,
                **payload,
            )
        for key, value in payload.items():
            setattr(instance, key, value)
        instance.updated_at = now
        instance.save()
        return instance

    def list_definitions(self) -> list[StoredProactiveDefinition]:
        items: list[StoredProactiveDefinition] = []
        query = ProactiveDefinitionModel.select().order_by(ProactiveDefinitionModel.proactive_id)
        for row in query:
            items.append(
                StoredProactiveDefinition(
                    definition=ProactiveDefinition(
                        proactive_id=row.proactive_id,
                        goal=row.goal,
                        method=row.method,
                        skill_refs=list(row.skill_refs_blob or []),
                        out_channel_id=row.out_channel_id,
                        schedule=dict(row.schedule_blob or {}),
                        out_reply_target=dict(row.out_reply_target_blob or {}),
                        enabled=bool(row.enabled),
                    ),
                    next_due_at_utc=row.next_due_at_utc,
                    last_run_at_utc=row.last_run_at_utc,
                )
            )
        return items

    def update_schedule_state(
        self,
        proactive_id: str,
        *,
        next_due_at_utc: str | None = None,
        last_run_at_utc: str | None = None,
    ) -> ProactiveDefinitionModel | None:
        instance = ProactiveDefinitionModel.get_or_none(ProactiveDefinitionModel.proactive_id == proactive_id)
        if instance is None:
            return None
        if next_due_at_utc is not None or next_due_at_utc is None:
            instance.next_due_at_utc = next_due_at_utc
        if last_run_at_utc is not None:
            instance.last_run_at_utc = last_run_at_utc
        instance.updated_at = utc_now()
        instance.save()
        return instance

    def begin_run(self, trigger: ProactiveTriggerEvent) -> str:
        run_id = str(uuid4())
        now = utc_now()
        ProactiveRunModel.create(
            proactive_run_id=run_id,
            proactive_id=trigger.proactive_id,
            trigger_kind=trigger.trigger_kind,
            status="running",
            trigger_metadata=dict(trigger.metadata or {}),
            started_at=now,
            created_at=now,
            updated_at=now,
        )
        return run_id

    def complete_run(self, proactive_run_id: str, *, turn_id: str, final_reply: str) -> None:
        row = ProactiveRunModel.get_or_none(ProactiveRunModel.proactive_run_id == proactive_run_id)
        if row is None:
            return
        now = utc_now()
        row.status = "completed"
        row.turn_id = turn_id
        row.output_summary = final_reply
        row.completed_at = now
        row.updated_at = now
        row.save()

    def fail_run(self, proactive_run_id: str, *, error_text: str) -> None:
        row = ProactiveRunModel.get_or_none(ProactiveRunModel.proactive_run_id == proactive_run_id)
        if row is None:
            return
        now = utc_now()
        row.status = "failed"
        row.error_text = error_text
        row.completed_at = now
        row.updated_at = now
        row.save()

    def delete_definition(self, proactive_id: str) -> bool:
        row = ProactiveDefinitionModel.get_or_none(ProactiveDefinitionModel.proactive_id == proactive_id)
        if row is None:
            return False
        row.delete_instance()
        return True

    def list_runs(self, proactive_id: str, *, limit: int = 20) -> list[StoredProactiveRun]:
        query = (
            ProactiveRunModel.select()
            .where(ProactiveRunModel.proactive_id == proactive_id)
            .order_by(ProactiveRunModel.started_at.desc(), ProactiveRunModel.proactive_run_id.desc())
            .limit(max(1, int(limit)))
        )
        return [self._to_stored_run(row) for row in query]

    def latest_run(self, proactive_id: str) -> StoredProactiveRun | None:
        row = (
            ProactiveRunModel.select()
            .where(ProactiveRunModel.proactive_id == proactive_id)
            .order_by(ProactiveRunModel.started_at.desc(), ProactiveRunModel.proactive_run_id.desc())
            .first()
        )
        if row is None:
            return None
        return self._to_stored_run(row)

    def _to_stored_run(self, row: ProactiveRunModel) -> StoredProactiveRun:
        return StoredProactiveRun(
            proactive_run_id=row.proactive_run_id,
            proactive_id=row.proactive_id,
            trigger_kind=row.trigger_kind,
            status=row.status,
            trigger_metadata=dict(row.trigger_metadata or {}),
            turn_id=row.turn_id,
            output_summary=row.output_summary,
            error_text=row.error_text,
            started_at=row.started_at,
            completed_at=row.completed_at,
        )
