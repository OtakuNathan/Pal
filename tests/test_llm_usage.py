from __future__ import annotations

import asyncio
from dataclasses import dataclass
from types import SimpleNamespace
import unittest

from pal.control import ControlEvent, ControlPlane
from pal.core import PalCore
from pal.llm.capabilities import (
    LLMIntrospectionProvider,
    register_with_core as register_llm_with_core,
)
from pal.llm.contracts import CanonicalLLMOutcome, CanonicalLLMRequest
from pal.llm.runtime import (
    EndpointResolver,
    LLMRuntime,
    _include_openai_stream_usage,
    _parse_openai_chat_stream_chunk,
    _response_usage,
)
from pal.shared import EventKind, IntrospectionCall, SourceKind


@dataclass
class _Settings:
    active_endpoint_id: str | None = "primary"
    think_level: str = "balanced"

    def get_think_level(self) -> str:
        return self.think_level

    def get_active_llm_endpoint_id(self) -> str | None:
        return self.active_endpoint_id

    def set_active_llm_endpoint_id(self, endpoint_id: str) -> None:
        self.active_endpoint_id = str(endpoint_id)


class _UsageInvoker:
    def __init__(self, outcome: CanonicalLLMOutcome) -> None:
        self.outcome = outcome

    def invoke(self, endpoint, request):
        _ = endpoint, request
        return self.outcome

    def invoke_stream(self, endpoint, request):
        raise AssertionError("streaming was not expected")


class _FailingInvoker:
    def invoke(self, endpoint, request):
        _ = endpoint, request
        raise RuntimeError("provider failed")

    def invoke_stream(self, endpoint, request):
        raise AssertionError("streaming was not expected")


def _endpoint(endpoint_id: str = "primary"):
    return SimpleNamespace(
        endpoint_id=endpoint_id,
        provider="openai",
        model_id="test-model",
        display_name="Test model",
        api_mode="openai_chat",
        base_url="https://example.test/v1",
        context_window=32_000,
        max_output_tokens=4_096,
        supports_reasoning=True,
        supports_tools=True,
        supports_streaming=False,
        supports_vision=False,
        priority=0,
        enabled=True,
        capabilities_blob={},
        input_modalities_blob=[],
    )


class LLMUsageNormalizationTests(unittest.TestCase):
    def test_openai_usage_splits_cached_uncached_write_and_reasoning_tokens(self) -> None:
        usage = _response_usage(
            {
                "usage": {
                    "prompt_tokens": 1_000,
                    "completion_tokens": 300,
                    "prompt_tokens_details": {
                        "cached_tokens": 800,
                        "cache_write_tokens": 50,
                    },
                    "completion_tokens_details": {
                        "reasoning_tokens": 200,
                    },
                    "cost": 1.25,
                }
            }
        )

        self.assertEqual(usage.input_tokens, 1_000)
        self.assertEqual(usage.uncached_input_tokens, 150)
        self.assertEqual(usage.cached_input_tokens, 800)
        self.assertEqual(usage.cache_write_input_tokens, 50)
        self.assertEqual(usage.output_tokens, 300)
        self.assertEqual(usage.reasoning_tokens, 200)
        self.assertAlmostEqual(usage.cost, 1.25)
        self.assertTrue(usage.reported)
        self.assertAlmostEqual(usage.cache_hit_rate, 0.8)

    def test_anthropic_usage_combines_separate_input_categories(self) -> None:
        usage = _response_usage(
            {
                "usage": {
                    "input_tokens": 100,
                    "cache_read_input_tokens": 800,
                    "cache_creation_input_tokens": 100,
                    "output_tokens": 50,
                }
            }
        )

        self.assertEqual(usage.input_tokens, 1_000)
        self.assertEqual(usage.uncached_input_tokens, 100)
        self.assertEqual(usage.cached_input_tokens, 800)
        self.assertEqual(usage.cache_write_input_tokens, 100)
        self.assertEqual(usage.output_tokens, 50)
        self.assertTrue(usage.reported)

    def test_missing_usage_is_distinguishable_from_reported_zero(self) -> None:
        self.assertFalse(_response_usage({}).reported)
        reported_zero = _response_usage({"usage": {}})
        self.assertTrue(reported_zero.reported)
        self.assertEqual(reported_zero.input_tokens, 0)

    def test_openai_terminal_stream_usage_chunk_is_normalized(self) -> None:
        events = _parse_openai_chat_stream_chunk(
            {
                "choices": [],
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 75},
                },
            }
        )

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_kind, "done")
        self.assertEqual(events[0].input_tokens, 100)
        self.assertEqual(events[0].uncached_input_tokens, 25)
        self.assertEqual(events[0].cached_input_tokens, 75)
        self.assertEqual(events[0].output_tokens, 20)
        self.assertTrue(events[0].usage_reported)

    def test_stream_usage_can_be_disabled_per_endpoint(self) -> None:
        enabled = _endpoint()
        disabled = _endpoint()
        disabled.capabilities_blob = {"supports_stream_usage": False}

        self.assertTrue(_include_openai_stream_usage(enabled))
        self.assertFalse(_include_openai_stream_usage(disabled))


