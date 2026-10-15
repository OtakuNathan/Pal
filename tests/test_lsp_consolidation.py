from unittest.mock import Mock

import pytest

from pal.core import PalCore
from pal.execution import register_with_core
from pal.lsp import build_lsp_plugin
from pal.shared.tool_protocol import new_tool_call


@pytest.fixture
def lsp_runtime(tmp_path):
    core = PalCore()
    register_with_core(core.context)
    handle = build_lsp_plugin(runtime_root=tmp_path).register_with_core(core.context)
    core.publish_module_capabilities("execution")
    core.publish_module_capabilities("lsp")
    provider = handle.ports["lsp"]
    provider._ensure_manager_started = Mock()
    provider.client.operation_sync = Mock(return_value={"status": "ok", "result": []})
    yield core.context.execution_runtime, provider
    core.close()


def invoke(runtime, name, args):
    return runtime.execute_tool(new_tool_call(name="call_tool", args={"name": name, "args": args}), turn_id="lsp-test")


def test_lsp_surface_has_ten_indirect_tools_and_no_old_aliases(lsp_runtime):
    runtime, _ = lsp_runtime
    generation = runtime.registry_generation
    names = {name for name, record in generation.search_records.items() if record["module_id"] == "lsp"}
    assert names == {
        "inspect_lsp_status", "prepare_lsp_workspace", "read_lsp_diagnostics",
        "read_lsp_hover", "find_lsp_symbol_locations", "find_lsp_references",
        "find_lsp_calls", "list_lsp_document_symbols", "search_lsp_workspace_symbols",
        "rescan_lsp_servers",
    }
    assert not names.intersection(generation.direct_aliases)
    for name in ("inspect_lsp_provider", "diagnose_lsp_server", "find_lsp_definitions",
                 "find_lsp_implementations", "prepare_lsp_call_hierarchy",
                 "find_lsp_incoming_calls", "find_lsp_outgoing_calls"):
        assert generation.record_for_alias(name) is None


@pytest.mark.parametrize("alias,selector,method", [
    ("find_lsp_symbol_locations", {"kind": "definition"}, "definition"),
    ("find_lsp_symbol_locations", {"kind": "implementation"}, "implementation"),
    ("find_lsp_calls", {"direction": "callers"}, "incoming_calls"),
    ("find_lsp_calls", {"direction": "callees"}, "outgoing_calls"),
])
def test_query_enum_routes_exact_operation_and_position(lsp_runtime, alias, selector, method):
    runtime, provider = lsp_runtime
    result = invoke(runtime, alias, {**selector, "file": "/repo/a.py", "line": 3, "character": 7,
                                    "workspace_root": "/repo", "name": "python"})
    assert result.ok
    operation, args = provider.client.operation_sync.call_args.args
    assert operation == method
    assert args == {"file": "/repo/a.py", "line": 3, "character": 7,
                    "workspace_root": "/repo", "server_id": "python"}


@pytest.mark.parametrize("alias,selector", [
    ("find_lsp_calls", {}),
    ("find_lsp_calls", {"direction": "both"}),
    ("find_lsp_symbol_locations", {"kind": "references"}),
])
def test_invalid_query_modes_do_not_reach_server(lsp_runtime, alias, selector):
    runtime, provider = lsp_runtime
    assert not invoke(runtime, alias, {**selector, "file": "/repo/a.py", "line": 0, "character": 0}).ok
    provider.client.operation_sync.assert_not_called()


def test_hub_and_workspace_inspection_remain_passive(lsp_runtime):
    runtime, provider = lsp_runtime
    provider.last_health = {"server_count": 2, "result": "old-result"}
    hub = invoke(runtime, "inspect_lsp_status", {})
    assert hub.ok
    assert hub.structured["cached_snapshot"] == {"server_count": 2}
    provider.client.operation_sync.assert_not_called()
    workspace = invoke(runtime, "inspect_lsp_status", {"workspace_root": "/repo"})
    assert workspace.ok
    provider._ensure_manager_started.assert_not_called()
    provider.client.operation_sync.assert_called_once_with("status", {"workspace_root": "/repo"})


def test_named_server_inspection_checks_only_selected_server(lsp_runtime):
    runtime, provider = lsp_runtime
    assert invoke(runtime, "inspect_lsp_status", {"workspace_root": "/repo", "name": "clangd"}).ok
    provider._ensure_manager_started.assert_called_once()
    provider.client.operation_sync.assert_called_once_with("doctor", {"workspace_root": "/repo", "server_id": "clangd"})
