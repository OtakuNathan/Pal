"""Discovery misses navigate only the caller's selected visible tool bucket."""
from types import SimpleNamespace

from pal.core import PalCore
from pal.execution.runtime import ExecutionRuntime
from pal.execution.generated_tool_models import ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchOutput as SearchOutput
from tests.test_tool_search_rules import runtime, mount, search


def test_miss_returns_navigation_without_changing_hit_or_suggestion_counts(runtime):
    mount(runtime, 'restart_channel_endpoint', search_objects=('endpoint',))
    result = search(runtime, 'reconnect endpoint', domain='channel')
    assert not result['hits'] and not result.get('suggestions')
    assert result['returned_count'] == result['total_count'] == 0
    assert result['directory']['tools'] == [{'alias': 'restart_channel_endpoint', 'purpose': 'Set a checkbox'}]
    assert 'not matches' in result['usage_hint']
    SearchOutput.model_validate(result, strict=True)
    assert 'directory' not in search(runtime, 'restart_channel_endpoint', domain='channel')


def test_directory_keeps_domains_filters_and_old_visible_generations(runtime):
    mount(runtime, 'restart_channel_endpoint', search_objects=('endpoint',))
    old = runtime.registry_generation
    mount(runtime, 'reload_plugin', search_objects=('plugin',))
    for kwargs in ({'domain': 'plugin'}, {'domain': 'channel', 'module_name': 'absent'},
                   {'domain': 'channel', 'family': 'absent'}, {'domain': 'channel', 'tags': ['absent']}):
        result = search(runtime, 'unmatched words', **kwargs)
        aliases = [x['alias'] for x in result.get('directory', {}).get('tools', [])]
        assert 'restart_channel_endpoint' not in aliases
    result = ExecutionRuntime._search_generation(old, {'domain': 'plugin', 'query': 'unmatched'})
    assert 'directory' not in result
    assert 'plugin' not in result['available_domains']
    unknown = search(runtime, 'reload plugin', domain='git')
    assert not unknown['hits'] and 'directory' not in unknown
    assert 'plugin' in unknown['available_domains']
    broad = search(runtime, 'unmatched words')
    assert not broad['hits'] and 'directory' not in broad
    assert 'channel' in broad['available_domains']


def test_directory_pagination_is_complete_deterministic_and_distinct_from_hit_limit():
    records = {f'tool_{i:02}': dict(alias=f'tool_{i:02}', domain='browser', purpose=f'Purpose {i}',
                                namespace='operation', module_id='browser', tags=[])
               for i in range(35)}
    generation = SimpleNamespace(search_records=records)
    query = {'domain': 'browser', 'query': 'unmatched words', 'top_k': 1}
    first = ExecutionRuntime._search_generation(generation, query)
    assert len(first['directory']['tools']) == 16
    assert first['directory']['next_offset'] == 16
    assert first['directory']['total_count'] == 35
    assert first == ExecutionRuntime._search_generation(SimpleNamespace(search_records=dict(reversed(list(records.items())))), query)
    aliases = []
    offset = 0
    while offset is not None:
        result = ExecutionRuntime._search_generation(generation, {**query, 'directory_offset': offset})
        assert not result['hits'] and not result.get('suggestions')
        assert all(set(x) == {'alias', 'purpose'} for x in result['directory']['tools'])
        aliases.extend(x['alias'] for x in result['directory']['tools'])
        offset = result['directory']['next_offset']
    assert aliases == sorted(records)
    empty = ExecutionRuntime._search_generation(generation, {**query, 'directory_offset': 99})
    assert empty['directory']['tools'] == []
    assert empty['directory']['next_offset'] is None


def test_browse_mode_ignores_words_but_keeps_filters(runtime):
    mount(runtime, 'restart_channel_endpoint', search_objects=('endpoint',))
    result = search(runtime, 'restart_channel_endpoint', domain='channel', directory_offset=0)
    assert result['hits'] == []
    assert result['directory']['tools'][0]['alias'] == 'restart_channel_endpoint'
    assert 'directory' not in search(runtime, '', domain='channel', directory_offset=0, namespace='introspection')
    result = search(runtime, '', directory_offset=0)
    assert not result['hits'] and 'directory' not in result
    assert 'available_domains' in result


def test_domain_alone_returns_names_instead_of_full_contracts(runtime):
    mount(runtime, 'restart_channel_endpoint', search_objects=('endpoint',))
    result = search(runtime, '', domain='channel')
    assert result['hits'] == []
    assert result['directory']['tools'] == [{'alias': 'restart_channel_endpoint', 'purpose': 'Set a checkbox'}]
    assert 'Domain directory requested' in result['usage_hint']
    SearchOutput.model_validate(result, strict=True)
    assert search(runtime, 'restart_channel_endpoint', domain='channel')['hits']


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
