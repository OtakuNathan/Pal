"""Pending batch: unified workflow control preserves owner and routing boundaries."""
from unittest.mock import Mock

import pytest

from pal.bunshin.workflow_capabilities import BunshinPublicProvider
from pal.core import PalCore
from pal.core.module_registry import ModuleHandle
from pal.execution import register_with_core
from pal.execution.tool_facade import EffectKind
from pal.shared.tool_protocol import new_tool_call


@pytest.fixture
def env(tmp_path):
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    provider = BunshinPublicProvider(tmp_path, context=core.context, wake_manager=Mock())
    core.context.register_module(ModuleHandle(module_id='bunshin', tier='detachable', introspection_provider=provider))
    core.publish_module_capabilities('bunshin')
    yield core.context.execution_runtime, provider
    core.context.execution_runtime.shutdown()


def invoke(runtime, alias, args):
    return runtime.execute_tool(new_tool_call(name='call_tool', args={'name': alias, 'args': args}))


def test_eleven_tools_and_only_start_remains_direct(env):
    runtime, _ = env
    generation = runtime.registry_generation
    names = {name for name, record in generation.search_records.items() if record['module_id'] == 'bunshin'}
    assert len(names) == 11
    assert names.intersection(generation.direct_aliases) == {'start_bunshin_workflow'}
    assert 'rebind_bunshin_task_delivery' in generation.indirect_aliases
    assert generation.record_for_alias('rebind_bunshin_task_delivery').execution.effect_kind is EffectKind.CONTROL
    for alias in ('resume_bunshin_workflow', 'archive_bunshin_workflow'):
        assert generation.record_for_alias(alias) is None
        assert alias not in generation.provider_specs
        assert not runtime.execute_tool(new_tool_call(name='read_tool', args={'name': alias})).ok
        assert not invoke(runtime, alias, {'task': 'demo'}).ok
    spec = runtime.get_tool_spec('control_bunshin_workflow')['input_schema']
    assert set(spec['properties']) == {'task', 'operation', 'reason'}
    assert spec['required'] == ['operation']
    assert set(spec['properties']['operation']['enum']) == {'pause', 'resume', 'cancel', 'archive'}
    assert {'restart_bunshin_execution', 'resolve_bunshin_triage'} <= names


@pytest.mark.parametrize('operation', ['pause', 'resume', 'cancel', 'archive'])
def test_control_selects_correct_owner_and_workflow_scope(env, monkeypatch, operation):
    runtime, provider = env
    selector = Mock(return_value=('task-id', 'workflow-id'))
    monkeypatch.setattr(provider.service, 'resolve_task_workflow_selector', selector)
    controls = {}
    for method, payload in [('control_workflow', {'status': 'accepted'}),
                            ('resume_workflow', {'status': 'triage_requires_resolution', 'triage': [{'subject': 'unit:demo'}]}),
                            ('archive_workflow', {'status': 'archived', 'already_archived': False, 'archive_scope': 'workflow'})]:
        handler = Mock(return_value=payload)
        monkeypatch.setattr(provider.service, method, handler)
        controls[method] = handler
    result = invoke(runtime, 'control_bunshin_workflow', {'task': 'demo', 'operation': operation})
    assert result.ok, result.llm_text
    assert selector.call_args.kwargs['include_terminal'] is (operation == 'archive')
    method = {'pause': 'control_workflow', 'cancel': 'control_workflow',
              'resume': 'resume_workflow', 'archive': 'archive_workflow'}[operation]
    controls[method].assert_called_once()
    assert all(not handler.called for name, handler in controls.items() if name != method)
    if operation in {'pause', 'cancel'}:
        assert controls[method].call_args.kwargs['command'] == operation
    if operation == 'resume':
        assert result.structured['status'] == 'triage_requires_resolution'
        assert result.structured['triage'] == [{'subject': 'unit:demo'}]
    assert provider.wake_manager.called is (operation != 'archive')


def test_repeat_archive_preserves_no_effect_receipt(env, monkeypatch):
    runtime, provider = env
    monkeypatch.setattr(provider.service, 'resolve_task_workflow_selector', lambda **kw: ('task-id', 'workflow-id'))
    monkeypatch.setattr(provider.service, 'archive_workflow', lambda **kw: {
        'status': 'archived', 'already_archived': True, 'archived': True, 'archive_scope': 'workflow'})
    result = invoke(runtime, 'control_bunshin_workflow', {'task': 'demo', 'operation': 'archive'})
    assert result.ok, result.llm_text
    assert result.invocation_result.effect.value == 'none'
    assert result.structured['already_archived'] is True


@pytest.mark.parametrize('args', [{}, {'task': 'demo', 'command': 'pause'},
    {'task': 'demo', 'operation': 'restart'}, {'task': 'demo', 'operation': 'resume', 'subject': 'unit:demo'}])
def test_invalid_control_does_not_select_or_mutate_workflow(env, monkeypatch, args):
    runtime, provider = env
    selector = Mock(side_effect=AssertionError('invalid request reached owner'))
    monkeypatch.setattr(provider.service, 'resolve_task_workflow_selector', selector)
    assert not invoke(runtime, 'control_bunshin_workflow', args).ok
    selector.assert_not_called()


def test_rebind_is_indirect_and_still_only_changes_delivery(env, monkeypatch):
    runtime, provider = env
    monkeypatch.setattr(provider.service, 'resolve_task_selector', lambda **kw: 'task-id')
    monkeypatch.setattr(provider, '_resolve_rebind_binding', lambda call, name: {'channel_id': name})
    request = Mock(return_value={'changed': True})
    monkeypatch.setattr(provider, '_request_manager', request)
    args = {'task': 'demo', 'channel_name': 'telegram_main'}
    wrong_mode = runtime.invoke_direct_tool(new_tool_call(name='rebind_bunshin_task_delivery', args=args))
    assert wrong_mode.error_code == 'wrong_invocation_mode'
    request.assert_not_called()
    rebound = invoke(runtime, 'rebind_bunshin_task_delivery', args)
    assert rebound.ok, rebound.llm_text
    request.assert_called_once_with('v2_rebind_task_delivery', {'task_id': 'task-id', 'binding': {'channel_id': 'telegram_main'}})
    assert rebound.structured['channel_name'] == 'telegram_main'
