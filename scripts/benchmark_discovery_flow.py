"""Real-model discovery flow; only discovery executes, business calls are validated.

Paired prompt ablation on the same current matcher, not a historical code comparison.
Synthetic tasks and isolated tool registry; no live business state is accessed.
"""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.benchmark_alias_search import fixture
from pal.tool_efficiency_benchmark import endpoint_snapshot, build_llm

CASES = [
    ('llm', '查看你当前正在使用的 LLM endpoint 信息。', 'inspect_llm'),
    ('attachment', '把本地文件 /fixture/report.pdf 作为附件发回当前对话即可，不需要定向到其他会话。', 'send_channel_attachment'),
    ('checkbox', '把当前浏览器页面 ref=e12 的复选框勾选上。', 'set_browser_checked_state'),
    ('reload', '重新加载已经附加的 lsp 插件，让刚改的插件代码生效。', 'manage_plugin'),
    ('screenshot', '给当前浏览器页面截一张图。', 'capture_browser_screenshot'),
    ('usage', '看看 LLM 的 token 用量和花费。', 'inspect_llm'),
    ('wrong-domain', '只查找专用的 Git 状态工具；若工具目录没有就说明没有找到。不要用浏览器、LSP 或 shell 工具代替。', None),
]

# Controlled recovery probes, reported separately from model-authored queries.
SEEDED_CASES = [
    ('llm-fuzzy', '查看当前使用的 LLM endpoint。请先用 llm endpoint inspectt 作为搜索词；之后可按结果继续发现并调用正确工具。', 'inspect_llm'),
    ('screenshot-fuzzy', '给当前浏览器页面截图。请先用 browser page screenshot 作为搜索词；之后可按结果继续发现并调用正确工具。', 'capture_browser_screenshot'),
    ('wrong-domain', '请先用 git check status 搜索专用 Git 状态工具。没有就说明没找到，不要用 browser、LSP 或 shell 代替。', None),
]

PARAPHRASE_CASES = [
    ('checkbox-off', '请确保浏览器页面上 ref=e27 的复选框处于未选中状态，目前是否勾选未知。', 'set_browser_checked_state'),
    ('checkbox-on', '网页表单里 ref=e31 是复选框，把它设为选中，不要根据当前状态盲目切换。', 'set_browser_checked_state'),
    ('active-model', '现在真正负责回答我的模型连接是哪一个？查看正在生效的配置即可，不是列出全部配置。', 'inspect_llm'),
    ('plugin-code', 'browser 插件已加载，我修改了它的实现，请让这个插件重新载入代码。', 'manage_plugin'),
]


def make_fixture(root):
    from pal.core.module_registry import ModuleHandle
    from pal.llm.capabilities import LLMIntrospectionProvider
    from pal.channel.capabilities import ChannelIntrospectionProvider
    from pal.channel import ChannelRuntime
    fx = fixture(root)
    providers = {
        'llm': LLMIntrospectionProvider(runtime=SimpleNamespace()),
        'channel': ChannelIntrospectionProvider(runtime=ChannelRuntime(), repository=SimpleNamespace(),
                                               provider_manager=SimpleNamespace()),
    }
    for name, provider in providers.items():
        fx.core.context.register_module(ModuleHandle(module_id=name, tier='detachable', introspection_provider=provider))
        fx.core.publish_module_capabilities(name)
    return fx


def prompt_messages(variant, task, prior_fragments=None, candidate_fragments=None):
    from pal.core.prompt import MinimalOperatingRulesPromptFragmentProvider
    from pal.shared import PromptAssemblyContext
    from pal.llm.conversions import message_ir_from_dict
    fragments = MinimalOperatingRulesPromptFragmentProvider().build_prompt_fragments(PromptAssemblyContext())
    grouped = {'system': [], 'developer': []}
    for f in fragments:
        text = f.content
        if variant == 'candidate' and candidate_fragments is not None:
            text = candidate_fragments[f.section]
        elif variant == 'prior_prompt':
            if prior_fragments is not None:
                text = prior_fragments[f.section]
            elif f.section == 'system_map':
                text = text.split('\nTool-surface search domains')[0]
            elif f.section == 'tool_policy':
                text = '- On failure' + text.split('- On failure', 1)[1]
        grouped[f.metadata['prompt_target']].append(f'<{f.section}>\n{text}\n</{f.section}>')
    grouped['system'].append('This is an isolated discovery evaluation with synthetic inputs. '
        'Find the appropriate tool and issue its call; business execution is intercepted. '
        'You may search again, browse a domain directory, or inspect tool contracts. If no suitable tool exists, say so. '
        'Do not ask for confirmation: the synthetic operation is authorized. No live business tools execute.')
    return [message_ir_from_dict({'role': role, 'content': '\n'.join(texts)})
            for role, texts in grouped.items()] + [message_ir_from_dict({'role': 'user', 'content': task})]


