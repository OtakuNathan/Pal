from contextlib import ExitStack
from unittest.mock import Mock, patch

import pytest

from pal.bunshin.ipc import BunshinManagerRpcError
from pal.bunshin.v2 import contract_submission, review_submission
from pal.bunshin.v2.submission_drafts import AUTHORING_CONTRACT_VERSION, SubmissionDraftStore
from pal.bunshin.v2.submission_errors import (
    SubmissionValidationError, role_gateway_error_kind, submission_validation,
)
from pal.foundation.sidecar import dispatch_sidecar_request
from pal.shared.tool_protocol import new_tool_call


@pytest.mark.parametrize('module', [contract_submission, review_submission])
@pytest.mark.parametrize('phase,exc,category,effect,retry', [
    ('checklist', SubmissionValidationError('unfinished task'), 'validation', 'not_started', 'correct_input'),
    ('checklist', OSError('storage unavailable'), 'submission_infrastructure_error', 'not_started', 'safe'),
    ('read', BunshinManagerRpcError('service unavailable', kind='role_gateway'), 'submission_infrastructure_error', 'not_started', 'safe'),
    ('submit', BunshinManagerRpcError('missing graph sink', kind='submission_validation'), 'validation', 'not_started', 'correct_input'),
    ('submit', TimeoutError('reply lost'), 'submission_outcome_unknown', 'unknown', 'reconcile_first'),
    ('submit', BunshinManagerRpcError('pinned binding unavailable', kind='role_gateway'), 'submission_outcome_unknown', 'unknown', 'reconcile_first'),
    ('read', ValueError('stale assignment context'), 'submission_infrastructure_error', 'not_started', 'do_not_retry'),
    ('read', RuntimeError('unexpected runtime bug'), 'submission_infrastructure_error', 'not_started', 'do_not_retry'),
])
def test_submit_errors_preserve_failure_meaning(module, phase, exc, category, effect, retry):
    with ExitStack() as stack:
        checklist = stack.enter_context(patch.object(module, 'assert_work_items_complete', return_value={'items': []}))
        stack.enter_context(patch.object(module.SubmissionDraftContext, 'from_workspace', return_value=Mock()))
        store = stack.enter_context(patch.object(module, 'SubmissionDraftStore')).return_value
        store.read.return_value.version = 1
        if module is contract_submission:
            stack.enter_context(patch.object(module, 'read_architect_yaml', return_value={}))
        else:
            stack.enter_context(patch.object(module, 'findings_from_work_items', return_value=[]))
        target = {'checklist': checklist, 'read': store.read, 'submit': store.mark_submitted}[phase]
        target.side_effect = exc
        name = 'contract_submit' if module is contract_submission else 'review_submit'
        result = getattr(module, name + '_tool_result')(
            new_tool_call(name=name, args={}, call_id='test'),
            {'runtime_root': '/unused', 'architect_path': '/unused/architect.yaml',
             'bunshin_v2': {'role': 'reviewer', 'mode': 'architecture'}},
        )
    assert not result.ok
    assert result.structured['error_category'] == category
    assert result.invocation_result.effect.value == effect
    assert result.invocation_result.retry.value == retry
    assert str(exc) in result.llm_text
    assert result.status == ('invalid' if category == 'validation' else 'error')
    if category != 'validation':
        assert 'Preserve' in result.llm_text


@pytest.mark.parametrize('status', [
    {'recorded': True, 'submission_artifact_ref': {'sha256': 'accepted-artifact'}, 'submission_payload_hash': 'accepted-payload'},
    {'recorded': True}, {'recorded': False}, OSError('still offline'),
])
def test_lost_submit_reply_queries_receipt_without_replaying(status):
    store = object.__new__(SubmissionDraftStore)
    store._role_gateway = Mock()
    lost_reply = TimeoutError('reply lost')
    store._role_gateway.request_sync.side_effect = [lost_reply, status]
    context = Mock(authoring_contract_version=AUTHORING_CONTRACT_VERSION, role='reviewer', mode='architecture')
    if isinstance(status, dict) and status.get('submission_artifact_ref'):
        result = store.mark_submitted(context, expected_version=1, submission_payload={})
        assert result == {
            'submitted': True, 'receipt_confirmed': True,
            'submission_artifact_ref': status['submission_artifact_ref'],
            'submission_payload_hash': status['submission_payload_hash'],
        }
    else:
        with pytest.raises(TimeoutError) as caught:
            store.mark_submitted(context, expected_version=1, submission_payload={})
        assert caught.value is lost_reply
    assert [call.args[0] for call in store._role_gateway.request_sync.call_args_list] == ['draft_submit', 'submission_status']


@pytest.mark.parametrize('exc,kind', [
    (ValueError('missing required field'), 'submission_validation'),
    (OSError('disk unavailable'), 'role_gateway'),
    (RuntimeError('compiler bug'), 'role_gateway'),
])
def test_validation_category_survives_rpc_serialization(exc, kind):
    import asyncio

    async def call_method(method, params):
        with submission_validation():
            raise exc

    response = asyncio.run(dispatch_sidecar_request(
        {'id': 'test', 'method': 'draft_submit'}, call_method,
        error_kind=role_gateway_error_kind,
    ))
    assert not response['ok']
    assert response['error']['kind'] == kind
    assert str(exc) in response['error']['message']


def test_pinned_manager_state_error_is_not_content_validation():
    from pal.bunshin.v2.role_gateway import RoleAssignmentGateway

    gateway = RoleAssignmentGateway(Mock())
    gateway.service.repository.read_artifact_record.return_value = None
    with pytest.raises(ValueError) as caught:
        gateway._compile_architect_submission(
            {'assignment': {'family_binding_sha': 'missing'}},
            {'source': 'architect.yaml', 'architecture': {}},
        )
    assert not isinstance(caught.value, SubmissionValidationError)
    assert role_gateway_error_kind(caught.value) == 'role_gateway'


def test_known_validation_rejection_does_not_query_receipt():
    store = object.__new__(SubmissionDraftStore)
    store._role_gateway = Mock()
    error = BunshinManagerRpcError('missing sink', kind='submission_validation')
    store._role_gateway.request_sync.side_effect = error
    context = Mock(authoring_contract_version=AUTHORING_CONTRACT_VERSION, role='reviewer', mode='architecture')
    with pytest.raises(BunshinManagerRpcError) as caught:
        store.mark_submitted(context, expected_version=1, submission_payload={})
    assert caught.value is error
    store._role_gateway.request_sync.assert_called_once()


def test_wrapped_yaml_io_error_is_infrastructure(tmp_path):
    from pal.bunshin.v2.contract_protocol import read_architect_yaml
    from pal.bunshin.v2.submission_errors import submission_error_result

    with pytest.raises(ValueError) as caught:
        with submission_validation():
            read_architect_yaml(tmp_path / 'missing.yaml')
    result = submission_error_result(
        new_tool_call(name='contract_submit', args={}, call_id='yaml'),
        caught.value, submission_started=False,
        invalid_code='invalid_contract_submission', correction='Correct content.',
    )
    assert result.structured['error_category'] == 'submission_infrastructure_error'
    assert result.invocation_result.retry.value == 'safe'
