"""Manager validation survives the serialized RPC and worker error projection."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from pal.core import PalCore
from pal.bunshin.authoring_errors import AuthoringProgress, authoring_error_result
from pal.bunshin.ipc import BunshinManagerRpcError
from pal.bunshin.role_gateway import RoleAssignmentGateway
from pal.bunshin.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
from pal.bunshin.submission_errors import role_gateway_error_kind
from pal.foundation.sidecar import dispatch_sidecar_request
from pal.shared.tool_protocol import new_tool_call
from tests.test_bunshin_verifier_progress_evidence import verifier


@pytest.fixture
def remote_authoring(verifier, monkeypatch):
    workspace = verifier.pack.workspace
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind='work_items')
    manager_store = SubmissionDraftStore(verifier.runtime_root)
    gateway = RoleAssignmentGateway(SimpleNamespace(runtime_root=verifier.runtime_root))
    monkeypatch.setattr(gateway, '_context', lambda *args, **kwargs: context)
    monkeypatch.setattr(gateway, '_authoring_workspace', lambda *args: workspace)
    authenticated = {'assignment': {'state': 'running'}}
    worker_store = object.__new__(SubmissionDraftStore)
    worker_store.read = lambda *args, **kwargs: manager_store.read(context, seed={'items': []})
    wire_results = []

    def rpc(method, params):
        async def dispatch(method, params):
            assert method == 'draft_mutate'
            return gateway._draft_mutate(authenticated, params)
        response = asyncio.run(dispatch_sidecar_request(
            {'id': 'test', 'method': method, 'params': params}, dispatch,
            error_kind=role_gateway_error_kind,
        ))
        response = json.loads(json.dumps(response))
        wire_results.append(response)
        if not response['ok']:
            error = response['error']
            raise BunshinManagerRpcError(error['message'], kind=error['kind'], payload=error)
        return response['result']

    worker_store._role_gateway = SimpleNamespace(request_sync=rpc)

    def invoke(args, operation):
        progress = AuthoringProgress()
        unused_reducer = Mock(side_effect=AssertionError('verifier reducer must execute at Manager'))
        try:
            result = progress.mutate(worker_store, context, operation_key=operation,
                                     request=args, reducer=unused_reducer, seed={'items': []})
        except Exception as exc:
            result = authoring_error_result(
                new_tool_call(name='update_finding', args=args), exc, progress,
                correction='Read read_verification_draft_status and correct the finding identity or revision.',
            )
        unused_reducer.assert_not_called()
        return result

    initial = invoke(dict(finding_id='finding-1', expected_revision=0,
                          finding_kind='module_defect', priority='p1',
                          summary='Observed contract violation.'), 'create')
    assert initial['created']
    return invoke, manager_store, context, wire_results


@pytest.mark.parametrize('args,cause', [
    ({'finding_id': 'finding-1', 'expected_revision': 2, 'reason': 'remove'}, 'revision is stale'),
    ({'finding_id': 'absent', 'expected_revision': 1, 'reason': 'remove'}, 'not in the current editable draft'),
    ({'plan': [{'step': 'a', 'status': 'in_progress'}, {'step': 'b', 'status': 'in_progress'}]}, 'at most one'),
])
def test_remote_semantic_rejection_is_precise_and_does_not_write(remote_authoring, args, cause):
    invoke, store, context, wire = remote_authoring
    before = store.read(context)
    result = invoke(args, 'rejected-edit')
    assert wire[-1]['error']['kind'] == 'submission_validation'
    assert cause in result.llm_text
    assert result.invocation_result.kind == 'rejected'
    assert result.invocation_result.effect.value == 'not_started'
    assert result.invocation_result.retry.value == 'correct_input'
    assert 'read_verification_draft_status' in result.llm_text
    after = store.read(context)
    assert after.version == before.version
    assert after.payload == before.payload


@pytest.mark.parametrize('error', [OSError('storage unavailable'), RuntimeError('submission Draft CAS conflict')])
def test_remote_storage_and_cas_errors_are_not_content_rejections(remote_authoring, error):
    invoke, _, _, wire = remote_authoring
    original = SubmissionDraftStore.mutate

    def fail_at_manager(self, *args, **kwargs):
        if self._role_gateway is None:
            raise error
        return original(self, *args, **kwargs)

    with patch.object(SubmissionDraftStore, 'mutate', fail_at_manager):
        result = invoke({'finding_id': 'finding-1', 'expected_revision': 1, 'reason': 'remove'}, 'failed-edit')
    assert wire[-1]['error']['kind'] == 'role_gateway'
    assert str(error) in result.llm_text
    assert result.invocation_result.kind == 'failed'
    assert result.invocation_result.retry.value == 'reconcile_first'
