"""Chinese memory tasks with real facade execution against disposable SQLite stores.

The authored resident union supplies competing discovery candidates. Only memory
CRUD and inspection handlers execute, via production alias/mode/input validation.
Other business handlers and arbitrary shell execution are blocked. The four native memory
CRUD schemas are visible initially, as in Pal. The LLM endpoint is read from the
requested runtime; production memory and configuration are never mutated.
Development and held-out cases are fixed before model runs. This small curated
suite is not a measurement of real traffic or recall across every subsystem.
"""
from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
import tempfile
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jsonschema import Draft202012Validator
from pal.core import PalCore
from pal.eval_tools import _eval_tool_result_message
from pal.execution.capabilities import register_with_core as register_execution
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import RejectedResult
from pal.llm.conversions import message_ir_from_dict, tool_definition_ir_from_dict
from pal.llm.ir import GenerationPolicyIR, LLMRequestIR
from pal.llm.serde import message_to_payload
from pal.memory import MemoryService, register_with_core as register_memory
from pal.memory.contracts import L3CommitRequest
from pal.memory.embedding import HashingEmbedder
from pal.memory.repository import L3ProviderSelector
from pal.memory.storage import MemoryStorage
from pal.plugins.l3 import MockL3Plugin, SQLiteVecL3Plugin, register_with_core as register_l3
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import new_tool_call
from pal.tool_efficiency_benchmark import build_llm, endpoint_snapshot
from scripts.benchmark_discovery_flow import prompt_messages
from scripts.benchmark_full_discovery_flow import build_flow_catalog, tool_contract


@dataclass(frozen=True)
class Case:
    case_id: str
    split: str
    prompt: str
    target: str | None
    goal: str


CASES = (
    Case('preference', 'development', '我以前说过喜欢喝什么？查一下长期记忆，别猜。', 'recall_memory', 'read_preference'),
    Case('exact-project', 'development', '读取记忆 {project}，告诉我 Pal 工具检索的约束，暂时不用改。', 'recall_memory', 'read_project'),
    Case('remember', 'development', '帮我记住：我希望每天 09:00 提醒我开始工作，这是长期偏好。', 'remember_memory', 'remember'),
    Case('correct', 'development', '把记忆 {preference} 中我喜欢的饮品改成红茶，原来的咖啡偏好过时了。保留其他记忆。', 'update_memory', 'correct'),
    Case('forget', 'development', '忘掉记忆 {preference} 这条饮品偏好，其余记忆保留。', 'forget_memory', 'forget'),
    Case('inventory', 'development', '帮我看看 sqlite_vec_l3 记忆后端的记录数量和索引健康情况。只检查，不重建索引。', 'inspect_memory_provider_inventory', 'inventory'),
    Case('repair-experience', 'held_out', 'SQLite 记忆报 database is locked，以前有没有处理经验？找出当时有效的修复办法。', 'recall_memory', 'read_case'),
    Case('already-known', 'held_out', '当前上下文已经明确：我喜欢咖啡。只复述这句话，不用查询或保存任何记忆。', None, 'no_op'),
    Case('save-case', 'held_out', '记下这次排障经验：browser 插件改代码后行为没变；目标是加载新代码；执行 reload_plugin；随后验证新行为成功。作为以后可复用的案例保存。', 'remember_memory', 'save_case'),
    Case('find-and-correct', 'held_out', '之前记过我爱喝咖啡，那条现在不对了，我改喝红茶了。找出并修正旧记录，别额外新增重复偏好，也别改项目记忆。', 'update_memory', 'correct'),
    Case('reconcile-original', 'held_out', '上次保存可能已经成功，返回 provider_id=sqlite_vec_l3、mem_ref={preference}。现在 active provider 已经切到 other_l3。先核实原库里那条的内容，不要再次写入，也不要切换 active provider。', 'recall_memory', 'reconcile'),
    Case('unknown-provider', 'held_out', '核实原 provider_id=missing_l3 的记忆 {preference}。如果原库不可用就明确告诉我；不要改查当前库，更不要写入或切换 provider。', 'recall_memory', 'unknown_provider'),
)
DISCOVERY = {'search_tools', 'read_tool', 'list_tools'}
WRITES = {'remember_memory', 'update_memory', 'forget_memory'}
ALLOWED = WRITES | {'recall_memory', 'inspect_memory_provider_inventory',
                   'inspect_memory_provider_state', 'inspect_active_memory_provider', 'list_memory_providers'}


