from __future__ import annotations

from pal.shared.tool_protocol import ToolCallIR, new_tool_call

from dataclasses import dataclass, replace
from types import SimpleNamespace
import unittest

from pal.llm.contracts import generation_result_from_values, request_ir_from_prompt
from pal.llm.ir import (
    LLMResponseDeltaKind,
    LLMResponseIR,
    LLMResponseUpdate,
    WireShape,
)
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext


@dataclass
class _Settings:
    active_endpoint_id: str = "test-endpoint"
    think_level: str = "medium"

    def get_think_level(self, endpoint_id: str) -> str:
        _ = endpoint_id
        return self.think_level

    def set_think_level(self, endpoint_id: str, think_level: str) -> None:
        _ = endpoint_id
        self.think_level = str(think_level)

    def get_active_llm_endpoint_id(self) -> str:
        return self.active_endpoint_id

    def set_active_llm_endpoint_id(self, endpoint_id: str) -> None:
        self.active_endpoint_id = endpoint_id


class _ScriptedInvoker:
    def __init__(self, responses: list[LLMResponseIR]) -> None:
        self.responses = list(responses)
        self.requests = []

    def invoke(self, endpoint, request, *, stream=False, timeout_seconds=180.0):
        _ = endpoint, stream, timeout_seconds
        self.requests.append(request)
        return self.responses.pop(0), ()

    def invoke_updates(self, endpoint, request, *, timeout_seconds=180.0):
        raise AssertionError("streaming was not expected")


class _StreamingRecoveryInvoker(_ScriptedInvoker):
    def __init__(self, first: LLMResponseIR, continuation: LLMResponseIR) -> None:
        super().__init__([continuation])
        self.first = first

    def invoke_updates(self, endpoint, request, *, timeout_seconds=180.0):
        _ = endpoint, timeout_seconds
        self.requests.append(request)
        partial = self.first.__class__(
            message=self.first.message.__class__(
                role=self.first.message.role,
                parts=tuple(part for part in self.first.message.parts if not isinstance(part, ToolCallIR)),
                message_id=self.first.message.message_id,
                state=self.first.message.state,
            ),
            finish_reason=self.first.finish_reason,
            usage=self.first.usage,
        )
        yield LLMResponseUpdate(partial, LLMResponseDeltaKind.TEXT, text_delta=partial.text)
        yield LLMResponseUpdate(self.first, LLMResponseDeltaKind.STATE)


def _endpoint(
    *,
    max_output_tokens: int = 64_000,
    max_output_tokens_upper_limit: int | None = None,
    context_window: int = 1_000_000,
):
    upper_limit = max_output_tokens_upper_limit or max_output_tokens
    return SimpleNamespace(
        endpoint_id="test-endpoint",
        provider="test",
        model_id="test-model",
        display_name="Test model",
        wire_shape=WireShape.OPENAI_COMPLETION.value,
        base_url="https://example.test/v1",
        auth_kind="api_key_ref",
        credential_ref="TEST_LLM_API_KEY",
        context_window=context_window,
        max_output_tokens=max_output_tokens,
        thinking_levels_blob=["off", "medium", "high"],
        default_thinking_level="medium",
        supports_tools=True,
        supports_streaming=True,
        supports_vision=False,
        capabilities_blob={
            "max_output_recovery": {
                "enabled": True,
                "upper_limit": upper_limit,
                "max_continuations": 3,
            }
        },
        input_modalities_blob=[],
        output_modalities_blob=[],
        priority=0,
        enabled=True,
        notes=None,
    )


def _runtime(invoker: _ScriptedInvoker, endpoint=None) -> LLMRuntime:
    return LLMRuntime(
        endpoint_resolver=EndpointResolver(endpoints=(endpoint or _endpoint(),)),
        settings_repository=_Settings(),
        endpoint_invoker=invoker,
        endpoint_retry_attempts=1,
    )


