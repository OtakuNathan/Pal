"""Whole authored catalog recall with independently generated real-model queries.

Contract-derived English tasks, not free-form user traffic. Static resident union
and separate role-tool union; neither claims live availability. No business calls.
Provider duplicate declarations are preserved in the manifest but one version per
alias is searched. Dynamic MCP and runtime-installed external plugins are excluded.
"""
from __future__ import annotations
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from collections import Counter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.audit_tool_discovery import declared_tools
from scripts.benchmark_discovery_flow import prompt_messages
from pal.tool_efficiency_benchmark import endpoint_snapshot, build_llm


def build_catalog():
    declarations = declared_tools()
    from pal.execution.discovery_terms import tool_domain
    records, cases = {}, []
    # The audit is sorted; keep the first provider (sqlite_vec before stubs).
    for tool in declarations:
        if tool.alias in records:
            continue
        records[tool.alias] = dict(alias=tool.alias, purpose=tool.guidance.purpose,
            use_when=tool.guidance.use_when, do_not_use_when=tool.guidance.do_not_use_when,
            search_terms=tool.vocabulary, search_objects=tool.guidance.search_objects,
            search_auxiliary_terms=tuple(sorted((set(tool.vocabulary) - set(tool.guidance.search_objects))
                                               | set(tool.guidance.search_terms))),
            search_term_rules=[r.model_dump() for r in tool.guidance.search_term_rules],
            search_scope_terms=tool.guidance.search_scope_terms,
            search_intents=tool.guidance.search_intents,
            domain=tool_domain(tool.alias, tool.module_id), module_id=tool.module_id,
            namespace=tool.namespace, family=tool.family, tags=[])
        cases.append(dict(alias=tool.alias, source=tool.source,
                          group='role_union' if tool.role_scoped else 'resident_union',
                          domain=records[tool.alias]['domain'], purpose=tool.guidance.purpose))
    pattern = re.compile(r'\b(?:' + '|'.join(re.escape(a) for a in sorted(records, key=len, reverse=True)) + r')\b')
    for case in cases:
        case['task'] = 'Find the tool for this task: ' + pattern.sub('[a tool]', case['purpose'])
    generations = {group: SimpleNamespace(search_records={c['alias']: records[c['alias']]
                   for c in cases if c['group'] == group})
                   for group in ('resident_union', 'role_union')}
    return declarations, records, cases, generations


def summarize(rows, cases):
    by_group = {}
    for group in ('all', 'resident_union', 'role_union'):
        selected = [r for r in rows if group == 'all' or r['group'] == group]
        by_group[group] = dict(completed=len(selected),
            expected=sum(group == 'all' or c['group'] == group for c in cases),
            errors=sum(bool(r.get('error')) for r in selected),
            first_top1=sum(r.get('first_top1', False) for r in selected),
            first_recall=sum(r.get('first_recall', False) for r in selected),
            eventual_strict_recall=sum(r.get('strict_recall', False) for r in selected),
            eventual_fuzzy_only=sum(not r.get('strict_recall') and r.get('fuzzy_recall', False) for r in selected),
            first_domain_mismatch=sum(r.get('first_domain_mismatch', False) for r in selected),
            exact_alias_recall=sum(r['exact_alias_recall'] for r in selected))
    return by_group


