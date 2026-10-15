from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pal.core import PalCore
from pal.execution import register_with_core
from pal.plugins.capabilities import register_with_core as register_plugins
from pal.shared.tool_protocol import new_tool_call


@pytest.fixture
def plugin_runtime():
    core = PalCore()
    register_with_core(core.context)
    host = SimpleNamespace(**{name: Mock(return_value={"status": "ok", "plugin_id": "demo"})
                             for name in ("attach", "detach", "reattach", "enable", "disable")})
    host.show_summary = Mock(return_value={"attached_count": 1})
    host.inspect_plugin = Mock(return_value={"name": "demo", "cleanup_pending": True})
    host.list_plugin_summaries = Mock(return_value=[{"name": "demo", "plugin_id": "demo"}])
    host.rescan = Mock(return_value={"scan_errors": []})
    host.rescan_and_attach_new_first_party = Mock(return_value={"scan_errors": [], "attach_errors": {}})
    register_plugins(core.context, host)
    core.publish_module_capabilities("execution")
    core.publish_module_capabilities("plugins")
    yield core.context.execution_runtime, host
    core.close()


def invoke(runtime, name, args):
    return runtime.execute_tool(new_tool_call(name="call_tool", args={"name": name, "args": args}), turn_id="plugin-test")


def test_plugin_surface_has_eight_indirect_tools(plugin_runtime):
    runtime, _ = plugin_runtime
    generation = runtime.registry_generation
    names = {name for name, record in generation.search_records.items() if record["module_id"] == "plugins"}
    assert names == {"manage_plugin", "inspect_plugin", "list_plugins", "rescan_plugins",
                     "install_package", "prepare_package", "uninstall_plugin", "inspect_package_status"}
    assert not names.intersection(generation.direct_aliases)
    for name in ("attach_plugin", "reload_plugin", "detach_plugin", "enable_plugin", "disable_plugin",
                 "inspect_plugin_host", "rescan_and_attach_first_party_plugins"):
        assert generation.record_for_alias(name) is None


@pytest.mark.parametrize("operation,method", [("attach", "attach"), ("detach", "detach"),
    ("reload", "reattach"), ("enable", "enable"), ("disable", "disable")])
def test_management_routes_only_selected_lifecycle_action(plugin_runtime, operation, method):
    runtime, host = plugin_runtime
    assert invoke(runtime, "manage_plugin", {"name": "demo", "operation": operation}).ok
    getattr(host, method).assert_called_once_with("demo")
    for other in {"attach", "detach", "reattach", "enable", "disable"} - {method}:
        getattr(host, other).assert_not_called()


def test_unknown_operation_is_rejected_without_lifecycle_calls(plugin_runtime):
    runtime, host = plugin_runtime
    assert not invoke(runtime, "manage_plugin", {"name": "demo", "operation": "restart"}).ok
    for method in ("attach", "detach", "reattach", "enable", "disable"):
        getattr(host, method).assert_not_called()


def test_inspection_keeps_hub_and_named_object_separate(plugin_runtime):
    runtime, host = plugin_runtime
    hub = invoke(runtime, "inspect_plugin", {})
    assert hub.ok and hub.structured["attached_count"] == 1
    host.inspect_plugin.assert_not_called()
    detail = invoke(runtime, "inspect_plugin", {"name": "demo"})
    assert detail.ok and detail.structured["cleanup_pending"] is True
    host.inspect_plugin.assert_called_once_with("demo")
    host.show_summary.assert_called_once()


@pytest.mark.parametrize("args,method", [({}, "rescan"),
    ({"mode": "scan_and_attach_new_first_party"}, "rescan_and_attach_new_first_party")])
def test_scan_mode_does_not_implicitly_attach(plugin_runtime, args, method):
    runtime, host = plugin_runtime
    assert invoke(runtime, "rescan_plugins", args).ok
    getattr(host, method).assert_called_once_with()
    other = "rescan_and_attach_new_first_party" if method == "rescan" else "rescan"
    getattr(host, other).assert_not_called()
