from __future__ import annotations

import json
from typing import Literal

import pytest
from jsonschema import Draft202012Validator
from pydantic import Field

from pal.core import PalCore
from pal.execution import register_with_core
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput, StrictToolModel, ToolGuidance
from pal.execution.tool_presentation import compact_input_contract
from pal.shared.tool_protocol import new_tool_call
from tests.capability_fixture import mount_test_capability


@pytest.fixture
def runtime():
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities("execution")
    yield core.context.execution_runtime
    core.context.execution_runtime.shutdown()


def test_word_search_distinguishes_install_and_uninstall_and_splits_alias(runtime):
    for alias, purpose in (("install_package", "Install package dependencies"), ("uninstall_plugin", "Uninstall plugin")):
        mount_test_capability(runtime, alias=alias, canonical_path=f"op_test_{alias}",
                              InputModel=EmptyToolInput, OutputModel=EmptyToolOutput,
                              handler=lambda _: {}, guidance=ToolGuidance(
                                  purpose=purpose, use_when=purpose, do_not_use_when="unrelated task"))
    for query in ("install", "install dependencies", "install_pack", "install_package"):
        result = runtime.execute_tool(new_tool_call(name="search_tools", args={"query": query}))
        assert result.ok
        aliases = [hit["alias"] for hit in result.structured["hits"]]
        assert aliases[0] == "install_package"
        assert "uninstall_plugin" not in aliases
    result = runtime.execute_tool(new_tool_call(name="search_tools", args={}))
    assert result.structured["top_k"] == 3
    assert len(result.structured["hits"]) == 3


def test_discovery_exposes_plugin_reattach_and_provider_install():
    from types import SimpleNamespace
    from pal.plugins.capabilities import register_with_core as register_plugins

    calls = []
    def reattach(name):
        calls.append(name)
        return {"status": "ok", "plugin_id": name, "enabled": True, "attached": True}

    core = PalCore()
    register_with_core(core.context)
    register_plugins(core.context, SimpleNamespace(reattach=reattach))
    for module in ("execution", "plugins"):
        core.publish_module_capabilities(module)
    runtime = core.context.execution_runtime
    try:
        for query, expected in (("reload plugin", "reload_plugin"), ("install provider", "install_package")):
            result = runtime.execute_tool(new_tool_call(name="search_tools", args={"query": query}))
            hit = result.structured["hits"][0]
            assert hit["alias"] == expected
            assert hit["input_contract"]
        result = runtime.execute_tool(new_tool_call(name="call_tool", args={
            "name": "reload_plugin", "args": {"name": "lsp"},
        }))
        assert result.ok
        assert calls == ["lsp"]
    finally:
        runtime.shutdown()


class _Target(StrictToolModel):
    title: Literal["primary", "secondary"]
    count: int = Field(1, ge=1, le=3, description="Number of samples, inclusive range 1–3.")


class _InspectInput(StrictToolModel):
    target: _Target


def test_search_hit_supports_first_call_without_read_tool(runtime):
    seen = []
    mount_test_capability(runtime, alias="inspect_samples", canonical_path="op_test_inspect_samples",
                          InputModel=_InspectInput, OutputModel=EmptyToolOutput,
                          handler=lambda value: seen.append(value.model_dump()) or {},
                          examples=({"target": {"title": "primary"}},),
                          guidance=ToolGuidance(purpose="Inspect samples", use_when="Target is known; preparation is internal.",
                                                do_not_use_when="No target is known."))
    result = runtime.execute_tool(new_tool_call(name="search_tools", args={"query": "inspect samples"}))
    hit = json.loads(result.llm_text)["hits"][0]
    assert hit["alias"] == "inspect_samples"
    assert hit["invocation_mode"] == "indirect"
    assert "preparation is internal" in hit["use_when"]
    assert hit["do_not_use_when"] == "No target is known."
    assert hit["execution"]["retry_policy"] == "automatic"
    assert "input_shape" not in hit and "search_text" not in hit
    assert "input_shape" in result.structured["hits"][0]
    schema = hit["input_contract"]
    nested = schema["$defs"]["_Target"]
    arguments = {"target": {"title": nested["properties"]["title"]["enum"][0],
                            "count": nested["properties"]["count"]["default"]}}
    Draft202012Validator(schema).validate(arguments)
    called = runtime.execute_tool(new_tool_call(name="call_tool", args={"name": hit["alias"], "args": arguments}))
    assert called.ok
    assert seen == [arguments]
    invalid = {"target": {"title": "primary", "count": 4}}
    assert not Draft202012Validator(schema).is_valid(invalid)
    rejected = runtime.execute_tool(new_tool_call(name="call_tool", args={"name": hit["alias"], "args": invalid}))
    assert not rejected.ok
    assert seen == [arguments]


