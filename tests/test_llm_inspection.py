"""Pending batch: one passive, parameterless LLM snapshot and an active label."""
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pal.core import PalCore
from pal.core.tool_surface import ToolSurface
from pal.execution import register_with_core as register_execution
from pal.llm.capabilities import LLMIntrospectionProvider, register_with_core
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.shared import IntrospectionCall
from pal.shared.tool_protocol import new_tool_call
from tests.test_llm_usage import _Settings, _endpoint


@pytest.fixture
def runtime():
    return LLMRuntime(
        endpoint_resolver=EndpointResolver(endpoints=(_endpoint(), _endpoint('backup'))),
        settings_repository=_Settings(think_level='high'),
    )


def snapshot(runtime):
    return LLMIntrospectionProvider(runtime).inspect(IntrospectionCall(name='inspect_llm'))


def test_active_endpoint_is_labelled_once_without_duplicating_capabilities(runtime, monkeypatch):
    for method in ('refresh_runtime_settings', 'refresh_llm_endpoints', 'set_active_endpoint', 'generate'):
        monkeypatch.setattr(runtime, method, Mock(side_effect=AssertionError('inspection mutated or invoked LLM')))
    result = snapshot(runtime)
    payload = result.structured
    assert payload['configured_active_endpoint_id'] == payload['effective_primary_endpoint_id'] == 'primary'
    assert payload['endpoint_count'] == len(payload['endpoints']) == 2
    assert [item['label'] for item in payload['endpoints']] == ['primary (active)', 'backup']
    assert [item['endpoint_id'] for item in payload['endpoints']] == ['primary', 'backup']
    assert result.llm_text.count('(active)') == 1
    assert 'active_model' not in payload and 'context_window' not in payload
    assert payload['thinking'] == {'available': True, 'current': 'high'}
    assert not payload['diagnostics']['partial']
    for endpoint in payload['endpoints']:
        assert endpoint['context_window'] == 32000
        assert endpoint['max_output_tokens'] == 4096
        assert endpoint['transport'] == 'http'
        assert endpoint['thinking_levels'] == ('off', 'medium', 'high')
        assert endpoint['usage']['status'] == 'not_recorded'
    for method in ('refresh_runtime_settings', 'refresh_llm_endpoints', 'set_active_endpoint', 'generate'):
        getattr(runtime, method).assert_not_called()


def test_all_endpoints_are_returned_without_a_selector_or_limit(runtime):
    runtime.endpoint_resolver.endpoints = tuple(_endpoint(str(index)) for index in range(9))
    runtime.active_endpoint_id = '8'
    payload = snapshot(runtime).structured
    assert payload['endpoint_count'] == len(payload['endpoints']) == 9
    assert [item['label'] for item in payload['endpoints'] if item['label'].endswith(' (active)')] == ['8 (active)']


def test_unknown_selection_and_empty_directory_do_not_invent_an_active_endpoint(runtime):
    runtime.active_endpoint_id = 'missing'
    payload = snapshot(runtime).structured
    assert payload['configured_active_endpoint_id'] == 'missing'
    assert payload['effective_primary_endpoint_id'] is None
    assert all('(active)' not in item['label'] for item in payload['endpoints'])
    assert payload['thinking'] == {'available': False, 'current': None}
    runtime.endpoint_resolver.endpoints = ()
    payload = snapshot(runtime).structured
    assert payload['endpoint_count'] == 0 and payload['endpoints'] == []
    assert payload['configured_active_endpoint_id'] == 'missing'
    assert payload['effective_primary_endpoint_id'] is None


