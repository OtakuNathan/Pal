from __future__ import annotations

import tempfile
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from pal.core.runtime_config import RuntimeConfig
from pal.llm.contracts import generation_result_from_values, request_ir_from_prompt
from pal.llm.credentials import LLMCredentialResolver
from pal.llm.endpoint_spec import LLMEndpointSpec, LLMEndpointSpecError
from pal.llm.ir import LLMFinishReason, WireShape
from pal.llm.shapes.base import _JSONFrame as JSONFrame
from pal.llm.models import LLMEndpointModel
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.llm.secret_store import InMemorySecretStore
from pal.llm.transport import (
    DefaultSDKClientFactory,
    LLMStreamControl,
    SDKJSONTransport,
    SDKTransportRequest,
)


def _endpoint(endpoint_id: str, *, model_id: str | None = None) -> LLMEndpointModel:
    return LLMEndpointModel(
        endpoint_id=endpoint_id,
        provider="test",
        model_id=model_id or f"{endpoint_id}-model",
        display_name=endpoint_id,
        wire_shape="openai_completion",
        base_url="https://example.test/v1",
        auth_kind="api_key_ref",
        credential_ref=f"{endpoint_id.upper()}_API_KEY",
        context_window=32_000,
        max_output_tokens=4_096,
        thinking_levels_blob=["off", "high"],
        default_thinking_level="off",
        supports_tools=True,
        supports_streaming=True,
        supports_vision=False,
        input_modalities_blob=["text"],
        output_modalities_blob=["text"],
        priority=0,
        enabled=True,
        capabilities_blob={},
    )


class _Settings:
    def __init__(self) -> None:
        self.active: str | None = None
        self.thinking: dict[str, str] = {}

    def get_active_llm_endpoint_id(self) -> str | None:
        return self.active

    def set_active_llm_endpoint_id(self, endpoint_id: str) -> None:
        self.active = endpoint_id

    def get_think_level(self, endpoint_id: str) -> str | None:
        return self.thinking.get(endpoint_id)

    def set_think_level(self, endpoint_id: str, level: str) -> None:
        self.thinking[endpoint_id] = level


class _Completions:
    def __init__(self, client: "_FakeClient") -> None:
        self.client = client

    def create(self, **payload):
        self.client.payloads.append(dict(payload))
        return self.client.response


class _Chat:
    def __init__(self, client: "_FakeClient") -> None:
        self.completions = _Completions(client)


class _FakeClient:
    def __init__(self, response) -> None:
        self.response = response
        self.payloads: list[dict] = []
        self.close_count = 0
        self.close_thread_ids: list[int] = []
        self.chat = _Chat(self)

    def close(self) -> None:
        self.close_count += 1
        self.close_thread_ids.append(threading.get_ident())


class _Factory:
    def __init__(self, responses: list[object]) -> None:
        self.responses = list(responses)
        self.clients: list[_FakeClient] = []

    def openai(self, *, api_key: str, base_url: str, timeout: float):
        _ = api_key, base_url, timeout
        client = _FakeClient(self.responses.pop(0))
        self.clients.append(client)
        return client

    def anthropic(self, *, api_key: str, base_url: str, timeout: float):
        raise AssertionError((api_key, base_url, timeout))


def _transport_request(
    endpoint_id: str = "one",
    *,
    api_key: str = "key",
    stream: bool = False,
    stream_control: LLMStreamControl | None = None,
) -> SDKTransportRequest:
    return SDKTransportRequest(
        request_id="test-request",
        endpoint_id=endpoint_id,
        wire_shape=WireShape.OPENAI_COMPLETION,
        api_key=api_key,
        base_url="https://example.test/v1",
        timeout_seconds=30,
        payload={"model": "demo", "messages": []},
        stream=stream,
        stream_control=stream_control,
    )


