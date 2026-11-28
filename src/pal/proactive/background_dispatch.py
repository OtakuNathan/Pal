"""The Proactive scheduler only owns occurrence admission and history."""
from __future__ import annotations

from dataclasses import dataclass
import time

from pal.core.events import EventHandler, EventSource
from pal.foundation import EventEnvelope
from pal.foundation.diagnostics import exception_reason
from pal.proactive.runtime import ProactiveManager
from pal.proactive.repository import ProactiveRepositoryPort
from pal.shared import EventKind, SourceKind
from pal.shared.background_protocol import BACKGROUND_WORKFLOWS
from pal.shared.timers import TimerScopePort, TimerHandlePort


@dataclass
class BackgroundScheduleSource(EventSource):
    manager: ProactiveManager
    timers: TimerScopePort
    source_id: str = "proactive.background"
    timer: TimerHandlePort | None = None
    next_poll: float = 0.0

    def prepare(self, context) -> bool:
        from pal.control.runtime_contracts import CORE_CONTROL
        host = context.port_registry.optional(CORE_CONTROL)
        if host is not None and host.snapshot().memory_maintenance:
            return False
        try:
            self.manager.enqueue_due_triggers()
            pending = bool(self.manager.pending_triggers)
        except Exception as exc:
            # A failed admission preserves the due cursor. Retry from the
            # durable schedule without taking down the resident event pump.
            exception_reason(exc)
            self._arm_timer(2)
            return False
        now = time.monotonic()
        if pending and now >= self.next_poll:
            self.next_poll = now + 2
            return True
        delay = self.manager.seconds_until_next_due()
        if pending:
            poll = max(0.01, self.next_poll - now)
            delay = poll if delay is None else min(delay, poll)
        self._arm_timer(delay)
        return False

    def _arm_timer(self, delay: float | None) -> None:
        if self.timer is not None:
            self.timer.cancel()
            self.timer = None
        if delay is not None:
            async def wake():
                self.timer = None
                self.manager._notify_ready()
            self.timer = self.timers.schedule(max(0.01, delay), wake)

    def drain(self, context) -> list[EventEnvelope]:
        return [EventEnvelope(event_kind=EventKind.PROACTIVE_TRIGGER, source_kind=SourceKind.PROACTIVE, payload={})]


@dataclass
class BackgroundScheduleHandler(EventHandler):
    repository: ProactiveRepositoryPort

    def can_handle(self, event_kind: str) -> bool:
        return event_kind == EventKind.PROACTIVE_TRIGGER

    async def handle(self, event: EventEnvelope, context) -> list[EventEnvelope]:
        backend = context.port_registry.optional(BACKGROUND_WORKFLOWS)
        if backend is None:
            return []
        runs = self.repository.pending_background_runs()
        if not runs:
            return []
        # Previously admitted work wins over queued occurrences.
        run = next((r for r in runs if r.status != "queued"), runs[0])
        try:
            if run.status == "dispatched":
                workflow_id = str(run.trigger_metadata["workflow_id"])
                status = (await backend.scheduled_run_status(workflow_id)).get("workflow") or {}
                state = str(status.get("workflow_state") or "").lower()
                if state in {"completed", "cancelled", "rejected"}:
                    delivery = status.get("delivery") or {}
                    self.repository.set_background_state(run.proactive_run_id, state,
                        result=str(delivery.get("result_text") or state), error="")
                return []
            dispatch = run.trigger_metadata.get("dispatch_request")
            if dispatch is None:
                definition = dict(run.trigger_metadata["definition"])
                route = {}
                channel_id = definition.get("out_channel_id")
                if channel_id and channel_id != "null":
                    # Resolve once, before dispatch. Never silently drop a
                    # configured destination or change it after an ambiguous ACK.
                    channel = context.port_registry.get("channel:channel")
                    try:
                        if channel is None:
                            raise ValueError("Channel runtime is unavailable")
                        target = channel.resolve_output_destination(
                            endpoint_id=channel_id,
                            reply_target=definition.get("out_reply_target") or None,
                        )
                        endpoint = channel.get_endpoint(channel_id)
                        route = {**target, "channel_kind": endpoint.endpoint.channel_kind}
                    except Exception as exc:
                        self.repository.set_background_state(
                            run.proactive_run_id, "failed",
                            error=f"Proactive output channel {channel_id!r}: {exception_reason(exc)}",
                        )
                        return []
                dispatch = {
                    "run_id": run.proactive_run_id, "task_name": run.proactive_id,
                    "goal": definition["goal"], "method": definition.get("method", ""),
                    "skill_refs": definition.get("skill_refs", []),
                    "delivery_binding": route, "discard_output": channel_id == "null",
                }
            dispatch = dict(dispatch)
            self.repository.set_background_state(run.proactive_run_id, "submitting", dispatch_request=dispatch)
            response = await backend.start_scheduled_run(dispatch)
            if response.get("status") == "busy":
                return []
            if response.get("status") == "rejected":
                self.repository.set_background_state(run.proactive_run_id, "failed", error=str(response.get("error") or "workflow admission rejected"))
                return []

            self.repository.set_background_state(run.proactive_run_id, "dispatched",
                                                  workflow_id=response["workflow_id"], error="")
        except Exception as exc:
            self.repository.set_background_state(run.proactive_run_id, "submitting" if run.status != "dispatched" else "dispatched",
                                                error=exception_reason(exc))
        return []

    async def shutdown(self) -> None:
        # The Bunshin workflow is independent of this plugin generation.
        pass
