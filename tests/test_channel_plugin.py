"""Plugin-owned channel code must not own the lifetime of delivery hubs."""
import shutil
import json
from types import SimpleNamespace

import pytest

from pal.core import PalCore
from pal.execution import register_with_core as register_execution
from pal.channel import ChannelRuntime, register_with_core as register_channel
from pal.channel.repository import ChannelEndpointRepository
from pal.plugins import PluginHost
from pal.shared import RuntimeStatus
from pal.wizard import WizardService


@pytest.fixture
def setup(tmp_path):
    wizard = WizardService()
    registration = wizard.provision_runtime(display_name='Channel plugin test', runtime_root=tmp_path,
        db_filename='test.sqlite3', pal_entrypoint='pal.runtime.test')
    db = wizard.create_database(registration)
    core = PalCore()
    register_execution(core.context)
    runtime = ChannelRuntime()
    register_channel(core.context, runtime, runtime_root=tmp_path)
    host = PluginHost(core.context, tmp_path)
    manager = core.context.require_port('channel:provider_manager')
    # Reuse the provider fixture contract, without composing the full runtime.
    from tests.test_bootstrap_and_repositories import PalV2BootstrapTests
    PalV2BootstrapTests._write_demo_runtime_channel_provider(SimpleNamespace(runtime_root=tmp_path))
    repository = ChannelEndpointRepository()
    repository.upsert(endpoint_id='demo', channel_kind='demo_runtime', binding_key='test', enabled=True)
    yield SimpleNamespace(host=host, manager=manager, runtime=runtime, root=tmp_path, core=core, repository=repository)
    host.shutdown()
    core.context.execution_runtime.shutdown()
    db.close()


def start(env):
    env.host.bootstrap()
    record = env.host._record('demo_runtime')
    assert record.attached, record.last_error
    assert env.runtime.get_endpoint('demo') is not None


def test_legacy_provider_is_a_plugin_generation_and_endpoint_detach_keeps_code(setup):
    start(setup)
    generation = setup.host.generations['demo_runtime']
    hub = setup.runtime.get_endpoint_hub('demo')
    assert setup.manager.detach_endpoint('demo').status == RuntimeStatus.OK
    assert setup.host.generations['demo_runtime'] is generation
    assert 'demo_runtime' in setup.manager.runtime_provider_handles
    assert setup.manager.attach_endpoint('demo').status == RuntimeStatus.OK
    assert setup.host.detach('demo_runtime')['status'] == RuntimeStatus.OK
    assert setup.runtime.get_endpoint('demo') is None
    assert setup.runtime.get_endpoint_hub('demo') is hub
    assert 'demo_runtime' not in setup.manager.runtime_provider_handles
    assert setup.host.attach('demo_runtime')['status'] == RuntimeStatus.OK
    assert setup.runtime.get_endpoint_hub('demo') is hub


def test_legacy_channel_inspection_reads_provider_installation_receipt(setup):
    start(setup)
    from pal.packages.service import PackageService
    record_path = PackageService(setup.root)._record_path('provider', 'demo_runtime')
    record_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {'kind': 'provider', 'id': 'demo_runtime', 'status': 'ready', 'version': '1'}
    record_path.write_text(json.dumps(receipt))
    snapshot = setup.host.inspect_plugin('demo_runtime')
    assert snapshot['installation']['items'] == [receipt]


def test_reload_uses_fresh_code_but_preserves_hub_and_binding(setup):
    start(setup)
    hub = setup.runtime.get_endpoint_hub('demo')
    old = setup.host.generations['demo_runtime']
    source = setup.root/'channel/providers/demo_runtime/runtime.py'
    source.write_text(source.read_text().replace("'provider_code_version': 1", "'provider_code_version': 200"))
    result = setup.host.reattach('demo_runtime')
    assert result['status'] == RuntimeStatus.OK, result
    assert setup.host.generations['demo_runtime'] is not old
    assert setup.runtime.get_endpoint_hub('demo') is hub
    assert setup.runtime.get_endpoint('demo').inspect_health()['provider_code_version'] == 200
    assert setup.repository.get('demo').binding_key == 'test'


