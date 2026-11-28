from __future__ import annotations

from dataclasses import dataclass, field, replace
from collections.abc import Callable
from datetime import datetime, timezone

from pal.proactive.contracts import (
    ScheduleEnginePort,
    ProactiveDefinition,
    ProactiveManagerPort,
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
            self.register(definition, now_utc=reference)
        return due

    def _compute_next_due(self, definition: ProactiveDefinition, *, now_utc: datetime | None = None) -> str | None:
        if not definition.enabled:
            return None
        return compute_next_proactive_run_at_utc(definition.schedule, now_utc=now_utc)


@dataclass
class ProactiveManager(ProactiveManagerPort):
    registered: dict[str, ProactiveDefinition] = field(default_factory=dict)
    schedule_engine: ScheduleEngine = field(default_factory=ScheduleEngine)
    repository: ProactiveRepositoryPort | None = None
    on_change: Callable[[], None] | None = None
    on_ready: Callable[[], None] | None = None

    def register(self, definition: ProactiveDefinition) -> None:
        previous = self.registered.get(definition.proactive_id)
        if previous is not None and previous.schedule == definition.schedule and previous.enabled == definition.enabled:
            next_due = self.schedule_engine.next_due_at(definition.proactive_id)
        else:
            next_due = self.schedule_engine._compute_next_due(definition)
        self._register_at(definition, next_due)

    def _register_at(self, definition: ProactiveDefinition, next_due: str | None) -> None:
        if self.repository is not None:
            self.repository.upsert_definition(definition, next_due_at_utc=next_due)
        self.registered[definition.proactive_id] = definition
        self.schedule_engine.next_due_by_proactive_id[definition.proactive_id] = next_due
        self._notify_change()
        self._notify_ready()

    def ensure_silent_once(self, *, proactive_id: str, goal: str, method: str) -> str:
        if proactive_id in self.registered:
            return proactive_id
        # Persist the due cursor explicitly: normal schedule parsing discards
        # past timestamps, while an internal handoff must be due immediately.
        due = utc_now_dt().isoformat()
        definition = ProactiveDefinition(
            proactive_id=proactive_id, goal=goal, method=method,
            out_channel_id="null", schedule={"cadence": "once", "run_at_utc": due},
        )
        self._register_at(definition, due)
        return proactive_id

    def hydrate(self, definition: ProactiveDefinition, *, next_due_at_utc: str | None = None, persist: bool = False) -> None:
        self.registered[definition.proactive_id] = definition
        self.schedule_engine.next_due_by_proactive_id[definition.proactive_id] = next_due_at_utc
        if persist and self.repository is not None:
            self.repository.upsert_definition(definition, next_due_at_utc=next_due_at_utc)
        self._notify_ready()

    def enqueue_trigger(self, trigger: ProactiveTriggerEvent) -> None:
        if self.repository is None:
            raise RuntimeError("proactive dispatch requires a repository")
        definition = self.registered.get(trigger.proactive_id)
        if definition is not None:
            self.repository.queue_background_run(
                trigger, definition, next_due=self.schedule_engine.next_due_at(trigger.proactive_id),
            )
            self._notify_ready()

    def enqueue_due_triggers(self, *, now_utc: datetime | None = None) -> tuple[ProactiveTriggerEvent, ...]:
        if self.repository is None:
            raise RuntimeError("proactive dispatch requires a repository")
        before = dict(self.schedule_engine.next_due_by_proactive_id)
        due = self.schedule_engine.collect_due(self.registered, now_utc=now_utc)
        for index, trigger in enumerate(due):
            try:
                self.enqueue_trigger(trigger)
            except Exception:
                for remaining in due[index:]:
                    self.schedule_engine.next_due_by_proactive_id[remaining.proactive_id] = before[remaining.proactive_id]
                raise
        return tuple(due)

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
        deleted = self.repository.delete_definition(proactive_id) if self.repository is not None else False
        removed = self.registered.pop(proactive_id, None)
        self.schedule_engine.unregister(proactive_id)
        if removed is not None or deleted:
            self._notify_change()
            self._notify_ready()
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

    def set_output_destination(
        self, proactive_id: str, out_channel_id: str | None, out_reply_target: dict[str, object],
    ) -> ProactiveDefinition | None:
        current = self.registered.get(proactive_id)
        if current is None:
            return None
        updated = replace(current, out_channel_id=out_channel_id, out_reply_target=dict(out_reply_target))
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
        if self.repository is not None:
            return tuple(ProactiveTriggerEvent(run.proactive_id, run.trigger_kind, run.trigger_metadata)
                         for run in self.repository.pending_background_runs())
        return ()

    def _notify_change(self) -> None:
        if self.on_change is not None:
            self.on_change()
