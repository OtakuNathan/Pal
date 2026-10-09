"""Strict external boundary: real stdio, lifecycle fencing and public discovery."""
import asyncio
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from pal.mcp.manager import McpManager
from pal.mcp.connector import AsyncStdioMcpConnector
from pal.mcp.compiler import McpCompiler
from pal.mcp.model import McpProtocolError, McpRemoteError, McpServerConfig
from pal.mcp.ipc import mcp_config_root, McpManagerClient, McpManagerRpcError, start_manager_server, cleanup_manager_endpoint
from pal.mcp.protocol import validate_tool_result, validate_tool_schema


SERVER = '''import json,sys
mode=sys.argv[1]
for line in sys.stdin:
 r=json.loads(line)
 if 'id' not in r: continue
 method=r['method']
 if method=='initialize':
  result={'protocolVersion':'2025-06-18','serverInfo':{'name':'strict-test','version':'1'},'capabilities':{'tools':{},'prompts':{}}}
  if mode=='version': result['protocolVersion']='2099-01-01'
  if mode=='initialize': result.pop('serverInfo')
 elif method=='tools/list':
  tools=[{'name':'write','inputSchema':{'type':'object'}}]
  if mode=='bad_tool': tools.append({'name':'bad','inputSchema':{'type':'string'}})
  if mode=='missing_schema': tools.append({'name':'bad'})
  if mode=='duplicate': tools.append(tools[0])
  if mode=='output_schema': tools[0]['outputSchema']={'type':'object','required':['value'],'properties':{'value':{'type':'integer'}}}
  result={'tools':tools}
  if mode=='cursor': result['nextCursor']='again'
 elif method=='prompts/list':
  result={'prompts':[{'name':'review'}]}
 elif method=='prompts/get':
  result={'messages':[]} if mode!='prompt' else {'messages':[{'role':'user','content':{'type':'image','data':'eA=='}}]}
 elif method=='tools/call':
  if mode=='json': print('NOT JSON: original evidence',flush=True); continue
  if mode=='encoding': sys.stdout.buffer.write(bytes([255,10])); sys.stdout.buffer.flush(); continue
  if mode=='rpc_error': print(json.dumps({'jsonrpc':'2.0','id':r['id'],'error':{'code':-32602,'message':'bad argument'}}),flush=True); continue
  result={'content':[{'type':'text','text':'done'}]}
  if mode=='content': result['content'][0]['annotations']={'priority':'bad'}
  if mode=='large': result['content'][0]['text']='x'*100000
  if mode=='output_schema': result['structuredContent']={'value':'bad'}
  if mode=='business': result['isError']=True
 else: result={}
 print(json.dumps({'jsonrpc':'2.0','id':r['id'],'result':result}),flush=True)
'''


def config(root, mode):
    script = root / 'server.py'
    script.write_text(SERVER)
    directory = mcp_config_root(root)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'demo.json'
    path.write_text(json.dumps({'command': [sys.executable, str(script), mode], 'request_timeout_ms': 2000}))
    return path


