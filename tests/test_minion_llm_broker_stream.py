from __future__ import annotations

import asyncio
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import PropertyMock, patch

import msgpack

from pal.foundation.sidecar import (
    SidecarEndpoint,
    SidecarRpcClient,
    pack_sidecar_message,
    read_sidecar_message,
    start_sidecar_server,
)
from pal.llm.ir import (
    GenerationPolicyIR,
    LLMFinishReason,
    LLMMessageIR,
    LLMRequestIR,
    LLMResponseDeltaKind,
    LLMResponseIR,
    LLMResponseUpdate,
    MessageRole,
    MessageState,
    TextPartIR,
)
from pal.llm.serde import request_to_payload, response_to_payload
from pal.minion.llm_broker import (
    BrokerStreamDecoder,
    MinionBrokerLLMRuntime,
    stream_update_to_payload,
)
from pal.minion.ipc import cleanup_manager_endpoint, start_manager_server
from pal.minion.manager import MinionManager


def _request() -> LLMRequestIR:
    return LLMRequestIR(
        messages=(LLMMessageIR(MessageRole.USER, (TextPartIR("work"),)),),
        tools=(),
        policy=GenerationPolicyIR(max_output_tokens=100),
    )


def _updates(chunks: int = 4, width: int = 5) -> list[LLMResponseUpdate]:
    message_id = "stream-message"
    text = ""
    updates: list[LLMResponseUpdate] = []
    for index in range(chunks):
        delta = chr(ord("a") + (index % 26)) * width
        text += delta
        response = LLMResponseIR(
            LLMMessageIR(
                MessageRole.ASSISTANT,
                (TextPartIR(text),),
                message_id=message_id,
                state=MessageState.IN_PROGRESS,
            ),
            LLMFinishReason.STOP,
        )
        updates.append(
            LLMResponseUpdate(
                response,
                LLMResponseDeltaKind.TEXT,
                text_delta=delta,
            )
        )
    final = LLMResponseIR(
        LLMMessageIR(
            MessageRole.ASSISTANT,
            (TextPartIR(text),),
            message_id=message_id,
            state=MessageState.COMPLETE,
        ),
        LLMFinishReason.STOP,
    )
    updates.append(LLMResponseUpdate(final, LLMResponseDeltaKind.STATE))
    return updates


