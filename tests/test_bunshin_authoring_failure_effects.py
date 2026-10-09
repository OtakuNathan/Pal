import asyncio
from unittest.mock import Mock

import pytest

from pal.core import PalCore
from pal.bunshin import work_items, verification_builder as verifier
from pal.bunshin.authoring_errors import AuthoringProgress, authoring_error_result
from pal.bunshin.semantic_evidence import scratch_fingerprint, scratch_probe_fingerprint
from pal.shared.tool_protocol import new_tool_call


def call(name='op_bunshin_update_checklist', **args):
    return new_tool_call(name=name, args=args)


@pytest.mark.parametrize('failure,expected', [
    ('mutate', ('unknown', 'reconcile_first')),
    ('render', ('applied', 'do_not_retry')),
])
def test_checklist_failure_reports_durable_effect(monkeypatch, failure, expected):
    monkeypatch.setattr(work_items, '_prepare_checklist', Mock(return_value=({}, object(), {}, Mock())))
    store = Mock()
    store.mutate.return_value = {'unfinished': []}
    if failure == 'mutate':
        store.mutate.side_effect = TimeoutError('reply lost')
    monkeypatch.setattr(work_items, 'SubmissionDraftStore', Mock(return_value=store))
    monkeypatch.setattr(work_items, '_next_action', Mock(side_effect=RuntimeError('render failed')))
    result = work_items.update_checklist_tool_result(call(), {'runtime_root': '/tmp'})
    assert result.invocation_result.kind == 'failed'
    assert (result.invocation_result.effect.value, result.invocation_result.retry.value) == expected
    assert 'Correct the semantic plan' not in result.llm_text
    if failure == 'render':
        assert result.structured['recorded_result'] == {'unfinished': []}


def test_finding_success_then_bad_response_is_not_input_rejection(monkeypatch):
    monkeypatch.setattr(work_items, '_prepare_add_finding', Mock(return_value=(object(), {}, 'hash', Mock())))
    store = Mock()
    store.mutate.return_value = {'finding_count': 1}  # Missing output field after commit.
    monkeypatch.setattr(work_items, 'SubmissionDraftStore', Mock(return_value=store))
    result = work_items.add_finding_tool_result(call(), {'runtime_root': '/tmp'})
    assert result.invocation_result.effect.value == 'applied'
    assert result.invocation_result.retry.value == 'do_not_retry'


def test_reducer_validation_before_write_remains_correctable():
    progress = AuthoringProgress()
    def reducer(_):
        raise ValueError('stale finding revision')
    store = Mock()
    store.mutate.side_effect = lambda *args, **kwargs: kwargs['reducer']({})
    try:
        progress.mutate(store, object(), reducer=reducer)
    except ValueError as exc:
        result = authoring_error_result(call(), exc, progress, correction='Read current revision.')
    assert result.invocation_result.kind == 'rejected'
    assert result.invocation_result.retry.value == 'correct_input'


def scratch_call():
    return call('op_bunshin_verification_scratch_write', path='probe.py', content='print(1)')


def test_unbound_scratch_never_writes_or_hashes_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    store = Mock()
    monkeypatch.setattr(verifier, '_store_context', Mock(return_value=(object(), store)))
    before = scratch_fingerprint({})
    (tmp_path / 'unrelated').write_text('user data')
    assert scratch_fingerprint({}) == before
    result = asyncio.run(verifier.verification_builder_tool_result(scratch_call(), {}, []))
    assert not (tmp_path / 'probe.py').exists()
    store.mutate.assert_not_called()
    assert result.invocation_result.effect.value == 'not_started'
    assert 'not bound' in result.llm_text
    with pytest.raises(RuntimeError, match='not bound'):
        scratch_probe_fingerprint({}, 'unrelated')


def test_scratch_write_survives_ledger_failure(monkeypatch, tmp_path):
    store = Mock()
    store.mutate.side_effect = OSError('ledger disk full')
    monkeypatch.setattr(verifier, '_store_context', Mock(return_value=(object(), store)))
    result = asyncio.run(verifier.verification_builder_tool_result(
        scratch_call(), {'review_scratch_dir': str(tmp_path)}, []))
    assert (tmp_path / 'probe.py').read_text() == 'print(1)'
    assert result.invocation_result.effect.value == 'applied'
    assert result.structured['scratch_file_written'] is True
    assert str(tmp_path / 'probe.py') in result.llm_text
    assert 'Correct only' not in result.llm_text


