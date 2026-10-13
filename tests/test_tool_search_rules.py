"""Conditional vocabulary, scope ranking and conservative recovery contracts."""
import json
from typing import Literal

import pytest
from pydantic import ValidationError

from pal.core import PalCore
from pal.execution import register_with_core
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput, StrictToolModel, ToolGuidance
from pal.shared.tool_protocol import new_tool_call
from tests.capability_fixture import mount_test_capability


@pytest.fixture
def runtime():
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    yield core.context.execution_runtime
    core.context.execution_runtime.shutdown()
    core.close()


def mount(runtime, alias='set_browser_checked_state', **metadata):
    guidance = ToolGuidance(purpose=metadata.pop('purpose', 'Set a checkbox'), use_when='Set a known checkbox',
                            do_not_use_when='Inspecting browser health', **metadata)
    mount_test_capability(runtime, alias=alias, canonical_path='op_test_' + alias,
                          InputModel=EmptyToolInput, OutputModel=EmptyToolOutput,
                          handler=lambda _: {}, guidance=guidance)


def search(runtime, query, **kwargs):
    result = runtime.execute_tool(new_tool_call(name='search_tools', args={'query': query, **kwargs}))
    assert result.ok, result.text
    return result.structured


def names(result):
    return [hit['alias'] for hit in result['hits']]


RULE = {'terms': ('check',), 'requires_any': ('checkbox', 'checkboxes')}


def test_conditional_words_preserve_exact_alias_and_do_not_expand_conditions(runtime):
    mount(runtime, search_objects=('checkbox', 'checkboxes', 'state'), search_term_rules=(RULE,))
    for query in ('browser check checkbox', 'checkbox browser check', 'browser_check_checkboxes'):
        assert names(search(runtime, query)) == ['set_browser_checked_state']
    assert names(search(runtime, 'set_browser_checked_state')) == ['set_browser_checked_state']
    for query in ('check browser state', 'browser check', 'browser check checkboxish'):
        result = search(runtime, query, top_k=5)
        assert result['hits'] == []
        assert not result.get('suggestions')


def test_scope_ranking_does_not_hide_explicit_provider_or_exact_alias(runtime):
    mount(runtime, 'forget_memory', search_objects=('memory',), search_terms=('delete',))
    mount(runtime, 'delete_provider_memory', search_objects=('memory',), search_scope_terms=('provider',))
    assert names(search(runtime, 'delete memory')) == ['forget_memory']
    broad = search(runtime, 'delete memory', top_k=5)
    assert names(broad) == ['forget_memory', 'delete_provider_memory']
    assert broad['hits'][0]['score'] > broad['hits'][1]['score']
    assert names(search(runtime, 'delete provider memory')) == ['delete_provider_memory']
    assert names(search(runtime, 'delete_provider_memory')) == ['delete_provider_memory']


def test_suggestions_are_separate_bounded_and_do_not_relax_filters(runtime):
    for alias in ('inspect_llm_endpoint', 'read_llm_endpoint', 'show_llm_endpoint', 'view_llm_endpoint'):
        mount(runtime, alias, search_objects=('endpoint',), search_terms=('inspect',))
    result = search(runtime, 'llm endpoint inspectt', top_k=2)
    assert result['hits'] == []
    assert result['total_count'] == result['returned_count'] == 0
    assert len(result['suggestions']) == 2
    assert len(search(runtime, 'llm endpoint inspectt', top_k=5)['suggestions']) == 3
    suggestion = result['suggestions'][0]
    assert suggestion['matched_terms'] == ['endpoint', 'llm']
    assert suggestion['unmatched_terms'] == ['inspectt']
    assert suggestion['contract']['alias'] == suggestion['alias']
    assert 'input_contract' in suggestion['contract']
    assert 'fuzzy' in suggestion['retrieval_sources']
    assert not search(runtime, 'llm endpoint inspectt', module_name='not_mounted').get('suggestions')
    assert not search(runtime, 'llm endpoint').get('suggestions')
    filtered = search(runtime, 'inspect_llm_endpoint', module_name='not_mounted')
    assert filtered['hits'] == []
    assert 'filter_suggestions' in filtered
    assert not filtered.get('suggestions')


def test_suggestions_do_not_discard_known_words_or_single_generic_matches(runtime):
    mount(runtime, 'inspect_llm_endpoint', search_objects=('endpoint',))
    mount(runtime, 'switch_browser_tab', search_objects=('tab',))
    mount(runtime, 'inspect_browser_status', search_objects=('status',))
    for query in ('llm switch endpoint', 'llm endpoint inspectt extraunknown',
                  'git status', 'endpoint inspectt', 'llm inspect inspectt'):
        result = search(runtime, query)
        assert result['hits'] == []
        assert not result.get('suggestions'), query