def test_usage_is_one_process_snapshot_with_only_summary_fields(runtime, monkeypatch):
    accounting = {
        'scope': 'resident_process', 'started_at': '2026-10-15T00:00:00Z',
        'request_count': 4, 'input_tokens': 100, 'output_tokens': 20,
        'cost': 0.1, 'cost_complete': False, 'cost_unknown_attempt_count': 1,
        'reported': True, 'usage_reporting_rate': 0.75, 'token_cache_ratio': 0.5,
        'latest_input_tokens': 25, 'prompt_cache_policy': {'private': 'POLICY_INTERNAL'},
        'by_endpoint': [{'endpoint_id': 'primary', 'request_count': 3, 'cost': 0.1,
                         'cost_complete': False, 'cost_unknown_attempt_count': 1,
                         'latest_provider_response_count': 1, 'latest_input_tokens': 25}],
    }
    reader = Mock(return_value=accounting)
    monkeypatch.setattr(runtime, 'usage_snapshot', reader)
    result = snapshot(runtime)
    reader.assert_called_once_with()
    payload = result.structured
    assert payload['usage']['scope'] == 'resident_process'
    assert payload['usage']['request_count'] == 4
    assert payload['usage']['started_at'] == accounting['started_at']
    assert payload['usage']['cost_complete'] is False
    primary, backup = payload['endpoints']
    assert primary['usage']['request_count'] == 3
    assert primary['usage']['cost_unknown_attempt_count'] == 1
    assert backup['usage']['status'] == 'not_recorded'
    for text in ('POLICY_INTERNAL', 'prompt_cache_policy', 'latest_input_tokens', 'latest_provider_response_count', 'by_endpoint'):
        assert text not in result.llm_text
    assert 'prompt_cache_policy' in accounting and 'latest_input_tokens' in accounting['by_endpoint'][0]


@pytest.mark.parametrize('section,method', [('thinking', 'thinking_status'), ('usage', 'usage_snapshot')])
def test_unavailable_section_keeps_other_diagnostics_and_a_bounded_error(runtime, monkeypatch, section, method):
    monkeypatch.setattr(runtime, method, Mock(side_effect=RuntimeError('backend unavailable token=hidden-secret ' + 'x' * 3000)))
    result = snapshot(runtime)
    payload = result.structured
    assert result.status == 'ok'
    assert payload['endpoint_count'] == 2
    assert payload[section]['status'] == 'unavailable'
    assert len(payload[section]['error']) <= 600
    assert section in payload['diagnostics']['unavailable_sections']
    assert payload['diagnostics']['partial'] is True
    assert 'Traceback' not in result.llm_text and 'hidden-secret' not in result.llm_text
    if section == 'usage':
        assert payload['thinking']['current'] == 'high'
        assert all(item['usage']['status'] == 'unavailable' for item in payload['endpoints'])
    else:
        assert payload['usage']['scope'] == 'resident_process'


def test_only_new_parameterless_tool_is_exposed_and_old_aliases_are_withdrawn(runtime, monkeypatch):
    core = PalCore()
    execution = core.context.execution_runtime
    try:
        register_execution(core.context)
        register_with_core(core.context, runtime)
        for module in ('execution', 'llm'):
            core.publish_module_capabilities(module)
        generation = execution.registry_generation
        names = {name for name, record in generation.search_records.items() if record['module_id'] == 'llm'}
        assert names == {'inspect_llm'}
        recovery = ToolSurface(core.context).select_failure_descriptors(SimpleNamespace(subsystem='llm'))
        assert {item.name for item in recovery} == {'read_tool', 'inspect_llm'}
        assert not names.intersection(generation.direct_aliases)
        contract = generation.search_records['inspect_llm']['input_contract']
        assert not contract.get('properties') and not contract.get('required')
        assert contract['additionalProperties'] is False
        result = execution.invoke_indirect_tool(new_tool_call(name='inspect_llm', args={}))
        assert result.kind == 'complete'
        assert result.output['endpoints'][0]['label'] == 'primary (active)'
        for alias in ('list_llm_endpoints', 'inspect_active_llm', 'inspect_llm_endpoint'):
            assert generation.record_for_alias(alias) is None
            assert not execution.execute_tool(new_tool_call(name='read_tool', args={'name': alias})).ok
            assert not execution.execute_tool(new_tool_call(name='call_tool', args={'name': alias, 'args': {}})).ok
            search = execution._search_generation(generation, {'query': alias})
            assert alias not in {hit['alias'] for hit in search['hits']}
        reader = Mock(side_effect=AssertionError('invalid input reached owner'))
        monkeypatch.setattr(runtime, 'usage_snapshot', reader)
        for args in ({'name': 'primary'}, {'view': 'detail'}, {'endpoint_id': 'primary'}, {'limit': 1}):
            assert execution.invoke_indirect_tool(new_tool_call(name='inspect_llm', args=args)).kind == 'rejected'
        reader.assert_not_called()
    finally:
        execution.shutdown()