def diagnose(rows, records, generations):
    """Explain fixed model queries without additional model calls or retuning."""
    from pal.execution.runtime import ExecutionRuntime
    from pal.execution.discovery_terms import normalize_domain, query_vocabulary, tool_search_terms
    counts = {'first_misses': Counter(), 'final_misses': Counter()}
    missing_words = Counter()
    details = []
    boundary_violations = []
    for row in rows:
        alias = row['alias']
        record = records[alias]
        generation = generations[row['group']]
        attempts = row.get('attempts', [])
        for attempt in attempts:
            domain = normalize_domain(str(attempt['query'].get('domain') or ''))
            if domain:
                for hit in attempt['hits'] + attempt['suggestions']:
                    if records[hit]['domain'] != domain:
                        boundary_violations.append({'expected': alias, 'returned': hit, 'domain': domain})
        for stage, attempt in [('first_misses', attempts[0] if attempts else None),
                               ('final_misses', attempts[-1] if attempts else None)]:
            if row.get('first_recall' if stage == 'first_misses' else 'strict_recall'):
                continue
            detail = {'alias': alias, 'group': row['group'], 'stage': stage}
            if attempt is None:
                cause = 'protocol_or_endpoint_error'
            else:
                args = attempt['query']
                terms = set(tool_search_terms(str(args.get('query') or '')))
                missing = terms - set(tool_search_terms(alias)) - query_vocabulary(record, terms)
                domain = normalize_domain(str(args.get('domain') or ''))
                broad = ExecutionRuntime._search_generation(generation, {**args, 'top_k': 5})
                unfiltered = ExecutionRuntime._search_generation(generation, {**args, 'domain': ''})
                if domain and domain != record['domain']:
                    cause = 'wrong_domain'
                elif missing:
                    cause = 'unmatched_query_words'
                elif any(h['alias'] == alias for h in broad['hits']):
                    cause = 'ranking_or_limit'
                else:
                    cause = 'other'
                detail.update(query=args, hits=attempt['hits'], missing_terms=sorted(missing),
                    expected_domain=record['domain'],
                    domain_removal_recovers=any(h['alias'] == alias for h in unfiltered['hits']),
                    larger_limit_recovers=any(h['alias'] == alias for h in broad['hits']))
                if stage == 'first_misses':
                    missing_words.update(missing)
            detail['cause'] = cause
            counts[stage][cause] += 1
            details.append(detail)
    return dict(counts={k: dict(v) for k, v in counts.items()},
                first_miss_words=missing_words.most_common(),
                domain_boundary_violations=boundary_violations, details=details)


