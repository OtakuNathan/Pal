"""Paired, isolated first-search benchmark. Never executes business tools.

The model first generates one search call, then selects/invokes from its results.
Only discovery and schema validation run locally. This measures discovery and
first-call contracts, not successful business execution or end-to-end agents.
Run the same script against pre/post source snapshots with --base/--candidate.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import tempfile
from types import SimpleNamespace

CASES = (
    ('remember', '记住我偏好简短的中文回答。', 'remember_memory'),
    ('recall', '查一下你记得我对回答风格有什么偏好。', 'recall_memory'),
    ('memory-update', '将现有记忆 mem_fixture 更新为：用户希望详细回答。', 'update_memory'),
    ('lsp-callers', '用 LSP 查找 /fixture/sample.py 中第 1 行第 5 列函数的调用者，项目根是 /fixture。', 'find_lsp_incoming_calls'),
    ('lsp-prepare', '为 /fixture 这个 Python 工作区配置并预热 LSP 环境。', 'prepare_lsp_workspace'),
    ('reload-plugin', '重新加载已附加的 lsp 插件，让刚改的插件代码生效。', 'reload_plugin'),
    ('install-provider', '在运行中的 Pal 安装本地频道 provider 包 /fixture/provider.palpkg。', 'install_package'),
    ('screenshot', '给当前已打开的浏览器页面截一张图。', 'capture_browser_screenshot'),
    ('read-artifact', '读取这次会话里 artifact_id 为 art_fixture 的文本附件。', 'read_artifact'),
    ('read-file', '读取本地 /fixture/sample.py 的第 1 到第 20 行。', 'read_file'),
)


def fixture(root):
    from pal.tool_efficiency_benchmark import FixtureExecution
    from pal.memory.capabilities import register_with_core as memory
    from pal.plugins.capabilities import register_with_core as plugins
    from pal.artifact.capabilities import register_with_core as artifact
    from pal.bunshin.workflow_capabilities import BunshinPublicProvider
    from pal.core.module_registry import ModuleHandle
    value = FixtureExecution(root, 'affordance-search-benchmark')
    from pal.memory.service import MemoryService
    memory(value.core.context, MemoryService())
    plugins(value.core.context, SimpleNamespace())
    artifact(value.core.context, SimpleNamespace())
    value.core.context.register_module(ModuleHandle(
        module_id='bunshin', tier='detachable', introspection_provider=BunshinPublicProvider(root)))
    for module in ('memory', 'plugins', 'artifact', 'bunshin'):
        value.core.publish_module_capabilities(module)
    return value


def summarize(rows):
    count = len(rows)
    rate = lambda name: sum(bool(row.get(name)) for row in rows) / count if count else 0
    usage = [item for row in rows for item in row.get('usage', [])]
    return dict(runs=count, errors=sum('error' in row for row in rows),
                first_search_top1=rate('top1'), first_search_recall=rate('recall'),
                first_selection=rate('selected'), first_call_valid=rate('valid'),
                mean_hits=statistics.mean(row.get('hit_count', 0) for row in rows) if rows else 0,
                mean_search_chars=statistics.mean(row.get('search_chars', 0) for row in rows) if rows else 0,
                input_tokens=sum(item['input_tokens'] for item in usage),
                output_tokens=sum(item['output_tokens'] for item in usage),
                usage_complete=bool(usage) and all(item['reported'] for item in usage))


async def run_case(llm, values, fx, case, repetition):
    from pal.eval_tools import _eval_tool_result_message
    from pal.llm.conversions import message_ir_from_dict, tool_definition_ir_from_dict
    from pal.llm.ir import GenerationPolicyIR, LLMRequestIR
    from pal.shared.json_values import thaw_json
    from pal.shared.tool_routing import TOOL_ROUTING_DEVELOPER_GUIDANCE
    case_id, prompt, expected = case
    runtime = fx.runtime
    specs = thaw_json(dict(runtime.registry_generation.provider_specs))
    row = dict(case=case_id, repetition=repetition, expected=expected, usage=[], responses=[],
               registry_generation=runtime.registry_generation.generation_hash)
    messages = [message_ir_from_dict({'role': 'system', 'content':
        'This is an isolated tool-discovery evaluation. First issue exactly one search_tools call '
        'for the task; do not set top_k or limit. After receiving results, issue exactly one '
        'business tool call with the arguments needed for the task. Business effects are simulated. '
        'Do not call read_tool or search a second time in this first-attempt evaluation.\n'
        + TOOL_ROUTING_DEVELOPER_GUIDANCE}),
        message_ir_from_dict({'role': 'user', 'content': prompt})]

    async def generate(tools):
        outcome = await asyncio.wait_for(llm.agenerate(LLMRequestIR(
            messages=tuple(messages), tools=tuple(tool_definition_ir_from_dict(tool) for tool in tools),
            model_hint=values['model_id'], policy=GenerationPolicyIR(max_output_tokens=4096, temperature=0),
            metadata={'preferred_endpoint_id': values['endpoint_id'], 'preferred_endpoint_source': 'alias_search_eval',
                      'endpoint_fallback_policy': 'none', 'think_level': 'medium', 'case_id': case_id})), timeout=150)
        if outcome.preferred_model_id != values['model_id'] or outcome.preferred_endpoint_id != values['endpoint_id']:
            raise RuntimeError('Endpoint or model drift')
        from pal.llm.serde import message_to_payload
        row['responses'].append(message_to_payload(outcome.response.message))
        usage = outcome.response.usage
        row['usage'].append({key: getattr(usage, key, 0) for key in ('input_tokens', 'output_tokens', 'reported')})
        messages.append(outcome.response.message)
        return list(outcome.tool_calls or ())

    calls = await generate([specs['search_tools']])
    if len(calls) != 1 or calls[0].name != 'search_tools':
        return {**row, 'error': 'expected_one_search'}
    row['search_args'] = dict(calls[0].args)
    if calls[0].args.get('top_k') is not None or calls[0].args.get('limit') is not None:
        return {**row, 'error': 'overrode_default_limit'}
    result = await runtime.execute_tool_async(calls[0], turn_id=f'eval:{case_id}:{repetition}')
    hits = list((result.structured or {}).get('hits', ()))
    aliases = [hit['alias'] for hit in hits]
    row.update(hits=aliases, hit_count=len(hits), search_chars=len(result.llm_text),
               top1=bool(aliases and aliases[0] == expected), recall=expected in aliases)
    messages.append(_eval_tool_result_message(calls[0], result))
    # A direct hit remains directly callable; indirect calls use call_tool.
    available = [specs['call_tool']] + [specs[name] for name in aliases if name in specs and name != 'call_tool']
    calls = await generate(available)
    if len(calls) != 1:
        return {**row, 'error': 'expected_one_selection'}
    call = calls[0]
    name = call.args.get('name') if call.name == 'call_tool' else call.name
    args = (call.args.get('args') or {}) if call.name == 'call_tool' else dict(call.args)
    record = runtime.registry_generation.record_for_alias(name)
    valid = False
    if record is not None:
        mode = record.execution.invocation_mode.value
        route_ok = (call.name == 'call_tool') == (mode == 'indirect')
        validated = runtime._validate_invocation_input(record, args)
        valid = route_ok and getattr(validated, 'kind', None) != 'rejected'
    row.update(selected_alias=name, selected_args=args, selection_route=call.name, selected=name == expected,
               valid=name == expected and valid)
    return row


async def worker(args):
    from pal.tool_efficiency_benchmark import endpoint_snapshot, build_llm
    values = endpoint_snapshot(args.runtime_root, args.endpoint)
    if values['model_id'].lower() != 'glm-5.3':
        raise ValueError('This run requires glm-5.3 exactly')
    llm = build_llm(args.runtime_root, values)
    try:
        for line in sys.stdin:
            request = json.loads(line)
            case = next(case for case in CASES if case[0] == request['case'])
            with tempfile.TemporaryDirectory(prefix='pal-search-fixture-') as temp:
                fx = fixture(Path(temp))
                try:
                    row = await run_case(llm, values, fx, case, request['repetition'])
                except Exception as exc:
                    # Avoid accidentally logging credentials/headers from provider errors.
                    row = dict(case=case[0], repetition=request['repetition'], error=type(exc).__name__)
                finally:
                    fx.close()
                print(json.dumps(row, ensure_ascii=False), flush=True)
    finally:
        llm.close()


async def compare(args):
    args.output.mkdir(parents=True, mode=0o700, exist_ok=False)
    rows = {'base': [], 'candidate': []}
    processes = {}
    try:
        for label, root in (('base', args.base), ('candidate', args.candidate)):
            log = (args.output / f'{label}.stderr').open('w')
            process = await asyncio.create_subprocess_exec(
                sys.executable, str(Path(__file__).resolve()), '--worker', '--runtime-root', str(args.runtime_root),
                '--endpoint', args.endpoint, env={**os.environ, 'PYTHONPATH': str(root.resolve() / 'src')},
                cwd=root, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=log)
            processes[label] = (process, log)
        for repetition in range(1, args.repetitions + 1):
            for case in CASES:
                if args.case and case[0] != args.case:
                    continue
                async def one(label):
                    process = processes[label][0]
                    process.stdin.write((json.dumps({'case': case[0], 'repetition': repetition}) + '\n').encode())
                    await process.stdin.drain()
                    line = await process.stdout.readline()
                    if not line:
                        raise RuntimeError(f'{label} worker exited; inspect its stderr')
                    row = json.loads(line)
                    rows[label].append(row)
                    (args.output / f'{label}.json').write_text(json.dumps(rows[label], ensure_ascii=False, indent=2))
                    print(json.dumps({'variant': label, **{key: row.get(key) for key in
                        ('case', 'repetition', 'top1', 'recall', 'valid', 'error')}}, ensure_ascii=False), flush=True)
                labels = ('base', 'candidate') if repetition % 2 else ('candidate', 'base')
                await asyncio.gather(*(one(label) for label in labels))
                # Quota/auth/connection failures must not trigger dozens of retries.
                if all(rows[label][-1].get('error') in {'RuntimeError', 'TimeoutError', 'LLMInvocationError'} for label in rows):
                    raise RuntimeError('Both endpoints failed; stop instead of spending more calls')
    finally:
        report = {label: summarize(values) for label, values in rows.items()}
        report.update(model='glm-5.3', endpoint=args.endpoint, repetitions=args.repetitions,
                      cases=1 if args.case else len(CASES), scope='isolated discovery plus schema validation; no business execution')
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)
        for process, log in processes.values():
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 10)
            except asyncio.TimeoutError:
                process.terminate()
                await process.wait()
            log.close()


def replay_queries(fx, queries):
    """Hold model-generated queries fixed to isolate search ranking changes."""
    from pal.execution.tool_presentation import render_tool_search
    rows = []
    for item in queries:
        result = fx.runtime._search_generation(fx.runtime.registry_generation, item['args'])
        aliases = [hit['alias'] for hit in result['hits']]
        rows.append({**item, 'hits': aliases, 'hit_count': len(aliases),
                     'search_chars': len(render_tool_search(fx.runtime.registry_generation, result)),
                     'top1': bool(aliases and aliases[0] == item['expected']),
                     'recall': item['expected'] in aliases,
                     'recall_at3': item['expected'] in aliases[:3]})
    summary = {key: value for key, value in summarize(rows).items()
               if key in {'runs', 'first_search_top1', 'first_search_recall', 'mean_hits', 'mean_search_chars'}}
    summary['recall_at3'] = sum(row['recall_at3'] for row in rows) / len(rows) if rows else 0
    return {'summary': summary, 'rows': rows, 'registry_generation': fx.runtime.registry_generation.generation_hash}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path)
    parser.add_argument('--endpoint', default='glm-5.3')
    parser.add_argument('--base', type=Path)
    parser.add_argument('--candidate', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--worker', action='store_true')
    parser.add_argument('--replay-input', type=Path)
    parser.add_argument('--case', choices=[case[0] for case in CASES])
    args = parser.parse_args()
    if args.replay_input:
        with tempfile.TemporaryDirectory(prefix='pal-search-replay-') as root:
            fx = fixture(Path(root))
            try:
                print(json.dumps(replay_queries(fx, json.loads(args.replay_input.read_text())), ensure_ascii=False))
            finally:
                fx.close()
        return
    if not args.runtime_root:
        parser.error('--runtime-root is required for model evaluation')
    if not args.worker and not all((args.base, args.candidate, args.output)):
        parser.error('--base, --candidate and --output are required')
    asyncio.run(worker(args) if args.worker else compare(args))


if __name__ == '__main__':
    main()
