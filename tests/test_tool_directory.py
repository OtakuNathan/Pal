"""Discovery misses navigate only the caller's selected visible tool bucket."""
from types import SimpleNamespace

from pal.core import PalCore
from pal.execution.runtime import ExecutionRuntime
from pal.execution.generated_tool_models import ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchOutput as SearchOutput
from tests.test_tool_search_rules import runtime, mount, search


def test_miss_returns_navigation_without_changing_hit_or_suggestion_counts(runtime):
    mount(runtime, 'manage_channel_endpoint', search_objects=('endpoint',))
    result = search(runtime, 'reconnect endpoint', domain='channel')
    assert not result['hits'] and not result.get('suggestions')
    assert result['returned_count'] == result['total_count'] == 0
    assert result['directory']['tools'] == [{'alias': 'manage_channel_endpoint', 'purpose': 'Set a checkbox'}]
    assert 'not matches' in result['usage_hint']
    SearchOutput.model_validate(result, strict=True)
    assert 'directory' not in search(runtime, 'manage_channel_endpoint', domain='channel')


def test_directory_keeps_domains_filters_and_old_visible_generations(runtime):
    mount(runtime, 'manage_channel_endpoint', search_objects=('endpoint',))
    old = runtime.registry_generation
    mount(runtime, 'reload_plugin', search_objects=('plugin',))
    for kwargs in ({'domain': 'plugin'}, {'domain': 'channel', 'module_name': 'absent'},
                   {'domain': 'channel', 'family': 'absent'}, {'domain': 'channel', 'tags': ['absent']}):
        result = search(runtime, 'unmatched words', **kwargs)
        aliases = [x['alias'] for x in result.get('directory', {}).get('tools', [])]
        assert 'manage_channel_endpoint' not in aliases
    result = ExecutionRuntime._search_generation(old, {'domain': 'plugin', 'query': 'unmatched'})
    assert 'directory' not in result
    assert 'plugin' not in result['available_domains']
    unknown = search(runtime, 'reload plugin', domain='git')
    assert not unknown['hits'] and 'directory' not in unknown
    assert 'plugin' in unknown['available_domains']
    broad = search(runtime, 'unmatched words')
    assert not broad['hits'] and 'directory' not in broad
    assert 'channel' in broad['available_domains']


def test_complete_directory_is_deterministic_and_distinct_from_hit_limit():
    records = {f'tool_{i:02}': dict(alias=f'tool_{i:02}', domain='browser', purpose=f'Purpose {i}',
                                namespace='operation', module_id='browser', tags=[])
               for i in range(35)}
    generation = SimpleNamespace(search_records=records)
    for query in ('', 'unmatched words'):
        args = {'domain': 'browser', 'query': query, 'top_k': 1}
        result = ExecutionRuntime._search_generation(generation, args)
        assert not result['hits'] and not result.get('suggestions')
        directory = result['directory']
        assert set(directory) == {'domain', 'tools', 'total_count'}
        assert directory['total_count'] == 35
        assert [item['alias'] for item in directory['tools']] == sorted(records)
        assert all(set(item) == {'alias', 'purpose'} for item in directory['tools'])
        assert result == ExecutionRuntime._search_generation(
            SimpleNamespace(search_records=dict(reversed(list(records.items())))), args)


def test_no_query_lists_domains_and_domain_browsing_keeps_filters(runtime):
    mount(runtime, 'manage_channel_endpoint', search_objects=('endpoint',))
    assert 'directory' not in search(runtime, '', domain='channel', namespace='introspection')
    result = search(runtime, '')
    assert not result['hits'] and 'directory' not in result
    assert 'channel' in result['available_domains']
    SearchOutput.model_validate(result, strict=True)


def test_directory_uses_ordinary_snapshot_and_file_read(runtime):
    import json
    from pathlib import Path
    from pal.execution.contracts import ToolCallBudget
    from pal.execution.tool_presentation import render_tool_search
    from pal.shared.tool_protocol import new_tool_call

    for i in range(35):
        mount(runtime, f'inspect_browser_item_{i:02}', purpose=f'Inspect browser item {i}.')
    args = {'domain': 'browser', 'top_k': 1}
    payload = runtime._search_generation(runtime.registry_generation, args)
    full_text = render_tool_search(runtime.registry_generation, payload)
    result = runtime.invoke_direct_tool(new_tool_call(name='search_tools', args=args),
        budget=ToolCallBudget(max_output_chars=1000, preview_chars=200), turn_id='directory')
    assert result.kind == 'complete'
    assert result.output['directory']['total_count'] == 35
    ref, = result.snapshot_refs
    assert len(result.llm_text) <= 1000
    assert Path(ref.path).read_text() == full_text
    assert len(json.loads(full_text)['directory']['tools']) == 35
    lines = full_text.splitlines()
    offset = next(i for i, line in enumerate(lines, 1) if 'inspect_browser_item_34' in line)
    read = runtime.invoke_direct_tool(new_tool_call(name='read_file', args={
        'file_path': ref.path, 'offset': offset, 'limit': 3}), turn_id='directory')
    assert read.kind == 'complete', read
    assert 'inspect_browser_item_34' in read.llm_text
    assert Path(ref.path).read_text() == full_text
    assert runtime.result_snapshots.references() == (ref,)


def test_search_schema_rejects_retired_directory_paging(runtime):
    from pal.shared.tool_protocol import new_tool_call

    result = runtime.invoke_direct_tool(new_tool_call(name='search_tools', args={
        'domain': 'browser', 'directory_offset': 0}))
    assert result.kind == 'rejected'


def test_domain_alone_returns_names_instead_of_full_contracts(runtime):
    mount(runtime, 'manage_channel_endpoint', search_objects=('endpoint',))
    result = search(runtime, '', domain='channel')
    assert result['hits'] == []
    assert result['directory']['tools'] == [{'alias': 'manage_channel_endpoint', 'purpose': 'Set a checkbox'}]
    assert 'Domain directory requested' in result['usage_hint']
    SearchOutput.model_validate(result, strict=True)
    assert search(runtime, 'manage_channel_endpoint', domain='channel')['hits']


def test_directory_summaries_are_bounded_and_do_not_change_full_contracts(runtime):
    from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput, ToolGuidance
    from pal.shared.tool_protocol import new_tool_call
    from tests.capability_fixture import mount_test_capability

    purpose = 'Inspect a model connection and its public metadata. ' * 30
    mount_test_capability(runtime, alias='inspect_llm_connection', canonical_path='op_test_llm_connection',
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=lambda _: {},
        guidance=ToolGuidance(purpose=purpose, use_when='Inspect a connection.', do_not_use_when='Other tasks.'))
    result = search(runtime, '', domain='llm')
    item = result['directory']['tools'][0]
    assert item['alias'] == 'inspect_llm_connection'
    assert item['purpose_truncated'] is True
    assert len(item['purpose']) <= 160
    assert item['purpose'].endswith('…')
    SearchOutput.model_validate(result, strict=True)
    reply = runtime.execute_tool(new_tool_call(name='read_tool', args={'name': item['alias']}))
    assert reply.ok
    assert purpose.strip() in reply.structured['description']


def test_public_partial_hit_output_accepts_disclosed_unmatched_terms(runtime):
    mount(runtime, 'inspect_llm_model', purpose='Inspect a model.', search_objects=('model',))
    result = search(runtime, 'inspect model configuration', domain='llm')
    assert result['hits'][0]['unmatched_terms'] == ['configuration']
    SearchOutput.model_validate(result, strict=True)
