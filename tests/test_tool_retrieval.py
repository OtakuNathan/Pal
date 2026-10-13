"""One search delivers reviewed retrieval candidates without losing boundaries."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pal.core import PalCore
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput, ToolGuidance
from pal.execution.generated_tool_models import ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchOutput as SearchOutput
from pal.execution.generated_tool_models import ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput as SearchInput
from pal.execution.tool_presentation import render_tool_search
from tests.capability_fixture import mount_test_capability
from tests.test_tool_search_rules import runtime, mount, search


def lookup(query, **filters):
    records = {
        'fill_browser_field': dict(alias='fill_browser_field', domain='browser',
            purpose='Replace text in a form field.', search_objects=('field',),
            search_intents=('set input field value',)),
        'set_browser_checked_state': dict(alias='set_browser_checked_state', domain='browser',
            purpose='Set whether a checkbox is checked.', search_objects=('checkbox',),
            search_intents=('check checkbox element',),
            search_term_rules=({'terms': ('check',), 'requires_any': ('checkbox',)},)),
    }
    return ExecutionRuntime._search_generation(SimpleNamespace(search_records=records), {'query': query, **filters})


def test_intent_recall_delivers_contract_and_skips_directory():
    result = lookup('set input field value', domain='browser')
    assert result['hits'][0]['alias'] == 'fill_browser_field'
    assert result['hits'][0]['purpose'] == 'Replace text in a form field.'
    assert not result['hits'][0]['unmatched_terms']
    assert 'search_intents' not in str(result)
    assert 'directory' not in result


def test_chinese_intents_and_english_noun_number_match_without_extra_calls():
    records = {'inspect_active_llm': dict(alias='inspect_active_llm', domain='llm',
        purpose='Inspect the active model.', search_intents=('查看当前模型和端点', 'view model capabilities'))}
    for query in ('查看当前模型和端点', 'view model capability'):
        result = ExecutionRuntime._search_generation(SimpleNamespace(search_records=records), {'query': query, 'domain': 'llm'})
        assert result['hits'][0]['alias'] == 'inspect_active_llm'
        assert not result['hits'][0]['unmatched_terms']


@pytest.mark.parametrize('query', ['delete input field value', 'set encrypted input field value',
                                 'set input field value never', 'set unknown object',
                                 'set input field git value', 'check element'])
def test_intent_candidates_disclose_unmatched_actions_and_constraints(query):
    result = lookup(query, domain='browser')
    if result['hits']:
        if query == 'check element':
            assert result['hits'][0]['alias'] == 'set_browser_checked_state'
            assert not result['hits'][0]['unmatched_terms']
        else:
            assert result['hits'][0]['unmatched_terms']
        assert 'not proof' in result['usage_hint']
    assert not result.get('suggestions')


def test_intents_obey_explicit_owner_filters_and_directory_mode():
    for filters in ({'domain': 'file'}, {'domain': 'browzer'},
                    {'domain': 'browser', 'module_name': 'absent'},
                    {'domain': 'browser', 'tags': ['absent']},
                    {'domain': 'browser', 'namespace': 'introspection'}):
        result = lookup('set input field value', **filters)
        assert not result['hits'] and not result.get('suggestions')
    result = lookup('set input field value', domain='browser', directory_offset=0)
    assert not result.get('suggestions') and result['directory']['total_count'] == 2
    assert lookup('fill_browser_field', domain='browser')['hits'][0]['alias'] == 'fill_browser_field'


def test_fused_suggestions_validate_and_render_full_contracts(runtime):
    mount(runtime, 'inspect_llm_endpoint', purpose='Inspect an endpoint.', search_objects=('endpoint',))
    result = search(runtime, 'endpiont', domain='llm')
    SearchOutput.model_validate(result, strict=True)
    suggestion, = result['suggestions']
    assert suggestion['contract']['invocation_mode'] in {'direct', 'indirect'}
    assert 'input_contract' in suggestion['contract']
    assert suggestion['unmatched_terms'] == ['endpiont']
    assert 'fuzzy' in suggestion['retrieval_sources']
    text = render_tool_search(runtime.registry_generation, result)
    assert 'input_contract' in text and 'search_text' not in text and 'input_shape' not in text
    assert 'directory' not in result


def test_intent_metadata_survives_json_and_visible_generation(runtime):
    def mount_intent(intents):
        mount_test_capability(runtime, alias='fill_browser_field', canonical_path='op_test_fill',
            InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=lambda _: {},
            guidance=ToolGuidance(purpose='Replace text in a form field.', use_when='Replace text.',
                do_not_use_when='Other tasks.', search_objects=('field',), search_intents=intents))
    mount_intent(())
    old = runtime.registry_generation
    mount_intent(('set input field value',))
    current = runtime.registry_generation
    assert old.generation_hash != current.generation_hash
    before = runtime._search_generation(old, {'query': 'set input field value', 'domain': 'browser'})
    assert before['hits'][0]['unmatched_terms'] == ['input', 'set', 'value']
    result = search(runtime, 'set input field value', domain='browser')
    SearchOutput.model_validate(result, strict=True)
    assert result['hits'][0]['alias'] == 'fill_browser_field'
    assert not result['hits'][0]['unmatched_terms']
    guidance = runtime.get_capability_spec('fill_browser_field')['guidance']
    import json
    assert ToolGuidance.model_validate_json(json.dumps(guidance), strict=True).search_intents == ('set input field value',)
    assert 'set input field value' not in str(current.provider_specs)


def test_fusion_is_stable_bounded_and_ignores_registry_order():
    records = {f'fill_browser_field_{i}': dict(alias=f'fill_browser_field_{i}', domain='browser',
        purpose='Replace text in a form field.', search_intents=('set input field value',)) for i in range(8)}
    args = {'query': 'set input field value', 'domain': 'browser', 'top_k': 5}
    first = ExecutionRuntime._search_generation(SimpleNamespace(search_records=records), args)
    assert len(first['hits']) == 5
    assert first == ExecutionRuntime._search_generation(SimpleNamespace(search_records=dict(reversed(list(records.items())))), args)
    assert first['hits'] == ExecutionRuntime._search_generation(SimpleNamespace(search_records=records),
        {**args, 'query': 'value field input set'})['hits']
    assert len(ExecutionRuntime._search_generation(SimpleNamespace(search_records=records),
        {**args, 'top_k': 1})['hits']) == 1


@pytest.mark.parametrize('field', ['top_k', 'limit'])
def test_public_hit_limit_is_one_to_five(field):
    for value in (1, 3, 5):
        SearchInput.model_validate({field: value}, strict=True)
    for value in (0, 6, 100):
        with pytest.raises(ValidationError):
            SearchInput.model_validate({field: value}, strict=True)
    assert SearchInput.model_json_schema()['properties'][field]['maximum'] == 5


def test_backend_caps_hits_when_called_without_public_validation():
    records = {f'inspect_browser_widget_{i}': dict(alias=f'inspect_browser_widget_{i}',
        domain='browser', purpose='Inspect a widget.') for i in range(12)}
    generation = SimpleNamespace(search_records=records)
    args = {'query': 'inspect widget', 'domain': 'browser'}
    default = ExecutionRuntime._search_generation(generation, args)
    assert len(default['hits']) == 3
    for field in ('top_k', 'limit'):
        capped = ExecutionRuntime._search_generation(generation, {**args, field: 100})
        assert len(capped['hits']) == capped['top_k'] == 5
        assert capped['total_count'] == 12 and capped['truncated']
    assert len(ExecutionRuntime._search_generation(generation,
        {**args, 'top_k': 2, 'limit': 100})['hits']) == 2


@pytest.mark.parametrize('intents', [('',), ('same', 'same'), ('x' * 161,), tuple(str(i) for i in range(9))])
def test_intent_metadata_is_bounded_and_nonempty(intents):
    with pytest.raises(ValidationError, match='search_intents'):
        ToolGuidance(purpose='p', use_when='u', do_not_use_when='d', search_intents=intents)
