"""System observations are independent of delivery and plugin execution."""
import asyncio
from queue import Empty
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pal.core.core_events import (
    CoreEventBus, MEMORY_SLEEP, FAILURE_STARTED, FAILURE_FINISHED,
    SAFE_MODE_STARTED, SAFE_MODE_FINISHED, TURN_TOOL_CALL_FAILED,
    TURN_TOOL_CALL_BEFORE, TURN_TOOL_CALL_AFTER, RUNTIME_SNAPSHOT,
)
from pal.core.main_context import MainContext
from pal.core.runtime import PalCore
from pal.plugins.lifecycle import PluginScope
from pal.failure import FailureSignal
from tests.test_failure_flow import _core_with_failure_runtime, _RecordingFailureLLM


def test_snapshot_loss_resync_isolation_and_unsubscribe():
    bus = CoreEventBus()
    sub = bus.open_subscription(max_pending=2)
    assert sub.get(timeout=0)[0] == RUNTIME_SNAPSHOT
    bus.emit(MEMORY_SLEEP, {"sleeping": True})
    bus.emit(FAILURE_STARTED, {"failure_id": "one", "subsystem": "channel"})
    bus.emit(FAILURE_FINISHED, {"failure_id": "one"})
    topic, snapshot = sub.get(timeout=0)
    assert topic == RUNTIME_SNAPSHOT
    assert snapshot["sleeping"] and not snapshot["failures"]
    snapshot["sleeping"] = False
    assert bus.snapshot()["sleeping"]
    assert sub.dropped == 2
    sub.close()
    bus.emit(MEMORY_SLEEP, {"sleeping": False})
    with pytest.raises(Empty):
        sub.get(timeout=0)


def test_candidate_plugin_receives_nothing_until_published_and_close_discards():
    context = MainContext()
    assert context.core_event_bus is context.turn_event_bus
    scope = PluginScope(context, "display")
    sub = scope.subscribe_core_events({MEMORY_SLEEP})
    context.core_event_bus.emit(MEMORY_SLEEP, {"sleeping": True})
    with pytest.raises(Empty):
        sub.get(timeout=0)
    scope.published = True
    scope.publish_core_subscriptions()
    assert sub.get(timeout=0)[1]["sleeping"]
    context.core_event_bus.emit(MEMORY_SLEEP, {"sleeping": False})
    assert sub.get(timeout=0) == (MEMORY_SLEEP, {"sleeping": False})
    assert scope.close() == []
    assert not context.core_event_bus.subscribers_for(MEMORY_SLEEP)


def test_dreaming_state_is_visible_without_any_channel():
    async def run():
        core = PalCore()
        core.state.memory_maintenance = True
        sub = core.context.core_event_bus.open_subscription({MEMORY_SLEEP})
        sub.get(timeout=0)
        core.begin_memory_sleep()
        assert sub.get(timeout=0)[1] == {"subsystem": "memory", "component": "dreaming", "sleeping": True}
        await core.leave_memory_maintenance_async()
        assert not sub.get(timeout=0)[1]["sleeping"]
    asyncio.run(run())


@pytest.mark.parametrize("subsystem,safe", [("channel", True), ("llm", False), ("persistence", False)])
def test_failure_events_cover_reports_and_actual_safe_mode_without_private_body(subsystem, safe):
    core = _core_with_failure_runtime()
    core.context.port_registry["llm:llm"] = _RecordingFailureLLM()
    events = []
    for topic in (FAILURE_STARTED, FAILURE_FINISHED, SAFE_MODE_STARTED, SAFE_MODE_FINISHED):
        core.context.core_event_bus.subscribe(topic, lambda topic, event: events.append((topic, event)))
    asyncio.run(core.handle_failure_async(FailureSignal(
        subsystem=subsystem, component="endpoint-one", failure_kind="delivery_failure",
        severity="medium", primary_blocker="PRIVATE BODY", evidence={"secret": "PRIVATE BODY"},
    ), origin="test"))
    assert [topic for topic, _ in events] == ([FAILURE_STARTED, SAFE_MODE_STARTED, SAFE_MODE_FINISHED, FAILURE_FINISHED]
                                           if safe else [FAILURE_STARTED, FAILURE_FINISHED])
    assert len({event["failure_id"] for _, event in events}) == 1
    assert all(event["subsystem"] == subsystem for _, event in events)
    assert "PRIVATE BODY" not in repr(events)
    assert core.context.core_event_bus.snapshot()["failures"] == []
    assert core.context.core_event_bus.snapshot()["safe_modes"] == []


