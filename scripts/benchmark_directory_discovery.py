"""Paired directory recovery evaluation on isolated current tool contracts.

Before uses a trusted pre-change matcher/prompt/schema snapshot. After uses current
code. Only discovery calls execute. Business calls are intercepted and input/mode
validated; success measures selection, not business effects. Cases are authored
Chinese requests, including impossible operations; not production traffic.
"""
from __future__ import annotations
import argparse
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import statistics
import sys
import tempfile
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.benchmark_discovery_flow import make_fixture, run_case
from scripts.replay_purpose_discovery import load_matcher
from pal.tool_efficiency_benchmark import endpoint_snapshot, build_llm
from types import SimpleNamespace

CASES = [
 ('reload-code', '我改了 Telegram channel provider 的 Python 实现，需要重新载入代码，让已有连接使用新实现。provider 的 plugin_id 是 telegram_demo。', 'manage_plugin'),
 ('reload-resources', 'channel provider 的公共资源和实现改了，请卸载旧实例并加载新的，再恢复原来的 endpoints。plugin_id 是 demo_provider。', 'manage_plugin'),
 ('discover-add', '我刚把一个新的 channel provider 包放进 runtime root，先扫描一下发现它。', 'rescan_plugins'),
 ('discover-remove', '一个 channel provider 包已经从磁盘删除了，重新扫描目录，让运行时发现这个变化。', 'rescan_plugins'),
 ('endpoint-reconnect', 'endpoint_id=demo_endpoint 仍然 attached，运行实例还在，但网络连接已经断了。请重建它的 transport session，不需要重新载入 Python 代码。', 'manage_channel_endpoint'),
 ('endpoint-detached', 'endpoint_id=demo_endpoint 已被明确 detach，现在没有运行实例。请恢复这个 endpoint 的收发连接。', 'manage_channel_endpoint'),
 ('attachment', '请把本地文件 /fixture/report.pdf 作为附件发到当前聊天 channel。', 'send_channel_attachment'),
 ('llm', '查看你当前正在使用的 LLM endpoint 信息。', 'inspect_llm'),
 ('checkbox', '把当前浏览器页面 ref=e12 的复选框勾选上。', 'set_browser_checked_state'),
 ('absent-channel-delete', '找一个专门删除 channel 附件的工具；如果只有发送附件工具就说明没有找到，不要用别的工具代替。', None),
 ('absent-plugin-backup', '找一个专门备份 plugin 数据的工具；如果没有就说明没有找到，不要用安装、卸载或重载代替。', None),
 ('absent-git', '只查找专用的 Git 状态工具；若工具目录没有就说明没有找到。不要用浏览器、LSP 或 shell 工具代替。', None),
]

RECOVERY_PROBES = [
 ('mode-recovery', '先通过 call_tool(name="list_plugins", args={}) 查看插件；如果返回调用错误，请按工具反馈纠正，然后重载 demo_provider 的代码。', 'manage_plugin'),
 ('attach-recovery', 'endpoint_id=demo_endpoint 仍然 attached，但连接坏了。先尝试 manage_channel_endpoint(operation=attach)；如果结果说没有重建 transport，再根据反馈修复连接，不重新载入 Python 代码。', 'manage_channel_endpoint'),
]


def install_endpoint_state(fx, root, *, attached):
    """A synthetic transport publishes real endpoint contracts without network I/O."""
    from pal.channel import ChannelEndpointQueueBase, EndpointConfig
    from pal.channel.provider_manager import ChannelEndpointProviderManager, FactoryChannelProvider
    provider = fx.core.context.module_registry.get('channel').introspection_provider
    record = SimpleNamespace(endpoint_id='demo_endpoint', channel_kind='demo_runtime',
        binding_key='fixture', enabled=True, detached_at=None if attached else 'detached')
    def set_attached(_, value):
        record.detached_at = None if value else 'detached'
        return record
    repository = SimpleNamespace(get=lambda _: record, set_attached=set_attached, list_all=lambda: [record])
    class Endpoint(ChannelEndpointQueueBase):
        def normalize_raw(self, payload): return {}
        def send_reply(self, response_handle, text): pass
        def inspect_health(self): return {'healthy': False, 'connected': False}
        def inspect_auth_state(self): return {'authorized': True}
    def create(row, *, runtime_root):
        return Endpoint(endpoint=EndpointConfig(endpoint_id=row.endpoint_id,
            channel_kind=row.channel_kind, binding_key=row.binding_key))
    manager = ChannelEndpointProviderManager(provider.runtime, repository, root)
    manager.register_provider(FactoryChannelProvider('demo', ('demo_runtime',), SimpleNamespace(create=create)))
    provider.repository, provider.provider_manager = repository, manager
    provider.main_context = fx.core.context
    if attached:
        assert manager.attach_endpoint(record.endpoint_id).status == 'ok'
    else:
        manager._ensure_hub(record, 'demo')
    provider._republish_capabilities()
    return manager


