"""Backpressure must not turn interruption into a host failure."""
import asyncio
from collections import deque
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pal.channel.contracts import ChannelDeliveryError, ChannelStreamUpdate, EndpointConfig, ResponseHandle
from pal.channel.endpoints.socket_endpoint import SocketChannelEndpoint, _SocketSession
from pal.channel.runtime import ChannelRuntime
from pal.core.contracts import CoreRuntimeState
from pal.core.runtime import TurnManager
from pal.core.turns import TurnContinuation
from pal.shared import ChannelStreamUpdateKind, QueuedStreamUpdate, TurnDeliveryBinding


def endpoint_and_handle(capacity=2):
    endpoint = SocketChannelEndpoint(endpoint=EndpointConfig("socket", "socket", "unused.sock"))
    session = _SocketSession("session", Mock(), outbound=asyncio.Queue(maxsize=capacity), ready_notified=True)
    endpoint.sessions[session.session_id] = session
    handle = ResponseHandle(endpoint_id="socket", reply_target={"session_id": "session", "request_id": "request"})
    return endpoint, session, handle


@pytest.mark.parametrize("occupied", [1, 2])
def test_abort_retries_terminal_frames_without_partial_enqueue_or_old_updates(occupied):
    endpoint, session, handle = endpoint_and_handle()
    for _ in range(occupied):
        session.outbound.put_nowait({"type": "occupied"})
    endpoint.queue_stream_update(ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="stale"), response_handle=handle)
    endpoint.abort_stream(handle)
    assert session.outbound.qsize() == occupied
    assert id(handle) not in endpoint._stream_sessions
    assert id(handle) not in endpoint._streamed_text_handles
    assert [item.update.kind for item in endpoint.stream_update_outbox] == [ChannelStreamUpdateKind.ERROR, ChannelStreamUpdateKind.DONE]
    while not session.outbound.empty():
        session.outbound.get_nowait()
        session.outbound.task_done()
    endpoint.flush_stream_update_outbox()
    assert [session.outbound.get_nowait()["type"] for _ in range(2)] == ["llm_error", "llm_done"]
    assert not endpoint.stream_update_outbox
    assert not endpoint._stream_sessions


def test_channel_cleans_shared_buffers_even_when_endpoint_abort_fails():
    endpoint, _, handle = endpoint_and_handle()
    channel = ChannelRuntime()
    channel.register_endpoint(endpoint)
    other = ResponseHandle(endpoint_id="socket", reply_target={"session_id": "session", "request_id": "other"})
    def queued(target, key):
        return QueuedStreamUpdate(key, target, endpoint.endpoint, ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="pending"))
    aborted, retained = queued(handle, "abort"), queued(other, "keep")
    channel.stream_update_outbox = deque([aborted, retained])
    hub = channel.endpoint_hubs["socket"]
    hub.append("stream", "abort", aborted)
    hub.append("stream", "keep", retained)
    endpoint.abort_stream = Mock(side_effect=ChannelDeliveryError("blocked", reason="transport_backpressure"))
    with pytest.raises(ChannelDeliveryError):
        channel.abort_stream(handle)
    assert list(channel.stream_update_outbox) == [retained]
    assert [entry.item for entry in hub.buffer] == [retained]
    assert hub.buffered_text_bytes == sum(entry.text_bytes for entry in hub.buffer)


@pytest.mark.parametrize("async_abort", [False, True])
def test_interrupt_cancels_execution_despite_sync_or_async_output_failure(async_abort):
    async def run():
        endpoint, _, handle = endpoint_and_handle()
        failure = ChannelDeliveryError("blocked", reason="transport_backpressure")
        abort = AsyncMock(side_effect=failure) if async_abort else Mock(side_effect=failure)
        execution = SimpleNamespace(interrupt_turn=AsyncMock())
        context = SimpleNamespace(port_registry={"agent_io:output": SimpleNamespace(abort_stream=abort)}, execution_runtime=execution)
        state = CoreRuntimeState()
        continuation = TurnContinuation("turn", iter(()), "request", delivery_binding=TurnDeliveryBinding(endpoint.endpoint, handle, "scope"))
        state.active_turns["turn"] = continuation
        state.active_turn_id = "turn"
        task = asyncio.create_task(asyncio.sleep(3600))
        state.turn_tasks["turn"] = task
        manager = TurnManager(context=context, state=state)
        try:
            assert await manager.interrupt_active_turn()
            execution.interrupt_turn.assert_awaited_once_with("turn")
            assert task.cancelling()
            assert continuation.interrupted
            assert state.resident_interrupt_task is None
            assert state.resident_interrupting_turn_id is None
            assert any(item["kind"] == "channel.abort_stream_failed" for item in state.diagnostics)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
    asyncio.run(run())


@pytest.mark.parametrize("stall", ["ack", "drain"])
def test_writer_stall_closes_connection_and_preserves_replay(stall):
    async def run():
        endpoint, session, _ = endpoint_and_handle()
        endpoint.delivery_timeout_seconds = 0.01
        session.delivery_ack_enabled = stall == "ack"
        session.writer.drain = AsyncMock(side_effect=(lambda: None))
        if stall == "drain":
            async def drain():
                await asyncio.Event().wait()
            session.writer.drain = drain
        session.outbound.put_nowait({"type": "text_delta", "text": "retained"})
        ready = Mock()
        endpoint.on_ready = ready
        await asyncio.wait_for(endpoint._writer_loop(session), 1)
        assert session.closed
        session.writer.close.assert_called_once()
        ready.assert_not_called()
        assert not session.delivery_ack_waiters
        endpoint._recover_session_outbound(session)
        assert endpoint._unacknowledged_frames[0]["text"] == "retained"
        assert "timed out" in endpoint.last_delivery_error
    asyncio.run(run())
