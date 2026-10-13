"""Configuration CLIs preserve ownership, persistence and recovery boundaries."""
import asyncio
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import pytest

from pal.core import PalCore
from pal.main import _build_parser
from pal.cli_config import open_config_database
from pal.core.cli import run_core_cli_async
from pal.bunshin.config_cli import run_bunshin_config_cli_async
from pal.bunshin.catalog import BunshinCatalogService
from pal.bunshin.catalog_store import profile_override_path
from pal.bunshin.ipc import start_manager_server, cleanup_manager_endpoint
from pal.bunshin.profiles import BunshinProfileRegistry
from pal.control.configuration import CoreConfigurationService, configuration_endpoint
from pal.foundation import PalV2Database
from pal.foundation.sidecar import dispatch_sidecar_request, open_sidecar_connection, pack_sidecar_message, read_sidecar_message, SidecarRpcClient
from pal.llm.models import PalRuntimeSettingModel
from pal.llm.repository import RuntimeSettingRepository
from pal.packages.process import runtime_lease
from pal.web_search.cli import run_web_search_cli
from pal.web_search.models import WebSearchProviderModel
from pal.web_search.repository import WebSearchProviderRepository
from pal.web_search.service import WebSearchService
from pal.web_search.contracts import WebSearchItem, WebSearchQuery


@pytest.fixture
def runtime_root(tmp_path):
    database = PalV2Database(tmp_path / 'pal.sqlite3')
    database.initialize((WebSearchProviderModel, PalRuntimeSettingModel))
    WebSearchProviderRepository().upsert(provider_id='brave', provider_kind='brave_search',
        enabled=True, settings_blob={'keep': 'original'}, auth_material_blob={'keep': 'secret-keep'})
    WebSearchProviderRepository().upsert(provider_id='ddg', provider_kind='duckduckgo_search', enabled=True)
    yield tmp_path
    database.close()


def parse(root, *args):
    return _build_parser().parse_args([*args, '--runtime-root', str(root), '--json'])


def invoke_web(root, *args, stdin=None, monkeypatch=None):
    if stdin is not None:
        monkeypatch.setattr(sys, 'stdin', io.StringIO(stdin))
    output = io.StringIO()
    with redirect_stdout(output):
        code = run_web_search_cli(parse(root, 'web-search', *args))
    return code, json.loads(output.getvalue())


def invoke_async(function, args):
    async def run():
        output = io.StringIO()
        with redirect_stdout(output):
            code = await function(args)
        return code, json.loads(output.getvalue())
    return asyncio.run(run())


def test_web_search_cli_updates_existing_service_without_clearing_request_errors(runtime_root, monkeypatch):
    calls = []
    class Provider:
        def search(self, record, query):
            calls.append((record.provider_id, dict(record.auth_material_blob), dict(record.settings_blob)))
            return [WebSearchItem(title='Result', url='https://example.test', snippet='result',
                                  provider_id=record.provider_id, provider_kind=record.provider_kind, rank=1)]
    service = WebSearchService(WebSearchProviderRepository(), RuntimeSettingRepository(),
                              providers={'brave_search': Provider(), 'duckduckgo_search': Provider()})
    service.last_errors['brave'] = 'previous transport error'
    patch = runtime_root / 'patch.json'
    patch.write_text('{"region":"GB"}')
    assert invoke_web(runtime_root, 'configure', 'brave', '--patch-file', str(patch))[0] == 0
    code, result = invoke_web(runtime_root, 'auth', 'brave', '--stdin',
        stdin='{"api_key":"secret-value"}', monkeypatch=monkeypatch)
    assert code == 0 and result['accepted_keys'] == ['api_key']
    assert 'secret-value' not in json.dumps(result)
    assert 'secret-keep' not in json.dumps(invoke_web(runtime_root, 'show', 'brave'))
    assert service.last_errors['brave'] == 'previous transport error'
    assert invoke_web(runtime_root, 'set-active', 'brave')[0] == 0
    assert service.query(WebSearchQuery(query='example')).effective_provider_id == 'brave'
    assert calls[-1][1] == {'keep': 'secret-keep', 'api_key': 'secret-value'}
    assert calls[-1][2] == {'keep': 'original', 'region': 'GB'}
    assert service.last_errors['brave'] == ''
    assert invoke_web(runtime_root, 'disable', 'brave')[0] == 0
    assert invoke_web(runtime_root, 'set-active', 'brave')[0] == 2
    assert service.configured_active_provider_id() == 'brave'
    assert service.query(WebSearchQuery(query='example')).effective_provider_id == 'ddg'


