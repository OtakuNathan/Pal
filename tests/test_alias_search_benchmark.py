from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from pal.llm.contracts import generation_result_from_values
from pal.shared.tool_protocol import new_tool_call


@pytest.fixture
def benchmark():
    path = Path(__file__).parents[1] / 'scripts' / 'benchmark_alias_search.py'
    spec = importlib.util.spec_from_file_location('alias_search_benchmark', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('invalid', [False, True])
def test_first_call_metric_validates_contract_without_business_execution(benchmark, tmp_path, invalid):
    fx = benchmark.fixture(tmp_path)
    calls = [new_tool_call(name='search_tools', args={'query': 'remember memory'}),
             new_tool_call(name='call_tool', args={'name': 'remember_memory', 'args':
                 {} if invalid else {'kind': 'fact', 'summary': 'Prefers concise Chinese answers.',
                                     'search_text': 'User preference concise Chinese answers'}})]
    class Model:
        async def agenerate(self, request):
            return generation_result_from_values(tool_calls=[calls.pop(0)],
                preferred_endpoint_id='fixture', preferred_model_id='glm-5.3',
                input_tokens=10, output_tokens=2, usage_reported=True)
    try:
        # The fake memory service cannot perform a write. Success is strictly
        # schema validity; invoking its handler would fail this test.
        row = asyncio.run(benchmark.run_case(Model(), {'endpoint_id': 'fixture', 'model_id': 'glm-5.3'},
                                           fx, benchmark.CASES[0], 1))
        assert row['top1'] and row['recall'] and row['selected']
        assert row['valid'] is (not invalid)
        assert len(row['usage']) == 2
    finally:
        fx.close()


def test_missing_search_is_counted_as_failure(benchmark, tmp_path):
    fx = benchmark.fixture(tmp_path)
    class Model:
        async def agenerate(self, request):
            return generation_result_from_values(text='Done', preferred_endpoint_id='fixture',
                preferred_model_id='glm-5.3', usage_reported=True)
    try:
        row = asyncio.run(benchmark.run_case(Model(), {'endpoint_id': 'fixture', 'model_id': 'glm-5.3'},
                                           fx, benchmark.CASES[0], 1))
        assert row['error'] == 'expected_one_search'
        assert benchmark.summarize([row])['first_search_top1'] == 0
    finally:
        fx.close()


def test_valid_call_and_search_recall_are_independent(benchmark, tmp_path):
    fx = benchmark.fixture(tmp_path)
    calls = [new_tool_call(name='search_tools', args={'query': 'remember memory'}),
             new_tool_call(name='recall_memory', args={'queries': ['answer style']})]
    class Model:
        async def agenerate(self, request):
            return generation_result_from_values(tool_calls=[calls.pop(0)],
                preferred_endpoint_id='fixture', preferred_model_id='glm-5.3', usage_reported=True)
    try:
        row = asyncio.run(benchmark.run_case(Model(), {'endpoint_id': 'fixture', 'model_id': 'glm-5.3'},
                                           fx, benchmark.CASES[1], 1))
        assert not row['top1'] and not row['recall']
        assert row['selected'] and row['valid']
    finally:
        fx.close()
