import json

from pal.core.runtime import PalCore
from pal.execution.capabilities import register_with_core
from pal.execution.tool_presentation import render_tool_search
from pal.lsp import build_lsp_plugin
from pal.shared.tool_protocol import new_tool_call


def test_search_projection_preserves_hits_filters_and_next_tool_routes(tmp_path):
    core = PalCore()
    register_with_core(core.context)
    build_lsp_plugin(runtime_root=tmp_path).register_with_core(core.context)
    for module in ('execution', 'lsp'):
        core.publish_module_capabilities(module)
    runtime = core.context.execution_runtime
    try:
        result = runtime.execute_tool(new_tool_call(name='search_tools',args={'query':'lsp','top_k':20,'facets':True}))
        assert result.ok
        visible = json.loads(result.llm_text)
        full = result.structured
        assert [hit['alias'] for hit in visible['hits']] == [hit['alias'] for hit in full['hits']]
        for field in ('total_count','truncated','applied_filters','facets'):
            assert visible[field] == full[field]
        assert 'returned_count' not in visible and 'top_k' not in visible
        for hit in visible['hits']:
            assert not {'score','search_text','tags','module_id','family','namespace'} & hit.keys()
            assert {'purpose','use_when','input_shape','invocation_mode'} <= hit.keys()
        prepare = next(hit for hit in visible['hits'] if hit['alias']=='lsp_prepare_workspace')
        routes = ' '.join(prepare['next_tools'])
        assert 'read_tool' in routes and 'call_tool' in routes
        assert 'lsp_status' in routes
        assert 'op_' not in routes
        # Rendering never mutates the full external response or index.
        before = json.dumps(full,sort_keys=True)
        render_tool_search(runtime.registry_generation,full)
        assert json.dumps(full,sort_keys=True) == before
    finally:
        runtime.shutdown()