def test_compaction_preserves_data_named_title_and_mcp_validation_rules():
    from tests.test_mcp_tool_schema_validation import MCP_SCHEMA

    schema = {**MCP_SCHEMA, "title": "Root", "default": {"title": "keep"}}
    compact = compact_input_contract(schema)
    assert "title" not in compact
    assert compact["default"] == {"title": "keep"}
    assert compact["$defs"] == schema["$defs"]
    for value in (
        {"kind": "job", "mode": "fast", "config": {"count": 1, "tags": ["a"]}, "choice": "ok"},
        {"kind": "job", "mode": "wrong", "config": {"count": 1, "tags": ["a"]}, "choice": "ok"},
    ):
        assert Draft202012Validator(compact).is_valid(value) == Draft202012Validator(schema).is_valid(value)
    data = {"type": "object", "properties": {"title": {"type": "object", "default": {"title": "data"}}}}
    assert compact_input_contract(data) == data


def test_task_words_handle_plural_without_promoting_next_tool_hints(runtime):
    from pal.execution.discovery_terms import tool_search_terms

    assert tool_search_terms("find controls") == ("find", "control")
    assert tool_search_terms("prepare dependencies") == ("prepare", "dependency")
    assert "install" not in tool_search_terms("uninstall")
    mount_test_capability(runtime, alias="find_control", canonical_path="op_test_find_control",
                          InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=lambda _: {},
                          guidance=ToolGuidance(purpose="Find a control", use_when="Locating controls",
                                                do_not_use_when="No controls needed"))
    assert runtime.execute_tool(new_tool_call(name="search_tools", args={"query": "find controls"})).structured["hits"][0]["alias"] == "find_control"


def test_specific_task_words_rank_above_partial_matches(runtime):
    result = runtime.execute_tool(new_tool_call(name="search_tools", args={"query": "read file", "top_k": 100}))
    aliases = [hit["alias"] for hit in result.structured["hits"]]
    assert aliases[0] == "read_file"
    assert aliases.index("read_tool") > aliases.index("read_file")


def test_exact_search_does_not_report_weaker_matches_as_truncation(runtime):
    precise = runtime.execute_tool(new_tool_call(name="search_tools", args={"query": "read file", "facets": True}))
    assert precise.ok
    assert [hit['alias'] for hit in precise.structured['hits']] == ['read_file']
    assert precise.structured['omitted_weaker_count'] > 0
    assert precise.structured['truncated'] is False
    assert 'usage_hint' not in precise.structured
    broad = runtime.execute_tool(new_tool_call(name="search_tools", args={"query": "read file", "top_k": 1}))
    assert broad.structured['truncated'] is True
    assert broad.structured['omitted_weaker_count'] == 0


def test_find_symbols_keeps_symbol_tools_despite_other_full_matches(tmp_path):
    from pal.lsp import build_lsp_plugin

    core = PalCore()
    register_with_core(core.context)
    build_lsp_plugin(runtime_root=tmp_path).register_with_core(core.context)
    for module in ("execution", "lsp"):
        core.publish_module_capabilities(module)
    runtime = core.context.execution_runtime
    try:
        result = runtime.execute_tool(new_tool_call(name="search_tools", args={"query": "find symbols", "top_k": 10}))
        aliases = {hit["alias"] for hit in result.structured["hits"]}
        assert {"list_lsp_document_symbols", "search_lsp_workspace_symbols"} <= aliases
    finally:
        runtime.shutdown()


def test_generated_examples_remain_validated_but_are_hidden_from_model(runtime):
    # Real built-in hydration generates a schema-valid but useless no-op edit.
    record = runtime.registry_generation.record_for_alias('edit_file')
    assert record.binding.descriptor.examples
    for example in record.binding.descriptor.examples:
        record.binding.descriptor.InputModel.model_validate(example, strict=True)
    assert record.example is None
    assert 'Valid example:' not in record.compiled_description
    result = runtime.execute_tool(new_tool_call(name='read_tool', args={'name': 'edit_file'}))
    assert result.ok
    assert 'Valid example:' not in result.llm_text
    assert result.structured.get('example') is None


def test_authored_examples_are_still_visible(runtime):
    mount_test_capability(runtime, alias='inspect_samples', canonical_path='op_test_inspect_samples',
                          InputModel=_InspectInput, OutputModel=EmptyToolOutput,
                          handler=lambda _: {}, examples=({'target': {'title': 'primary'}},))
    record = runtime.registry_generation.record_for_alias('inspect_samples')
    assert record.example == {'target': {'title': 'primary'}}
    result = runtime.execute_tool(new_tool_call(name='read_tool', args={'name': 'inspect_samples'}))
    assert result.ok
    assert 'Valid example:' in result.llm_text
    assert 'primary' in result.llm_text


