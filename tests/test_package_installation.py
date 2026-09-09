from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
import threading
import time
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

from pal.packages.archive import build, digest, unpack
from pal.packages.environment import PackageEnvironment, installed_environment
from pal.packages.process import PackageError, install_lock, run_command
from pal.packages.service import PackageService


def wheel(root: Path, name: str, version: str, *, requirements=(), code="") -> Path:
    path = root / f"{name}-{version}-py3-none-any.whl"
    info = f"{name}-{version}.dist-info"
    files = {f"{name}/__init__.py": code or f"VERSION = {version!r}\n",
             f"{info}/METADATA": f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n" + "".join(f"Requires-Dist: {req}\n" for req in requirements),
             f"{info}/WHEEL": "Wheel-Version: 1.0\nGenerator: pal-test\nRoot-Is-Purelib: true\nTag: py3-none-any\n"}
    files[f"{info}/RECORD"] = "".join(f"{name},,\n" for name in files) + f"{info}/RECORD,,\n"
    with zipfile.ZipFile(path, "w") as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return path


def package(root: Path, name="demo", version="1.0", *, backend: Path | None = None,
            hooks: str | None = None, runtime: str | None = None) -> Path:
    files = {"host/plugin.toml": f'plugin_id = "{name}"\nversion = "{version}"\nentrypoint = "{name}_runtime"\nlifecycle_protocol = "raii.v1"\nmodule_id = "{name}"\n',
             f"host/{name}_runtime.py": runtime or "def build_plugin(context): return context\n"}
    if hooks:
        files["hooks.py"] = hooks
    contents = {key: value.encode() for key, value in files.items()}
    if backend:
        contents[backend.name] = backend.read_bytes()
    manifest = dict(protocol="pal.install.v1", kind="plugin", id=name, version=version,
                    python="venv" if backend else "host", wheel=backend.name if backend else None,
                    hooks="hooks.py" if hooks else None,
                    files={key: hashlib.sha256(value).hexdigest() for key, value in contents.items()})
    path = root / f"{name}-{version}.palpkg"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("package.json", json.dumps(manifest))
        for name, value in contents.items():
            archive.writestr(name, value)
    return path


def test_conflicting_dependencies_and_repeated_preparation_are_isolated(tmp_path):
    dependency = "pal_test_private_dependency"
    assert importlib.util.find_spec(dependency) is None
    before_path = list(sys.path)
    service = PackageService(tmp_path / "runtime")
    environments = []
    for name, version in (("first", "1.0"), ("second", "2.0")):
        dep = wheel(tmp_path, dependency, version)
        backend = wheel(tmp_path, f"pal_test_{name}", "1.0", requirements=[f"{dependency} @ {dep.as_uri()}"])
        hooks = f'''def verify(context):
    import sys
    import {dependency} as dependency
    return {{"ok": dependency.VERSION == {version!r} and sys.prefix != sys.base_prefix,
            "dependency_version": dependency.VERSION, "python": sys.executable}}
'''
        artifact = package(tmp_path, name, backend=backend, hooks=hooks)
        result = service.install(artifact)
        assert result["status"] == "ready"
        assert result["verify_result"]["dependency_version"] == version
        target = tmp_path / "runtime/plugins/community" / name
        env = installed_environment(target)
        assert env is not None
        assert str(env.python_executable) == result["verify_result"]["python"]
        environments.append(env.root)
        # Repeated preparation must not mutate the interpreter still used by
        # the currently installed sidecar, even with an identical artifact.
        sentinel = env.root / "sidecar-live"
        sentinel.touch()
        repeated = service.prepare(name)
        assert repeated["environment"] != result["environment"]
        assert repeated["verify_result"]["dependency_version"] == version
        assert sentinel.is_file()
    assert environments[0] != environments[1]
    assert sys.path == before_path
    assert importlib.util.find_spec(dependency) is None