def test_rules_scope_and_objects_survive_serialization_and_change_fingerprint(runtime):
    from pal.bunshin.tool_guidance import bunshin_tool_guidance

    mount(runtime, search_objects=('checkbox', 'checkboxes', 'state'))
    old = runtime.registry_generation
    mount(runtime, search_objects=('checkbox', 'checkboxes', 'state'), search_term_rules=(RULE,))
    conditional = runtime.registry_generation
    assert conditional.generation_hash != old.generation_hash
    assert runtime._search_generation(old, {'query': 'browser check checkbox'})['hits'] == []
    assert names(search(runtime, 'browser check checkbox')) == ['set_browser_checked_state']
    mount(runtime, search_objects=('checkbox', 'checkboxes', 'state'), search_term_rules=(RULE,),
          search_scope_terms=('provider',))
    assert conditional.generation_hash != runtime.registry_generation.generation_hash
    spec = runtime.get_capability_spec('set_browser_checked_state')
    restored = ToolGuidance.model_validate_json(json.dumps(spec['guidance']), strict=True)
    adapted = bunshin_tool_guidance('op_test_set_browser_checked_state', restored)
    assert adapted.search_term_rules == restored.search_term_rules
    assert adapted.search_scope_terms == ('provider',)
    for payload in (search(runtime, 'browser check checkbox'), runtime.list_tool_specs(),
                    runtime.registry_generation.provider_specs):
        text = str(payload)
        assert 'search_term_rules' not in text
        assert 'search_scope_terms' not in text


@pytest.mark.parametrize('rule', [
    {'terms': (), 'requires_any': ('checkbox',)},
    {'terms': ('Check',), 'requires_any': ('checkbox',)},
    {'terms': ('check', 'check'), 'requires_any': ('checkbox',)},
    {'terms': ('check',), 'requires_any': ()},
    {'terms': ('check',), 'requires_any': ('check',)},
])
def test_invalid_rules_rejected(rule):
    with pytest.raises(ValidationError):
        ToolGuidance(purpose='p', use_when='u', do_not_use_when='d', search_term_rules=(rule,))


def test_registration_rejects_unconditional_bypasses_and_recursive_conditions(runtime):
    with pytest.raises(ValueError, match='unconditional'):
        mount(runtime, search_objects=('checkbox', 'checkboxes'), search_terms=('check',), search_term_rules=(RULE,))
    with pytest.raises(ValueError, match='unconditional'):
        mount(runtime, 'check_browser', search_objects=('checkbox', 'checkboxes'), search_term_rules=(RULE,))
    with pytest.raises(ValueError, match='indexed words'):
        mount(runtime, search_term_rules=(RULE,))

    class Input(StrictToolModel):
        operation: Literal['check', 'uncheck']

    with pytest.raises(ValueError, match='unconditional'):
        mount_test_capability(runtime, alias='manage_browser_checkbox', canonical_path='op_test_manage',
            InputModel=Input, OutputModel=EmptyToolOutput, handler=lambda _: {}, examples=({'operation': 'check'},),
            guidance=ToolGuidance(purpose='p', use_when='u', do_not_use_when='d',
                search_objects=('checkbox', 'checkboxes'), search_enum_fields=('operation',),
                search_term_rules=(RULE,)))


def test_suggestion_vocabulary_is_captured_with_the_visible_generation(runtime):
    mount(runtime, 'inspect_llm_endpoint', search_objects=('endpoint',))
    visible = runtime.registry_generation
    query = {'query': 'llm endpoint inspectt'}
    assert runtime._search_generation(visible, query)['suggestions']
    mount(runtime, 'inspectt_browser')
    assert not runtime._search_generation(runtime.registry_generation, query).get('suggestions')
    assert runtime._search_generation(visible, query)['suggestions']
    assert runtime._search_generation(visible, {'query': 'inspectt_browser'})['hits'] == []