@pytest.mark.parametrize('mode', ['version', 'initialize', 'bad_tool', 'missing_schema', 'duplicate', 'cursor'])
def test_invalid_server_is_rejected_whole_and_not_automatically_retried(tmp_path, mode):
    config(tmp_path, mode)
    async def run():
        manager = McpManager(tmp_path)
        result = await manager.rescan()
        assert not result['ok'] and result['attached_count'] == 0
        assert manager.snapshot()['snapshots'] == []
        first = manager.read_server('demo')['failure_details']
        assert first['error'] and first['stderr_path']
        await manager.rescan()
        assert manager.read_server('demo')['failure_details'] == first
        assert manager.states['demo'].connector is None
        await manager.close_all()
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['content', 'output_schema', 'json', 'prompt', 'encoding'])
def test_call_protocol_failure_quarantines_and_keeps_evidence_across_ipc(tmp_path, mode):
    config(tmp_path, mode)
    async def run():
        manager = McpManager(tmp_path)
        assert (await manager.rescan())['ok']
        server, _ = await start_manager_server(tmp_path, manager._handle_client)
        client = McpManagerClient(tmp_path)
        try:
            method = 'render_prompt' if mode == 'prompt' else 'call_tool'
            params = {'server_id':'demo', 'tool_name':'write', 'prompt_name':'review'}
            with pytest.raises(McpManagerRpcError):
                await client.request(method, params)
            detail = await client.read_server('demo')
            assert not detail['attached'] and detail['quarantined']
            evidence = detail['failure_details']['protocol_details']
            assert evidence.get('raw_response') or evidence.get('raw_response_base64')
            assert not (await client.snapshot())['snapshots']
            with pytest.raises(McpManagerRpcError):
                await client.request(method, params)
            await manager.rescan()
            assert not manager.states['demo'].attached
            # Persistence prevents an automatic retry after manager restart.
            replacement = McpManager(tmp_path)
            await replacement.rescan()
            assert not replacement.states['demo'].attached
            # Correcting the service requires explicit attachment.
            config(tmp_path, 'good')
            await replacement.rescan()
            assert not replacement.states['demo'].attached
            assert (await replacement.attach_server('demo'))['ok']
            await replacement.close_all()
        finally:
            await manager.close_all()
            server.close()
            await server.wait_closed()
            await cleanup_manager_endpoint(tmp_path)
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['business', 'rpc_error'])
def test_valid_error_does_not_quarantine(tmp_path, mode):
    config(tmp_path, mode)
    async def run():
        manager = McpManager(tmp_path)
        try:
            assert (await manager.rescan())['ok']
            if mode == 'business':
                assert (await manager.call_tool('demo', 'write', {}))['isError']
            else:
                with pytest.raises(McpRemoteError):
                    await manager.call_tool('demo', 'write', {})
            assert manager.states['demo'].attached
            assert not manager.quarantine
        finally:
            await manager.close_all()
    asyncio.run(run())


def test_cleanup_fence_survives_config_removal_and_manager_restart(tmp_path, monkeypatch):
    import pal.mcp.manager as module
    instances = []
    class Connector:
        server_info = {}; server_capabilities = {}
        def __init__(self, config): instances.append(self)
        async def initialize(self): raise McpProtocolError('bad initialization', payload={'raw_response':{'marker':'original'}})
        async def close(self): raise OSError('termination unconfirmed')
        def diagnostics(self): return {'process_id':12345}
    monkeypatch.setattr(module, 'AsyncStdioMcpConnector', Connector)
    path = config(tmp_path, 'unused')
    async def run():
        manager = module.McpManager(tmp_path)
        await manager.rescan()
        assert 'original' in json.dumps(manager.read_server('demo'))
        path.unlink()
        await manager.rescan()
        config(tmp_path, 'unused')
        await manager.rescan()
        await manager.attach_server('demo')
        replacement = module.McpManager(tmp_path)
        await replacement.rescan()
        assert not (await replacement.attach_server('demo'))['ok']
        assert len(instances) == 1
    asyncio.run(run())


def test_cleanup_failure_of_attached_server_withdraws_authority(tmp_path, monkeypatch):
    config(tmp_path, 'good')
    async def run():
        manager = McpManager(tmp_path)
        assert (await manager.rescan())['ok']
        connector = manager.states['demo'].connector
        original_close = connector.close
        async def failure(): raise OSError('termination unconfirmed')
        monkeypatch.setattr(connector, 'close', failure)
        try:
            result = await manager.detach_server('demo')
            assert not result['ok'] and not result['attached']
            assert not manager.snapshot()['snapshots']
            assert not (await manager.attach_server('demo'))['ok']
        finally:
            await original_close()
    asyncio.run(run())


