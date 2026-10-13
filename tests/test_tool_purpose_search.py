"""Purpose candidates disclose unsupported words without claiming capabilities."""
from types import SimpleNamespace

import pytest

from pal.core import PalCore  # Initialize runtime dependencies in their supported order.
from pal.execution.runtime import ExecutionRuntime


def lookup(query, *, domain='', records=None):
    records = records or {
        'edit_file': dict(alias='edit_file', domain='file', purpose='Apply valid exact replacements to one local UTF-8 file and report any failed items.', search_objects=('file',)),
        'read_file': dict(alias='read_file', domain='file', purpose='Read local file text.', search_objects=('file',)),
        'set_browser_checked_state': dict(alias='set_browser_checked_state', domain='browser', purpose='Set whether a checkbox element is checked.', search_objects=('checkbox', 'element')),
    }
    return ExecutionRuntime._search_generation(SimpleNamespace(search_records=records), {'query': query, 'domain': domain})


def aliases(result):
    return [hit['alias'] for hit in result['hits']]


def test_purpose_recalls_replacements_and_discloses_extra_requirements():
    assert aliases(lookup('apply exact replacements utf-8 file', domain='file'))[0] == 'edit_file'
    assert aliases(lookup('edit replacements', domain='file')) == ['edit_file']
    assert aliases(lookup('edit requested replacements', domain='file')) == ['edit_file']
    result = lookup('edit encrypted replacements', domain='file')
    assert aliases(result) == ['edit_file']
    assert 'encrypted' in result['hits'][0]['unmatched_terms']


def test_purpose_keeps_domains_but_actions_are_not_hard_gates():
    assert not aliases(lookup('edit replacements', domain='browser'))
    result = lookup('delete checkbox', domain='browser')
    assert aliases(result) == ['set_browser_checked_state']
    assert result['hits'][0]['unmatched_terms'] == ['delete']
    assert aliases(lookup('edit_file', domain='file')) == ['edit_file']
    assert not aliases(lookup('edit_file', domain='browser'))


def test_negative_guidance_and_alias_mentions_do_not_supply_evidence():
    record = dict(alias='inspect_widget', domain='core', purpose='Inspect widgets. Do not erase secrets; use delete_file instead.',
                  do_not_use_when='Encrypt archives', use_when='Before deleting data',
                  failure_next_steps='Restart a server', search_objects=('widget',))
    for query in ('erase secrets', 'delete file', 'encrypt archives', 'restart server'):
        assert not aliases(lookup(query, records={'inspect_widget': record}))


def test_purpose_ranking_is_stable_under_registry_and_query_order():
    records = {
        'edit_file': dict(alias='edit_file', domain='file', purpose='Apply replacements to file text.', search_objects=('file',)),
        'patch_file': dict(alias='patch_file', domain='file', purpose='Apply replacements to file text and report failed items.', search_objects=('file',)),
    }
    first = lookup('apply replacements', records=records)
    assert first == lookup('apply replacements', records=dict(reversed(list(records.items()))))
    assert aliases(first) == aliases(lookup('replacements apply', records=records))


def test_limitation_suffix_preserves_operation_but_not_excluded_capability():
    record = dict(alias='restart_channel_endpoint', domain='channel',
                  purpose='Restart one channel endpoint runtime instance without reloading provider code.')
    records = {record['alias']: record}
    assert aliases(lookup('restart endpoint runtime instance', domain='channel', records=records)) == [record['alias']]
    result = lookup('restart provider code', domain='channel', records=records)
    assert aliases(result) == [record['alias']]
    assert result['hits'][0]['unmatched_terms'] == ['code', 'provider']
    assert not aliases(lookup('reload provider code', domain='channel', records=records))


def test_compound_actions_need_main_operation_and_preserve_conditions():
    records = {
        'enable_plugin': dict(alias='enable_plugin', domain='plugin',
                              purpose='Enable and attach a disabled plugin in one step.'),
        'manage_browser_network_capture': dict(alias='manage_browser_network_capture', domain='browser',
                              purpose='Observe fetch/XHR API traffic through a JavaScript hook.'),
    }
    assert aliases(lookup('enable attach disabled plugin', domain='plugin', records=records)) == ['enable_plugin']
    assert aliases(lookup('observe fetch XHR traffic', domain='browser', records=records)) == ['manage_browser_network_capture']
    assert not aliases(lookup('browser fetch', records=records))
    assert not aliases(lookup('disable attach plugin', records=records))


