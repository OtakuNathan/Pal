"""Model-visible recovery must preserve facts without duplicating protocol bodies."""
import json

import pytest

from pal.mcp.normalize import normalize_prompt_result, normalize_protocol_error, normalize_tool_result
from pal.memory.contracts import L3MutationResult, L3RecallResult, MemoryQuery
from pal.memory.rendering import render_mutation_result_for_llm, render_recall_result_for_llm


@pytest.mark.parametrize("is_error", [False, True])
def test_mcp_body_is_displayed_once_and_raw_protocol_is_preserved(is_error):
    raw = {"isError": is_error, "content": [
        {"type": "text", "text": "unique tool body"},
        {"type": "resource_link", "uri": "file:///result", "name": "result"},
    ], "structuredContent": {"count": 3}, "_meta": {"cursor": "next"}}
    result = normalize_tool_result(raw, server_id="demo", tool_name="inspect")
    assert result.llm_text.count("unique tool body") == 1
    assert result.structured["raw_result"] == raw
    visible = json.loads(result.llm_text.split("\n", 1)[1])
    assert visible["raw_result"] == raw
    if is_error:
        assert "read_tool" in result.llm_text
        assert "read_mcp_server" in result.llm_text


def test_mcp_prompt_and_protocol_error_do_not_duplicate_content():
    raw = {"description": "unique description", "messages": [
        {"role": "user", "content": {"type": "text", "text": "unique prompt body"}},
    ]}
    result = normalize_prompt_result(raw, server_id="demo", prompt_name="review")
    assert result.llm_text.count("unique prompt body") == 1
    assert result.llm_text.count("unique description") == 1
    assert result.structured["raw_result"] == raw
    error = normalize_protocol_error(ValueError("unique error body"), server_id="demo", name="inspect", kind="tool")
    assert error.llm_text.count("unique error body") == 1
    assert "read_mcp_server" in error.llm_text


def test_memory_conflict_exposes_existing_recovery_information():
    result = L3MutationResult(status="conflict", document_id="fact:old", metadata={
        "reason": "replaced", "successors": ["fact:new"],
    })
    text = render_mutation_result_for_llm("update", result)
    assert "status: conflict" in text
    assert "reason: replaced" in text
    assert "successors: fact:new" in text


def test_degraded_empty_recall_is_not_reported_as_definitive_absence():
    text = render_recall_result_for_llm(provider_id="memory", query=MemoryQuery(queries=["test"]),
        result=L3RecallResult(metadata={"degraded": True, "degraded_reason": "embedding unavailable"}), view="summary")
    assert "Recall degraded: embedding unavailable" in text
    assert "No matching memories found." not in text
