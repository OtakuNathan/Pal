"""Public invocation, discovery and delivery regressions from the 2026-10-09 audit."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest

from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
from pal.bunshin.runner_components.artifacts import Artifacts
from pal.bunshin.runner_components.completion import Completion
from pal.bunshin.runner_components.text_deliverables import TextDeliverables
from pal.core import PalCore
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput, StrictToolModel, ToolGuidance
from pal.mcp.compiler import McpCompiler
from pal.mcp.connector import AsyncStdioMcpConnector
from pal.mcp.manager import McpManager
from pal.mcp.ipc import mcp_config_root
from pal.mcp.model import McpDiscoverySnapshot, McpToolSpec, McpServerConfig, McpProtocolError
from pal.mcp.plugin import _introspection_from_rpc
from pal.shared import BunshinInvocationPack
from pal.shared.tool_protocol import new_tool_call
from tests.capability_fixture import mount_test_capability


@pytest.fixture
def runtime():
    runtime = ExecutionRuntime()
    yield runtime
    runtime.shutdown()


@pytest.fixture
def scoped(runtime, tmp_path):
    workspace = {'artifact_dir': str(tmp_path / 'final'), 'artifact_stage_dir': str(tmp_path / 'stage'),
                 'invocation_id': 'audit'}
    scoped = BunshinScopedExecutionRuntime(base_runtime=runtime, workspace=workspace,
        allowed_capabilities=['op_bunshin_artifact_write', 'op_bunshin_artifact_edit'])
    yield scoped
    scoped.base_runtime.runtime.shutdown()


def artifact_call(scoped, name='write_workflow_artifact', **args):
    return asyncio.run(scoped.execute_tool_async(new_tool_call(name=name, args=args), turn_id='audit'))


@pytest.mark.parametrize('required', [False, True])
def test_artifact_public_call_survives_completion_fallback_and_promotion(scoped, tmp_path, required):
    if required:
        scoped.workspace['output_policy'] = {'primary_artifact': 'report.json'}
    result = artifact_call(scoped, relative_path='report.json', content='{"real":true}')
    assert result.ok, result.llm_text
    edited = artifact_call(scoped, 'edit_workflow_artifact', relative_path='report.json', content='\nsecond', operation='append')
    assert edited.ok, edited.llm_text
    pack = BunshinInvocationPack(invocation_id='audit', workspace=scoped.workspace)
    artifacts = Artifacts(pack, scoped.produced_artifacts)
    assert Completion(artifacts, pack, tmp_path).completion_evidence_present()
    fallback = TextDeliverables(artifacts, SimpleNamespace(policy_from_workspace_or_profile=lambda _: {}), 'worker', pack, 'run')
    asyncio.run(fallback.persist_text_deliverable_if_needed('summary only'))
    payload = artifacts.artifact_payload()
    assert Path(payload['primary_artifact']['path']).read_text() == '{"real":true}\nsecond'
    assert not list((tmp_path / 'final').glob('invocation*'))


def test_artifact_cleanup_preserves_unregistered_evidence(scoped, tmp_path):
    assert artifact_call(scoped, relative_path='report.txt', content='kept').ok
    (tmp_path / 'stage' / 'orphan.txt').write_text('unregistered evidence')
    Artifacts(BunshinInvocationPack(invocation_id="audit", workspace=scoped.workspace), scoped.produced_artifacts).artifact_payload()
    assert (tmp_path / 'stage' / 'orphan.txt').read_text() == 'unregistered evidence'
    assert not (tmp_path / 'stage' / 'report.txt').exists()


def test_artifact_public_schema_rejects_unsupported_type_and_explains_collision(scoped, tmp_path):
    bad = artifact_call(scoped, relative_path='report.txt', content='x', artifact_type='text')
    assert not bad.ok
    assert not (tmp_path / 'stage' / 'report.txt').exists()
    for text in ('first', 'second'):
        assert artifact_call(scoped, relative_path='report.txt', content=text).ok
    assert (tmp_path / 'stage' / 'report.txt').read_text() == 'first'
    assert (tmp_path / 'stage' / 'report_2.txt').read_text() == 'second'


def test_artifact_postwrite_failure_preserves_cause_and_effect(scoped, tmp_path, monkeypatch):
    original_read = Path.read_text
    def fail(path, *args, **kwargs):
        if path.name != "report.txt":
            return original_read(path, *args, **kwargs)
        try:
            raise OSError('metadata root cause')
        except OSError as exc:
            raise RuntimeError('metadata failed') from exc
    with monkeypatch.context() as patch:
        patch.setattr(Path, 'read_text', fail)
        result = artifact_call(scoped, relative_path='report.txt', content='written once')
    assert not result.ok
    assert result.invocation_result.effect.value == 'applied'
    assert 'metadata root cause' in result.llm_text
    assert 'before retrying' in result.llm_text
    assert (tmp_path / 'stage' / 'report.txt').read_text() == 'written once'


class MultiplexInput(StrictToolModel):
    operation: Literal['list', 'close', 'select']
    format: Literal['json', 'text'] = 'json'


def mount_search(runtime, alias='manage_browser_tabs', **guidance):
    return mount_test_capability(runtime, alias=alias, canonical_path='op_test_' + alias,
        InputModel=MultiplexInput, OutputModel=EmptyToolOutput, handler=lambda _: {},
        examples=({"operation": "list"},),
        guidance=ToolGuidance(purpose='Tabs', use_when='Requested', do_not_use_when='Other', **guidance))


def search(runtime, query):
    return runtime._search_generation(runtime.registry_generation, {'query': query, 'top_k': 1})['hits']


def test_explicit_synonyms_and_selected_enums_without_arbitrary_values(runtime):
    mount_search(runtime, search_terms=('tab', 'tabs', 'switch'), search_enum_fields=('operation',))
    for query in ('browser close tabs', 'browser list tab', 'switch tabs'):
        assert search(runtime, query)[0]['alias'] == 'manage_browser_tabs'
    assert search(runtime, 'browser json tabs') == []
    assert search(runtime, 'browser closes tabs') == []
    assert 'search_terms' not in json.dumps(search(runtime, 'browser tabs'))
    assert 'search_terms' not in json.dumps(runtime.list_tool_specs())


def test_raw_exact_alias_wins_case_collision(runtime):
    for alias in ('READ_WIDGET', 'read_widget'):
        mount_search(runtime, alias)
    assert search(runtime, 'read_widget')[0]['alias'] == 'read_widget'
    assert search(runtime, 'READ_WIDGET')[0]['alias'] == 'READ_WIDGET'


def test_search_vocabulary_changes_fingerprint_without_mutating_old_generation(runtime):
    mount_search(runtime, search_terms=('tab',))
    old = runtime.registry_generation
    mount_search(runtime, search_terms=('tab', 'pages'))
    assert old.generation_hash != runtime.registry_generation.generation_hash
    assert runtime._search_generation(old, {'query': 'pages'})['hits'] == []
    assert search(runtime, 'pages')


@pytest.mark.parametrize('raw', [{}, {'error': 'failed'}, {'content': [{'type': 'text'}]},
    {'content': None}, {'content': [], 'isError': 'false'}])
def test_malformed_mcp_result_is_unknown_failure_through_public_invocation(runtime, raw):
    invoker = SimpleNamespace(call_tool=lambda *args: raw)
    projection = McpCompiler().compile(module_id='mcp', snapshots=(McpDiscoverySnapshot(
        server_id='audit', transport='stdio', tools=(McpToolSpec(name='write', input_schema={'type': 'object'}),)),), invoker=invoker)
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=projection.mounted_subtree))
    result = runtime.invoke_indirect_tool(new_tool_call(name='call_mcp_audit_write', args={}))
    assert result.effect.value == 'unknown'
    assert result.__class__.__name__ == 'FailedResult'
    assert 'mcp_protocol_error' in str(result)


def test_mcp_normalizer_keeps_validator_and_error_type_in_one_generation():
    # Isolate deliberate import eviction from the rest of this test process.
    script = """