def corrected_drink_text(text: str) -> bool:
    """Allow a clearly deprecated old preference, but not two current ones.

    This is a bounded Chinese fixture oracle, not a general semantic judge. Both
    stored summary and source text must contain the new preference. Mentioning
    coffee is allowed only in a clause explicitly describing it as past/negated.
    """
    if '红茶' not in text:
        return False
    clauses = re.split(r'[，,。；;（）()\n]', text)
    if not any('红茶' in clause and not re.search(r'不(?:再)?(?:喜欢|爱|想)?(?:喝)?红茶', clause)
               for clause in clauses):
        return False
    return all(re.search(r'此前|以前|之前|过去|原来|原先|原本|曾经|旧|过时|失效|不再|不喜欢|不喝|不爱|已改|[从由].*咖啡.*改(?:为|成|喝).*红茶', clause)
               for clause in clauses if '咖啡' in clause)


class MemoryFixture:
    def __init__(self, root: Path, case: Case):
        self.core = PalCore()
        self.runtime = self.core.context.execution_runtime
        register_execution(self.core.context)
        self.memory = MemoryService(l3_selector=L3ProviderSelector(resolver=self.runtime.l3_plugin_registry.require))
        register_memory(self.core.context, self.memory)
        storage = MemoryStorage(root)
        storage.create_initial()
        self.repository = storage.open()
        self.provider = SQLiteVecL3Plugin(service=self.memory, repository=self.repository, embedder=HashingEmbedder())
        register_l3(self.core.context, self.provider)
        self.memory.l3_selector.active_provider_id = self.provider.provider_id
        for module in ('execution', 'memory', self.provider.module_id):
            self.core.publish_module_capabilities(module)
        seeds = {
            'preference': ('fact', '用户喜欢咖啡。', '用户 饮品 偏好 喜欢 喝 咖啡', {}),
            'project': ('fact', 'Pal 工具检索应避免向量检索，优先使用 domain 和 action。', 'Pal 工具 检索 向量 domain action', {}),
            'case': ('case', 'SQLite database is locked：缩短写事务并在提交后重试，验证成功。',
                     'SQLite database is locked 记忆 锁 写事务 修复 重试',
                     {'situation': 'SQLite database is locked', 'task': '恢复记忆写入',
                      'action': '缩短写事务并在提交后重试', 'result': '写入成功'}),
        }
        self.refs = {}
        for key, (kind, summary, search_text, payload) in seeds.items():
            result = self.provider.commit(L3CommitRequest(
                mutation_id=f'seed-{key}', kind=kind, summary=summary,
                search_text=search_text, scope='system', payload=payload))
            if result.status != 'ok':
                raise RuntimeError(f'Fixture seed failed: {result.status}')
            self.refs[key] = result.document_id
        self.other = MockL3Plugin(provider_id='other_l3', records=[{
            'document_id': self.refs['preference'], 'document_kind': 'fact',
            'summary': '另一个库的同名记录：用户喜欢汽水。'}])
        self.runtime.l3_plugin_registry.register(self.other)
        if case.goal == 'reconcile':
            self.memory.l3_selector.active_provider_id = self.other.provider_id
        self.initial = self.snapshot()

    def snapshot(self):
        # Inspect documents directly: grading must not perform semantic recall,
        # update usage counters, or consume a model's tool/result context.
        repo = self.repository
        documents = [repo.get_document(row['document_id']) for row in repo.list_projection_rows()]
        keys = ('document_id', 'document_kind', 'summary', 'search_text', 'payload')
        return {'documents': sorted([{k: doc.get(k) for k in keys} for doc in documents if doc],
                                   key=lambda doc: doc['document_id']),
                'active_provider': self.memory.l3_selector.active_provider_id,
                'other_documents': json.loads(json.dumps(self.other.records, ensure_ascii=False))}

    def close(self):
        self.runtime.shutdown()
        self.repository.close()


