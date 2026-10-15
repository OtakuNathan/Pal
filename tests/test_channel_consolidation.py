"""One channel control facade preserves operation and lifecycle boundaries."""
from unittest.mock import Mock

import pytest

from pal.shared import IntrospectionCall, IntrospectionResult, RuntimeStatus
from pal.shared.tool_protocol import new_tool_call
from tests.test_channel_proactive_error_fidelity import channel_env, invoke
from tests.test_channel_attach_idempotence import env as endpoint_env


RETIRED = {"attach_channel_endpoint", "detach_channel_endpoint", "restart_channel_endpoint",
           "enable_channel_endpoint", "disable_channel_endpoint"}


def test_channel_surface_has_seven_indirect_entries_and_no_legacy_aliases(channel_env):
    core, channel, _ = channel_env
    channel.publish_endpoint_when_ready("channel-main")
    core.publish_module_capabilities("channel")
    runtime = core.context.execution_runtime
    generation = runtime.registry_generation
    names = {name for name, record in generation.search_records.items() if record['module_id'] == 'channel'}
    assert names == {'manage_channel_endpoint', 'inspect_channel_state', 'list_channel_endpoints',
                     'inspect_channel_endpoint', 'set_channel_endpoint_auth_material',
                     'send_channel_message', 'send_channel_attachment'}
    assert not names.intersection(generation.direct_aliases)
    spec = runtime.get_tool_spec('manage_channel_endpoint')['input_schema']
    assert set(spec['properties']) == {'name', 'operation'}
    assert set(spec['required']) == {'name', 'operation'}
    assert set(spec['properties']['operation']['enum']) == {'attach', 'detach', 'restart', 'enable', 'disable'}
    for name in RETIRED:
        assert generation.record_for_alias(name) is None
        assert name not in generation.provider_specs
        assert runtime.invoke_direct_tool(new_tool_call(name='read_tool', args={'name': name})).kind != 'complete'
        assert runtime.invoke_indirect_tool(new_tool_call(name=name, args={'name': 'channel-main'})).error_code == 'unknown_tool'
    assert runtime.invoke_direct_tool(new_tool_call(name='manage_channel_endpoint',
        args={'name': 'channel-main', 'operation': 'restart'})).error_code == 'wrong_invocation_mode'


@pytest.mark.parametrize('operation,method', [('attach', 'attach'), ('detach', 'detach'),
    ('restart', 'restart_endpoint'), ('enable', 'enable'), ('disable', 'disable')])
def test_facade_dispatches_only_the_requested_operation(channel_env, monkeypatch, operation, method):
    core, _, _ = channel_env
    provider = core.context.module_registry.require('channel').introspection_provider
    handlers = {}
    for name in ('attach', 'detach', 'restart_endpoint', 'enable', 'disable'):
        handler = Mock(return_value=IntrospectionResult(status=RuntimeStatus.OK,
            text=name, structured={'chosen': name}, llm_text=name))
        monkeypatch.setattr(provider, name, handler)
        handlers[name] = handler
    result = invoke(core.context.execution_runtime, 'manage_channel_endpoint',
                    {'name': 'channel-main', 'operation': operation})
    assert result.ok and result.structured['chosen'] == method
    handlers[method].assert_called_once()
    assert handlers[method].call_args.args[0].args['name'] == 'channel-main'
    assert all(not handler.called for name, handler in handlers.items() if name != method)


@pytest.mark.parametrize('args', [
    {'name': 'channel-main'}, {'name': 'channel-main', 'operation': 'reload'},
    {'name': ' ', 'operation': 'restart'}, {'name': 'channel-main', 'operation': 'restart', 'enabled': True},
])
def test_invalid_or_combined_operations_are_rejected_before_dispatch(channel_env, monkeypatch, args):
    core, _, _ = channel_env
    provider = core.context.module_registry.require('channel').introspection_provider
    dispatch = Mock(side_effect=AssertionError('invalid input reached owner'))
    monkeypatch.setattr(provider, '_restart_endpoint', dispatch)
    result = invoke(core.context.execution_runtime, 'manage_channel_endpoint', args)
    assert not result.ok
    assert result.structured['kind'] == 'rejected'
    dispatch.assert_not_called()


def test_enabled_state_does_not_replace_transport_and_rolls_back_on_persistence_failure(endpoint_env):
    env = endpoint_env
    env.manager.attach_endpoint('demo')
    endpoint = env.runtime.get_endpoint('demo')
    def persist(name, enabled):
        env.record.enabled = enabled
        return env.record
    env.provider.repository.set_enabled = persist
    result = env.provider.manage_endpoint(IntrospectionCall(name='manage_channel_endpoint',
        args={'name': 'demo', 'operation': 'disable'}))
    assert result.status == 'ok'
    assert not env.record.enabled and not endpoint.enabled
    assert env.runtime.get_endpoint('demo') is endpoint
    def fail(name, enabled):
        raise OSError('durable state unavailable')
    env.provider.repository.set_enabled = fail
    result = env.provider.manage_endpoint(IntrospectionCall(name='manage_channel_endpoint',
        args={'name': 'demo', 'operation': 'enable'}))
    assert result.status == 'error'
    assert not endpoint.enabled and not env.record.enabled
    assert env.runtime.get_endpoint('demo') is endpoint


@pytest.mark.parametrize('operation', ['disable', 'detach'])
def test_recovery_socket_remains_protected(endpoint_env, operation):
    from pal.channel.provider_manager import recovery_socket_path
    env = endpoint_env
    env.manager.attach_endpoint('demo')
    endpoint = env.runtime.get_endpoint('demo')
    env.record.channel_kind = 'socket'
    env.record.binding_key = str(recovery_socket_path(env.manager.runtime_root))
    env.provider.runtime_root = env.manager.runtime_root
    before = env.runtime.get_endpoint_hub('demo').snapshot
    result = env.provider.manage_endpoint(IntrospectionCall(name='manage_channel_endpoint',
        args={'name': 'demo', 'operation': operation}))
    assert result.status == 'invalid'
    assert result.structured['reason'] == 'recovery_socket_control_channel'
    assert env.runtime.get_endpoint('demo') is endpoint
    assert endpoint.enabled and endpoint.attached
    assert env.runtime.get_endpoint_hub('demo').snapshot == before