class LLMTransportLifecycleTests(unittest.TestCase):
    def test_default_sdk_clients_disable_nested_retries(self) -> None:
        factory = DefaultSDKClientFactory()
        with patch("openai.OpenAI") as openai_client:
            factory.openai(
                api_key="key",
                base_url="https://example.test/v1",
                timeout=600,
            )
        openai_client.assert_called_once_with(
            api_key="key",
            base_url="https://example.test/v1",
            timeout=600,
            max_retries=0,
        )

        with patch("anthropic.Anthropic") as anthropic_client:
            factory.anthropic(
                api_key="key",
                base_url="https://example.test/v1",
                timeout=600,
            )
        anthropic_client.assert_called_once_with(
            api_key="key",
            base_url="https://example.test/v1",
            timeout=600,
            max_retries=0,
        )

    def test_sdk_client_is_reused_until_endpoint_switch(self) -> None:
        factory = _Factory([
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]},
        ])
        transport = SDKJSONTransport(client_factory=factory)

        self.assertEqual(len(tuple(transport.frames(_transport_request()))), 1)
        self.assertEqual(len(tuple(transport.frames(_transport_request()))), 1)
        self.assertEqual(len(factory.clients), 1)
        self.assertEqual(len(factory.clients[0].payloads), 2)

        transport.activate_endpoint("two")
        self.assertEqual(factory.clients[0].close_count, 1)

    def test_endpoint_switch_retires_an_in_use_client_after_its_lease(self) -> None:
        factory = _Factory([[{"chunk": 1}, {"chunk": 2}]])
        transport = SDKJSONTransport(client_factory=factory)
        iterator = transport.frames(_transport_request())

        self.assertEqual(next(iterator), JSONFrame(0, {"chunk": 1}))
        transport.activate_endpoint("two")
        self.assertEqual(factory.clients[0].close_count, 0)
        self.assertEqual(list(iterator), [JSONFrame(1, {"chunk": 2})])
        self.assertEqual(factory.clients[0].close_count, 1)

    def test_retired_in_flight_entry_is_closed_even_if_same_key_reopens(self) -> None:
        factory = _Factory(
            [
                [{"chunk": "old-1"}, {"chunk": "old-2"}],
                [{"chunk": "new"}],
            ]
        )
        transport = SDKJSONTransport(client_factory=factory)
        old_iterator = transport.frames(_transport_request())
        self.assertEqual(next(old_iterator), JSONFrame(0, {"chunk": "old-1"}))
        transport.activate_endpoint("two")

        self.assertEqual(
            list(transport.frames(_transport_request())),
            [JSONFrame(0, {"chunk": "new"})],
        )
        self.assertEqual(len(factory.clients), 2)
        self.assertEqual(factory.clients[0].close_count, 0)

        self.assertEqual(list(old_iterator), [JSONFrame(1, {"chunk": "old-2"})])
        self.assertEqual(factory.clients[0].close_count, 1)

    def test_key_change_never_reuses_a_client_created_with_another_key(self) -> None:
        response = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        factory = _Factory([response, response])
        transport = SDKJSONTransport(client_factory=factory)

        tuple(transport.frames(_transport_request(api_key="key-one")))
        tuple(transport.frames(_transport_request(api_key="key-two")))

        self.assertEqual(len(factory.clients), 2)

    def test_concurrent_requests_never_share_a_cancellable_client_lease(self) -> None:
        factory = _Factory(
            [
                [{"chunk": "first"}, {"chunk": "last"}],
                [{"chunk": "other"}],
            ]
        )
        transport = SDKJSONTransport(client_factory=factory)
        first = transport.frames(_transport_request(stream=True))

        self.assertEqual(next(first), JSONFrame(0, {"chunk": "first"}))
        self.assertEqual(
            list(transport.frames(_transport_request(stream=True))),
            [JSONFrame(0, {"chunk": "other"})],
        )
        self.assertEqual(len(factory.clients), 2)
        first.close()

    def test_response_close_failure_quarantines_client_and_removes_it_from_pool(self) -> None:
        class _FailingCloseStream:
            def __init__(self) -> None:
                self.close_count = 0

            def __iter__(self):
                yield {"chunk": "done"}

            def close(self) -> None:
                self.close_count += 1
                raise OSError("response close failed")

        response = _FailingCloseStream()
        factory = _Factory([response])
        transport = SDKJSONTransport(client_factory=factory)
        frames = transport.frames(_transport_request(stream=True))
        self.assertEqual(next(frames), JSONFrame(0, {"chunk": "done"}))
        entry = next(iter(next(iter(transport._clients.values()))))
        self.assertEqual(list(frames), [])
        self.assertEqual(transport._clients, {})
        self.assertEqual(factory.clients[0].close_count, 1)
        self.assertTrue(entry.owner.quarantined)
        self.assertIs(entry.owner._resource.response, response)
        self.assertIsNotNone(entry.owner._resource.frames)
        self.assertEqual(response.close_count, 1)

    def test_client_close_failure_quarantines_root_instead_of_reusing_it(self) -> None:
        response = {"choices": [{"message": {"content": "ok"}}]}
        factory = _Factory([response, response])
        transport = SDKJSONTransport(client_factory=factory)

        list(transport.frames(_transport_request()))

        def fail_close() -> None:
            raise OSError("client close failed")

        factory.clients[0].close = fail_close  # type: ignore[method-assign]
        transport.activate_endpoint("two")
        self.assertEqual(transport._clients, {})

        list(transport.frames(_transport_request()))
        self.assertEqual(len(factory.clients), 2)

    def test_stream_control_leaves_response_and_client_close_to_worker(self) -> None:
        class _BlockingResponse:
            def __init__(self, control: LLMStreamControl) -> None:
                self.control = control
                self.iterating = threading.Event()
                self.close_count = 0
                self.close_thread_ids: list[int] = []

            def __iter__(self):
                self.iterating.set()
                while not self.control.cancelled:
                    time.sleep(0.005)
                if False:
                    yield {}

            def close(self) -> None:
                self.close_count += 1
                self.close_thread_ids.append(threading.get_ident())

        control = LLMStreamControl()
        response = _BlockingResponse(control)
        factory = _Factory([response])
        transport = SDKJSONTransport(client_factory=factory)
        errors: list[BaseException] = []
        worker_thread_ids: list[int] = []

        def consume() -> None:
            worker_thread_ids.append(threading.get_ident())
            try:
                list(
                    transport.frames(
                        _transport_request(stream=True, stream_control=control)
                    )
                )
            except BaseException as exc:  # noqa: BLE001 - test captures worker failure
                errors.append(exc)

        thread = threading.Thread(target=consume)
        thread.start()
        self.assertTrue(response.iterating.wait(timeout=1.0))
        self.assertTrue(control.provider_started)

        control.cancel("interrupted")
        self.assertEqual(response.close_count, 0)
        self.assertEqual(factory.clients[0].close_count, 0)
        thread.join(timeout=1.0)

        self.assertFalse(thread.is_alive())
        self.assertGreaterEqual(response.close_count, 1)
        self.assertGreaterEqual(factory.clients[0].close_count, 1)
        self.assertEqual(response.close_thread_ids, worker_thread_ids)
        self.assertEqual(factory.clients[0].close_thread_ids, worker_thread_ids)
        self.assertTrue(errors)

    def test_stream_control_observes_raw_keepalive_bytes_hidden_by_the_sdk(self) -> None:
        class _RawStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b": OPENROUTER PROCESSING\n\n"

        class _SDKStream:
            def __init__(self) -> None:
                self.response = httpx.Response(200, stream=_RawStream())

            def __iter__(self):
                _ = b"".join(self.response.iter_bytes())
                yield {"chunk": "done"}

            def close(self) -> None:
                self.response.close()

        stream = _SDKStream()
        control = LLMStreamControl()
        before = control.last_network_activity_at
        time.sleep(0.005)
        transport = SDKJSONTransport(client_factory=_Factory([stream]))

        self.assertEqual(
            list(
                transport.frames(
                    _transport_request(stream=True, stream_control=control)
                )
            ),
            [JSONFrame(0, {"chunk": "done"})],
        )

        self.assertGreater(control.last_network_activity_at, before)


