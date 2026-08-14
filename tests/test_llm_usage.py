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
from pal.llm.contracts import generation_result_from_values, request_ir_from_prompt
from pal.llm.ir import WireShape
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext, _JSONFrame as JSONFrame
from pal.llm.shapes.builder import usage_from_mapping
from pal.shared import EventKind, IntrospectionCall, SourceKind


@dataclass
class _Settings:
    active_endpoint_id: str | None = "primary"
    think_level: str = "balanced"

    def get_think_level(self, endpoint_id: str) -> str:
        _ = endpoint_id
        return self.think_level

    def set_think_level(self, endpoint_id: str, think_level: str) -> None:
        _ = endpoint_id
        self.think_level = str(think_level)

    def get_active_llm_endpoint_id(self) -> str | None:
        return self.active_endpoint_id

    def set_active_llm_endpoint_id(self, endpoint_id: str) -> None:
        self.active_endpoint_id = str(endpoint_id)


class _UsageInvoker:
    def __init__(self, outcome: generation_result_from_values) -> None:
        self.outcome = outcome

    def invoke(self, endpoint, request, *, stream=False, timeout_seconds=180.0):
        _ = endpoint, request, stream, timeout_seconds
        return self.outcome.response, ()

    def invoke_updates(self, endpoint, request, *, timeout_seconds=180.0):
        raise AssertionError("streaming was not expected")


class _FailingInvoker:
    def invoke(self, endpoint, request, *, stream=False, timeout_seconds=180.0):
        _ = endpoint, request, stream, timeout_seconds
        raise RuntimeError("provider failed")

    def invoke_updates(self, endpoint, request, *, timeout_seconds=180.0):
        raise AssertionError("streaming was not expected")


def _endpoint(endpoint_id: str = "primary"):
    return SimpleNamespace(
        endpoint_id=endpoint_id,
        provider="openai",
        model_id="test-model",
        display_name="Test model",
        wire_shape="openai_completion",
        base_url="https://example.test/v1",
        auth_kind="api_key_ref",
        credential_ref="TEST_LLM_API_KEY",
        context_window=32_000,
        max_output_tokens=4_096,
        thinking_levels_blob=["off", "medium", "high"],
        default_thinking_level="medium",
        supports_tools=True,
        supports_streaming=False,
        supports_vision=False,
        priority=0,
        enabled=True,
        capabilities_blob={},
        input_modalities_blob=[],
        output_modalities_blob=[],
        notes=None,
    )


class LLMUsageNormalizationTests(unittest.TestCase):
    def test_openai_usage_splits_cached_uncached_write_and_reasoning_tokens(self) -> None:
        usage = usage_from_mapping({
            "prompt_tokens": 1_000,
            "completion_tokens": 300,
            "prompt_tokens_details": {"cached_tokens": 800, "cache_write_tokens": 50},
            "completion_tokens_details": {"reasoning_tokens": 200},
            "cost": 1.25,
        })

        self.assertEqual(usage.input_tokens, 1_000)
        self.assertEqual(usage.uncached_input_tokens, 150)
        self.assertEqual(usage.cached_input_tokens, 800)
        self.assertEqual(usage.cache_write_input_tokens, 50)
        self.assertEqual(usage.output_tokens, 300)
        self.assertEqual(usage.reasoning_tokens, 200)
        self.assertAlmostEqual(usage.cost, 1.25)
        self.assertTrue(usage.reported)

    def test_anthropic_usage_combines_separate_input_categories(self) -> None:
        usage = usage_from_mapping({
            "input_tokens": 100,
            "cache_read_input_tokens": 800,
            "cache_creation_input_tokens": 100,
            "output_tokens": 50,
        })

        self.assertEqual(usage.input_tokens, 1_000)
        self.assertEqual(usage.uncached_input_tokens, 100)
        self.assertEqual(usage.cached_input_tokens, 800)
        self.assertEqual(usage.cache_write_input_tokens, 100)
        self.assertEqual(usage.output_tokens, 50)
        self.assertTrue(usage.reported)

    def test_missing_usage_is_distinguishable_from_reported_zero(self) -> None:
        self.assertFalse(usage_from_mapping(None).reported)
        reported_zero = usage_from_mapping({})
        self.assertTrue(reported_zero.reported)
        self.assertEqual(reported_zero.input_tokens, 0)

    def test_openai_terminal_stream_usage_chunk_is_normalized(self) -> None:
        codec = codec_for_shape(WireShape.OPENAI_COMPLETION)
        updates = list(codec.decode(
            [
                JSONFrame(0, {
                    "choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}],
                }),
                JSONFrame(1, {
                    "choices": [],
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "prompt_tokens_details": {"cached_tokens": 75},
                    },
                }),
            ],
            ShapeContext(
                wire_shape=WireShape.OPENAI_COMPLETION,
                endpoint_id="primary",
                model_id="test-model",
            ),
        ))
        usage = updates[-1].response.usage
        self.assertEqual(usage.input_tokens, 100)
        self.assertEqual(usage.uncached_input_tokens, 25)
        self.assertEqual(usage.cached_input_tokens, 75)
        self.assertEqual(usage.output_tokens, 20)
        self.assertTrue(usage.reported)


class LLMUsageLedgerTests(unittest.TestCase):
    def test_llm_queries_do_not_refresh_or_activate_an_endpoint(self) -> None:
        runtime = LLMRuntime(
            endpoint_resolver=EndpointResolver(endpoints=(_endpoint(),)),
            settings_repository=_Settings(),
            endpoint_invoker=_FailingInvoker(),
            endpoint_retry_attempts=1,
        )
        runtime.refresh_runtime_settings = lambda: self.fail(  # type: ignore[method-assign]
            "read-only LLM query refreshed runtime settings"
        )

        provider = LLMIntrospectionProvider(runtime=runtime)
        result = provider.think_level(IntrospectionCall(name="llm_think_level"))
        active = provider.active(IntrospectionCall(name="llm_active"))

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.structured["endpoint_id"], "primary")
        self.assertEqual(active.status, "ok")
        self.assertEqual(active.structured["endpoint_id"], "primary")

    def test_runtime_ledger_and_status_expose_cache_statistics(self) -> None:
        outcome = generation_result_from_values(
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
            request_ir_from_prompt(
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
        self.assertIn("Prompt cache token ratio: 80.0%", status["message"])
        self.assertIn("Prompt cache request hit rate: 100.0%", status["message"])
        self.assertIn("\n\nInput tokens:", status["message"])
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
            request_ir_from_prompt(
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
            endpoint_invoker=_UsageInvoker(generation_result_from_values(text="ok")),
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