import importlib
import sys
from pal.mcp.normalize import normalize_tool_result, normalize_prompt_result, normalize_tool_payload
old_model = importlib.import_module('pal.mcp.model')
for name in ('pal.mcp.protocol', 'pal.mcp.model'):
    sys.modules.pop(name, None)
new_protocol = importlib.import_module('pal.mcp.protocol')
assert new_protocol.McpProtocolError is not old_model.McpProtocolError
for normalize, name in ((normalize_tool_result, 'tool_name'),
                        (normalize_prompt_result, 'prompt_name')):
    result = normalize({}, server_id='audit', **{name: 'probe'})
    assert result.status == 'error'
    assert result.structured['error_code'] == 'mcp_protocol_error'
try:
    normalize_tool_payload({'name': 'bad', 'inputSchema': 'not-a-schema'})
except old_model.McpProtocolError:
    pass
else:
    raise AssertionError('invalid tool schema was accepted')
"""
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('raw', [{'content': []}, {'content': [{'type': 'text', 'text': ''}]},
    {'content': [], 'structuredContent': {'error': 'a business field'}}])
def test_valid_empty_mcp_result_remains_success(raw):
    from pal.mcp.normalize import normalize_tool_result
    assert normalize_tool_result(raw, server_id='s', tool_name='t').status == 'ok'


@pytest.mark.parametrize('envelope', [{}, {'result': None}, {'result': {}, 'error': {'message': 'bad'}}])
def test_connector_rejects_malformed_envelope(envelope):
    async def run():
        connector = AsyncStdioMcpConnector(McpServerConfig(server_id='audit', command=('unused',)))
        async def write(payload):
            connector._pending[payload['id']].set_result({'jsonrpc': '2.0', 'id': payload['id'], **envelope})
        connector._write = write
        with pytest.raises(McpProtocolError) as error:
            await connector.call_tool('write', {})
        assert error.value.payload['raw_response'] == {'jsonrpc': '2.0', 'id': 1, **envelope}
        assert not connector._pending
    asyncio.run(run())


def test_failed_real_mcp_process_is_visible_in_rescan_attach_and_read(tmp_path):
    root = mcp_config_root(tmp_path)
    root.mkdir(parents=True)
    (root / 'broken.json').write_text(json.dumps({'command': [sys.executable, '-c',
        'import sys; print("configuration root cause", file=sys.stderr); sys.exit(7)']}))
    async def run():
        manager = McpManager(tmp_path)
        try:
            scan = await manager.rescan()
            assert scan['ok'] is False and scan['errors']
            result = await manager.attach_server('broken')
            assert result['attached'] is False and result['status'] == 'error'
            assert _introspection_from_rpc('attach', result).status == 'error'
            details = manager.read_server('broken')['failure_details']
            assert 'configuration root cause' in Path(details['stderr_path']).read_text()
            assert details['exit_code'] == 7
        finally:
            await manager.close_all()
    asyncio.run(run())


@pytest.mark.parametrize('name', ['read_file', 'readFile'])
def test_dynamic_mcp_names_keep_operation_and_plural_recall(runtime, name):
    calls = []
    server = 'a_very_long_server_' * 6 + 'archive'
    invoker = SimpleNamespace(call_tool=lambda *args: calls.append(args) or {'content': []})
    projection = McpCompiler().compile(module_id='mcp', snapshots=(McpDiscoverySnapshot(
        server_id=server, transport='stdio', tools=(McpToolSpec(name=name, input_schema={'type': 'object'}),)),), invoker=invoker)
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=projection.mounted_subtree))
    alias = search(runtime, 'archive read files')[0]['alias']
    assert 'read_file' in alias and len(alias) <= 64
    result = runtime.invoke_indirect_tool(new_tool_call(name=alias, args={}))
    assert result.__class__.__name__ == 'CompleteResult'
    assert calls == [(server, name, {})]


def test_core_configuration_is_mounted_and_callable():
    from pal.core.capabilities import register_with_core
    core = PalCore()
    from tests.test_cache_warm_deadline import _Settings
    settings = _Settings()
    core.cache_warm_deadline.settings_provider = lambda: settings
    register_with_core(core)
    core.publish_module_capabilities('core')
    runtime = core.context.execution_runtime
    try:
        result = runtime.invoke_indirect_tool(new_tool_call(name='configure_core', args={'mode': 'audit'}))
        assert result.__class__.__name__ == 'CompleteResult'
        assert core.state.mode == 'audit'
        result = runtime.invoke_indirect_tool(new_tool_call(name='configure_core_cache_warm_deadline', args={'enabled': False}))
        assert result.__class__.__name__ == 'CompleteResult'
    finally:
        runtime.shutdown()


def test_real_browser_multiplex_actions_are_discoverable():
    from tests.test_browser_cli_plugin import _FakeManager
    from pal.web_fetch import WebFetchService, register_with_core
    core = PalCore()
    register_with_core(core.context, WebFetchService(browser_manager=_FakeManager()))
    core.publish_module_capabilities('web_fetch')
    runtime = core.context.execution_runtime
    try:
        for query, alias in (
            ('browser list tabs', 'manage_browser_tabs'), ('browser close tabs', 'manage_browser_tabs'),
            ('browser select tab', 'manage_browser_tabs'), ('browser dismiss dialog', 'handle_browser_dialog'),
            ('browser reload page', 'navigate_browser_history'), ('browser read requests', 'manage_browser_network_capture'),
        ):
            assert search(runtime, query)[0]['alias'] == alias
    finally:
        runtime.shutdown()


def test_mcp_local_guidance_override_survives_projection(runtime):
    snapshot = McpDiscoverySnapshot(server_id='audit', transport='stdio', server_info={
        'pal_tool_guidance': {'lookup': {'search_terms': ['file', 'files', 'read'],
            'search_enum_fields': ['operation'], 'use_when': 'Read reviewed audit records.'}}},
        tools=(McpToolSpec(name='lookup', input_schema={'type': 'object', 'properties': {
            'operation': {'type': 'string', 'enum': ['inspect']}}}),))
    projection = McpCompiler().compile(module_id='mcp', snapshots=(snapshot,), invoker=SimpleNamespace())
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=projection.mounted_subtree))
    hit = search(runtime, 'inspect files')[0]
    assert hit['alias'] == 'call_mcp_audit_lookup'
    assert hit['use_when'] == 'Read reviewed audit records.'


def test_rescan_partial_failure_and_cleanup_failure_keep_original_diagnostics(tmp_path, monkeypatch):
    from pal.mcp import manager as module
    instances = []
    class Connector:
        server_info = {}
        server_capabilities = {}
        def __init__(self, config):
            self.config = config
            instances.append(self)
        async def initialize(self):
            if self.config.server_id == 'broken':
                raise ValueError('initialization root cause')
        async def list_tools_all(self): return ()
        async def close(self):
            if self.config.server_id == 'broken':
                raise OSError('cleanup failed')
        def stderr_tail(self): return ('server config root cause',)
        def diagnostics(self): return {'stderr_tail': list(self.stderr_tail())}
    monkeypatch.setattr(module, 'AsyncStdioMcpConnector', Connector)
    root = mcp_config_root(tmp_path)
    root.mkdir(parents=True)
    for name in ('good', 'broken'):
        (root / (name + '.json')).write_text(json.dumps({'command': ['unused']}))
    async def run():
        manager = module.McpManager(tmp_path)
        result = await manager.rescan()
        assert not result['ok'] and result['attached_count'] == 1
        broken = manager.read_server('broken')
        assert 'initialization root cause' in broken['failure_details']['error']
        assert 'cleanup failed' in broken['failure_details']['cleanup_error']
        before = len(instances)
        assert not (await manager.attach_server('broken'))['ok']
        assert len(instances) == before  # Cleanup failure cannot authorize another process.
        await manager.close_all()
    asyncio.run(run())


def test_mcp_attach_failure_reaches_public_facade(runtime, tmp_path, monkeypatch):
    from pal.mcp.plugin import McpManagerPluginProvider
    from pal.execution.capability_compiler import compile_provider_subtree
    from pal.execution.capabilities import ExecutionIntrospectionProvider
    execution_tree = compile_provider_subtree(ExecutionIntrospectionProvider(runtime),
        module_id='execution', lifecycle_scope='runtime', detachable=False)
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=execution_tree))
    provider = McpManagerPluginProvider(tmp_path, SimpleNamespace())
    provider.client = SimpleNamespace(attach_server_sync=lambda _: {
        'attached': False, 'ok': False, 'status': 'error', 'last_error': 'config root cause'})
    for method in ('_ensure_manager_started', '_refresh_projection', '_refresh_module_capabilities'):
        monkeypatch.setattr(provider, method, lambda: None)
    tree = compile_provider_subtree(provider, module_id='mcp', lifecycle_scope='detachable', detachable=True)
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=tree))
    result = runtime.execute_tool(new_tool_call(name='call_tool', args={'name': 'attach_mcp_server', 'args': {'name': 'broken'}}))
    assert not result.ok and result.invocation_result.effect.value == 'unknown'
    assert 'MCP server attach failed' in result.llm_text
    assert 'config root cause' in result.llm_text


def test_workflow_operation_enums_reach_real_registry(runtime, tmp_path):
    from pal.bunshin.workflow_capabilities import BunshinPublicProvider
    from pal.execution.capability_compiler import compile_provider_subtree
    # Discovery compilation needs only the provider's static contract, not a running workflow.
    provider = object.__new__(BunshinPublicProvider)
    provider.runtime_root = tmp_path
    tree = compile_provider_subtree(provider, module_id='bunshin', lifecycle_scope='detachable', detachable=True)
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=tree))
    assert search(runtime, 'bunshin cancel workflows')[0]['alias'] == 'control_bunshin_workflow'
    assert search(runtime, 'bunshin execute trusted workflows')[0]['alias'] == 'start_bunshin_workflow'


def test_selected_enum_reference_is_resolved_without_indexing_other_enums():
    from pal.execution.discovery_terms import discovery_vocabulary
    guidance = ToolGuidance(purpose='Operate', use_when='Requested', do_not_use_when='Other',
        search_enum_fields=('operation',))
    schema = {'properties': {'operation': {'$ref': '#/$defs/Operation'},
                              'format': {'enum': ['json']}},
              '$defs': {'Operation': {'anyOf': [{'enum': ['close_tab']}, {'const': 'list_tabs'}]}}}
    words = discovery_vocabulary(guidance, schema)
    assert set(words) == {'close', 'tab', 'list', 'tabs'}
    with pytest.raises(ValueError, match='operation'):
        discovery_vocabulary(guidance, {'properties': {'operation': {'type': 'string'}}})


def test_malformed_mcp_evidence_survives_real_manager_ipc(tmp_path):
    from pal.mcp.ipc import McpManagerClient, McpManagerRpcError, start_manager_server, cleanup_manager_endpoint
    from pal.mcp.manager import McpManager
    from pal.mcp.model import McpProtocolError
    from pal.mcp.normalize import normalize_protocol_error
    async def run():
        manager = McpManager(tmp_path)
        raw = {'jsonrpc': '2.0', 'id': 1, 'result': None, 'evidence': 'invalid-response-marker'}
        async def fail(method, params):
            raise McpProtocolError('invalid tools/call response', payload={'raw_response': raw})
        manager._call_method = fail
        server, _ = await start_manager_server(tmp_path, manager._handle_client)
        try:
            with pytest.raises(McpManagerRpcError) as error:
                await McpManagerClient(tmp_path).request('call_tool', {'server_id': 'audit'})
            result = normalize_protocol_error(error.value, server_id='audit', name='write', kind='tool')
            assert 'invalid-response-marker' in json.dumps(result.structured)
            assert result.status == 'error'
        finally:
            server.close()
            await server.wait_closed()
            await cleanup_manager_endpoint(tmp_path)
    asyncio.run(run())


def test_colliding_primary_artifact_promotes_latest_bytes(scoped, tmp_path):
    for text in ('first', 'second'):
        assert artifact_call(scoped, relative_path='report.txt', content=text).ok
    payload = Artifacts(BunshinInvocationPack(invocation_id='audit', workspace=scoped.workspace),
                        scoped.produced_artifacts).artifact_payload()
    assert len(payload['artifacts']) == 1
    assert payload['primary_artifact']['relative_path'] == 'report.txt'
    assert Path(payload['primary_artifact']['path']).read_text() == 'second'
    assert not (tmp_path / 'stage' / 'report.txt').exists()
    assert not (tmp_path / 'stage' / 'report_2.txt').exists()