class LLMEndpointSpecTests(unittest.TestCase):
    def test_leaf_ir_import_does_not_initialize_execution_or_runtime(self) -> None:
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import pal.llm.ir; "
                    "assert 'pal.execution' not in sys.modules; "
                    "assert 'pal.llm.runtime' not in sys.modules"
                ),
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_endpoint_row_is_the_only_validated_thinking_enum_source(self) -> None:
        endpoint = _endpoint("valid")
        spec = LLMEndpointSpec.from_value(endpoint)
        self.assertEqual(spec.thinking_levels_blob, ("off", "high"))
        self.assertEqual(spec.default_thinking_level, "off")

        endpoint.default_thinking_level = "medium"
        with self.assertRaisesRegex(LLMEndpointSpecError, "is not declared"):
            LLMEndpointSpec.from_value(endpoint)

    def test_credential_resolution_never_borrows_a_generic_provider_key(self) -> None:
        endpoint = _endpoint("explicit")
        endpoint.credential_ref = "EXPLICIT_ENDPOINT_KEY"
        resolver = LLMCredentialResolver(secret_store=InMemorySecretStore())

        with patch.dict("os.environ", {"OPENAI_API_KEY": "wrong-node-key"}, clear=True):
            self.assertIsNone(resolver.resolve_api_key(endpoint))

    def test_malformed_endpoint_json_is_not_silently_repaired(self) -> None:
        endpoint = _endpoint("invalid-json")
        endpoint.capabilities_blob = "{broken"

        with self.assertRaisesRegex(LLMEndpointSpecError, "capabilities_blob contains invalid JSON"):
            LLMEndpointSpec.from_value(endpoint)


