from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pal.core.runtime_config import RuntimeConfig
from pal.core.turn_executor import TurnExecutor
from pal.llm.contracts import LLMPreflightRequest
from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.ir import (
    GenerationPolicyIR,
    LLMFinishReason,
    LLMMessageIR,
    LLMRequestIR,
    LLMResponseDeltaKind,
    LLMResponseIR,
    LLMResponseUpdate,
    MessageRole,
    TextPartIR,
)
from pal.llm.models import LLMEndpointModel
from pal.llm.response_hooks import ProviderResponseHookError
from pal.llm.repository import RuntimeSettingRepository
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.llm.transport import (
    LLMEndpointSpecStaleError,
    LLMProviderStartedError,
    LLMStreamCancelledError,
    LLMStreamControl,
)


def _endpoint(
    endpoint_id: str = "ep",
    *,
    model_id: str = "test-model",
    provider: str = "test",
) -> LLMEndpointModel:
    return LLMEndpointModel(
        endpoint_id=endpoint_id,
        provider=provider,
        model_id=model_id,
        display_name="Test",
        wire_shape="openai_completion",
        base_url="https://example.test",
        auth_kind="api_key_ref",
        credential_ref="key",
        context_window=10_000,
        max_output_tokens=1_000,
        thinking_levels_blob=["off", "low", "high"],
        default_thinking_level="low",
        supports_tools=True,
        supports_streaming=True,
        supports_vision=False,
        input_modalities_blob=["text"],
        output_modalities_blob=["text"],
        priority=0,
        enabled=True,
        capabilities_blob={},
    )


def _request(text: str = "hello") -> LLMRequestIR:
    return LLMRequestIR(
        messages=(LLMMessageIR(MessageRole.USER, (TextPartIR(text),)),),
        tools=(),
        policy=GenerationPolicyIR(max_output_tokens=100),
    )


class _Settings:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get_active_llm_endpoint_id(self) -> str | None:
        return self.values.get("active")

    def set_active_llm_endpoint_id(self, value: str) -> None:
        self.values["active"] = value

    def get_think_level(self, endpoint_id: str) -> str | None:
        return self.values.get(f"think:{endpoint_id}")

    def set_think_level(self, endpoint_id: str, value: str) -> None:
        self.values[f"think:{endpoint_id}"] = value


class _Invoker:
    def __init__(self) -> None:
        self.requests: list[LLMRequestIR] = []

    def invoke(self, endpoint, request, **kwargs):
        self.requests.append(request)
        response = LLMResponseIR(
            LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR("ok"),)),
            LLMFinishReason.STOP,
        )
        return response, ()

    def invoke_updates(self, endpoint, request, **kwargs):
        self.requests.append(request)
        response = LLMResponseIR(
            LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR("partial"),)),
            LLMFinishReason.STOP,
        )
        yield LLMResponseUpdate(response, LLMResponseDeltaKind.TEXT, text_delta="partial")
        raise RuntimeError("connection dropped")


class _ResponseHookFailingInvoker:
    def __init__(self) -> None:
        self.attempts = 0

    def invoke(self, endpoint, request, **kwargs):
        self.attempts += 1
        raise ProviderResponseHookError("DeepSeek DSML response is malformed")


class _StaleOnceInvoker:
    def __init__(self) -> None:
        self.attempts = 0

    def invoke(self, endpoint, request, **kwargs):
        _ = endpoint, request, kwargs
        self.attempts += 1
        if self.attempts == 1:
            raise LLMEndpointSpecStaleError("stale")
        return (
            LLMResponseIR(
                LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR("fresh"),)),
                LLMFinishReason.STOP,
            ),
            (),
        )

    def invoke_updates(self, endpoint, request, **kwargs):
        _ = endpoint, request, kwargs
        self.attempts += 1
        if self.attempts == 1:
            raise LLMEndpointSpecStaleError("stale")
        response = LLMResponseIR(
            LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR("fresh"),)),
            LLMFinishReason.STOP,
        )
        yield LLMResponseUpdate(response, LLMResponseDeltaKind.STATE)


class _SequenceInvoker:
    def __init__(self, responses: list[LLMResponseIR]) -> None:
        self.responses = list(responses)
        self.attempts = 0

    def invoke(self, endpoint, request, **kwargs):
        response = self.responses[self.attempts]
        self.attempts += 1
        return response, ()