def test_uppercase_state_identifiers_survive_but_alias_references_do_not():
    record = dict(alias='resume_workflow', domain='bunshin',
                  purpose='Resume TRIAGE_REQUIRED workflow items; use delete_file instead.')
    assert aliases(lookup('resume triage required workflow', records={record['alias']: record})) == ['resume_workflow']
    assert not aliases(lookup('delete file', records={record['alias']: record}))


def test_file_operations_domain_is_file_even_though_implementation_is_execution():
    from pal.execution.discovery_terms import tool_domain
    assert tool_domain('edit_file', 'execution') == 'file'
    assert tool_domain('inspect_execution_state', 'execution') == 'execution'


def test_object_only_metadata_does_not_boost_purpose_candidates():
    record = dict(alias='inspect_active_llm', domain='llm', purpose='Inspect the active LLM runtime.',
                  search_objects=('model',))
    result = lookup('inspect active configuration model', domain='llm', records={record['alias']: record})
    assert aliases(result) == ['inspect_active_llm']
    assert result['hits'][0]['matched_terms'] == ['active', 'inspect']
    assert result['hits'][0]['unmatched_terms'] == ['configuration', 'model']
    verbose = lookup('inspect active model module backend runtime configuration', domain='llm', records={record['alias']: record})
    assert aliases(verbose) == ['inspect_active_llm']
    assert not aliases(lookup('inspect active configuration model', domain='channel', records={record['alias']: record}))


@pytest.mark.parametrize('query', ['inspect encrypted model', 'inspect remote model',
                                 'inspect model auth', 'delete model configuration',
                                 'inspect model never', 'inspect sql database model'])
def test_partial_candidates_disclose_constraints_and_unsupported_actions(query):
    record = dict(alias='inspect_llm_model', domain='llm', purpose='Inspect model state.',
                  search_objects=('model',))
    result = lookup(query, domain='llm', records={record['alias']: record})
    assert aliases(result) == [record['alias']]
    assert result['hits'][0]['unmatched_terms']
    assert 'not proof' in result['usage_hint']


def test_domain_and_generic_context_alone_do_not_supply_object_evidence():
    record = dict(alias='inspect_llm_model', domain='llm', purpose='Inspect model state.')
    for query in ('inspect runtime configuration', 'inspect llm configuration', 'git inspect model'):
        assert not aliases(lookup(query, records={record['alias']: record}))


def test_positive_purpose_can_retrieve_without_structured_object_conditions():
    record = dict(alias='inspect_llm_endpoint', domain='llm', purpose='Inspect endpoint usage.',
                  search_objects=('model', 'endpoint'),
                  search_term_rules=({'terms': ('usage',), 'requires_any': ('endpoint',)},))
    result = lookup('inspect model usage configuration', domain='llm', records={record['alias']: record})
    assert aliases(result) == [record['alias']]
    assert 'model' in result['hits'][0]['unmatched_terms']
    assert aliases(lookup('inspect endpoint usage configuration', domain='llm', records={record['alias']: record})) == [record['alias']]


def test_complete_candidates_outrank_partial_context_matches():
    records = {
        'inspect_plugin_host': dict(alias='inspect_plugin_host', domain='plugin', purpose='Inspect plugin host state.'),
        'inspect_plugin_host_health': dict(alias='inspect_plugin_host_health', domain='plugin', purpose='Inspect plugin host health.'),
    }
    result = ExecutionRuntime._search_generation(SimpleNamespace(search_records=records),
                                                {'query': 'inspect host health', 'domain': 'plugin', 'top_k': 5})
    assert aliases(result) == ['inspect_plugin_host_health', 'inspect_plugin_host']
    assert result['hits'][0]['score'] > result['hits'][1]['score']
    assert result['hits'][1]['unmatched_terms'] == ['health']
    reverse = SimpleNamespace(search_records=dict(reversed(list(records.items()))))
    assert result == ExecutionRuntime._search_generation(reverse, {'query': 'inspect host health', 'domain': 'plugin', 'top_k': 5})
    assert result['hits'] == ExecutionRuntime._search_generation(reverse, {'query': 'health host inspect', 'domain': 'plugin', 'top_k': 5})['hits']
