import pytest

from pal.core import PalCore
from pal.execution import register_with_core
from pal.execution.tool_facade import EmptyToolInput, EmptyToolOutput
from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
from pal.bunshin.prompt_adapter import BunshinPromptFragmentProvider
from pal.shared import PromptAssemblyContext
from tests.capability_fixture import mount_test_capability


@pytest.mark.parametrize('name,authored', [
    ('op_bunshin_memory_candidate_write', False),
    ('op_bunshin_artifact_write', False),
    ('op_bunshin_artifact_edit', True),
])
def test_workflow_descriptions_only_publish_authored_examples(name, authored):
    from pal.bunshin.scoped_execution import _WORKSPACE_TOOL_SPECS, _workflow_capability
    from pal.execution.tool_registry import _compile_record

    descriptor, binding = _workflow_capability(
        name=name, spec=_WORKSPACE_TOOL_SPECS[name], handler=lambda *_: {})
    record = _compile_record(descriptor, binding)
    assert ('Valid example:' in record.compiled_description) is authored
    if authored:
        assert 'report.md' in record.compiled_description


@pytest.mark.parametrize('indirect', [False, True])
def test_worker_routes_only_through_its_actual_surface(indirect):
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    runtime = core.context.execution_runtime
    mount_test_capability(runtime, alias='fixture_session', canonical_path='op_fixture_session',
                          InputModel=EmptyToolInput, OutputModel=EmptyToolOutput,
                          handler=lambda _: {}, metadata={'preserve_role_invocation_mode': indirect})
    # The native backend adds read/call but not search for its retained indirect tools.
    allowed = ['op_fixture_session', 'op_tool_read', 'op_tool_call']
    if not indirect:
        allowed.append('op_tool_search')
    scoped = BunshinScopedExecutionRuntime(runtime, allowed)
    try:
        names = [spec['function']['name'] for spec in scoped.build_llm_tool_contracts()]
        assert ('fixture_session' in names) is (not indirect)
        assert ('call_tool' in names) is indirect
        assert ('read_tool' in names) is indirect
        assert 'search_tools' not in names
        provider = BunshinPromptFragmentProvider(
            scaffold_factory=lambda: {'visible_capabilities': names}, role_context_factory=lambda: '')
        fragments = provider.build_prompt_fragments(PromptAssemblyContext(core_mode='bunshin'))
        routing = next(f.content for f in fragments if f.section == 'tool_routing')
        assert ('call_tool' in routing) is indirect
        assert ('read_tool' in routing) is indirect
        assert 'search_tools' not in routing
    finally:
        scoped.base_runtime.runtime.shutdown()
        runtime.shutdown()
