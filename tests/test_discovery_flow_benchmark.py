"""Verify evaluation mechanics without spending model calls or invoking business tools."""
import asyncio

from scripts import benchmark_discovery_flow as benchmark
from pal.llm.contracts import generation_result_from_values
from pal.shared.tool_protocol import new_tool_call


def test_default_discovery_cases_use_current_callable_aliases(tmp_path):
    fx = benchmark.make_fixture(tmp_path)
    try:
        for _, _, expected in benchmark.CASES + benchmark.SEEDED_CASES + benchmark.PARAPHRASE_CASES:
            if expected is not None:
                assert fx.runtime.registry_generation.record_for_alias(expected) is not None, expected
    finally:
        fx.close()


def test_fuzzy_contract_review_then_validated_selection_never_executes_business(tmp_path):
    fx = benchmark.make_fixture(tmp_path)
    alias = 'inspect_browser_status'
    record = fx.runtime.registry_generation.record_for_alias(alias)
    business = new_tool_call(name='call_tool', args={'name': alias, 'args': {}}) if (
        record.execution.invocation_mode.value == 'indirect') else new_tool_call(name=alias, args={})
    calls = [new_tool_call(name='search_tools', args={'query': 'browser healt', 'domain': 'browser'}),
             new_tool_call(name='read_tool', args={'name': alias}), business]
    original = fx.runtime.execute_tool_async
    executed = []
    async def discovery_only(call, **kwargs):
        assert call.name in {'search_tools', 'read_tool', 'list_tools'}
        executed.append(call.name)
        return await original(call, **kwargs)
    fx.runtime.execute_tool_async = discovery_only
    class Model:
        async def agenerate(self, request):
            return generation_result_from_values(tool_calls=[calls.pop(0)],
                preferred_endpoint_id='fixture', preferred_model_id='fixture', usage_reported=True)
    try:
        row = asyncio.run(benchmark.run_case(Model(), {'endpoint_id': 'fixture', 'model_id': 'fixture'},
            fx, 'candidate', ('fuzzy', 'Check browser health.', alias), 1))
        assert row['valid']
        assert row['searches'][0]['hits'] == []
        assert alias in row['searches'][0]['suggestions']
        assert executed == ['search_tools', 'read_tool']
        assert row['reads']
    finally:
        fx.close()


def test_prompt_ablation_only_removes_new_conventions_and_domain_map():
    from pal.llm.serde import message_to_payload
    prior = str([message_to_payload(m) for m in benchmark.prompt_messages('prior_prompt', 'task')])
    candidate = str([message_to_payload(m) for m in benchmark.prompt_messages('candidate', 'task')])
    assert 'Tool naming and discovery conventions' not in prior
    assert 'Tool-surface search domains' not in prior
    assert 'Tool naming and discovery conventions' in candidate
    assert 'Tool-surface search domains' in candidate
    assert 'never blindly retry a mutation' in prior and 'never blindly retry a mutation' in candidate


def test_saved_prompts_allow_replaying_an_experiment_without_changing_runtime_policy():
    from pal.core.prompt import MinimalOperatingRulesPromptFragmentProvider
    from pal.shared import PromptAssemblyContext
    from pal.llm.serde import message_to_payload
    fragments = {f.section: f.content for f in
                 MinimalOperatingRulesPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext())}
    prior = {**fragments, 'tool_policy': 'Saved prior policy'}
    candidate = {**fragments, 'tool_policy': 'Saved candidate policy'}
    for variant, expected, excluded in [('prior_prompt', 'Saved prior policy', 'Saved candidate policy'),
                                        ('candidate', 'Saved candidate policy', 'Saved prior policy')]:
        text = str([message_to_payload(m) for m in benchmark.prompt_messages(variant, 'task', prior, candidate)])
        assert expected in text
        assert excluded not in text


def test_directory_contract_review_and_selection_are_intercepted(tmp_path):
    fx = benchmark.make_fixture(tmp_path)
    alias = 'restart_channel_endpoint'
    record = fx.runtime.registry_generation.record_for_alias(alias)
    calls = [new_tool_call(name='search_tools', args={'domain': 'channel'}),
             new_tool_call(name='read_tool', args={'name': alias}),
             new_tool_call(name='call_tool', args={'name': alias, 'args': {'name': 'demo_endpoint'}})]
    executed = []
    original = fx.runtime.execute_tool_async
    async def discovery_only(call, **kwargs):
        assert call.name in {'search_tools', 'read_tool', 'list_tools'}
        executed.append(call.name)
        return await original(call, **kwargs)
    fx.runtime.execute_tool_async = discovery_only
    class Model:
        async def agenerate(self, request):
            return generation_result_from_values(tool_calls=[calls.pop(0)],
                preferred_endpoint_id='fixture', preferred_model_id='fixture', usage_reported=True)
    try:
        row = asyncio.run(benchmark.run_case(Model(), {'endpoint_id': 'fixture', 'model_id': 'fixture'},
            fx, 'candidate', ('reconnect', 'Reconnect demo_endpoint.', alias), 1))
        assert row['valid'], row
        assert row['searches'][0]['hits'] == []
        assert alias in [x['alias'] for x in row['searches'][0]['directory']['tools']]
        assert executed == ['search_tools', 'read_tool']
    finally:
        fx.close()


