"""Exercise timeout/reconnect against real temporary Unix sockets, not Pal."""
import asyncio
import tempfile
from pathlib import Path

import pytest

from pal.channel.contracts import ChannelDeliveryError, ChannelStreamUpdate, EndpointConfig, ResponseHandle
from pal.channel.endpoints.socket_endpoint import SocketChannelEndpoint
from pal.channel.endpoints.socket_protocol import pack_socket_message, read_socket_message
from pal.shared import ChannelStreamUpdateKind


def test_stalled_ack_reconnect_preserves_delivery_id_and_pending_outbox():
    async def run():
        endpoint = SocketChannelEndpoint(
            endpoint=EndpointConfig("test", "socket", str(socket_path)),
            delivery_timeout_seconds=0.05,
        )
        clients = []
        async def wait_for(predicate):
            async with asyncio.timeout(2):
                while not predicate():
                    await asyncio.sleep(0.001)
        async def connect():
            reader, writer = await asyncio.open_unix_connection(str(endpoint.socket_path))
            clients.append(writer)
            writer.write(pack_socket_message({"type": "session_ready", "delivery_ack_v1": True}))
            await writer.drain()
            await wait_for(lambda: any(s.ready_notified for s in endpoint.sessions.values()))
            return reader, writer
        async def ack(writer, frame):
            writer.write(pack_socket_message({"type": "delivery_ack", "delivery_id": frame["_pal_delivery_id"]}))
            await writer.drain()
        await endpoint.start_async()
        try:
            reader, writer = await connect()
            session = next(iter(endpoint.sessions.values()))
            handle = ResponseHandle(endpoint_id="test", reply_target={"session_id": session.session_id, "request_id": "request"})
            endpoint.send_stream_update(handle, ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="first"))
            original = await asyncio.wait_for(read_socket_message(reader), 2)
            # No ACK: the endpoint must end this session without losing ownership.
            endpoint.queue_stream_update(ChannelStreamUpdate(kind=ChannelStreamUpdateKind.TEXT_DELTA, text="second"), response_handle=handle)
            await wait_for(lambda: not endpoint.sessions)
            assert await asyncio.wait_for(reader.read(), 2) == b""
            assert len(endpoint._unacknowledged_frames) == 1
            with pytest.raises(ChannelDeliveryError) as failure:
                endpoint._require_session(handle)
            assert not failure.value.permanent
            endpoint.flush_stream_update_outbox()
            assert len(endpoint.stream_update_outbox) == 1

            endpoint.delivery_timeout_seconds = 2
            endpoint.on_ready = endpoint.flush_stream_update_outbox
            replacement_reader, replacement_writer = await connect()
            replay = await asyncio.wait_for(read_socket_message(replacement_reader), 2)
            assert replay == original  # Includes stable ACK id for peer dedupe.
            await ack(replacement_writer, replay)
            pending = await asyncio.wait_for(read_socket_message(replacement_reader), 2)
            assert pending["text"] == "second"
            assert pending["request_id"] == "request"
            await ack(replacement_writer, pending)
            await endpoint.quiesce_delivery_async()
            assert not endpoint.stream_update_outbox
            assert not endpoint._unacknowledged_frames
            replacement = next(iter(endpoint.sessions.values()))
            assert not replacement.delivery_ack_waiters
            assert not replacement.closed
            assert endpoint._require_session(handle) is replacement
        finally:
            for writer in clients:
                writer.close()
            await endpoint.stop_async()
            await asyncio.gather(*(writer.wait_closed() for writer in clients), return_exceptions=True)
    # macOS sun_path is only 104 bytes; pytest's nested temp path can exceed it.
    with tempfile.TemporaryDirectory(prefix="pal-sock-", dir="/tmp") as directory:
        socket_path = Path(directory) / "test.sock"
        asyncio.run(run())
