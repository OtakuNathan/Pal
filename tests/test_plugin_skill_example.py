from types import SimpleNamespace

from pal.execution.capability_compiler import compile_provider_subtree
from pal.execution.runtime import ExecutionRuntime
from pal.shared.tool_protocol import CompleteResult, new_tool_call
from pal.skill.builtin_skills import PAL_PLUGIN_DEVELOPMENT_MANUAL


def test_documented_plugin_provider_can_be_loaded_and_called():
    code = PAL_PLUGIN_DEVELOPMENT_MANUAL.split('Example `capabilities.py`:\n\n```python\n', 1)[1].split('\n```', 1)[0]
    namespace = {}
    exec(compile(code, '<documented-plugin-provider>', 'exec'), namespace)
    subtree = compile_provider_subtree(namespace['DemoProvider'](), module_id='demo_tools',
                                       lifecycle_scope='runtime', detachable=True)
    runtime = ExecutionRuntime()
    try:
        runtime.mount_subtree(SimpleNamespace(mounted_subtree=subtree))
        result = runtime.invoke_indirect_tool(new_tool_call(name='demo_echo', args={'message': 'hello'}))
        assert isinstance(result, CompleteResult), result
        assert result.output == {'message': 'hello'}
        assert result.llm_text == 'Echo: hello'
    finally:
        runtime.shutdown()
