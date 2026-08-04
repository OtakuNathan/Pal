from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pal.core.runtime_config import RuntimeConfig
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


class _SequenceInvoker:
    def __init__(self, responses: list[LLMResponseIR]) -> None:
        self.responses = list(responses)
        self.attempts = 0

    def invoke(self, endpoint, request, **kwargs):
        response = self.responses[self.attempts]
        self.attempts += 1
        return response, ()


class LLMRuntimeIRTests(unittest.TestCase):
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