async def run_case(llm, values, fx, variant, case, repetition, prior_fragments=None, candidate_fragments=None,
                   *, tool_specs=None, max_output_tokens=2048, intercepted_introspection=None,
                   recover_errors=False, intercepted_results=None):
    from pal.llm.conversions import tool_definition_ir_from_dict
    from pal.llm.ir import GenerationPolicyIR, LLMRequestIR, TextPartIR
    from pal.llm.serde import message_to_payload
    from pal.shared.json_values import thaw_json
    from pal.eval_tools import _eval_tool_result_message
    from pal.execution.tool_facade import InvocationMode, RejectedResult
    from pal.execution.tool_search import _invocation_capability_result
    from pal.shared.tool_protocol import new_tool_call
    key, task, expected = case
    row = dict(case=key, task=task, variant=variant, repetition=repetition, expected=expected,
               responses=[], searches=[], reads=[], usage=[], selected=False, valid=False)
    runtime = fx.runtime
    generation = runtime.registry_generation
    specs = tool_specs if tool_specs is not None else thaw_json(dict(generation.provider_specs))
    discovery = {'search_tools', 'read_tool'}
    available = {name: specs[name] for name in discovery | {'call_tool'} if name in specs}
    messages = prompt_messages(variant, task, prior_fragments, candidate_fragments)
    for _ in range(6):
        outcome = await asyncio.wait_for(llm.agenerate(LLMRequestIR(
            messages=tuple(messages), tools=tuple(tool_definition_ir_from_dict(t) for t in available.values()),
            model_hint=values['model_id'], policy=GenerationPolicyIR(max_output_tokens=max_output_tokens, temperature=0),
            metadata={'preferred_endpoint_id': values['endpoint_id'], 'endpoint_fallback_policy': 'none',
                      'think_level': 'medium', 'case_id': key})), timeout=150)
        if outcome.preferred_endpoint_id != values['endpoint_id'] or outcome.preferred_model_id != values['model_id']:
            raise RuntimeError('Endpoint or model drift')
        message = outcome.response.message
        row['responses'].append(message_to_payload(message))
        usage = outcome.response.usage
        if outcome.finish_reason.value == 'error':
            row['error'] = 'endpoint_failure'
            return row
        row['usage'].append({k: getattr(usage, k, 0) for k in ('input_tokens', 'output_tokens', 'reported')})
        messages.append(message)
        calls = list(outcome.tool_calls or ())
        if not calls:
            row['abstained'] = True
            row['valid'] = expected is None and bool(row['searches']) and any(
                isinstance(part, TextPartIR) and part.text.strip() for part in message.parts)
            return row
        for call in calls:
            alias = call.args.get('name') if call.name == 'call_tool' else call.name
            args = dict(call.args.get('args') or {}) if call.name == 'call_tool' else dict(call.args)
            if alias in discovery:
                result = await runtime.execute_tool_async(call, turn_id=f'eval:{variant}:{key}:{repetition}')
                data = result.structured or {}
                if alias == 'search_tools':
                    row['searches'].append({'args': dict(call.args), 'hits': [h['alias'] for h in data.get('hits', [])],
                        'suggestions': [s['alias'] for s in data.get('suggestions', [])],
                        'directory': data.get('directory'), 'available_domains': data.get('available_domains'),
                        'ok': result.ok})
                if alias == 'read_tool':
                    row['reads'].append(dict(call.args))
                # Direct tools are exposed only after a search hit or explicit contract read.
                exposed = [h['alias'] for h in data.get('hits', [])]
                if alias == 'read_tool' and result.ok:
                    exposed.append(args.get('name'))
                for name in exposed:
                    if name in specs:
                        available[name] = specs[name]
                messages.append(_eval_tool_result_message(call, result))
                continue
            record = generation.record_for_alias(alias) if isinstance(alias, str) else None
            if recover_errors and isinstance(alias, str):
                target_call = new_tool_call(name=alias, args=args)
                resolved = runtime._resolve_invocation_record(generation, target_call,
                    invocation_mode=InvocationMode.INDIRECT if call.name == 'call_tool' else InvocationMode.DIRECT)
                validated = (resolved if isinstance(resolved, RejectedResult)
                             else runtime._validate_invocation_input(resolved, args))
                if isinstance(validated, RejectedResult):
                    # Execute only a proven rejection path: lookup/mode/input
                    # checks run before any business handler or lifecycle hook.
                    result = await runtime.execute_tool_async(call, turn_id=f'eval:{variant}:{key}:{repetition}')
                    assert not result.ok
                    data = result.structured or {}
                    row.setdefault('rejections', []).append({'alias': alias, 'route': call.name, 'result': data})
                    for affordance in data.get('affordances', []):
                        if affordance['tool'] in specs:
                            available[affordance['tool']] = specs[affordance['tool']]
                    messages.append(_eval_tool_result_message(call, result))
                    continue
            if record and alias in (intercepted_introspection or {}):
                validated = runtime._validate_invocation_input(record, args)
                route_ok = (call.name == 'call_tool') == (record.execution.invocation_mode.value == 'indirect')
                if route_ok and getattr(validated, 'kind', None) != 'rejected':
                    # Synthetic preflight state supplied by the evaluator, never
                    # a call to the business handler or the user's runtime.
                    data = intercepted_introspection[alias]
                    row.setdefault('preflights', []).append({'alias': alias, 'args': args})
                    messages.append(_eval_tool_result_message(call, SimpleNamespace(
                        ok=True, status='ok', structured=data, llm_text=json.dumps(data, ensure_ascii=False))))
                    continue
            feedback_key = alias
            operation = args.get('operation')
            if record and isinstance(operation, str) and (alias, operation) in (intercepted_results or {}):
                feedback_key = (alias, operation)
            if record and feedback_key in (intercepted_results or {}):
                validated = runtime._validate_invocation_input(record, args)
                route_ok = (call.name == 'call_tool') == (record.execution.invocation_mode.value == 'indirect')
                if route_ok and getattr(validated, 'kind', None) != 'rejected':
                    raw = intercepted_results[feedback_key]
                    normalized = runtime._normalize_invocation_result(record,
                        new_tool_call(name=alias, args=args), raw, budget=None,
                        turn_id=f'eval:{variant}:{key}:{repetition}')
                    result = _invocation_capability_result(runtime, alias, normalized)
                    row.setdefault('action_feedback', []).append({'alias': alias, 'args': args,
                        'result': result.structured, 'effect': normalized.effect.value,
                        'affordances': [a.model_dump(mode='json') for a in result.affordances]})
                    for affordance in result.affordances:
                        if affordance.tool in specs:
                            available[affordance.tool] = specs[affordance.tool]
                    messages.append(_eval_tool_result_message(call, result))
                    continue
            row.update(selected_alias=alias, selected_args=args, selected=alias == expected,
                       selection_route=call.name)
            if record:
                validated = runtime._validate_invocation_input(record, args)
                route_ok = (call.name == 'call_tool') == (record.execution.invocation_mode.value == 'indirect')
                row['valid'] = alias == expected and route_ok and getattr(validated, 'kind', None) != 'rejected'
                # A merged alias alone no longer establishes the requested action.
                operations = {'reload': 'reload', 'plugin-code': 'reload', 'reload-code': 'reload',
                              'reload-resources': 'reload', 'mode-recovery': 'reload',
                              'attach': 'attach', 'enable': 'enable'}
                expected_operation = operations.get(key) if expected == 'manage_plugin' else (
                    {'close': 'close', 'cache': 'clear_cache'}.get(key) if expected == 'manage_browser_session' else
                    {'restart': 'restart', 'reconnect': 'restart', 'transport': 'restart',
                     'endpoint-reconnect': 'restart', 'endpoint-detached': 'attach',
                     'attach-recovery': 'restart'}.get(key) if expected == 'manage_channel_endpoint' else
                    {'pause': 'disable', 'resume': 'enable', 'enable': 'enable',
                     'disable': 'disable', 'delete': 'delete', 'remove': 'delete'}.get(key)
                    if expected == 'manage_proactive_task' else
                    {'attach': 'attach', 'connect': 'attach', 'detach': 'detach',
                     'disconnect': 'detach'}.get(key) if expected == 'manage_mcp_server' else
                    {'pause': 'pause', 'resume': 'resume', 'cancel': 'cancel',
                     'archive': 'archive'}.get(key) if expected == 'control_bunshin_workflow' else None)
                if expected_operation is not None:
                    row['operation_correct'] = args.get('operation') == expected_operation
                    row['valid'] = row['valid'] and row['operation_correct']
            return row  # Never execute ANY business invocation, including shell/read_file.
    row['error'] = 'round_limit'
    return row


