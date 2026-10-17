"""Recovery surfaces follow current aliases after tool consolidation."""
from types import SimpleNamespace

import pytest

from pal.core import PalCore
from pal.execution import register_with_core as register_execution
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput, ToolGuidance
from pal.memory import MemoryService, register_with_core as register_memory
from pal.plugins.capabilities import register_with_core as register_plugins
from pal.shared.tool_protocol import new_tool_call

from tests.capability_fixture import mount_test_capability


@pytest.fixture
def core():
    core = PalCore()
    register_execution(core.context)
    core.publish_module_capabilities('execution')
    try:
        yield core
    finally:
        core.close()


def test_execution_recovery_uses_domain_discovery_instead_of_retired_inventory(core):
    descriptors = core.tool_surface.select_failure_descriptors(SimpleNamespace(subsystem='execution'))
    assert {item.name for item in descriptors} == {'read_tool', 'search_tools', 'inspect_execution_state'}
    contracts = core.tool_surface.build_tool_contracts_from_descriptors(descriptors)
    assert {item['function']['name'] for item in contracts} == {'read_tool', 'search_tools'}
    runtime = core.context.execution_runtime
    domains = runtime.execute_tool(new_tool_call(name='search_tools', args={'directory_offset': 0}))
    assert domains.ok and 'file' in domains.structured['available_domains']
    directory = runtime.execute_tool(new_tool_call(name='search_tools', args={'domain': 'file'}))
    assert directory.ok and directory.structured['directory']['tools']


def test_memory_recovery_keeps_record_inspection_without_backend_maintenance(core):
    register_memory(core.context, MemoryService())
    core.publish_module_capabilities('memory')
    descriptors = core.tool_surface.select_failure_descriptors(SimpleNamespace(subsystem='memory'))
    assert {item.name for item in descriptors} == {'read_tool', 'inspect_memory_state'}


def test_plugin_recovery_uses_consolidated_management_and_inspection(core):
    register_plugins(core.context, SimpleNamespace())
    core.publish_module_capabilities('plugins')
    descriptors = core.tool_surface.select_failure_descriptors(SimpleNamespace(subsystem='plugin'))
    assert {item.name for item in descriptors} == {
        'read_tool', 'inspect_plugin', 'list_plugins', 'rescan_plugins', 'manage_plugin'}


def test_recovery_resolves_the_current_alias_instead_of_an_old_canonical_path(core):
    runtime = core.context.execution_runtime
    mount_test_capability(runtime, alias='inspect_browser_status', canonical_path='intro_test_browser_health',
        InputModel=EmptyToolInput, OutputModel=EmptyToolOutput, handler=lambda _: {},
        guidance=ToolGuidance(purpose='Inspect browser health', use_when='Browser diagnosis',
                              do_not_use_when='Other tasks'))
    descriptors = core.tool_surface.select_failure_descriptors(SimpleNamespace(subsystem='web_fetch'))
    assert {item.name for item in descriptors} == {'read_tool', 'inspect_browser_status'}
    browser = next(item for item in descriptors if item.name == 'inspect_browser_status')
    assert browser.canonical_path == 'intro_test_browser_health'