def test_verify_failure_preserves_installed_generation(tmp_path):
    service = PackageService(tmp_path / "runtime")
    first = service.install(package(tmp_path))
    target = tmp_path / "runtime/plugins/community/demo"
    receipt = (target / ".pal-package.json").read_bytes()
    bad = package(tmp_path, version="2.0", hooks='def verify(context): return {"ok": False, "detail": "deliberate failure"}\n')
    with pytest.raises(PackageError, match="deliberate failure"):
        service.install(bad)
    assert (target / ".pal-package.json").read_bytes() == receipt
    assert first["version"] == "1.0"
    record = service.status(name="demo")["items"][0]
    assert record["stage"] == "verify" and record["status"] == "failed"


def test_activation_failure_rolls_back_files(tmp_path):
    service = PackageService(tmp_path / "runtime")
    service.install(package(tmp_path))
    events = []

    class Activation:
        def gate(self):
            from contextlib import nullcontext
            return nullcontext()
        def before(self, kind, name):
            events.append("detach")
            return {"existed": True}
        def after(self, kind, name, state):
            events.append("attach")
            assert len(list((tmp_path / "runtime/plugins/community").glob("*/plugin.toml"))) == 1
            raise RuntimeError("cannot attach")
        def restore(self, kind, name, state):
            events.append("restore")

    service.activation = Activation()
    with pytest.raises(RuntimeError, match="cannot attach"):
        service.install(package(tmp_path, version="2.0"))
    manifest = (tmp_path / "runtime/plugins/community/demo/plugin.toml").read_text()
    assert 'version = "1.0"' in manifest
    assert events == ["detach", "attach", "detach", "restore"]


