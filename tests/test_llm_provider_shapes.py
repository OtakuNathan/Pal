from __future__ import annotations

import unittest

from pal.llm.llm_adaptor.anthropic_api import chat_messages_to_anthropic_messages
from pal.llm.llm_adaptor.base import chat_messages_to_openai_compatible_messages
from pal.llm.llm_adaptor.openai_responses import chat_messages_to_responses_input


def _tool_protocol_messages() -> list[dict[str, object]]:
    return [
        {"role": "user", "content": "run probe"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "hidden top-level reasoning",
            "provider_specific_fields": {"reasoning_content": "hidden provider reasoning"},
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "probe", "arguments": '{"ok": true}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "probe result"},
    ]


class LLMProviderShapeTests(unittest.TestCase):
    def test_openai_chat_preserves_standard_tool_protocol_shape(self) -> None:
        rendered = chat_messages_to_openai_compatible_messages(_tool_protocol_messages())

        self.assertEqual([message["role"] for message in rendered], ["user", "assistant", "tool"])
        assistant = rendered[1]
        tool_result = rendered[2]
        self.assertEqual(assistant["tool_calls"][0]["id"], "call_1")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "probe")
        self.assertEqual(assistant["tool_calls"][0]["function"]["arguments"], '{"ok": true}')
        self.assertEqual(tool_result["tool_call_id"], "call_1")
        self.assertEqual(tool_result["content"], "probe result")
        self.assertNotIn("provider_specific_fields", assistant)
        self.assertNotIn("reasoning_content", assistant)

    def test_openai_responses_maps_tool_protocol_to_responses_items(self) -> None:
        _, items = chat_messages_to_responses_input(_tool_protocol_messages())

        self.assertIn({"role": "user", "content": "run probe"}, items)
        self.assertIn(
            {
                "type": "function_call",
                "name": "probe",
                "arguments": '{"ok": true}',
                "call_id": "call_1",
            },
            items,
        )
        self.assertIn(
            {"type": "function_call_output", "call_id": "call_1", "output": "probe result"},
            items,
        )
        self.assertFalse(any(item.get("role") == "tool" for item in items))

    def test_anthropic_messages_maps_tool_protocol_to_content_blocks(self) -> None:
        system, messages = chat_messages_to_anthropic_messages(
            [{"role": "system", "content": "rules"}, *_tool_protocol_messages()]
        )

        self.assertEqual(system, "rules")
        self.assertEqual([message["role"] for message in messages], ["user", "assistant", "user"])
        self.assertEqual(
            messages[1]["content"],
            [{"type": "tool_use", "id": "call_1", "name": "probe", "input": {"ok": True}}],
        )
        self.assertEqual(
            messages[2]["content"],
            [{"type": "tool_result", "tool_use_id": "call_1", "content": "probe result"}],
        )


if __name__ == "__main__":
    unittest.main()
