from __future__ import annotations

import unittest

from pal.llm.endpoint import ShapeEndpointInvoker
from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    LLMResponseDeltaKind,
    LLMResponseIR,
    LLMResponseUpdate,
    MessageRole,
    ReasoningPartIR,
    TextPartIR,
    ThinkingLevel,
    WireShape,
)
from pal.llm.models import LLMEndpointModel
from pal.llm.response_hooks import (
    ProviderResponseHookError,
    ProviderResponseHookRegistry,
)
from pal.llm.shapes.base import _JSONFrame
from pal.shared.enums import LLMFinishReason
from pal.shared.tool_protocol import ToolCallIR


_OFFICIAL_TOKEN = "｜DSML｜"
_OBSERVED_TOKEN = "｜｜DSML｜｜"


def _dsml_call(
    *,
    token: str = _OFFICIAL_TOKEN,
    name: str = "search_tools",
    parameters: tuple[tuple[str, bool, str], ...] = (
        ("namespace", True, "introspection"),
        ("top_k", False, "3"),
    ),
) -> str:
    rendered = "\n".join(
        f'<{token}parameter name="{key}" string="{str(is_string).lower()}">{value}</{token}parameter>'
        for key, is_string, value in parameters
    )
    return (
        f"<{token}tool_calls>\n"
        f'<{token}invoke name="{name}">\n'
        f"{rendered}\n"
        f"</{token}invoke>\n"
        f"</{token}tool_calls>"
    )


def _request(*, thinking: ThinkingLevel = ThinkingLevel.OFF) -> LLMRequestIR:
    return LLMRequestIR(
        messages=(LLMMessageIR(MessageRole.USER, (TextPartIR("hello"),)),),
        tools=(),
        policy=GenerationPolicyIR(max_output_tokens=100, thinking_level=thinking),
    )


def _endpoint(
    *,
    provider: str = "deepseek",
    wire_shape: str = "anthropic_messages",
) -> LLMEndpointModel:
    return LLMEndpointModel(
        endpoint_id=f"{provider}-endpoint",
        provider=provider,
        model_id="deepseek-v4-flash",
        display_name="DeepSeek",
        wire_shape=wire_shape,
        base_url="https://example.test",
        auth_kind="api_key_ref",
        credential_ref="key",
        context_window=10_000,
        max_output_tokens=1_000,
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


class _FramesTransport:
    def __init__(self, frames: list[dict]) -> None:
        self.payloads = list(frames)

    def frames(self, request):
        for index, payload in enumerate(self.payloads):
            yield _JSONFrame(index, payload)

    def activate_endpoint(self, endpoint_id: str) -> None:
        pass

    def close(self) -> None:
        pass


def _anthropic_complete(text: str, *, reasoning: str = "") -> list[dict]:
    content: list[dict] = []
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning})
    content.append({"type": "text", "text": text})
    return [{"content": content, "stop_reason": "stop", "usage": {"input_tokens": 4, "output_tokens": 8}}]


def _anthropic_stream(text: str) -> list[dict]:
    midpoint = max(1, len(text) // 2)
    return [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[:midpoint]}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text[midpoint:]}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "stop"}},
        {"type": "message_stop"},
    ]


def _anthropic_character_stream(text: str, *, stop_reason: str = "stop") -> list[dict]:
    return [
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        *(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": char}}
            for char in text
        ),
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": stop_reason}},
        {"type": "message_stop"},
    ]


