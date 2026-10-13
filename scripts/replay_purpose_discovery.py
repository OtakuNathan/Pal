"""Replay frozen model queries; measures retrieval, not new model behavior.

Baseline source is a trusted local runtime.py snapshot. No business/model calls.
The second attempt was generated with old results and an evaluator retry hint;
its recall is a fixed-query union, not autonomous recovery under the new matcher.
"""
from __future__ import annotations

import argparse
import ast
import json
import random
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pal.core import PalCore  # Initialize runtime dependencies before importing runtime.
from pal.execution import runtime
from scripts.evaluate_tool_discovery import evaluate


def load_matcher(path, terms_source=None):
    tree = ast.parse(path.read_text())
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'ExecutionRuntime')
    method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_search_generation')
    method.decorator_list = []
    namespace = dict(vars(runtime))
    if terms_source is not None:
        helpers = {}
        exec(compile(terms_source.read_text(), str(terms_source), "exec"), helpers)
        namespace.update({key: value for key, value in helpers.items() if not key.startswith("__")})
    exec(compile(ast.Module(body=[method], type_ignores=[]), str(path), 'exec'), namespace)
    return namespace[method.name]


def replay(search, rows, generations):
    details = []
    for row in rows:
        hits = [[h['alias'] for h in search(generations[row['group']], a['query'])['hits']]
                for a in row['attempts']]
        details.append(dict(alias=row['alias'], group=row['group'], hits=hits,
            first=bool(hits and row['alias'] in hits[0]),
            top1=bool(hits and hits[0][:1] == [row['alias']]),
            either=any(row['alias'] in h for h in hits)))
    return dict(first=sum(r['first'] for r in details), top1=sum(r['top1'] for r in details),
                either=sum(r['either'] for r in details), details=details,
                challenge=evaluate(search, generations['resident_union']))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline-source', type=Path, required=True)
    parser.add_argument('--baseline-terms-source', type=Path)
    parser.add_argument('--current-catalog', action='store_true')
    parser.add_argument('--input', type=Path, default=Path('test-logs/discovery-full-model'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    records = json.loads((args.input / 'catalog.json').read_text())
    rows = json.loads((args.input / 'runs.json').read_text())
    generations = {group: SimpleNamespace(search_records={r['alias']: records[r['alias']]
                   for r in rows if r['group'] == group})
                   for group in ('resident_union', 'role_union')}
    before_generations = generations
    if args.current_catalog:
        from scripts.benchmark_full_discovery import build_catalog
        _, records, _, generations = build_catalog()
    search = runtime.ExecutionRuntime._search_generation
    report = dict(method='Frozen purpose-derived model queries; no new model calls; assisted second-query union.',
                  before=replay(load_matcher(args.baseline_source, args.baseline_terms_source), rows, before_generations),
                  after=replay(search, rows, generations))
    report['regressed_first'] = [a['alias'] for a, b in zip(report['before']['details'], report['after']['details'])
                                if a['first'] and not b['first']]
    rng = random.Random(20261012)
    checks = violations = exact = 0
    for group, generation in generations.items():
        exact += sum(search(generation, {'query': alias})['hits'][0]['alias'] == alias
                     for alias in generation.search_records)
        for row in (r for r in rows if r['group'] == group):
            for attempt in row['attempts']:
                query = attempt['query']
                expected = search(generation, query)
                domain = expected['applied_filters'].get('domain')
                violations += sum(records[h['alias']]['domain'] != domain
                                  for h in expected['hits'] + expected.get('suggestions', []) if domain)
                items = list(generation.search_records.items())
                rng.shuffle(items)
                assert search(SimpleNamespace(search_records=dict(items)), query) == expected
                checks += 1
    report['checks'] = dict(registry_permutations=checks, exact_alias_top1=exact, domain_violations=violations)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    for name in ('before', 'after'):
        print(name, {k: v for k, v in report[name].items() if k not in {'details', 'challenge'}})
        print(report[name]['challenge']['metrics'])
    print(report['checks'], 'regressions:', report['regressed_first'])


if __name__ == '__main__':
    main()