def test_failed_reload_keeps_delivery_boundary_and_can_recover(setup):
    start(setup)
    hub = setup.runtime.get_endpoint_hub('demo')
    source = setup.root/'channel/providers/demo_runtime/runtime.py'
    original = source.read_text()
    source.write_text('raise RuntimeError("broken replacement")\n')
    assert setup.host.reattach('demo_runtime')['status'] == RuntimeStatus.ERROR
    assert setup.runtime.get_endpoint_hub('demo') is hub
    assert not hub.published
    assert setup.runtime.get_endpoint('demo') is None
    source.write_text(original)
    result = setup.host.reattach('demo_runtime')
    assert result['status'] == RuntimeStatus.OK, result
    assert setup.runtime.get_endpoint_hub('demo') is hub


def test_failed_cleanup_stays_owned_by_the_plugin_generation(setup):
    start(setup)
    handle = setup.manager.runtime_provider_handles['demo_runtime']
    blocked = True
    def cleanup():
        if blocked:
            raise RuntimeError('resource busy')
    handle.cleanup_callbacks.append(cleanup)
    result = setup.host.detach('demo_runtime')
    assert result['status'] == RuntimeStatus.ERROR
    assert result['cleanup_pending']
    assert setup.manager.runtime_provider_handles['demo_runtime'] is handle
    assert 'demo_runtime' in setup.host.generations
    assert not setup.runtime.get_endpoint_hub('demo').published
    assert setup.host.attach('demo_runtime')['status'] == RuntimeStatus.ERROR
    blocked = False
    assert setup.host.detach('demo_runtime')['status'] == RuntimeStatus.OK
    assert 'demo_runtime' not in setup.manager.runtime_provider_handles


def test_disabled_plugin_cannot_be_implicitly_reenabled_by_endpoint_attach(setup):
    start(setup)
    assert setup.host.disable('demo_runtime')['status'] == RuntimeStatus.OK
    assert setup.manager.attach_endpoint('demo').status != RuntimeStatus.OK
    assert setup.runtime.get_endpoint('demo') is None
    assert setup.runtime.get_endpoint_hub('demo') is not None
    assert setup.host.enable('demo_runtime')['status'] == RuntimeStatus.OK


def test_native_channel_extension_uses_the_same_host(setup):
    legacy = setup.root/'channel/providers/demo_runtime'
    root = setup.root/'plugins/community/demo_runtime'
    root.mkdir(parents=True)
    source = (legacy/'runtime.py').read_text()
    shutil.rmtree(legacy)
    (root/'demo_channel.py').write_text(source + '''
from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
from pal.channel.plugin import ChannelExtension
class Plugin:
    def start(self, scope):
        provider = FactoryChannelProvider('demo_runtime', ('demo_runtime',), DemoRuntimeFactory())
        return ModuleHandle(module_id='demo_runtime', tier=MODULE_TIER_DETACHABLE,
                            detachable=True, channel_extension=ChannelExtension(provider=provider))
def build_plugin(context): return Plugin()
''')
    (root/'plugin.toml').write_text('''plugin_id = "demo_runtime"
entrypoint = "demo_channel"
version = "1"
lifecycle_protocol = "raii.v1"
requires_ports = ["channel:provider_manager"]
[channel]
endpoint_types = ["demo_runtime"]
''')
    setup.host.rescan()
    hub = setup.runtime.get_endpoint_hub('demo')
    assert hub is not None  # Manifest discovery does not require provider code.
    assert setup.runtime.get_endpoint('demo') is None
    start(setup)
    assert setup.host.reattach('demo_runtime')['status'] == RuntimeStatus.OK
    assert setup.runtime.get_endpoint_hub('demo') is hub
    assert setup.host.detach('demo_runtime')['status'] == RuntimeStatus.OK
    # Failed recovery transfer must retain the metadata needed to retry removal.
    with pytest.raises(RuntimeError, match='recovery endpoint hub'):
        setup.host.forget_uninstalled('demo_runtime')
    assert 'demo_runtime' in setup.manager.discovered_runtime_providers
    assert 'demo_runtime' in setup.host.manifests
    setup.runtime.ensure_endpoint_hub('recovery', provider_id='socket', channel_kind='socket')
    setup.runtime.set_recovery_endpoint('recovery')
    setup.host.forget_uninstalled('demo_runtime')
    assert setup.runtime.get_endpoint_hub('demo') is None
    assert 'demo_runtime' not in setup.manager.plugin_provider_ids
    assert setup.repository.get('demo') is not None


