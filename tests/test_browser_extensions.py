from pathlib import Path
import json

import pytest

from pal.web_fetch.browser_service import _PlaywrightCliWorker, BrowserServiceError, _SessionRecord
from pal.web_fetch.extensions import inspect_extension, read_extensions, validate_extension_url


def extension(tmp_path):
    path = tmp_path / 'extension'
    path.mkdir()
    (path / 'manifest.json').write_text(json.dumps({'manifest_version': 3, 'name': 'Fixture', 'version': '1.0'}))
    return path


def test_mount_persists_and_unmount_handles_deleted_source(tmp_path):
    path = extension(tmp_path)
    worker = _PlaywrightCliWorker(runtime_root=tmp_path / 'runtime', max_concurrency=1)
    key = 'a' * 64
    result = worker._extension_action(key, 'extension_manage', {'operation': 'mount', 'path': str(path)}, True, 1000)
    item = result['extensions'][0]
    assert result['browser_running'] is False
    assert len(item['id']) == 32
    assert validate_extension_url(item['manifest_url'], [item]) == item['manifest_url']
    with pytest.raises(ValueError):
        validate_extension_url('chrome-extension://' + 'b' * 32 + '/manifest.json', [item])
    (path / 'manifest.json').unlink()
    worker._extension_action(key, 'extension_manage', {'operation': 'unmount', 'extension_id': item['id']}, True, 1000)
    assert read_extensions(worker.paths.profiles / key) == []


def test_failed_shutdown_does_not_change_mounts(tmp_path):
    path = extension(tmp_path)
    worker = _PlaywrightCliWorker(runtime_root=tmp_path / 'runtime', max_concurrency=1)
    key = 'b' * 64
    worker.sessions[key] = _SessionRecord(key=key, name='fixture', persistent=True)
    def fail(*args, **kwargs):
        raise BrowserServiceError('shutdown failed')
    worker._run = fail
    with pytest.raises(BrowserServiceError):
        worker._extension_action(key, 'extension_manage', {'operation': 'mount', 'path': str(path)}, True, 1000)
    assert key in worker.sessions
    assert read_extensions(worker.paths.profiles / key) == []


def test_extensions_are_scoped_and_start_with_full_chromium(tmp_path):
    path = extension(tmp_path)
    worker = _PlaywrightCliWorker(runtime_root=tmp_path / 'runtime', max_concurrency=1)
    key = 'c' * 64
    worker._extension_action(key, 'extension_manage', {'operation': 'mount', 'path': str(path)}, True, 1000)
    calls = []
    worker._run = lambda record, argv, **kwargs: calls.append(argv) or ''
    worker._ensure_session(key, persistent=True, timeout_ms=1000, restore_page=False)
    config_path = Path(next(arg.split('=', 1)[1] for arg in calls[0] if arg.startswith('--config=')))
    config = json.loads(config_path.read_text())['browser']['launchOptions']
    assert config['channel'] == 'chromium'
    assert '--load-extension=' + str(path) in config['args']
    worker._ensure_session('d' * 64, persistent=True, timeout_ms=1000, restore_page=False)
    assert '--config=' + str(worker.paths.config) in calls[1]
    assert 'args' not in json.loads(worker.paths.config.read_text())['browser']['launchOptions']
    with pytest.raises(BrowserServiceError, match='persistent'):
        worker._extension_action('e' * 64, 'extensions', {}, False, 1000)


def test_reload_validates_current_manifest(tmp_path):
    path = extension(tmp_path)
    worker = _PlaywrightCliWorker(runtime_root=tmp_path / 'runtime', max_concurrency=1)
    key = 'f' * 64
    result = worker._extension_action(key, 'extension_manage', {'operation': 'mount', 'path': str(path)}, True, 1000)
    extension_id = result['extensions'][0]['id']
    (path / 'manifest.json').write_text('{}')
    with pytest.raises(ValueError, match='Manifest V3'):
        worker._extension_action(key, 'extension_manage', {'operation': 'reload', 'extension_id': extension_id}, True, 1000)
    assert read_extensions(worker.paths.profiles / key)[0]['id'] == extension_id


def test_full_chromium_detection_requires_matching_revision(tmp_path):
    from pal.web_fetch.browser_service import BrowserRuntimePaths, _chromium_installed
    paths = BrowserRuntimePaths(tmp_path)
    package = paths.tooling_current / 'node_modules/playwright-core/browsers.json'
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({'browsers': [{'name': 'chromium', 'revision': '1243'}]}))
    old = paths.browser_cache / 'chromium-1234/chrome-linux/chrome'
    old.parent.mkdir(parents=True)
    old.touch()
    assert not _chromium_installed(paths)
    current = paths.browser_cache / 'chromium-1243/chrome-linux-arm64/chrome'
    current.parent.mkdir(parents=True)
    current.touch()
    assert _chromium_installed(paths)


def test_shell_only_startup_schedules_full_browser_install(tmp_path):
    worker = _PlaywrightCliWorker(runtime_root=tmp_path, max_concurrency=1)
    worker._cli_ready = lambda: True
    installs = []
    worker._schedule_install = lambda **kwargs: installs.append(kwargs)
    def fail(**kwargs):
        raise BrowserServiceError("Executable doesn't exist: chromium-1243/chrome", code='cli_command_failed')
    worker._execute_ready = fail
    with pytest.raises(BrowserServiceError) as error:
        worker.execute(session_key='a' * 64, action='navigate', args={'url':'https://example.com'}, persistent=True, timeout_ms=1000)
    assert error.value.code == 'dependency_installing'
    assert installs == [{'browser_only': True, 'reason': 'Chromium build missing'}]
