"""Pending batch: bounded failure inspection keeps complete evidence in owned files."""
import asyncio
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from pal.core import PalCore
from pal.execution import register_with_core as register_execution
from pal.failure.capabilities import register_with_core
from pal.failure.contracts import FailureReport
from pal.failure.runtime import FailureRuntime
from pal.shared import IntrospectionCall
from pal.shared.tool_protocol import new_tool_call


@pytest.fixture
def env(tmp_path):
    core = PalCore()
    register_execution(core.context)
    failure = FailureRuntime()
    handle = register_with_core(core, failure)
    for module in ('execution', 'failure'):
        core.publish_module_capabilities(module)
    execution = core.context.execution_runtime
    execution.configure_runtime_root(tmp_path)
    yield core, failure, handle.introspection_provider
    execution.shutdown()


def report(number, **changes):
    return FailureReport(**{
        'report_id': f'failure-{number}', 'subsystem': 'channel', 'component': 'main',
        'severity': 'medium', 'failure_kind': 'delivery_failure', 'why_blocked': 'network unavailable',
        'current_blocker': 'transport disconnected', 'impact': 'reply was not delivered',
        'attempted_actions': ['inspect endpoint', 'restart endpoint'],
        'evidence': {'body': f'EVIDENCE_ONLY_{number}', 'api_key': 'hidden-secret'},
        'documents_checked': ['channel documentation'], 'possible_solutions': ['restore transport'],
        'safe_to_retry': False, 'requires_developer_action': True,
        'recommended_next_step': 'reconcile delivery before retrying',
        'verification_status': 'failed', 'related_ids': {'endpoint_id': 'main'},
        **changes,
    })


def inspect(env, *, with_storage=True):
    core, _, provider = env
    meta = {'execution_runtime': core.context.execution_runtime, 'turn_id': 'failure-turn',
            'tool_call': new_tool_call(name='inspect_failure', args={})} if with_storage else {}
    return provider.show(IntrospectionCall(name='inspect_failure', meta=meta))


def test_fixed_recent_window_includes_counts_and_complete_readonly_detail_file(env):
    core, failure, _ = env
    failure.recent_reports = [report(index) for index in range(20)]
    failure.recent_failures = [{}, {}, {}]
    result = inspect(env)
    payload = result.structured
    assert payload['recent_report_count'] == 20 and payload['recent_failure_count'] == 3
    assert payload['returned_report_count'] == payload['report_limit'] == 16
    assert payload['omitted_report_count'] == 4
    assert payload['latest_report_id'] == 'failure-19'
    assert [item['report_id'] for item in payload['recent_diagnostics']] == [f'failure-{index}' for index in range(4, 20)]
    assert 'items' not in payload and 'EVIDENCE_ONLY' not in result.llm_text
    assert 'hidden-secret' not in result.llm_text
    path = Path(payload['reports_file']['file_path'])
    assert path.is_relative_to(core.context.execution_runtime.result_snapshots.root)
    assert path.stat().st_mode & 0o222 == 0
    saved = json.loads(path.read_text())['reports']
    assert [item['report_id'] for item in saved] == [f'failure-{index}' for index in range(4, 20)]
    assert saved[-1]['evidence']['body'] == 'EVIDENCE_ONLY_19'
    assert saved[-1]['documents_checked'] == ['channel documentation']
    assert saved[-1]['related_ids'] == {'endpoint_id': 'main'}
    assert 'hidden-secret' not in path.read_text()
    assert failure.recent_reports[-1].evidence['api_key'] == 'hidden-secret'
    assert len(result.snapshot_refs) == 1 and result.snapshot_refs[0].path == str(path)
    assert not payload['diagnostics']['partial']


def test_empty_tracking_creates_no_file_and_does_not_claim_health(env, monkeypatch):
    core, _, _ = env
    capture = Mock(side_effect=AssertionError('empty report list wrote a snapshot'))
    monkeypatch.setattr(core.context.execution_runtime.result_snapshots, 'capture', capture)
    result = inspect(env)
    assert result.structured['recent_diagnostics'] == []
    assert result.structured['recent_report_count'] == result.structured['returned_report_count'] == 0
    assert result.structured['reports_file'] == {'status': 'not_needed', 'reason': 'no_recent_reports'}
    assert 'healthy' not in result.structured
    assert not result.structured['diagnostics']['partial']
    assert result.snapshot_refs == ()
    capture.assert_not_called()