def test_scratch_symlink_cannot_escape_bound_root(monkeypatch, tmp_path):
    root = tmp_path / 'scratch'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.mkdir()
    (root / 'link').symlink_to(outside, target_is_directory=True)
    store = Mock()
    monkeypatch.setattr(verifier, '_store_context', Mock(return_value=(object(), store)))
    result = asyncio.run(verifier.verification_builder_tool_result(
        call('op_bunshin_verification_scratch_write', path='link/probe.py', content='x'),
        {'review_scratch_dir': str(root)}, []))
    assert not (outside / 'probe.py').exists()
    assert result.invocation_result.kind == 'rejected'
    (outside / 'probe.py').write_text('existing')
    with pytest.raises(ValueError, match='escapes'):
        scratch_probe_fingerprint({'review_scratch_dir': str(root)}, 'link/probe.py')


def test_draft_read_failure_has_no_mutation_effect(monkeypatch):
    monkeypatch.setattr(verifier, '_draft_status', Mock(side_effect=OSError('storage unavailable')))
    result = asyncio.run(verifier.verification_builder_tool_result(
        call('op_bunshin_verification_draft_status'), {}, []))
    assert result.invocation_result.effect.value == 'none'
    assert result.invocation_result.retry.value == 'safe'


@pytest.mark.parametrize('stage,effect,retry', [
    ('submit', 'unknown', 'reconcile_first'),
    ('report', 'applied', 'do_not_retry'),
])
def test_verification_submission_tracks_acceptance(monkeypatch, stage, effect, retry):
    from types import SimpleNamespace
    store = Mock(uses_role_gateway=True)
    store.read.return_value = SimpleNamespace(payload={}, version=1)
    store.mark_submitted.return_value = {'submission_artifact_ref': {'sha256': 'receipt'}}
    report = Mock(side_effect=OSError('report disk full'))
    if stage == 'submit':
        store.mark_submitted.side_effect = TimeoutError('submission reply lost')
    monkeypatch.setattr(verifier, '_store_context', Mock(return_value=(object(), store)))
    for name, result in {
        'recorded_cases': [{'name': 'smoke'}], 'findings_from_work_items': [],
        'assert_work_items_complete': {'version': 1, 'items': []},
        '_validate_case_references': None, 'verification_corpus_snapshot': {},
        '_default_summary': 'Checked', '_internal_context': {},
        '_policy_exceptions': [], 'validate_semantic_verification_plan_shape': None,
        '_preflight_verification_submission': [],
    }.items():
        monkeypatch.setattr(verifier, name, Mock(return_value=result))
    monkeypatch.setattr(verifier, '_case_declaration', lambda value: value)
    monkeypatch.setattr(verifier, '_write_bunshin_artifact', report)
    produced = []
    result = asyncio.run(verifier.verification_builder_tool_result(
        call('op_bunshin_verification_submit'), {}, produced))
    assert not result.ok
    assert result.invocation_result.effect.value == effect
    assert result.invocation_result.retry.value == retry
    assert produced == []
    if stage == 'report':
        assert 'already accepted' in result.llm_text
        report.assert_called_once()
    else:
        report.assert_not_called()


def test_preflight_io_failure_is_not_bad_input(monkeypatch):
    def unavailable(*args, **kwargs):
        try:
            raise OSError('contract storage unavailable')
        except OSError as exc:
            raise ValueError('cannot read contract') from exc
    monkeypatch.setattr(verifier, '_preflight_verification_case_execution', unavailable)
    result = asyncio.run(verifier.verification_builder_tool_result(
        call('op_bunshin_verification_run_adversarial_case'), {}, []))
    assert result.invocation_result.kind == 'failed'
    assert result.invocation_result.effect.value == 'not_started'
    assert result.invocation_result.retry.value == 'safe'
    assert 'contract storage unavailable' in result.llm_text
    assert 'Correct the listed prerequisites' not in result.llm_text
