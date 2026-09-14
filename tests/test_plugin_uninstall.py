import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from pal.packages.service import PackageService
from pal.packages.process import PackageError
from pal.packages.uninstall import removal_record, validate_data_paths
from pal.plugins.settings import PluginSettings


def install_copy(root, declaration=''):
    target = root / 'plugins/community/demo'
    target.mkdir(parents=True)
    (target / 'plugin.toml').write_text('plugin_id = "demo"\n' + declaration)
    (target / 'legacy-data.txt').write_text('user data')
    return target


def test_default_uninstall_retains_copy_and_prevents_prepare(tmp_path):
    target = install_copy(tmp_path)
    service = PackageService(tmp_path)
    result = service.uninstall('demo')
    assert result['status'] == 'uninstalled' and result['data_retained']
    assert not target.exists()
    from pathlib import Path
    assert (Path(result['retired_path']) / 'legacy-data.txt').read_text() == 'user data'
    assert service.uninstall('demo')['operation_id'] == result['operation_id']
    with pytest.raises(PackageError):
        service.prepare('demo')


def test_purge_requires_manifest_before_detaching(tmp_path):
    target = install_copy(tmp_path)
    with pytest.raises(PackageError, match='declaration'):
        PackageService(tmp_path).uninstall('demo', purge_data=True)
    assert target.exists()
    assert removal_record(tmp_path, 'demo') is None


def test_purge_owned_data_and_retained_config(tmp_path):
    target = install_copy(tmp_path, '[uninstall]\ndata_paths = ["data/demo"]\n')
    data = tmp_path / 'data/demo'
    data.mkdir(parents=True)
    (data / 'prefs').write_text('private')
    settings = PluginSettings(tmp_path)
    settings.set('plugin.retained:demo', {'enabled': False, 'config': {'x': 1}})
    service = PackageService(tmp_path)
    service.uninstall('demo')
    result = service.uninstall('demo', purge_data=True)
    assert result['data_purged']
    assert not target.exists() and not data.exists()
    assert settings.get('plugin.retained:demo') is None
    assert not (tmp_path / 'packages/previous/plugin/demo').exists()


@pytest.mark.parametrize('path', ['.', 'data', '/tmp/demo', '../data/demo', 'data/../memory', 'data/core', 'data/demo/*'])
def test_purge_rejects_unsafe_paths(tmp_path, path):
    with pytest.raises(PackageError):
        validate_data_paths(tmp_path, [path])


def test_purge_rejects_symlinks_and_overlapping_ownership(tmp_path):
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data/demo').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(PackageError, match='symlink'):
        validate_data_paths(tmp_path, ['data/demo'])
    with pytest.raises(PackageError, match='overlaps'):
        validate_data_paths(tmp_path, ['data/other'], other_paths=['data/other/child'])


def test_retry_after_retirement_failure(tmp_path):
    target = install_copy(tmp_path)
    import pal.packages.uninstall as module
    original = module.os.replace
    def fail_target(source, destination):
        if source == target:
            raise OSError('injected retirement failure')
        return original(source, destination)
    with patch.object(module.os, 'replace', side_effect=fail_target):
        with pytest.raises(OSError, match='injected'):
            PackageService(tmp_path).uninstall('demo')
    assert removal_record(tmp_path, 'demo')['status'] == 'cleanup_failed'
    assert target.exists()
    assert PackageService(tmp_path).uninstall('demo')['status'] == 'uninstalled'


def test_failed_detached_generation_must_finish_cleanup(tmp_path):
    target = install_copy(tmp_path)
    host = SimpleNamespace(first_party_records={}, generations={'demo': object()},
                           _record=lambda name: SimpleNamespace(attached=False),
                           detach=Mock(return_value={'status': 'error'}), forget_uninstalled=Mock())
    activation = SimpleNamespace(host=host, gate=nullcontext)
    with pytest.raises(PackageError, match='cleanup'):
        PackageService(tmp_path, activation=activation).uninstall('demo')
    host.detach.assert_called_once_with('demo')
    host.forget_uninstalled.assert_not_called()
    assert target.exists()


def test_builtin_and_provider_cannot_be_uninstalled(tmp_path):
    (tmp_path / 'plugins/_builtin/demo').mkdir(parents=True)
    with pytest.raises(PackageError, match='Built-in'):
        PackageService(tmp_path).uninstall('demo')
    (tmp_path / 'channel/providers/provider').mkdir(parents=True)
    with pytest.raises(PackageError, match='Provider'):
        PackageService(tmp_path).uninstall('provider')