def test_fuzzy_recovery_preserves_domains_actions_and_unknown_constraints(runtime):
    mount(runtime, 'inspect_browser_status', search_objects=('status',), search_terms=('check',))
    mount(runtime, 'forget_memory', search_objects=('memory',), search_terms=('delete',))
    for query in ('git check status', 'database check status', 'git delete memory',
                  'database delete memory', 'browser status frobnication',
                  'browzer inspect status', 'browser delete status', 'browser memory inspect status'):
        result = search(runtime, query)
        assert not result['hits'], query
        assert not result.get('suggestions'), query
    for query in ('browser inspectt status', 'browser insepct status', 'browser details status'):
        result = search(runtime, query)
        assert not result['hits']
        assert [s['alias'] for s in result['suggestions']] == ['inspect_browser_status']
    assert names(search(runtime, 'browser inspect status')) == ['inspect_browser_status']
    assert not search(runtime, 'browser inspect status').get('suggestions')


def test_fuzzy_known_action_can_only_use_declared_recovery_equivalence(runtime):
    mount(runtime, 'disable_proactive_task', search_objects=('task',))
    mount(runtime, 'suspend_browser_session', search_objects=('session',))
    result = search(runtime, 'proactive suspend task')
    assert not result['hits']
    assert [s['alias'] for s in result['suggestions']] == ['disable_proactive_task']
    assert not search(runtime, 'proactive restart task').get('suggestions')


def test_fuzzy_domain_recognition_handles_plural_aliases(runtime):
    mount(runtime, 'list_plugins', search_objects=('plugin', 'plugins'))
    result = search(runtime, 'plugin listt plugins')
    assert not result['hits']
    assert [s['alias'] for s in result['suggestions']] == ['list_plugins']


def test_explicit_domains_normalize_full_names_and_never_cross_buckets(runtime):
    mount(runtime, 'inspect_llm_endpoint', purpose='Inspect an endpoint.', search_objects=('endpoint',))
    mount(runtime, 'inspect_channel_endpoint', purpose='Inspect an endpoint.', search_objects=('endpoint',))
    for domain in ('llm', 'LLM', 'large language model', 'large_language_model'):
        result = search(runtime, 'inspect endpoint', domain=domain)
        assert names(result) == ['inspect_llm_endpoint']
        assert result['applied_filters']['domain'] == 'llm'
        assert result['hits'][0]['domain'] == 'llm'
    fuzzy = search(runtime, 'inspectt endpoint', domain='large language model')
    assert names(fuzzy) == ['inspect_llm_endpoint']
    assert fuzzy['hits'][0]['unmatched_terms'] == ['inspectt']
    for domain in ('git', 'browrser'):
        result = search(runtime, 'inspect endpoint', domain=domain)
        assert not result['hits'] and not result.get('suggestions')
        assert 'llm' in result['available_domains']
    assert names(search(runtime, 'inspect_llm_endpoint')) == ['inspect_llm_endpoint']
    assert not search(runtime, 'inspect_llm_endpoint', domain='channel')['hits']
    assert not search(runtime, 'inspect endpoint', domain='llm', module_name='absent')['hits']


def test_domain_ownership_precedes_target_words_and_tracks_visible_generation(runtime):
    mount_test_capability(runtime, alias='reload_lsp_plugin', canonical_path='op_plugins_reload_lsp',
        module_id='plugins', InputModel=EmptyToolInput, OutputModel=EmptyToolOutput,
        handler=lambda _: {}, guidance=ToolGuidance(purpose='Reload an LSP plugin.',
            use_when='New plugin code.', do_not_use_when='Endpoint connection restart.'))
    old = runtime.registry_generation
    assert names(search(runtime, 'reload lsp', domain='plugins')) == ['reload_lsp_plugin']
    assert not search(runtime, 'reload lsp', domain='language server protocol')['hits']
    mount(runtime, 'inspect_llm_endpoint', purpose='Inspect an endpoint.', search_objects=('endpoint',))
    assert 'llm' not in runtime._search_generation(old, {'domain': 'llm'})['available_domains']
    assert names(search(runtime, 'endpoint', domain='llm')) == ['inspect_llm_endpoint']


def test_registered_builtin_operation_domains():
    from scripts.benchmark_discovery_flow import make_fixture
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as root:
        fx = make_fixture(Path(root))
        try:
            expected = {'set_browser_checked_state': 'browser', 'reload_plugin': 'plugin',
                        'inspect_active_llm': 'llm', 'send_channel_attachment': 'channel',
                        'read_file': 'file', 'run_shell': 'shell'}
            for alias, domain in expected.items():
                record = fx.runtime.registry_generation.search_records[alias]
                assert record['domain'] == domain
                assert names(search(fx.runtime, alias, domain=domain)) == [alias]
        finally:
            fx.close()