def test_long_report_is_summarized_without_losing_file_evidence_or_retry_semantics(env):
    _, failure, _ = env
    long_text = 'cause ' * 1000
    actions = ['action ' * 100 for _ in range(10)]
    failure.recent_reports = [report(1, why_blocked=long_text, current_blocker=long_text,
        impact=long_text, recommended_next_step=long_text, attempted_actions=actions,
        evidence={'body': 'FULL_EVIDENCE_ONLY ' * 10000})]
    result = inspect(env)
    item, = result.structured['recent_diagnostics']
    assert len(item['why_blocked']) <= 240 and len(item['impact']) <= 160
    assert len(item['attempted_actions']) == 4 and all(len(value) <= 120 for value in item['attempted_actions'])
    assert set(item['truncated_fields']) == {'why_blocked', 'current_blocker', 'impact', 'recommended_next_step', 'attempted_actions'}
    assert item['safe_to_retry'] is False and item['verification_status'] == 'failed'
    assert item['requires_developer_action'] is True
    assert 'FULL_EVIDENCE_ONLY' not in result.llm_text and len(result.llm_text) < 5000
    saved, = json.loads(Path(result.structured['reports_file']['file_path']).read_text())['reports']
    assert saved['why_blocked'] == long_text and saved['attempted_actions'] == actions
    assert saved['evidence'] == failure.recent_reports[0].evidence


@pytest.mark.parametrize('with_storage', [False, True])
def test_storage_failure_keeps_summaries_without_raw_fallback_or_a_fictitious_path(env, monkeypatch, with_storage):
    core, failure, _ = env
    original = report(1)
    failure.recent_reports = [original]
    monkeypatch.setattr(core.context.execution_runtime.result_snapshots, 'capture',
                        Mock(side_effect=OSError('disk full token=hidden-secret ' + 'x' * 1000)))
    result = inspect(env, with_storage=with_storage)
    assert result.status == 'ok'
    assert result.structured['recent_diagnostics'][0]['current_blocker'] == original.current_blocker
    assert result.structured['reports_file']['status'] == 'unavailable'
    assert 'file_path' not in result.structured['reports_file']
    assert len(result.structured['reports_file']['error']) <= 400
    assert result.structured['diagnostics']['partial'] is True
    assert 'reports_file' in result.structured['diagnostics']['unavailable_sections']
    assert 'EVIDENCE_ONLY' not in result.llm_text and 'hidden-secret' not in result.llm_text
    assert result.snapshot_refs == ()
    assert failure.recent_reports == [original]


def test_runtime_preserves_the_snapshot_reference_and_its_readable_file(env):
    core, failure, _ = env
    failure.recent_reports = [report(1)]
    execution = core.context.execution_runtime
    result = asyncio.run(execution.execute_tool_async(new_tool_call(name='call_tool', args={
        'name': 'inspect_failure', 'args': {}}), turn_id='failure-turn'))
    assert result.ok, result.llm_text
    path = result.structured['reports_file']['file_path']
    assert any(ref.path == path for ref in result.snapshot_refs)
    read = asyncio.run(execution.execute_tool_async(new_tool_call(name='read_file', args={
        'file_path': path}), turn_id='failure-turn'))
    assert read.ok, read.llm_text
    assert 'EVIDENCE_ONLY_1' in read.llm_text and 'hidden-secret' not in read.llm_text


def test_one_indirect_parameterless_tool_replaces_both_old_entries(env, monkeypatch):
    core, failure, _ = env
    execution = core.context.execution_runtime
    generation = execution.registry_generation
    names = {name for name, record in generation.search_records.items() if record['module_id'] == 'failure'}
    assert names == {'inspect_failure'} and not names.intersection(generation.direct_aliases)
    contract = generation.search_records['inspect_failure']['input_contract']
    assert not contract.get('properties') and not contract.get('required')
    assert contract['additionalProperties'] is False
    for alias in ('inspect_failure_state', 'list_failure_reports'):
        assert generation.record_for_alias(alias) is None
        assert not execution.execute_tool(new_tool_call(name='read_tool', args={'name': alias})).ok
        assert not execution.execute_tool(new_tool_call(name='call_tool', args={'name': alias, 'args': {}})).ok
        search = execution._search_generation(generation, {'query': alias})
        assert alias not in {hit['alias'] for hit in search['hits']}
    dispatch = Mock(side_effect=AssertionError('invalid parameters reached owner'))
    monkeypatch.setattr(failure, 'show_summary', dispatch)
    for args in ({'limit': 1}, {'view': 'summary'}, {'report_id': 'failure-1'}):
        assert execution.invoke_indirect_tool(new_tool_call(name='inspect_failure', args=args)).kind == 'rejected'
    dispatch.assert_not_called()
