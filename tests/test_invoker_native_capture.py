"""Invoker native capture remains independent of projection persistence."""
import json
import unittest
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR


def _user(text):
    return LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR(text),))


class InvokerNativeCaptureTests(unittest.TestCase):
    def test_attempt_result_carries_native_payload(self) -> None:
        from pal.llm.endpoint import ShapeEndpointInvoker
        from pal.llm.models import LLMEndpointModel
        from pal.llm.ir import GenerationPolicyIR, LLMRequestIR

        frames = [
            {
                "content": [
                    {"type": "thinking", "thinking": "private", "signature": "sig-1"},
                    {"type": "text", "text": "answer"},
                ],
                "stop_reason": "stop",
                "usage": {"input_tokens": 3, "output_tokens": 5},
            }
        ]

        class _FramesTransport:
            def frames(self, request):
                for index, payload in enumerate(frames):
                    from pal.llm.shapes.base import _JSONFrame

                    yield _JSONFrame(index, payload)

            def activate_endpoint(self, endpoint_id: str) -> None:
                pass

            def close(self) -> None:
                pass

        attempts: list = []
        invoker = ShapeEndpointInvoker(
            credential_resolver=lambda endpoint: "secret",
            transport=_FramesTransport(),  # type: ignore[arg-type]
            attempt_sink=attempts.append,
        )
        endpoint = LLMEndpointModel(
            endpoint_id="anthropic-1",
            provider="anthropic",
            model_id="claude-x",
            base_url="",
            auth_kind="api_key_ref",
            credential_ref="key",
            context_window=10_000,
            max_output_tokens=1_000,
            thinking_levels_blob=[],
            default_thinking_level="",
            supports_tools=True,
            supports_streaming=False,
            supports_vision=False,
            input_modalities_blob=["text"],
            output_modalities_blob=["text"],
            priority=0,
            enabled=True,
            capabilities_blob={},
            wire_shape="anthropic_messages",
        )
        request = LLMRequestIR(
            messages=(_user("hello"),),
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=64),
        )
        response, _updates = invoker.invoke(endpoint, request)

        self.assertEqual(len(attempts), 1)
        native_json = attempts[0].native_payload_json
        self.assertTrue(native_json)
        payload = json.loads(native_json)
        self.assertEqual(payload["content"][0]["signature"], "sig-1")
        # Semantics unaffected by the capture wiring.
        self.assertEqual(response.text, "answer")


if __name__ == "__main__":
    unittest.main()
