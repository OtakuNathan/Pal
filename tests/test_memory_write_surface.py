"""Model memory CRUD uses the facade while backend adapters stay internal."""
import pytest

from pal.core import PalCore
from pal.memory import MemoryService, register_with_core as register_memory
from pal.memory.contracts import MemoryQuery
from pal.memory.embedding import HashingEmbedder
from pal.memory.repository import L3ProviderSelector
from pal.memory.storage import MemoryStorage
from pal.plugins.l3 import MockL3Plugin, SQLiteVecL3Plugin, register_with_core as register_l3
from pal.shared import IntrospectionCall
from pal.shared.tool_protocol import new_tool_call


INTERNAL_ALIASES = ("write_provider_memory", "recall_provider_memory",
                    "update_provider_memory", "delete_provider_memory")


@pytest.fixture(params=["mock", "sqlite_vec"])
def memory_runtime(request, tmp_path):
    core = PalCore()
    runtime = core.context.execution_runtime
    memory = MemoryService(l3_selector=L3ProviderSelector(
        resolver=runtime.l3_plugin_registry.require))
    repository = None
    try:
        register_memory(core.context, memory)
        if request.param == "sqlite_vec":
            storage = MemoryStorage(tmp_path)
            storage.create_initial()
            repository = storage.open()
            provider = SQLiteVecL3Plugin(
                service=memory, repository=repository, embedder=HashingEmbedder())
        else:
            provider = MockL3Plugin()
        register_l3(core.context, provider)
        memory.l3_selector.active_provider_id = provider.provider_id
        core.publish_module_capabilities("memory")
        core.publish_module_capabilities(provider.module_id)
        yield runtime, provider, memory
    finally:
        runtime.shutdown()
        if repository is not None:
            repository.close()


def test_model_writes_route_through_facade_and_backend_alias_is_unavailable(memory_runtime):
    runtime, provider, _ = memory_runtime
    generation = runtime.registry_generation
    assert generation.record_for_alias("write_provider_memory") is None
    assert "write_provider_memory" not in generation.search_records
    assert "write_provider_memory" not in generation.provider_specs
    args = {"kind": "fact", "summary": "Use explicit interfaces", "search_text": "explicit interfaces"}
    rejected = runtime.invoke_indirect_tool(new_tool_call(
        name="write_provider_memory", args={"name": provider.provider_id, **args}))
    assert rejected.kind == "rejected"
    assert rejected.error_code == "unknown_tool"
    assert not provider.recall(MemoryQuery(queries=["explicit interfaces"])).hits

    result = runtime.invoke_indirect_tool(new_tool_call(name="remember_memory", args=args))
    assert result.kind == "complete", result
    recalled = provider.recall(MemoryQuery(mem_ref=result.output["mem_ref"]))
    assert recalled.hits[0]["summary"] == args["summary"]


def test_backend_write_adapter_still_works_for_internal_tests(memory_runtime):
    _, provider, _ = memory_runtime
    result = provider.commit_write(IntrospectionCall(
        name="backend-test", args={"kind": "fact", "summary": "Backend test record",
                                   "search_text": "backend test record"}))
    assert result.status == "ok", result
    recalled = provider.recall(MemoryQuery(mem_ref=result.structured["mem_ref"]))
    assert recalled.hits[0]["summary"] == "Backend test record"


def test_crud_facade_and_recovery_surface(memory_runtime):
    runtime, provider, _ = memory_runtime
    generation = runtime.registry_generation
    for alias in INTERNAL_ALIASES:
        assert generation.record_for_alias(alias) is None
        assert alias not in generation.search_records
        assert alias not in generation.provider_specs
        result = runtime.invoke_indirect_tool(new_tool_call(name=alias, args={"name": provider.provider_id}))
        assert result.kind == "rejected" and result.error_code == "unknown_tool"
    for alias in ("inspect_memory_provider_inventory", "list_memory_providers", "set_active_memory_provider",
                  "attach_memory_provider", "detach_memory_provider", "refresh_memory_provider_indexes"):
        assert generation.record_for_alias(alias) is None
    result = runtime.invoke_indirect_tool(new_tool_call(name="remember_memory", args={
        "kind": "fact", "summary": "Prefer explicit interfaces", "search_text": "explicit interfaces"}))
    assert result.kind == "complete", result
    mem_ref = result.output["mem_ref"]
    updated = runtime.invoke_indirect_tool(new_tool_call(name="update_memory", args={
        "mem_ref": mem_ref, "content": "Prefer typed interfaces"}))
    assert updated.kind == "complete", updated
    mem_ref = updated.output["mem_ref"]
    recalled = runtime.invoke_direct_tool(new_tool_call(name="recall_memory", args={"mem_ref": mem_ref}))
    assert recalled.kind == "complete", recalled
    assert recalled.output["hits_preview"][0]["summary"] == "Prefer typed interfaces"
    deleted = runtime.invoke_indirect_tool(new_tool_call(name="forget_memory", args={"mem_ref": mem_ref}))
    assert deleted.kind == "complete", deleted
    assert not provider.recall(MemoryQuery(mem_ref=mem_ref)).hits