class _StartedFailingShapeInvoker(ShapeEndpointInvoker):
    def __init__(self, *, wait_for_cancel: bool = False) -> None:
        super().__init__(credential_resolver=lambda _endpoint: "key")
        self.attempts = 0
        self.wait_for_cancel = wait_for_cancel
        self.control = None

    def invoke_updates(self, endpoint, request, *, timeout_seconds=180.0, stream_control=None):
        _ = endpoint, request, timeout_seconds
        self.attempts += 1
        self.control = stream_control
        assert stream_control is not None
        stream_control.touch_network()
        if self.wait_for_cancel:
            while not stream_control.cancelled:
                time.sleep(0.005)
            raise LLMStreamCancelledError("LLM stream cancelled: wall_timeout")
        raise RuntimeError("provider disconnected after response headers")
        yield  # pragma: no cover - keeps this an iterator


class LLMRuntimeIRTests(unittest.TestCase):
    def test_local_database_failure_is_not_reported_as_provider_failure(self) -> None:
        class DatabaseFailingInvoker:
            def invoke(self, endpoint, request, **kwargs):
                _ = endpoint, request, kwargs
                raise sqlite3.DatabaseError("file is not a database")

        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint(),)),
            _Settings(),  # type: ignore[arg-type]
            endpoint_invoker=DatabaseFailingInvoker(),
            config=RuntimeConfig(
                runtime_root=Path(tempfile.mkdtemp()),
                llm_endpoint_retry_attempts=3,
            ),
        )

        result = runtime.generate(_request())

        self.assertEqual(result.finish_reason, LLMFinishReason.ERROR)
        self.assertEqual(
            result.response.message.metadata.get("failure_subsystem"),
            "persistence",
        )
        self.assertEqual(
            result.response.message.metadata.get("failure_kind"),
            "local_state",
        )

    def test_endpoint_resolution_database_failure_keeps_local_provenance(self) -> None:
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint(),)),
            _Settings(),  # type: ignore[arg-type]
            endpoint_invoker=_Invoker(),
            config=RuntimeConfig(runtime_root=Path(tempfile.mkdtemp())),
        )

        with patch.object(
            runtime,
            "_enabled_endpoints",
            side_effect=sqlite3.DatabaseError("file is not a database"),
        ):
            generated = runtime.generate(_request())
            streamed = list(runtime._iter_stream_updates(_request()))

        self.assertEqual(
            generated.response.message.metadata.get("failure_subsystem"),
            "persistence",
        )
        self.assertEqual(
            streamed[-1].response.message.metadata.get("failure_subsystem"),
            "persistence",
        )

    def test_stale_endpoint_snapshot_refreshes_once_before_nonstream_invocation(self) -> None:
        invoker = _StaleOnceInvoker()
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint(),)),
            _Settings(),  # type: ignore[arg-type]
            endpoint_invoker=invoker,
            config=RuntimeConfig(runtime_root=Path(tempfile.mkdtemp())),
        )

        result = runtime.generate(_request())

        self.assertEqual(result.text, "fresh")
        self.assertEqual(invoker.attempts, 2)

    def test_stale_endpoint_snapshot_refreshes_once_before_stream_invocation(self) -> None:
        invoker = _StaleOnceInvoker()
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint(),)),
            _Settings(),  # type: ignore[arg-type]
            endpoint_invoker=invoker,
            config=RuntimeConfig(runtime_root=Path(tempfile.mkdtemp())),
        )

        updates = list(runtime._iter_stream_updates(_request()))

        self.assertEqual(updates[-1].response.text, "fresh")
        self.assertEqual(invoker.attempts, 2)

    def test_provider_started_error_is_never_retried_nonstream(self) -> None:
        class Invoker:
            attempts = 0

            def invoke(inner_self, endpoint, request, **kwargs):
                _ = endpoint, request, kwargs
                inner_self.attempts += 1
                raise LLMProviderStartedError("started")

        invoker = Invoker()
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint(),)),
            _Settings(),  # type: ignore[arg-type]
            endpoint_invoker=invoker,
            config=RuntimeConfig(
                runtime_root=Path(tempfile.mkdtemp()),
                llm_endpoint_retry_attempts=3,
            ),
        )

        result = runtime.generate(_request())

        self.assertEqual(result.response.finish_reason, LLMFinishReason.ERROR)
        self.assertEqual(invoker.attempts, 1)

    def test_initial_and_recovery_passes_share_one_response_hook_registry(self) -> None:
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint(provider="deepseek"),)),
            _Settings(),  # type: ignore[arg-type]
            config=RuntimeConfig(runtime_root=Path(tempfile.mkdtemp())),
        )

        invoker = runtime.endpoint_invoker
        self.assertIsInstance(invoker, ShapeEndpointInvoker)
        self.assertIs(runtime.provider_response_hooks, invoker.response_hooks)

    def test_db_thinking_enum_drives_request_without_provider_heuristics(self) -> None:
        invoker = _Invoker()
        settings = _Settings()
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint(),)),
            settings,  # type: ignore[arg-type]
            endpoint_invoker=invoker,
            config=RuntimeConfig(runtime_root=Path(tempfile.mkdtemp())),
        )
        result = runtime.generate(_request())
        self.assertEqual(result.text, "ok")
        self.assertEqual(invoker.requests[0].policy.thinking_level.value, "low")

    def test_stream_failure_after_semantic_delta_does_not_retry_or_fallback(self) -> None:
        invoker = _Invoker()
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint("first"), _endpoint("second"))),
            _Settings(),  # type: ignore[arg-type]
            endpoint_invoker=invoker,
            config=RuntimeConfig(runtime_root=Path(tempfile.mkdtemp()), llm_endpoint_retry_attempts=3),
        )
        updates = list(runtime._iter_stream_updates(_request()))
        self.assertEqual(len(invoker.requests), 1)
        self.assertEqual(updates[0].text_delta, "partial")
        self.assertEqual(updates[-1].response.finish_reason, LLMFinishReason.ERROR)

    def test_stream_failure_after_provider_started_does_not_retry_or_fallback(self) -> None:
        invoker = _StartedFailingShapeInvoker()
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint("first"), _endpoint("second"))),
            _Settings(),  # type: ignore[arg-type]
            endpoint_invoker=invoker,
            config=RuntimeConfig(
                runtime_root=Path(tempfile.mkdtemp()),
                llm_endpoint_retry_attempts=3,
            ),
        )

        updates = list(
            runtime._iter_stream_updates(
                _request(),
                stream_control=LLMStreamControl(),
            )
        )

        self.assertEqual(invoker.attempts, 1)
        self.assertEqual(updates[-1].response.finish_reason, LLMFinishReason.ERROR)
        self.assertIn("kind=unknown", updates[-1].response.text)
        self.assertNotIn("provider disconnected", updates[-1].response.text)

    def test_stream_wall_timeout_cancels_without_retrying(self) -> None:
        invoker = _StartedFailingShapeInvoker(wait_for_cancel=True)
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint("first"), _endpoint("second"))),
            _Settings(),  # type: ignore[arg-type]
            endpoint_invoker=invoker,
            config=RuntimeConfig(
                runtime_root=Path(tempfile.mkdtemp()),
                llm_endpoint_retry_attempts=3,
                llm_stream_wall_timeout_seconds=0.05,
                llm_stream_cleanup_timeout_seconds=0.5,
            ),
        )

        async def collect():
            return [update async for update in runtime.astream(_request())]

        updates = asyncio.run(collect())

        self.assertEqual(invoker.attempts, 1)
        self.assertTrue(invoker.control.cancelled)
        self.assertEqual(updates[-1].response.finish_reason, LLMFinishReason.ERROR)

    def test_stream_consumer_cancellation_closes_worker_without_waiting_for_network_timeout(self) -> None:
        invoker = _StartedFailingShapeInvoker(wait_for_cancel=True)
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint("first"),)),
            _Settings(),  # type: ignore[arg-type]
            endpoint_invoker=invoker,
            config=RuntimeConfig(
                runtime_root=Path(tempfile.mkdtemp()),
                llm_endpoint_retry_attempts=3,
                llm_stream_wall_timeout_seconds=30.0,
                llm_stream_cleanup_timeout_seconds=0.5,
            ),
        )

        async def cancel_pending_stream() -> float:
            iterator = runtime.astream(_request()).__aiter__()
            pending = asyncio.create_task(anext(iterator))
            deadline = asyncio.get_running_loop().time() + 1.0
            while invoker.control is None:
                if asyncio.get_running_loop().time() >= deadline:
                    self.fail("stream worker did not start")
                await asyncio.sleep(0.005)
            started = asyncio.get_running_loop().time()
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            return asyncio.get_running_loop().time() - started

        elapsed = asyncio.run(cancel_pending_stream())

        self.assertTrue(invoker.control.cancelled)
        self.assertLess(elapsed, 0.75)

    def test_resident_stream_projects_waiting_status_without_adding_llm_output(self) -> None:
        response = LLMResponseIR(
            LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR("done"),)),
            LLMFinishReason.STOP,
        )

        class _SlowRuntime:
            last_endpoint_id = "ep"
            last_model_id = "test-model"

            async def astream(self, request):
                _ = request
                await asyncio.sleep(0.04)
                yield LLMResponseUpdate(
                    response,
                    LLMResponseDeltaKind.TEXT,
                    text_delta="done",
                )

        class _Output:
            def __init__(self) -> None:
                self.statuses: list[tuple[str, dict]] = []

            def queue_status(self, binding, kind, *, payload=None):
                _ = binding
                self.statuses.append((kind, dict(payload or {})))
                return "status"

        output = _Output()
        executor = object.__new__(TurnExecutor)
        executor._config = RuntimeConfig(
            llm_wait_status_seconds=(0.01, 0.02),
        )
        executor.context = SimpleNamespace(
            port_registry={"agent_io:output": output},
        )

        async def record_update(_continuation, _update):
            return None

        executor._handle_ir_stream_update = record_update
        continuation = SimpleNamespace(
            delivery_binding=object(),
            interrupted=False,
        )

        outcome = asyncio.run(
            executor.stream_llm_request_async(
                continuation,
                _SlowRuntime(),
                _request(),
            )
        )

        self.assertEqual(outcome.text, "done")
        self.assertEqual([kind for kind, _ in output.statuses], ["llm_waiting", "llm_waiting"])
        self.assertIn("elapsed", output.statuses[0][1]["text"])

    def test_preflight_operates_on_the_same_ir_request(self) -> None:
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint(),)),
            _Settings(),  # type: ignore[arg-type]
            endpoint_invoker=_Invoker(),
            config=RuntimeConfig(runtime_root=Path(tempfile.mkdtemp())),
        )
        advice = runtime.preflight(LLMPreflightRequest(request=_request("x" * 50_000)))
        self.assertEqual(str(advice.status), "compact_required")

    def test_preflight_measures_the_fully_hooked_request(self) -> None:
        runtime_root = Path(tempfile.mkdtemp())
        hook_root = runtime_root / "llm" / "models"
        hook_root.mkdir(parents=True)
        (hook_root / "test_model.py").write_text(
            """
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
MODEL_ID = "test-model"
def adjust_messages(messages):
    return (*messages, LLMMessageIR(MessageRole.DEVELOPER, (TextPartIR("x" * 40000),)))
""".strip(),
            encoding="utf-8",
        )
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint(),)),
            _Settings(),  # type: ignore[arg-type]
            endpoint_invoker=_Invoker(),
            config=RuntimeConfig(runtime_root=runtime_root),
        )

        advice = runtime.preflight(LLMPreflightRequest(request=_request("small")))

        self.assertEqual(str(advice.status), "compact_required")
        self.assertGreater(advice.breakdown["estimated_input_tokens"], 9_000)

    def test_provider_response_hook_error_is_bounded_and_not_a_success(self) -> None:
        invoker = _ResponseHookFailingInvoker()
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(_endpoint(),)),
            _Settings(),  # type: ignore[arg-type]
            endpoint_invoker=invoker,  # type: ignore[arg-type]
            config=RuntimeConfig(
                runtime_root=Path(tempfile.mkdtemp()),
                llm_endpoint_retry_attempts=2,
            ),
        )

        result = runtime.generate(_request())

        self.assertEqual(invoker.attempts, 2)
        self.assertEqual(result.finish_reason, LLMFinishReason.ERROR)
        self.assertIn("kind=response_error", result.text)
        self.assertNotIn("DSML response is malformed", result.text)

    def test_output_recovery_is_normalized_after_dsml_fragments_are_merged(self) -> None:
        token = "｜DSML｜"
        first_text = (
            f"<{token}tool_calls>\n"
            f'<{token}invoke name="search_tools">\n'
            f'<{token}parameter name="query" string="true">deepseek'
        )
        second_text = (
            f" parser</{token}parameter>\n"
            f"</{token}invoke>\n"
            f"</{token}tool_calls>"
        )
        responses = [
            LLMResponseIR(
                LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR(first_text),)),
                LLMFinishReason.LENGTH,
            ),
            LLMResponseIR(
                LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR(second_text),)),
                LLMFinishReason.STOP,
            ),
        ]
        invoker = _SequenceInvoker(responses)
        endpoint = _endpoint(provider="deepseek")
        endpoint.capabilities_blob = {
            "max_output_recovery": {
                "enabled": True,
                "upper_limit": 1_000,
                "max_continuations": 1,
            }
        }
        runtime = LLMRuntime(
            EndpointResolver(endpoints=(endpoint,)),
            _Settings(),  # type: ignore[arg-type]
            endpoint_invoker=invoker,  # type: ignore[arg-type]
            config=RuntimeConfig(
                runtime_root=Path(tempfile.mkdtemp()),
                llm_endpoint_retry_attempts=1,
                llm_max_output_recovery_attempts=1,
            ),
        )
        request = LLMRequestIR(
            messages=_request().messages,
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=1_000),
        )

        result = runtime.generate(request)

        self.assertEqual(invoker.attempts, 2)
        self.assertEqual(result.finish_reason, LLMFinishReason.TOOL_CALLS)
        self.assertEqual(result.text, "")
        self.assertEqual(result.tool_calls[0].name, "search_tools")
        self.assertEqual(result.tool_calls[0].args, {"query": "deepseek parser"})


if __name__ == "__main__":
    unittest.main()