class LLMUsageLedgerTests(unittest.TestCase):
    def test_runtime_ledger_and_status_expose_cache_statistics(self) -> None:
        outcome = CanonicalLLMOutcome(
            text="ok",
            input_tokens=1_000,
            uncached_input_tokens=150,
            cached_input_tokens=800,
            cache_write_input_tokens=50,
            output_tokens=300,
            reasoning_tokens=200,
            cost=1.25,
            usage_reported=True,
        )
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(endpoints=(_endpoint(),)),
            settings_repository=_Settings(),
            endpoint_invoker=_UsageInvoker(outcome),
            endpoint_retry_attempts=1,
        )

        result = runtime.generate(
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=128,
            )
        )
        snapshot = runtime.usage_snapshot()

        self.assertEqual(result.provider_response_count, 1)
        self.assertEqual(snapshot["request_count"], 1)
        self.assertEqual(snapshot["provider_request_count"], 1)
        self.assertEqual(snapshot["input_tokens"], 1_000)
        self.assertEqual(snapshot["cached_input_tokens"], 800)
        self.assertEqual(snapshot["reasoning_tokens"], 200)
        self.assertAlmostEqual(snapshot["cache_hit_rate"], 0.8)
        self.assertAlmostEqual(snapshot["usage_reporting_rate"], 1.0)
        self.assertEqual(snapshot["by_endpoint"][0]["endpoint_id"], "primary")

        provider = LLMIntrospectionProvider(runtime=runtime)
        status = provider.handle_status_control_action(None)
        self.assertIn("Prompt cache hit rate: 80.0%", status["message"])
        self.assertIn("1 successful", status["message"])
        self.assertEqual(status["usage"]["cached_input_tokens"], 800)

        introspection = provider.usage(IntrospectionCall(name="llm_usage"))
        self.assertEqual(introspection.structured["usage"]["input_tokens"], 1_000)

    def test_failed_request_and_provider_attempt_are_counted_separately(self) -> None:
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(endpoints=(_endpoint(),)),
            settings_repository=_Settings(),
            endpoint_invoker=_FailingInvoker(),
            endpoint_retry_attempts=1,
        )

        outcome = runtime.generate(
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "hello"}],
                max_output_tokens=128,
            )
        )
        snapshot = runtime.usage_snapshot()

        self.assertEqual(outcome.finish_reason, "error")
        self.assertEqual(snapshot["request_count"], 1)
        self.assertEqual(snapshot["successful_request_count"], 0)
        self.assertEqual(snapshot["failed_request_count"], 1)
        self.assertEqual(snapshot["provider_request_count"], 1)
        self.assertEqual(snapshot["failed_attempt_count"], 1)
        self.assertEqual(snapshot["by_endpoint"][0]["failed_request_count"], 1)

    def test_status_command_routes_to_llm_module(self) -> None:
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(endpoints=(_endpoint(),)),
            settings_repository=_Settings(),
            endpoint_invoker=_UsageInvoker(CanonicalLLMOutcome(text="ok")),
            endpoint_retry_attempts=1,
        )
        core = PalCore()
        register_llm_with_core(core.context, runtime)
        action = ControlPlane().parse_event(
            ControlEvent(
                event_kind=EventKind.SLASH_COMMAND,
                source_kind=SourceKind.CHANNEL,
                payload={"text": "/status"},
            )
        )

        self.assertIsNotNone(action)
        assert action is not None
        self.assertEqual(action.action_kind, "show_llm_status")
        self.assertEqual(action.target_scope, "llm")
        handled = asyncio.run(core.context.control_action_registry.handle(action))
        self.assertTrue(handled.handled)
        self.assertIn("LLM status", handled.message)


if __name__ == "__main__":
    unittest.main()