class LLMErrorSemanticsTests(unittest.TestCase):
    def _runtime(self, invoker, endpoints, *, attempts: int = 2) -> LLMRuntime:
        return LLMRuntime(
            endpoint_resolver=EndpointResolver(endpoints=tuple(endpoints)),
            settings_repository=_Settings(),  # type: ignore[arg-type]
            endpoint_invoker=invoker,
            endpoint_retry_attempts=attempts,
            config=RuntimeConfig(
                runtime_root=Path(tempfile.mkdtemp()),
                llm_endpoint_retry_attempts=attempts,
            ),
        )

    def test_finish_reason_error_retries_and_never_counts_as_success(self) -> None:
        class _Invoker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def invoke(self, endpoint, request, **kwargs):
                _ = request, kwargs
                self.calls.append(endpoint.endpoint_id)
                if endpoint.endpoint_id == "bad":
                    return generation_result_from_values(
                        text="provider error",
                        finish_reason="error",
                    ).response, ()
                return generation_result_from_values(text="ok").response, ()

        invoker = _Invoker()
        runtime = self._runtime(invoker, [_endpoint("bad"), _endpoint("good")])

        result = runtime.generate(
            request_ir_from_prompt(messages=[{"role": "user", "content": "work"}], max_output_tokens=100)
        )

        self.assertEqual(result.text, "ok")
        self.assertEqual(invoker.calls, ["bad", "bad", "good"])
        usage = runtime.usage_snapshot()
        self.assertEqual(usage["successful_request_count"], 1)
        self.assertEqual(usage["failed_attempt_count"], 2)

    def test_output_recovery_error_falls_back_without_false_success(self) -> None:
        class _Invoker:
            def __init__(self) -> None:
                self.bad_calls = 0
                self.calls: list[str] = []

            def invoke(self, endpoint, request, **kwargs):
                _ = request, kwargs
                self.calls.append(endpoint.endpoint_id)
                if endpoint.endpoint_id == "bad":
                    self.bad_calls += 1
                    reason = "length" if self.bad_calls == 1 else "error"
                    return generation_result_from_values(text=reason, finish_reason=reason).response, ()
                return generation_result_from_values(text="recovered elsewhere").response, ()

        bad = _endpoint("bad")
        bad.capabilities_blob = {
            "max_output_recovery": {
                "enabled": True,
                "upper_limit": 8_192,
                "max_continuations": 1,
            }
        }
        invoker = _Invoker()
        runtime = self._runtime(invoker, [bad, _endpoint("good")], attempts=1)

        result = runtime.generate(
            request_ir_from_prompt(
                messages=[{"role": "user", "content": "work"}],
                max_output_tokens=100,
                metadata={"max_output_recovery_enabled": True},
            )
        )

        self.assertEqual(result.text, "recovered elsewhere")
        self.assertEqual(invoker.calls, ["bad", "bad", "good"])
        usage = runtime.usage_snapshot()
        self.assertEqual(usage["successful_request_count"], 1)
        self.assertEqual(usage["failed_attempt_count"], 1)

    def test_invalid_key_skips_same_endpoint_retry_and_falls_back(self) -> None:
        class _Invoker:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def invoke(self, endpoint, request, **kwargs):
                _ = request, kwargs
                self.calls.append(endpoint.endpoint_id)
                if endpoint.endpoint_id == "bad-key":
                    raise RuntimeError("Error code: 401 - invalid_api_key")
                return generation_result_from_values(text="ok").response, ()

        invoker = _Invoker()
        runtime = self._runtime(invoker, [_endpoint("bad-key"), _endpoint("good")], attempts=3)

        result = runtime.generate(
            request_ir_from_prompt(messages=[{"role": "user", "content": "work"}], max_output_tokens=100)
        )

        self.assertEqual(result.text, "ok")
        self.assertEqual(invoker.calls, ["bad-key", "good"])


if __name__ == "__main__":
    unittest.main()
