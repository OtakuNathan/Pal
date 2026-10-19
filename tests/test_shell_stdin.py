import asyncio
import hashlib
import shlex
import sys
from unittest.mock import patch

import pytest

from pal.execution.generated_tool_models import ExecutionShellExecShellExecCapabilityMixinShellInput as ShellInput
from pal.execution.shell_exec import ShellExecTool
from pal.execution.tool_facade import EffectOutcome


def command(source):
    return shlex.join([sys.executable, '-c', source])


@pytest.mark.parametrize('payload', [None, '', '  中文\n\r\n\x00\"\' $HOME $(false) EOF  ', 'x' * 300000],
                         ids=['omitted', 'empty', 'literal', 'large'])
def test_literal_input_and_eof(tmp_path, payload):
    source = 'import sys,hashlib;print(hashlib.sha256(sys.stdin.buffer.read()).hexdigest())'
    result = ShellExecTool(output_root=tmp_path).invoke({'cmd': command(source), 'target': 0, 'stdin': payload})
    assert result.status == 'ok', result.text
    assert result.structured['target'] == 0
    assert result.structured['stdout'].strip() == hashlib.sha256((payload or '').encode()).hexdigest()
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize('args', [{'target': 1}, {'target': -1}, {'target': True}, {'target': '0'},
                                 {'stdin': 12}, {'stdin': '\ud800'}])
def test_invalid_inputs_never_spawn(tmp_path, args):
    with patch('pal.execution.shell_exec.subprocess.Popen') as spawn:
        result = ShellExecTool(output_root=tmp_path).invoke({'cmd': 'echo unexpected', **args})
    spawn.assert_not_called()
    assert result.status == 'invalid'
    assert result.effect_receipt.outcome == EffectOutcome.NOT_STARTED


def test_script_input_and_early_exit(tmp_path):
    tool = ShellExecTool(output_root=tmp_path)
    result = tool.invoke({'cmd': shlex.join([sys.executable, '-']), 'stdin': 'print("literal $HOME")\n'})
    assert result.structured['stdout'] == 'literal $HOME\n'
    result = tool.invoke({'cmd': command('raise SystemExit(7)'), 'stdin': 'x' * 300000})
    assert result.structured['returncode'] == 7
    assert list(tmp_path.iterdir()) == []


def test_stdin_timeout_and_failed_spawn_clean_up(tmp_path):
    tool = ShellExecTool(output_root=tmp_path)
    result = tool.invoke({'cmd': command('import time;time.sleep(30)'), 'stdin': 'x' * 300000, 'timeout_ms': 50})
    assert result.structured['timed_out']
    result = tool.invoke({'cmd': 'true', 'stdin': 'content', 'cwd': str(tmp_path / 'missing')})
    assert result.structured['error_code'] == 'shell_spawn_failed'
    assert list(tmp_path.iterdir()) == []


def test_async_stdin_cancellation_cleans_up(tmp_path):
    async def run():
        marker = tmp_path / 'started'
        source = f'import pathlib,time;pathlib.Path({str(marker)!r}).touch();time.sleep(30)'
        task = asyncio.create_task(ShellExecTool(output_root=tmp_path / 'output').ainvoke(
            {'cmd': command(source), 'stdin': 'x' * 300000}))
        async with asyncio.timeout(5):
            while not marker.exists():
                await asyncio.sleep(.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert list((tmp_path / 'output').iterdir()) == []
    asyncio.run(run())


def test_shell_input_contract_defaults():
    model = ShellInput.model_validate({'cmd': 'cat'})
    assert model.target == 0 and model.stdin is None
    assert ShellInput.model_validate({'cmd': 'cat', 'stdin': ''}).stdin == ''
    assert ShellInput.model_validate({'cmd': 'cat', 'stdin': None}).stdin is None
