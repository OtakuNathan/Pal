"""P2: semantic/native split at the decode boundary.

Three real shape codecs decode synthetic-opaque fixtures; NativeCapture
(pass-through, pre-hook) must record the native envelope, and the structural
continuation contracts must judge it.  The DeepSeek case pins the actual
loss point: the hook projects replay=None, the capture still holds the
envelope.  Gate (PLAN P2): nothing may delete signatures/reasoning to make
tests pass — every preserved-path test asserts the opaque value byte-for-byte.
"""
from __future__ import annotations

import unittest

from pal.llm.ir import (
    GenerationPolicyIR,
    LLMRequestIR,
    LLMResponseDeltaKind,
    WireShape,
)
from pal.llm.continuation_policy import (
    ContinuationDecisionKind,
    NativeCandidate,
    validate_candidate,
)
from pal.llm.native_capture import decode_with_capture
from pal.llm.response_hooks import ProviderResponseHookContext
from pal.llm.shapes import codec_for_shape
from pal.llm.shapes.base import ShapeContext
from pal.llm.shapes.base import _JSONFrame
from pal.llm.deepseek_response import normalize_deepseek_updates
from pal.shared.tool_protocol import ToolCallIR


def _context(shape: WireShape) -> ShapeContext:
    return ShapeContext(
        wire_shape=shape, endpoint_id="demo", model_id="demo-model"
    )


def _frames(payloads: list[dict]):
    return (_JSONFrame(index, payload) for index, payload in enumerate(payloads))


def _decode_captured(shape: WireShape, payloads: list[dict]):
    codec = codec_for_shape(shape)
    updates = codec.decode(_frames(payloads), _context(shape))
    capture = decode_with_capture(
        updates, endpoint_id="demo", model_id="demo-model"
    )
    return capture


def _anthropic_payload(blocks: list[dict], stop_reason: str = "tool_use") -> dict:
    return {
        "content": blocks,
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 3, "output_tokens": 5},
    }


class AnthropicCaptureTests(unittest.TestCase):
    def test_thinking_signature_and_tool_use_preserved_verbatim(self) -> None:
        capture = _decode_captured(
            WireShape.ANTHROPIC_MESSAGES,
            [
                _anthropic_payload(
                    [
                        {"type": "thinking", "thinking": "private", "signature": "sig-abc"},
                        {"type": "text", "text": "checking"},
                        {
                            "type": "tool_use",
                            "id": "call-a",
                            "name": "lookup",
                            "input": {"q": "life"},
                        },
                    ]
                )
            ],
        )
        semantic = list(capture)
        candidate = capture.result()

        self.assertIsNotNone(candidate)
        decision = validate_candidate(candidate)
        self.assertEqual(decision.kind, ContinuationDecisionKind.PRESERVED, decision.issues)
        # P2 gate: the signature survives byte-for-byte, not as a placeholder.
        blocks = candidate.payload()["content"]
        self.assertEqual(blocks[0]["signature"], "sig-abc")
        self.assertEqual(blocks[0]["thinking"], "private")
        self.assertEqual(candidate.call_ids, ("call-a",))
        # The semantic stream is untouched by capture (pass-through).
        self.assertTrue(
            any(
                isinstance(part, ToolCallIR) and part.call_id == "call-a"
                for part in semantic[-1].response.message.parts
            )
        )

    def test_thinking_without_signature_is_degraded_not_stripped(self) -> None:
        capture = _decode_captured(
            WireShape.ANTHROPIC_MESSAGES,
            [
                _anthropic_payload(
                    [
                        {"type": "thinking", "thinking": "private"},
                        {"type": "text", "text": "answer"},
                    ],
                    stop_reason="stop",
                )
            ],
        )
        list(capture)
        candidate = capture.result()
        decision = validate_candidate(candidate)

        self.assertEqual(decision.kind, ContinuationDecisionKind.DEGRADED)
        self.assertTrue(
            any(issue.code == "thinking_missing_signature" for issue in decision.issues)
        )
        # The thinking text itself is still captured, never deleted.
        blocks = candidate.payload()["content"]
        self.assertEqual(blocks[0]["thinking"], "private")

    def test_unknown_block_type_is_reported_not_ignored(self) -> None:
        capture = _decode_captured(
            WireShape.ANTHROPIC_MESSAGES,
            [
                _anthropic_payload(
                    [
                        {"type": "text", "text": "answer"},
                        {"type": "mystery_block", "data": "..."},
                    ],
                    stop_reason="stop",
                )
            ],
        )
        list(capture)
        decision = validate_candidate(capture.result())
        self.assertEqual(decision.kind, ContinuationDecisionKind.DEGRADED)
        self.assertTrue(any(issue.code == "unknown_block_type" for issue in decision.issues))