def test_reconciliation_reads_original_store_without_switch_or_fallback(memory_runtime):
    runtime, provider, memory = memory_runtime
    created = provider.commit_write(IntrospectionCall(name="backend-test", args={
        "kind": "fact", "summary": "Original store", "search_text": "original store"}))
    mem_ref = created.structured["mem_ref"]
    other = MockL3Plugin(provider_id="other_l3", records=[{
        "document_id": mem_ref, "document_kind": "fact", "summary": "Different store"}])
    # The registry resolver is the same one used by the production facade.
    runtime.l3_plugin_registry.register(other)
    memory.l3_selector.active_provider_id = other.provider_id
    recalled = runtime.invoke_direct_tool(new_tool_call(name="recall_memory", args={
        "provider_id": provider.provider_id, "mem_ref": mem_ref}))
    assert recalled.kind == "complete", recalled
    assert recalled.output["provider_id"] == provider.provider_id
    assert recalled.output["hits_preview"][0]["summary"] == "Original store"
    assert memory.l3_selector.active_provider_id == other.provider_id
    active = runtime.invoke_direct_tool(new_tool_call(name="recall_memory", args={"mem_ref": mem_ref}))
    assert active.output["hits_preview"][0]["summary"] == "Different store"
    missing = runtime.invoke_direct_tool(new_tool_call(name="recall_memory", args={
        "provider_id": "unknown-provider", "mem_ref": mem_ref}))
    assert missing.kind != "complete", missing
    assert memory.l3_selector.active_provider_id == other.provider_id


def test_backend_crud_adapters_still_work_for_internal_tests(memory_runtime):
    _, provider, _ = memory_runtime
    created = provider.commit_write(IntrospectionCall(name="backend-test", args={
        "kind": "fact", "summary": "Backend record", "search_text": "backend record"}))
    mem_ref = created.structured["mem_ref"]
    updated = provider.correct_patch(IntrospectionCall(name="backend-test", args={
        "mem_ref": mem_ref, "summary": "Corrected backend record"}))
    assert updated.status == "ok", updated
    mem_ref = updated.structured["mem_ref"]
    recalled = provider.recall_query(IntrospectionCall(name="backend-test", args={"mem_ref": mem_ref}))
    assert recalled.structured["hits_preview"][0]["summary"] == "Corrected backend record"
    deleted = provider.delete_memory(IntrospectionCall(name="backend-test", args={"mem_ref": mem_ref}))
    assert deleted.status == "ok", deleted
    assert not provider.recall(MemoryQuery(mem_ref=mem_ref)).hits


@pytest.mark.parametrize("asynchronous", [False, True])
def test_native_memory_schema_routes_with_original_semantics_and_gates(memory_runtime, asynchronous):
    import asyncio
    from pal.execution.tool_facade import InvocationMode, RetryPolicy
    runtime, provider, _ = memory_runtime
    generation = runtime.registry_generation
    for alias in ("recall_memory", "remember_memory", "update_memory", "forget_memory"):
        assert alias in generation.provider_specs
    record = generation.indirect_aliases["remember_memory"]
    assert record.execution.invocation_mode is InvocationMode.INDIRECT
    assert record.execution.retry_policy is RetryPolicy.RECONCILE_FIRST
    args = {"kind": "fact", "summary": "Native memory", "search_text": "Native memory"}
    call = new_tool_call(name="remember_memory", args=args)
    assert runtime.invoke_direct_tool(call).error_code == "wrong_invocation_mode"
    def invoke(call, **kwargs):
        if asynchronous:
            return asyncio.run(runtime.invoke_model_tool_async(call, **kwargs))
        return runtime.invoke_model_tool(call, **kwargs)
    assert invoke(call, allow_tools=False).error_code == "finalization_only"
    assert not provider.recall(MemoryQuery(queries=["Native memory"])).hits
    result = invoke(call)
    assert result.kind == "complete", result
    assert provider.recall(MemoryQuery(mem_ref=result.output["mem_ref"])).hits
    # Explicit opt-ins must not enable guessed, deferred tools.
    assert invoke(new_tool_call(name="attach_memory_provider", args={"name": provider.provider_id})).error_code == "unknown_tool"
    executed = (asyncio.run(runtime.execute_tool_async(call, allow_tools=False)) if asynchronous
                else runtime.execute_tool(call, allow_tools=False))
    assert not executed.ok and executed.structured["error_code"] == "finalization_only"


