"""Memory bodies reach the shared pager intact, through both recall entrypoints."""
import json

import pytest

from pal.core.runtime import PalCore
from pal.execution.contracts import ToolCallBudget
from pal.memory import MemoryService, register_with_core as register_memory
from pal.memory.repository import L3ProviderSelector
from pal.plugins.l3 import MockL3Plugin, register_with_core as register_l3
from pal.shared.tool_protocol import new_tool_call


@pytest.mark.parametrize("view", ["summary", "origin"])
@pytest.mark.parametrize("alias", ["recall_memory", "memory_provider_recall"])
def test_long_memory_can_be_read_completely_across_pages(view, alias):
    core = PalCore()
    runtime = core.context.execution_runtime
    memory = MemoryService(l3_selector=L3ProviderSelector(
        resolver=runtime.l3_plugin_registry.require))
    body = "  pagination 原始记忆\n" + "    保留缩进与内容。\n" * 500 + "完整结尾\n  "
    record = {
        "document_id": "fact:paging", "scope": "system", "title": "pagination",
        "summary": body if view == "summary" else "pagination short summary",
        "search_text": body if view == "origin" else "pagination short original",
    }
    try:
        register_memory(core.context, memory)
        provider = MockL3Plugin(records=[record])
        register_l3(core.context, provider)
        memory.l3_selector.active_provider_id = provider.provider_id
        core.publish_module_capabilities("memory")
        core.publish_module_capabilities(provider.module_id)
        runtime.begin_tool_result_turn(turn_id="read-memory", scope_key="paging-test")
        invoke = runtime.invoke_direct_tool if alias == "recall_memory" else runtime.invoke_indirect_tool
        args = {"queries": ["pagination"], "view": view}
        if alias == "memory_provider_recall":
            args["name"] = provider.provider_id
        result = invoke(
            new_tool_call(name=alias, args=args,
                          call_id="memory-result"),
            budget=ToolCallBudget(max_output_chars=500, preview_chars=300),
            turn_id="read-memory",
        )
        assert result.kind == "paged", result
        assert any(hint.tool == "read_tool_result" for hint in result.affordances)
        pages = [
            runtime.read_tool_result_page(
                result_ref="memory-result", page=number, turn_id="read-memory")
            for number in range(1, result.result_handle["page_count"] + 1)
        ]
        restored = "".join(page.content for page in pages)
        assert f"[fact:paging]: {body}\n</recalled_memories>" in restored
        assert len(restored) == result.result_handle["original_size"]
        handle = runtime.logical_state.read_pager(
            execution_lifetime_id="paging-test", result_ref="memory-result",
            page=1, page_size=None, anchor="head").manifest
        payload = json.loads(handle.output_json)
        key = "summary" if view == "summary" else "search_text"
        assert payload["hits_preview"][0][key] == body
    finally:
        runtime.shutdown()
