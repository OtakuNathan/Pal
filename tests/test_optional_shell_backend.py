"""The default backend works with native imports unavailable, even on native CI."""
import os
from pathlib import Path
import subprocess
import sys


def test_builtin_shell_without_native_dependency():
    script = '''
import asyncio, importlib.abc, sys
class NoNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(('_pal_shell_', 'pal_shell_worker', 'pal_shell_remote', 'pal_shell_native')):
            raise ModuleNotFoundError(fullname)
sys.meta_path.insert(0, NoNative())
from pal.core import PalCore
from pal.core.main_context import MainContext
from pal.execution import register_with_core
from pal.execution.backend import build_execution_runtime
from pal.shared.tool_protocol import new_tool_call
async def main():
    runtime = build_execution_runtime()
    core = PalCore(context=MainContext(execution_runtime=runtime))
    try:
        register_with_core(core.context)
        core.publish_module_capabilities('execution')
        record = runtime.registry_generation.record_for_alias('run_shell')
        assert not {'wait_ms', 'target'} & record.input_schema['properties'].keys()
        assert 'shell_session' not in runtime.registry_generation.indirect_aliases
        result = await runtime.execute_tool_async(new_tool_call(name='run_shell', args={'cmd': 'printf standalone'}), turn_id='test')
        assert result.ok and 'standalone' in result.text, result.text
        assert not any(k.startswith('pal_shell_native') for k in sys.modules)
    finally:
        runtime.shutdown()
        core.close()
asyncio.run(main())
'''
    # A stale pre-plugin switch must not implicitly activate optional code.
    env = dict(os.environ, PAL_SHELL_BACKEND='native')
    env['PYTHONPATH'] = str(Path(__file__).resolve().parents[1] / 'src')
    completed = subprocess.run([sys.executable, '-c', script], env=env, capture_output=True, text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
