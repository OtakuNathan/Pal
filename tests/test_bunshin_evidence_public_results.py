"""Evidence wrappers preserve the provider's public result across recording/replay."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pal.bunshin import semantic_evidence as evidence
from pal.shared import ToolExecutionResult
from pal.shared.result_snapshot import ResultSnapshotRef
from pal.shared.tool_protocol import new_tool_call
from tests.test_bunshin_verifier_progress_evidence import verifier


TRACE = 'Traceback (most recent call last):\n  File "/private/provider.py", line 7\nOSError: backend offline'
PUBLIC = 'Backend offline. Accepted item 0; item 1 was not submitted. Reconcile item 0 before retrying.'


def source_result(*, ok=False):
    return ToolExecutionResult(name='diagnostics', ok=ok, text='provider result', llm_text=PUBLIC,
        structured={'status': 'ok' if ok else 'error',
                    'result': {'status': 'ok', 'diagnostics_state': 'fresh', 'diagnostics': []},
                    'evidence': {'last_error': TRACE}},
        snapshot_refs=(ResultSnapshotRef('snapshot', '/tmp/public-result', 'digest', 12),))


def run(verifier, adapter, *, call_id='evidence', shell=False):
    call = new_tool_call(name='verification_probe', call_id=call_id,
        args={'name': 'probe', **({'command': 'probe'} if shell else {'file': 'sample.py'})})
    kwargs = dict(workspace=verifier.pack.workspace, original_adapter=adapter, draft_kind='verification')
    return asyncio.run(evidence.run_shell_evidence(call, case_kind='unit', obligation_tag='test', **kwargs)
                       if shell else evidence.run_lsp_evidence(call, **kwargs))


@pytest.mark.parametrize('shell', [False, True])
def test_failed_provider_result_is_preserved_on_first_delivery_and_operation_replay(verifier, shell):
    source = source_result()
    adapter = SimpleNamespace(execute_tool_async=AsyncMock(return_value=source))
    first = run(verifier, adapter, shell=shell)
    replay = run(verifier, adapter, shell=shell)
    assert first.ok and replay.ok  # Recording evidence succeeded; the probe did not.
    assert first.structured['case']['status'] == 'UNKNOWN'
    assert replay.structured['replayed'] is True
    for result in (first, replay):
        assert PUBLIC in result.llm_text
        assert 'Traceback' not in result.llm_text
        assert '/private/provider.py' not in result.llm_text
        assert result.snapshot_refs == source.snapshot_refs
    assert adapter.execute_tool_async.await_count == 1


def test_lsp_reuse_preserves_public_text_without_rebuilding_it_from_evidence(verifier):
    source = source_result(ok=True)
    adapter = SimpleNamespace(execute_tool_async=AsyncMock(return_value=source))
    first = run(verifier, adapter)
    reused = run(verifier, adapter, call_id='another-call')
    assert first.ok and reused.ok
    assert reused.structured['reused'] is True
    assert reused.structured['execution'] == source.structured
    assert PUBLIC in reused.llm_text
    assert 'Traceback' not in reused.llm_text
    assert reused.snapshot_refs == source.snapshot_refs
    assert adapter.execute_tool_async.await_count == 1


def test_successful_public_business_text_is_not_sanitized_by_wrapper(verifier):
    source = source_result(ok=True)
    source = ToolExecutionResult(name=source.name, ok=True, llm_text='Source excerpt:\n' + TRACE,
                                 structured=source.structured)
    adapter = SimpleNamespace(execute_tool_async=AsyncMock(return_value=source))
    assert run(verifier, adapter).llm_text.endswith(source.llm_text)
    assert run(verifier, adapter).llm_text.endswith(source.llm_text)


@pytest.mark.parametrize('phase', ['artifact', 'record', 'bind'])
@pytest.mark.parametrize('shell', [False, True])
def test_recording_fault_retains_lower_result_and_reports_only_its_own_failure(verifier, monkeypatch, phase, shell):
    source = source_result()
    adapter = SimpleNamespace(execute_tool_async=AsyncMock(return_value=source))
    def fail(*args, **kwargs):
        try:
            raise PermissionError('evidence storage denied')
        except PermissionError as cause:
            raise RuntimeError('storage wrapper') from cause
    if phase == 'artifact':
        monkeypatch.setattr(evidence.ContentAddressedArtifactStore, 'put_bytes', fail)
    elif phase == 'record':
        monkeypatch.setattr(evidence.SubmissionDraftStore, 'mutate', fail)
    else:
        monkeypatch.setattr(evidence, 'bind_recorded_case_execution', fail)
    result = run(verifier, adapter, shell=shell)
    assert not result.ok
    assert result.llm_text.startswith(PUBLIC)
    assert 'PermissionError: evidence storage denied' in result.llm_text
    assert 'storage wrapper' not in result.llm_text
    assert 'Traceback' not in result.llm_text
    assert 'do not rerun the underlying tool' in result.llm_text
    assert result.structured['evidence_recorded'] is (phase == 'bind')
    assert result.structured['execution'] == source.structured
    assert result.snapshot_refs == source.snapshot_refs
    assert adapter.execute_tool_async.await_count == 1


def test_legacy_record_without_public_response_does_not_expose_private_evidence(verifier, monkeypatch):
    monkeypatch.setattr(evidence, '_save_public_result', lambda *args: {})
    adapter = SimpleNamespace(execute_tool_async=AsyncMock(return_value=source_result()))
    assert run(verifier, adapter).ok
    replay = run(verifier, adapter)
    assert replay.ok
    assert 'original public response was not saved' in replay.llm_text
    assert 'no fresh tool execution occurred' in replay.llm_text
    assert 'Traceback' not in replay.llm_text
    assert adapter.execute_tool_async.await_count == 1


def test_unexpected_verifier_exception_reports_root_cause():
    error = RuntimeError('outer wrapper')
    error.__cause__ = PermissionError('cannot read evidence')
    result = evidence._error_result(new_tool_call(name='probe', args={}), error, execution_started=False)
    assert result.text == 'PermissionError: cannot read evidence'
    assert 'outer wrapper' not in result.llm_text
