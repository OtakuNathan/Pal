"""Verifier feedback is checked at the real scoped facade/provider boundary."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime
from pal.bunshin.v2.role_contracts import OrchestrationRole, RoleActivation, RoleMode
from pal.bunshin.v2.semantic_orchestration.role_policy import apply_v2_role_capability_policy
from pal.bunshin.v2.swe_verification import semantic_verification_submission_errors
from pal.bunshin.v2.verification_builder import compile_verification_invocation_tool_contract
from pal.bunshin.v2.work_items import update_checklist_tool_result
from pal.core.turn_executor import TurnExecutor
from pal.llm.ir import GenerationPolicyIR, LLMMessageIR, LLMRequestIR, MessageRole, WireShape
from pal.llm.shapes.base import ShapeContext
from pal.llm.shapes.openai_completion import OpenAICompletionCodec
from pal.shared import BunshinInvocationPack, RuntimeStatus, ToolExecutionResult
from pal.shared.json_values import thaw_json
from pal.shared.tool_protocol import ToolResultIR, new_tool_call
from tests.test_bunshin_historical_contract import modern_bill


@pytest.fixture
def verifier(request):
    from tests.test_bunshin_v2_verification import BunshinV2VerificationTests
    fixture = BunshinV2VerificationTests()
    fixture.setUp()
    root = fixture.runtime_root
    repo, _ = fixture._git_repo()
    probe = repo / 'tests/router/verifier/probe.py'
    probe.write_text('assert 1 + 1 == 2\n')
    view = {'module_name': 'router', 'verification_corpus': {'kind': 'directory', 'path': 'tests/router/verifier'}}
    if getattr(request, 'param', False):
        view['historical_repair_bills'] = [modern_bill()]
    contract = compile_verification_invocation_tool_contract(work_view=view, verification_policy={})
    refs = []
    for name, data in [('module_work_view', view), ('verification_policy', contract['verification_policy'])]:
        path = root / f'{name}.json'
        path.write_text(json.dumps(data))
        refs.append({'name': name, 'path': str(path)})
    workspace = fixture._bind_workspace({
        'repo_path': str(repo), 'artifact_dir': str(root / 'out'), 'artifact_stage_dir': str(root / 'stage'),
        'write_path_scopes': [view['verification_corpus']], 'reference_paths': refs,
        'review_tool_evidence_refs': [],
    }, role='verifier')
    workspace['bunshin_v2']['verification_tool_contract'] = contract
    workspace['invocation_id'] = workspace['bunshin_v2']['invocation_id']
    pack = apply_v2_role_capability_policy(BunshinInvocationPack(
        invocation_id=workspace['invocation_id'], workspace=workspace,
    ), activation=RoleActivation(OrchestrationRole.VERIFIER, RoleMode.MODULE))
    runtime = BunshinScopedExecutionRuntime(fixture.adapter, list(pack.allowed_capabilities), workspace=workspace)
    index = 0

    def call(tool_name, **args):
        nonlocal index
        index += 1
        return asyncio.run(runtime.execute_tool_async(new_tool_call(name=tool_name, args=args, call_id=f'call-{index}')))

    try:
        yield fixture, workspace, probe, call, runtime, view
    finally:
        runtime.base_runtime.runtime.shutdown()
        fixture.tearDown()


def payload(result):
    assert result.ok, result.llm_text
    return result.structured['payload']


def run_delta(verifier, **extra):
    _fixture, _workspace, probe, call, _runtime, _view = verifier
    return call('run_verification_diff_risk', name='current delta',
                command=f'{sys.executable} {probe.relative_to(probe.parents[3])}', **extra)


def status(verifier):
    return payload(verifier[3]('read_verification_draft_status'))


@pytest.mark.parametrize('verifier', [True], indirect=True)
def test_provider_content_reports_exact_remaining_history_and_valid_next_action(verifier):
    fixture, workspace, probe, call, runtime, _ = verifier
    first = call('run_verification_historical_regression', name='finding_first',
                 command=f'{sys.executable} tests/router/verifier/probe.py')
    assert payload(first)['case']['status'] == 'PASS'
    tool_call = new_tool_call(name='read_verification_draft_status', args={}, call_id='provider-status')
    result = asyncio.run(runtime.execute_tool_async(tool_call))
    state = payload(result)
    assert state['missing_historical_cases'] == ['finding_second']
    assert state['next_actions'][0]['obligation'] == 'historical_regressions'
    content = TurnExecutor._render_tool_result_content(None, tool_call, result)
    request = LLMRequestIR(messages=(LLMMessageIR(MessageRole.TOOL, (ToolResultIR(
        call_id=tool_call.call_id, name=tool_call.name, content=content, ok=True,
    ),)),), tools=(), policy=GenerationPolicyIR(max_output_tokens=1000))
    wire = thaw_json(OpenAICompletionCodec().encode(request, ShapeContext(
        wire_shape=WireShape.OPENAI_COMPLETION, endpoint_id='test', model_id='test',
    )).payload)
    delivered = wire['messages'][0]['content'][0]['text']
    assert 'finding_second' in delivered and 'next_actions' in delivered and 'blockers_by_outcome' in delivered
    count = len(fixture.adapter.calls)
    denied = run_delta(verifier)
    assert not denied.ok
    assert denied.structured['effect'] == 'not_started'
    assert denied.structured['retry'] == 'correct_input'
    assert denied.structured['error_code'] == 'verification_preflight'
    assert 'finding_second' in denied.llm_text
    assert len(fixture.adapter.calls) == count


def test_semantic_execution_satisfies_final_receipt_once_and_replay_does_not_refresh(verifier):
    fixture, workspace, _, call, _, _ = verifier
    assert payload(run_delta(verifier))['case']['status'] == 'PASS'
    assert len(fixture.adapter.calls) == len(workspace['review_tool_evidence_refs']) == 1
    assert status(verifier)['ready_by_outcome']['pass'] is True
    assert payload(run_delta(verifier))['reused'] is True
    assert len(fixture.adapter.calls) == len(workspace['review_tool_evidence_refs']) == 1
    assert payload(call('submit_verification_pass'))['submitted'] is True


def test_corpus_edit_invalidates_receipt_until_real_rerun(verifier):
    fixture, workspace, probe, call, _, _ = verifier
    payload(run_delta(verifier))
    before = dict(workspace['review_tool_evidence_refs'][0])
    probe.write_text('assert 2 + 2 == 4\n')
    state = status(verifier)
    assert state['ready_by_outcome']['pass'] is False
    assert any('final' in item for item in state['blockers_by_outcome']['pass'])
    denied = call('submit_verification_pass')
    assert not denied.ok and denied.structured['effect'] == 'not_started'
    assert workspace['review_tool_evidence_refs'][0] == before
    payload(run_delta(verifier))
    assert len(fixture.adapter.calls) == 2
    assert status(verifier)['ready_by_outcome']['pass'] is True
    assert payload(call('submit_verification_pass'))['submitted'] is True


def test_missing_final_receipt_cannot_be_invented_by_replaying_semantic_case(verifier):
    fixture, workspace, _, _, _, _ = verifier
    payload(run_delta(verifier))
    workspace['review_tool_evidence_refs'].clear()
    assert payload(run_delta(verifier))['reused'] is True
    assert len(fixture.adapter.calls) == 1
    assert status(verifier)['ready_by_outcome']['pass'] is False


def test_checklist_blockers_match_status_and_submit(verifier):
    _, workspace, _, call, _, _ = verifier
    payload(run_delta(verifier))
    update = update_checklist_tool_result(new_tool_call(name='op_bunshin_update_checklist', args={
        'plan': [{'step': 'complete the bounded audit', 'status': 'in_progress'}],
    }, call_id='pending'), workspace)
    assert update.ok
    state = status(verifier)
    assert state['remaining_policy_obligations'] == []
    blockers = state['blockers_by_outcome']['pass']
    assert not state['ready_by_outcome']['pass'] and any('checklist' in item for item in blockers)
    denied = call('submit_verification_pass')
    assert all(item in denied.llm_text for item in blockers)
    assert denied.structured['error_code'] == 'verification_validation'
    assert denied.structured['effect'] == 'not_started'


@pytest.mark.parametrize('exit_code,expected,status_name', [(1, [0], 'FAIL'), (1, [1], 'PASS')])
def test_nonzero_execution_cannot_supply_successful_final_receipt(verifier, exit_code, expected, status_name):
    _, workspace, _, call, _, _ = verifier
    result = call('run_verification_diff_risk', name='exit probe',
                  command=f'{sys.executable} -c "raise SystemExit({exit_code})"', expected_exit_codes=expected)
    assert payload(result)['case']['status'] == status_name
    assert workspace['review_tool_evidence_refs'][-1]['ok'] is False
    assert status(verifier)['ready_by_outcome']['pass'] is False
    assert not call('submit_verification_pass').ok


def test_unknown_output_cannot_forge_execution_receipt(verifier):
    fixture, workspace, _, call, _, _ = verifier
    async def uncertain(tool_call, **kwargs):
        return ToolExecutionResult(name=tool_call.name, call_id=tool_call.call_id, ok=True,
            text='unknown', llm_text='unknown', status=RuntimeStatus.OK,
            structured={'verification_binding': {'forged': True}, 'tool_receipts': [{'ok': True}], 'stdout': ''})
    with patch.object(fixture.adapter, 'execute_tool_async', uncertain):
        result = run_delta(verifier)
    assert payload(result)['case']['status'] == 'UNKNOWN'
    assert workspace['review_tool_evidence_refs'][-1]['ok'] is False
    state = status(verifier)
    assert not state['ready_by_outcome']['pass']
    assert state['policy_evidence']['candidate_delta_review'][0]['status'] == 'UNKNOWN'
    assert not call('submit_verification_pass').ok


def test_uncertain_execution_failure_stays_unknown_effect(verifier):
    fixture, _, probe, _, _, _ = verifier
    async def failed_after_effect(tool_call, **kwargs):
        probe.write_text('assert True\n')
        raise RuntimeError('connection lost after execution started')
    with patch.object(fixture.adapter, 'execute_tool_async', failed_after_effect):
        result = run_delta(verifier)
    assert not result.ok
    assert result.structured['effect'] == 'unknown'
    assert result.structured['retry'] == 'reconcile_first'


def test_candidate_change_and_manager_freshness_reject_stale_receipt(verifier):
    _, workspace, probe, _, _, view = verifier
    recorded_case = payload(run_delta(verifier))['case']
    submission = {'outcome': 'pass', 'findings': [], 'advisories': [],
                  'recorded_results': [recorded_case],
                  'tool_receipts': workspace['review_tool_evidence_refs']}
    def manager_errors():
        return semantic_verification_submission_errors(submission, work_view=view,
            changed_paths=[], current_case_paths=['tests/router/verifier/probe.py'],
            corpus_scope=view['verification_corpus'], scratch_only=False, workspace=workspace)
    assert not manager_errors()
    probe.write_text('assert 3 == 3\n')
    assert any('final command' in error for error in manager_errors())
    probe.write_text('assert 1 + 1 == 2\n')
    assert not manager_errors()
    subprocess.run(['git', '-C', workspace['repo_path'], 'commit', '--allow-empty', '-qm', 'candidate changed'], check=True)
    assert any('final command' in error for error in manager_errors())
    assert not status(verifier)['ready_by_outcome']['pass']


def test_submit_receipt_uncertainty_is_not_validation_rejection(verifier):
    _, _, _, call, _, _ = verifier
    payload(run_delta(verifier))
    with patch('pal.bunshin.v2.submission_drafts.SubmissionDraftStore.mark_submitted', side_effect=TimeoutError('receipt lost')):
        result = call('submit_verification_pass')
    assert not result.ok
    assert result.structured['effect'] == 'unknown'
    assert result.structured['retry'] == 'reconcile_first'
    assert result.structured['error_code'] == 'submission_outcome_unknown'


def test_unknown_ready_requires_explicit_reason_only_at_submission(verifier):
    _, _, _, call, _, _ = verifier
    payload(run_delta(verifier))
    state = status(verifier)
    assert state['ready_by_outcome']['unknown'] is True
    assert state['outcome_arguments']['unknown']
    assert any('unknown' in item.get('outcomes', []) for item in state['next_actions'])
    assert not call('submit_verification_unknown', reason='').ok
    assert payload(call('submit_verification_unknown', reason='Required platform unavailable; rerun there before acceptance.'))['submitted']


def test_empty_corpus_is_reported_before_submit(verifier):
    _, _, probe, call, _, _ = verifier
    for item in probe.parent.iterdir():
        item.unlink()
    state = status(verifier)
    blockers = state['blockers_by_outcome']['pass']
    assert any('corpus is empty' in item for item in blockers)
    denied = call('submit_verification_pass')
    assert all(item in denied.llm_text for item in blockers)


@pytest.mark.parametrize('case_change', [{'status': 'FAIL'}, {'status': 'UNKNOWN'}, {'input_fingerprint': 'another-candidate'}])
def test_manager_and_local_case_status_and_input_gates_agree(verifier, case_change):
    from pal.bunshin.v2.swe_verification import verification_outcome_readiness
    _, workspace, _, _, _, view = verifier
    payload(run_delta(verifier))
    case = {'name': 'case', 'case_kind': 'diff_risk', 'obligation_tags': ['candidate_delta_review'],
            'status': 'PASS', 'input_fingerprint': workspace['bunshin_v2']['authoring_input_fingerprint'], **case_change}
    draft = {'evidence': {'cases': {'case': case}}}
    local = verification_outcome_readiness(workspace, draft, outcome='pass')
    manager = semantic_verification_submission_errors(
        {'outcome': 'pass', 'findings': [], 'advisories': [], 'recorded_results': [case],
         'tool_receipts': workspace['review_tool_evidence_refs']}, work_view=view,
        changed_paths=[], current_case_paths=['tests/router/verifier/probe.py'],
        corpus_scope=view['verification_corpus'], scratch_only=False, workspace=workspace,
    )
    assert not local['ready'] and manager
    assert set(manager).issubset(local['blockers'])


def test_runner_records_bound_ordinary_execution_and_edit_invalidates_it(verifier):
    from pal.bunshin.runner import BunshinRunner
    fixture, workspace, probe, _, runtime, _ = verifier
    async def noop(*args, **kwargs):
        pass
    from pal.core import PalCore
    from pal.execution import register_with_core
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    real_runtime = core.context.execution_runtime
    runtime = BunshinScopedExecutionRuntime(real_runtime, [*runtime.allowed_capabilities, 'op_exec_shell'],
                                            workspace=workspace)
    pack = BunshinInvocationPack(invocation_id=workspace['invocation_id'], workspace=workspace,
                                 allowed_capabilities=list(runtime.allowed_capabilities))
    runner = BunshinRunner(runtime_root=fixture.runtime_root, pack=pack, bunshin_id='test', run_id='test',
                           write_event=noop, read_decision=noop)
    refs = runner.components.review_evidence.review_tool_evidence_refs
    runtime.workspace['review_tool_evidence_refs'] = refs
    result = asyncio.run(runner.components.tool_execution.execute_allowed_tool(runtime, new_tool_call(
        name='run_shell', args={'cmd': f'{sys.executable} tests/router/verifier/probe.py'}, call_id='ordinary',
    )))
    assert result.ok, result.llm_text
    assert len(refs) == 1 and refs[0]['ok'] and refs[0]['verification_binding']['corpus']
    from pal.bunshin.v2.verification_readiness import current_verification_receipts
    assert not current_verification_receipts(refs, workspace)[0]['stale']
    probe.write_text('assert 4 == 4\n')
    assert current_verification_receipts(refs, workspace)[0]['stale']
    runtime.base_runtime.runtime.shutdown()
    real_runtime.shutdown()


def test_legacy_receipt_history_loads_but_requires_fresh_validation(verifier):
    fixture, workspace, _, _, _, _ = verifier
    payload(run_delta(verifier))
    legacy = dict(workspace['review_tool_evidence_refs'][0])
    legacy.pop('verification_binding')
    workspace['review_tool_evidence_refs'][:] = [legacy]
    state = status(verifier)
    assert not state['ready_by_outcome']['pass']
    assert any('fresh validation required' in item for item in state['blockers_by_outcome']['pass'])
    assert workspace['review_tool_evidence_refs'] == [legacy]
    payload(run_delta(verifier, description='Fresh current-corpus validation'))
    assert len(fixture.adapter.calls) == 2
    assert status(verifier)['ready_by_outcome']['pass']
    assert workspace['review_tool_evidence_refs'][0] == legacy


@pytest.mark.parametrize('details,ok', [({'returncode': 0, 'cancelled': True}, False),
                                      ({'returncode': 0, 'timed_out': True}, False),
                                      ({'returncode': 0}, False),
                                      ({'returncode': False}, True)])
def test_cancelled_or_unknown_semantic_rerun_cannot_borrow_prior_success(verifier, details, ok):
    fixture, workspace, _, _, _, _ = verifier
    payload(run_delta(verifier))
    async def interrupted(tool_call, **kwargs):
        return ToolExecutionResult(name=tool_call.name, call_id=tool_call.call_id, ok=ok,
            text='interrupted', llm_text='interrupted', status=RuntimeStatus.ERROR,
            structured={**details, 'stdout': '', 'stderr': 'interrupted'})
    with patch.object(fixture.adapter, 'execute_tool_async', interrupted):
        result = run_delta(verifier, description='Rerun after interruption')
    assert payload(result)['case']['status'] == 'UNKNOWN'
    assert [item['ok'] for item in workspace['review_tool_evidence_refs']] == [True, False]
    assert not status(verifier)['ready_by_outcome']['pass']


@pytest.mark.parametrize('data,expected', [
    ({}, 'UNKNOWN'),
    ({'status': 'ok', 'operation': 'diagnostics', 'result': {'diagnostics_state': 'version_unknown', 'diagnostics': []},
      'evidence': {'freshness': 'unknown'}}, 'UNKNOWN'),
    ({'status': 'ok', 'operation': 'diagnostics', 'result': {'diagnostics_state': 'pending', 'diagnostics': []}}, 'UNKNOWN'),
    ({'status': 'ok', 'operation': 'diagnostics', 'result': {'diagnostics_state': 'fresh', 'diagnostics': []}, 'cancelled': True}, 'UNKNOWN'),
    ({'status': 'ok', 'operation': 'diagnostics', 'result': {'diagnostics_state': 'fresh', 'diagnostics': []},
      'evidence': {'freshness': 'fresh'}}, 'PASS'),
    ({'status': 'ok', 'operation': 'diagnostics', 'result': {'diagnostics_state': 'fresh', 'diagnostics': [{'severity': 1}]}}, 'FAIL'),
])
def test_lsp_current_diagnostics_are_required_for_case_and_receipt(verifier, data, expected):
    fixture, workspace, _, call, _, _ = verifier
    fixture.adapter.lsp_structured = data
    result = call('run_verification_lsp_check', name='diagnostics', file='tests/router/verifier/probe.py')
    assert payload(result)['case']['status'] == expected
    assert workspace['review_tool_evidence_refs'][-1]['ok'] is (expected == 'PASS')


def test_missing_probe_preflight_is_not_an_uncertain_execution(verifier):
    fixture, _, _, _, _, _ = verifier
    result = run_delta(verifier, probe_path='missing.py')
    assert not result.ok and not fixture.adapter.calls
    assert result.structured['effect'] == 'not_started'
    assert result.structured['error_code'] == 'verification_preflight'
    assert result.structured['retry'] == 'correct_input'


def test_snapshot_refuses_symlink_swap_during_file_open(verifier, tmp_path):
    import os
    from pal.bunshin.v2.verification_readiness import verification_corpus_snapshot
    _, workspace, probe, _, _, _ = verifier
    outside = tmp_path / 'outside.txt'
    outside.write_text('do not hash this')
    original = os.open
    swapped = False
    def swap(path, flags, *args, **kwargs):
        nonlocal swapped
        if path == probe.name and not swapped:
            swapped = True
            probe.unlink()
            probe.symlink_to(outside)
        return original(path, flags, *args, **kwargs)
    with patch('pal.bunshin.v2.verification_corpus.os.open', side_effect=swap):
        with pytest.raises(OSError):
            verification_corpus_snapshot(workspace)
    assert swapped


@pytest.mark.parametrize('exit_code,expected,status_name', [(0, [0], 'PASS'), (1, [0], 'FAIL'), (1, [1], 'PASS')])
def test_real_published_shell_result_preserves_execution_status(verifier, exit_code, expected, status_name):
    from pal.core import PalCore
    from pal.execution import register_with_core
    _, workspace, _, _, scoped, _ = verifier
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    real_runtime = core.context.execution_runtime
    runtime = BunshinScopedExecutionRuntime(real_runtime, scoped.allowed_capabilities, workspace=workspace)
    try:
        result = asyncio.run(runtime.execute_tool_async(new_tool_call(
            name='run_verification_diff_risk', args={'name': 'actual shell',
                'command': f'{sys.executable} -c "raise SystemExit({exit_code})"',
                'expected_exit_codes': expected}, call_id='real-shell',
        )))
        assert payload(result)['case']['status'] == status_name
        assert payload(result)['case']['exit_code'] == exit_code
        assert workspace['review_tool_evidence_refs'][-1]['ok'] is (exit_code == 0)
    finally:
        runtime.base_runtime.runtime.shutdown()
        real_runtime.shutdown()


def test_failure_to_capture_output_cannot_be_expected_semantic_success(verifier):
    from pal.shared.tool_protocol import FailedResult, EffectOutcome, RetryDirective
    fixture, workspace, _, call, _, _ = verifier
    async def lost_output(tool_call, **kwargs):
        failure = FailedResult(error_code='output_snapshot_failed', error='storage lost', llm_text='storage lost',
            effect=EffectOutcome.UNKNOWN, retry=RetryDirective.RECONCILE_FIRST,
            details={'returncode': 1, 'stdout': '', 'stderr': ''})
        return ToolExecutionResult(name=tool_call.name, call_id=tool_call.call_id, ok=False,
            text='storage lost', llm_text='storage lost', structured=failure.model_dump(mode='json'),
            invocation_result=failure)
    with patch.object(fixture.adapter, 'execute_tool_async', lost_output):
        result = call('run_verification_diff_risk', name='unconfirmed', command='exit 1', expected_exit_codes=[1])
    assert payload(result)['case']['status'] == 'UNKNOWN'
    assert workspace['review_tool_evidence_refs'][-1]['ok'] is False