async def main(args):
    from pal.execution.runtime import ExecutionRuntime
    from pal.execution.discovery_terms import normalize_domain
    from pal.execution.generated_tool_models import ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput as SearchInput
    from pal.llm.conversions import message_ir_from_dict, tool_definition_ir_from_dict
    from pal.llm.ir import GenerationPolicyIR, LLMRequestIR
    from pal.llm.serde import message_to_payload
    from pal.eval_tools import _eval_tool_result_message
    declarations, records, cases, generations = build_catalog()
    # Namespaces/families are copied where explicitly declared. Inheritance and
    # role visibility are not reconstructed; disallow those filters in this sweep.
    schema = SearchInput.model_json_schema()
    for key in ('namespace', 'family', 'module_name', 'tags', 'facets'):
        schema['properties'].pop(key, None)
    definition = tool_definition_ir_from_dict({'type': 'function', 'function': {
        'name': 'search_tools', 'description': 'Find tools. Use domain for a known operation owner and query for short action/object words. Exact aliases and complete structured matches rank first; positive purpose text supplies additional relevance. Separate fuzzy suggestions appear on misses.',
        'parameters': schema}})
    values = endpoint_snapshot(args.runtime_root, args.endpoint)
    llm = build_llm(args.runtime_root, values)
    args.output.mkdir(parents=True, exist_ok=args.resume)
    fingerprint = hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()
    manifest = dict(scope=__doc__, model=values['model_id'], endpoint=values['endpoint_id'],
        max_output_tokens=args.max_output_tokens, catalog_sha256=fingerprint, declarations=len(declarations), aliases=len(records),
        duplicate_declarations=[{'alias': d.alias, 'source': d.source} for d in declarations], cases=cases,
        prompt=[message_to_payload(m) for m in prompt_messages('candidate', '')[:-1]])
    if args.resume:
        old = json.loads((args.output/'manifest.json').read_text())
        if old['catalog_sha256'] != fingerprint or old['model'] != manifest['model'] or old['endpoint'] != manifest['endpoint'] or old['prompt'] != manifest['prompt'] or old.get('max_output_tokens', 1024) != args.max_output_tokens:
            raise ValueError('Cannot resume with a changed catalog, endpoint, model or prompt')
    else:
        (args.output/'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    (args.output/'catalog.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
    rows = json.loads((args.output/'runs.json').read_text()) if args.resume else []
    semaphore = asyncio.Semaphore(args.concurrency)
    failures = 0
    stopped = False

    async def one(case):
        nonlocal failures, stopped
        if any(r['alias'] == case['alias'] for r in rows):
            return
        async with semaphore:
            if stopped:
                return
            alias, generation = case['alias'], generations[case['group']]
            exact = ExecutionRuntime._search_generation(generation, {'query': alias})
            row = dict(**case, attempts=[], usage=[], responses=[], exact_alias_recall=exact['hits'][0]['alias'] == alias)
            messages = prompt_messages('candidate', case['task'])
            messages.insert(-1, message_ir_from_dict({'role': 'system', 'content':
                'This evaluation measures search recall only. Issue exactly one search_tools call per response, '
                'without top_k or limit. Do not guess invocation arguments, execute a business tool, or ask '
                'for task-specific IDs. Select search words from the task description. Tool availability is '
                'a static catalog for this case. At most two attempts are allowed.'}))
            try:
                for attempt in range(2):
                    outcome = await asyncio.wait_for(llm.agenerate(LLMRequestIR(
                        messages=tuple(messages), tools=(definition,), model_hint=values['model_id'],
                        policy=GenerationPolicyIR(max_output_tokens=args.max_output_tokens, temperature=0),
                        metadata={'preferred_endpoint_id': values['endpoint_id'], 'endpoint_fallback_policy': 'none',
                                  'think_level': 'medium', 'case_id': alias})), timeout=150)
                    if outcome.finish_reason.value == 'error':
                        metadata = outcome.response.message.metadata
                        row['error'] = 'endpoint_failure'
                        row['endpoint_failure'] = {key: metadata.get(key) for key in
                                                   ('failure_kind', 'error_type', 'failure_subsystem')}
                        break
                    if outcome.preferred_endpoint_id != values['endpoint_id'] or outcome.preferred_model_id != values['model_id']:
                        raise RuntimeError('Endpoint/model drift')
                    row['responses'].append(message_to_payload(outcome.response.message))
                    row['usage'].append({k: getattr(outcome.response.usage, k, 0)
                                         for k in ('input_tokens', 'output_tokens', 'reported')})
                    calls = list(outcome.tool_calls or ())
                    if len(calls) != 1 or calls[0].name != 'search_tools':
                        row['error'] = 'expected_one_search'
                        break
                    call = calls[0]
                    query = dict(call.args)
                    SearchInput.model_validate(query, strict=True)
                    if set(query) - set(schema['properties']) or any(query.get(k) is not None for k in ('top_k', 'limit')):
                        row['error'] = 'unsupported_filter_or_limit'
                        break
                    result = ExecutionRuntime._search_generation(generation, query)
                    hits = [h['alias'] for h in result['hits']]
                    suggestions = [s['alias'] for s in result.get('suggestions', [])]
                    row['attempts'].append(dict(query=query, hits=hits, suggestions=suggestions,
                                               applied_filters=result['applied_filters']))
                    if attempt == 0:
                        row.update(first_recall=alias in hits, first_top1=hits[:1] == [alias],
                            first_domain_mismatch=bool(query.get('domain')) and normalize_domain(query['domain']) != case['domain'])
                    row['strict_recall'] = row.get('strict_recall', False) or alias in hits
                    row['fuzzy_recall'] = row.get('fuzzy_recall', False) or alias in suggestions
                    if row['strict_recall']:
                        break
                    messages.append(outcome.response.message)
                    messages.append(_eval_tool_result_message(call, SimpleNamespace(
                        ok=True, status='ok', structured=result, llm_text=json.dumps(result, ensure_ascii=False))))
                    messages.append(message_ir_from_dict({'role':'user', 'content':
                        'Try one alternative search for the same original task. You may shorten the query or revise the domain. Do not set top_k or limit.'}))
                failures = 0
            except Exception as exc:
                row['error'] = type(exc).__name__  # Never persist credentials/provider error bodies.
                if str(exc) in {'Endpoint/model drift', 'LLM stream completed without semantic output'}:
                    row['error_detail'] = str(exc)
                failures += 1
                if failures >= 3:
                    stopped = True
            rows.append(row)
            (args.output/'runs.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
            report = dict(model=values['model_id'], catalog_sha256=fingerprint,
                          stopped=stopped, metrics=summarize(rows, cases))
            (args.output/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))
            print(json.dumps({'completed':len(rows), 'alias':alias, 'first':row.get('first_recall'),
                              'eventual':row.get('strict_recall'), 'error':row.get('error')}, ensure_ascii=False), flush=True)
    try:
        await one(cases[0])
        if not rows or rows[-1].get('error'):
            return
        await asyncio.gather(*(one(c) for c in cases[1:]))
        (args.output/'diagnostics.json').write_text(json.dumps(
            diagnose(rows, records, generations), ensure_ascii=False, indent=2))
    finally:
        llm.close()


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-root', type=Path, required=True)
    parser.add_argument('--endpoint')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--concurrency', type=int, default=6)
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--max-output-tokens', type=int, default=1024)
    asyncio.run(main(parser.parse_args()))