def summarize(rows):
    positives = [r for r in rows if r['expected'] is not None]
    negatives = [r for r in rows if r['expected'] is None]
    return dict(runs=len(rows), errors=sum(bool(r.get('error')) for r in rows),
        positive_runs=len(positives), correct_selected=sum(bool(r.get('selected')) for r in positives),
        correct_valid=sum(bool(r.get('valid')) for r in positives),
        wrong_selections=sum('selected_alias' in r and (r['selected_alias'] != r['expected'] or r.get('operation_correct') is False) for r in rows),
        negative_runs=len(negatives), correct_abstentions=sum(bool(r.get('valid')) for r in negatives),
        negative_selections=sum('selected_alias' in r for r in negatives),
        directory_seen=sum(any(s.get('directory') for s in r.get('searches', [])) for r in rows),
        recoverable_error_runs=sum(bool(r.get('rejections')) for r in rows),
        recovered_error_runs=sum(bool(r.get('rejections')) and bool(r.get('valid')) for r in rows),
        action_feedback_runs=sum(bool(r.get('action_feedback')) for r in rows),
        action_feedback_resolved=sum(bool(r.get('action_feedback')) and bool(r.get('valid')) for r in rows),
        mean_rounds=statistics.mean(len(r.get('responses', [])) for r in rows) if rows else 0,
        input_tokens=sum(u['input_tokens'] for r in rows for u in r.get('usage', [])),
        output_tokens=sum(u['output_tokens'] for r in rows for u in r.get('usage', [])))