@pytest.mark.parametrize('content', [
    {'type':'text','text':'x','_meta':False},
    {'type':'text','text':'x','annotations':{'priority':'bad'}},
    {'type':'resource_link','name':'x','uri':'file:///x','size':'bad'},
    {'type':'image','data':'not base64!','mimeType':'image/png'},
])
def test_known_optional_content_fields_are_validated(content):
    with pytest.raises(McpProtocolError):
        validate_tool_result({'content':[content]})


def test_standard_empty_mixed_content_and_extensions_remain_valid():
    validate_tool_result({'content':[],'structuredContent':{'error':'business data'},'vendor':{'x':1}})
    validate_tool_result({'content':[
        {'type':'text','text':'x','annotations':{'priority':0.5}},
        {'type':'image','data':'eA==','mimeType':'image/png'},
        {'type':'audio','data':'eA==','mimeType':'audio/wav'},
        {'type':'resource','resource':{'uri':'file:///x','text':'x'}},
        {'type':'resource_link','name':'x','uri':'file:///x','size':1},
    ]})


def test_schema_literals_are_not_treated_as_schema_references():
    validate_tool_schema({'type':'object','properties':{'$ref':{'type':'string'}},
                          'examples':[{'$ref':'external business data'}]}, 'input')


@pytest.mark.parametrize('schema', [None, {}, {'type':'array'},
    {'type':'object','properties':{'x':{'$ref':'https://example.invalid/schema'}}},
    {'type':'object','properties':{'x':{'$ref':'#/$defs/absent'}}},
])
def test_unsupported_schemas_fail_before_execution(schema):
    with pytest.raises(McpProtocolError):
        validate_tool_schema(schema, 'input')


def test_protocol_failure_removes_live_public_alias_via_real_ipc(tmp_path):
    from pal.mcp.plugin import McpManagerPluginProvider
    from pal.execution.runtime import ExecutionRuntime
    from pal.shared.tool_protocol import new_tool_call
    config(tmp_path, 'content')
    async def run():
        manager = McpManager(tmp_path)
        assert (await manager.rescan())['ok']
        server, _ = await start_manager_server(tmp_path, manager._handle_client)
        runtime = ExecutionRuntime()
        provider = McpManagerPluginProvider(tmp_path, core_context=None)
        def mount():
            runtime.mount_subtree(SimpleNamespace(mounted_subtree=provider.projection.mounted_subtree))
        provider.refresh_capabilities = mount
        try:
            await asyncio.to_thread(provider._refresh_after_call_failure)
            assert 'call_mcp_demo_write' in runtime.registry_generation.indirect_aliases
            result = await asyncio.to_thread(runtime.invoke_indirect_tool,
                new_tool_call(name='call_mcp_demo_write', args={}))
            assert result.__class__.__name__ == 'FailedResult'
            assert result.effect.value == 'unknown'
            assert 'call_mcp_demo_write' not in runtime.registry_generation.indirect_aliases
            assert manager.states['demo'].attached is False
        finally:
            runtime.shutdown()
            await manager.close_all()
            server.close()
            await server.wait_closed()
            await cleanup_manager_endpoint(tmp_path)
    asyncio.run(run())


@pytest.mark.parametrize('field', [
    {'allOf':[{'enum':['open','close']},{'enum':['close']}]},
    {'$ref':'#/$defs/action','enum':['close']},
])
def test_selected_enum_excludes_values_forbidden_by_full_field_contract(field):
    from pal.execution.discovery_terms import discovery_vocabulary
    from pal.execution.tool_facade import ToolGuidance
    schema={'type':'object','properties':{'operation':field},
            '$defs':{'action':{'enum':['open','close']}}}
    guidance=ToolGuidance(purpose='x',use_when='x',do_not_use_when='x',search_enum_fields=('operation',))
    assert discovery_vocabulary(guidance,schema) == ('close',)


