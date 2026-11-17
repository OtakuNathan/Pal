from __future__ import annotations
from tests.core_factory import build_core
from tests.control_factory import build_control_plane
from pal.core.capabilities import register_with_core as register_core

import asyncio
from unittest.mock import AsyncMock

import pytest

from pal.control import ControlPlane, ControlCommandSpec, register_with_core as register_control
from pal.control.contracts import ControlAction, ControlEvent, ControlRoute, InteractionResult
from pal.control.catalog import ControlCatalogSource
from pal.control.interactions import build_control_catalog_payload
from pal.core.runtime import PalCore
from pal.execution import register_with_core as register_execution
from pal.plugins.host import PluginHost
from pal.plugins.models import PluginBundleModel
from pal.foundation.persistence import PalV2Database
from pal.shared import EventKind


def command(name, *, aliases=()):
    return ControlCommandSpec(name=name, aliases=aliases, description="Plugin command", handler=lambda invocation: None, show_in_panel=True)


def test_command_batch_is_atomic_and_old_lease_cannot_remove_replacement():
    control = build_control_plane()
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
    core = build_core()
    register_core(core)
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
from pal.control import ControlAction, ControlCommandSpec, ControlInteractionSpec
from pal.control.runtime_contracts import CORE_CONTROL
from pal.core.control_action_registry import ControlActionSpec
class Plugin:
    def start(self, scope):
        controls = scope.context.require_port(CORE_CONTROL)
        def command(invocation):
            return ControlAction(action_kind="demo_action", target_scope="slash_demo", route=invocation.route, args={"version": 1})
        def interaction(result):
            return ControlAction(action_kind="demo_action", target_scope="slash_demo", route=result.route, args={"version": 1})
        async def execute(action):
            await controls.reply(action, f"Demo version {action.args['version']}")
        return ModuleHandle(module_id="slash_demo", tier=MODULE_TIER_DETACHABLE, detachable=True,
            control_action_handlers={"demo_action": ControlActionSpec(execute)},
            control_interactions=(ControlInteractionSpec("demo.greet", interaction),),
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
    source.write_text(source.read_text().replace('"version": 1', '"version": 22'))
    assert host.reattach("slash_demo")["status"] == "ok"
    second = control.registry.resolve("demo_hi")
    assert second is not None and second is not first
    old.close()
    assert control.registry.resolve("demo_hello") is second
    assert host.detach("slash_demo")["status"] == "ok"
    assert control.registry.resolve("demo_hi") is None
    assert control.registry.resolve("demo_hello") is None
    assert "demo.greet" not in control.interaction_handlers
    assert core.context.control_action_registry.resolve("demo_action") is None


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


def test_module_command_executes_locally_and_delivers_through_core(runtime, monkeypatch):
    core, control, host, source = runtime
    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr(core, "_deliver_control_delivery_async", delivered)
    route = ControlRoute("socket", "socket", {"request_id": "local"}, "socket:local")
    action = control.parse_event(ControlEvent(
        EventKind.SLASH_COMMAND, "channel", {"text": "/demo_hi"}, route=route))
    assert action is not None
    before = dict(core.state.active_turns)
    asyncio.run(core.handle_control_action_async(action))
    assert delivered.await_args.args[0].text == "Demo version 1"
    assert delivered.await_args.args[0].route == route
    assert core.state.active_turns == before
    assert core.state.memory_control_reservations == 0
    assert core.context.port_registry.get("llm:llm") is None
    assert core.context.port_registry.get("memory:memory") is None


def test_withdrawn_generation_cannot_remove_replacement_action(runtime, monkeypatch):
    core, control, host, source = runtime
    old = host.generations["slash_demo"].handle
    source.write_text(source.read_text().replace('"version": 1', '"version": 22'))
    assert host.reattach("slash_demo")["status"] == "ok"
    replacement = host.generations["slash_demo"].handle
    assert replacement is not old
    core.context.withdraw_module_control(old)
    assert control.registry.resolve("demo_hello") is not None
    assert core.context.control_action_registry.resolve("demo_action") is replacement.control_action_handlers["demo_action"]
    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr(core, "_deliver_control_delivery_async", delivered)
    result = InteractionResult("greeting", "demo", "demo.greet", route=ControlRoute("test", "socket"))
    action = control.handle_interaction(result)
    assert action is not None and action.action_kind == "demo_action"
    asyncio.run(core.handle_control_action_async(action))
    assert delivered.await_args.args[0].text == "Demo version 22"


def test_optional_cache_reminder_plugin_reload_and_detach(runtime):
    from pathlib import Path
    import pal
    from tests.cache_reminder_factory import _Settings
    from tests.llm_fakes import scripted_llm
    from tests.test_cache_warm_deadline import _snapshot

    core, control, host, _ = runtime
    core.context.port_registry["llm:llm"] = scripted_llm(prompt_cache_reminder_snapshot=_snapshot)
    core.context.port_registry["llm:settings"] = _Settings()
    directory = host.builtin_root / "cache_reminder"
    directory.mkdir(parents=True)
    manifest = Path(pal.__file__).parent / "plugins_builtin/cache_reminder/plugin.toml"
    (directory / "plugin.toml").write_text(manifest.read_text())
    host.rescan()
    loaded = host.attach("cache_reminder")
    assert loaded["status"] == "ok", loaded
    old = host.generations["cache_reminder"].handle
    manager = core.context.port_registry["cache_reminder:reminder"]
    manager.schedule_after_turn_commit(route=ControlRoute("test", "socket"), turn_id="one")
    assert manager.inspect()["timer_scheduled"]
    replaced = host.reattach("cache_reminder")
    assert replaced["status"] == "ok", replaced
    successor = host.generations["cache_reminder"].handle
    core.context.withdraw_module_control(old)
    assert core.context.configuration_operations["cache_reminder.inspect"][1] is successor.configuration_handlers["cache_reminder.inspect"]
    assert not manager.inspect()["timer_scheduled"]
    assert core.context.port_registry["cache_reminder:reminder"] is not manager
    assert host.detach("cache_reminder")["status"] == "ok"
    assert core.context.port_registry.get("cache_reminder:reminder") is None
    assert core.context.control_action_registry.resolve("cache_warm_deadline_compact") is None
    assert "cache_reminder.inspect" not in core.context.configuration_operations