def test_plugin_detach_buffers_delivery_and_physical_removal_reroutes_it(setup):
    from pal.channel.contracts import TurnDeliveryBinding
    start(setup)
    endpoint = setup.runtime.get_endpoint('demo')
    binding = TurnDeliveryBinding(endpoint=endpoint.endpoint,
        response_handle=endpoint.build_response_handle(reply_target={}), control_scope_key='test')
    assert setup.host.detach('demo_runtime')['status'] == RuntimeStatus.OK
    reply = setup.runtime.queue_reply(binding, 'retain across plugin unload')
    hub = setup.runtime.get_endpoint_hub('demo')
    assert [item.delivery_id for item in hub.buffer] == [reply]
    recovery = setup.runtime.ensure_endpoint_hub('recovery', provider_id='socket', channel_kind='socket')
    setup.runtime.set_recovery_endpoint('recovery')
    shutil.rmtree(setup.root/'channel/providers/demo_runtime')
    result = setup.host.rescan()
    assert not result['scan_errors'], result
    assert setup.runtime.get_endpoint_hub('demo') is None
    assert [item.delivery_id for item in recovery.buffer] == [reply]
    assert setup.repository.get('demo') is not None


def test_provider_package_activation_uses_plugin_cleanup_owner(setup):
    from pal.packages.integration import RuntimeActivation
    from pal.packages.process import PackageError
    start(setup)
    activation = RuntimeActivation(setup.host)
    handle = setup.manager.runtime_provider_handles['demo_runtime']
    blocked = True
    def cleanup():
        if blocked:
            raise RuntimeError('busy transport resource')
    handle.cleanup_callbacks.append(cleanup)
    with pytest.raises(PackageError, match='could not be detached'):
        activation.before('provider', 'demo_runtime')
    assert setup.manager.runtime_provider_handles['demo_runtime'] is handle
    blocked = False
    state = activation.before('provider', 'demo_runtime')
    assert state['plugin_owned']
    # A failed cleanup logically detached the old generation. File replacement
    # must not quietly reactivate it; explicit attach/reload can do so afterward.
    assert activation.after('provider', 'demo_runtime', state) == 'installed_inactive'
    assert setup.host.attach('demo_runtime')['status'] == RuntimeStatus.OK


def test_model_surface_has_only_plugin_code_lifecycle(setup):
    start(setup)
    setup.host.publish_management_capabilities()
    setup.core.publish_module_capabilities('channel')
    names = setup.core.context.capability_registry.descriptors
    assert 'reload_plugin' in names
    assert 'rescan_plugins' in names
    assert 'restart_channel_endpoint' in names
    assert 'reload_channel_provider' not in names
    assert 'rescan_channel_providers' not in names


def test_async_shutdown_drains_on_transport_loop_before_releasing_generation(setup):
    import asyncio
    start(setup)
    handle = setup.manager.runtime_provider_handles['demo_runtime']
    calls = []

    async def scenario():
        await setup.runtime.start_async()
        owner = asyncio.get_running_loop()
        async def cleanup():
            assert asyncio.get_running_loop() is owner
            await asyncio.sleep(0)
            calls.append('closed')
        handle.cleanup_callbacks.append(cleanup)
        await setup.manager.stop_async()
        assert setup.manager.shutdown_errors
        assert setup.host.generations['demo_runtime']
        assert setup.runtime.get_endpoint('demo') is not None
        await setup.host.shutdown_async()
        assert not setup.host.shutdown_errors
        assert not setup.host.generations
        assert calls == ['closed']
        assert setup.runtime.get_endpoint_hub('demo') is not None
        await setup.manager.stop_async()
        assert not setup.manager.shutdown_errors
    asyncio.run(scenario())