def test_web_cli_invalid_input_and_runtime_target_are_isolated(runtime_root, monkeypatch):
    code, result = invoke_web(runtime_root, 'auth', 'brave', '--stdin',
        stdin='invalid-secret-input', monkeypatch=monkeypatch)
    assert code == 2 and 'invalid-secret-input' not in json.dumps(result)
    missing = runtime_root / 'missing'
    assert invoke_web(missing, 'list')[0] == 2
    assert not missing.exists()
    with sqlite3.connect(runtime_root / 'pal.sqlite3') as connection:
        assert 'api_key' not in connection.execute('SELECT auth_material_blob FROM web_search_providers WHERE provider_id=?', ('brave',)).fetchone()[0]
    other = runtime_root / 'other'
    other.mkdir()
    with sqlite3.connect(other / 'pal.sqlite3') as db:
        db.executescript('CREATE TABLE unrelated (value TEXT); INSERT INTO unrelated VALUES ("keep");')
    assert invoke_web(other, 'disable', 'brave')[0] == 2
    with sqlite3.connect(other / 'pal.sqlite3') as db:
        assert db.execute('SELECT value FROM unrelated').fetchone()[0] == 'keep'
    assert (runtime_root / 'pal.sqlite3').is_file()


def test_core_offline_configuration_is_atomic_and_reports_on_start(runtime_root):
    args = parse(runtime_root, 'core', 'cache-reminder', 'configure', '--no-enabled', '--lead-seconds', '31')
    code, result = invoke_async(run_core_cli_async, args)
    assert code == 0 and result['activation'] == 'on_start'
    invalid = parse(runtime_root, 'core', 'cache-reminder', 'configure', '--enabled', '--min-prefix-tokens', '1')
    assert invoke_async(run_core_cli_async, invalid)[0] == 2
    assert invoke_async(run_core_cli_async, parse(runtime_root, 'core', 'cache-reminder', 'show'))[1]['state']['enabled'] is False
    assert invoke_async(run_core_cli_async, parse(runtime_root, 'core', 'cache-reminder', 'show'))[1]['state']['lead_seconds'] == 31


@pytest.mark.parametrize('force_tcp', [False, True])
def test_core_cli_reaches_busy_owner_and_shutdown_closes_idle_clients(runtime_root, monkeypatch, force_tcp):
    if force_tcp:
        async def unavailable_unix_server(*args, **kwargs):
            raise OSError('Unix socket unavailable in this fixture')
        monkeypatch.setattr(asyncio, 'start_unix_server', unavailable_unix_server)

    async def run():
        from tests.test_cache_warm_deadline import _route, _snapshot
        core = PalCore()
        core.context.port_registry['llm:llm'] = SimpleNamespace(settings_repository=RuntimeSettingRepository(),
            prompt_cache_warm_deadline_snapshot=_snapshot)
        core.state.active_turn_id = 'waiting-for-cli'
        owner = core.cache_warm_deadline
        owner.configure(enabled=True, lead_seconds=30, min_prefix_tokens=1024)
        assert owner.schedule_after_turn_commit(route=_route(), turn_id='previous')
        service = CoreConfigurationService(runtime_root, owner)
        with runtime_lease(runtime_root):
            await service.start_async()
            endpoint = configuration_endpoint(runtime_root)
            try:
                if force_tcp:
                    assert endpoint.port_path.exists()
                process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'pal.main',
                    'core', 'cache-reminder', 'configure', '--no-enabled', '--runtime-root', str(runtime_root), '--json',
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
                assert process.returncode == 0, stderr.decode()
                payload = json.loads(stdout)
                assert payload['activation'] == 'live' and payload['state']['timer_scheduled'] is False
                assert core.state.active_turn_id == 'waiting-for-cli'
                client = SidecarRpcClient(endpoint)
                with pytest.raises(Exception):
                    await client.request('call_tool', {'name': 'configure_core'})
                reader, writer = await open_sidecar_connection(endpoint)
                await asyncio.sleep(0)
                await asyncio.wait_for(service.stop_async(), 3)
                assert await reader.read() == b''
                writer.close()
                await writer.wait_closed()
                assert not endpoint.socket_path.exists() and not endpoint.port_path.exists()
            finally:
                await service.stop_async()
                core.context.execution_runtime.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['core', 'bunshin'])
