import asyncio
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock

from pal.channel.capabilities import TypingSubscriber
from pal.core import PalCore
from pal.core.turn_events import TURN_END, TURN_START


def test_typing_end_uses_start_route_and_is_idempotent():
    runtime = Mock()
    subscriber = TypingSubscriber(runtime)
    start = {"turn_id": "a", "endpoint_id": "tg", "reply_target": {"chat_id": 1, "thread_id": 2}}
    subscriber(TURN_START, start)
    subscriber(TURN_START, start)
    subscriber(TURN_END, {"turn_id": "a", "endpoint_id": "", "reply_target": {"chat_id": 1}})
    subscriber(TURN_END, start)
    assert runtime.queue_endpoint_status.call_count == 2
    runtime.queue_endpoint_status.assert_called_with(
        "tg", "working_stop", reply_target=start["reply_target"],
        payload={"typing_owner": "turn:a", "turn_id": "a"},
    )
    assert subscriber._routes == {}


class ContinuationTypingTests(IsolatedAsyncioTestCase):
    async def test_direct_continuation_pairs_events_even_on_failure(self):
        core = PalCore()
        events = []
        for topic in (TURN_START, TURN_END):
            core.context.turn_event_bus.subscribe(topic, lambda topic, event: events.append(topic))
        continuation = SimpleNamespace(turn_id="direct", delivery_binding=None, opening_event=None, control_scope_key="")
        core._begin_tool_result_turn = Mock()
        core._run_turn_continuation_async = AsyncMock(side_effect=ValueError("preflight failed"))
        try:
            with self.assertRaisesRegex(ValueError, "preflight failed"):
                await core.run_turn_continuation_async(continuation)
            self.assertEqual(events, [TURN_START, TURN_END])
        finally:
            core.state.active_turns.clear()
            core.close()

    async def test_tracked_continuation_does_not_duplicate_task_events(self):
        core = PalCore()
        events = []
        for topic in (TURN_START, TURN_END):
            core.context.turn_event_bus.subscribe(topic, lambda topic, event: events.append(topic))
        continuation = SimpleNamespace(turn_id="tracked", delivery_binding=None, opening_event=None, control_scope_key="")
        core.state.turn_tasks["tracked"] = asyncio.current_task()
        core._begin_tool_result_turn = Mock()
        core._run_turn_continuation_async = AsyncMock(return_value="done")
        try:
            self.assertEqual(await core.run_turn_continuation_async(continuation), "done")
            self.assertEqual(events, [])
        finally:
            core.state.turn_tasks.clear()
            core.state.active_turns.clear()
            core.close()


def test_buffered_typing_statuses_do_not_coalesce_different_owners():
    from pal.channel.runtime import ChannelEndpointHub
    from pal.channel.contracts import QueuedStatus
    from pal.shared import EndpointConfig, ResponseHandle

    hub = ChannelEndpointHub(endpoint_id="tg")
    endpoint = EndpointConfig("tg", "telegram", "test")
    handle = ResponseHandle(endpoint_id="tg", reply_target={"chat_id": 42})
    for kind in ("typing_start", "working_stop"):
        for owner in ("a", "b", "b"):
            item = QueuedStatus(status_id=f"{kind}:{owner}", response_handle=handle,
                                endpoint=endpoint, kind=kind, payload={"typing_owner": owner})
            hub.append("status", item.status_id, item)
    assert [(entry.item.kind, entry.item.payload["typing_owner"]) for entry in hub.buffer] == [
        ("typing_start", "a"), ("typing_start", "b"), ("working_stop", "a"), ("working_stop", "b")]
