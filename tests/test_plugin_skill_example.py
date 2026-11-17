from types import SimpleNamespace
import asyncio
from unittest.mock import AsyncMock

from pal.core.runtime import PalCore as _PalCoreBootstrap
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


def test_documented_plugin_registration_and_command_reload(tmp_path, monkeypatch):
    from tests.core_factory import build_core
    from pal.control import ControlPlane, register_with_core as register_control
    from pal.control.contracts import ControlEvent, ControlRoute
    from pal.core.capabilities import register_with_core as register_core
    from pal.execution import register_with_core as register_execution
    from pal.foundation.persistence import PalV2Database
    from pal.plugins.host import PluginHost
    from pal.plugins.models import PluginBundleModel
    from pal.shared import EventKind

    def example(label, language):
        return PAL_PLUGIN_DEVELOPMENT_MANUAL.split(label, 1)[1].split(
            f'```{language}\n', 1)[1].split('\n```', 1)[0]

    root = tmp_path / 'plugins/community/demo_tools'
    root.mkdir(parents=True)
    manifest = example('Minimal `plugin.toml`:', 'toml').replace(
        'requires_ports = []', 'requires_ports = ["core:control", "control:control"]')
    (root / 'plugin.toml').write_text(manifest)
    (root / 'capabilities.py').write_text(example('Example `capabilities.py`:', 'python'))
    source = root / 'runtime.py'
    runtime_code = example('Example `runtime.py`:', 'python')
    source.write_text(runtime_code)
    database = PalV2Database(tmp_path / 'pal.sqlite3')
    database.initialize([PluginBundleModel])
    core = build_core()
    register_core(core)
    register_execution(core.context)
    control = ControlPlane()
    register_control(core.context, control)
    host = PluginHost(core.context, tmp_path)
    delivered = AsyncMock(return_value=True)
    monkeypatch.setattr(core, '_deliver_control_delivery_async', delivered)
    try:
        host.bootstrap()
        assert 'demo_tools' in host.generations, host._record('demo_tools').last_error
        result = core.context.execution_runtime.invoke_indirect_tool(
            new_tool_call(name='demo_echo', args={'message': 'hello'}))
        assert isinstance(result, CompleteResult), result
        assert result.output == {'message': 'hello'}

        command_code = example('### Local slash commands and replies', 'python')
        source.write_text(runtime_code.replace('return handle',
            'return build_command_handle(scope, provider)') + '\n' + command_code)
        assert host.reattach('demo_tools')['status'] == 'ok'
        route = ControlRoute('socket', 'socket', {'request_id': 'demo'}, 'socket:demo')
        action = control.parse_event(ControlEvent(
            EventKind.SLASH_COMMAND, 'channel', {'text': '/demo_ping'}, route=route))
        assert action is not None
        asyncio.run(core.handle_control_action_async(action))
        delivery = delivered.await_args.args[0]
        assert delivery.text == 'Demo plugin is ready.'
        assert delivery.route == route
        assert not core.state.active_turns

        assert host.detach('demo_tools')['status'] == 'ok'
        assert control.registry.resolve('demo_ping') is None
        assert core.context.control_action_registry.resolve('demo_tools_ping') is None
        assert core.context.module_registry.get('demo_tools') is None
    finally:
        host.shutdown()
        core.context.execution_runtime.shutdown()
        database.close()
