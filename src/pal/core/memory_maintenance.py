"""Core-owned admission boundary for resident memory maintenance."""
from __future__ import annotations

from pal.control import ControlDelivery
from pal.core.core_events import MEMORY_SLEEP
from pal.shared.maintenance import MaintenanceToken, owns_maintenance


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

    async def try_enter_memory_maintenance_async(self) -> MaintenanceToken | None:
        async with self.state.channel_turn_transition_lock:
            self.state.memory_maintenance_changed.clear()
            if self.memory_maintenance_busy():
                return None
            token = MaintenanceToken()
            self.state.memory_maintenance = True
            self.state.memory_maintenance_token = token
            self.state.memory_maintenance_component = "maintenance"
            return token

    async def wait_for_memory_maintenance_async(self) -> None:
        await self.state.memory_maintenance_changed.wait()

    def begin_memory_sleep(self, token: MaintenanceToken, *, component: str = "maintenance") -> None:
        """Publish sleep after maintenance admission without a channel notice."""
        if not owns_maintenance(self.state.memory_maintenance_token, token):
            raise RuntimeError("memory sleep requires the current maintenance owner")
        self.state.memory_maintenance_component = component
        self.context.core_event_bus.emit(MEMORY_SLEEP, {
            "subsystem": "memory", "component": component, "sleeping": True,
        })

    async def leave_memory_maintenance_async(self, token: MaintenanceToken) -> bool:
        async with self.state.channel_turn_transition_lock:
            if not owns_maintenance(self.state.memory_maintenance_token, token):
                return False
            self.state.memory_maintenance = False
            self.state.memory_maintenance_token = None
            self.context.core_event_bus.emit(MEMORY_SLEEP, {
                "subsystem": "memory", "component": self.state.memory_maintenance_component, "sleeping": False,
            })
            self.state.memory_maintenance_changed.set()
        self.notify_ready()
        return True

    def memory_notification_route(self):
        channel = self.context.port_registry.get("channel:channel")
        return channel.last_user_route() if channel is not None else None

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