def grade(case: Case, fixture: MemoryFixture, calls: list[dict], final_text: str):
    after = fixture.snapshot()
    before = fixture.initial
    documents = {d['document_id']: d for d in after['documents']}
    originals = {d['document_id']: d for d in before['documents']}
    successful = [c for c in calls if c['result'].get('kind') == 'complete']
    targets = [c for c in calls if c['alias'] == case.target]
    # A no-op requires a final answer and no business calls. Mere absence of
    # changes is insufficient for requested read/write/inspection tasks.
    selected = bool(targets) if case.target else bool(final_text.strip()) and not calls
    schema_valid = any(c['arguments_valid'] for c in targets) if case.target else selected
    accepted = any(c in successful for c in targets) if case.target else selected
    unchanged = after == before
    constraints = (after['active_provider'] == before['active_provider']
                   and after['other_documents'] == before['other_documents'])
    for key in ('project', 'case'):
        ref = fixture.refs[key]
        constraints = constraints and documents.get(ref) == originals[ref]
    read_keys = {'read_preference': 'preference', 'read_project': 'project', 'read_case': 'case', 'reconcile': 'preference'}
    if case.goal in read_keys:
        ref = fixture.refs[read_keys[case.goal]]
        goal = any(c['alias'] == 'recall_memory'
                   and c['result'].get('output', {}).get('provider_id') == fixture.provider.provider_id
                   and any(h.get('mem_ref', h.get('document_id')) == ref
                           for h in c['result'].get('output', {}).get('hits_preview', [])) for c in successful)
        goal = goal and unchanged and not any(c['alias'] in WRITES for c in calls)
    elif case.goal == 'correct':
        ref = fixture.refs['preference']
        changed = [d for d in after['documents'] if d != originals.get(d['document_id'])]
        goal = (accepted and len(changed) == 1
                and corrected_drink_text(changed[0]['summary'])
                and corrected_drink_text(changed[0]['search_text']))
        goal = goal and len(after['documents']) == len(before['documents']) and ref not in documents
        goal = goal and any(c['alias'] == 'update_memory' and c['args'].get('mem_ref') == ref for c in successful)
    elif case.goal == 'forget':
        goal = accepted and fixture.refs['preference'] not in documents and len(documents) == len(originals)-1
    elif case.goal in {'remember', 'save_case'}:
        added = [d for ref, d in documents.items() if ref not in originals]
        constraints = constraints and all(documents.get(ref) == doc for ref, doc in originals.items())
        goal = accepted and len(added) == 1
        if goal and case.goal == 'remember':
            text = added[0]['summary']
            goal = added[0]['document_kind'] == 'fact' and ('09:00' in text or '9:00' in text or '9点' in text) and '提醒' in text
        elif goal:
            doc = added[0]
            payload = doc['payload'] or {}
            goal = (doc['document_kind'] == 'case' and 'reload_plugin' in json.dumps(doc, ensure_ascii=False)
                    and all(str(payload.get(k) or '').strip() for k in ('situation', 'task', 'action', 'result')))
    elif case.goal == 'inventory':
        goal = (accepted and unchanged and not any(c['alias'] in WRITES for c in calls)
                and any(c['alias'] == case.target and c['result'].get('output', {}).get('fact_count') == 2
                        and c['result'].get('output', {}).get('case_count') == 1
                        and 'pending_embeddings' in c['result'].get('output', {}) for c in successful))
    elif case.goal == 'unknown_provider':
        goal = (schema_valid and bool(final_text.strip()) and unchanged and bool(targets)
                and all(c['args'].get('provider_id') == 'missing_l3' and c['result'].get('kind') != 'complete'
                        for c in calls if c['alias'] == 'recall_memory')
                and not any(c['alias'] in WRITES for c in calls))
    else:
        goal = selected and unchanged and '咖啡' in final_text
    return {'decision_correct': selected,
            'tool_identified': selected if case.target else None,
            'arguments_valid': schema_valid if case.target else None,
            'execution_accepted': accepted if case.target else None,
            'no_op_respected': selected if case.target is None else None,
            'expected_unavailability_observed': bool(goal) if case.goal == 'unknown_provider' else None,
            'goal_verified': bool(goal),
            'constraints_preserved': bool(constraints), 'business_success': bool(goal and constraints),
            'state_before': before, 'state_after': after}