class DeepSeekProviderResponseHookTests(unittest.TestCase):
    def test_observed_dsml_variant_becomes_structured_tool_call_and_strips_internal_projection(self) -> None:
        leaked = (
            "<closed_tool_interaction>\n"
            "Historical tool evidence\n"
            "[old tool interaction cleared]\n"
            "</closed_tool_interaction>\n"
            + _dsml_call(token=_OBSERVED_TOKEN)
        )
        invoker = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(_anthropic_complete(leaked)),  # type: ignore[arg-type]
        )

        response, updates = invoker.invoke(_endpoint(), _request())

        self.assertEqual(response.finish_reason, LLMFinishReason.TOOL_CALLS)
        self.assertEqual(response.text, "")
        self.assertNotIn("DSML", response.text)
        self.assertNotIn("closed_tool_interaction", response.text)
        self.assertEqual(len(response.tool_calls), 1)
        self.assertEqual(response.tool_calls[0].name, "search_tools")
        self.assertEqual(response.tool_calls[0].args, {"namespace": "introspection", "top_k": 3})
        self.assertTrue(response.tool_calls[0].call_id.startswith("call_ds_"))
        self.assertEqual(updates[-1].delta_kind, LLMResponseDeltaKind.STATE)

    def test_deepseek_stream_never_exposes_raw_dsml_delta(self) -> None:
        raw = _dsml_call()
        invoker = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(_anthropic_stream(raw)),  # type: ignore[arg-type]
        )

        updates = list(invoker.invoke_updates(_endpoint(), _request()))

        self.assertTrue(any(update.delta_kind == LLMResponseDeltaKind.TOOL_CALL for update in updates))
        self.assertFalse(any("DSML" in update.text_delta for update in updates))
        self.assertFalse(any("DSML" in update.response.text for update in updates))
        self.assertEqual(updates[-1].response.finish_reason, LLMFinishReason.TOOL_CALLS)

    def test_non_deepseek_provider_does_not_apply_dsml_parser(self) -> None:
        raw = _dsml_call()
        invoker = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(_anthropic_stream(raw)),  # type: ignore[arg-type]
        )

        updates = list(
            invoker.invoke_updates(
                _endpoint(provider="other"),
                _request(),
            )
        )

        self.assertTrue(any("DSML" in update.text_delta for update in updates))
        self.assertEqual(updates[-1].response.tool_calls, ())

    def test_native_structured_tool_call_is_preserved(self) -> None:
        frames = [{
            "content": [{"type": "tool_use", "id": "call-native", "name": "read_file", "input": {"path": "a"}}],
            "stop_reason": "tool_use",
        }]
        invoker = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(frames),  # type: ignore[arg-type]
        )

        response, _ = invoker.invoke(_endpoint(), _request())

        self.assertEqual(response.tool_calls[0].call_id, "call-native")
        self.assertEqual(response.tool_calls[0].name, "read_file")

    def test_multiple_calls_and_json_parameter_types_are_preserved(self) -> None:
        first = _dsml_call(
            name="first",
            parameters=(("payload", False, '{"enabled":true,"items":[1,2],"value":null}'),),
        )
        second_invoke = (
            f'<{_OFFICIAL_TOKEN}invoke name="second">\n'
            f'<{_OFFICIAL_TOKEN}parameter name="message" string="true">hello</{_OFFICIAL_TOKEN}parameter>\n'
            f"</{_OFFICIAL_TOKEN}invoke>"
        )
        raw = first.replace(
            f"</{_OFFICIAL_TOKEN}tool_calls>",
            second_invoke + f"\n</{_OFFICIAL_TOKEN}tool_calls>",
        )
        invoker = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(_anthropic_complete(raw)),  # type: ignore[arg-type]
        )

        response, _ = invoker.invoke(_endpoint(), _request())

        self.assertEqual([call.name for call in response.tool_calls], ["first", "second"])
        self.assertEqual(
            response.tool_calls[0].args,
            {"payload": {"enabled": True, "items": [1, 2], "value": None}},
        )
        self.assertEqual(response.tool_calls[1].args, {"message": "hello"})

    def test_internal_projection_is_removed_even_without_dsml(self) -> None:
        raw = (
            "Before\n"
            "<closed_tool_interaction>secret historical tool result</closed_tool_interaction>\n"
            "[old tool result cleared]\n"
            "After"
        )
        invoker = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(_anthropic_complete(raw)),  # type: ignore[arg-type]
        )

        response, updates = invoker.invoke(_endpoint(), _request())

        self.assertIn("Before", response.text)
        self.assertIn("After", response.text)
        self.assertNotIn("closed_tool_interaction", response.text)
        self.assertNotIn("old tool result", response.text)
        self.assertFalse(any("closed_tool_interaction" in update.text_delta for update in updates))

    def test_structured_reasoning_and_textual_thinking_both_normalize(self) -> None:
        structured = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(_anthropic_complete(_dsml_call(), reasoning="structured thought")),  # type: ignore[arg-type]
        )
        structured_response, _ = structured.invoke(
            _endpoint(),
            _request(thinking=ThinkingLevel.HIGH),
        )
        self.assertEqual(structured_response.reasoning_text, "structured thought")

        textual = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(_anthropic_complete("private thought</think>Useful note\n\n" + _dsml_call())),  # type: ignore[arg-type]
        )
        textual_response, textual_updates = textual.invoke(
            _endpoint(),
            _request(thinking=ThinkingLevel.HIGH),
        )
        self.assertEqual(textual_response.reasoning_text, "private thought")
        self.assertEqual(textual_response.text, "Useful note")
        self.assertFalse(
            any(
                update.delta_kind == LLMResponseDeltaKind.TEXT
                and "private thought" in update.text_delta
                for update in textual_updates
            )
        )

    def test_malformed_dsml_is_a_response_error_not_assistant_text(self) -> None:
        invoker = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(_anthropic_complete(f"<{_OFFICIAL_TOKEN}tool_calls>broken")),  # type: ignore[arg-type]
        )

        with self.assertRaises(ProviderResponseHookError):
            invoker.invoke(_endpoint(), _request())

    def test_length_truncated_dsml_exposes_only_recovery_state(self) -> None:
        raw = f"<{_OFFICIAL_TOKEN}tool_calls>\n<{_OFFICIAL_TOKEN}invoke name=\"read_file\">"
        frames = [{
            "content": [{"type": "text", "text": raw}],
            "stop_reason": "max_tokens",
        }]
        invoker = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(frames),  # type: ignore[arg-type]
        )

        updates = list(invoker.invoke_updates(_endpoint(), _request()))

        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].delta_kind, LLMResponseDeltaKind.STATE)
        self.assertEqual(updates[0].response.finish_reason, LLMFinishReason.LENGTH)
        self.assertEqual(updates[0].text_delta, "")

    def test_provider_match_is_case_insensitive_and_independent_of_wire_shape(self) -> None:
        response = LLMResponseIR(
            message=LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR(_dsml_call()),)),
            finish_reason=LLMFinishReason.STOP,
        )
        update = LLMResponseUpdate(response, LLMResponseDeltaKind.STATE)
        registry = ProviderResponseHookRegistry.builtin()

        for shape in WireShape:
            with self.subTest(shape=shape):
                normalized = tuple(
                    registry.normalize(
                        endpoint_id="ep",
                        provider_id="DeepSeek",
                        model_id="any-model-name",
                        wire_shape=shape,
                        request=_request(),
                        updates=(update,),
                    )
                )
                self.assertIsInstance(normalized[-2].tool_call, ToolCallIR)
                self.assertEqual(normalized[-1].response.finish_reason, LLMFinishReason.TOOL_CALLS)

    def test_dsml_prefix_split_at_every_character_never_reaches_text_stream(self) -> None:
        raw = _dsml_call().replace(_OFFICIAL_TOKEN, "| DSML |")
        invoker = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(_anthropic_character_stream(raw)),  # type: ignore[arg-type]
        )

        updates = list(invoker.invoke_updates(_endpoint(), _request()))

        self.assertEqual(updates[-1].response.finish_reason, LLMFinishReason.TOOL_CALLS)
        self.assertEqual(updates[-1].response.tool_calls[0].name, "search_tools")
        self.assertFalse(any("DSML" in update.text_delta for update in updates))
        self.assertFalse(any("DSML" in update.response.text for update in updates))

    def test_partial_dsml_prefix_is_not_released_as_assistant_text(self) -> None:
        registry = ProviderResponseHookRegistry.builtin()
        for finish_reason in (
            LLMFinishReason.STOP,
            LLMFinishReason.ERROR,
            LLMFinishReason.CONTENT_FILTER,
        ):
            with self.subTest(finish_reason=finish_reason):
                response = LLMResponseIR(
                    message=LLMMessageIR(
                        MessageRole.ASSISTANT,
                        (TextPartIR("before <｜DSML｜tool_"),),
                    ),
                    finish_reason=finish_reason,
                )
                with self.assertRaises(ProviderResponseHookError):
                    tuple(
                        registry.normalize(
                            endpoint_id="ep",
                            provider_id="deepseek",
                            model_id="model",
                            wire_shape=WireShape.ANTHROPIC_MESSAGES,
                            request=_request(),
                            updates=(LLMResponseUpdate(response, LLMResponseDeltaKind.STATE),),
                        )
                    )

    def test_unsuccessful_terminal_response_cannot_promote_dsml_to_tool_call(self) -> None:
        registry = ProviderResponseHookRegistry.builtin()
        for finish_reason in (LLMFinishReason.ERROR, LLMFinishReason.CONTENT_FILTER):
            with self.subTest(finish_reason=finish_reason):
                response = LLMResponseIR(
                    message=LLMMessageIR(MessageRole.ASSISTANT, (TextPartIR(_dsml_call()),)),
                    finish_reason=finish_reason,
                )
                with self.assertRaises(ProviderResponseHookError):
                    tuple(
                        registry.normalize(
                            endpoint_id="ep",
                            provider_id="deepseek",
                            model_id="model",
                            wire_shape=WireShape.OPENAI_COMPLETION,
                            request=_request(),
                            updates=(LLMResponseUpdate(response, LLMResponseDeltaKind.STATE),),
                        )
                    )

    def test_native_tool_call_wins_over_textual_dsml_mirror(self) -> None:
        native = ToolCallIR("provider-call-id", "read_file", {"path": "native.txt"})
        response = LLMResponseIR(
            message=LLMMessageIR(
                MessageRole.ASSISTANT,
                (TextPartIR(_dsml_call(name="wrong_mirror")), native),
            ),
            finish_reason=LLMFinishReason.TOOL_CALLS,
        )

        normalized = tuple(
            ProviderResponseHookRegistry.builtin().normalize(
                endpoint_id="ep",
                provider_id="deepseek",
                model_id="model",
                wire_shape=WireShape.OPENAI_RESPONSE,
                request=_request(),
                updates=(LLMResponseUpdate(response, LLMResponseDeltaKind.STATE),),
            )
        )

        self.assertEqual(normalized[-1].response.tool_calls, (native,))
        self.assertNotIn("DSML", normalized[-1].response.text)
        self.assertNotIn("wrong_mirror", normalized[-1].response.text)

    def test_dsml_in_reasoning_is_rejected_before_it_can_stream(self) -> None:
        response = LLMResponseIR(
            message=LLMMessageIR(
                MessageRole.ASSISTANT,
                (ReasoningPartIR(_dsml_call()),),
            ),
            finish_reason=LLMFinishReason.STOP,
        )
        with self.assertRaises(ProviderResponseHookError):
            tuple(
                ProviderResponseHookRegistry.builtin().normalize(
                    endpoint_id="ep",
                    provider_id="deepseek",
                    model_id="model",
                    wire_shape=WireShape.ANTHROPIC_MESSAGES,
                    request=_request(thinking=ThinkingLevel.HIGH),
                    updates=(LLMResponseUpdate(response, LLMResponseDeltaKind.STATE),),
                )
            )

    def test_ordinary_deepseek_text_still_streams_incrementally(self) -> None:
        invoker = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(_anthropic_character_stream("plain response")),  # type: ignore[arg-type]
        )

        updates = list(invoker.invoke_updates(_endpoint(), _request()))

        text_updates = [
            update.text_delta
            for update in updates
            if update.delta_kind == LLMResponseDeltaKind.TEXT
        ]
        self.assertGreater(len(text_updates), 1)
        self.assertEqual("".join(text_updates), "plain response")


if __name__ == "__main__":
    unittest.main()