def test_content_correction_cannot_accept_partial_or_conflicting_fields(memory_runtime, tmp_path):
    runtime, provider, _ = memory_runtime
    created = runtime.invoke_model_tool(new_tool_call(name="remember_memory", args={
        "kind": "fact", "summary": "喜欢咖啡", "search_text": "喜欢咖啡"}))
    ref = created.output["mem_ref"]
    for args in ({"summary": "喜欢红茶"}, {"search_text": "喜欢红茶"},
                 {"content": " "}, {"content": "喜欢红茶", "summary": "喜欢咖啡"}):
        result = runtime.invoke_model_tool(new_tool_call(name="update_memory", args={"mem_ref": ref, **args}))
        assert result.error_code == "invalid_arguments"
        assert provider.recall(MemoryQuery(mem_ref=ref)).hits[0]["summary"] == "喜欢咖啡"
    corrected = runtime.invoke_model_tool(new_tool_call(name="update_memory", args={
        "mem_ref": ref, "content": "喜欢红茶"}))
    assert corrected.kind == "complete", corrected
    hit = provider.recall(MemoryQuery(mem_ref=corrected.output["mem_ref"], view="origin")).hits[0]
    assert hit["summary"] == hit["search_text"] == "喜欢红茶"

    if isinstance(provider, SQLiteVecL3Plugin):
        old = provider.recall(MemoryQuery(mem_ref=ref))
        assert old.metadata["lookup_status"] == "superseded"
        assert old.metadata["successors"] == [corrected.output["mem_ref"]]
        history = MemoryStorage(tmp_path).history(ref, repo=provider.repository)
        assert history[0]["document"]["summary"] == "喜欢咖啡"


@pytest.mark.parametrize("star", ["{}", {"situation": "only one field"}])
def test_native_case_contract_and_recovery_keep_nested_object_constraints(memory_runtime, star):
    from jsonschema import Draft202012Validator
    runtime, provider, _ = memory_runtime
    generation = runtime.registry_generation
    schema = generation.provider_specs["remember_memory"]["function"]["input_schema"]
    assert schema["properties"]["star"]["type"] == "object"
    assert set(schema["properties"]["star"]["required"]) == {"situation", "task", "action", "result"}
    args = {"kind": "case", "summary": "Plugin reload", "search_text": "reload_plugin", "star": star}
    assert not Draft202012Validator(schema).is_valid(args)
    invalid = runtime.invoke_model_tool(new_tool_call(name="remember_memory", args=args))
    assert invalid.error_code == "invalid_arguments"
    assert invalid.effect == "not_started"
    assert invalid.details["input_schema"] == schema
    assert "JSON objects must not be stringified" in invalid.llm_text
    assert not provider.recall(MemoryQuery(queries=["reload_plugin"])).hits
    args["star"] = {"situation": "Old plugin behavior", "task": "Load code", "action": "reload_plugin", "result": "Verified"}
    assert Draft202012Validator(schema).is_valid(args)
    saved = runtime.invoke_model_tool(new_tool_call(name="remember_memory", args=args))
    assert saved.kind == "complete", saved
    hit = provider.recall(MemoryQuery(mem_ref=saved.output["mem_ref"], view="origin")).hits[0]
    assert hit["payload"]["action"] == "reload_plugin"


@pytest.mark.parametrize("name", ["", "   "])
def test_empty_indirect_name_rejects_before_dispatch(name):
    from pal.execution.capabilities import register_with_core as register_execution
    core = PalCore()
    runtime = core.context.execution_runtime
    try:
        register_execution(core.context)
        core.publish_module_capabilities("execution")
        result = runtime.invoke_model_tool(new_tool_call(name="call_tool", args={"name": name, "args": {}}))
        assert result.kind == "rejected"
        assert result.error_code == "invalid_arguments"
        assert result.effect == "not_started"
    finally:
        runtime.shutdown()


def test_hub_health_keeps_counts_without_backend_network_probe(memory_runtime, monkeypatch):
    runtime, provider, _ = memory_runtime
    monkeypatch.setattr(provider, 'inspect', lambda: (_ for _ in ()).throw(AssertionError('full backend probe forbidden')))
    if provider.provider_id == 'mock_l3':
        # The fixture's passive stub delegates locally; replace it with an
        # explicit local summary to make a full inspect call observable here.
        monkeypatch.setattr(provider, 'inspect_summary', lambda: {'record_count': len(provider.records)})
    result = runtime.invoke_indirect_tool(new_tool_call(name='inspect_memory_state', args={}))
    assert result.kind == 'complete'
    state = result.output['active_provider']
    assert state['mounted']
    assert 'counts' in state and 'inspection_error' not in state
    assert not {'embedding_identity', 'generation_id', 'embedding_transport', 'vector_backend_detail'}.intersection(state)
