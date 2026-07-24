from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import unittest

from pal.llm.contracts import CanonicalLLMOutcome, CanonicalLLMRequest, CanonicalToolCall
from pal.llm.llm_adaptor.anthropic_api import think_level_to_anthropic_thinking
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.shared import LLMFinishReason, LLMStreamEventKind
from pal.stream_events import NormalizedLLMStreamEvent


@dataclass
class _Settings:
    active_endpoint_id: str = "test-endpoint"
    think_level: str = "balanced"

    def get_think_level(self) -> str:
        return self.think_level

    def get_active_llm_endpoint_id(self) -> str:
        return self.active_endpoint_id

    def set_active_llm_endpoint_id(self, endpoint_id: str) -> None:
        self.active_endpoint_id = endpoint_id


class _ScriptedInvoker:
    def __init__(self, outcomes: list[CanonicalLLMOutcome]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[CanonicalLLMRequest] = []

    def invoke(self, endpoint, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        _ = endpoint
        self.requests.append(request)
        return self.outcomes.pop(0)

    def invoke_stream(self, endpoint, request: CanonicalLLMRequest):
        raise AssertionError("streaming was not expected")


class _ScriptedStreamInvoker:
    def __init__(self, outcomes: list[list[NormalizedLLMStreamEvent]]) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[CanonicalLLMRequest] = []

    def invoke(self, endpoint, request: CanonicalLLMRequest) -> CanonicalLLMOutcome:
        raise AssertionError("non-streaming was not expected")

    def invoke_stream(self, endpoint, request: CanonicalLLMRequest):
        _ = endpoint
        self.requests.append(request)
        return list(self.outcomes.pop(0))


def _endpoint(*, upper_limit: int = 64_000, max_continuations: int = 3):
    return SimpleNamespace(
        endpoint_id="test-endpoint",
        provider="test",
        model_id="test-model",
        api_mode="openai_chat",
        base_url="https://example.test/v1",
        context_window=1_000_000,
        max_output_tokens=32_000,
        supports_streaming=True,
        supports_vision=False,
        capabilities_blob={
            "max_output_recovery": {
                "enabled": True,
                "upper_limit": upper_limit,
                "max_continuations": max_continuations,
            }
        },
        input_modalities_blob=[],
    )


def _runtime(invoker: _ScriptedInvoker, endpoint=None) -> LLMRuntime:
    return LLMRuntime(
        endpoint_resolver=EndpointResolver(endpoints=(endpoint or _endpoint(),)),
        settings_repository=_Settings(),
        endpoint_invoker=invoker,
        endpoint_retry_attempts=1,
    )


class LLMOutputRecoveryTests(unittest.TestCase):
    def test_escalates_from_endpoint_default_to_upper_limit(self) -> None:
        invoker = _ScriptedInvoker(
            [
                CanonicalLLMOutcome(
                    text="discard me",
                    tool_calls=[CanonicalToolCall(name="unsafe_partial", args={})],
                    finish_reason="length",
                    input_tokens=100,
                    uncached_input_tokens=40,
                    cached_input_tokens=50,
                    cache_write_input_tokens=10,
                    output_tokens=20,
                    reasoning_tokens=15,
                    cost=0.1,
                    usage_reported=True,
                ),
                CanonicalLLMOutcome(
                    text="complete",
                    finish_reason=LLMFinishReason.STOP,
                    input_tokens=110,
                    uncached_input_tokens=20,
                    cached_input_tokens=80,
                    cache_write_input_tokens=10,
                    output_tokens=7,
                    reasoning_tokens=5,
                    cost=0.2,
                    usage_reported=True,
                ),
            ]
        )

        outcome = _runtime(invoker).generate(
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "work"}],
                max_output_tokens=32_000,
                metadata={"prompt_budget_snapshot": {}},
            )
        )

        self.assertEqual([request.max_output_tokens for request in invoker.requests], [32_000, 64_000])
        self.assertEqual(outcome.text, "complete")
        self.assertEqual(outcome.tool_calls, [])
        self.assertEqual(outcome.input_tokens, 210)
        self.assertEqual(outcome.uncached_input_tokens, 60)
        self.assertEqual(outcome.cached_input_tokens, 130)
        self.assertEqual(outcome.cache_write_input_tokens, 20)
        self.assertEqual(outcome.output_tokens, 27)
        self.assertEqual(outcome.reasoning_tokens, 20)
        self.assertAlmostEqual(outcome.cost, 0.3)
        self.assertTrue(outcome.usage_reported)
        self.assertEqual(outcome.provider_response_count, 2)

    def test_continues_in_pieces_and_only_exposes_final_tool_call(self) -> None:
        invoker = _ScriptedInvoker(
            [
                CanonicalLLMOutcome(text="first attempt", finish_reason="length"),
                CanonicalLLMOutcome(
                    text="piece one ",
                    tool_calls=[CanonicalToolCall(name="partial_one", args={"bad": True})],
                    finish_reason="length",
                ),
                CanonicalLLMOutcome(
                    text="piece two ",
                    tool_calls=[CanonicalToolCall(name="partial_two", args={"bad": True})],
                    finish_reason="max_tokens",
                ),
                CanonicalLLMOutcome(
                    text="done",
                    tool_calls=[CanonicalToolCall(name="complete_tool", args={"ok": True})],
                    finish_reason=LLMFinishReason.TOOL_CALLS,
                ),
            ]
        )

        outcome = _runtime(invoker).generate(
            CanonicalLLMRequest(
                messages=[{"role": "user", "content": "work"}],
                max_output_tokens=32_000,
                metadata={"prompt_budget_snapshot": {}},
            )
        )

        self.assertEqual([request.max_output_tokens for request in invoker.requests], [32_000, 64_000, 64_000, 64_000])
        self.assertEqual(outcome.text, "piece one piece two done")
        self.assertEqual([call.name for call in outcome.tool_calls], ["complete_tool"])
        continuation_messages = invoker.requests[2].messages
        self.assertEqual(continuation_messages[-2], {"role": "assistant", "content": "piece one "})
        self.assertIn("No tool call from the truncated response was executed", continuation_messages[-1]["content"])
        self.assertNotIn("prompt_budget_snapshot", invoker.requests[2].metadata)

    def test_exhausted_truncation_never_exposes_tool_calls(self) -> None:
        endpoint = _endpoint(upper_limit=32_000, max_continuations=0)
        invoker = _ScriptedInvoker(
            [
                CanonicalLLMOutcome(
                    text="partial",
                    tool_calls=[CanonicalToolCall(name="must_not_run", args={})],
                    finish_reason="length",
                )
            ]
        )

        outcome = _runtime(invoker, endpoint).generate(
            CanonicalLLMRequest(messages=[{"role": "user", "content": "work"}], max_output_tokens=32_000)
        )

        self.assertEqual(outcome.finish_reason, "length")
        self.assertEqual(outcome.text, "partial")
        self.assertEqual(outcome.tool_calls, [])

    def test_intentionally_small_request_does_not_escalate(self) -> None:
        invoker = _ScriptedInvoker([CanonicalLLMOutcome(text="short partial", finish_reason="length")])

        outcome = _runtime(invoker).generate(
            CanonicalLLMRequest(messages=[{"role": "user", "content": "summarize"}], max_output_tokens=192)
        )

        self.assertEqual(len(invoker.requests), 1)
        self.assertEqual(outcome.finish_reason, "length")

    def test_upper_limit_recovery_does_not_overcommit_context_window(self) -> None:
        endpoint = _endpoint()
        endpoint.context_window = 50_000
        invoker = _ScriptedInvoker([CanonicalLLMOutcome(text="partial", finish_reason="length")])

        outcome = _runtime(invoker, endpoint).generate(
            CanonicalLLMRequest(messages=[{"role": "user", "content": "work"}], max_output_tokens=32_000)
        )

        self.assertEqual(len(invoker.requests), 1)
        self.assertEqual(outcome.finish_reason, "length")

    def test_stream_recovery_buffers_and_discards_partial_tool_calls(self) -> None:
        endpoint = _endpoint(upper_limit=32_000, max_continuations=1)
        invoker = _ScriptedStreamInvoker(
            [
                [
                    NormalizedLLMStreamEvent(event_kind=LLMStreamEventKind.TEXT_DELTA, text="piece "),
                    NormalizedLLMStreamEvent(
                        event_kind=LLMStreamEventKind.TOOL_CALL,
                        tool_call=CanonicalToolCall(name="must_not_run", args={}),
                    ),
                    NormalizedLLMStreamEvent(
                        event_kind=LLMStreamEventKind.DONE,
                        finish_reason="length",
                        input_tokens=50,
                        uncached_input_tokens=10,
                        cached_input_tokens=35,
                        cache_write_input_tokens=5,
                        output_tokens=12,
                        reasoning_tokens=9,
                        cost=0.05,
                        usage_reported=True,
                    ),
                ],
                [
                    NormalizedLLMStreamEvent(
                        event_kind=LLMStreamEventKind.TOOL_CALL,
                        tool_call=CanonicalToolCall(name="complete_tool", args={"ok": True}),
                    ),
                    NormalizedLLMStreamEvent(
                        event_kind=LLMStreamEventKind.DONE,
                        finish_reason=LLMFinishReason.TOOL_CALLS,
                        input_tokens=61,
                        uncached_input_tokens=11,
                        cached_input_tokens=40,
                        cache_write_input_tokens=10,
                        output_tokens=4,
                        reasoning_tokens=2,
                        cost=0.07,
                        usage_reported=True,
                    ),
                ],
            ]
        )

        events = _runtime(invoker, endpoint).generate_stream(
            CanonicalLLMRequest(messages=[{"role": "user", "content": "work"}], max_output_tokens=32_000)
        )

        calls = [event.tool_call.name for event in events if event.tool_call is not None]
        self.assertEqual(calls, ["complete_tool"])
        self.assertEqual("".join(event.text for event in events), "piece ")
        self.assertEqual(events[-1].finish_reason, LLMFinishReason.TOOL_CALLS)
        self.assertEqual(events[-1].input_tokens, 111)
        self.assertEqual(events[-1].uncached_input_tokens, 21)
        self.assertEqual(events[-1].cached_input_tokens, 75)
        self.assertEqual(events[-1].cache_write_input_tokens, 15)
        self.assertEqual(events[-1].output_tokens, 16)
        self.assertEqual(events[-1].reasoning_tokens, 11)
        self.assertAlmostEqual(events[-1].cost, 0.12)
        self.assertTrue(events[-1].usage_reported)
        self.assertEqual(events[-1].provider_response_count, 2)

    def test_explicit_anthropic_thinking_budget_is_clamped_below_output_limit(self) -> None:
        thinking = think_level_to_anthropic_thinking(
            "high",
            32_000,
            thinking_budget_tokens=64_000,
        )

        self.assertEqual(thinking, {"type": "enabled", "budget_tokens": 31_999})
        self.assertEqual(
            think_level_to_anthropic_thinking("low", 32_000, thinking_budget_tokens=1),
            {"type": "enabled", "budget_tokens": 1024},
        )


if __name__ == "__main__":
    unittest.main()