def test_directory_metric_does_not_count_navigation_or_wrong_selection_as_success():
    from scripts.benchmark_directory_discovery import summarize
    rows = [dict(expected='restart_channel_endpoint', selected=False, valid=False,
                 selected_alias='attach_channel_endpoint', responses=[{}],
                 searches=[{'directory': {'tools': [{'alias': 'restart_channel_endpoint'}]}}]),
            dict(expected=None, selected=False, valid=False, selected_alias='send_channel_attachment',
                 responses=[{}]), dict(expected=None, valid=True, responses=[{}])]
    report = summarize(rows)
    assert report['directory_seen'] == 1
    assert report['correct_selected'] == report['correct_valid'] == 0
    assert report['wrong_selections'] == 2
    assert report['negative_selections'] == report['correct_abstentions'] == 1


def test_synthetic_preflight_allows_continuation_without_executing_handler(tmp_path):
    fx = benchmark.make_fixture(tmp_path)
    alias = 'reload_plugin'
    calls = [new_tool_call(name='read_tool', args={'name': alias}),
             new_tool_call(name='list_plugins', args={}),
             new_tool_call(name='call_tool', args={'name': alias, 'args': {'name': 'demo'}})]
    original = fx.runtime.execute_tool_async
    executed = []
    async def discovery_only(call, **kwargs):
        assert call.name == 'read_tool'
        executed.append(call.name)
        return await original(call, **kwargs)
    fx.runtime.execute_tool_async = discovery_only
    class Model:
        async def agenerate(self, request):
            return generation_result_from_values(tool_calls=[calls.pop(0)],
                preferred_endpoint_id='fixture', preferred_model_id='fixture', usage_reported=True)
    try:
        row = asyncio.run(benchmark.run_case(Model(), {'endpoint_id': 'fixture', 'model_id': 'fixture'},
            fx, 'candidate', ('reload', 'Reload demo.', alias), 1,
            intercepted_introspection={'list_plugins': {'items': [{'name': 'demo', 'plugin_id': 'demo', 'attached': True}]}}))
        assert row['valid'], row
        assert row['preflights'] == [{'alias': 'list_plugins', 'args': {}}]
        assert executed == ['read_tool']
    finally:
        fx.close()


def test_real_wrong_mode_affordance_recovers_without_running_business(tmp_path):
    fx = benchmark.make_fixture(tmp_path)
    calls = [new_tool_call(name='call_tool', args={'name': 'list_plugins', 'args': {}}),
             new_tool_call(name='list_plugins', args={}),
             new_tool_call(name='call_tool', args={'name': 'reload_plugin', 'args': {'name': 'demo'}})]
    original = fx.runtime.execute_tool_async
    executed = []
    async def rejection_only(call, **kwargs):
        assert call.name == 'call_tool' and call.args['name'] == 'list_plugins'
        executed.append(call.name)
        return await original(call, **kwargs)
    fx.runtime.execute_tool_async = rejection_only
    class Model:
        async def agenerate(self, request):
            if len(calls) == 2:
                assert request.messages[-1].parts[0].structured['error_code'] == 'wrong_invocation_mode'
                assert 'list_plugins' in [t.name for t in request.tools]
            return generation_result_from_values(tool_calls=[calls.pop(0)],
                preferred_endpoint_id='fixture', preferred_model_id='fixture', usage_reported=True)
    try:
        row = asyncio.run(benchmark.run_case(Model(), {'endpoint_id': 'fixture', 'model_id': 'fixture'},
            fx, 'candidate', ('reload', 'Reload demo.', 'reload_plugin'), 1, recover_errors=True,
            intercepted_introspection={'list_plugins': {'items': [{'name': 'demo'}]}}))
        assert row['valid'], row
        assert row['rejections'][0]['result']['effect'] == 'not_started'
        assert executed == ['call_tool']
    finally:
        fx.close()


def test_noop_attach_feedback_recovers_to_restart_without_running_handlers(tmp_path):
    from scripts.benchmark_directory_discovery import install_endpoint_state
    fx = benchmark.make_fixture(tmp_path)
    manager = install_endpoint_state(fx, tmp_path, attached=True)
    assert fx.runtime.registry_generation.record_for_alias('inspect_channel_endpoint') is not None
    transport = manager.runtime.get_endpoint('demo_endpoint')
    calls = [new_tool_call(name='call_tool', args={'name': 'attach_channel_endpoint', 'args': {'name': 'demo_endpoint'}}),
             new_tool_call(name='call_tool', args={'name': 'restart_channel_endpoint', 'args': {'name': 'demo_endpoint'}})]
    async def forbidden(*args, **kwargs):
        raise AssertionError('No business handler should execute')
    fx.runtime.execute_tool_async = forbidden
    class Model:
        async def agenerate(self, request):
            if len(calls) == 1:
                result = request.messages[-1].parts[0]
                assert result.structured['transport_restarted'] is False
                assert 'restart_channel_endpoint' in result.content
            return generation_result_from_values(tool_calls=[calls.pop(0)],
                preferred_endpoint_id='fixture', preferred_model_id='fixture', usage_reported=True)
    try:
        row = asyncio.run(benchmark.run_case(Model(), {'endpoint_id': 'fixture', 'model_id': 'fixture'},
            fx, 'candidate', ('restart', 'Repair still attached endpoint.', 'restart_channel_endpoint'), 1,
            recover_errors=True, intercepted_results={'attach_channel_endpoint': manager.attach_endpoint('demo_endpoint')}))
        assert row['valid'], row
        assert row['action_feedback'][0]['effect'] == 'none'
        assert manager.runtime.get_endpoint('demo_endpoint') is transport
    finally:
        fx.close()
