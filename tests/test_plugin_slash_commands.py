from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from pal.control import ControlPlane, ControlCommandSpec, register_with_core as register_control
from pal.control.catalog import ControlCatalogSource
from pal.control.interactions import build_control_catalog_payload
from pal.core import PalCore
from pal.execution import register_with_core as register_execution
from pal.plugins import PluginHost
from pal.plugins.models import PluginBundleModel
from pal.foundation.persistence import PalV2Database


def command(name, *, aliases=()):
    return ControlCommandSpec(name=name, aliases=aliases, description="Plugin command", handler=lambda invocation: None, show_in_panel=True)


def test_command_batch_is_atomic_and_old_lease_cannot_remove_replacement():
    control = ControlPlane()
    original = control.registry.resolve("status")
    with pytest.raises(ValueError):
        control.install_commands("demo", (command("first"), command("second", aliases=("status",))))
    assert control.registry.resolve("first") is None
    assert control.registry.resolve("status") is original
    lease = control.install_commands("demo", (command("hello", aliases=("hi",)),))
    lease.close()
    replacement = control.install_commands("demo", (command("hello", aliases=("hi",)),))
    lease.close()
    assert control.registry.resolve("hi") is not None
    replacement.close()
    assert control.registry.resolve("hi") is None


@pytest.mark.parametrize("spec", [command("UpperCase"), command("a" * 33), command("twice", aliases=("twice",)),
                                  ControlCommandSpec("missing_description", lambda invocation: None, show_in_panel=True)])
def test_invalid_declarations_never_publish(spec):
    control = ControlPlane()
    before = build_control_catalog_payload(control)
    with pytest.raises(ValueError):
        control.install_commands("demo", (spec,))
    assert build_control_catalog_payload(control) == before


@pytest.fixture
def runtime(tmp_path):
    database = PalV2Database(tmp_path / "pal.sqlite3")
    database.initialize([PluginBundleModel])
    core = PalCore()
    register_execution(core.context)
    control = ControlPlane()
    register_control(core.context, control)
    root = tmp_path / "plugins/community/slash_demo"
    root.mkdir(parents=True)
    (root / "plugin.toml").write_text('''plugin_id = "slash_demo"
entrypoint = "slash_demo_plugin"
version = "1"
enabled_by_default = true
lifecycle_protocol = "raii.v1"
requires_ports = ["control:control"]
''')
    source = root / "slash_demo_plugin.py"
    source.write_text('''from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
from pal.control import ControlAction, ControlCommandSpec
class Plugin:
    def start(self, scope):
        def command(invocation):
            return ControlAction(action_kind="demo_action", target_scope="slash_demo", route=invocation.route, args={"version": 1})
        return ModuleHandle(module_id="slash_demo", tier=MODULE_TIER_DETACHABLE, detachable=True,
            control_commands=(ControlCommandSpec(name="demo_hello", aliases=("demo_hi",), handler=command,
                description="Demo greeting", show_in_panel=True),))
def build_plugin(context): return Plugin()
''')
    host = PluginHost(core.context, tmp_path)
    host.bootstrap()
    assert host._record("slash_demo").attached, host._record("slash_demo").last_error
    try:
        yield core, control, host, source
    finally:
        host.shutdown()
        core.context.execution_runtime.shutdown()
        database.close()


def test_plugin_commands_follow_generation_and_catalog_lifecycle(runtime):
    core, control, host, source = runtime
    old = host.generations["slash_demo"].handle.control_command_lease
    assert {item["command"] for item in build_control_catalog_payload(control)["commands"]} >= {"demo_hello"}
    first = control.registry.resolve("demo_hi")
    assert first is not None
    source.write_text(source.read_text().replace('"version": 1', '"version": 2'))
    assert host.reattach("slash_demo")["status"] == "ok"
    second = control.registry.resolve("demo_hi")
    assert second is not None and second is not first
    old.close()
    assert control.registry.resolve("demo_hello") is second
    assert host.detach("slash_demo")["status"] == "ok"
    assert control.registry.resolve("demo_hi") is None
    assert control.registry.resolve("demo_hello") is None


def test_failure_after_registration_rolls_back_command_surface(runtime, monkeypatch):
    _, control, host, _ = runtime
    assert host.detach("slash_demo")["status"] == "ok"
    def fail(*args):
        assert control.registry.resolve("demo_hello") is not None
        raise RuntimeError("publication failure after command contribution")
    monkeypatch.setattr(host, "_bind_plugin_module", fail)
    assert host.attach("slash_demo")["status"] == "error"
    assert control.registry.resolve("demo_hello") is None
    assert control.registry.resolve("demo_hi") is None


def test_catalog_changes_wake_core_and_coalesce_without_model_calls(runtime):
    core, control, _, _ = runtime
    source = ControlCatalogSource(control)
    core.publish_control_catalog_async = AsyncMock()
    async def run():
        core.bind_async_wakeup_sources()
        first = control.install_commands("extra", (command("extra_one"),))
        second = control.install_commands("extra", (command("extra_two"),))
        assert source.prepare(core.context)
        events = source.drain(core.context)
        assert len(events) == 1 and not source.prepare(core.context)
        await core.handle_control_action_async(events[0].payload)
        core.publish_control_catalog_async.assert_awaited_once()
        first.close()
        second.close()
        assert source.prepare(core.context)
    asyncio.run(run())
