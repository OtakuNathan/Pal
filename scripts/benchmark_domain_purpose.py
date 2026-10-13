"""Compare domain-purpose retrieval with a frozen matcher on the same catalog.

Replays earlier model queries, then runs fixed Chinese cases through a real
model. Only discovery and evaluation-only alias/mode selection execute.
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
from scripts.benchmark_discovery_flow import prompt_messages
from scripts.benchmark_full_discovery_flow import build_flow_catalog, manifest_prompt, run_case, summarize
from scripts.replay_purpose_discovery import load_matcher
from pal.execution.runtime import ExecutionRuntime
from pal.tool_efficiency_benchmark import build_llm, endpoint_snapshot


def candidates(result):
    return [row['alias'] for row in result['hits'] + result.get('suggestions', [])]


def replay(searches, generations, cases):
    rows = []
    rng = random.Random(20261013)
    for case in cases:
        for attempt in case.get('searches', []):
            args = {**attempt['args'], 'top_k': 3}
            generation = generations[case['group']]
            outputs = {variant: search(generation, args) for variant, search in searches.items()}
            rows.append({'case_id': case['case_id'], 'alias': case['alias'], 'args': args,
                         **{variant: candidates(output) for variant, output in outputs.items()}})
            shuffled = list(generation.search_records.items())
            rng.shuffle(shuffled)
            check = searches['current'](SimpleNamespace(search_records=dict(shuffled)), args)
            assert check == outputs['current'], 'Registry ordering changed results'
            domain = check['applied_filters'].get('domain')
            if domain:
                assert all(generation.search_records[alias]['domain'] == domain for alias in candidates(check))
    summary = {variant: {'positive_queries': sum(row['alias'] is not None for row in rows),
                        'expected_in_top3': sum(row['alias'] in row[variant] for row in rows if row['alias']),
                        'negative_queries': sum(row['alias'] is None for row in rows),
                        'negative_with_candidates': sum(bool(row[variant]) for row in rows if row['alias'] is None)}
               for variant in searches}
    return {'scope': 'Frozen searches, not fresh model success; same catalog and top_k=3',
            'deterministic_and_domain_checks': len(rows), 'summary': summary, 'rows': rows}


async def main(args):
    from pal.execution.generated_tool_models import ExecutionToolSearchExecutionDiscoveryCapabilityMixinSearchInput as SearchInput
    records, _, generations = build_flow_catalog()
    before = load_matcher(args.baseline / 'src/pal/execution/runtime.py',
                          args.baseline / 'src/pal/execution/discovery_terms.py')
    searches = {'before': before, 'current': ExecutionRuntime._search_generation}
    args.output.mkdir(parents=True, exist_ok=True)
    earlier = json.loads((args.prior / 'runs.json').read_text())
    frozen = replay(searches, generations, [row for row in earlier if row['variant'] == 'current'])
    (args.output / 'replay.json').write_text(json.dumps(frozen, ensure_ascii=False, indent=2))
    print(json.dumps(frozen['summary']), flush=True)
    if args.replay_only:
        return
    if args.variant != 'paired':
        searches = {args.variant: searches[args.variant]}
    cases = json.loads((args.prior / 'manifest.json').read_text())['cases']
    if args.distinct:
        cases = list({(case['case_id'], case['task']): case for case in reversed(cases)}.values())
    if args.case_id:
        cases = [case for case in cases if case['case_id'] in args.case_id]
    schema = SearchInput.model_json_schema()
    for key in ('namespace', 'family', 'module_name', 'tags', 'facets'):
        schema['properties'].pop(key, None)
    # Both variants get the new task-description prompt, isolating the matcher.
    prompt = prompt_messages('candidate', '')[:-1]
    values = endpoint_snapshot(args.runtime_root, args.endpoint)
    paths = [Path(__file__), Path('src/pal/execution/domain_search.py'), Path('src/pal/execution/runtime.py'),
             Path('src/pal/execution/discovery_terms.py'), Path('src/pal/shared/tool_routing.py'),
             Path('scripts/benchmark_full_discovery_flow.py')]
    manifest = {'scope': __doc__, 'endpoint': values['endpoint_id'], 'model': values['model_id'],
                'cases': cases, 'schema': schema, 'prompt': manifest_prompt(prompt),
                'variants': list(searches), 'max_output_tokens': args.max_output_tokens,
                'catalog_sha256': hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest(),
                'source_sha256': {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}}
    if args.resume:
        assert json.loads((args.output / 'manifest.json').read_text()) == json.loads(json.dumps(manifest)), 'Trial changed'
        rows = json.loads((args.output / 'runs.json').read_text())
    else:
        assert not (args.output / 'manifest.json').exists(), 'Use another output or --resume'
        (args.output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
        rows = []
    (args.output / 'catalog.json').write_text(json.dumps(records, ensure_ascii=False, indent=2))
    llm = build_llm(args.runtime_root, values)
    semaphore = asyncio.Semaphore(args.concurrency)
    consecutive_failures = 0
    stopped = False

    def save():
        (args.output / 'runs.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2))
        report = {'stopped': stopped, 'metrics': {variant: summarize(
            [row for row in rows if row['variant'] == variant], cases) for variant in searches}}
        (args.output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2))

    async def one(variant, case):
        nonlocal consecutive_failures, stopped
        if any(row['variant'] == variant and row['case_id'] == case['case_id']
               and row['repetition'] == case['repetition'] for row in rows):
            return
        async with semaphore:
            if stopped:
                return
            try:
                row = await run_case(llm, values, case, generations[case['group']], searches[variant],
                                     schema, prompt, max_output_tokens=args.max_output_tokens)
            except Exception as exc:
                row = {**case, 'correct': False, 'error': type(exc).__name__}
            consecutive_failures = consecutive_failures + 1 if row.get('error') else 0
            if consecutive_failures >= 3:
                stopped = True
            row['variant'] = variant
            rows.append(row)
            save()
            print(json.dumps({'completed': len(rows), 'variant': variant, 'case': case['case_id'],
                              'correct': row.get('correct'), 'error': row.get('error')}), flush=True)

    try:
        pending = [(variant, case) for case in cases for variant in searches]
        random.Random(20261013).shuffle(pending)
        await asyncio.gather(*(one(variant, case) for variant, case in pending))
    finally:
        save()
        llm.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline', type=Path, required=True)
    parser.add_argument('--prior', type=Path, default=Path('test-logs/discovery-fused-glm53'))
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--runtime-root', type=Path, default=Path.home() / '.pal')
    parser.add_argument('--endpoint', default='glm-5.3')
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--max-output-tokens', type=int, default=8192)
    parser.add_argument('--variant', choices=('before', 'current', 'paired'), default='paired')
    parser.add_argument('--case-id', action='append')
    parser.add_argument('--distinct', action='store_true', help='Test each fixed task once, omitting repeated tasks.')
    parser.add_argument('--replay-only', action='store_true')
    parser.add_argument('--resume', action='store_true')
    asyncio.run(main(parser.parse_args()))
