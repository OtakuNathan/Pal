"""Domain purpose recall preserves visibility and marks partial evidence."""
from types import SimpleNamespace

from pal.core import PalCore
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput, ToolGuidance
from tests.capability_fixture import mount_test_capability
from tests.test_tool_search_rules import runtime


def search(records, query='', **filters):
    return ExecutionRuntime._search_generation(SimpleNamespace(search_records=records),
        {'domain': 'browser', 'query': query, **filters})


def test_purpose_ranks_without_alias_action_object_or_scope_boosts():
    records = {
        'delete_browser_checkbox': {'alias': 'delete_browser_checkbox', 'domain': 'browser',
            'purpose': 'Inspect connection health.', 'search_objects': ['checkbox'],
            'search_scope_terms': ['checkbox']},
        'unrelated_alias': {'alias': 'unrelated_alias', 'domain': 'browser',
            'purpose': 'Set whether a checkbox is checked.'},
    }
    result = search(records, 'delete checkbox encrypted')
    hit, = result['hits']
    assert hit['alias'] == 'unrelated_alias'
    assert hit['matched_terms'] == ['checkbox']
    assert hit['unmatched_terms'] == ['delete', 'encrypted']
    assert search(records, 'delete_browser_checkbox')['hits'][0]['alias'] == 'delete_browser_checkbox'


def test_tool_owned_auxiliary_phrases_and_noun_number_are_searchable():
    record = {'alias': 'read_browser', 'domain': 'browser', 'purpose': 'Capture visible content.',
              'search_auxiliary_terms': ['page details', '查看网页'],
              'search_intents': ['read rendered pages']}
    for query in ('page detail', '查看网页', 'read rendered page'):
        hit, = search({'read_browser': record}, query)['hits']
        assert hit['alias'] == 'read_browser'
        assert not hit['unmatched_terms']


def test_conditional_auxiliary_words_require_original_query_condition():
    record = {'alias': 'set_browser_checked_state', 'domain': 'browser',
              'purpose': 'Set a checkbox.', 'search_auxiliary_terms': [],
              'search_term_rules': [{'terms': ['check'], 'requires_any': ['checkbox']}]}
    records = {record['alias']: record}
    assert not search(records, 'check')['hits']
    hit, = search(records, 'check checkbox')['hits']
    assert hit['matched_terms'] == ['check', 'checkbox']


def test_no_positive_evidence_does_not_force_candidates_or_expand_synonyms():
    records = {'list_browser_tabs': {'alias': 'list_browser_tabs', 'domain': 'browser',
                                    'purpose': 'List open tabs.'}}
    for query in ('quantum teleportation', 'enumerate', 'browser'):
        result = search(records, query)
        assert not result['hits'] and not result.get('suggestions')
        assert result['directory']['tools'][0]['alias'] == 'list_browser_tabs'


def test_spelling_only_fallback_retains_original_missing_words():
    record = {'alias': 'capture_browser_snapshot', 'domain': 'browser',
              'purpose': 'Capture a snapshot.'}
    result = search({record['alias']: record}, 'snaphsot')
    assert not result['hits']
    suggestion, = result['suggestions']
    assert suggestion['alias'] == record['alias']
    assert suggestion['unmatched_terms'] == ['snaphsot']
    assert suggestion['contract']['purpose'] == record['purpose']


def test_filters_and_unrelated_buckets_do_not_change_rank_statistics():
    eligible = {'a': {'alias': 'a', 'domain': 'browser', 'purpose': 'Read a rendered page.',
                      'namespace': 'operation', 'module_id': 'web_fetch', 'tags': ['user']}}
    records = {**eligible,
               'hidden': {'alias': 'hidden', 'domain': 'browser', 'purpose': 'Read a rendered page.',
                          'namespace': 'introspection', 'module_id': 'web_fetch', 'tags': ['user']},
               'foreign': {'alias': 'foreign', 'domain': 'file', 'purpose': 'Read a rendered page.',
                           'namespace': 'operation', 'module_id': 'execution', 'tags': ['user']}}
    filters = {'namespace': 'operation', 'module_name': 'web_fetch', 'tags': ['user']}
    assert search(eligible, 'rendered page', **filters) == search(records, 'rendered page', **filters)
    assert not search(records, 'a', domain='file')['hits']
    unavailable = search(records, 'a', domain='browzer')
    assert not unavailable['hits'] and not unavailable.get('directory')
    assert 'browser' in unavailable['available_domains']


def test_default_three_and_stable_ties_under_query_and_registry_permutation():
    records = {f'tool_{i}': {'alias': f'tool_{i}', 'domain': 'browser', 'purpose': 'Inspect a widget.'}
               for i in range(8)}
    first = search(records, 'inspect widget')
    assert [hit['alias'] for hit in first['hits']] == ['tool_0', 'tool_1', 'tool_2']
    assert first['total_count'] == 8 and first['truncated']
    assert first == search(dict(reversed(list(records.items()))), 'inspect widget')
    assert first['hits'] == search(records, 'widget inspect')['hits']
    assert len(search(records, 'inspect widget', top_k=100)['hits']) == 5


def test_authored_auxiliary_terms_are_distinct_from_objects_and_fingerprinted(runtime):
    def mount(auxiliary):
        mount_test_capability(runtime, alias='inspect_browser_fixture', canonical_path='op_test_browser_fixture',
            InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=lambda _: {},
            guidance=ToolGuidance(purpose='Inspect a connection.', use_when='Inspect a connection.',
                do_not_use_when='Other tasks.', search_objects=('needle',), search_terms=auxiliary))
    mount(())
    old = runtime.registry_generation
    mount(('needle',))
    current = runtime.registry_generation
    assert old.search_records['inspect_browser_fixture']['search_terms'] == current.search_records['inspect_browser_fixture']['search_terms']
    assert old.generation_hash != current.generation_hash
    assert not search(old.search_records, 'needle')['hits']
    assert search(current.search_records, 'needle')['hits'][0]['alias'] == 'inspect_browser_fixture'
    assert 'search_auxiliary_terms' not in str(search(current.search_records, 'needle')['hits'])
