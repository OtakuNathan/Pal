from __future__ import annotations
from tests.core_factory import build_core

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from pal.channel.runtime import ChannelRuntime
from pal.channel.capabilities import subscribe_core_state
from pal.control import ControlAction, ControlRoute, ControlDelivery
from pal.core.runtime import PalCore
from pal.foundation import EventEnvelope
from pal.shared import EventKind
from pal.shared.maintenance import MaintenanceToken


class DreamingAdmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.core = build_core()
        self.channel = ChannelRuntime()
        self.core.context.port_registry["channel:channel"] = self.channel
        self.provider = SimpleNamespace(repository=SimpleNamespace(freeze=Mock()))
        self.route = ControlRoute(endpoint_id="test", channel_kind="test", reply_target={"chat_id": "123", "thread_id": "456"})
        self.channel.remember_user_route(self.route)
        self.addCleanup(subscribe_core_state(self.core.context, self.channel))

    async def test_sleep_broadcast_is_after_admission_and_cleared_on_service_recovery(self):
        first = SimpleNamespace(on_runtime_state=Mock())
        broken = SimpleNamespace(on_runtime_state=Mock(side_effect=RuntimeError("UI offline")))
        self.channel.endpoint_registry.endpoints.update(first=first, broken=broken)
        with self.assertRaises(RuntimeError):
            self.core.begin_memory_sleep(MaintenanceToken())
        token = await self.core.try_enter_memory_maintenance_async()
        self.assertIsNotNone(token)
        first.on_runtime_state.assert_not_called()
        with self.assertLogs("pal.channel.runtime", level="ERROR"):
            self.core.begin_memory_sleep(token)
        first.on_runtime_state.assert_called_once_with({"sleeping": True, "failures": [], "safe_modes": [], "turns": []})
        replacement = SimpleNamespace(endpoint=SimpleNamespace(endpoint_id="replacement"), on_runtime_state=Mock())
        self.channel._bind_endpoint_ready(replacement)
        replacement.on_runtime_state.assert_called_once_with({"sleeping": True, "failures": [], "safe_modes": [], "turns": []})
        with self.assertLogs("pal.channel.runtime", level="ERROR"):
            await self.core.leave_memory_maintenance_async(token)
        first.on_runtime_state.assert_called_with({"sleeping": False, "failures": [], "safe_modes": [], "turns": []})
        self.assertEqual(self.channel.runtime_state, {"sleeping": False, "failures": [], "safe_modes": [], "turns": []})

    async def test_admission_waits_for_ingress_and_leaves_storage_fencing_to_memory(self):
        self.core.state.memory_ingress_reservations = 1
        self.assertFalse(await self.core.try_enter_memory_maintenance_async())
        self.provider.repository.freeze.assert_not_called()
        self.core.state.memory_ingress_reservations = 0
        self.assertTrue(await self.core.try_enter_memory_maintenance_async())
        self.provider.repository.freeze.assert_not_called()
        self.assertTrue(self.core.state.memory_maintenance)

    async def test_direct_turn_holds_admission_across_await_and_releases_on_failure(self):
        async def admitted(envelope):
            self.assertFalse(await self.core.try_enter_memory_maintenance_async())
            raise RuntimeError("turn failed")
        self.core._process_admitted_channel_turn_async = admitted
        with self.assertRaisesRegex(RuntimeError, "turn failed"):
            await self.core.process_channel_turn_async(SimpleNamespace(
                event=SimpleNamespace(event_id="failed-direct-turn"),
            ))
        self.assertEqual(self.core.state.memory_ingress_reservations, 0)
        self.assertTrue(await self.core.try_enter_memory_maintenance_async())

    async def test_sleep_refuses_before_preparation_without_changing_route(self):
        self.core.state.memory_maintenance = True
        self.core._route_from_channel_envelope = Mock(return_value=ControlRoute(endpoint_id="other", channel_kind="test"))
        self.core._prepare_channel_turn_async = AsyncMock()
        self.core.deliver_memory_notice_async = AsyncMock(return_value=True)
        await self.core.schedule_channel_turn_async(SimpleNamespace())
        self.core._prepare_channel_turn_async.assert_not_awaited()
        self.assertFalse(self.core.state.pending_channel_turns)
        self.assertEqual(self.channel.last_user_route(), self.route)

    async def test_scheduled_turn_releases_ingress_on_failure_and_cancellation(self):
        self.core.remember_user_route = Mock()
        for error in (RuntimeError("prepare failed"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                self.core.state.memory_maintenance_changed.clear()
                self.core._schedule_admitted_channel_turn_async = AsyncMock(side_effect=error)
                with self.assertRaises(type(error)):
                    await self.core.schedule_channel_turn_async(SimpleNamespace())
                self.assertEqual(self.core.state.memory_ingress_reservations, 0)
                self.assertTrue(self.core.state.memory_maintenance_changed.is_set())
        self.assertTrue(await self.core.try_enter_memory_maintenance_async())

    async def test_admission_does_not_wait_for_bunshin_processes(self):
        self.core.context.port_registry["bunshin:bunshin"] = SimpleNamespace(active_runs=["long-running-worker"])
        token = await self.core.try_enter_memory_maintenance_async()
        self.assertIsNotNone(token)
        await self.core.leave_memory_maintenance_async(token)
        self.assertFalse(self.core.state.memory_maintenance)

    async def test_sleep_blocks_mutating_controls(self):
        self.core.state.memory_maintenance = True
        self.core.deliver_memory_notice_async = AsyncMock(return_value=True)
        self.core._handle_admitted_control_action_async = AsyncMock()
        await self.core.handle_control_action_async(ControlAction(action_kind="reset_memory", target_scope="memory", route=self.route))
        self.core._handle_admitted_control_action_async.assert_not_awaited()

    async def test_busy_turn_completion_wakes_waiting_admission(self):
        task = asyncio.create_task(asyncio.sleep(0))
        self.core.state.turn_tasks["turn"] = task
        self.assertFalse(await self.core.try_enter_memory_maintenance_async())
        waiter = asyncio.create_task(self.core.wait_for_memory_maintenance_async())
        await task
        self.core._on_turn_task_done("turn", task)
        await asyncio.wait_for(waiter, timeout=1)
        self.assertTrue(await self.core.try_enter_memory_maintenance_async())

    async def test_notification_waits_for_delivery_and_reports_failure(self):
        for event_kind, expected in ((EventKind.REPLY_DELIVERED, True), (EventKind.REPLY_FAILED, False)):
            waiter = asyncio.create_task(self.channel.wait_for_reply("notice"))
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            self.channel.mailbox.put(EventEnvelope(event_kind=event_kind, source_kind="channel", payload={"reply_id": "notice"}))
            self.assertEqual(await waiter, expected)
            self.channel.mailbox.drain()

    async def test_stale_or_copied_token_cannot_change_the_current_maintenance(self):
        first = await self.core.try_enter_memory_maintenance_async()
        self.assertIsNotNone(first)
        copied = MaintenanceToken(token_id=first.token_id)
        self.assertFalse(await self.core.leave_memory_maintenance_async(copied))
        with self.assertRaisesRegex(RuntimeError, "current maintenance owner"):
            self.core.begin_memory_sleep(copied)
        self.assertTrue(await self.core.leave_memory_maintenance_async(first))
        second = await self.core.try_enter_memory_maintenance_async()
        self.core.begin_memory_sleep(second, component="second")
        self.core.state.memory_maintenance_changed.clear()
        self.assertFalse(await self.core.leave_memory_maintenance_async(first))
        with self.assertRaisesRegex(RuntimeError, "current maintenance owner"):
            self.core.begin_memory_sleep(first, component="stale")
        self.assertTrue(self.core.state.memory_maintenance)
        self.assertFalse(self.core.state.memory_maintenance_changed.is_set())
        self.assertIs(self.core.control_runtime.snapshot().memory_maintenance_token, second)
        self.assertEqual(self.core.state.memory_maintenance_component, "second")
        self.assertTrue(await self.core.leave_memory_maintenance_async(second))

    async def test_control_delivery_waits_for_the_channel_receipt(self):
        self.core._route_to_channel_envelope = Mock(return_value=object())
        self.core._delivery_binding_for_envelope = Mock(return_value=object())
        for event_kind, expected in ((EventKind.REPLY_DELIVERED, True), (EventKind.REPLY_FAILED, False)):
            reply_id = str(event_kind)
            self.channel.queue_reply = Mock(return_value=reply_id)
            waiter = asyncio.create_task(self.core.control_runtime.deliver(
                ControlDelivery(delivery_kind="reply", route=self.route, text="Pal will sleep"),
                require_provider=True, wait=True))
            await asyncio.sleep(0)
            self.assertFalse(waiter.done())
            self.channel.mailbox.put(EventEnvelope(event_kind=event_kind, source_kind="channel",
                                                   payload={"reply_id": reply_id}))
            self.assertEqual(await waiter, expected)
            self.channel.mailbox.drain()