async def run_case(llm, values, case, repetition, catalog, max_rounds):
    with tempfile.TemporaryDirectory(prefix='pal-memory-eval-') as folder:
        fx = MemoryFixture(Path(folder), case)
        try:
            records = dict(catalog)
            # Published provider contracts carry name selectors missing from a
            # purely authored static union. Use real contracts for this owner.
            records.update(thaw_json(dict(fx.runtime.registry_generation.search_records)))
            view = SimpleNamespace(search_records=records)
            specs = thaw_json(dict(fx.runtime.registry_generation.provider_specs))
            available = {a: specs[a] for a in DISCOVERY | {'call_tool', 'recall_memory', 'remember_memory', 'update_memory', 'forget_memory'} if a in specs}
            task = case.prompt.format_map(fx.refs)
            messages = [m for m in prompt_messages('candidate', '')[:-1]
                        if m.role.value != 'system']
            base = prompt_messages('candidate', '')[0].text.split('This is an isolated discovery evaluation')[0]
            messages.insert(0, message_ir_from_dict({'role': 'system', 'content': base +
                '\nThis is an authorized task against disposable memory fixtures. Memory tools execute real '
                'handlers and persist changes. Search the authored resident union as needed; other business '
                'handlers are blocked by the fixture. Only the user-requested memory changes are authorized. '
                'Do not use an evaluation selection tool. Finish with a normal answer based on actual results.'}))
            messages.append(message_ir_from_dict({'role': 'user', 'content': task}))
            row = {'case_id': case.case_id, 'split': case.split, 'repetition': repetition,
                   'task': task, 'expected_alias': case.target, 'goal': case.goal, 'responses': [], 'calls': [],
                   'searches': [], 'usage': [], 'final_text': '', 'protocol_errors': [], 'business_calls': [],
                   'initial_tool_names': sorted(available), 'tool_window_hashes': [],
                   'fixture_refs': fx.refs, 'registry_generation': fx.runtime.registry_generation.generation_hash}
            for _ in range(max_rounds):
                row['tool_window_hashes'].append(hashlib.sha256(json.dumps(available, sort_keys=True).encode()).hexdigest())
                outcome = await asyncio.wait_for(llm.agenerate(LLMRequestIR(
                    messages=tuple(messages), tools=tuple(tool_definition_ir_from_dict(available[a]) for a in sorted(available)),
                    model_hint=values['model_id'], policy=GenerationPolicyIR(max_output_tokens=8192, temperature=0),
                    metadata={'preferred_endpoint_id': values['endpoint_id'], 'endpoint_fallback_policy': 'none',
                              'think_level': 'medium', 'case_id': case.case_id})), 150)
                row['usage'].append({k: getattr(outcome.response.usage, k, 0) for k in ('input_tokens', 'output_tokens', 'cached_input_tokens', 'uncached_input_tokens', 'cache_write_input_tokens', 'cache_partition_reported', 'reported_fields')})
                if outcome.finish_reason.value == 'error':
                    metadata = outcome.response.message.metadata
                    row['error'] = 'endpoint_failure'
                    row['endpoint_failure'] = {key: metadata.get(key) for key in ('failure_kind', 'error_type', 'failure_subsystem')}
                    row['responses'].append({'role': 'assistant', 'finish_reason': 'error', 'endpoint_failure': row['endpoint_failure']})
                    break
                if outcome.preferred_endpoint_id != values['endpoint_id'] or outcome.preferred_model_id != values['model_id']:
                    raise RuntimeError('Endpoint/model drift')
                row['responses'].append(message_to_payload(outcome.response.message))
                messages.append(outcome.response.message)
                if not outcome.tool_calls:
                    row['final_text'] = outcome.response.message.text; break
                for call in outcome.tool_calls:
                    args = dict(call.args)
                    valid_outer = call.name in available and Draft202012Validator(
                        tool_definition_ir_from_dict(available[call.name]).input_schema).is_valid(args)
                    data = {'kind': 'rejected', 'error_code': 'invalid_arguments', 'effect': 'not_started'}
                    if call.name not in available or (call.name in DISCOVERY | {'call_tool'} and not valid_outer):
                        row['protocol_errors'].append({'call': call.name, 'args': args})
                    elif call.name in DISCOVERY:
                        if call.name == 'search_tools':
                            data = ExecutionRuntime._search_generation(view, args)
                            row['searches'].append({'args': args, 'hits': [h['alias'] for h in data['hits']]})
                        elif call.name == 'read_tool':
                            data = tool_contract(records[args['name']]) if args['name'] in records else {
                                'kind': 'rejected', 'error_code': 'unknown_tool'}
                        else:
                            data = {'tools': [tool_contract(r) for _, r in sorted(records.items())]}
                        exposed = [h['alias'] for h in data.get('hits', [])]
                        if call.name == 'read_tool':
                            exposed.append(args['name'])
                        for name in exposed:
                            if name in specs:
                                available[name] = specs[name]
                    else:
                        alias = args['name'] if call.name == 'call_tool' else call.name
                        inner = args.get('args', {}) if call.name == 'call_tool' else args
                        record = fx.runtime.registry_generation.record_for_alias(alias)
                        input_valid = record is not None and not isinstance(
                            fx.runtime._validate_invocation_input(record, inner), RejectedResult)
                        if alias in ALLOWED:
                            invoke = fx.runtime.invoke_indirect_tool_async if call.name == 'call_tool' else fx.runtime.invoke_model_tool_async
                            result = await invoke(new_tool_call(name=alias, args=inner), turn_id=f'memory-eval:{case.case_id}:{repetition}')
                            data = result.model_dump(mode='json')
                            for affordance in result.affordances:
                                if affordance.tool in specs:
                                    available[affordance.tool] = specs[affordance.tool]
                        else:
                            data = {'kind': 'rejected', 'error_code': 'fixture_scope_block', 'effect': 'not_started'}
                        row['business_calls'].append({'alias': alias, 'route': call.name, 'args': inner,
                                                      'arguments_valid': input_valid, 'result': data})
                    row['calls'].append({'name': call.name, 'args': args, 'result': data})
                    messages.append(_eval_tool_result_message(call, SimpleNamespace(
                        ok=data.get('kind') not in {'rejected', 'failed'}, status=data.get('kind', 'ok'),
                        structured=data, llm_text=json.dumps(data, ensure_ascii=False))))
            else:
                row['error'] = 'round_limit'
            row.update(grade(case, fx, row['business_calls'], row['final_text']))
            return row
        finally:
            fx.close()


