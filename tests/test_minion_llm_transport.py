from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from pal.foundation import PalV2Database
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.endpoint_spec import endpoint_spec_fingerprint
from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    LLMUsageIR,
    MessageRole,
    TextPartIR,
    WireShape,
)
from pal.llm.models import LLMEndpointModel, PalRuntimeSettingModel
from pal.llm.repository import LLMEndpointRepository, RuntimeSettingRepository
from pal.llm.shapes.base import _JSONFrame as JSONFrame
from pal.llm.transport import (
    EncodedTransportRequest,
    LLMEndpointSpecStaleError,
    LLMProviderStartedError,
    LLMStreamCancelledError,
    StreamControl,
)
from pal.minion.ipc import cleanup_manager_endpoint, start_manager_server
from pal.minion.llm_transport import ManagerProxyTransport
from pal.minion.manager import MinionManager, MinionRunState
from pal.minion.manager_main import _open_runtime_database
from pal.shared import MinionInvocationPack
from pal.wizard.runtime import ALL_MODELS


class _CapturingTransport:
    def __init__(self, *, block: bool = False) -> None:
        self.requests: list[EncodedTransportRequest] = []
        self.block = block
        self.started = threading.Event()
        self.cancelled = threading.Event()

    def frames(self, _endpoint, request):
        self.requests.append(request)
        control = request.stream_control
        if control is not None:
            control.touch_network()
        self.started.set()
        if self.block:
            while control is not None and not control.cancelled:
                self.cancelled.wait(0.01)
            self.cancelled.set()
            if control is not None:
                control.raise_if_cancelled()
            return
        yield JSONFrame(
            0,
            {
                "choices": [
                    {"delta": {"content": "ok"}, "finish_reason": None}
                ]
            },
        )
        yield JSONFrame(
            1,
            {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            },
        )

    def close(self) -> None:
        return None


class _FailingAfterStartTransport:
    def frames(self, _endpoint, request):
        if request.stream_control is not None:
            request.stream_control.touch_network()
        raise RuntimeError("provider connection failed after acceptance")
        yield  # pragma: no cover - keep this method an iterator

    def close(self) -> None:
        return None


def _register_endpoint() -> object:
    endpoint = LLMEndpointRepository().upsert(
        endpoint_id="proxy-endpoint",
        provider="OpenAI",
        model_id="proxy-model",
        display_name="Proxy",
        wire_shape="openai_completion",
        base_url="http://provider.invalid/v1",
        auth_kind="local_provider_auth",
        credential_ref="",
        context_window=8192,
        max_output_tokens=512,
        thinking_levels_blob=["off"],
        default_thinking_level="off",
        supports_tools=True,
        supports_streaming=True,
        supports_vision=False,
        input_modalities_blob=["text"],
        output_modalities_blob=["text"],
        priority=0,
        enabled=True,
        capabilities_blob={},
        notes="test",
    )
    RuntimeSettingRepository().set_active_llm_endpoint_id(endpoint.endpoint_id)
    return endpoint


def _manager(root: Path) -> tuple[PalV2Database, MinionManager]:
    database = PalV2Database(db_path=root / "pal.sqlite3")
    database.initialize(ALL_MODELS)
    return database, MinionManager(root)


def _params(endpoint, *, request_id: str = "request-1") -> dict:
    return {
        "run_id": "run-1",
        "request_id": request_id,
        "endpoint_id": endpoint.endpoint_id,
        "endpoint_spec_fingerprint": endpoint_spec_fingerprint(endpoint),
        "wire_shape": endpoint.wire_shape,
        "timeout_seconds": 30,
        "stream": True,
        "payload": {"model": endpoint.model_id, "max_tokens": 64},
        "extra_body": {},
    }