def test_live_owner_failure_never_falls_back_to_offline_mutation(runtime_root, kind):
    with runtime_lease(runtime_root):
        if kind == 'core':
            args = parse(runtime_root, 'core', 'cache-reminder', 'configure', '--no-enabled', '--timeout-seconds', '.1')
            code, result = invoke_async(run_core_cli_async, args)
            assert RuntimeSettingRepository().get('cache_warm_deadline_enabled') is None
        else:
            patch = runtime_root / 'patch.json'
            patch.write_text('{"display_name":"Must not be written"}')
            args = parse(runtime_root, 'bunshin', 'profile', 'patch', 'software_engineering.v2_coder', '--patch-file', str(patch), '--timeout-seconds', '.1')
            code, result = invoke_async(run_bunshin_config_cli_async, args)
            assert not profile_override_path(runtime_root, 'software_engineering', 'v2_coder').exists()
        assert code == 2
        assert result['outcome'] == 'unknown'
        assert 'do not fall back' in result['recovery']


def test_bunshin_offline_patch_preserves_fields_null_and_generation(runtime_root):
    service = BunshinCatalogService(runtime_root)
    before = service.snapshot(include_definitions=True)
    pinned = BunshinProfileRegistry(runtime_root=runtime_root).get('software_engineering.v2_coder')
    patch = runtime_root / 'profile.json'
    patch.write_text('{"display_name":"CLI Coder","preferred_endpoint_name":"test-endpoint"}')
    args = parse(runtime_root, 'bunshin', 'profile', 'patch', 'software_engineering.v2_coder', '--patch-file', str(patch), '--if-generation', before['generation'])
    code, result = invoke_async(run_bunshin_config_cli_async, args)
    assert code == 0 and result['owner'] == 'offline'
    changed = BunshinProfileRegistry(runtime_root=runtime_root).get('software_engineering.v2_coder')
    assert changed.preferred_endpoint_id == 'test-endpoint' and changed.display_name == 'CLI Coder'
    assert changed.behavior_fragment == pinned.behavior_fragment
    assert pinned.display_name != changed.display_name
    assert invoke_async(run_bunshin_config_cli_async, args)[0] == 2
    patch.write_text('{"preferred_endpoint_name":null}')
    args.if_generation = ''
    assert invoke_async(run_bunshin_config_cli_async, args)[0] == 0
    assert not BunshinProfileRegistry(runtime_root=runtime_root).get('software_engineering.v2_coder').preferred_endpoint_id
    assert not json.loads(profile_override_path(runtime_root, 'software_engineering', 'v2_coder').read_text()).get('preferred_endpoint_id')
    reset = parse(runtime_root, 'bunshin', 'profile', 'reset', 'software_engineering.v2_coder')
    assert invoke_async(run_bunshin_config_cli_async, reset)[1]['status'] == 'reset'
    assert invoke_async(run_bunshin_config_cli_async, reset)[1]['status'] == 'unchanged'
    audit = list((runtime_root / 'data' / 'bunshin' / 'catalog').glob('*audit*'))
    assert audit and 'pal_cli' in audit[0].read_text()


def test_bunshin_online_cli_uses_catalog_owner(runtime_root):
    async def run():
        service = BunshinCatalogService(runtime_root)
        methods = {'catalog_snapshot': service.snapshot, 'catalog_set_family_override': service.set_family_override,
                   'catalog_reset_family_override': service.reset_family_override, 'catalog_refresh': service.refresh}
        calls = []
        async def call(method, params):
            calls.append(method)
            return methods[method](**params)
        async def handler(reader, writer):
            try:
                request = await read_sidecar_message(reader)
                writer.write(pack_sidecar_message(await dispatch_sidecar_request(request, call)))
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        patch = runtime_root / 'family.json'
        patch.write_text('{"display_name":"CLI Family"}')
        with runtime_lease(runtime_root):
            server, _ = await start_manager_server(runtime_root, handler)
            try:
                for argv in [('catalog', 'show'), ('family', 'patch', 'software_engineering', '--patch-file', str(patch)),
                             ('catalog', 'refresh'), ('family', 'reset', 'software_engineering')]:
                    with redirect_stdout(io.StringIO()) as output:
                        assert await run_bunshin_config_cli_async(parse(runtime_root, 'bunshin', *argv)) == 0
                    assert json.loads(output.getvalue())['owner'] == 'live'
            finally:
                server.close()
                await server.wait_closed()
                await cleanup_manager_endpoint(runtime_root)
        assert calls == ['catalog_snapshot', 'catalog_set_family_override', 'catalog_refresh', 'catalog_reset_family_override']
    asyncio.run(run())


