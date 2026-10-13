"""Paired keyword-first versus bounded domain-directory discovery evaluation.

Both variants use the same authored catalog and fixed Chinese requests. Only
discovery executes. Selection validates alias/mode, not business arguments or
execution. Original requests repeat; held-out requests are fixed independently
of tool purposes. Source snapshots are trusted local pre-trial files.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import runpy
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.benchmark_full_discovery_flow import build_flow_catalog, manifest_prompt, run_case, summarize
from scripts.benchmark_discovery_flow import prompt_messages
from scripts.replay_purpose_discovery import load_matcher
from pal.execution.runtime import ExecutionRuntime
from pal.tool_efficiency_benchmark import build_llm, endpoint_snapshot


HELD_OUT = [
    ('attach', '插件 browser 已经启用但还没挂载，把它挂回来；已有实例应当保留。', 'attach_plugin'),
    ('enable', '插件 web_fetch 被禁用了，把它启用并加载起来。', 'enable_plugin'),
    ('plugin', '检查 browser 插件为什么加载失败，看看它的代次、依赖和最近错误。', 'inspect_plugin'),
    ('references', '找项目里这个函数都在哪里被引用，不需要调用链。', 'find_lsp_references'),
    ('hover', '看看源码这个位置的符号类型、签名和文档说明。', 'read_lsp_hover'),
    ('snapshot', '获取当前网页的可访问性结构和元素引用，方便接着操作。', 'capture_browser_snapshot'),
    ('fill', '把网页输入框 ref=e20 的内容替换成 hello，不是追加输入。', 'fill_browser_field'),
    ('type', '光标已经在网页输入框里，逐键输入 hello，保留原有文字。', 'type_browser_text'),
    ('select', '网页下拉框 ref=e21 选择 option value 为 pro 的那一项。', 'select_browser_option'),
    ('close', '关闭当前对话的浏览器实例，保留配置和登录数据。', 'close_browser'),
    ('cache', '清掉浏览器 HTTP 缓存，保留 cookies 和站点存储。', 'clear_browser_cache'),
    ('pause', '暂时暂停 morning-report 定时任务，保留任务定义和运行历史。', 'disable_proactive_task'),
    ('schedule', '把 morning-report 的执行计划改成每周一十点，其他任务配置保持不变。', 'update_proactive_schedule'),
    ('history', '列出 morning-report 最近几次执行的历史，不是只看最后一次。', 'list_proactive_runs'),
    ('grep', '在收到的附件内容中查找付款条款，给出匹配片段。', 'grep_artifact'),
    ('forget', '忘掉 mem_ref=m1 这条持久记忆，不要卸载记忆存储后端。', 'forget_memory'),
    ('negative-git', '找专用的 Git 状态工具；没有就说明没有，不要用 shell 替代。', None),
    ('negative-cluster', '找专用的 Kubernetes 集群健康工具，不要用网页浏览器或者 shell 替代。', None),
    ('negative-message-delete', '找一个删除已经送出的通道消息的专用工具，发送新消息不能替代。', None),
    ('negative-endpoint-delete', '找一个永久删除模型 endpoint 配置的专用工具，不要用切换或者 shell 替代。', None),
]


def before_schema(root):
    path = root / 'src/pal/execution/generated_tool_models.py'
    spec = importlib.util.spec_from_file_location('_pal_domain_trial_before_models', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput.model_json_schema()


def paired_prompts(root, directory_policy=None):
    from pal.core import prompt as prompt_module
    names = ('TOOL_EXECUTION_SYSTEM_POLICY', 'TOOL_ROUTING_DEVELOPER_GUIDANCE', 'TOOL_EFFICIENCY_DEVELOPER_GUIDANCE')
    saved = {name: getattr(prompt_module, name) for name in names}
    try:
        if directory_policy is not None:
            policy = runpy.run_path(str(directory_policy))
            for name in names:
                setattr(prompt_module, name, policy[name])
        current = prompt_messages('candidate', '')[:-1]
        previous = runpy.run_path(str(root / 'src/pal/shared/tool_routing.py'))
        for name in names:
            setattr(prompt_module, name, previous[name])
        before = prompt_messages('candidate', '')[:-1]
    finally:
        for name in names:
            setattr(prompt_module, name, saved[name])
    return {'before': before, 'directory': current}


async def main(args):
    from pal.execution.generated_tool_models import ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput as SearchInput
    records, _, generations = build_flow_catalog()
    original = json.loads(args.original_manifest.read_text())['cases']
    unique = {c['case_id']: {k: v for k, v in c.items() if k != 'repetition'} for c in original}
    cases = [{**c, 'cohort': 'original', 'repetition': repetition}
             for repetition in range(args.repetitions) for c in unique.values()]
    cases += [dict(alias=alias, case_id='heldout-' + key, task=task, group='resident_union',
                   category='catalog' if alias else 'negative', cohort='heldout', repetition=0)
              for key, task, alias in HELD_OUT]
    assert all(c['alias'] is None or c['alias'] in generations['resident_union'].search_records for c in cases)
    schemas = {'before': before_schema(args.before_source), 'directory': SearchInput.model_json_schema()}
    for schema in schemas.values():
        for name in ('namespace', 'family', 'module_name', 'tags', 'facets'):
            schema['properties'].pop(name, None)
    prompts = paired_prompts(args.before_source, args.directory_policy)
    searches = {'before': load_matcher(args.before_source / 'src/pal/execution/runtime.py'),
                'directory': ExecutionRuntime._search_generation}
    values = endpoint_snapshot(args.runtime_root, args.endpoint)
    source_files = [Path(__file__), Path('scripts/benchmark_full_discovery_flow.py'),
                    Path('src/pal/execution/runtime.py'), Path('src/pal/shared/tool_routing.py'),
                    args.before_source / 'src/pal/execution/runtime.py']
    if args.directory_policy is not None:
        source_files.append(args.directory_policy)
    manifest = dict(scope=__doc__, model=values['model_id'], endpoint=values['endpoint_id'], cases=cases,
                    catalog_sha256=hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
                    schemas=schemas, prompts={k: manifest_prompt(v) for k, v in prompts.items()},
                    max_output_tokens=args.max_output_tokens,
                    source_sha256={str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in source_files})
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
            for cohort in ('all', 'original', 'heldout'):
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
                row = await run_case(llm, values, case, generations['resident_union'], searches[variant],
                                     schemas[variant], prompts[variant], max_output_tokens=args.max_output_tokens,
                                     discovery_description=(
                                         'Search for tools. Known domains are strict. Use short action/object words. Directory entries on misses are navigation, not matches; read a plausible contract. Browse with directory_offset and follow next_offset.'
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
    parser.add_argument('--before-source', type=Path, required=True)
    parser.add_argument('--original-manifest', type=Path, required=True)
    parser.add_argument('--directory-policy', type=Path, help='Trusted saved directory-first routing policy; otherwise use current policy.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, default=2)
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--max-output-tokens', type=int, default=8192)
    parser.add_argument('--resume', action='store_true')
    asyncio.run(main(parser.parse_args()))
