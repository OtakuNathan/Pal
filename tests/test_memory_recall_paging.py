"""Memory bodies reach the output snapshots intact, through both recall entrypoints."""
from pathlib import Path

import pytest

from pal.core.runtime import PalCore
from pal.execution.contracts import ToolCallBudget
from pal.memory import MemoryService, register_with_core as register_memory
from pal.memory.repository import L3ProviderSelector
from pal.plugins.l3 import MockL3Plugin, register_with_core as register_l3
from pal.shared.tool_protocol import new_tool_call


@pytest.mark.parametrize("view", ["summary", "origin"])
@pytest.mark.parametrize("alias", ["recall_memory", "memory_provider_recall"])
def test_long_memory_can_be_read_completely_from_snapshot(view, alias):
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
        assert result.kind == "complete", result
        assert len(result.snapshot_refs) == 1
        restored = Path(result.snapshot_refs[0].path).read_text(encoding="utf-8")
        assert f"[fact:paging]: {body}\n</recalled_memories>" in restored
        from pal.memory.contracts import MemoryPackRequest
        from pal.memory.prompt import MemoryPromptFragmentProvider
        from pal.shared import PromptAssemblyContext
        pack = memory.build_pack(MemoryPackRequest(turn_kind="chat"))
        for turn_id in ("read-memory", "read-memory", "next-turn"):
            fragments = MemoryPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext(
                metadata={"memory_pack": pack, "typed_l1_projection": True, "turn_id": turn_id}))
            assert not any("recalled_memories" in fragment.content for fragment in fragments)
        payload = result.output
        key = "summary" if view == "summary" else "search_text"
        assert payload["hits_preview"][0][key] == body
    finally:
        runtime.shutdown()


@pytest.mark.parametrize("alias", ["recall_memory", "memory_provider_recall"])
def test_all_selected_hits_remain_available_in_snapshot(alias):
    core = PalCore()
    runtime = core.context.execution_runtime
    memory = MemoryService(l3_selector=L3ProviderSelector(resolver=runtime.l3_plugin_registry.require))
    records = [{"document_id": f"fact:item-{index}", "scope": "system", "title": "many hits",
                "summary": f"record-{index}: " + "complete body " * 40} for index in range(6)]
    try:
        register_memory(core.context, memory)
        provider = MockL3Plugin(records=records)
        register_l3(core.context, provider)
        memory.l3_selector.active_provider_id = provider.provider_id
        core.publish_module_capabilities("memory")
        core.publish_module_capabilities(provider.module_id)
        runtime.begin_tool_result_turn(turn_id="many", scope_key="many")
        args = {"queries": ["many hits"], "limit": 6}
        if alias == "memory_provider_recall":
            args["name"] = provider.provider_id
        invoke = runtime.invoke_direct_tool if alias == "recall_memory" else runtime.invoke_indirect_tool
        result = invoke(new_tool_call(name=alias, args=args, call_id="many-result"),
                        budget=ToolCallBudget(max_output_chars=500, preview_chars=300), turn_id="many")
        assert result.kind == "complete"
        content = Path(result.snapshot_refs[0].path).read_text(encoding="utf-8")
        for record in records:
            assert f"[{record['document_id']}]: {record['summary']}" in content
        payload = result.output
        assert payload["hit_count"] == len(payload["hits_preview"]) == 6
    finally:
        runtime.shutdown()
