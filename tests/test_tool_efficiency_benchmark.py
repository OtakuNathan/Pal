from __future__ import annotations

import asyncio
from pathlib import Path

from pal.core.runtime import PalCore
from pal.eval_tools import EvalCase, _run_case
from pal.llm.contracts import generation_result_from_values
from pal.shared.tool_protocol import new_tool_call
from pal.tool_efficiency_benchmark import FixtureExecution, MANIFEST, summarize
from pal.eval_tools import load_tools_benchmark
from types import SimpleNamespace


def test_fixture_isolates_shell_and_validates_real_lsp_chain(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fixture = FixtureExecution(tmp_path, 'lsp-ready')
    async def run():
        result = await fixture.execute_tool_async(new_tool_call(name='run_shell',args={'cmd':'touch should-not-exist'}),turn_id='t')
        assert not result.ok and not (tmp_path/'should-not-exist').exists()
        for name,args in [('lsp_prepare_workspace',{'workspace_root':'.'}),('call_tool',{'name':'lsp_diagnostics','args':{'file':'sample.py','workspace_root':'.'}})]:
            result = await fixture.execute_tool_async(new_tool_call(name=name,args=args),turn_id='t')
            assert result.ok, result.llm_text
        assert result.structured['diagnostics'] == []
    try:
        asyncio.run(run())
    finally:
        fixture.close()


def test_fixture_read_cannot_escape_and_page_contains_exact_tail(tmp_path):
    fixture = FixtureExecution(tmp_path, 'page-evidence')
    async def run():
        result = await fixture.execute_tool_async(new_tool_call(name='read_file',args={'file_path':'/etc/passwd'}),turn_id='t')
        assert not result.ok
        result = await fixture.execute_tool_async(new_tool_call(name='read_tool_result',args={'result_ref':'fixture-long','anchor':'tail','page':1}),turn_id='t')
        assert result.ok, result.llm_text
        assert result.structured['page_text'].endswith('FINAL_MARKER\n \t')
        assert result.invocation_result.llm_text.endswith('FINAL_MARKER\n \t')
    try:
        asyncio.run(run())
    finally:
        fixture.close()


def test_eval_does_not_accept_failed_target_and_requires_complete_chain(tmp_path):
    class LLM:
        async def agenerate(self, _request):
            result = generation_result_from_values(tool_calls=[new_tool_call(name='call_tool',args={'name':'lsp_diagnostics','args':{'file':'sample.py'}})])
            return SimpleNamespace(response=result.response,tool_calls=result.tool_calls,preferred_endpoint_id='test',preferred_model_id='test')
    fixture = FixtureExecution(tmp_path,'lsp-ready')
    try:
        result = asyncio.run(_run_case(case=EvalCase(case_id='test',category='discovery',prompt='diagnose',expected_alias='lsp_diagnostics',expected_first_alias='lsp_prepare_workspace',max_rounds=1,required_aliases=('lsp_prepare_workspace','lsp_diagnostics')),
            repetition=1,model='test',temperature=0,reasoning='medium',tool_contracts=[],llm_runtime=LLM(),execution_runtime=fixture))
        assert not result['eventual_correct']
        assert not result['chain_complete']
        assert not summarize([result])['usage_complete']
    finally:
        fixture.close()


def test_v2_manifest_and_usage_count_reasoning_once():
    manifest,cases = load_tools_benchmark(MANIFEST)
    assert len(cases) == 13 and manifest['repetitions'] == 3
    assert manifest['max_output_tokens'] == 8192
    assert all('module_id=' not in c.prompt for c in cases)
    summary = summarize([{'eventual_correct':True,'calls':[{'provider_alias':'read_tool'}],
        'usage':[{'input_tokens':100,'output_tokens':30,'reasoning_tokens':20,'cached_input_tokens':40,'reported':True}]}])
    assert summary['total_tokens'] == 130
    assert summary['discovery_tokens'] == 130


def test_fixture_allows_real_read_file_contract(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / 'pyproject.toml').write_text('[project]\nname = "fixture"\n')
    fixture = FixtureExecution(tmp_path, 'select-file-read')
    try:
        result = asyncio.run(fixture.execute_tool_async(new_tool_call(name='read_file',args={'file_path':'pyproject.toml'}),turn_id='t'))
        assert result.ok, result.llm_text
        assert 'fixture' in result.llm_text
        assert fixture.unsafe_attempts == 0
    finally:
        fixture.close()


def test_forbidden_navigation_cannot_pass_by_later_checking_status(tmp_path):
    class LLM:
        async def agenerate(self, _request):
            result = generation_result_from_values(tool_calls=[
                new_tool_call(name='call_tool',args={'name':'lsp_diagnostics','args':{'file':'sample.py'}}),
                new_tool_call(name='call_tool',args={'name':'lsp_status','args':{}}),
            ])
            return SimpleNamespace(response=result.response,tool_calls=result.tool_calls)
    fixture = FixtureExecution(tmp_path,'lsp-partial')
    try:
        result = asyncio.run(_run_case(case=EvalCase(case_id='partial',category='discovery',prompt='check readiness',expected_alias='lsp_status',expected_first_alias='lsp_status',max_rounds=1,forbidden_aliases=('lsp_diagnostics',)),
            repetition=1,model='test',temperature=0,reasoning='medium',tool_contracts=[],llm_runtime=LLM(),execution_runtime=fixture))
        assert result['dangerous_retry']
        assert not result['eventual_correct']
        assert summarize([result])['forbidden_or_dangerous_runs'] == 1
    finally:
        fixture.close()
