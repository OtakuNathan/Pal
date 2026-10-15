"""Attach preserves a live transport; restart alone requests replacement."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from pal.channel import ChannelEndpointQueueBase, ChannelRuntime, EndpointConfig
from pal.channel.capabilities import ChannelIntrospectionProvider
from pal.channel.provider_manager import ChannelEndpointProviderManager, FactoryChannelProvider
from pal.execution.tool_facade import EffectOutcome


class Endpoint(ChannelEndpointQueueBase):
    def normalize_raw(self, payload): return {}
    def send_reply(self, response_handle, text): pass
    def inspect_health(self): return {'healthy': False}
    def inspect_auth_state(self): return {'authorized': True}


@pytest.fixture
def env(tmp_path):
    record = SimpleNamespace(endpoint_id='demo', channel_kind='demo', binding_key='fixture',
                             enabled=True, detached_at=None)
    def set_attached(_, attached):
        record.detached_at = None if attached else 'detached'
        return record
    repo = SimpleNamespace(get=lambda _: record, set_attached=set_attached, list_all=lambda: [record])
    runtime = ChannelRuntime()
    made = []
    def create(row, *, runtime_root):
        endpoint = Endpoint(endpoint=EndpointConfig(endpoint_id=row.endpoint_id,
                            channel_kind=row.channel_kind, binding_key=row.binding_key))
        made.append(endpoint)
        return endpoint
    manager = ChannelEndpointProviderManager(runtime, repo, tmp_path)
    manager.register_provider(FactoryChannelProvider('demo', ('demo',), SimpleNamespace(create=create)))
    provider = ChannelIntrospectionProvider(runtime=runtime, repository=repo, provider_manager=manager)
    yield SimpleNamespace(manager=manager, runtime=runtime, made=made, provider=provider, record=record)


def test_repeated_attach_preserves_transport_and_explains_it_did_not_restart(env):
    assert env.manager.attach_endpoint('demo').status == 'ok'
    transport = env.runtime.get_endpoint('demo')
    hub = env.runtime.get_endpoint_hub('demo')
    epoch = hub.transition_epoch
    result = env.provider._attach_endpoint_provider('demo')
    assert result.status == 'ok'
    assert result.structured['changed'] is False
    assert result.structured['transport_restarted'] is False
    assert result.effect_receipt.outcome == EffectOutcome.NONE
    assert len(env.made) == 1 and env.runtime.get_endpoint('demo') is transport
    assert hub.transition_epoch == epoch
    assert [a.arguments['name'] for a in result.affordances] == [
        'inspect_channel_endpoint', 'manage_channel_endpoint']
    assert all(a.arguments['args']['name'] == 'demo' for a in result.affordances)
    assert result.affordances[1].arguments['args']['operation'] == 'restart'
    assert 'does not establish' in result.llm_text


def test_restart_replaces_transport_but_detach_then_attach_builds_a_new_one(env):
    env.manager.attach_endpoint('demo')
    old = env.runtime.get_endpoint('demo')
    assert env.manager.restart_endpoint('demo').status == 'ok'
    assert env.runtime.get_endpoint('demo') is not old
    assert len(env.made) == 2
    assert env.manager.detach_endpoint('demo').status == 'ok'
    assert env.record.detached_at is not None
    assert env.manager.attach_endpoint('demo').status == 'ok'
    assert env.record.detached_at is None
    assert len(env.made) == 3


def test_core_owner_attach_reports_preserved_instance_and_reload_replaces_it(env):
    env.manager.attach_endpoint('demo')
    old = env.runtime.get_endpoint('demo')
    result = env.provider.attach_module('channel.endpoint:demo')
    assert result.status == 'ok' and not result.fresh_instance
    assert env.runtime.get_endpoint('demo') is old
    result = env.provider.reload_module('channel.endpoint:demo')
    assert result.status == 'ok' and result.fresh_instance
    assert env.runtime.get_endpoint('demo') is not old


def test_idempotent_attach_does_not_hide_pending_provider_cleanup(env):
    env.manager.attach_endpoint('demo')
    old = env.runtime.get_endpoint('demo')
    env.manager.runtime_provider_handles['demo'] = SimpleNamespace(cleanup_started=True)
    result = env.manager.attach_endpoint('demo')
    assert result.status != 'ok'
    assert env.runtime.get_endpoint('demo') is old
    assert len(env.made) == 1


def test_public_facade_preserves_noop_facts_and_conditional_affordances(tmp_path):
    import asyncio
    from scripts.benchmark_discovery_flow import make_fixture
    from scripts.benchmark_directory_discovery import install_endpoint_state
    from pal.shared.tool_protocol import new_tool_call
    fx = make_fixture(tmp_path)
    try:
        manager = install_endpoint_state(fx, tmp_path, attached=True)
        old = manager.runtime.get_endpoint('demo_endpoint')
        result = asyncio.run(fx.runtime.execute_tool_async(new_tool_call(name='call_tool',
            args={'name': 'manage_channel_endpoint', 'args': {'name': 'demo_endpoint', 'operation': 'attach'}})))
        assert result.ok, result
        assert result.structured['changed'] is False
        assert result.structured['transport_restarted'] is False
        assert manager.runtime.get_endpoint('demo_endpoint') is old
        assert 'manage_channel_endpoint' in result.llm_text
        assert 'inspect_channel_endpoint' in result.llm_text
        assert '"effect":"none"' in result.llm_text
    finally:
        fx.close()
