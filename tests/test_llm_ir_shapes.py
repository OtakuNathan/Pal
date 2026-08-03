from __future__ import annotations

from pal.shared.tool_protocol import (
    ToolCallIR,
    ToolDefinitionIR,
    ToolResultIR,
    new_tool_call,
)

import json
import importlib
import unittest

from pal.llm.conversions import LLMConversionError, message_ir_from_dict, message_ir_to_dict
from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    LLMResponseDeltaKind,
    MessageRole,
    ReasoningPartIR,
    TextPartIR,
    WireShape,
)
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext, ShapeDecodeError, _JSONFrame as JSONFrame
from pal.llm.serde import part_from_payload, request_to_payload, response_from_payload, response_to_payload
from pal.llm.transport import _iter_json_frames


def _context(shape: WireShape) -> ShapeContext:
    return ShapeContext(wire_shape=shape, endpoint_id="demo", model_id="demo-model")


def decode_frames(codec, frames, context):
    updates = tuple(codec.decode(frames, context))
    if not updates:
        raise AssertionError("codec produced no response updates")
    return updates[-1].response, updates


class LLMIRShapeTests(unittest.TestCase):
    def test_tool_protocol_has_no_llm_facade_exports(self) -> None:
        llm = importlib.import_module("pal.llm")
        ir = importlib.import_module("pal.llm.ir")
        contracts = importlib.import_module("pal.llm.contracts")

        for module in (llm, ir, contracts):
            self.assertFalse(hasattr(module, "ToolCallIR"))
            self.assertFalse(hasattr(module, "ToolDefinitionIR"))
            self.assertFalse(hasattr(module, "ToolResultIR"))
        self.assertFalse(hasattr(llm, "JSONFrame"))
        self.assertFalse(hasattr(ir, "JSONFrame"))

    def test_tool_call_identity_is_explicit_for_received_ir(self) -> None:
        with self.assertRaises(TypeError):
            ToolCallIR(name="read", arguments={})  # type: ignore[call-arg]

        local = new_tool_call(name="read", arguments={"path": "a"})
        self.assertTrue(local.call_id.startswith("call_"))
        self.assertEqual(local.args, {"path": "a"})

    def test_ir_serde_preserves_zero_provider_responses_and_rejects_missing_call_id(self) -> None:
        response = response_from_payload(
            {
                "message": {
                    "role": "assistant",
                    "parts": [{"kind": "text", "text": "failed"}],
                    "message_id": "message-1",
                    "state": "complete",
                },
                "finish_reason": "error",
                "provider_response_count": 0,
            }
        )
        self.assertEqual(response.provider_response_count, 0)
        self.assertEqual(response_to_payload(response)["provider_response_count"], 0)
        with self.assertRaisesRegex(ValueError, "missing call_id"):
            part_from_payload({"kind": "tool_call", "name": "read", "arguments": {}})

    def test_legacy_conversion_preserves_reasoning_and_rejects_lossy_tool_data(self) -> None:
        message = message_ir_from_dict(
            {
                "role": "assistant",
                "reasoning_content": "inspect the contract",
                "content": "done",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "read", "arguments": '{"path":"a"}'},
                    }
                ],
            }
        )

        rendered = message_ir_to_dict(message)

        self.assertEqual(rendered["reasoning_content"], "inspect the contract")
        self.assertEqual(rendered["tool_calls"][0]["id"], "call-1")
        self.assertEqual(
            json.loads(rendered["tool_calls"][0]["function"]["arguments"]),
            {"path": "a"},
        )
        self.assertEqual(message_ir_from_dict(rendered), message)

    def test_legacy_conversion_ignores_ill_formed_call_identity(self) -> None:
        message = message_ir_from_dict(
            {
                "role": "assistant",
                "content": "not executable",
                "tool_calls": [
                    {"type": "function", "function": {"name": "read", "arguments": "{}"}},
                    {"id": "call-2", "type": "function", "function": {"arguments": "{}"}},
                ],
            }
        )

        self.assertEqual(message.tool_calls, ())

    def test_wire_decoders_never_synthesize_missing_tool_call_ids(self) -> None:
        cases = (
            (
                WireShape.OPENAI_COMPLETION,
                {
                    "choices": [
                        {
                            "message": {
                                "content": "ignored malformed call",
                                "tool_calls": [
                                    {"type": "function", "function": {"name": "read", "arguments": "{}"}}
                                ],
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            ),
            (
                WireShape.OPENAI_RESPONSE,
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ignored malformed call"}],
                        },
                        {"type": "function_call", "id": "item-only", "name": "read", "arguments": "{}"}
                    ],
                },
            ),
            (
                WireShape.ANTHROPIC_MESSAGES,
                {
                    "content": [
                        {"type": "text", "text": "ignored malformed call"},
                        {"type": "tool_use", "name": "read", "input": {}},
                    ],
                    "stop_reason": "tool_use",
                },
            ),
        )

        for shape, payload in cases:
            with self.subTest(shape=shape.value):
                response, _ = decode_frames(
                    codec_for_shape(shape),
                    [JSONFrame(0, payload)],
                    _context(shape),
                )
                self.assertEqual(response.tool_calls, ())

    def test_error_terminals_never_promote_tool_calls(self) -> None:
        cases = (
            (
                WireShape.OPENAI_COMPLETION,
                {
                    "choices": [
                        {
                            "message": {
                                "content": "failed",
                                "tool_calls": [
                                    {
                                        "id": "call-1",
                                        "function": {"name": "write", "arguments": "{}"},
                                    }
                                ],
                            },
                            "finish_reason": "error",
                        }
                    ]
                },
            ),
            (
                WireShape.OPENAI_RESPONSE,
                {
                    "status": "failed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "failed"}],
                        },
                        {
                            "type": "function_call",
                            "call_id": "call-1",
                            "name": "write",
                            "arguments": "{}",
                        },
                    ],
                },
            ),
            (
                WireShape.ANTHROPIC_MESSAGES,
                {
                    "content": [
                        {"type": "text", "text": "failed"},
                        {"type": "tool_use", "id": "call-1", "name": "write", "input": {}},
                    ],
                    "stop_reason": "error",
                },
            ),
        )

        for shape, payload in cases:
            with self.subTest(shape=shape.value):
                response, _ = decode_frames(
                    codec_for_shape(shape),
                    [JSONFrame(0, payload)],
                    _context(shape),
                )
                self.assertEqual(response.finish_reason, "error")
                self.assertEqual(response.tool_calls, ())

    def test_legacy_conversion_reports_exact_invalid_argument_location(self) -> None:
        with self.assertRaisesRegex(
            LLMConversionError,
            r"tool call 'read' \(call-1\) arguments are invalid JSON at line 1 column 9",
        ):
            message_ir_from_dict(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "function": {"name": "read", "arguments": '{"path":]'},
                        }
                    ],
                }
            )

    def test_ir_deep_freezes_all_json_mappings(self) -> None:
        source = {"nested": {"values": ["a", "b"]}}
        request = LLMRequestIR(
            messages=(LLMMessageIR(MessageRole.USER, (TextPartIR("work"),)),),
            tools=(
                ToolDefinitionIR(
                    name="read",
                    description="read",
                    input_schema=source,
                ),
            ),
            policy=GenerationPolicyIR(max_output_tokens=100),
            metadata=source,
        )
        call = new_tool_call(
            call_id="call-1",
            name="read",
            arguments=source,
        )
        source["nested"]["values"].append("mutated")

        self.assertEqual(request.metadata["nested"]["values"], ("a", "b"))
        self.assertEqual(call.arguments["nested"]["values"], ("a", "b"))
        with self.assertRaises(TypeError):
            request.metadata["new"] = True
        with self.assertRaises(TypeError):
            request.tools[0].input_schema["nested"]["new"] = True

        serialized = request_to_payload(request)
        self.assertEqual(json.loads(json.dumps(serialized)), serialized)
        for shape in WireShape:
            encoded = codec_for_shape(shape).encode(request, _context(shape)).payload
            self.assertEqual(json.loads(json.dumps(encoded)), encoded)

    def test_openai_completion_stream_and_single_shot_are_semantically_equal(self) -> None:
        shape = WireShape.OPENAI_COMPLETION
        codec = codec_for_shape(shape)
        complete, _ = decode_frames(
            codec,
            [
                JSONFrame(
                    0,
                    {
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "reasoning_content": "inspect",
                                    "content": "done",
                                    "tool_calls": [
                                        {
                                            "id": "call-1",
                                            "type": "function",
                                            "function": {"name": "read", "arguments": '{"path":"a"}'},
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                    },
                )
            ],
            _context(shape),
        )
        streamed, _ = decode_frames(
            codec,
            [
                JSONFrame(0, {"choices": [{"delta": {"reasoning_content": "inspect"}, "finish_reason": None}]}),
                JSONFrame(1, {"choices": [{"delta": {"content": "done"}, "finish_reason": None}]}),
                JSONFrame(
                    2,
                    {
                        "choices": [
                            {
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "call-1",
                                            "function": {"name": "read", "arguments": '{"path"'},
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ]
                    },
                ),
                JSONFrame(
                    3,
                    {
                        "choices": [
                            {
                                "delta": {"tool_calls": [{"index": 0, "function": {"arguments": ':"a"}'}}]},
                                "finish_reason": "tool_calls",
                            }
                        ],
                        "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                    },
                ),
            ],
            _context(shape),
        )
        self.assertEqual(complete.message.text, streamed.message.text)
        self.assertEqual(complete.message.reasoning_text, streamed.message.reasoning_text)
        self.assertEqual(complete.message.tool_calls, streamed.message.tool_calls)
        self.assertEqual(complete.finish_reason, streamed.finish_reason)
        self.assertEqual(complete.usage, streamed.usage)

    def test_openai_responses_stream_and_single_shot_are_semantically_equal(self) -> None:
        shape = WireShape.OPENAI_RESPONSE
        codec = codec_for_shape(shape)
        output = [
            {"type": "reasoning", "summary": [{"type": "summary_text", "text": "inspect"}]},
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
            {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": '{"path":"a"}'},
        ]
        complete, _ = decode_frames(
            codec,
            [JSONFrame(0, {"output": output, "usage": {"input_tokens": 7, "output_tokens": 3}})],
            _context(shape),
        )
        streamed, _ = decode_frames(
            codec,
            [
                JSONFrame(0, {"type": "response.reasoning_summary_text.delta", "delta": "inspect"}),
                JSONFrame(1, {"type": "response.output_text.delta", "delta": "done"}),
                JSONFrame(2, {"type": "response.output_item.added", "output_index": 2, "item": {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": ""}}),
                JSONFrame(3, {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": '{"path"'}),
                JSONFrame(4, {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": ':"a"}'}),
                JSONFrame(5, {"type": "response.output_item.done", "output_index": 2, "item": output[2]}),
                JSONFrame(6, {"type": "response.completed", "response": {"usage": {"input_tokens": 7, "output_tokens": 3}}}),
            ],
            _context(shape),
        )
        self.assertEqual(complete.message.text, streamed.message.text)
        self.assertEqual(complete.message.reasoning_text, streamed.message.reasoning_text)
        self.assertEqual(complete.message.tool_calls, streamed.message.tool_calls)
        self.assertEqual(complete.finish_reason, streamed.finish_reason)
        self.assertEqual(complete.usage, streamed.usage)

    def test_anthropic_stream_and_single_shot_are_semantically_equal(self) -> None:
        shape = WireShape.ANTHROPIC_MESSAGES
        codec = codec_for_shape(shape)
        content = [
            {"type": "thinking", "thinking": "inspect", "signature": "sig"},
            {"type": "text", "text": "done"},
            {"type": "tool_use", "id": "call-1", "name": "read", "input": {"path": "a"}},
        ]
        complete, _ = decode_frames(
            codec,
            [JSONFrame(0, {"content": content, "stop_reason": "tool_use", "usage": {"input_tokens": 7, "output_tokens": 3}})],
            _context(shape),
        )
        streamed, _ = decode_frames(
            codec,
            [
                JSONFrame(0, {"type": "content_block_start", "index": 0, "content_block": {"type": "thinking", "thinking": ""}}),
                JSONFrame(1, {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "inspect"}}),
                JSONFrame(2, {"type": "content_block_delta", "index": 0, "delta": {"type": "signature_delta", "signature": "sig"}}),
                JSONFrame(3, {"type": "content_block_start", "index": 1, "content_block": {"type": "text", "text": ""}}),
                JSONFrame(4, {"type": "content_block_delta", "index": 1, "delta": {"type": "text_delta", "text": "done"}}),
                JSONFrame(5, {"type": "content_block_start", "index": 2, "content_block": {"type": "tool_use", "id": "call-1", "name": "read", "input": {}}}),
                JSONFrame(6, {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": '{"path"'}}),
                JSONFrame(7, {"type": "content_block_delta", "index": 2, "delta": {"type": "input_json_delta", "partial_json": ':"a"}'}}),
                JSONFrame(8, {"type": "content_block_stop", "index": 2}),
                JSONFrame(9, {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"input_tokens": 7, "output_tokens": 3}}),
            ],
            _context(shape),
        )
        self.assertEqual(complete.message.text, streamed.message.text)
        self.assertEqual(complete.message.reasoning_text, streamed.message.reasoning_text)
        self.assertEqual(complete.message.tool_calls, streamed.message.tool_calls)
        self.assertEqual(complete.finish_reason, streamed.finish_reason)
        self.assertEqual(complete.usage, streamed.usage)

    def test_anthropic_fallback_replays_reasoning_when_envelope_is_missing(self) -> None:
        codec = codec_for_shape(WireShape.ANTHROPIC_MESSAGES)
        message = LLMMessageIR(
            MessageRole.ASSISTANT,
            (
                ReasoningPartIR("inspect"),
                new_tool_call("call-1", "read", {"path": "a"}),
            ),
        )
        request = LLMRequestIR(
            messages=(message,),
            tools=(),
            policy=GenerationPolicyIR(
                max_output_tokens=100,
                thinking_level="high",
            ),
        )

        encoded = codec.encode(request, _context(WireShape.ANTHROPIC_MESSAGES)).payload
        blocks = encoded["messages"][0]["content"]
        self.assertEqual(blocks[0], {"type": "thinking", "thinking": "inspect"})
        self.assertEqual(blocks[1]["type"], "tool_use")

    def test_fragmented_invalid_tool_json_never_becomes_executable(self) -> None:
        shape = WireShape.OPENAI_COMPLETION
        codec = codec_for_shape(shape)
        with self.assertRaises(ShapeDecodeError):
            list(codec.decode(
                [
                    JSONFrame(0, {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "read", "arguments": '{"path"'}}]}, "finish_reason": None}]}),
                    JSONFrame(1, {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]}),
                ],
                _context(shape),
            ))

    def test_stream_end_without_terminal_never_promotes_tool_draft(self) -> None:
        frames_by_shape = {
            WireShape.OPENAI_COMPLETION: [
                JSONFrame(0, {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "read", "arguments": '{"path"'}}]}, "finish_reason": None}]})
            ],
            WireShape.OPENAI_RESPONSE: [
                JSONFrame(0, {"type": "response.output_item.added", "output_index": 0, "item": {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": ""}}),
                JSONFrame(1, {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": '{"path"'}),
            ],
            WireShape.ANTHROPIC_MESSAGES: [
                JSONFrame(0, {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call-1", "name": "read", "input": {}}}),
                JSONFrame(1, {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path"'}}),
            ],
        }
        for shape, frames in frames_by_shape.items():
            with self.subTest(shape=shape):
                with self.assertRaises(ShapeDecodeError):
                    list(codec_for_shape(shape).decode(frames, _context(shape)))

    def test_length_terminal_discards_even_valid_tool_call(self) -> None:
        frames_by_shape = {
            WireShape.OPENAI_COMPLETION: [
                JSONFrame(0, {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]}),
                JSONFrame(1, {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "read", "arguments": '{"path":"a"}'}}]}, "finish_reason": "length"}]}),
            ],
            WireShape.OPENAI_RESPONSE: [
                JSONFrame(0, {"type": "response.output_text.delta", "delta": "partial"}),
                JSONFrame(1, {"type": "response.output_item.added", "output_index": 1, "item": {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": ""}}),
                JSONFrame(2, {"type": "response.output_item.done", "output_index": 1, "item": {"type": "function_call", "call_id": "call-1", "name": "read", "arguments": '{"path":"a"}'}}),
                JSONFrame(3, {"type": "response.incomplete", "response": {"usage": {}}}),
            ],
            WireShape.ANTHROPIC_MESSAGES: [
                JSONFrame(0, {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
                JSONFrame(1, {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial"}}),
                JSONFrame(2, {"type": "content_block_start", "index": 1, "content_block": {"type": "tool_use", "id": "call-1", "name": "read", "input": {}}}),
                JSONFrame(3, {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"path":"a"}'}}),
                JSONFrame(4, {"type": "content_block_stop", "index": 1}),
                JSONFrame(5, {"type": "message_delta", "delta": {"stop_reason": "max_tokens"}}),
            ],
        }
        for shape, frames in frames_by_shape.items():
            with self.subTest(shape=shape):
                updates = list(codec_for_shape(shape).decode(frames, _context(shape)))
                response = updates[-1].response
                self.assertFalse(any(update.delta_kind == LLMResponseDeltaKind.TOOL_CALL for update in updates))
                self.assertEqual(response.message.tool_calls, ())
                self.assertEqual(response.finish_reason.value, "length")

    def test_shape_replay_is_scoped_to_endpoint_and_model(self) -> None:
        shape = WireShape.OPENAI_COMPLETION
        codec = codec_for_shape(shape)
        response, _ = decode_frames(
            codec,
            [JSONFrame(0, {"choices": [{"message": {"role": "assistant", "reasoning_content": "r", "content": "x"}, "finish_reason": "stop"}]})],
            _context(shape),
        )
        request = LLMRequestIR(
            messages=(response.message,),
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=100),
        )
        matching = codec.encode(request, _context(shape)).payload["messages"][0]
        other = codec.encode(request, ShapeContext(shape, "other", "demo-model")).payload["messages"][0]
        self.assertEqual(matching["reasoning_content"], "r")
        self.assertNotIn("reasoning_content", other)

    def test_encoder_keeps_tool_contract_and_result_ids(self) -> None:
        request = LLMRequestIR(
            messages=(
                LLMMessageIR(MessageRole.USER, (TextPartIR("go"),)),
                LLMMessageIR(MessageRole.ASSISTANT, (new_tool_call("call-1", "read", {"path": "a"}),)),
                LLMMessageIR(MessageRole.TOOL, (ToolResultIR("call-1", "read", "ok"),)),
            ),
            tools=(ToolDefinitionIR("read", "read a file", {"type": "object"}),),
            policy=GenerationPolicyIR(max_output_tokens=100),
        )
        for shape in WireShape:
            payload = codec_for_shape(shape).encode(request, _context(shape)).payload
            self.assertEqual(payload["model"], "demo-model")
            self.assertTrue(payload["tools"])

    def test_sdk_single_shot_and_stream_share_json_frame_boundary(self) -> None:
        class SDKObject:
            def __init__(self, value: int) -> None:
                self.value = value

            def model_dump(self, **_: object) -> dict[str, int]:
                return {"value": self.value}

        self.assertEqual(
            list(_iter_json_frames(SDKObject(1))),
            [JSONFrame(0, {"value": 1})],
        )
        self.assertEqual(
            list(_iter_json_frames(iter([SDKObject(1), SDKObject(2)]))),
            [JSONFrame(0, {"value": 1}), JSONFrame(1, {"value": 2})],
        )


if __name__ == "__main__":
    unittest.main()