class OpenAIResponseCaptureTests(unittest.TestCase):
    def test_encrypted_reasoning_preserved_verbatim(self) -> None:
        capture = _decode_captured(
            WireShape.OPENAI_RESPONSE,
            [
                {
                    "output": [
                        {
                            "type": "reasoning",
                            "encrypted_content": "ENC-OPAQUE-BYTES",
                            "summary": [],
                        },
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ans"}],
                        },
                        {
                            "type": "function_call",
                            "call_id": "call-a",
                            "name": "lookup",
                            "arguments": "{}",
                        },
                    ],
                    "usage": {"input_tokens": 2, "output_tokens": 4},
                }
            ],
        )
        semantic = list(capture)
        candidate = capture.result()

        decision = validate_candidate(candidate)
        self.assertEqual(decision.kind, ContinuationDecisionKind.PRESERVED, decision.issues)
        items = candidate.payload()["output"]
        self.assertEqual(items[0]["encrypted_content"], "ENC-OPAQUE-BYTES")
        self.assertEqual(candidate.call_ids, ("call-a",))
        self.assertTrue(
            any(update.delta_kind == LLMResponseDeltaKind.TOOL_CALL for update in semantic)
        )

    def test_reasoning_without_encrypted_content_is_degraded(self) -> None:
        capture = _decode_captured(
            WireShape.OPENAI_RESPONSE,
            [
                {
                    "output": [
                        {"type": "reasoning", "summary": []},
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": "ans"}],
                        },
                    ],
                    "usage": {"input_tokens": 2, "output_tokens": 4},
                }
            ],
        )
        list(capture)
        decision = validate_candidate(capture.result())
        self.assertEqual(decision.kind, ContinuationDecisionKind.DEGRADED)
        self.assertTrue(
            any(
                issue.code == "reasoning_missing_encrypted_content"
                for issue in decision.issues
            )
        )


class OpenAICompletionCaptureTests(unittest.TestCase):
    def test_reasoning_content_and_tool_calls_preserved_verbatim(self) -> None:
        message = {
            "role": "assistant",
            "reasoning_content": "chain of thought",
            "content": "checking",
            "tool_calls": [
                {
                    "id": "call-a",
                    "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q": "life"}'},
                }
            ],
        }
        capture = _decode_captured(
            WireShape.OPENAI_COMPLETION,
            [
                {
                    "choices": [{"message": message, "finish_reason": "tool_calls"}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 4},
                }
            ],
        )
        list(capture)
        candidate = capture.result()

        decision = validate_candidate(candidate)
        self.assertEqual(decision.kind, ContinuationDecisionKind.PRESERVED, decision.issues)
        captured_message = candidate.payload()["message"]
        self.assertEqual(captured_message["reasoning_content"], "chain of thought")
        self.assertEqual(
            captured_message["tool_calls"][0]["function"]["arguments"],
            '{"q": "life"}',
        )
        self.assertEqual(candidate.call_ids, ("call-a",))


class DeepSeekHookSplitTests(unittest.TestCase):
    """The core P2 split: capture sits between codec and the lossy hook."""

    def _hook_context(self) -> ProviderResponseHookContext:
        return ProviderResponseHookContext(
            endpoint_id="deepseek-v4.1-flash",
            provider_id="deepseek",
            model_id="deepseek-flash",
            wire_shape=WireShape.ANTHROPIC_MESSAGES,
            request=LLMRequestIR(
                messages=(),
                tools=(),
                policy=GenerationPolicyIR(max_output_tokens=100),
            ),
        )

    def test_hook_projects_replay_none_but_capture_keeps_envelope(self) -> None:
        codec = codec_for_shape(WireShape.ANTHROPIC_MESSAGES)
        updates = codec.decode(
            _frames(
                [
                    _anthropic_payload(
                        [
                            {
                                "type": "thinking",
                                "thinking": "private",
                                "signature": "sig-keep-me",
                            },
                            {"type": "text", "text": "answer"},
                        ],
                        stop_reason="stop",
                    )
                ]
            ),
            _context(WireShape.ANTHROPIC_MESSAGES),
        )
        capture = decode_with_capture(updates, endpoint_id="demo", model_id="demo-model")

        # The DeepSeek hook consumes the CAPTURE (pre-hook position).
        projected = list(
            normalize_deepseek_updates(self._hook_context(), capture)
        )

        # Semantic stream: hook stripped the envelope on every projection...
        self.assertTrue(projected)
        for update in projected:
            self.assertIsNone(update.response.message.replay)
        # ...and the semantic reasoning still flows.
        self.assertTrue(
            any(update.delta_kind == LLMResponseDeltaKind.REASONING for update in projected)
        )

        # Native side: the capture still holds the full envelope.
        candidate = capture.result()
        self.assertIsNotNone(candidate)
        decision = validate_candidate(candidate)
        self.assertEqual(decision.kind, ContinuationDecisionKind.PRESERVED, decision.issues)
        blocks = candidate.payload()["content"]
        self.assertEqual(blocks[0]["signature"], "sig-keep-me")


class CandidateContractTests(unittest.TestCase):
    def test_shape_mismatch_is_unsupported(self) -> None:
        candidate = NativeCandidate(
            wire_shape=WireShape.OPENAI_RESPONSE,
            endpoint_id="demo",
            model_id="demo-model",
            payload_json="{}",
            call_ids=(),
        )
        from pal.llm.continuation_policy import contract_for_shape

        anthropic_contract = contract_for_shape(WireShape.ANTHROPIC_MESSAGES)
        assert anthropic_contract is not None
        decision = anthropic_contract.validate(candidate)
        self.assertEqual(decision.kind, ContinuationDecisionKind.UNSUPPORTED)

    def test_malformed_payload_is_unsupported_not_crashing(self) -> None:
        candidate = NativeCandidate(
            wire_shape=WireShape.ANTHROPIC_MESSAGES,
            endpoint_id="demo",
            model_id="demo-model",
            payload_json="[]",  # not an object
            call_ids=(),
        )
        decision = validate_candidate(candidate)
        self.assertEqual(decision.kind, ContinuationDecisionKind.UNSUPPORTED)


if __name__ == "__main__":
    unittest.main()
