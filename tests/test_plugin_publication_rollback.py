"""Regressions for the staged publication/rollback contract in the core model."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pal.core import PalCore
from pal.plugins import PluginHost
from pal.plugins.lifecycle import PluginScope
from pal.shared import RuntimeStatus
from pal.skill.repository import ReadOnlySkillRepository
from pal.skill.service import SkillService
from tests.test_plugin_raii import _write_plugin


class MemoryPluginRepository:
    def __init__(self):
        self.rows = {}

    def upsert_discovered(self, **values):
        self.rows[values["plugin_id"]] = SimpleNamespace(
            **values, enabled=values["enabled_by_default"], attached=False,
            config_blob={}, last_load_status="discovered", last_error=None)

    def get(self, plugin_id):
        return self.rows.get(plugin_id)

    def set_config(self, plugin_id, config):
        self.rows[plugin_id].config_blob = dict(config)

    def set_attached(self, plugin_id, attached):
        self.rows[plugin_id].attached = attached

    def set_enabled(self, plugin_id, enabled):
        self.rows[plugin_id].enabled = enabled
        return self.rows[plugin_id]

    def set_load_status(self, plugin_id, *, status, error_text=None):
        self.rows[plugin_id].last_load_status = status
        self.rows[plugin_id].last_error = error_text


@pytest.fixture(params=["first_party", "third_party"])
def staged_plugin(tmp_path, monkeypatch, request):
    root = (tmp_path / "builtin" if request.param == "first_party"
            else tmp_path / "plugins" / "community")
    _write_plugin(root, "publication_test")
    source = root / "publication_test_runtime.py"
    source.write_text(source.read_text().replace(
        "        return handle", "        scope.subscribe_core_events()\n        return handle"))
    monkeypatch.syspath_prepend(str(root))
    core = PalCore()
    host = PluginHost(context=core.context, runtime_root=tmp_path,
                      builtin_root=tmp_path / "builtin", services={"ledger": []},
                      third_party_repository=MemoryPluginRepository())
    host.rescan()
    yield host, core, root
    host.shutdown()


def assert_rolled_back(host, core):
    assert "publication_test" not in host.generations
    assert "publication_test" not in host.first_party_handles
    assert "publication_test" not in host.third_party_handles
    assert core.context.module_registry.get("publication_test") is None
    record = host._record("publication_test")
    assert not record.attached
    assert record.last_load_status == "load_failed"


@pytest.mark.parametrize("cleanup_blocked", [False, True])
@pytest.mark.parametrize("phase", ["start", "replay"])
def test_cancelled_start_rolls_back_and_preserves_failed_cleanup(staged_plugin, cleanup_blocked, phase):
    host, core, root = staged_plugin
    attempts = []

    async def cleanup():
        attempts.append("cleanup")
        if cleanup_blocked:
            raise asyncio.CancelledError("cleanup cancelled")

    host.services["ledger"].append(cleanup)
    source = root / "publication_test_runtime.py"
    code = "import asyncio\n" + source.read_text().replace(
        "    def start(self, scope):",
        "    async def start(self, scope):\n        scope.defer(self.ledger[0])"
    )
    if phase == "start":
        code = code.replace("        return handle", "        raise asyncio.CancelledError('publication cancelled')")
    source.write_text(code)
    with patch.object(host, "_replay_optional_contributions", side_effect=asyncio.CancelledError("publication cancelled")):
        with pytest.raises(asyncio.CancelledError, match="publication cancelled"):
            host.attach("publication_test")
    assert attempts == ["cleanup"]
    assert core.context.module_registry.get("publication_test") is None
    assert not host._record("publication_test").attached
    if cleanup_blocked:
        assert host.attach("publication_test")["reason"] == "plugin_cleanup_pending"
        cleanup_blocked = False
        assert host.detach("publication_test")["status"] == RuntimeStatus.OK
        assert attempts == ["cleanup", "cleanup"]
    else:
        assert_rolled_back(host, core)
    assert host.services["ledger"].count(("publication_test", "cleanup", None)) == 1


def test_cancelled_enable_restores_disabled_setting(staged_plugin):
    host, core, root = staged_plugin
    assert host.disable("publication_test")["status"] == RuntimeStatus.OK
    source = root / "publication_test_runtime.py"
    source.write_text("import asyncio\n" + source.read_text().replace(
        "    def start(self, scope):", "    async def start(self, scope):"
    ).replace("        return handle", "        raise asyncio.CancelledError('start cancelled')"))
    with pytest.raises(asyncio.CancelledError, match="start cancelled"):
        host.enable("publication_test")
    assert not host._record("publication_test").enabled
    assert_rolled_back(host, core)


def test_detach_import_cleanup_failure_retains_retry_owner(staged_plugin):
    host, _, _ = staged_plugin
    assert host.attach("publication_test")["status"] == RuntimeStatus.OK
    generation = host.generations["publication_test"]
    with patch.object(host, "_drop_plugin_import_cache", side_effect=RuntimeError("imports blocked")):
        assert host.detach("publication_test")["status"] == RuntimeStatus.ERROR
    assert host.generations.get("publication_test") is generation
    assert host.attach("publication_test")["reason"] == "plugin_cleanup_pending"
    with patch.object(host, "_drop_plugin_import_cache", wraps=host._drop_plugin_import_cache) as cleanup:
        assert host.detach("publication_test")["status"] == RuntimeStatus.OK
        cleanup.assert_called_once()
    assert host.services["ledger"].count(("publication_test", "cleanup", None)) == 1


@pytest.mark.parametrize("phase", ["binding", "replay", "subscriptions", "status"])
def test_late_publication_failure_releases_maps_and_allows_retry(staged_plugin, phase):
    host, core, _ = staged_plugin
    target, name = {
        "binding": (host, "_bind_plugin_module"),
        "replay": (host, "_replay_optional_contributions"),
        "subscriptions": (PluginScope, "publish_core_subscriptions"),
        "status": (host, "_set_state"),
    }[phase]
    original = getattr(target, name)
    captured = []

    def fail_after_publication(*args, **kwargs):
        result = original(*args, **kwargs)
        if phase != "status" or kwargs.get("attached"):
            captured.append(host.generations["publication_test"])
            raise RuntimeError(f"failed {phase}")
        return result

    with patch.object(target, name, side_effect=fail_after_publication, autospec=True):
        assert host.attach("publication_test")["status"] == RuntimeStatus.ERROR
    assert captured
    assert not captured[0].scope.cleanups
    assert captured[0].scope._core_subscriptions
    assert all(subscription.closed for subscription in captured[0].scope._core_subscriptions)
    assert_rolled_back(host, core)
    assert host.attach("publication_test")["status"] == RuntimeStatus.OK
    assert host.generations["publication_test"] is not captured[0]


def test_real_declared_skills_replay_failure_allows_retry(staged_plugin):
    host, core, root = staged_plugin
    source = root / "publication_test_runtime.py"
    source.write_text(source.read_text().replace(
        "class Provider:",
        "class Provider:\n"
        "    calls = 0\n"
        "    def declared_skills(self):\n"
        "        self.calls += 1\n"
        "        if self.calls == 2:\n"
        "            raise RuntimeError('declaration replay failed')\n"
        "        return ()"))
    core.context.port_registry["skill:skill"] = SkillService(repository=ReadOnlySkillRepository())
    assert host.attach("publication_test")["status"] == RuntimeStatus.ERROR
    assert "declaration replay failed" in host._record("publication_test").last_error
    assert_rolled_back(host, core)
    source.write_text(source.read_text().replace("if self.calls == 2:", "if False:"))
    assert host.attach("publication_test")["status"] == RuntimeStatus.OK


def test_failed_rollback_retains_cleanup_owner_until_retry(staged_plugin):
    host, core, _ = staged_plugin
    cleanup_blocked = True
    captured = []

    def cleanup():
        if cleanup_blocked:
            raise RuntimeError("cleanup still pending")

    def fail_replay(_plugin_id):
        generation = host.generations["publication_test"]
        captured.append(generation)
        generation.scope.defer(cleanup)
        raise RuntimeError("replay failed")

    with patch.object(host, "_replay_optional_contributions", side_effect=fail_replay):
        assert host.attach("publication_test")["status"] == RuntimeStatus.ERROR
    generation = captured[0]
    assert host.generations["publication_test"] is generation
    handles = host.first_party_handles or host.third_party_handles
    assert handles["publication_test"] is generation.handle
    assert generation.scope.cleanups
    assert not generation.handle.mounted
    assert core.context.module_registry.get("publication_test") is None
    assert host._record("publication_test").last_load_status == "cleanup_failed"
    assert host.attach("publication_test")["reason"] == "plugin_cleanup_pending"
    assert host.detach("publication_test")["status"] == RuntimeStatus.ERROR
    cleanup_blocked = False
    assert host.detach("publication_test")["status"] == RuntimeStatus.OK
    assert "publication_test" not in host.generations
    assert host.attach("publication_test")["status"] == RuntimeStatus.OK


@pytest.mark.parametrize("phase", ["start", "staged_handle", "register", "provider", "capability"])
def test_early_failure_retains_cleanup_until_detach_succeeds(staged_plugin, phase):
    host, core, root = staged_plugin
    blocked = True
    fail_start = True
    attempts = []

    def cleanup():
        attempts.append("cleanup")
        if blocked:
            raise RuntimeError("early cleanup pending")

    def start_probe():
        if fail_start:
            raise RuntimeError("start failed")

    host.services["ledger"].extend([cleanup, start_probe])
    source = root / "publication_test_runtime.py"
    code = source.read_text().replace(
        "    def start(self, scope):",
        "    def start(self, scope):\n        scope.defer(self.ledger[0])")
    if phase == "start":
        code = code.replace("        scope.defer(self.ledger[0])",
                            "        scope.defer(self.ledger[0])\n        self.ledger[1]()")
    elif phase == "staged_handle":
        code = code.replace("        return handle", "        self.ledger[1]()\n        return handle")
    source.write_text(code)
    target, name = {
        "register": (core.context, "register_module"),
        "provider": (host, "_restore_provider_refs"),
        "capability": (host, "_publish_module_capabilities"),
    }.get(phase, (host, "_publish_module_capabilities"))
    with patch.object(target, name, side_effect=RuntimeError("publication failed")):
        result = host.attach("publication_test")
    assert result["status"] == RuntimeStatus.ERROR
    assert result["cleanup_pending"]
    assert host._record("publication_test").last_load_status == "cleanup_failed"
    assert not host._record("publication_test").attached
    assert core.context.module_registry.get("publication_test") is None
    assert host.attach("publication_test")["reason"] == "plugin_cleanup_pending"
    assert host.enable("publication_test")["status"] == RuntimeStatus.ERROR
    assert host.detach("publication_test")["status"] == RuntimeStatus.ERROR
    assert attempts == ["cleanup", "cleanup"]
    blocked = False
    fail_start = False
    assert host.detach("publication_test")["status"] == RuntimeStatus.OK
    assert attempts == ["cleanup"] * 3
    assert host.detach("publication_test")["status"] == RuntimeStatus.OK
    assert attempts == ["cleanup"] * 3
    assert host.attach("publication_test")["status"] == RuntimeStatus.OK


def test_shutdown_retries_unpublished_scope(staged_plugin):
    host, _, root = staged_plugin
    blocked = True
    attempts = []

    def cleanup():
        attempts.append("cleanup")
        if blocked:
            raise RuntimeError("cleanup pending")

    host.services["ledger"].append(cleanup)
    source = root / "publication_test_runtime.py"
    source.write_text(source.read_text().replace(
        "    def start(self, scope):",
        "    def start(self, scope):\n        scope.defer(self.ledger[0])\n        raise RuntimeError('start failed')"))
    assert host.attach("publication_test")["status"] == RuntimeStatus.ERROR
    blocked = False
    host.shutdown()
    assert attempts == ["cleanup", "cleanup"]
    assert host._record("publication_test").last_load_status == "detached"


@pytest.mark.parametrize("failure", ["close", "restore"])
def test_failed_extension_cleanup_remains_owned_by_host(staged_plugin, monkeypatch, failure):
    from pal.execution import register_with_core
    from tests.test_execution_extension_admission import ProbeExtension

    host, core, root = staged_plugin
    register_with_core(core.context)
    core.publish_module_capabilities("execution")
    slot = core.context.execution_runtime
    original = slot.implementation
    extension = ProbeExtension()
    blocked = True

    def fail_activation():
        raise RuntimeError("activation failed")

    def cleanup():
        if blocked and failure == "close":
            raise RuntimeError("candidate cleanup pending")

    mount = original.mount_subtree

    def restore(*args, **kwargs):
        if blocked and failure == "restore":
            raise RuntimeError("restoration pending")
        return mount(*args, **kwargs)

    monkeypatch.setattr(original, "mount_subtree", restore)

    extension.activate_callback = fail_activation
    extension.close_callback = cleanup
    host.services["ledger"].append(extension)
    source = root / "publication_test_runtime.py"
    source.write_text(source.read_text().replace(
        "        return handle", "        handle.execution_extension = self.ledger[0]\n        return handle"))
    result = host.attach("publication_test")
    assert result["status"] == RuntimeStatus.ERROR
    assert result["cleanup_pending"]
    assert slot.implementation is (original if failure == "close" else extension.candidate)
    assert extension.closed == [extension.candidate] * (2 if failure == "close" else 0)
    restored = slot.registry_generation
    assert host.attach("publication_test")["reason"] == "plugin_cleanup_pending"
    assert host.detach("publication_test")["status"] == RuntimeStatus.ERROR
    assert extension.closed == [extension.candidate] * (3 if failure == "close" else 0)
    blocked = False
    assert host.detach("publication_test")["status"] == RuntimeStatus.OK
    assert extension.closed == [extension.candidate] * (4 if failure == "close" else 1)
    assert slot.implementation is original
    if failure == "close":
        assert slot.registry_generation is restored
    extension.activate_callback = lambda: None
    assert host.attach("publication_test")["status"] == RuntimeStatus.OK
    assert host.detach("publication_test")["status"] == RuntimeStatus.OK


def test_pending_rollback_blocks_dependents_and_disable_until_reload_cleans(staged_plugin):
    host, core, root = staged_plugin
    _write_plugin(root, "publication_dependent", requires=("publication_test",))
    host.rescan()
    blocked = True
    attempts = []

    def cleanup():
        attempts.append("cleanup")
        if blocked:
            raise RuntimeError("early cleanup pending")

    host.services["ledger"].append(cleanup)
    source = root / "publication_test_runtime.py"
    source.write_text(source.read_text().replace(
        "    def start(self, scope):",
        "    def start(self, scope):\n        scope.defer(self.ledger[0])"))
    with patch.object(host, "_publish_module_capabilities", side_effect=RuntimeError("publication failed")):
        assert host.attach("publication_test")["status"] == RuntimeStatus.ERROR
    assert host.attach("publication_dependent")["status"] == RuntimeStatus.ERROR
    assert core.context.module_registry.get("publication_dependent") is None
    assert host.disable("publication_test")["status"] == RuntimeStatus.ERROR
    assert host._record("publication_test").enabled
    assert attempts == ["cleanup"] * 2
    blocked = False
    assert host.reattach("publication_test")["status"] == RuntimeStatus.OK
    assert attempts == ["cleanup"] * 3
    assert host.attach("publication_dependent")["status"] == RuntimeStatus.OK
    assert host.detach("publication_test")["status"] == RuntimeStatus.OK
    assert core.context.module_registry.get("publication_dependent") is None


def test_pending_plugin_task_keeps_its_resource_alive_until_joined():
    async def scenario():
        scope = PluginScope(PalCore().context, "task-owner")
        events = []
        ready, finishing, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        scope.defer(lambda: events.append("resource_closed"))
        async def work():
            ready.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                finishing.set()
                await release.wait()
                events.append("task_finished")
        task = scope.track_task(asyncio.create_task(work()))
        await ready.wait()
        assert scope.close()
        await finishing.wait()
        assert not events
        assert scope.close()
        assert not events
        release.set()
        await task
        assert not scope.close()
        assert events == ["task_finished", "resource_closed"]
        assert not scope.cleanups
    asyncio.run(scenario())


def test_shutdown_retains_dependencies_when_dependent_cleanup_fails(staged_plugin):
    host, core, root = staged_plugin
    _write_plugin(root, "shutdown_child", requires=("publication_test",))
    host.rescan()
    assert host.enable("shutdown_child")["status"] == RuntimeStatus.OK
    blocked = True
    def cleanup():
        if blocked:
            raise RuntimeError("child still running")
    host.generations["shutdown_child"].scope.defer(cleanup)
    parent = host.generations["publication_test"]
    host.shutdown()
    assert host.shutdown_errors
    assert host.generations["publication_test"] is parent
    assert not parent.cleanup_started
    assert "shutdown_child" in host.generations
    blocked = False
    host.shutdown()
    assert not host.shutdown_errors
    assert not host.generations


@pytest.mark.parametrize("fail_first", [False, True])
def test_scope_retains_scheduled_cleanup_and_its_dependencies(fail_first):
    async def scenario():
        core = PalCore()
        scope = PluginScope(core.context, "scheduled_cleanup")
        release = asyncio.Event()
        tasks = []
        events = []
        async def cleanup():
            await release.wait()
            if fail_first and len(tasks) == 1:
                raise RuntimeError("retryable cleanup failure")
            events.append("task closed")
        def schedule():
            task = asyncio.create_task(cleanup())
            tasks.append(task)
            return task
        scope.defer(lambda: events.append("dependency closed"))
        scope.defer(schedule)
        assert scope.close()
        assert scope.close()
        assert len(tasks) == 1 and not events
        release.set()
        await asyncio.gather(tasks[0], return_exceptions=True)
        if fail_first:
            assert "retryable cleanup failure" in scope.close()[0]
            assert not events
            assert scope.close()
            assert len(tasks) == 2
            await tasks[1]
        assert scope.close() == []
        assert scope.close() == []
        assert events == ["task closed", "dependency closed"]
        assert not scope.cleanups
    asyncio.run(scenario())