def summarize(rows):
    return {split: {'completed': len(subset), **{key: sum(bool(r.get(key)) for r in subset) for key in (
        'decision_correct', 'tool_identified', 'arguments_valid', 'execution_accepted',
        'no_op_respected', 'expected_unavailability_observed', 'goal_verified',
        'constraints_preserved', 'business_success')},
        'tool_identification_cases': sum(r.get('expected_alias') is not None for r in subset),
        'no_op_cases': sum(r.get('expected_alias') is None for r in subset),
        'expected_unavailability_cases': sum(r.get('goal') == 'unknown_provider' for r in subset),
        'endpoint_or_harness_errors': sum(bool(r.get('error')) for r in subset),
        'protocol_error_runs': sum(bool(r.get('protocol_errors')) for r in subset),
        'argument_rejections': sum(c['result'].get('error_code') == 'invalid_arguments' for r in subset for c in r['business_calls']),
        'mode_rejections': sum(c['result'].get('error_code') == 'wrong_invocation_mode' for r in subset for c in r['business_calls'])}
        for split in ('all', 'development', 'held_out')
        if (subset := [r for r in rows if split == 'all' or r['split'] == split])}


async def main(args):
    args.output.mkdir(parents=True, exist_ok=False)
    values = endpoint_snapshot(args.runtime_root, args.endpoint)
    records, _, generations = build_flow_catalog()
    resident = generations['resident_union']
    catalog = {a: records[a] for a in resident.search_records}
    manifest = {'scope': __doc__, 'endpoint': values['endpoint_id'], 'model': values['model_id'],
                'temperature': 0, 'configured_think_level_request': 'medium', 'effective_think_level': 'medium' if 'medium' in values['thinking_levels_blob'] else values['default_thinking_level'], 'max_output_tokens': 8192,
                'repetitions': args.repetitions, 'concurrency': args.concurrency,
                'cases': [vars(case) for case in CASES], 'production_memory_mutations': False,
                'source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'implementation_sha256': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (
                    'src/pal/execution/tool_registry.py', 'src/pal/execution/runtime.py',
                    'src/pal/memory/capabilities.py', 'src/pal/execution/generated_tool_models.py',
                    'src/pal/core/tool_surface.py', 'src/pal/execution/tool_presentation.py')},
                'catalog_sha256': hashlib.sha256(json.dumps(catalog, sort_keys=True).encode()).hexdigest()}
    (args.output/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (args.output/'benchmark-source.py').write_text(Path(__file__).read_text())
    llm = build_llm(args.runtime_root, values)
    rows = []
    semaphore = asyncio.Semaphore(args.concurrency)
    async def one(case, repetition):
        async with semaphore:
            try:
                row = await run_case(llm, values, case, repetition, catalog, args.max_rounds)
            except Exception as exc:
                row = {'case_id': case.case_id, 'split': case.split, 'repetition': repetition,
                       'expected_alias': case.target, 'goal': case.goal,
                       'business_calls': [], 'error': f'{type(exc).__name__}: {exc}'}
            rows.append(row)
            (args.output/'runs.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            (args.output/'report.json').write_text(json.dumps(summarize(rows), ensure_ascii=False, indent=2))
            print(json.dumps({k: row.get(k) for k in ('case_id', 'repetition', 'decision_correct', 'business_success', 'error')}, ensure_ascii=False), flush=True)
    try:
        await asyncio.gather(*(one(case, repetition) for repetition in range(args.repetitions) for case in CASES))
    finally:
        llm.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--repetitions', type=int, default=3)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--max-rounds', type=int, default=8)
    asyncio.run(main(parser.parse_args()))
