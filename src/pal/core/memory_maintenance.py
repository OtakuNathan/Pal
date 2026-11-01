"""Core-owned admission boundary for resident memory maintenance."""
from __future__ import annotations

import json
from dataclasses import asdict

from pal.control import ControlDelivery
from pal.core.core_events import MEMORY_SLEEP
from pal.memory.dreaming.contracts import DreamingFailureNotice
from pal.shared.proactive_protocol import SILENT_ONCE_SCHEDULER


SLEEP_REPLY = "Pal is sleeping. This message was not accepted or queued; please try again after it wakes."


class MemoryMaintenanceMixin:
    def memory_maintenance_busy(self) -> bool:
        channel = self.context.port_registry.get("channel:channel")
        mailbox = getattr(channel, "mailbox", None)
        return bool(self.state.memory_maintenance or self.state.resident_quiescing
                    or self.state.memory_ingress_reservations or self.state.memory_control_reservations
                    or self.state.pending_channel_turns or self.main_loop.queue
                    or (mailbox is not None and mailbox.has_pending())
                    or self.turn_manager.latest_active_turn_id() is not None
                    or any(not task.done() for task in self.state.turn_tasks.values()))

    async def try_enter_memory_maintenance_async(self, provider) -> bool:
        async with self.state.channel_turn_transition_lock:
            self.state.memory_maintenance_changed.clear()
            if self.memory_maintenance_busy():
                return False
            # No await between admission and the writer fence. Repository
            # mutations share this fence, including statistics and indexing.
            provider.repository.freeze()
            self.state.memory_maintenance = True
            return True

    async def wait_for_memory_maintenance_async(self) -> None:
        await self.state.memory_maintenance_changed.wait()

    def begin_memory_sleep(self) -> None:
        """Publish sleep only after admission and the required notice succeed."""
        if not self.state.memory_maintenance:
            raise RuntimeError("memory sleep requires maintenance admission")
        self.context.core_event_bus.emit(MEMORY_SLEEP, {
            "subsystem": "memory", "component": "dreaming", "sleeping": True,
        })

    async def leave_memory_maintenance_async(self) -> None:
        async with self.state.channel_turn_transition_lock:
            self.state.memory_maintenance = False
            self.context.core_event_bus.emit(MEMORY_SLEEP, {
                "subsystem": "memory", "component": "dreaming", "sleeping": False,
            })
            self.state.memory_maintenance_changed.set()
        self.notify_ready()

    def memory_notification_route(self):
        channel = self.context.port_registry.get("channel:channel")
        return channel.last_user_route() if channel is not None else None

    def schedule_memory_failure_reminder(self, notice: DreamingFailureNotice) -> str | None:
        scheduler = self.context.port_registry.optional(SILENT_ONCE_SCHEDULER)
        if scheduler is None:
            return None
        return scheduler.ensure_silent_once(
            proactive_id=f"memory-dreaming-failure-{notice.run_id}",
            goal="Record this dreaming failure as a pending user notice in shared L1.",
            method=(
                "The user has NOT been notified. Produce a short handoff note, in the user's "
                "language, for the next ordinary user conversation to mention before answering "
                "their message. Include the run id, failure summary and diagnostic log path. "
                "These are facts at the time of failure, not a claim about the current runtime. "
                "Output is intentionally discarded by the null channel; your turn remains in L1. "
                "Do not call tools, send a message, retry dreaming or change configuration. "
                "Do not claim the user has already been told. Failure facts: "
                + json.dumps(asdict(notice), ensure_ascii=False)
            ),
        )

    async def deliver_memory_notice_async(self, route, text, *, require_provider=True) -> bool:
        if not require_provider:
            return await self._deliver_control_delivery_async(
                ControlDelivery(delivery_kind="reply", route=route, text=text))
        envelope = self._route_to_channel_envelope(route, require_provider=True)
        if envelope is None:
            return False
        channel = self.context.require_port("channel:channel")
        reply_id = channel.queue_reply(self._delivery_binding_for_envelope(envelope), text)
        return await channel.wait_for_reply(reply_id)

    def remember_user_route(self, envelope) -> None:
        channel = self.context.port_registry.get("channel:channel")
        remember = getattr(channel, "remember_user_route", None)
        if callable(remember):
            remember(self._route_from_channel_envelope(envelope))
