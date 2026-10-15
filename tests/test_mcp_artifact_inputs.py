"""Pending batch: MCP input adaptation stays local and server control stays scoped."""
from types import SimpleNamespace
from unittest.mock import Mock

import base64
import pytest
from jsonschema import Draft202012Validator

from pal.core import PalCore
from pal.core.module_registry import ModuleHandle
from pal.execution import register_with_core
from pal.execution.contracts import CapabilityCall
from pal.execution.tool_facade import EffectOutcome
from pal.mcp.artifact_inputs import McpArtifactInputError, McpArtifactInputs, resolve_artifact_input
from pal.mcp.compiler import McpCompiler
from pal.mcp.config import load_mcp_server_file
from pal.mcp.model import McpDiscoverySnapshot, McpToolSpec
from pal.mcp.plugin import McpManagerPluginProvider
from pal.shared import IntrospectionResult, RuntimeStatus
from pal.shared.tool_protocol import new_tool_call
from tests.test_mcp import FakeInvoker


SCHEMA = {'type': 'object', 'properties': {'image': {'type': 'string'}, 'caption': {'type': 'string'}},
          'required': ['image'], 'additionalProperties': False}


def projection(resolver, invoker, mode='path', schema=None):
    snapshot = McpDiscoverySnapshot(server_id='demo', transport='stdio',
        server_info={'pal_artifact_inputs': {'analyze': {'image': mode}}},
        tools=(McpToolSpec(name='analyze', input_schema=schema or SCHEMA),))
    return McpCompiler().compile(module_id='mcp', snapshots=(snapshot,), invoker=invoker, artifact_resolver=resolver)


def test_compiler_accepts_reference_but_only_dispatches_resolved_server_arguments():
    invoker = FakeInvoker()
    resolver = Mock(return_value='/owned/image.png')
    compiled = projection(resolver, invoker)
    descriptor = compiled.mounted_subtree.descriptors[0]
    args = {'image': {'artifact_id': 'a1'}, 'caption': 'look'}
    Draft202012Validator(descriptor.mcp_input_schema).validate(args)
    assert not Draft202012Validator(SCHEMA).is_valid(args)
    call = CapabilityCall(name=descriptor.name, args=args, meta={'turn_id': 'origin'})
    result = compiled.mounted_subtree.bound_actions[0].callable(call)
    assert result.status == RuntimeStatus.OK
    resolver.assert_called_once_with('a1', 'path', call)
    assert invoker.tool_calls == [('demo', 'analyze', {'image': '/owned/image.png', 'caption': 'look'})]
    assert args['image'] == {'artifact_id': 'a1'}
    assert '/owned/image.png' not in result.llm_text


def test_original_strings_pass_through_without_resolving():
    invoker = FakeInvoker()
    resolver = Mock(side_effect=AssertionError('plain input must not resolve artifacts'))
    compiled = projection(resolver, invoker)
    compiled.mounted_subtree.bound_actions[0].callable(CapabilityCall(name='analyze', args={'image': 'https://example.test/a.png'}))
    resolver.assert_not_called()
    assert invoker.tool_calls[0][2]['image'] == 'https://example.test/a.png'


@pytest.mark.parametrize('resolver', [None, Mock(side_effect=PermissionError('wrong scope'))])
def test_resolution_failure_rejects_before_external_dispatch(resolver):
    invoker = FakeInvoker()
    result = projection(resolver, invoker).mounted_subtree.bound_actions[0].callable(
        CapabilityCall(name='analyze', args={'image': {'artifact_id': 'a1'}}))
    assert result.status == RuntimeStatus.INVALID
    assert result.effect_receipt.outcome == EffectOutcome.NOT_STARTED
    assert result.structured['error_code'] == 'mcp_artifact_input_invalid'
    assert not invoker.tool_calls