class LLMOutputRecoveryTests(unittest.TestCase):
    def test_committed_tool_item_short_circuits_length_recovery(self) -> None:
        response = generation_result_from_values(
            text="partial",
            tool_calls=[new_tool_call(name="write", args={"path": "a"})],
            finish_reason="length",
        ).response
        response = replace(
            response,
            message=replace(
                response.message,
                metadata={
                    "committed_items": [
                        {"item_id": "item-1", "item_kind": "tool_call"}
                    ]
                },
            ),
        )
        invoker = _ScriptedInvoker([response])

        outcome = _runtime(invoker).generate(
            request_ir_from_prompt(
                messages=[{"role": "user", "content": "work"}],
                max_output_tokens=64_000,
                metadata={"max_output_recovery_enabled": True},
            )
        )

        self.assertEqual(outcome.finish_reason, "length")
        self.assertEqual([call.name for call in outcome.tool_calls], ["write"])
        self.assertEqual(len(invoker.requests), 1)

    def test_continuation_keeps_text_but_never_exposes_truncated_tool_calls(self) -> None:
        responses = [
            generation_result_from_values(
                text="piece one ",
                tool_calls=[new_tool_call(name="must_not_run", args={})],
                finish_reason="length",
                input_tokens=10,
                output_tokens=20,
                usage_reported=True,
            ).response,
            generation_result_from_values(
                text="done",
                tool_calls=[new_tool_call(name="complete_tool", args={"ok": True})],
                finish_reason="tool_calls",
                input_tokens=12,
                output_tokens=4,
                usage_reported=True,
            ).response,
        ]
        invoker = _ScriptedInvoker(responses)

        outcome = _runtime(invoker, _endpoint(max_output_tokens=32_000)).generate(
            request_ir_from_prompt(
                messages=[{"role": "user", "content": "work"}],
                max_output_tokens=32_000,
                metadata={"max_output_recovery_enabled": True},
            )
        )

        self.assertEqual(outcome.text, "piece one done")
        self.assertEqual([call.name for call in outcome.tool_calls], ["complete_tool"])
        self.assertEqual(outcome.input_tokens, 22)
        self.assertEqual(outcome.output_tokens, 24)
        continuation_request = invoker.requests[1]
        self.assertEqual(continuation_request.messages[-2].text, "piece one ")
        self.assertEqual(continuation_request.messages[-2].tool_calls, ())
        self.assertIsNone(continuation_request.messages[-2].replay)
        self.assertIn("Continue directly", continuation_request.messages[-1].text)

    def test_recovery_never_expands_past_model_max_output(self) -> None:
        invoker = _ScriptedInvoker(
            [
                generation_result_from_values(text="a", finish_reason="length").response,
                generation_result_from_values(text="b", finish_reason="stop").response,
            ]
        )

        _runtime(invoker, _endpoint(max_output_tokens=8_192)).generate(
            request_ir_from_prompt(
                messages=[{"role": "user", "content": "work"}],
                max_output_tokens=64_000,
            )
        )

        self.assertEqual([request.policy.max_output_tokens for request in invoker.requests], [8_192, 8_192])

    def test_streaming_length_recovery_holds_terminal_and_continues_in_place(self) -> None:
        first = generation_result_from_values(
            text="piece one ",
            tool_calls=[new_tool_call(name="must_not_run", args={})],
            finish_reason="length",
        ).response
        continuation = generation_result_from_values(
            text="done",
            tool_calls=[new_tool_call(name="complete_tool", args={"ok": True})],
            finish_reason="tool_calls",
        ).response
        invoker = _StreamingRecoveryInvoker(first, continuation)

        updates = list(
            _runtime(invoker)._iter_stream_updates(
                request_ir_from_prompt(
                    messages=[{"role": "user", "content": "work"}],
                    max_output_tokens=32_000,
                    metadata={"max_output_recovery_enabled": True},
                )
            )
        )

        self.assertEqual([update.text_delta for update in updates if update.delta_kind == LLMResponseDeltaKind.TEXT], ["piece one ", "done"])
        self.assertEqual(
            [update.tool_call.name for update in updates if update.delta_kind == LLMResponseDeltaKind.TOOL_CALL],
            ["complete_tool"],
        )
        self.assertEqual(sum(update.delta_kind == LLMResponseDeltaKind.STATE for update in updates), 1)
        self.assertEqual(updates[-1].response.text, "piece one done")

    def test_disabled_recovery_returns_safe_truncation_without_calling_again(self) -> None:
        invoker = _ScriptedInvoker(
            [
                generation_result_from_values(
                    text="partial",
                    tool_calls=[new_tool_call(name="must_not_run", args={})],
                    finish_reason="length",
                ).response,
            ]
        )

        outcome = _runtime(invoker).generate(
            request_ir_from_prompt(
                messages=[{"role": "user", "content": "work"}],
                max_output_tokens=64_000,
                metadata={"max_output_recovery_enabled": False},
            )
        )

        self.assertEqual(outcome.finish_reason, "length")
        self.assertEqual(outcome.text, "partial")
        self.assertEqual(outcome.tool_calls, ())
        self.assertEqual(len(invoker.requests), 1)

    def test_recovery_escalates_to_upper_limit_before_continuing(self) -> None:
        invoker = _ScriptedInvoker(
            [
                generation_result_from_values(
                    text="discarded partial",
                    finish_reason="length",
                    output_tokens=20,
                    usage_reported=True,
                ).response,
                generation_result_from_values(
                    text="complete replacement",
                    finish_reason="stop",
                    output_tokens=4,
                    usage_reported=True,
                ).response,
            ]
        )
        endpoint = _endpoint(
            max_output_tokens=32_000,
            max_output_tokens_upper_limit=64_000,
        )

        outcome = _runtime(invoker, endpoint).generate(
            request_ir_from_prompt(
                messages=[{"role": "user", "content": "work"}],
                max_output_tokens=64_000,
            )
        )

        self.assertEqual(outcome.text, "complete replacement")
        self.assertEqual(outcome.output_tokens, 24)
        self.assertEqual(
            [item.policy.max_output_tokens for item in invoker.requests],
            [32_000, 64_000],
        )
        self.assertEqual(
            invoker.requests[1].metadata["max_output_recovery_stage"],
            "escalate",
        )

    def test_recovery_stops_when_continuation_would_overflow_context(self) -> None:
        invoker = _ScriptedInvoker(
            [generation_result_from_values(text="partial", finish_reason="length").response]
        )
        endpoint = _endpoint(max_output_tokens=1_024, context_window=2_200)

        outcome = _runtime(invoker, endpoint).generate(
            request_ir_from_prompt(
                messages=[{"role": "user", "content": "x" * 2_000}],
                max_output_tokens=1_024,
            )
        )

        self.assertEqual(outcome.finish_reason, "compact_required")
        self.assertEqual(len(invoker.requests), 0)

    def test_anthropic_explicit_budget_is_clamped_below_output_limit(self) -> None:
        request = request_ir_from_prompt(
            messages=[{"role": "user", "content": "work"}],
            max_output_tokens=32_000,
            thinking_budget_tokens=64_000,
        )
        request = request.__class__(
            messages=request.messages,
            tools=request.tools,
            policy=request.policy.__class__(
                max_output_tokens=32_000,
                thinking_level="high",
                thinking_budget_tokens=64_000,
            ),
        )
        context = ShapeContext(
            wire_shape=WireShape.ANTHROPIC_MESSAGES,
            endpoint_id="anthropic",
            model_id="claude",
        )
        payload = codec_for_shape(WireShape.ANTHROPIC_MESSAGES).encode(request, context).payload

        self.assertEqual(payload["thinking"], {"type": "enabled", "budget_tokens": 31_999})


if __name__ == "__main__":
    unittest.main()
