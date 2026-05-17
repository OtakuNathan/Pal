from __future__ import annotations

from dataclasses import dataclass, field
from collections.abc import Callable
from typing import Any
from datetime import datetime, timezone

from pal.core.mailbox import Mailbox
from pal.proactive.contracts import (
    ScheduleEnginePort,
    ProactiveDefinition,
    ProactiveManagerPort,
    ProactiveRunnerPort,
    ProactiveTriggerEvent,
)
from pal.proactive.repository import ProactiveRepositoryPort
from pal.proactive.scheduling import compute_next_proactive_run_at_utc, utc_now_dt


@dataclass
class ScheduleEngine(ScheduleEnginePort):
    next_due_by_proactive_id: dict[str, str | None] = field(default_factory=dict)

    def next_due_at(self, proactive_id: str) -> str | None:
        return self.next_due_by_proactive_id.get(proactive_id)

    def register(self, definition: ProactiveDefinition, *, now_utc: datetime | None = None) -> str | None:
        next_due = self._compute_next_due(definition, now_utc=now_utc)
        self.next_due_by_proactive_id[definition.proactive_id] = next_due
        return next_due

    def unregister(self, proactive_id: str) -> None:
        self.next_due_by_proactive_id.pop(proactive_id, None)

    def mark_completed(self, definition: ProactiveDefinition, *, now_utc: datetime | None = None) -> str | None:
        next_due = self._compute_next_due(definition, now_utc=now_utc)
        self.next_due_by_proactive_id[definition.proactive_id] = next_due
        return next_due

    def collect_due(self, definitions: dict[str, ProactiveDefinition], *, now_utc: datetime | None = None) -> list[ProactiveTriggerEvent]:
        reference = now_utc or utc_now_dt()
        due: list[ProactiveTriggerEvent] = []
        for proactive_id, definition in sorted(definitions.items()):
            next_due = self.next_due_by_proactive_id.get(proactive_id)
            if not definition.enabled or not next_due:
                continue
            try:
                due_at = datetime.fromisoformat(str(next_due).replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                continue
            if due_at > reference:
                continue
            due.append(
                ProactiveTriggerEvent(
                    proactive_id=proactive_id,
                    trigger_kind="scheduled",
                    metadata={"scheduled_for": next_due},
                )
            )
            self.mark_completed(definition, now_utc=reference)
        return due

    def _compute_next_due(self, definition: ProactiveDefinition, *, now_utc: datetime | None = None) -> str | None:
        if not definition.enabled:
            return None
        return compute_next_proactive_run_at_utc(definition.schedule, now_utc=now_utc)


@dataclass
class ProactiveRunner(ProactiveRunnerPort):
    triggered: list[ProactiveTriggerEvent] = field(default_factory=list)
    results: list[dict[str, Any]] = field(default_factory=list)
    repository: ProactiveRepositoryPort | None = None

    def run(self, trigger: ProactiveTriggerEvent) -> None:
        self.triggered.append(trigger)

    def begin_run(self, trigger: ProactiveTriggerEvent) -> str | None:
        self.run(trigger)
        if self.repository is None:
            return None
        return self.repository.begin_run(trigger)

    def complete_run(self, proactive_run_id: str | None, *, turn_id: str, final_reply: str) -> None:
        self.results.append(
            {
                "proactive_run_id": proactive_run_id,
                "turn_id": turn_id,
                "final_reply": final_reply,
            }
        )
        if proactive_run_id is not None and self.repository is not None:
            self.repository.complete_run(proactive_run_id, turn_id=turn_id, final_reply=final_reply)

    def fail_run(self, proactive_run_id: str | None, *, error_text: str) -> None:
        self.results.append(
            {
                "proactive_run_id": proactive_run_id,
                "error_text": error_text,
            }
        )
        if proactive_run_id is not None and self.repository is not None:
            self.repository.fail_run(proactive_run_id, error_text=error_text)


@dataclass
class ProactiveManager(ProactiveManagerPort):
    registered: dict[str, ProactiveDefinition] = field(default_factory=dict)
    trigger_mailbox: Mailbox[ProactiveTriggerEvent] = field(default_factory=Mailbox)
    schedule_engine: ScheduleEngine = field(default_factory=ScheduleEngine)
    repository: ProactiveRepositoryPort | None = None
    on_change: Callable[[], None] | None = None
    on_ready: Callable[[], None] | None = None

    def __post_init__(self) -> None:
        self.trigger_mailbox.on_put = self._notify_ready

    def register(self, definition: ProactiveDefinition) -> None:
        self.registered[definition.proactive_id] = definition
        next_due = self.schedule_engine.register(definition)
        if self.repository is not None:
            self.repository.upsert_definition(definition, next_due_at_utc=next_due)
        self._notify_change()
        self._notify_ready()

    def hydrate(self, definition: ProactiveDefinition, *, next_due_at_utc: str | None = None, persist: bool = False) -> None:
        self.registered[definition.proactive_id] = definition
        self.schedule_engine.next_due_by_proactive_id[definition.proactive_id] = next_due_at_utc
        if persist and self.repository is not None:
            self.repository.upsert_definition(definition, next_due_at_utc=next_due_at_utc)
        self._notify_ready()

    def enqueue_trigger(self, trigger: ProactiveTriggerEvent) -> None:
        self.trigger_mailbox.put(trigger)

    def enqueue_due_triggers(self, *, now_utc: datetime | None = None) -> tuple[ProactiveTriggerEvent, ...]:
        due = self.schedule_engine.collect_due(self.registered, now_utc=now_utc)
        for trigger in due:
            self.trigger_mailbox.put(trigger)
        return tuple(due)

    def mark_run_completed(self, proactive_id: str, *, now_utc: datetime | None = None) -> str | None:
        definition = self.registered.get(proactive_id)
        if definition is None:
            return None
        next_due = self.schedule_engine.mark_completed(definition, now_utc=now_utc)
        if self.repository is not None:
            self.repository.update_schedule_state(
                proactive_id,
                next_due_at_utc=next_due,
                last_run_at_utc=(now_utc or utc_now_dt()).isoformat(),
            )
        self._notify_ready()
        return next_due

    def seconds_until_next_due(self, *, now_utc: datetime | None = None) -> float | None:
        reference = now_utc or utc_now_dt()
        nearest: float | None = None
        for proactive_id, definition in self.registered.items():
            if not definition.enabled:
                continue
            raw_due = self.schedule_engine.next_due_by_proactive_id.get(proactive_id)
            if not raw_due:
                continue
            try:
                due_at = datetime.fromisoformat(str(raw_due).replace("Z", "+00:00")).astimezone(timezone.utc)
            except ValueError:
                continue
            delay = max(0.0, (due_at - reference).total_seconds())
            nearest = delay if nearest is None else min(nearest, delay)
        return nearest

    def _notify_ready(self) -> None:
        if self.on_ready is not None:
            self.on_ready()

    def create_task(
        self,
        *,
        proactive_id: str,
        goal: str,
        method: str = "",
        skill_refs: list[str] | None = None,
        out_channel_id: str | None = None,
        schedule: dict[str, object] | None = None,
        out_reply_target: dict[str, object] | None = None,
        enabled: bool = True,
    ) -> ProactiveDefinition:
        definition = ProactiveDefinition(
            proactive_id=proactive_id,
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

    def destroy_task(self, proactive_id: str) -> bool:
        removed = self.registered.pop(proactive_id, None)
        self.schedule_engine.unregister(proactive_id)
        deleted = self.repository.delete_definition(proactive_id) if self.repository is not None else removed is not None
        if removed is not None or deleted:
            self._notify_change()
        return removed is not None or deleted

    def set_enabled(self, proactive_id: str, enabled: bool) -> ProactiveDefinition | None:
        current = self.registered.get(proactive_id)
        if current is None:
            return None
        updated = ProactiveDefinition(
            proactive_id=current.proactive_id,
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
            self.schedule_engine.next_due_by_proactive_id[proactive_id] = None
            if self.repository is not None:
                self.repository.upsert_definition(updated, next_due_at_utc=None)
        return updated

    def set_output_channel(self, proactive_id: str, out_channel_id: str | None) -> ProactiveDefinition | None:
        current = self.registered.get(proactive_id)
        if current is None:
            return None
        updated = ProactiveDefinition(
            proactive_id=current.proactive_id,
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

    def set_output_target(self, proactive_id: str, out_reply_target: dict[str, object] | None) -> ProactiveDefinition | None:
        current = self.registered.get(proactive_id)
        if current is None:
            return None
        updated = ProactiveDefinition(
            proactive_id=current.proactive_id,
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

    def update_schedule(self, proactive_id: str, schedule: dict[str, object]) -> ProactiveDefinition | None:
        current = self.registered.get(proactive_id)
        if current is None:
            return None
        updated = ProactiveDefinition(
            proactive_id=current.proactive_id,
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
    def pending_triggers(self) -> tuple[ProactiveTriggerEvent, ...]:
        return self.trigger_mailbox.peek_all()

    def _notify_change(self) -> None:
        if self.on_change is not None:
            self.on_change()