def test_converted_value_must_satisfy_original_constraints_without_echoing_bytes():
    schema = {'type': 'object', 'properties': {'image': {'type': 'string', 'pattern': '^https://'}}, 'required': ['image']}
    invoker = FakeInvoker()
    result = projection(lambda *args: 'data:image/png;base64,SECRET_IMAGE_BYTES', invoker, 'url', schema).mounted_subtree.bound_actions[0].callable(
        CapabilityCall(name='analyze', args={'image': {'artifact_id': 'a1'}}))
    assert result.status == RuntimeStatus.INVALID
    assert 'SECRET_IMAGE_BYTES' not in result.llm_text
    assert not invoker.tool_calls


@pytest.mark.parametrize('field', ['missing', 'caption'])
def test_configuration_cannot_target_unknown_or_non_string_parameters(field):
    schema = {'type': 'object', 'properties': {'caption': {'type': 'integer'}}}
    with pytest.raises(ValueError, match='top-level string'):
        McpArtifactInputs(schema, {field: 'path'}).exposed_schema()


@pytest.mark.parametrize('mode', ['path', 'url', 'data_url', 'base64'])
def test_scoped_artifact_converts_without_reading_private_source_urls(tmp_path, mode):
    path = tmp_path / 'a.png'
    path.write_bytes(b'image-bytes')
    def info(artifact_id, scope):
        assert artifact_id == 'a1' and scope == 'conversation'
        return {'artifact': {'mime_type': 'image/png', 'metadata': {
            'source_url': 'https://private.test/SECRET_TOKEN',
            'local_file': {'preferred_path': str(path), 'preferred_mime_type': 'image/png'}}}}
    context = SimpleNamespace(execution_runtime=SimpleNamespace(turn_io=SimpleNamespace(
        artifact_scope_for_turn=lambda turn: 'conversation' if turn == 'origin' else None)),
        port_registry={'artifact:artifact': SimpleNamespace(info=info)})
    result = resolve_artifact_input(context, 'a1', mode, CapabilityCall(name='analyze', meta={'turn_id': 'origin'}))
    expected = str(path.resolve()) if mode == 'path' else base64.b64encode(path.read_bytes()).decode()
    assert result == (f'data:image/png;base64,{expected}' if mode in {'url', 'data_url'} else expected)
    assert 'SECRET_TOKEN' not in result
    with pytest.raises(McpArtifactInputError, match='scope_unavailable'):
        resolve_artifact_input(context, 'a1', mode, CapabilityCall(name='analyze', meta={'turn_id': 'other'}))
    context.port_registry['artifact:artifact'].info = Mock(side_effect=KeyError('artifact_not_found'))
    with pytest.raises(McpArtifactInputError, match='unavailable'):
        resolve_artifact_input(context, 'a1', mode, CapabilityCall(name='analyze', meta={'turn_id': 'origin'}))


def test_local_config_carries_exact_tool_and_parameter_mapping(tmp_path):
    path = tmp_path / 'demo.toml'
    path.write_text('server_id="demo"\ncommand="server"\n[artifact_inputs.analyze]\nimage="path"\n')
    config, = load_mcp_server_file(path)
    assert config.config.artifact_inputs == {'analyze': {'image': 'path'}}
    path.write_text('command="server"\n[artifact_inputs.analyze]\nimage="auto"\n')
    with pytest.raises(ValueError, match='representation|path, url'):
        load_mcp_server_file(path)


@pytest.fixture
def managed(tmp_path):
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    provider = McpManagerPluginProvider(runtime_root=tmp_path, core_context=core.context)
    core.context.register_module(ModuleHandle(module_id='mcp', tier='detachable', introspection_provider=provider))
    core.publish_module_capabilities('mcp')
    yield core.context.execution_runtime, provider
    core.context.execution_runtime.shutdown()