def test_core_configuration_normalizes_relative_long_runtime_roots(runtime_root, monkeypatch):
    async def run():
        from tests.test_cache_warm_deadline import _Settings
        from pal.core.cache_warm_deadline import CacheWarmDeadlineManager
        long_root = runtime_root / ('long-directory-' * 8)
        long_root.mkdir()
        monkeypatch.chdir(runtime_root)
        owner = CacheWarmDeadlineManager(cache_snapshot=lambda: {}, settings_provider=_Settings,
            has_active_turn=lambda: False, deliver_notice=None, expire_notice=None)
        service = CoreConfigurationService(Path(long_root.name), owner)
        await service.start_async()
        try:
            assert service.runtime_root == long_root.resolve()
            result = await SidecarRpcClient(configuration_endpoint(long_root)).request('cache_reminder.inspect')
            assert result['activation'] == 'live'
        finally:
            await service.stop_async()
        assert not configuration_endpoint(long_root).port_path.exists()
    asyncio.run(run())


def test_core_timeout_reports_unknown_and_does_not_repeat_or_fall_back(runtime_root):
    async def run():
        core = PalCore()
        core.context.port_registry['llm:llm'] = SimpleNamespace(settings_repository=RuntimeSettingRepository())
        service = CoreConfigurationService(runtime_root, core.cache_warm_deadline)
        call_method = service._call_method
        calls = []
        gate = asyncio.Event()
        async def delayed(method, params):
            calls.append(method)
            result = await call_method(method, params)
            await gate.wait()
            return result
        service._call_method = delayed
        with runtime_lease(runtime_root):
            await service.start_async()
            try:
                args = parse(runtime_root, 'core', 'cache-reminder', 'configure', '--no-enabled', '--timeout-seconds', '.05')
                with redirect_stdout(io.StringIO()) as output:
                    assert await run_core_cli_async(args) == 2
                assert json.loads(output.getvalue())['outcome'] == 'unknown'
                assert core.cache_warm_deadline.settings().enabled is False
                assert calls == ['cache_reminder.configure']
            finally:
                gate.set()
                await service.stop_async()
                core.context.execution_runtime.shutdown()
    asyncio.run(run())


@pytest.mark.parametrize('argv', [
    ('web-search', 'list'), ('core', 'cache-reminder', 'show'), ('bunshin', 'catalog', 'show'),
])
def test_main_configuration_errors_honor_json_without_provisioning(tmp_path, monkeypatch, argv):
    from pal.main import main
    missing = tmp_path / 'unconfigured'
    monkeypatch.setattr(sys, 'argv', ['pal', *argv, '--runtime-root', str(missing), '--json'])
    with redirect_stdout(io.StringIO()) as output:
        assert main() == 2
    assert json.loads(output.getvalue())['status'] == 'error'
    assert not missing.exists()


def test_model_cli_benchmark_executes_only_fixture_commands(tmp_path):
    from scripts.benchmark_configuration_cli import validate_command
    credentials = tmp_path / 'credentials.json'
    credentials.write_text('{"api_key":"fixture"}')
    tokens, stdin = validate_command(
        f'pal web-search auth brave --stdin --runtime-root {tmp_path} < {credentials}', tmp_path)
    assert tokens[:4] == ['web-search', 'auth', 'brave', '--stdin']
    assert stdin == credentials
    for command in (
        f'pal web-search list --runtime-root {tmp_path}; touch forbidden',
        f'pal web-search list --runtime-root {tmp_path} | cat',
        'pal run --runtime-root /tmp',
        'pal web-search list --runtime-root /tmp',
        f'pal web-search auth brave --stdin --runtime-root {tmp_path}',
        f'pal web-search configure brave --patch-file /tmp/outside.json --runtime-root {tmp_path}',
        f'pal core cache-reminder show --runtime-root {tmp_path} < {credentials}',
    ):
        with pytest.raises(ValueError):
            validate_command(command, tmp_path)
