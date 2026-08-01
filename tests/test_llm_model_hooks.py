from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pal.llm.ir import (
    GenerationPolicyIR,
    LLMMessageIR,
    LLMRequestIR,
    MessageRole,
    TextPartIR,
)
from pal.llm.model_hooks import ModelHookError, ModelHookRegistry
from pal.shared.tool_protocol import ToolDefinitionIR


class LLMModelHookTests(unittest.TestCase):
    def test_runtime_root_exact_model_hook_is_the_only_match_key(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal-model-hook-"))
        hook_root = root / "llm" / "models"
        hook_root.mkdir(parents=True)
        (hook_root / "demo.py").write_text(
            """
MODEL_ID = "exact-model"
DEVELOPER_INSTRUCTIONS = ("Use compact tool calls.",)
def adjust_messages(messages):
    return messages
def adjust_tools(tools):
    return tools
""".strip(),
            encoding="utf-8",
        )
        registry = ModelHookRegistry.load(root)
        request = LLMRequestIR(
            messages=(LLMMessageIR(MessageRole.USER, (TextPartIR("hello"),)),),
            tools=(),
            policy=GenerationPolicyIR(max_output_tokens=100),
        )
        untouched = registry.apply("almost-exact-model", request)
        self.assertIs(untouched, request)
        adjusted = registry.apply("exact-model", request)
        self.assertEqual(adjusted.policy.max_output_tokens, 100)
        self.assertEqual(adjusted.messages[0].role, MessageRole.DEVELOPER)

    def test_hook_may_only_replace_messages_and_tools(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal-model-hook-"))
        hook_root = root / "llm" / "models"
        hook_root.mkdir(parents=True)
        (hook_root / "demo.py").write_text(
            """
from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
from pal.shared.tool_protocol import ToolDefinitionIR
MODEL_ID = "exact-model"
def adjust_messages(messages):
    return (*messages, LLMMessageIR(MessageRole.USER, (TextPartIR("hooked"),)))
def adjust_tools(tools):
    return (*tools, ToolDefinitionIR(name="hook_tool", description="hook", input_schema={"type": "object"}))
""".strip(),
            encoding="utf-8",
        )
        request = LLMRequestIR(
            messages=(LLMMessageIR(MessageRole.USER, (TextPartIR("hello"),)),),
            tools=(ToolDefinitionIR(name="base", description="base", input_schema={"type": "object"}),),
            policy=GenerationPolicyIR(max_output_tokens=100),
            model_hint="preserved-model",
            metadata={"routing": "preserved"},
        )

        adjusted = ModelHookRegistry.load(root).apply("exact-model", request)

        self.assertEqual([message.text for message in adjusted.messages], ["hello", "hooked"])
        self.assertEqual([tool.name for tool in adjusted.tools], ["base", "hook_tool"])
        self.assertIs(adjusted.policy, request.policy)
        self.assertEqual(adjusted.model_hint, "preserved-model")
        self.assertEqual(dict(adjusted.metadata), {"routing": "preserved"})

    def test_hook_cannot_change_routing_or_credentials(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="pal-model-hook-"))
        hook_root = root / "llm" / "models"
        hook_root.mkdir(parents=True)
        (hook_root / "bad.py").write_text(
            """
MODEL_ID = "bad"
def adjust_generation_policy(policy):
    return {"provider": "other"}
""".strip(),
            encoding="utf-8",
        )
        with self.assertRaises(ModelHookError):
            ModelHookRegistry.load(root)

if __name__ == "__main__":
    unittest.main()
