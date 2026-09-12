from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pal.control import ControlAction, ControlRoute
from pal.core import PalCore


class RuntimeStatusTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.core = PalCore()
        self.core._deliver_control_delivery_async = AsyncMock()
        self.route = ControlRoute(endpoint_id="test", channel_kind="test")
        self.action = ControlAction(
            action_kind="show_status", target_scope="runtime", route=self.route,
        )

    async def status(self) -> str:
        self.core._deliver_control_delivery_async.reset_mock()
        await self.core.handle_control_action_async(self.action)
        self.core._deliver_control_delivery_async.assert_awaited_once()
        delivery = self.core._deliver_control_delivery_async.await_args.args[0]
        self.assertEqual(delivery.route, self.route)
        return delivery.text

    async def test_status_tracks_active_turn_and_queue_without_consuming_messages(self) -> None:
        self.assertIn("State: idle", await self.status())
        self.core.state.active_turns["turn"] = SimpleNamespace()
        queued = SimpleNamespace()
        self.core.state.pending_channel_turns.append(queued)

        message = await self.status()
        self.assertIn("State: in_turn", message)
        self.assertIn("Active turns: 1", message)
        self.assertIn("Queued messages: 1", message)
        self.assertEqual(list(self.core.state.active_turns), ["turn"])
        self.assertIs(self.core.state.pending_channel_turns[0], queued)

        self.core.state.active_turns.clear()
        self.assertIn("State: queued", await self.status())
        self.core.state.pending_channel_turns.clear()
        message = await self.status()
        self.assertIn("State: idle", message)
        self.assertIn("Active turns: 0", message)
        self.assertIn("Queued messages: 0", message)

    async def test_status_remains_available_during_memory_maintenance(self) -> None:
        self.core.state.memory_maintenance = True
        message = await self.status()
        self.assertIn("State: memory_maintenance", message)
        self.assertNotIn("State: idle", message)
        self.assertTrue(self.core.state.memory_maintenance)

    async def test_status_does_not_report_idle_while_quiescing(self) -> None:
        self.core.state.resident_quiescing = True
        self.assertIn("State: quiescing", await self.status())

    async def test_status_available_without_llm_statistics(self) -> None:
        message = await self.status()
        self.assertIn("State: idle", message)
        self.assertIn("LLM statistics unavailable.", message)