def test_alias_words_converge_without_description_contamination(runtime):
    for alias, purpose, when in (
        ('remember_memory', 'Store a durable fact.', 'Saving a preference.'),
        ('inspect_active_memory_provider', 'Show the active memory provider.', 'Remember memory before changing providers.'),
        ('unrelated_helper', 'Inspect a helper.', 'remember memory'),
    ):
        mount_test_capability(runtime, alias=alias, canonical_path=f'op_test_{alias}',
                              InputModel=EmptyToolInput, OutputModel=EmptyToolOutput,
                              handler=lambda _: {}, guidance=ToolGuidance(
                                  purpose=purpose, use_when=when, do_not_use_when='Unrelated work.'))
    for query in ('remember_memory', 'remember memory', 'memory remember'):
        result = runtime.execute_tool(new_tool_call(name='search_tools', args={'query': query}))
        assert [hit['alias'] for hit in result.structured['hits']] == ['remember_memory']
    broad = runtime.execute_tool(new_tool_call(name='search_tools', args={'query': 'remember memory', 'top_k': 20}))
    aliases = [hit['alias'] for hit in broad.structured['hits']]
    assert 'inspect_active_memory_provider' in aliases
    assert 'unrelated_helper' not in aliases


def test_domain_partial_alias_and_purpose_synonym(tmp_path):
    from pal.lsp import build_lsp_plugin
    core = PalCore()
    register_with_core(core.context)
    build_lsp_plugin(runtime_root=tmp_path).register_with_core(core.context)
    for module in ('execution', 'lsp'):
        core.publish_module_capabilities(module)
    runtime = core.context.execution_runtime
    try:
        def hits(query):
            return [item['alias'] for item in runtime.execute_tool(
                new_tool_call(name='search_tools', args={'query': query})).structured['hits']]
        assert set(hits('lsp prepare')) == {'prepare_lsp_workspace', 'prepare_lsp_call_hierarchy'}
        assert hits('workspace prepare lsp') == ['prepare_lsp_workspace']
        assert hits('lsp callers') == ['find_lsp_incoming_calls']
        assert hits('find_lsp_incoming_calls') == ['find_lsp_incoming_calls']
    finally:
        runtime.shutdown()


def test_inventory_is_compact_but_exact_schema_remains_available(runtime):
    result = runtime.execute_tool(new_tool_call(name='call_tool', args={'name': 'list_tools', 'args': {}}))
    assert result.ok
    compact, _ = json.JSONDecoder().raw_decode(result.llm_text)
    assert compact['tools']
    for tool in compact['tools']:
        assert set(tool) == {'name', 'purpose', 'module', 'invocation_mode'}
    full = runtime.execute_tool(new_tool_call(name='read_tool', args={'name': 'read_file'}))
    assert full.ok
    assert 'file_path' in full.llm_text
    assert len(result.llm_text) < len(json.dumps(result.structured))


def test_model_list_contains_selection_capabilities_without_credentials():
    from types import SimpleNamespace
    from pal.llm.capabilities import LLMIntrospectionProvider
    from pal.shared import IntrospectionCall
    endpoints = [SimpleNamespace(endpoint_id=name, model_id=name, provider='test',
        supports_vision=vision, supports_tools=True, context_window=64000, api_key='SECRET')
        for name, vision in [('text', False), ('vision', True)]]
    runtime = SimpleNamespace(endpoint_resolver=SimpleNamespace(enabled=lambda: endpoints))
    result = LLMIntrospectionProvider(runtime).list_endpoints(IntrospectionCall(name='list_llm_endpoints'))
    assert [item['name'] for item in result.structured['items'] if item['supports_vision']] == ['vision']
    assert all(item['context_window'] == 64000 for item in result.structured['items'])
    assert 'SECRET' not in result.llm_text


def test_builtin_tool_navigation_references_declared_public_aliases():
    import ast
    import re
    from pathlib import Path
    from pal.skill.builtin_skills import builtin_declared_skills
    aliases = set()
    references = set()
    for path in (Path(__file__).parents[1] / 'src' / 'pal').rglob('*.py'):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func.id if isinstance(node.func, ast.Name) else ''
            if function == 'capability_action':
                for keyword in node.keywords:
                    if keyword.arg == 'aliases':
                        try:
                            aliases.update(ast.literal_eval(keyword.value))
                        except (ValueError, TypeError):
                            pass  # Dynamic plugin aliases are checked at projection time.
            elif function == 'ToolGuidance':
                for keyword in node.keywords:
                    if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                        references.update(re.findall(r'\(use ([a-z][a-z0-9_]+)[).,]', keyword.value.value))
    for skill in builtin_declared_skills():
        references.update(skill.capability_refs)
    assert not references - aliases, sorted(references - aliases)