def test_interrupted_safe_mode_clears_only_its_own_failure():
    async def run():
        core = _core_with_failure_runtime()
        entered = asyncio.Event()
        async def generate(request):
            entered.set()
            await asyncio.Event().wait()
        core.context.port_registry["llm:llm"] = SimpleNamespace(agenerate=generate)
        bus = core.context.core_event_bus
        bus.emit(FAILURE_STARTED, {"failure_id": "other"})
        task = asyncio.create_task(core.handle_failure_async(FailureSignal(
            subsystem="channel", component="ep", failure_kind="delivery_failure",
            severity="medium", primary_blocker="offline",
        ), origin="test"))
        await entered.wait()
        assert len(bus.snapshot()["failures"]) == 2
        assert len(bus.snapshot()["safe_modes"]) == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert bus.snapshot()["failures"] == [{"failure_id": "other"}]
        assert bus.snapshot()["safe_modes"] == []
    asyncio.run(run())


def test_tool_failure_is_observed_before_escalation():
    from pal.core.turns import ToolCallEffect
    from pal.shared import ToolExecutionResult
    from pal.shared.tool_protocol import new_tool_call

    async def run():
        core = PalCore()
        executor = core.turn_executor
        executor._build_tool_call_budget = lambda *args, **kwargs: None
        executor._log_tool_call_start = lambda *args: None
        executor._execute_tool_async = AsyncMock(return_value=ToolExecutionResult(name="probe", ok=False, text="failed", llm_text="failed"))
        executor._should_enter_failure_flow_for_tool_result = lambda result: True
        seen = []
        bus = core.context.core_event_bus
        for topic in (TURN_TOOL_CALL_BEFORE, TURN_TOOL_CALL_FAILED, TURN_TOOL_CALL_AFTER):
            bus.subscribe(topic, lambda topic, event: seen.append((topic, event)))
        async def escalate(*args, **kwargs):
            assert [topic for topic, _ in seen] == [TURN_TOOL_CALL_BEFORE, TURN_TOOL_CALL_FAILED]
            assert seen[-1][1]["call_id"] == "call-one"
            raise RuntimeError("stop after observing escalation boundary")
        executor._handle_failure_async = escalate
        continuation = SimpleNamespace(turn_id="t", pending_tool_call_batch=[], finalization_only=False)
        effect = ToolCallEffect(tool_call=new_tool_call(name="probe", args={}, call_id="call-one"))
        with pytest.raises(RuntimeError, match="escalation boundary"):
            await executor._handle_tool_call(effect, continuation)
    asyncio.run(run())


def test_socket_ready_finishes_drain_even_when_hub_has_no_pending_deliveries(tmp_path):
    from pal.channel.runtime import ChannelRuntime
    from pal.channel.endpoints.socket_endpoint import SocketChannelEndpoint
    from pal.channel.contracts import EndpointConfig

    async def run():
        channel = ChannelRuntime()
        endpoint = SocketChannelEndpoint(
            endpoint=EndpointConfig("socket", "socket", "socket.sock"),
            socket_path=tmp_path / "socket.sock",
        )
        outbound = asyncio.Queue()
        outbound.put_nowait({"type": "runtime_state"})
        endpoint.sessions["one"] = SimpleNamespace(
            session_id="one", ready_notified=True, closed=False, outbound=outbound,
            inflight_payload=None, delivery_ack_waiters={},
        )
        channel.register_endpoint(endpoint)
        hub = channel.get_endpoint_hub("socket")
        assert hub.state == "draining"
        assert not hub.buffer and not hub.transport_backlog
        outbound.get_nowait()
        endpoint.on_ready()
        assert hub.state == "attached"
    asyncio.run(run())


def test_socket_observations_skip_ack_and_replay_but_chat_keeps_ack(tmp_path):
    from pal.channel.endpoints.socket_endpoint import SocketChannelEndpoint
    from pal.channel.contracts import EndpointConfig

    class DisplayEndpoint(SocketChannelEndpoint):
        def is_ephemeral_frame(self, frame):
            return frame.get("type") in {"core_event", "runtime_state"}

    async def run():
        endpoint = DisplayEndpoint(
            endpoint=EndpointConfig("socket", "socket", "socket.sock"),
            socket_path=tmp_path / "socket.sock",
        )
        drained = asyncio.Queue()
        class Writer:
            def write(self, payload):
                pass

            async def drain(self):
                drained.put_nowait(dict(session.inflight_payload))

        session = SimpleNamespace(
            session_id="display", outbound=asyncio.Queue(), writer=Writer(),
            delivery_ack_enabled=True, delivery_ack_waiters={}, inflight_payload=None,
            closed=False, outbound_recovered=False,
        )
        session.outbound.put_nowait({"type": "core_event"})
        session.outbound.put_nowait({"type": "text_delta", "text": "hello"})
        task = asyncio.create_task(endpoint._writer_loop(session))
        try:
            observation = await asyncio.wait_for(drained.get(), 1)
            chat = await asyncio.wait_for(drained.get(), 1)
            assert "_pal_delivery_id" not in observation
            assert chat["_pal_delivery_id"] in session.delivery_ack_waiters
            session.outbound.put_nowait({"type": "runtime_state"})
        finally:
            task.cancel()
            await task
        endpoint._recover_session_outbound(session)
        assert list(endpoint._unacknowledged_frames) == [chat]

    asyncio.run(run())
