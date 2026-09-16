"""Execution replacement obeys the existing plugin generation lifecycle."""
import asyncio
from pathlib import Path
import sqlite3
import sys

import pytest

from pal.core import PalCore
from pal.core.main_context import MainContext
from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
from pal.execution import register_with_core
from pal.execution.runtime import ExecutionRuntime
from pal.execution.worker_extensions import worker_extensions, worker_dependency_paths
from pal.plugins import PluginHost
from pal.shared import RuntimeStatus
from pal.shared.tool_protocol import new_tool_call


class ReplacementRuntime(ExecutionRuntime):
    def execution_diagnostics(self):
        return {'backend': 'test-extension'}


class Extension:
    def __init__(self):
        self.busy = self.fail = False
        self.closed = []

    def build_runtime(self, previous):
        return ReplacementRuntime(sync_executor=previous.sync_executor)

    def build_provider(self, runtime):
        return runtime.build_introspection_provider()

    def build_state_port(self, runtime):
        return runtime.build_runtime_state_port()

    def activate(self, context, handle, runtime):
        if self.fail:
            raise RuntimeError('activation failed')

    def check_detach(self, runtime):
        if self.busy:
            raise RuntimeError('execution busy')

    def close(self, runtime):
        self.closed.append(runtime)


@pytest.fixture
def execution():
    core = PalCore(context=MainContext())
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    try:
        yield core, core.context.execution_runtime
    finally:
        core.context.execution_runtime.shutdown()
        core.close()


def test_replace_restore_preserves_state_and_unrelated_tools(execution):
    core, slot = execution
    original = slot.implementation
    generation = slot.registry_generation
    state, pager, pool = slot.logical_state, slot.tool_result_pager, slot.sync_executor
    extension = Extension()
    handle = ModuleHandle('test', MODULE_TIER_DETACHABLE)
    slot.install(extension, core.context, handle)
    assert slot.execution_diagnostics()['backend'] == 'test-extension'
    assert set(slot.registry_generation.search_records) == set(generation.search_records)
    assert slot.logical_state is state and slot.tool_result_pager is pager and slot.sync_executor is pool
    result = asyncio.run(slot.execute_tool_async(new_tool_call(name='run_shell', args={'cmd': 'printf replacement'})))
    assert result.ok and 'replacement' in result.text
    slot.uninstall(core.context, handle)
    assert slot.implementation is original
    assert slot.logical_state is state and slot.tool_result_pager is pager and slot.sync_executor is pool
    assert len(extension.closed) == 1
    assert core.context.require_port('execution:execution') is slot


def test_activation_failure_leaves_original_projection(execution):
    core, slot = execution
    original, generation = slot.implementation, slot.registry_generation
    provider = core.context.introspection_registry['execution']
    extension = Extension()
    extension.fail = True
    with pytest.raises(RuntimeError, match='activation failed'):
        slot.install(extension, core.context, ModuleHandle('test', MODULE_TIER_DETACHABLE))
    assert slot.implementation is original and slot.registry_generation is generation
    assert core.context.introspection_registry['execution'] is provider
    assert len(extension.closed) == 1


def test_competing_extension_does_not_replace_active_owner(execution):
    core, slot = execution
    owner = ModuleHandle('first', MODULE_TIER_DETACHABLE)
    extension = Extension()
    slot.install(extension, core.context, owner)
    with pytest.raises(RuntimeError, match='already active'):
        slot.install(Extension(), core.context, ModuleHandle('second', MODULE_TIER_DETACHABLE))
    slot.uninstall(core.context, ModuleHandle('unrelated', MODULE_TIER_DETACHABLE))
    assert slot.execution_diagnostics()['backend'] == 'test-extension'
    slot.uninstall(core.context, owner)


def test_host_rejects_busy_detach_before_withdrawing(execution, tmp_path):
    core, slot = execution
    original = slot.implementation
    root = tmp_path / 'builtin'
    plugin = root / 'replacement'
    plugin.mkdir(parents=True)
    (plugin / 'plugin.toml').write_text('''plugin_id = "replacement"
entrypoint = "replacement_runtime"
version = "1.0"
module_id = "replacement"
enabled_by_default = true
lifecycle_protocol = "raii.v1"
requires_ports = ["execution:extensions"]
''')
    (root / 'replacement_runtime.py').write_text('''from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
class Plugin:
    plugin_id = 'replacement'
    version = '1.0'
    def __init__(self, extension): self.extension = extension
    def start(self, scope):
        return ModuleHandle('replacement', MODULE_TIER_DETACHABLE, detachable=True, execution_extension=self.extension)
def build_plugin(extension): return Plugin(extension)
''')
    extension = Extension()
    sys.path.insert(0, str(root))
    host = PluginHost(core.context, tmp_path, builtin_root=root, services={'extension': extension})
    try:
        host.bootstrap()
        assert 'replacement' in host.generations
        active = slot.implementation
        extension.busy = True
        assert host.detach('replacement')['status'] == RuntimeStatus.ERROR
        assert slot.implementation is active
        assert core.context.module_registry.get('replacement') is not None
        assert 'run_shell' in slot.registry_generation.direct_aliases
        extension.busy = False
        assert host.detach('replacement')['status'] == RuntimeStatus.OK
        assert slot.implementation is original
        assert core.context.module_registry.get('replacement') is None
    finally:
        extension.busy = False
        host.shutdown()
        sys.path.remove(str(root))


def test_worker_discovery_requires_enabled_attached_plugin(tmp_path):
    plugin = tmp_path / 'plugin'
    plugin.mkdir()
    (plugin / 'plugin.toml').write_text('[execution]\nrole_entrypoint="example:activate"\nworker_modules=["missing_worker_module"]\n')
    with sqlite3.connect(tmp_path / 'pal.sqlite3') as db:
        db.execute('CREATE TABLE plugin_bundles (filesystem_path TEXT, enabled INT, attached INT)')
        db.execute('INSERT INTO plugin_bundles VALUES (?, 1, 0)', (str(plugin),))
    assert worker_extensions(tmp_path) == []
    with sqlite3.connect(tmp_path / 'pal.sqlite3') as db:
        db.execute('UPDATE plugin_bundles SET attached=1')
    assert worker_extensions(tmp_path)[0][0] == plugin
    with pytest.raises(RuntimeError, match='dependency unavailable'):
        worker_dependency_paths(tmp_path, {})


def test_worker_uses_configured_database(tmp_path):
    custom = tmp_path / 'custom.sqlite3'
    plugin = tmp_path / 'plugin'
    plugin.mkdir()
    (plugin / 'plugin.toml').write_text('[execution]\nrole_entrypoint="example:activate"\n')
    with sqlite3.connect(custom) as db:
        db.execute('CREATE TABLE plugin_bundles (filesystem_path TEXT, enabled INT, attached INT)')
        db.execute('INSERT INTO plugin_bundles VALUES (?, 1, 1)', (str(plugin),))
    assert worker_extensions(tmp_path) == []
    assert worker_extensions(tmp_path, database_path=custom)[0][0] == plugin
    assert worker_dependency_paths(tmp_path, {}, database_path=custom) == [plugin]