async def main(args):
    from pal.core.prompt import MinimalOperatingRulesPromptFragmentProvider
    from pal.shared import PromptAssemblyContext
    from pal.shared.json_values import thaw_json
    prior_fragments = json.loads((args.baseline/'prior-fragments.json').read_text())
    cases = RECOVERY_PROBES if args.recovery_probes else CASES
    candidate_fragments = {f.section:f.content for f in
        MinimalOperatingRulesPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext())}
    baseline_matcher = load_matcher(args.baseline/'prior-runtime.py')
    # This is a trusted local source snapshot, not downloaded code.
    namespace = {'__name__': 'pal.execution.generated_tool_models'}
    exec(compile((args.baseline/'prior-models.py').read_text(), 'prior-models.py', 'exec'), namespace)
    prior_schema = namespace['ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput'].model_json_schema()
    values = endpoint_snapshot(args.runtime_root, args.endpoint)
    llm = build_llm(args.runtime_root, values)
    args.output.mkdir(parents=True, exist_ok=args.resume)
    manifest = dict(scope=__doc__, endpoint=values['endpoint_id'], model=values['model_id'], cases=cases,
        repetitions=args.repetitions, max_output_tokens=args.max_output_tokens,
        synthetic_preflights=args.synthetic_preflights,
        recover_affordances=args.recover_affordances, recovery_probes=args.recovery_probes,
        prior_fragments=prior_fragments, candidate_fragments=candidate_fragments,
        source_sha256={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in
            [args.baseline/'prior-runtime.py', args.baseline/'prior-models.py',
             Path('src/pal/execution/runtime.py'), Path('src/pal/execution/generated_tool_models.py')]})
    if args.resume:
        if json.loads((args.output/'manifest.json').read_text()) != json.loads(json.dumps(manifest)):
            raise ValueError('Changed experiment cannot resume')
    else:
        (args.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
    rows = json.loads((args.output/'runs.json').read_text()) if args.resume else []
    semaphore = asyncio.Semaphore(args.concurrency)
    async def one(variant, case, rep):
        existing = next((r for r in rows if (r['variant'],r['case'],r['repetition'])==(variant,case[0],rep)),None)
        if existing is not None:
            return existing
        async with semaphore:
            with tempfile.TemporaryDirectory(prefix='pal-directory-eval-') as root:
                fx = make_fixture(Path(root))
                try:
                    attached = case[0] != 'endpoint-detached'
                    manager = install_endpoint_state(fx, Path(root), attached=attached) if args.recover_affordances else None
                    specs = thaw_json(dict(fx.runtime.registry_generation.provider_specs))
                    if variant == 'before':
                        fx.runtime._search_generation = baseline_matcher
                        specs = copy.deepcopy(specs)
                        function = specs['search_tools']['function']
                        function['parameters'] = prior_schema
                        function['description'] = ('Find callable tools using an optional strict domain bucket and '
                            'short action/object keywords or an alias. Positive purpose text supplies relevance. '
                            'Separate fuzzy suggestions require contract review. On a miss shorten the query, '
                            'remove guessed filters, or browse search_tools(domain=...). An empty result does not prove unavailability.')
                    row = await run_case(llm, values, fx,
                        'prior_prompt' if variant == 'before' else 'candidate', case, rep,
                        prior_fragments, candidate_fragments, tool_specs=specs,
                        max_output_tokens=args.max_output_tokens,
                        recover_errors=args.recover_affordances,
                        intercepted_results={('manage_channel_endpoint', 'attach'): manager.attach_endpoint('demo_endpoint')}
                            if args.recover_affordances and attached else None,
                        intercepted_introspection={
                            'list_plugins': {'items': [{'name': p, 'plugin_id': p, 'enabled': True, 'attached': True}
                                for p in ('telegram_demo', 'demo_provider')]},
                            'list_channel_endpoints': {'items': [{'name': 'demo_endpoint', 'endpoint_id': 'demo_endpoint',
                                'channel_kind': 'demo_runtime', 'enabled': True, 'attached': attached}]},
                            'inspect_channel_endpoint': {'endpoint_id': 'demo_endpoint',
                                'attached': attached, 'healthy': False, 'connected': False},
                        } if args.synthetic_preflights else None)
                    row['variant'] = variant
                except Exception as exc:
                    row = dict(variant=variant,case=case[0],repetition=rep,expected=case[2],error=type(exc).__name__)
                finally:
                    fx.close()
            rows.append(row)
            (args.output/'runs.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2))
            print(json.dumps({k:row.get(k) for k in ('variant','case','repetition','selected_alias','valid','error')},ensure_ascii=False),flush=True)
            return row
    try:
        smoke = await asyncio.gather(*(one(v,cases[0],1) for v in ('before','after')))
        if not any(r.get('error') for r in smoke):
            await asyncio.gather(*(one(v,c,rep) for rep in range(1,args.repetitions+1)
                for c in cases for v in (('before','after') if rep%2 else ('after','before'))
                if not (rep==1 and c==cases[0])))
    finally:
        report = dict(model=values['model_id'],endpoint=values['endpoint_id'],
            variants={v:summarize([r for r in rows if r['variant']==v]) for v in ('before','after')},
            limitations=['Authored scenarios and isolated current registry, not held-out production traffic.',
                'Six autonomous response rounds allowed; no evaluator retry hint.',
                'Business invocations intercepted: valid selection is not successful execution.',
                'Optional synthetic preflights return evaluator-authored plugin/endpoint listings, never live state.',
                'With recover-affordances, real lookup/mode/input rejection paths run and model recovery continues. Already-attached feedback comes from the real manager on a synthetic transport; replacing business calls remain intercepted.',
                'Before search definition is reconstructed from saved input schema and a compact prior description.',
                'Current tool implementations and contracts are identical in both variants.'])
        (args.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        print(json.dumps(report,ensure_ascii=False),flush=True)
        llm.close()

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root',type=Path,required=True)
    parser.add_argument('--endpoint',required=True)
    parser.add_argument('--baseline',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--repetitions',type=int,default=2)
    parser.add_argument('--concurrency',type=int,default=3)
    parser.add_argument('--max-output-tokens',type=int,default=8192)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--synthetic-preflights',action='store_true')
    parser.add_argument('--recover-affordances',action='store_true')
    parser.add_argument('--recovery-probes',action='store_true')
    asyncio.run(main(parser.parse_args()))
