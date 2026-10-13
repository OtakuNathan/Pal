from __future__ import annotations

import unittest

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



if __name__ == "__main__":
    unittest.main()