def test_live_uninstall_suspends_dependents_and_restores_preferences(tmp_path):
    import sys
    from pal.core import PalCore
    from pal.foundation.persistence import PalV2Database
    from pal.plugins.host import PluginHost
    from pal.plugins.models import PluginBundleModel
    from pal.packages.integration import RuntimeActivation
    from tests.test_plugin_raii import _write_plugin
    from tests.test_package_installation import package
    database = PalV2Database(tmp_path / 'pal.sqlite3')
    database.initialize([PluginBundleModel])
    community = tmp_path / 'plugins/community'
    _write_plugin(community, 'demo')
    _write_plugin(community, 'dependent', requires=('demo',))
    host = PluginHost(PalCore().context, tmp_path, services={'ledger': []})
    sys.path.insert(0, str(community))
    try:
        host.bootstrap()
        assert set(host.generations) == {'demo', 'dependent'}
        host.third_party_repository.set_config('demo', {'preference': 'saved'})
        service = PackageService(tmp_path, activation=RuntimeActivation(host))
        assert service.uninstall('demo')['status'] == 'uninstalled'
        assert not host.generations
        assert host._record('demo') is None
        assert host._record('dependent').config['suspended_by'] == ['demo']
        host.bootstrap()
        assert not host.generations
        assert host.attach('demo')['status'] == 'error'
        assert host._record('dependent') is not None
        # Reinstall disabled preferences without resurrecting a retired generation.
        retained = host.settings.get('plugin.retained:demo')
        retained['enabled'] = False
        host.settings.set('plugin.retained:demo', retained)
        artifact = package(tmp_path, 'demo')
        assert service.install(artifact)['status'] == 'ready'
        assert not host._record('demo').enabled
        assert host._record('demo').config['preference'] == 'saved'
        assert 'demo' not in host.generations
        assert service.uninstall('demo')['status'] == 'uninstalled'
        assert not host.settings.get('plugin.retained:demo')['enabled']
    finally:
        host.shutdown()
        sys.path.remove(str(community))
        for name in ('demo_runtime', 'dependent_runtime'):
            sys.modules.pop(name, None)
        database.close()


def test_builtin_disable_survives_host_restart(tmp_path):
    from pal.core import PalCore
    from pal.plugins.host import PluginHost
    from tests.test_plugin_raii import _write_plugin
    builtin = tmp_path / 'builtin'
    _write_plugin(builtin, 'demo')
    first = PluginHost(PalCore().context, tmp_path, builtin_root=builtin)
    first.rescan()
    assert first.disable('demo')['status'] == 'ok'
    second = PluginHost(PalCore().context, tmp_path, builtin_root=builtin)
    second.rescan()
    assert not second.first_party_records['demo'].enabled


def test_task_cleanup_is_not_complete_until_cancelled_task_exits():
    import asyncio
    from pal.core import PalCore
    from pal.plugins.lifecycle import PluginScope
    async def check():
        scope = PluginScope(PalCore().context, 'demo')
        task = scope.track_task(asyncio.create_task(asyncio.sleep(60)))
        await asyncio.sleep(0)
        assert scope.close()  # Cannot join the current loop synchronously.
        assert scope.cleanups
        with pytest.raises(asyncio.CancelledError):
            await task
        assert scope.close() == []
        assert not scope.cleanups
    asyncio.run(check())


def test_package_worker_joins_task_finally_before_removing_resources():
    import asyncio
    from pal.core import PalCore
    from pal.plugins.lifecycle import PluginScope
    async def check():
        finished = []
        async def worker():
            try:
                await asyncio.sleep(60)
            finally:
                await asyncio.sleep(0.01)
                finished.append(True)
        scope = PluginScope(PalCore().context, 'demo')
        scope.track_task(asyncio.create_task(worker()))
        await asyncio.sleep(0)
        assert await asyncio.to_thread(scope.close) == []
        assert finished == [True]
    asyncio.run(check())


def test_partial_purge_resumes_without_restoring_deleted_data(tmp_path):
    import pal.packages.uninstall as module
    install_copy(tmp_path, '[uninstall]\ndata_paths = ["data/demo"]\n')
    data = tmp_path / 'data/demo'
    data.mkdir(parents=True)
    (data / 'private').touch()
    original = module.shutil.rmtree
    def fail_retired(path, *args, **kwargs):
        if path == tmp_path / 'packages/previous/plugin/demo':
            raise OSError('injected purge failure')
        return original(path, *args, **kwargs)
    service = PackageService(tmp_path)
    with patch.object(module.shutil, 'rmtree', side_effect=fail_retired):
        with pytest.raises(OSError, match='injected'):
            service.uninstall('demo', purge_data=True)
    assert not data.exists()
    assert removal_record(tmp_path, 'demo')['status'] == 'cleanup_failed'
    result = service.uninstall('demo')  # Retry retains the explicit purge decision.
    assert result['data_purged'] and result['status'] == 'uninstalled'
    assert not data.exists()


@pytest.mark.parametrize('referenced', [False, True])
def test_purge_only_removes_unreferenced_private_environment(tmp_path, referenced):
    install_copy(tmp_path, '[uninstall]\ndata_paths = []\n')
    environment = tmp_path / 'packages/environments/plugin/demo/env'
    environment.mkdir(parents=True)
    if referenced:
        records = tmp_path / 'packages/records'
        records.mkdir()
        (records / 'plugin-other.json').write_text(json.dumps({'id': 'other', 'environment': str(environment)}))
    result = PackageService(tmp_path).uninstall('demo', purge_data=True)
    assert result['status'] == 'uninstalled'
    assert environment.exists() is referenced


def test_bad_removal_record_does_not_abort_unrelated_plugin_discovery(tmp_path):
    from pal.core import PalCore
    from pal.foundation.persistence import PalV2Database
    from pal.plugins.host import PluginHost
    from pal.plugins.models import PluginBundleModel
    from tests.test_plugin_raii import _write_plugin
    database = PalV2Database(tmp_path / 'pal.sqlite3')
    database.initialize([PluginBundleModel])
    community = tmp_path / 'plugins/community'
    _write_plugin(community, 'broken')
    _write_plugin(community, 'healthy')
    records = tmp_path / 'packages/records'
    records.mkdir(parents=True)
    (records / 'plugin-broken.json').write_text('{incomplete')
    host = PluginHost(PalCore().context, tmp_path)
    try:
        result = host.rescan()
        assert result['scan_errors']
        assert 'healthy' in host.manifests
        assert 'broken' not in host.manifests
    finally:
        database.close()