def test_archive_corruption_and_traversal_rejected(tmp_path):
    artifact = package(tmp_path)
    with zipfile.ZipFile(artifact, "a") as archive:
        archive.writestr("unexpected", "not in checksums")
    with pytest.raises(PackageError, match="checksums"):
        unpack(artifact, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
    with zipfile.ZipFile(tmp_path / "traversal.palpkg", "w") as archive:
        archive.writestr("../outside", "bad")
    with pytest.raises(ValueError, match="unsafe"):
        unpack(tmp_path / "traversal.palpkg", tmp_path / "bad")
    assert not (tmp_path / "outside").exists()


def test_build_host_only_package_and_prepare_builtin_without_hooks(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "package.toml").write_text('kind="plugin"\nid="demo"\nversion="1.0"\npython="host"\nhost_files=["plugin.toml", "demo_runtime.py"]\n')
    with zipfile.ZipFile(package(tmp_path)) as archive:
        for name in ("plugin.toml", "demo_runtime.py"):
            (source / name).write_bytes(archive.read(f"host/{name}"))
    result = PackageService(tmp_path / "runtime").install(build(source, tmp_path / "dist"))
    assert result["environment"] is None
    builtin = PackageService(tmp_path / "runtime").prepare("checklist", kind="builtin")
    assert builtin["status"] == "ready" and builtin["verify_result"]["skipped"]


def test_environment_never_inherits_pythonpath(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", "/host/site-packages")
    monkeypatch.setenv("PYTHONHOME", "/host")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1080")
    env = PackageEnvironment(tmp_path).child_env()
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:1080"
    assert env["PYTHONNOUSERSITE"] == "1"
    with pytest.raises(RuntimeError, match="missing"):
        PackageEnvironment(tmp_path).python_executable


def test_install_lock_serializes_threads(tmp_path):
    events = []
    entered = threading.Event()
    def second():
        entered.set()
        with install_lock(tmp_path):
            events.append("second")
    with install_lock(tmp_path):
        thread = threading.Thread(target=second)
        thread.start()
        assert entered.wait(1)
        time.sleep(0.15)
        assert events == []
    thread.join(2)
    assert events == ["second"]


def test_package_tools_remain_indirect(tmp_path):
    from pal.core import PalCore
    from pal.plugins.host import PluginHost
    core = PalCore()
    host = PluginHost(core.context, tmp_path)
    host.publish_management_capabilities()
    contracts = core._build_llm_tool_contracts()
    direct = {item["function"]["name"] for item in contracts}
    assert not direct & {"package_install", "package_prepare", "package_status"}
    listing = next(item["function"] for item in contracts if item["function"]["name"] == "plugins_list")
    assert "package_install" in listing["description"]
    host.shutdown()


def test_live_job_uses_existing_host_and_rolls_back_failed_attach(tmp_path):
    from pal.core import PalCore
    from pal.foundation.persistence import PalV2Database
    from pal.plugins.host import PluginHost
    from pal.plugins.models import PluginBundleModel
    from pal.packages.jobs import PackageJobs
    database = PalV2Database(tmp_path / "pal.sqlite3")
    database.initialize([PluginBundleModel])
    host = PluginHost(PalCore().context, tmp_path)
    jobs = PackageJobs(host)
    runtime = '''from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
def build_plugin(context):
    class Plugin:
        def start(self, scope):
            handle = ModuleHandle(module_id="live", tier=MODULE_TIER_DETACHABLE,
                                  detachable=True, ports={"version": "1.0"})
            scope.context.register_module(handle)
            return handle
    return Plugin()
'''
    def wait(job_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            state = jobs.status(job_id)["jobs"][0]
            if state["status"] != "running":
                return state
            time.sleep(0.05)
        pytest.fail("installation job did not finish")
    try:
        # Tool execution normally holds a read fence: queuing must not synchronously acquire write.
        with host.context.execution_runtime.lifecycle_gate.read():
            job = jobs.start("install", path=package(tmp_path, "live", runtime=runtime))
            assert job["job_id"]
        result = wait(job["job_id"])
        assert result["status"] == "ready", result
        assert host.generations["live"].handle.ports["version"] == "1.0"
        failed = package(tmp_path, "live", "2.0", runtime='def build_plugin(context): raise RuntimeError("bad generation")\n')
        result = wait(jobs.start("install", path=failed)["job_id"])
        assert result["status"] == "failed", result
        assert host.generations["live"].handle.ports["version"] == "1.0"
    finally:
        jobs.shutdown()
        host.shutdown()
        database.close()


def test_offline_install_does_not_replace_live_files(tmp_path):
    from pal.packages.process import runtime_lease
    service = PackageService(tmp_path / "runtime")
    service.install(package(tmp_path))
    with runtime_lease(service.runtime_root):
        with pytest.raises(PackageError, match="Pal is running"):
            service.install(package(tmp_path, version="2.0"))
    assert 'version = "1.0"' in (service.runtime_root / "plugins/community/demo/plugin.toml").read_text()


def test_cancellation_stops_hook_process(tmp_path):
    from pal.packages.process import CommandControl, command_control
    control = CommandControl()
    marker = tmp_path / "started"
    errors = []
    def worker():
        try:
            with command_control(control):
                run_command([sys.executable, "-c", f"from pathlib import Path; import time; Path({str(marker)!r}).touch(); time.sleep(60)"])
        except Exception as exc:
            errors.append(str(exc))
    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert marker.exists()
    control.stop()
    thread.join(3)
    assert not thread.is_alive()
    assert errors


def test_release_installer_prepares_before_wizard_can_start_service():
    script = (Path(__file__).resolve().parents[1] / "scripts/install_package.sh").read_text()
    assert script.index('"$pal_bin" package prepare --all-builtin') < script.index('"$pal_bin" setup --runtime-root')


def test_host_payload_cannot_copy_external_symlinks(tmp_path):
    source = tmp_path / "source"
    (source / "host_assets").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("private")
    (source / "host_assets/linked").symlink_to(outside)
    (source / "package.toml").write_text('kind="plugin"\nid="demo"\nversion="1.0"\npython="host"\nhost_files=["host_assets"]\n')
    with pytest.raises(PackageError, match="symlinks"):
        build(source, tmp_path / "dist")


def test_community_cannot_shadow_builtin_before_setup(tmp_path):
    with pytest.raises(PackageError, match="conflicts with a built-in"):
        PackageService(tmp_path / "fresh-runtime").install(package(tmp_path, "checklist"))


def test_cancellation_after_successful_switch_keeps_ready_status(tmp_path):
    from contextlib import nullcontext
    from pal.packages.process import CommandControl, command_control
    control = CommandControl()

    class Activation:
        def gate(self):
            return nullcontext()
        def before(self, kind, name):
            return {}
        def after(self, kind, name, state):
            control.stop()
            return "attached"

    service = PackageService(tmp_path / "runtime", activation=Activation())
    with command_control(control):
        result = service.install(package(tmp_path))
    assert result["status"] == "ready"
    assert service.status(name="demo")["items"][0]["status"] == "ready"


def test_publish_rejects_symlink_and_cleans_failed_staging(tmp_path):
    service = PackageService(tmp_path / "runtime")
    source = tmp_path / "source"
    source.mkdir()
    target = tmp_path / "target"
    target.symlink_to(source, target_is_directory=True)
    record = dict(id="demo", kind="provider")
    with pytest.raises(PackageError, match="symlink"):
        service._publish(source, target, record)
    assert target.is_symlink()
    target.unlink()
    with patch("pal.packages.service.shutil.copytree", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            service._publish(source, target, record)
    assert list((service.root / "staging").iterdir()) == []


def test_legacy_prepare_does_not_report_failed_install_as_ready(tmp_path):
    service = PackageService(tmp_path / "runtime")
    service._save(dict(kind="provider", id="demo"), "install", status="failed")
    with pytest.raises(PackageError, match="reinstall its .whl"):
        service.prepare("demo", kind="provider")


def test_corrupt_record_does_not_hide_other_package_status(tmp_path):
    service = PackageService(tmp_path / "runtime")
    service.install(package(tmp_path))
    service._record_path("plugin", "broken").write_text("{")
    result = service.status()
    assert [record["id"] for record in result["items"]] == ["demo"]
    assert result["errors"][0]["record"] == "plugin-broken.json"


def test_interrupted_artifact_cache_is_rebuilt_from_original(tmp_path):
    service = PackageService(tmp_path / "runtime")
    artifact = package(tmp_path)
    cached = service.root / "artifacts" / digest(artifact)
    cached.mkdir(parents=True)
    (cached / "package.json").write_text("{")
    assert service.install(artifact)["status"] == "ready"
    assert (cached / "host/plugin.toml").is_file()


def test_provider_scan_failures_cannot_report_success(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from pal.packages.integration import RuntimeActivation
    directory = tmp_path / "channel/providers/demo"
    directory.mkdir(parents=True)
    (directory / "provider.toml").write_text('enabled=true\n')
    manager = Mock()
    manager.discovered_runtime_providers = {}
    manager._provider_hub_ids.return_value = []
    manager.rescan_providers.return_value = {"scan_errors": [f"{directory}: RuntimeError: broken backend"]}
    host = SimpleNamespace(runtime_root=tmp_path, context=Mock())
    host.context.require_port.return_value = manager
    activation = RuntimeActivation(host)
    with pytest.raises(PackageError, match="broken backend"):
        activation.after("provider", "demo", {"existed": False, "endpoints": []})
    manager.rescan_providers.return_value = {"scan_errors": []}
    with pytest.raises(PackageError, match="not discovered"):
        activation.after("provider", "demo", {"existed": False, "endpoints": []})
    manager.discovered_runtime_providers = {"demo": object()}
    manager.rescan_providers.return_value = {"scan_errors": ["/providers/other/: broken"]}
    activation._check_provider_scan(manager, "demo", manager.rescan_providers())


def test_new_provider_rollback_removes_discovery(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from pal.packages.integration import RuntimeActivation
    manager = Mock()
    manager._provider_hub_ids.return_value = []
    manager.rescan_providers.return_value = {"scan_errors": []}
    host = SimpleNamespace(context=Mock())
    host.context.require_port.return_value = manager
    RuntimeActivation(host).restore("provider", "demo", {"existed": False})
    manager.rescan_providers.assert_called_once()
