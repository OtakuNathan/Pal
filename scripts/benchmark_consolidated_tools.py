"""Model smoke: discover consolidated tools and validate task-specific parameters.

Uses a disposable mounted fixture, not the entire authored union or live tools.
Only discovery executes. Business route/schema/selected fields are validated;
actual page, device and lifecycle effects are covered by deterministic tests.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.benchmark_discovery_flow import make_fixture, run_case
from pal.emotion import build_emotion_handle
from pal.tool_efficiency_benchmark import endpoint_snapshot, build_llm

# Fixed before the first model run. A merged tool name does not establish the
# intended action; check these parameters independently of schema validation.
CASES = (
    ('reload', 'lsp 插件已加载，我更新了它的 Python 实现。让新代码生效。', 'manage_plugin', {'name': 'lsp', 'operation': 'reload'}),
    ('attach', 'demo 插件已经启用但目前没挂载。挂载它，若已有实例应保留。', 'manage_plugin', {'name': 'demo', 'operation': 'attach'}),
    ('enable', 'demo 插件已禁用，现在启用并加载它。', 'manage_plugin', {'name': 'demo', 'operation': 'enable'}),
    ('page-open', '在当前会话浏览器打开 https://example.com 并给我正文和链接。', 'read_browser_page', {'url': 'https://example.com'}),
    ('page-current', '网页内容刚变了，重新读取当前页的正文，不导航到别的 URL。', 'read_browser_page', {}),
    ('cache', '清除当前会话浏览器的 HTTP 缓存，保留 cookies 和登录数据。', 'manage_browser_session', {'operation': 'clear_cache'}),
    ('close', '关闭当前会话的浏览器进程，保留 profile 和登录数据。', 'manage_browser_session', {'operation': 'close'}),
    ('reset', '我确认要删除当前会话的整个浏览器 profile，包括登录数据。', 'manage_browser_session', {'operation': 'reset', 'confirm': True}),
    ('definition', '用 LSP 找 /fixture/a.py 第 0 行第 4 个 UTF-16 字符位置符号的定义。', 'find_lsp_symbol_locations', {'kind': 'definition', 'file': '/fixture/a.py', 'line': 0, 'character': 4}),
    ('implementation', '用 LSP 找 /fixture/a.py 第 0 行第 4 个 UTF-16 字符位置符号的具体实现。', 'find_lsp_symbol_locations', {'kind': 'implementation', 'file': '/fixture/a.py', 'line': 0, 'character': 4}),
    ('callers', '用 LSP 找哪些函数调用 /fixture/a.py 第 0 行第 4 个 UTF-16 字符处的函数。', 'find_lsp_calls', {'direction': 'callers', 'file': '/fixture/a.py', 'line': 0, 'character': 4}),
    ('callees', '用 LSP 找 /fixture/a.py 第 0 行第 4 个 UTF-16 字符处的函数调用了哪些函数。', 'find_lsp_calls', {'direction': 'callees', 'file': '/fixture/a.py', 'line': 0, 'character': 4}),
    ('memory-health', '看一下记忆模块和当前后端的健康摘要、记录数量，不做维护。', 'inspect_memory_state', {}),
    ('emotion', '让所有启用的表情设备表达 wink，时长由运行时负责。', 'show_emotion', {'emotion': 'wink'}),
    ('file', '读取本地 UTF-8 文件 /fixture/a.py。', 'read_file', {'file_path': '/fixture/a.py'}),
    ('no-backend-tool', '找直接切换记忆存储后端的专用模型工具。没有就说明没有；不要改用 shell、CLI 或通用插件管理工具。', None, {}),
)


def parameters_correct(key, actual, required):
    if key == 'page-current' and actual.get('url'):
        return False
    return all(actual.get(name) == value for name, value in required.items())


async def main(args):
    values = endpoint_snapshot(args.runtime_root, args.endpoint)
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / 'manifest.json').write_text(json.dumps({
        'scope': __doc__, 'endpoint': values['endpoint_id'], 'model': values['model_id'], 'cases': CASES,
        'repetitions': 1, 'max_output_tokens': 2048,
    }, ensure_ascii=False, indent=2))
    llm = build_llm(args.runtime_root, values)
    rows = []
    gate = asyncio.Semaphore(2)
    async def one(case):
        key, task, expected, required = case
        async with gate:
            with tempfile.TemporaryDirectory(prefix='pal-consolidated-tools-') as temporary:
                fx = make_fixture(Path(temporary))
                emotion = build_emotion_handle(fx.core.context)
                fx.core.publish_module_capabilities('emotion')
                scope = SimpleNamespace(published=True, handle=None, defer=lambda _: None)
                emotion.ports['emotion'].register(scope, 'fixture', ('happy', 'wink'), lambda: [])
                try:
                    row = await run_case(llm, values, fx, 'candidate', (key, task, expected), 1)
                    row['parameters_correct'] = parameters_correct(key, row.get('selected_args', {}), required)
                    row['task_correct'] = bool(row.get('valid') and row['parameters_correct'])
                    row['fixture_tool_count'] = len(fx.runtime.registry_generation.search_records)
                except Exception as exc:
                    row = {'case': key, 'error': type(exc).__name__, 'task_correct': False}
                finally:
                    fx.close()
            rows.append(row)
            (args.output / 'runs.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            print(json.dumps({name: row.get(name) for name in ('case', 'task_correct', 'selected_alias', 'selected_args', 'error')}, ensure_ascii=False), flush=True)
            return row
    try:
        smoke = await one(CASES[0])
        if not smoke.get('error'):
            await asyncio.gather(*(one(case) for case in CASES[1:]))
    finally:
        llm.close()
        report = {'model': values['model_id'], 'completed': len(rows), 'expected': len(CASES),
                  'task_correct': sum(bool(row.get('task_correct')) for row in rows),
                  'errors': sum(bool(row.get('error')) for row in rows),
                  'mean_rounds': statistics.mean(len(row.get('responses', [])) for row in rows) if rows else 0,
                  'scope': __doc__}
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