def test_valid_remote_error_keeps_its_kind_across_ipc(tmp_path):
    from pal.mcp.normalize import normalize_protocol_error
    config(tmp_path, 'rpc_error')
    async def run():
        manager = McpManager(tmp_path)
        assert (await manager.rescan())['ok']
        server, _ = await start_manager_server(tmp_path, manager._handle_client)
        try:
            with pytest.raises(McpManagerRpcError) as raised:
                await McpManagerClient(tmp_path).call_tool('demo', 'write', {})
            result = normalize_protocol_error(raised.value, server_id='demo', name='write', kind='tool')
            assert result.structured['error_kind'] == 'remote'
            assert manager.states['demo'].attached
        finally:
            await manager.close_all()
            server.close()
            await server.wait_closed()
            await cleanup_manager_endpoint(tmp_path)
    asyncio.run(run())


def test_valid_result_larger_than_asyncio_default_line_limit(tmp_path):
    config(tmp_path, 'large')
    async def run():
        manager = McpManager(tmp_path)
        try:
            assert (await manager.rescan())['ok']
            result = await manager.call_tool('demo', 'write', {})
            assert len(result['content'][0]['text']) == 100000
            assert manager.states['demo'].attached
        finally:
            await manager.close_all()
    asyncio.run(run())


def test_reference_target_cannot_hide_a_remote_reference():
    schema = {'type': 'object', 'properties': {'x': {'$ref': '#/extension'}},
              'extension': {'$ref': 'https://example.invalid/schema'}}
    with pytest.raises(McpProtocolError):
        validate_tool_schema(schema, 'input')


def test_quarantine_write_failure_keeps_original_protocol_evidence(tmp_path, monkeypatch):
    config(tmp_path, 'content')
    async def run():
        manager = McpManager(tmp_path)
        assert (await manager.rescan())['ok']
        def fail(): raise OSError('quarantine disk unavailable')
        monkeypatch.setattr(manager, '_save_quarantine', fail)
        with pytest.raises(McpProtocolError) as raised:
            await manager.call_tool('demo', 'write', {})
        assert 'raw_response' in raised.value.payload
        assert 'quarantine disk unavailable' in raised.value.payload['quarantine_persistence_error']
        assert not manager.states['demo'].attached
        assert manager.states['demo'].connector is None
        assert not (await manager.rescan())['ok']
    asyncio.run(run())


def test_attach_does_not_publish_when_quarantine_storage_is_unavailable(tmp_path, monkeypatch):
    config(tmp_path, 'good')
    async def run():
        manager = McpManager(tmp_path)
        def fail(): raise OSError('quarantine disk unavailable')
        monkeypatch.setattr(manager, '_save_quarantine', fail)
        result = await manager.rescan()
        assert not result['ok']
        assert not manager.snapshot()['snapshots']
        assert manager.states['demo'].connector is None
        assert 'quarantine_persistence_error' in manager.read_server('demo')['failure_details']
    asyncio.run(run())


@pytest.mark.parametrize('mode', ['good', 'content'])
def test_capability_arrives_after_provider_and_leaves_before_cleanup(tmp_path, monkeypatch, mode):
    config(tmp_path, mode)
    async def run():
        manager = McpManager(tmp_path)
        initialize = AsyncStdioMcpConnector.initialize
        close = AsyncStdioMcpConnector.close
        events = []
        def withdrawn():
            assert not manager.states['demo'].attached
            assert not manager.snapshot()['snapshots']
        async def initializing(connector):
            withdrawn()
            await initialize(connector)
            withdrawn()
            events.append('provider ready')
        async def closing(connector):
            withdrawn()
            events.append('provider closing')
            await close(connector)
        monkeypatch.setattr(AsyncStdioMcpConnector, 'initialize', initializing)
        monkeypatch.setattr(AsyncStdioMcpConnector, 'close', closing)
        assert (await manager.rescan())['ok']
        assert manager.states['demo'].attached
        if mode == 'good':
            assert (await manager.detach_server('demo'))['ok']
        else:
            with pytest.raises(McpProtocolError):
                await manager.call_tool('demo', 'write', {})
        assert events == ['provider ready', 'provider closing']
    asyncio.run(run())
