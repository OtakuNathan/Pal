import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pal.core import PalCore  # Initialize runtime modules before worker imports.
from pal.bunshin import ask_question, candidate_builder
from pal.bunshin.submission_errors import SubmissionValidationError
from pal.shared.tool_protocol import new_tool_call


@pytest.mark.parametrize('stage,error,effect,retry,code', [
    ('read', OSError('storage unavailable'), 'not_started', 'safe', 'submission_infrastructure_error'),
    ('mark', TimeoutError('reply lost'), 'unknown', 'reconcile_first', 'submission_outcome_unknown'),
    ('mark', SubmissionValidationError('missing obligation'), 'not_started', 'correct_input', 'invalid_candidate_submission'),
    ('report', OSError('disk full'), 'applied', 'do_not_retry', 'submission_post_acceptance_error'),
])
def test_candidate_submission_failure_preserves_stage(monkeypatch, tmp_path, stage, error, effect, retry, code):
    store = Mock(uses_role_gateway=True)
    store.read.return_value = SimpleNamespace(version=1)
    report = Mock(return_value={})
    if stage == 'read':
        store.read.side_effect = error
    elif stage == 'mark':
        store.mark_submitted.side_effect = error
    else:
        report.side_effect = error
    monkeypatch.setattr(candidate_builder, 'SubmissionDraftStore', Mock(return_value=store))
    monkeypatch.setattr(candidate_builder.SubmissionDraftContext, 'from_workspace', Mock(return_value=object()))
    monkeypatch.setattr(candidate_builder, '_bound_candidate_work_view', Mock(return_value={}))
    monkeypatch.setattr(candidate_builder, 'assert_work_items_complete', Mock(return_value={'items': []}))
    monkeypatch.setattr(candidate_builder, '_live_worktree_delta', Mock(return_value=['source.py']))
    monkeypatch.setattr(candidate_builder, 'validate_candidate_submission', Mock(return_value=[]))
    monkeypatch.setattr(candidate_builder, '_write_bunshin_artifact', report)
    produced = []
    result = asyncio.run(candidate_builder.candidate_builder_tool_result(
        new_tool_call(name='op_bunshin_candidate_submit', args={}),
        {'runtime_root': str(tmp_path)}, produced,
    ))
    assert not result.ok
    assert result.invocation_result.error_code == code
    assert result.invocation_result.effect.value == effect
    assert result.invocation_result.retry.value == retry
    assert str(error) in result.llm_text
    assert produced == []
    if stage == 'report':
        store.mark_submitted.assert_called_once()
        assert 'already accepted' in result.llm_text
        assert 'Do not resubmit' in result.llm_text
    else:
        report.assert_not_called()


def question_call(**overrides):
    args = dict(title='Storage', question='Which storage?', option_1='SQLite',
                option_2='Postgres', option_3='Files')
    args.update(overrides)
    return new_tool_call(name='op_bunshin_ask_question', args=args)


@pytest.mark.parametrize('response,effect,retry,code', [
    ({'answers': [{'answer': 'Use SQLite'}], 'task_revision': {'appended': False}},
     'applied', 'do_not_retry', 'question_revision_unconfirmed'),
    ({'answers': []}, 'applied', 'do_not_retry', 'user_question_cancelled'),
    (TimeoutError('reply lost'), 'unknown', 'reconcile_first', 'user_question_outcome_unknown'),
])
def test_question_failure_keeps_known_answer_and_effect(response, effect, retry, code):
    calls = []

    async def request(payload):
        calls.append(payload)
        if isinstance(response, Exception):
            raise response
        return response

    result = asyncio.run(ask_question.ask_question_tool_result(question_call(), request_user=request))
    assert not result.ok
    assert len(calls) == 1
    assert result.invocation_result.error_code == code
    assert result.invocation_result.effect.value == effect
    assert result.invocation_result.retry.value == retry
    if code == 'question_revision_unconfirmed':
        assert result.structured['answer'] == 'Use SQLite'
        assert 'Use SQLite' in result.llm_text
        assert 'Do not ask the user again' in result.llm_text
        assert 'Manager' in result.llm_text


def test_question_invalid_arguments_do_not_contact_user():
    async def request(_):
        pytest.fail('invalid input must not contact the user')
    result = asyncio.run(ask_question.ask_question_tool_result(question_call(title=''), request_user=request))
    assert result.invocation_result.kind == 'rejected'
    assert result.invocation_result.retry.value == 'correct_input'


def test_question_unavailable_does_not_claim_unknown_effect():
    result = asyncio.run(ask_question.ask_question_tool_result(question_call(), request_user=None))
    assert result.invocation_result.effect.value == 'not_started'
    assert result.invocation_result.retry.value == 'do_not_retry'


def test_question_success_preserves_recorded_revision():
    response = {'answers': [{'answer': 'Use SQLite'}], 'task_revision': {'appended': True, 'revision': 2}}
    async def request(_):
        return response
    result = asyncio.run(ask_question.ask_question_tool_result(question_call(), request_user=request))
    assert result.ok
    assert result.structured['answer'] == 'Use SQLite'
    assert result.structured['task_revision'] == response['task_revision']