class MinionLLMBrokerStreamTests(unittest.TestCase):
    def test_manager_refreshes_only_an_already_loaded_host_runtime(self) -> None:
        async def scenario() -> None:
            root = Path(tempfile.mkdtemp(prefix="pal-minion-llm-refresh-"))
            manager = MinionManager(root)

            cold = await manager.refresh_llm_endpoints()
            self.assertFalse(cold["runtime_loaded"])
            self.assertFalse(cold["refreshed"])

            class Runtime:
                calls = 0

                def refresh_llm_endpoints(inner_self):
                    inner_self.calls += 1
                    return {"enabled_count": 2}

            runtime = Runtime()
            manager._host_broker_bundle = type("Bundle", (), {"llm_runtime": runtime})()
            loaded = await manager.refresh_llm_endpoints()

            self.assertEqual(runtime.calls, 1)
            self.assertTrue(loaded["runtime_loaded"])
            self.assertTrue(loaded["refreshed"])
            self.assertEqual(loaded["runtime"]["enabled_count"], 2)

        asyncio.run(scenario())

    def test_broker_wire_is_delta_only_until_one_terminal_response(self) -> None:
        updates = _updates(chunks=100, width=20)
        payloads = [stream_update_to_payload(item) for item in updates]

        self.assertTrue(all("response" not in item for item in payloads[:-1]))
        self.assertIn("response", payloads[-1])

        decoder = BrokerStreamDecoder()
        decoded = [decoder.feed(item) for item in payloads]
        self.assertEqual(decoded[-1].response, updates[-1].response)
        self.assertTrue(decoder.terminal_seen)

        compact_size = sum(len(msgpack.packb(item, use_bin_type=True)) for item in payloads)
        cumulative_size = sum(
            len(msgpack.packb(response_to_payload(item.response), use_bin_type=True))
            for item in updates
        )
        self.assertLess(compact_size * 10, cumulative_size)

    def test_sidecar_stream_delivers_first_item_before_terminal(self) -> None:
        async def scenario() -> None:
            root = Path(tempfile.mkdtemp(prefix="pal-sidecar-stream-"))
            endpoint = SidecarEndpoint(root, "stream-test")
            release_terminal = asyncio.Event()

            async def handler(reader, writer) -> None:
                request = await read_sidecar_message(reader)
                request_id = str(request["id"])
                writer.write(
                    pack_sidecar_message(
                        {
                            "type": "stream_item",
                            "id": request_id,
                            "ok": True,
                            "result": {"value": 1},
                        }
                    )
                )
                await writer.drain()
                await release_terminal.wait()
                writer.write(
                    pack_sidecar_message(
                        {
                            "type": "stream_end",
                            "id": request_id,
                            "ok": True,
                            "result": {},
                        }
                    )
                )
                await writer.drain()
                writer.close()
                await writer.wait_closed()

            server, _ = await start_sidecar_server(endpoint, handler)
            try:
                client = SidecarRpcClient(endpoint, request_timeout_seconds=1)
                stream = client.stream("demo").__aiter__()
                self.assertEqual(
                    await asyncio.wait_for(anext(stream), timeout=1),
                    {"value": 1},
                )
                release_terminal.set()
                with self.assertRaises(StopAsyncIteration):
                    await asyncio.wait_for(anext(stream), timeout=1)
            finally:
                server.close()
                await server.wait_closed()

        asyncio.run(scenario())

    def test_minion_broker_reconstructs_body_equivalent_updates_incrementally(self) -> None:
        async def scenario() -> None:
            source = _updates()
            release_second = asyncio.Event()

            class Client:
                async def stream(self, _method, _params):
                    yield {"update": stream_update_to_payload(source[0])}
                    await release_second.wait()
                    for update in source[1:]:
                        yield {"update": stream_update_to_payload(update)}

            runtime = MinionBrokerLLMRuntime(Path("/tmp/pal-broker-stream"), "run")
            with patch.object(
                MinionBrokerLLMRuntime,
                "_client",
                new_callable=PropertyMock,
                return_value=Client(),
            ):
                stream = runtime.astream(_request()).__aiter__()
                first = await asyncio.wait_for(anext(stream), timeout=1)
                self.assertEqual(first.response, source[0].response)
                release_second.set()
                remaining = [item async for item in stream]
            self.assertEqual(remaining[-1].response, source[-1].response)

        asyncio.run(scenario())

    def test_manager_consumes_runtime_astream_without_collecting_legacy_list(self) -> None:
        async def scenario() -> None:
            source = _updates()

            class Runtime:
                async def astream(self, _request):
                    for update in source:
                        yield update

            class Manager:
                def _require_broker_run(self, _params):
                    return object()

                async def _llm_broker_runtime(self):
                    return Runtime()

                def _llm_progress_sink(self, _state):
                    return lambda *_args, **_kwargs: None

            manager = Manager()
            observed = [
                item
                async for item in MinionManager.llm_broker_stream_updates(
                    manager,
                    {"run_id": "run", "request": request_to_payload(_request())},
                )
            ]
            self.assertEqual(observed, source)

        asyncio.run(scenario())

    def test_manager_to_minion_stream_is_live_end_to_end(self) -> None:
        async def scenario() -> None:
            root = Path(tempfile.mkdtemp(prefix="pal-minion-stream-e2e-"))
            source = _updates()
            release_remaining = asyncio.Event()

            class Runtime:
                async def astream(self, _request):
                    yield source[0]
                    await release_remaining.wait()
                    for update in source[1:]:
                        yield update

            class Manager:
                logger = logging.getLogger("test.minion.stream")
                _handle_client = MinionManager._handle_client
                _serve_llm_stream_request = MinionManager._serve_llm_stream_request
                llm_broker_stream_updates = MinionManager.llm_broker_stream_updates

                def _require_broker_run(self, _params):
                    return object()

                async def _llm_broker_runtime(self):
                    return Runtime()

                def _llm_progress_sink(self, _state):
                    return lambda *_args, **_kwargs: None

            manager = Manager()
            server, _ = await start_manager_server(root, manager._handle_client)
            try:
                broker = MinionBrokerLLMRuntime(
                    root,
                    "run",
                    request_timeout_seconds=1,
                )
                stream = broker.astream(_request()).__aiter__()
                first = await asyncio.wait_for(anext(stream), timeout=1)
                self.assertEqual(first.response, source[0].response)
                release_remaining.set()
                remaining = [item async for item in stream]
                self.assertEqual(remaining[-1].response, source[-1].response)
            finally:
                server.close()
                await server.wait_closed()
                await cleanup_manager_endpoint(root)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