def test_five_management_entries_and_retired_tools_are_unavailable(managed):
    runtime, _ = managed
    generation = runtime.registry_generation
    assert {name for name, record in generation.search_records.items() if record['module_id'] == 'mcp'} == {
        'inspect_mcp_state', 'list_mcp_servers', 'read_mcp_server', 'rescan_mcp_servers', 'manage_mcp_server'}
    spec = runtime.get_tool_spec('manage_mcp_server')['input_schema']
    assert set(spec['properties']) == set(spec['required']) == {'name', 'operation'}
    assert set(spec['properties']['operation']['enum']) == {'attach', 'detach'}
    for alias in ('attach_mcp_server', 'detach_mcp_server', 'prepare_mcp_image'):
        assert generation.record_for_alias(alias) is None
        assert not runtime.execute_tool(new_tool_call(name='read_tool', args={'name': alias})).ok
        assert not runtime.execute_tool(new_tool_call(name='call_tool', args={'name': alias, 'args': {'name': 'demo'}})).ok


@pytest.mark.parametrize('operation', ['attach', 'detach'])
def test_manage_dispatches_only_selected_existing_handler(managed, monkeypatch, operation):
    runtime, provider = managed
    for name in ('attach', 'detach'):
        monkeypatch.setattr(provider, name + '_server', Mock(return_value=IntrospectionResult(
            status=RuntimeStatus.OK, text=name, llm_text=name, structured={'operation': name})))
    result = runtime.execute_tool(new_tool_call(name='call_tool', args={
        'name': 'manage_mcp_server', 'args': {'name': 'demo', 'operation': operation}}))
    assert result.ok and result.structured['operation'] == operation
    getattr(provider, operation + '_server').assert_called_once()
    getattr(provider, ('detach' if operation == 'attach' else 'attach') + '_server').assert_not_called()


@pytest.mark.parametrize('args', [{'name': 'demo'}, {'name': 'demo', 'operation': 'restart'},
    {'name': ' ', 'operation': 'attach'}, {'name': 'demo', 'operation': 'attach', 'enabled': True}])
def test_invalid_management_inputs_do_not_reach_handlers(managed, monkeypatch, args):
    runtime, provider = managed
    attach, detach = Mock(), Mock()
    monkeypatch.setattr(provider, 'attach_server', attach)
    monkeypatch.setattr(provider, 'detach_server', detach)
    assert not runtime.execute_tool(new_tool_call(name='call_tool', args={'name': 'manage_mcp_server', 'args': args})).ok
    attach.assert_not_called()
    detach.assert_not_called()


def test_reference_validates_through_public_facade_and_keeps_original_call_small(managed):
    runtime, _ = managed
    invoker = FakeInvoker()
    compiled = projection(lambda *args: 'data:image/png;base64,ENCODED_BYTES', invoker, 'url')
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=compiled.mounted_subtree))
    result = runtime.execute_tool(new_tool_call(name='call_tool', args={
        'name': 'call_mcp_demo_analyze', 'args': {'image': {'artifact_id': 'a1'}}}))
    assert result.ok, result.llm_text
    assert invoker.tool_calls[0][2] == {'image': 'data:image/png;base64,ENCODED_BYTES'}
    assert 'ENCODED_BYTES' not in result.llm_text


def test_inline_conversion_limit_is_checked_before_dispatch(tmp_path, monkeypatch):
    from pal.mcp import artifact_inputs
    path = tmp_path / 'image.png'
    path.write_bytes(b'too big')
    monkeypatch.setattr(artifact_inputs, '_MAX_ENCODED_SOURCE_BYTES', 3)
    service = SimpleNamespace(info=lambda *args: {'artifact': {'metadata': {'local_file': {'preferred_path': str(path)}}}})
    context = SimpleNamespace(execution_runtime=SimpleNamespace(turn_io=SimpleNamespace(artifact_scope_for_turn=lambda turn: 'scope')),
        port_registry={'artifact:artifact': service})
    with pytest.raises(McpArtifactInputError, match='inline conversion limit'):
        resolve_artifact_input(context, 'a1', 'base64', CapabilityCall(name='analyze'))
    assert resolve_artifact_input(context, 'a1', 'path', CapabilityCall(name='analyze')) == str(path.resolve())
