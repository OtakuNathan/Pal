"""CLI-only configuration aliases cannot leak through another model surface."""
import json

from pal.core import PalCore
from pal.core.capabilities import register_with_core as register_core
from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
from pal.execution import register_with_core as register_execution
from pal.bunshin.workflow_capabilities import BunshinPublicProvider
from pal.web_search import register_with_core as register_search
from pal.web_search.service import WebSearchService
from pal.shared.tool_protocol import new_tool_call

from tests.test_configuration_cli import runtime_root


REMOVED = {
    'configure_web_search_provider', 'set_web_search_provider_auth_material',
    'enable_web_search_provider', 'disable_web_search_provider', 'set_active_web_search_provider',
    'configure_core_cache_warm_deadline', 'configure_core', 'inspect_core_cache_warm_deadline',
    'inspect_web_search_provider_health', 'inspect_web_search_provider_auth', 'inspect_active_web_search_provider', 'set_bunshin_profile_override', 'reset_bunshin_profile_override',
    'set_bunshin_family_override', 'reset_bunshin_family_override', 'refresh_bunshin_catalog',
}


def test_configuration_writes_are_absent_and_live_observations_remain(runtime_root):
    from pal.web_search.repository import WebSearchProviderRepository
    from pal.llm.repository import RuntimeSettingRepository
    core = PalCore()
    runtime = core.context.execution_runtime
    try:
        register_execution(core.context)
        register_core(core)
        register_search(core.context, WebSearchService(WebSearchProviderRepository(), RuntimeSettingRepository()))
        core.context.register_module(ModuleHandle(module_id='bunshin', tier=MODULE_TIER_DETACHABLE,
            introspection_provider=BunshinPublicProvider(runtime_root, context=core.context)))
        for module in ('execution', 'core', 'web_search', 'bunshin'):
            core.publish_module_capabilities(module)
        generation = runtime.registry_generation
        expected = {'search_web', 'inspect_web_search_provider', 'inspect_web_search_state',
                    'list_web_search_providers', 'observe_core', 'read_bunshin_catalog', 'start_bunshin_workflow'}
        assert expected <= set(generation.search_records)
        listing = runtime.invoke_indirect_tool(new_tool_call(name='list_tools', args={}))
        assert listing.kind == 'complete'
        for alias in REMOVED:
            assert generation.record_for_alias(alias) is None
            assert alias not in json.dumps(listing.output)
            searched = runtime._search_generation(generation, {'query': alias})
            assert alias not in [h['alias'] for h in searched['hits'] + searched.get('suggestions', [])]
            contract = runtime.invoke_direct_tool(new_tool_call(name='read_tool', args={'name': alias}))
            assert contract.kind != 'complete'
            invocation = runtime.invoke_indirect_tool(new_tool_call(name=alias, args={}))
            assert invocation.kind == 'rejected' and invocation.error_code == 'unknown_tool'
        for record in generation.search_records.values():
            prose = ' '.join(str(record.get(k, '')) for k in ('purpose', 'use_when', 'do_not_use_when', 'failure_next_steps'))
            assert not any(alias in prose for alias in REMOVED)
        health = runtime.invoke_indirect_tool(new_tool_call(name='inspect_web_search_provider', args={'name': 'brave'}))
        assert health.kind == 'complete' and health.output['health']['healthy'] is False
    finally:
        runtime.shutdown()
