"""MCP prompts reach the model through the complete execution facade."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from pal.execution.tool_facade import EffectOutcome
from pal.mcp.compiler import McpCompiler
from pal.mcp.model import McpDiscoverySnapshot, McpPromptSpec
from pal.mcp.normalize import normalize_prompt_result
from tests.test_tool_failure_affordances import runtime, invoke, metadata


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("messages", [
    [],
    [{"role": "user", "content": {"type": "text", "text": "  complete prompt\n\n"}}],
    [{"role": "assistant", "content": {"type": "image", "mimeType": "image/png", "data":
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD1sAAAAASUVORK5CYII="}}],
])
def test_prompt_call_tool_preserves_messages_and_read_receipt(runtime, asynchronous, messages):
    payload = {"description": "complete prompt description", "messages": messages}

    class Invoker:
        def render_prompt(self, server_id, prompt_name, arguments):
            return payload

    snapshot = McpDiscoverySnapshot(server_id="review", transport="stdio",
        prompts=(McpPromptSpec(name="code_review"),)).with_hash()
    projection = McpCompiler().compile(module_id="mcp_review", snapshots=(snapshot,), invoker=Invoker())
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=projection.mounted_subtree))

    result = invoke(runtime, "render_mcp_review_code_review", {}, asynchronous=asynchronous)

    assert result.ok, result.llm_text
    assert result.structured["messages"] == messages
    assert result.structured["description"] == payload["description"]
    assert result.structured["raw_result"] == payload
    assert result.invocation_result.effect is EffectOutcome.NONE
    assert result.structured["unsupported_content_types"] == (["image"] if messages and
        messages[0]["content"]["type"] == "image" else [])
    assert "complete prompt description" in result.llm_text
    if messages and messages[0]["content"]["type"] == "text":
        assert "  complete prompt\\n\\n" in result.llm_text


def test_prompt_normalizer_supplies_read_receipt():
    result = normalize_prompt_result({"messages": []}, server_id="review", prompt_name="example")
    assert result.effect_receipt is not None
    assert result.effect_receipt.outcome is EffectOutcome.NONE


@pytest.mark.parametrize("asynchronous", [False, True])
def test_prompt_protocol_failure_preserves_cause_and_unknown_effect(runtime, asynchronous):
    class Invoker:
        def render_prompt(self, server_id, prompt_name, arguments):
            try:
                raise OSError("underlying prompt connection failure")
            except OSError as cause:
                raise RuntimeError("prompt retrieval failed") from cause

    snapshot = McpDiscoverySnapshot(server_id="review", transport="stdio",
        prompts=(McpPromptSpec(name="code_review"),)).with_hash()
    projection = McpCompiler().compile(module_id="mcp_review", snapshots=(snapshot,), invoker=Invoker())
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=projection.mounted_subtree))

    result = invoke(runtime, "render_mcp_review_code_review", {}, asynchronous=asynchronous)

    assert not result.ok
    assert "underlying prompt connection failure" in result.llm_text
    assert "prompt retrieval failed" in result.llm_text
    assert metadata(result)["effect"] == "unknown"
    assert metadata(result)["error_code"] == "mcp_protocol_error"
