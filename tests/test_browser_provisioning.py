from pathlib import Path
from unittest.mock import patch

import pytest

from pal.packages.process import PackageError
from pal.web_fetch.browser_service import BrowserRuntimePaths
from pal.web_fetch import provisioning


def test_matched_dependencies_do_not_install_again(tmp_path):
    with patch.object(provisioning, "_ensure_node"), patch.object(provisioning, "_ensure_cli"), \
         patch.object(provisioning, "_chromium_installed", return_value=True), \
         patch.object(provisioning, "verify", return_value={"ok": True}), \
         patch.object(provisioning, "run_command") as command:
        assert provisioning.prepare(tmp_path)["ok"]
        command.assert_not_called()


def test_missing_browser_installs_full_chromium(tmp_path):
    with patch.object(provisioning, "_ensure_node"), patch.object(provisioning, "_ensure_cli"), \
         patch.object(provisioning, "_chromium_installed", return_value=False), \
         patch.object(provisioning, "verify", return_value={"ok": True}), \
         patch.object(provisioning, "run_command") as command:
        provisioning.prepare(tmp_path)
        assert command.call_args.args[0][-3:] == ["install", "chromium", "--no-shell"]


def test_only_missing_system_libraries_trigger_system_install(tmp_path):
    with patch.object(provisioning, "_ensure_node"), patch.object(provisioning, "_ensure_cli"), \
         patch.object(provisioning, "_chromium_installed", return_value=True), \
         patch.object(provisioning, "_install_system_libraries") as libraries, \
         patch.object(provisioning, "verify", side_effect=[PackageError("Host system is missing dependencies"), {"ok": True}]):
        assert provisioning.prepare(tmp_path)["ok"]
        libraries.assert_called_once()
    with patch.object(provisioning, "_ensure_node"), patch.object(provisioning, "_ensure_cli"), \
         patch.object(provisioning, "_chromium_installed", return_value=True), \
         patch.object(provisioning, "_install_system_libraries") as libraries, \
         patch.object(provisioning, "verify", side_effect=PackageError("Extension verification failed")):
        with pytest.raises(PackageError, match="Extension"):
            provisioning.prepare(tmp_path)
        libraries.assert_not_called()


def test_node_18_does_not_pass_dependency_check(tmp_path):
    with patch.object(provisioning, "node_major", return_value=18), \
         patch.object(provisioning, "_installed_cli_version", return_value=provisioning.PLAYWRIGHT_CLI_VERSION), \
         patch.object(provisioning, "_chromium_installed", return_value=True):
        state = provisioning.inspect(tmp_path)
        assert not state["ok"]
        assert state["required_node_major"] == 20


def test_missing_system_permissions_fail_without_prompting(tmp_path):
    with patch.object(provisioning.platform, "system", return_value="Linux"), \
         patch.object(provisioning.os, "geteuid", return_value=1000), \
         patch.object(provisioning.shutil, "which", side_effect=lambda name, **kw: "/usr/bin/" + name), \
         patch.object(provisioning, "run_command", side_effect=PackageError("sudo: a password is required")) as command:
        with pytest.raises(PackageError, match="password is required"):
            provisioning._install_system_libraries(BrowserRuntimePaths(tmp_path))
        assert command.call_args.args[0][:2] == ["/usr/bin/sudo", "-n"]


def test_running_sidecar_recognizes_external_dependency_repair(tmp_path):
    from pal.web_fetch.browser_service import _PlaywrightCliWorker, PLAYWRIGHT_CLI_VERSION
    worker = _PlaywrightCliWorker(runtime_root=tmp_path, max_concurrency=1)
    worker._node_major_cached = None
    worker._cli_version_cached = ""
    worker.paths.cli.parent.mkdir(parents=True, exist_ok=True)
    worker.paths.cli.touch()
    with patch.object(worker, "_node_major", return_value=24), \
         patch.object(worker, "_detected_cli_version", return_value=PLAYWRIGHT_CLI_VERSION):
        assert worker._cli_ready()


def test_full_chromium_detection_accepts_playwright_macos_layout(tmp_path):
    import json
    from pal.web_fetch.browser_service import _chromium_installed
    paths = BrowserRuntimePaths(tmp_path)
    metadata = paths.tooling_current / "node_modules/playwright-core/browsers.json"
    metadata.parent.mkdir(parents=True)
    metadata.write_text(json.dumps({"browsers": [{"name": "chromium", "revision": "1243"}]}))
    executable = paths.browser_cache / "chromium-1243/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing"
    executable.parent.mkdir(parents=True)
    executable.touch()
    assert _chromium_installed(paths)
