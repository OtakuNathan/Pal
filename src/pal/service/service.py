from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any
from datetime import datetime, timezone

from pal.core.mailbox import Mailbox
from pal.service.contracts import (
    ScheduleEnginePort,
    ServiceDefinition,
    ServiceManagerPort,
    ServiceRunnerPort,
    ServiceTriggerEvent,
)
from pal.service.repository import ServiceRepositoryPort
from pal.service.scheduling import compute_next_service_run_at_utc, utc_now_dt


@dataclass
class ScheduleEngine(ScheduleEnginePort):
    next_due_by_service_id: dict[str, str | None] = field(default_factory=dict)

    def next_due_at(self, service_id: str) -> str | None:
        return self.next_due_by_service_id.get(service_id)

    def register(self, definition: ServiceDefinition, *, now_utc: datetime | None = None) -> str | None:
        next_due = self._compute_next_due(definition, now_utc=now_utc)
        self.next_due_by_service_id[definition.service_id] = next_due
        return next_due

    def unregister(self, service_id: str) -> None:
        self.next_due_by_service_id.pop(service_id, None)

    def mark_completed(self, definition: ServiceDefinition, *, now_utc: datetime | None = None) -> str | None:
        next_due = self._compute_next_due(definition, now_utc=now_utc)
        self.next_due_by_service_id[definition.service_id] = next_due
        return next_due

    def collect_due(self, definitions: dict[str, ServiceDefinition], *, now_utc: datetime | None = None) -> list[ServiceTriggerEvent]:
        reference = now_utc or utc_now_dt()
        due: list[ServiceTriggerEvent] = []
        for service_id, definition in sorted(definitions.items()):
            next_due = self.next_due_by_service_id.get(service_id)
            if not definition.enabled or not next_due:
                continue
            try:
                due_at = datetime.fromisoformat(str(next_due).replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                continue
            if due_at > reference:
                continue
            due.append(
                ServiceTriggerEvent(
                    service_id=service_id,
                    trigger_kind="scheduled",
                    metadata={"scheduled_for": next_due},
                )
            )
            self.mark_completed(definition, now_utc=reference)
        return due

    def _compute_next_due(self, definition: ServiceDefinition, *, now_utc: datetime | None = None) -> str | None:
        if not definition.enabled:
            return None
        return compute_next_service_run_at_utc(definition.schedule, now_utc=now_utc)


@dataclass
class ServiceRunner(ServiceRunnerPort):
    triggered: list[ServiceTriggerEvent] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    repository: ServiceRepositoryPort | None = None

    def run(self, trigger: ServiceTriggerEvent) -> None:
        self.triggered.append(trigger)

    def begin_run(self, trigger: ServiceTriggerEvent) -> str | None:
        self.run(trigger)
        if self.repository is None:
            return None
        return self.repository.begin_run(trigger)

    def complete_run(self, service_run_id: str | None, *, turn_id: str, final_reply: str) -> None:
        self.results.append(
            {
                "service_run_id": service_run_id,
                "turn_id": turn_id,
                "final_reply": final_reply,
            }
        )
        if service_run_id is not None and self.repository is not None:
            self.repository.complete_run(service_run_id, turn_id=turn_id, final_reply=final_reply)

    def fail_run(self, service_run_id: str | None, *, error_text: str) -> None:
        self.results.append(
            {
                "service_run_id": service_run_id,
                "error_text": error_text,
            }
        )
        if service_run_id is not None and self.repository is not None:
            self.repository.fail_run(service_run_id, error_text=error_text)


@dataclass
class ServiceManager(ServiceManagerPort):
    registered: dict[str, ServiceDefinition] = field(default_factory=dict)
    trigger_mailbox: Mailbox[ServiceTriggerEvent] = field(default_factory=Mailbox)
    schedule_engine: ScheduleEngine = field(default_factory=ScheduleEngine)
    repository: ServiceRepositoryPort | None = None
    on_change: Callable[[], None] | None = None

    def register(self, service: ServiceDefinition) -> None:
        self.registered[service.service_id] = service
        next_due = self.schedule_engine.register(service)
        if self.repository is not None:
            self.repository.upsert_definition(service, next_due_at_utc=next_due)
        self._notify_change()

    def hydrate(self, service: ServiceDefinition, *, next_due_at_utc: str | None = None, persist: bool = False) -> None:
        self.registered[service.service_id] = service
        self.schedule_engine.next_due_by_service_id[service.service_id] = next_due_at_utc
        if persist and self.repository is not None:
            self.repository.upsert_definition(service, next_due_at_utc=next_due_at_utc)

    def enqueue_trigger(self, trigger: ServiceTriggerEvent) -> None:
        self.trigger_mailbox.put(trigger)

    def enqueue_due_triggers(self, *, now_utc: datetime | None = None) -> tuple[ServiceTriggerEvent, ...]:
        due = self.schedule_engine.collect_due(self.registered, now_utc=now_utc)
        for trigger in due:
            self.trigger_mailbox.put(trigger)
        return tuple(due)

    def mark_run_completed(self, service_id: str, *, now_utc: datetime | None = None) -> str | None:
        definition = self.registered.get(service_id)
        if definition is None:
            return None
        next_due = self.schedule_engine.mark_completed(definition, now_utc=now_utc)
        if self.repository is not None:
            self.repository.update_schedule_state(
                service_id,
                next_due_at_utc=next_due,
                last_run_at_utc=(now_utc or utc_now_dt()).isoformat(),
            )
        return next_due

    def create_service(
        self,
        *,
        service_id: str,
        goal: str,
        method: str = "",
        skill_refs: list[str] | None = None,
        out_channel_id: str | None = None,
        schedule: dict[str, object] | None = None,
        out_reply_target: dict[str, object] | None = None,
        enabled: bool = True,
    ) -> ServiceDefinition:
        definition = ServiceDefinition(
            service_id=service_id,
            goal=goal,
            method=method,
            skill_refs=list(skill_refs or []),
            out_channel_id=out_channel_id,
            schedule=dict(schedule or {}),
            out_reply_target=dict(out_reply_target or {}),
            enabled=enabled,
        )
        self.register(definition)
        return definition

    def destroy_service(self, service_id: str) -> bool:
        removed = self.registered.pop(service_id, None)
        self.schedule_engine.unregister(service_id)
        deleted = self.repository.delete_definition(service_id) if self.repository is not None else removed is not None
        if removed is not None or deleted:
            self._notify_change()
        return removed is not None or deleted

    def set_enabled(self, service_id: str, enabled: bool) -> ServiceDefinition | None:
        current = self.registered.get(service_id)
        if current is None:
            return None
        updated = ServiceDefinition(
            service_id=current.service_id,
            goal=current.goal,
            method=current.method,
            skill_refs=list(current.skill_refs),
            out_channel_id=current.out_channel_id,
            out_reply_target=dict(current.out_reply_target),
            schedule=dict(current.schedule),
            enabled=enabled,
        )
        self.register(updated)
        if not enabled:
            self.schedule_engine.next_due_by_service_id[service_id] = None
            if self.repository is not None:
                self.repository.upsert_definition(updated, next_due_at_utc=None)
        return updated

    def set_output_channel(self, service_id: str, out_channel_id: str | None) -> ServiceDefinition | None:
        current = self.registered.get(service_id)
        if current is None:
            return None
        updated = ServiceDefinition(
            service_id=current.service_id,
            goal=current.goal,
            method=current.method,
            skill_refs=list(current.skill_refs),
            out_channel_id=out_channel_id,
            out_reply_target=dict(current.out_reply_target),
            schedule=dict(current.schedule),
            enabled=current.enabled,
        )
        self.register(updated)
        return updated

    def set_output_target(self, service_id: str, out_reply_target: dict[str, object] | None) -> ServiceDefinition | None:
        current = self.registered.get(service_id)
        if current is None:
            return None
        updated = ServiceDefinition(
            service_id=current.service_id,
            goal=current.goal,
            method=current.method,
            skill_refs=list(current.skill_refs),
            out_channel_id=current.out_channel_id,
            out_reply_target=dict(out_reply_target or {}),
            schedule=dict(current.schedule),
            enabled=current.enabled,
        )
        self.register(updated)
        return updated

    def update_schedule(self, service_id: str, schedule: dict[str, object]) -> ServiceDefinition | None:
        current = self.registered.get(service_id)
        if current is None:
            return None
        updated = ServiceDefinition(
            service_id=current.service_id,
            goal=current.goal,
            method=current.method,
            skill_refs=list(current.skill_refs),
            out_channel_id=current.out_channel_id,
            out_reply_target=dict(current.out_reply_target),
            schedule=dict(schedule),
            enabled=current.enabled,
        )
        self.register(updated)
        return updated

    @property
    def pending_triggers(self) -> tuple[ServiceTriggerEvent, ...]:
        return self.trigger_mailbox.peek_all()

    def _notify_change(self) -> None:
        if self.on_change is not None:
            self.on_change()