class MinionLLMTransportTests(unittest.TestCase):
    def test_manager_process_bootstrap_binds_endpoint_database_read_only(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal-minion-manager-database-"))
        writable = PalV2Database(db_path=root / "pal.sqlite3")
        writable.initialize((LLMEndpointModel, PalRuntimeSettingModel))
        RuntimeSettingRepository().set_active_llm_endpoint_id("glm-test")
        writable.close()

        manager_database = _open_runtime_database(root)
        try:
            self.assertEqual(
                RuntimeSettingRepository().get_active_llm_endpoint_id(),
                "glm-test",
            )
            self.assertTrue(manager_database.read_only)
        finally:
            manager_database.close()

    def test_direct_and_proxy_transports_receive_identical_provider_payloads(self) -> None:
        async def scenario() -> None:
            root = Path(tempfile.mkdtemp(prefix="pal-minion-transport-parity-"))
            database, manager = _manager(root)
            endpoint = _register_endpoint()
            manager.runs["run-1"] = MinionRunState(
                minion_id="minion-1",
                run_id="run-1",
                pack=MinionInvocationPack(invocation_id="minion-1"),
            )
            direct_capture = _CapturingTransport()
            proxy_capture = _CapturingTransport()
            manager._llm_json_transport = proxy_capture  # type: ignore[assignment]
            server, _ = await start_manager_server(root, manager._handle_client)
            request = LLMRequestIR(
                messages=(
                    LLMMessageIR(
                        MessageRole.USER,
                        (TextPartIR("same request"),),
                    ),
                ),
                tools=(),
                policy=GenerationPolicyIR(max_output_tokens=64),
                logical_scope_id="minion:parity",
            )
            try:
                direct = ShapeEndpointInvoker(transport=direct_capture)
                proxy = ShapeEndpointInvoker(
                    transport=ManagerProxyTransport(
                        root,
                        "run-1",
                        request_timeout_seconds=2,
                    )
                )
                direct_updates = await asyncio.to_thread(
                    lambda: list(direct.invoke_updates(endpoint, request))
                )
                proxy_updates = await asyncio.to_thread(
                    lambda: list(proxy.invoke_updates(endpoint, request))
                )

                self.assertEqual(
                    direct_capture.requests[0].payload,
                    proxy_capture.requests[0].payload,
                )
                self.assertEqual(
                    direct_capture.requests[0].extra_body,
                    proxy_capture.requests[0].extra_body,
                )
                direct_response = direct_updates[-1].response
                proxy_response = proxy_updates[-1].response
                self.assertEqual(direct_response.text, proxy_response.text)
                self.assertEqual(
                    direct_response.finish_reason,
                    proxy_response.finish_reason,
                )
                self.assertEqual(direct_response.usage, proxy_response.usage)
            finally:
                server.close()
                await server.wait_closed()
                await cleanup_manager_endpoint(root)
                database.close()

        with patch.dict(os.environ, {"PAL_MINION_SANDBOXED": "0"}, clear=False):
            asyncio.run(scenario())

    def test_raw_proxy_stream_and_usage_receipt_are_end_to_end(self) -> None:
        async def scenario() -> None:
            root = Path(tempfile.mkdtemp(prefix="pal-minion-raw-transport-"))
            database, manager = _manager(root)
            endpoint = _register_endpoint()
            manager.runs["run-1"] = MinionRunState(
                minion_id="minion-1",
                run_id="run-1",
                pack=MinionInvocationPack(
                    invocation_id="minion-1",
                    metadata={"preferred_endpoint_id": endpoint.endpoint_id},
                ),
            )
            capture = _CapturingTransport()
            manager._llm_json_transport = capture  # type: ignore[assignment]
            server, _ = await start_manager_server(root, manager._handle_client)
            try:
                proxy = ManagerProxyTransport(root, "run-1", request_timeout_seconds=2)
                request = EncodedTransportRequest(
                    request_id="request-1",
                    wire_shape=WireShape.OPENAI_COMPLETION,
                    timeout_seconds=30,
                    payload={"model": endpoint.model_id, "max_tokens": 64},
                    stream=True,
                )
                frames = await asyncio.to_thread(
                    lambda: list(proxy.frames(endpoint, request))
                )
                self.assertEqual([frame.sequence for frame in frames], [0, 1])
                self.assertEqual(len(capture.requests), 1)

                await asyncio.to_thread(
                    proxy.report_usage,
                    endpoint,
                    request_id="request-1",
                    usage=LLMUsageIR(
                        input_tokens=7,
                        output_tokens=2,
                        reported=True,
                    ),
                    provider_response_count=1,
                )
                record = manager._llm_transport_requests["request-1"]
                self.assertTrue(record.transport_terminal)
                self.assertTrue(record.usage_received)
                duplicate = await manager.llm_usage_receipt(
                    {
                        "run_id": "run-1",
                        "request_id": "request-1",
                        "endpoint_id": endpoint.endpoint_id,
                        "model_id": endpoint.model_id,
                        "provider": endpoint.provider,
                        "provider_response_count": 1,
                        "usage": dict(
                            LLMUsageIR(
                                input_tokens=7,
                                output_tokens=2,
                                reported=True,
                            ).__dict__
                        ),
                    }
                )
                self.assertTrue(duplicate["duplicate"])
                self.assertEqual(
                    manager._llm_usage_ledger.snapshot()["successful_request_count"],
                    1,
                )
                with self.assertRaisesRegex(ValueError, "changed its payload"):
                    await manager.llm_usage_receipt(
                        {
                            "run_id": "run-1",
                            "request_id": "request-1",
                            "endpoint_id": endpoint.endpoint_id,
                            "model_id": endpoint.model_id,
                            "provider": endpoint.provider,
                            "provider_response_count": 1,
                            "usage": dict(
                                LLMUsageIR(
                                    input_tokens=7,
                                    output_tokens=3,
                                    reported=True,
                                ).__dict__
                            ),
                        }
                    )
            finally:
                server.close()
                await server.wait_closed()
                await cleanup_manager_endpoint(root)
                database.close()

        with patch.dict(os.environ, {"PAL_MINION_SANDBOXED": "0"}, clear=False):
            asyncio.run(scenario())

    def test_stale_endpoint_is_rejected_before_provider_invocation(self) -> None:
        async def scenario() -> None:
            root = Path(tempfile.mkdtemp(prefix="pal-minion-stale-endpoint-"))
            database, manager = _manager(root)
            endpoint = _register_endpoint()
            manager.runs["run-1"] = MinionRunState(
                minion_id="minion-1",
                run_id="run-1",
                pack=MinionInvocationPack(
                    invocation_id="minion-1",
                    metadata={"preferred_endpoint_id": endpoint.endpoint_id},
                ),
            )
            capture = _CapturingTransport()
            manager._llm_json_transport = capture  # type: ignore[assignment]
            server, _ = await start_manager_server(root, manager._handle_client)
            try:
                old_endpoint = LLMEndpointModel(**dict(endpoint.__data__))
                LLMEndpointRepository().upsert(
                    **{
                        **{
                            key: value
                            for key, value in old_endpoint.__data__.items()
                            if key not in {"created_at", "updated_at"}
                        },
                        "model_id": "changed-model",
                    }
                )
                proxy = ManagerProxyTransport(root, "run-1", request_timeout_seconds=2)
                with self.assertRaises(LLMEndpointSpecStaleError):
                    await asyncio.to_thread(
                        lambda: list(
                            proxy.frames(
                                old_endpoint,
                                EncodedTransportRequest(
                                    request_id="stale-request",
                                    wire_shape=WireShape.OPENAI_COMPLETION,
                                    timeout_seconds=30,
                                    payload={"model": "proxy-model", "max_tokens": 64},
                                    stream=True,
                                ),
                            )
                        )
                    )
                self.assertEqual(capture.requests, [])
            finally:
                server.close()
                await server.wait_closed()
                await cleanup_manager_endpoint(root)
                database.close()

        with patch.dict(os.environ, {"PAL_MINION_SANDBOXED": "0"}, clear=False):
            asyncio.run(scenario())

    def test_duplicate_transport_request_id_never_reinvokes_provider(self) -> None:
        async def scenario() -> None:
            root = Path(tempfile.mkdtemp(prefix="pal-minion-request-replay-"))
            database, manager = _manager(root)
            endpoint = _register_endpoint()
            manager.runs["run-1"] = MinionRunState(
                minion_id="minion-1",
                run_id="run-1",
                pack=MinionInvocationPack(invocation_id="minion-1"),
            )
            capture = _CapturingTransport()
            manager._llm_json_transport = capture  # type: ignore[assignment]

            first = [
                item
                async for item in manager.llm_transport_stream_frames(
                    _params(endpoint, request_id="same-id")
                )
            ]
            self.assertTrue(first)
            with self.assertRaisesRegex(RuntimeError, "already used"):
                _ = [
                    item
                    async for item in manager.llm_transport_stream_frames(
                        _params(endpoint, request_id="same-id")
                    )
                ]
            self.assertEqual(len(capture.requests), 1)
            database.close()

        asyncio.run(scenario())

    def test_manager_rejects_encoded_authority_bypasses_before_provider(self) -> None:
        async def scenario() -> None:
            root = Path(tempfile.mkdtemp(prefix="pal-minion-transport-authority-"))
            database, manager = _manager(root)
            endpoint = _register_endpoint()
            state = MinionRunState(
                minion_id="minion-1",
                run_id="run-1",
                pack=MinionInvocationPack(invocation_id="minion-1"),
            )
            manager.runs["run-1"] = state
            capture = _CapturingTransport()
            manager._llm_json_transport = capture  # type: ignore[assignment]

            changed_model = _params(endpoint, request_id="changed-model")
            changed_model["payload"] = {
                "model": "different-model",
                "max_tokens": 64,
            }
            excessive_output = _params(endpoint, request_id="excessive-output")
            excessive_output["payload"] = {
                "model": endpoint.model_id,
                "max_tokens": 513,
            }
            extra_body_override = _params(endpoint, request_id="extra-override")
            extra_body_override["extra_body"] = {"model": "different-model"}
            nested_extra_body = _params(endpoint, request_id="nested-extra")
            nested_extra_body["payload"] = {
                "model": endpoint.model_id,
                "max_tokens": 64,
                "extra_body": {"max_tokens": 999},
            }

            for params in (
                changed_model,
                excessive_output,
                extra_body_override,
                nested_extra_body,
            ):
                with self.assertRaises(RuntimeError):
                    _ = [
                        item
                        async for item in manager.llm_transport_stream_frames(params)
                    ]

            state.status = "completed"
            with self.assertRaisesRegex(RuntimeError, "terminal"):
                _ = [
                    item
                    async for item in manager.llm_transport_stream_frames(
                        _params(endpoint, request_id="terminal-run")
                    )
                ]
            self.assertEqual(capture.requests, [])
            database.close()

        asyncio.run(scenario())

    def test_consumer_cancel_closes_proxy_and_manager_provider_lease(self) -> None:
        async def scenario() -> None:
            root = Path(tempfile.mkdtemp(prefix="pal-minion-proxy-cancel-"))
            database, manager = _manager(root)
            endpoint = _register_endpoint()
            manager.runs["run-1"] = MinionRunState(
                minion_id="minion-1",
                run_id="run-1",
                pack=MinionInvocationPack(invocation_id="minion-1"),
            )
            capture = _CapturingTransport(block=True)
            manager._llm_json_transport = capture  # type: ignore[assignment]
            server, _ = await start_manager_server(root, manager._handle_client)
            try:
                proxy = ManagerProxyTransport(root, "run-1", request_timeout_seconds=2)
                control = StreamControl()
                iterator = proxy.frames(
                    endpoint,
                    EncodedTransportRequest(
                        request_id="cancel-request",
                        wire_shape=WireShape.OPENAI_COMPLETION,
                        timeout_seconds=30,
                        payload={"model": endpoint.model_id, "max_tokens": 64},
                        stream=True,
                        stream_control=control,
                    ),
                )
                pending = asyncio.create_task(asyncio.to_thread(next, iterator))
                self.assertTrue(
                    await asyncio.to_thread(capture.started.wait, 1.0)
                )
                for _ in range(100):
                    if control.provider_started:
                        break
                    await asyncio.sleep(0.01)
                self.assertTrue(control.provider_started)
                control.cancel("test_cancel")
                with self.assertRaises(Exception):
                    await asyncio.wait_for(pending, timeout=2)
                self.assertTrue(
                    await asyncio.to_thread(capture.cancelled.wait, 1.0)
                )
            finally:
                server.close()
                await server.wait_closed()
                await cleanup_manager_endpoint(root)
                database.close()

        with patch.dict(os.environ, {"PAL_MINION_SANDBOXED": "0"}, clear=False):
            asyncio.run(scenario())

    def test_pre_cancelled_proxy_request_never_reaches_manager(self) -> None:
        async def scenario() -> None:
            root = Path(tempfile.mkdtemp(prefix="pal-minion-pre-cancel-"))
            database, manager = _manager(root)
            endpoint = _register_endpoint()
            manager.runs["run-1"] = MinionRunState(
                minion_id="minion-1",
                run_id="run-1",
                pack=MinionInvocationPack(invocation_id="minion-1"),
            )
            capture = _CapturingTransport()
            manager._llm_json_transport = capture  # type: ignore[assignment]
            server, _ = await start_manager_server(root, manager._handle_client)
            try:
                proxy = ManagerProxyTransport(root, "run-1", request_timeout_seconds=2)
                control = StreamControl()
                control.cancel("test_pre_cancel")
                request = EncodedTransportRequest(
                    request_id="pre-cancelled-request",
                    wire_shape=WireShape.OPENAI_COMPLETION,
                    timeout_seconds=30,
                    payload={"model": endpoint.model_id, "max_tokens": 64},
                    stream=True,
                    stream_control=control,
                )
                with self.assertRaises(LLMStreamCancelledError):
                    await asyncio.to_thread(
                        lambda: list(proxy.frames(endpoint, request))
                    )
                self.assertEqual(capture.requests, [])
                self.assertNotIn(
                    "pre-cancelled-request",
                    manager._llm_transport_requests,
                )
            finally:
                server.close()
                await server.wait_closed()
                await cleanup_manager_endpoint(root)
                database.close()

        with patch.dict(os.environ, {"PAL_MINION_SANDBOXED": "0"}, clear=False):
            asyncio.run(scenario())

    def test_failure_after_remote_provider_start_is_never_retryable_without_control(self) -> None:
        async def scenario() -> None:
            root = Path(tempfile.mkdtemp(prefix="pal-minion-provider-start-"))
            database, manager = _manager(root)
            endpoint = _register_endpoint()
            manager.runs["run-1"] = MinionRunState(
                minion_id="minion-1",
                run_id="run-1",
                pack=MinionInvocationPack(invocation_id="minion-1"),
            )
            manager._llm_json_transport = _FailingAfterStartTransport()  # type: ignore[assignment]
            server, _ = await start_manager_server(root, manager._handle_client)
            try:
                proxy = ManagerProxyTransport(root, "run-1", request_timeout_seconds=2)
                request = EncodedTransportRequest(
                    request_id="provider-started-request",
                    wire_shape=WireShape.OPENAI_COMPLETION,
                    timeout_seconds=30,
                    payload={"model": endpoint.model_id, "max_tokens": 64},
                    stream=False,
                )
                with self.assertRaises(LLMProviderStartedError):
                    await asyncio.to_thread(
                        lambda: list(proxy.frames(endpoint, request))
                    )
            finally:
                server.close()
                await server.wait_closed()
                await cleanup_manager_endpoint(root)
                database.close()

        with patch.dict(os.environ, {"PAL_MINION_SANDBOXED": "0"}, clear=False):
            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