async def main(args):
    cases = PARAPHRASE_CASES if args.paraphrases else SEEDED_CASES if args.seeded else CASES
    prior_fragments = json.loads(args.prior_fragments.read_text()) if args.prior_fragments else None
    candidate_fragments = json.loads(args.candidate_fragments.read_text()) if args.candidate_fragments else None
    if args.case:
        cases = [case for case in cases if case[0] == args.case]
        if not cases:
            raise ValueError('Unknown case')
    values = endpoint_snapshot(args.runtime_root, args.endpoint)
    llm = build_llm(args.runtime_root, values)
    args.output.mkdir(parents=True, exist_ok=args.resume)
    rows = json.loads((args.output / 'runs.json').read_text()) if args.resume else []
    semaphore = asyncio.Semaphore(4)
    async def one(variant, case, repetition):
        existing = next((r for r in rows if (r['variant'], r['case'], r['repetition']) ==
                         (variant, case[0], repetition)), None)
        if existing is not None:
            return existing
        async with semaphore:
            with tempfile.TemporaryDirectory(prefix='pal-discovery-flow-') as root:
                fx = make_fixture(Path(root))
                try:
                    row = await run_case(llm, values, fx, variant, case, repetition, prior_fragments, candidate_fragments)
                except Exception as exc:
                    row = dict(variant=variant, case=case[0], repetition=repetition, error=type(exc).__name__)
                finally:
                    fx.close()
            rows.append(row)
            (args.output / 'runs.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            print(json.dumps({k: row.get(k) for k in ('variant', 'case', 'repetition', 'valid', 'error')}, ensure_ascii=False), flush=True)
            return row
    try:
        # Start with a paired smoke test; stop on endpoint failures before spending more calls.
        smoke = await asyncio.gather(*(one(v, cases[0], 1) for v in ('prior_prompt', 'candidate')))
        if any(r.get('error') for r in smoke):
            return
        await asyncio.gather(*(one(v, c, rep) for rep in range(1, args.repetitions + 1)
            for c in cases for v in ('prior_prompt', 'candidate') if not (rep == 1 and c == cases[0])))
    finally:
        report = dict(model=values['model_id'], endpoint=values['endpoint_id'], scope=__doc__,
                      prior_fragments=str(args.prior_fragments) if args.prior_fragments else None,
                      candidate_fragments=str(args.candidate_fragments) if args.candidate_fragments else None,
                      query_source='seeded recovery probes' if args.seeded else 'model-authored', variants={})
        for variant in ('prior_prompt', 'candidate'):
            subset = [r for r in rows if r['variant'] == variant]
            positive = [r for r in subset if r['case'] != 'wrong-domain']
            report['variants'][variant] = dict(runs=len(subset), errors=sum('error' in r for r in subset),
                correct_valid=sum(bool(r.get('valid')) for r in subset), positive_runs=len(positive),
                first_search_recall=sum(bool(r.get('searches')) and r['expected'] in r['searches'][0]['hits'] for r in positive),
                input_tokens=sum(u['input_tokens'] for r in subset for u in r.get('usage', [])),
                output_tokens=sum(u['output_tokens'] for r in subset for u in r.get('usage', [])))
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
        print(json.dumps(report, ensure_ascii=False), flush=True)
        llm.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--endpoint')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, default=2)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--seeded', action='store_true')
    parser.add_argument('--case')
    parser.add_argument('--paraphrases', action='store_true')
    parser.add_argument('--prior-fragments', type=Path)
    parser.add_argument('--candidate-fragments', type=Path)
    asyncio.run(main(parser.parse_args()))
