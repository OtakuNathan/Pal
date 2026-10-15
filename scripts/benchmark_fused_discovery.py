"""Paired tool discovery: frozen keyword-first baseline versus lexical fusion.

Both variants identify tools without executing business handlers. Regression
requests include repeated earlier Chinese cases; novel requests are fixed before
the first model run. Static resident/role unions are not live availability.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import random
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.benchmark_domain_directory import before_schema, paired_prompts
from scripts.benchmark_full_discovery_flow import build_flow_catalog, manifest_prompt, run_case, summarize
from scripts.replay_purpose_discovery import load_matcher
from pal.execution.runtime import ExecutionRuntime
from pal.tool_efficiency_benchmark import build_llm, endpoint_snapshot


NOVEL = [
    ('model', '我想核实这轮对话实际走了哪个模型连接，以及目前累计消耗多少 token。', 'inspect_llm'),
    ('endpoint', '备用模型连接能接受多少上下文、支持哪些能力？查看它公布的限制。', 'inspect_llm'),
    ('transport', 'Telegram 的连接卡死了，只重建这个 endpoint 的传输连接。', 'manage_channel_endpoint'),
    ('reload', '我更新了 browser 插件实现，要让新代码生效，并恢复受影响的依赖插件。', 'manage_plugin'),
    ('attach', '插件仍启用，只是被卸下了运行实例。把它重新挂载回来。', 'manage_plugin'),
    ('checkbox', '网页复选框 e7 我要它处于未勾选状态，现在是什么状态还不知道。', 'set_browser_checked_state'),
    ('fill', '网页表单 e8 里已有旧内容，把整段值替换为 paid。', 'fill_browser_field'),
    ('type', '网页输入框已有前缀，光标位置已经对了。再逐键输入后缀 xyz。', 'type_browser_text'),
    ('snapshot', '获取网页控件结构，给我能用于下一步点击的元素引用。', 'capture_browser_snapshot'),
    ('cache', '页面可能拿到了旧的 HTTP 响应，清除网络缓存，但登录状态要保留。', 'manage_browser_session'),
    ('history', '早报自动任务过去几次运行都怎样？需要多次运行记录来比较。', 'list_proactive_runs'),
    ('pause', '暂停早报的定时执行，任务定义以后还要用。', 'manage_proactive_task'),
    ('timing', '早报任务的内容不变，只把触发时间改到每天八点半。', 'update_proactive_schedule'),
    ('hover', '代码这个位置的符号是什么类型？顺便查看签名和文档。', 'read_lsp_hover'),
    ('references', '查代码里引用这个符号的所有位置，不只找函数的调用关系。', 'find_lsp_references'),
    ('artifact', '已知 PDF 逐页文本路径 /fixture/pages/page_*.txt，请搜索付款期限相关内容。', 'run_shell'),
    ('negative-repository', '需要专门返回 Git 工作区状态的工具。没有的话说明没有，别改用 shell。', None),
    ('negative-checkbox', '需要专门删除网页复选框 DOM 节点的工具；取消勾选或运行脚本都不算。', None),
    ('negative-endpoint', '需要永久删除模型 endpoint 配置的专门工具，切换模型不能替代。', None),
    ('negative-memory', '需要 memory 模块里直接发送邮件附件的专门工具，不要换成 channel。', None),
]


async def main(args):
    from pal.execution.generated_tool_models import ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput as SearchInput
    records, _, generations = build_flow_catalog()
    baseline_records = json.loads((args.baseline / 'before-catalog.json').read_text())
    before_generations = {group: SimpleNamespace(**{
        **vars(generation), 'search_records': {a: baseline_records[a] for a in generation.search_records}
    }) for group, generation in generations.items()}
    cases = [{**case, 'cohort': 'regression'} for case in json.loads(args.regression_manifest.read_text())['cases']]
    cases += [dict(alias=alias, case_id='novel-' + key, task=task, group='resident_union',
                   category='catalog' if alias else 'negative', cohort='heldout_new', repetition=0)
              for key, task, alias in NOVEL]
    schemas = {'before': before_schema(args.baseline / 'before-source'), 'current': SearchInput.model_json_schema()}
    for schema in schemas.values():
        for key in ('namespace', 'family', 'module_name', 'tags', 'facets'):
            schema['properties'].pop(key, None)
    prompt_pair = paired_prompts(args.baseline / 'before-source')
    prompts = {'before': prompt_pair['before'], 'current': prompt_pair['directory']}
    searches = {'before': load_matcher(args.baseline / 'before-source/src/pal/execution/runtime.py',
                                      args.baseline / 'before-source/src/pal/execution/discovery_terms.py'),
                'current': ExecutionRuntime._search_generation}
    values = endpoint_snapshot(args.runtime_root, args.endpoint)
    source_paths = [Path(__file__), Path('scripts/benchmark_full_discovery_flow.py'),
                    Path('src/pal/execution/runtime.py'), Path('src/pal/execution/discovery_terms.py'),
                    Path('src/pal/shared/tool_routing.py')]
    manifest = dict(scope=__doc__, model=values['model_id'], endpoint=values['endpoint_id'], cases=cases,
                    variants=list(searches), schemas=schemas,
                    prompts={k: manifest_prompt(v) for k, v in prompts.items()},
                    max_output_tokens=args.max_output_tokens,
                    catalog_sha256=hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
                    baseline_catalog_sha256=hashlib.sha256(json.dumps(baseline_records, sort_keys=True).encode()).hexdigest(),
                    source_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_paths})
    args.output.mkdir(parents=True, exist_ok=args.resume)
    if args.resume:
        assert json.loads((args.output / 'manifest.json').read_text()) == json.loads(json.dumps(manifest)), 'Changed trial inputs'
    else:
        (args.output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (args.output / 'catalog.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
    rows = json.loads((args.output / 'runs.json').read_text()) if args.resume else []
    llm = build_llm(args.runtime_root, values)
    semaphore = asyncio.Semaphore(args.concurrency)
    failures = 0
    stopped = False

    def save():
        (args.output / 'runs.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        metrics = {}
        for variant in searches:
            for cohort in ('all', 'regression', 'heldout_new'):
                selected = [r for r in rows if r['variant'] == variant and (cohort == 'all' or r['cohort'] == cohort)]
                expected = [c for c in cases if cohort == 'all' or c['cohort'] == cohort]
                metrics[f'{variant}:{cohort}'] = summarize(selected, expected)
        (args.output / 'report.json').write_text(json.dumps(dict(stopped=stopped, metrics=metrics), ensure_ascii=False, indent=2))

    async def one(variant, case):
        nonlocal failures, stopped
        if any(r['variant'] == variant and r['case_id'] == case['case_id'] and r['repetition'] == case['repetition'] for r in rows):
            return
        async with semaphore:
            if stopped:
                return
            try:
                row = await run_case(llm, values, case,
                    (before_generations if variant == 'before' else generations)[case['group']],
                    searches[variant], schemas[variant], prompts[variant], max_output_tokens=args.max_output_tokens,
                    discovery_description=(
                        'Search or browse tools. Known domains are strict. Domain alone returns up to 16 names and brief purposes; read a plausible contract. Add short action/object query words to rank full contracts, or browse another page with directory_offset and next_offset. Directory entries are navigation, not matches.'
                        if variant == 'before' else None))
                failures = failures + 1 if row.get('error') == 'endpoint_failure' else 0
            except Exception as exc:
                row = {**case, 'error': type(exc).__name__, 'correct': False}
                failures += 1
            if failures >= 3:
                stopped = True
            row['variant'] = variant
            rows.append(row)
            save()
            print(json.dumps(dict(completed=len(rows), variant=variant, case=case['case_id'],
                                  correct=row.get('correct'), error=row.get('error')), ensure_ascii=False), flush=True)

    try:
        pending = [(v, c) for c in cases for v in searches]
        random.Random(20261013).shuffle(pending)
        await asyncio.gather(*(one(v, c) for v, c in pending))
    finally:
        save()
        llm.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--endpoint', default='glm-5.3')
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--regression-manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--max-output-tokens', type=int, default=8192)
    parser.add_argument('--resume', action='store_true')
    asyncio.run(main(parser.parse_args()))
