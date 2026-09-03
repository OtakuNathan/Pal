from __future__ import annotations

import unittest
from pathlib import Path

from pal.core.turn_events import (
    ALL_TURN_TOPICS,
    TURN_END,
    TURN_START,
    TURN_TOOL_CALL_AFTER,
    TURN_TOOL_CALL_BEFORE,
    TurnEventBus,
)


class TurnEventBusTests(unittest.TestCase):
    def test_subscribe_and_emit(self) -> None:
        bus = TurnEventBus()
        received: list[tuple[str, dict]] = []

        def handler(topic: str, event: dict) -> None:
            received.append((topic, event))

        bus.subscribe(TURN_START, handler)
        bus.emit(TURN_START, {"turn_id": "t1"})

        self.assertEqual(len(received), 1)
        self.assertEqual(received[0], (TURN_START, {"turn_id": "t1"}))

    def test_multiple_subscribers(self) -> None:
        bus = TurnEventBus()
        count_a: list[int] = []
        count_b: list[int] = []

        def handler_a(topic: str, event: dict) -> None:
            count_a.append(1)

        def handler_b(topic: str, event: dict) -> None:
            count_b.append(1)

        bus.subscribe(TURN_START, handler_a)
        bus.subscribe(TURN_START, handler_b)
        bus.emit(TURN_START, {"turn_id": "t1"})

        self.assertEqual(len(count_a), 1)
        self.assertEqual(len(count_b), 1)

    def test_no_subscribers_for_topic(self) -> None:
        bus = TurnEventBus()
        bus.emit(TURN_END, {"turn_id": "t1"})  # should not raise

    def test_unsubscribe(self) -> None:
        bus = TurnEventBus()
        received: list[int] = []

        def handler(topic: str, event: dict) -> None:
            received.append(1)

        bus.subscribe(TURN_START, handler)
        bus.emit(TURN_START, {"turn_id": "t1"})
        bus.unsubscribe(TURN_START, handler)
        bus.emit(TURN_START, {"turn_id": "t2"})

        self.assertEqual(len(received), 1)

    def test_no_duplicate_subscribe(self) -> None:
        bus = TurnEventBus()

        def handler(topic: str, event: dict) -> None:
            pass

        bus.subscribe(TURN_START, handler)
        bus.subscribe(TURN_START, handler)
        self.assertEqual(len(bus.subscribers_for(TURN_START)), 1)

    def test_subscribers_for_empty(self) -> None:
        bus = TurnEventBus()
        self.assertEqual(bus.subscribers_for(TURN_END), ())

    def test_emit_to_wrong_topic_does_not_reach_other(self) -> None:
        bus = TurnEventBus()
        received: list[tuple[str, dict]] = []

        def handler(topic: str, event: dict) -> None:
            received.append((topic, event))

        bus.subscribe(TURN_START, handler)
        bus.emit(TURN_END, {"turn_id": "t1"})

        self.assertEqual(len(received), 0)

    def test_subscriber_exception_is_captured_and_does_not_stop_emit(self) -> None:
        bus = TurnEventBus()
        received: list[tuple[str, dict]] = []

        def bad_handler(topic: str, event: dict) -> None:
            raise RuntimeError("boom")

        def good_handler(topic: str, event: dict) -> None:
            received.append((topic, event))

        bus.subscribe(TURN_START, bad_handler)
        bus.subscribe(TURN_START, good_handler)
        bus.emit(TURN_START, {"turn_id": "t1"})

        self.assertEqual(received, [(TURN_START, {"turn_id": "t1"})])
        self.assertEqual(len(bus.diagnostics), 1)
        self.assertEqual(bus.diagnostics[0]["kind"], "turn_event_subscriber_failed")
        self.assertEqual(bus.diagnostics[0]["topic"], TURN_START)
        self.assertIn("RuntimeError: boom", bus.diagnostics[0]["error"])

    def test_all_topics_defined(self) -> None:
        self.assertIn(TURN_START, ALL_TURN_TOPICS)
        self.assertIn(TURN_END, ALL_TURN_TOPICS)
        self.assertIn(TURN_TOOL_CALL_BEFORE, ALL_TURN_TOPICS)
        self.assertIn(TURN_TOOL_CALL_AFTER, ALL_TURN_TOPICS)


class OledTurnStateSubscriberTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.oled_plugin_dir = Path.home() / ".pal" / "plugins" / "community" / "oled_emotion"
        if not cls.oled_plugin_dir.exists():
            raise unittest.SkipTest("OLED community plugin not installed")

    def _make_subscriber(self) -> tuple:
        import sys
        dir_str = str(self.oled_plugin_dir)
        if dir_str not in sys.path:
            sys.path.insert(0, dir_str)

        from introspection import OledTurnStateSubscriber, _send_emotion as original_send

        sent: list[str] = []

        def mock_send(emotion: str, socket_path) -> str:
            sent.append(emotion)
            return "OK"

        import introspection as mod
        mod._send_emotion = mock_send

        mock_socket = Path("/tmp/test_oled.sock")
        sub = OledTurnStateSubscriber(socket_path=mock_socket)
        return sub, sent

    def test_attach_detach_resubscribes_turn_events(self) -> None:
        import sys
        dir_str = str(self.oled_plugin_dir)
        if dir_str not in sys.path:
            sys.path.insert(0, dir_str)

        from introspection import register_with_core

        class FakeContext:
            def __init__(self) -> None:
                self.turn_event_bus = TurnEventBus()
                self.modules = []

            def register_module(self, handle) -> None:
                self.modules.append(handle)

        context = FakeContext()
        handle = register_with_core(context, plugin_dir=self.oled_plugin_dir)
        provider = handle.introspection_provider
        provider.sidecar_manager.ensure_running = lambda: "already_running"
        provider.sidecar_manager.stop_sync = lambda: None

        for topic in (TURN_START, TURN_TOOL_CALL_BEFORE, TURN_TOOL_CALL_AFTER, TURN_END):
            self.assertEqual(context.turn_event_bus.subscribers_for(topic), ())

        provider._subscribe_turn_events()
        for topic in (TURN_START, TURN_TOOL_CALL_BEFORE, TURN_TOOL_CALL_AFTER, TURN_END):
            self.assertEqual(len(context.turn_event_bus.subscribers_for(topic)), 1)

        provider._unsubscribe_turn_events()
        for topic in (TURN_START, TURN_TOOL_CALL_BEFORE, TURN_TOOL_CALL_AFTER, TURN_END):
            self.assertEqual(context.turn_event_bus.subscribers_for(topic), ())

        provider._subscribe_turn_events()
        for topic in (TURN_START, TURN_TOOL_CALL_BEFORE, TURN_TOOL_CALL_AFTER, TURN_END):
            self.assertEqual(len(context.turn_event_bus.subscribers_for(topic)), 1)

        provider._unsubscribe_turn_events()
        for topic in (TURN_START, TURN_TOOL_CALL_BEFORE, TURN_TOOL_CALL_AFTER, TURN_END):
            self.assertEqual(context.turn_event_bus.subscribers_for(topic), ())

    def test_standby_to_thinking_on_turn_start(self) -> None:
        sub, sent = self._make_subscriber()
        sub(TURN_START, {"turn_id": "t1"})
        self.assertEqual(sub.state, "thinking")
        self.assertIn("thinking", sent)

    def test_turn_start_sends_thinking_without_local_wakeup_state(self) -> None:
        sub, sent = self._make_subscriber()
        sub(TURN_START, {"turn_id": "t1"})
        self.assertEqual(sub.state, "thinking")
        self.assertEqual(sent, ["thinking"])

    def test_sidecar_queue_inserts_wakeup_shock_when_sleeping(self) -> None:
        import sys
        dir_str = str(self.oled_plugin_dir)
        if dir_str not in sys.path:
            sys.path.insert(0, dir_str)

        from sidecar import EmotionRequest

        request = EmotionRequest()
        request.push("thinking", is_sleeping=True)

        first = request.pop()
        second = request.pop()

        self.assertEqual(first["emotion"], "shock")
        self.assertTrue(first["wakeup"])
        self.assertTrue(first["uninterruptible"])
        self.assertEqual(second["emotion"], "thinking")
        self.assertFalse(second["wakeup"])

    def test_thinking_to_working_on_tool_call(self) -> None:
        sub, sent = self._make_subscriber()
        sub.state = "thinking"
        sub(TURN_TOOL_CALL_BEFORE, {"turn_id": "t1", "tool_name": "shell"})
        self.assertEqual(sub.state, "working")
        self.assertIn("working", sent)

    def test_working_to_thinking_on_tool_result(self) -> None:
        sub, sent = self._make_subscriber()
        sub.state = "working"
        sub(TURN_TOOL_CALL_AFTER, {"turn_id": "t1", "tool_name": "shell", "ok": True})
        self.assertEqual(sub.state, "thinking")
        self.assertIn("thinking", sent)

    def test_turn_end_returns_to_standby(self) -> None:
        sub, sent = self._make_subscriber()
        sub.state = "thinking"
        sub(TURN_END, {"turn_id": "t1", "status": "success"})
        self.assertEqual(sub.state, "standby")
        self.assertIn("standby", sent)

    def test_turn_end_failed_shows_error(self) -> None:
        sub, sent = self._make_subscriber()
        sub.state = "thinking"
        sub(TURN_END, {"turn_id": "t1", "status": "failed"})
        self.assertEqual(sub.state, "standby")
        self.assertIn("error", sent)
        self.assertIn("standby", sent)

    def test_full_lifecycle(self) -> None:
        sub, sent = self._make_subscriber()
        sub(TURN_START, {"turn_id": "t1"})
        sub(TURN_TOOL_CALL_BEFORE, {"turn_id": "t1", "tool_name": "shell"})
        sub(TURN_TOOL_CALL_AFTER, {"turn_id": "t1", "tool_name": "shell", "ok": True})
        sub(TURN_TOOL_CALL_BEFORE, {"turn_id": "t1", "tool_name": "op_tool_read"})
        sub(TURN_TOOL_CALL_AFTER, {"turn_id": "t1", "tool_name": "op_tool_read", "ok": True})
        sub(TURN_END, {"turn_id": "t1", "status": "success"})
        self.assertEqual(sub.state, "standby")
        self.assertEqual(sent, ["thinking", "working", "thinking", "working", "thinking", "standby"])


if __name__ == "__main__":
    unittest.main()
