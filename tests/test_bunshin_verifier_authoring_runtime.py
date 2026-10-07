"""Consumed verifier contracts and invocation-local authoring serialization."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from pal.bunshin.runner_components.agent_session import _build_scoped_execution_runtime
from pal.bunshin.runner_components.tool_session import ToolSession
from pal.bunshin.scoped_execution import BunshinScopedExecutionRuntime, BunshinScopedExecutionShellInput
from pal.bunshin.v2.submission_drafts import SubmissionDraftContext, SubmissionDraftStore
from pal.bunshin.v2.work_items import ADD_FINDING_CAPABILITY, REMOVE_FINDING_CAPABILITY, UPDATE_FINDING_CAPABILITY
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import (
    EffectOutcome, EffectReceipt, EmptyToolInput, StructuredToolOutput, ToolHandlerResult,
)
from pal.execution.tool_semantics import DIRECT_CONTROL, DIRECT_LOCAL_WRITE
from pal.shared import BunshinInvocationPack, ToolExecutionResult
from pal.shared.tool_protocol import new_tool_call
from tests.capability_fixture import mount_test_capability
from tests.test_bunshin_verifier_tool_feedback import payload, run_delta, verifier


EDIT_CAPABILITIES = (UPDATE_FINDING_CAPABILITY, REMOVE_FINDING_CAPABILITY)


async def _check_cancel():
    pass


def _contracts(runtime):
    return {item['function']['name']: item['function'] for item in runtime.build_llm_tool_contracts()}


def _mount(runtime, canonical, alias, handler, *, execution=DIRECT_LOCAL_WRITE, input_model=EmptyToolInput, examples=()):
    mount_test_capability(
        runtime, canonical_path=canonical, alias=alias,
        InputModel=input_model, OutputModel=StructuredToolOutput,
        handler=handler, execution=execution, examples=examples,
        metadata={'preserve_role_contract': True},
    )


def _completed(output=None):
    return ToolHandlerResult(
        output=output or {'changed': True}, llm_text='completed',
        effect_receipt=EffectReceipt(outcome=EffectOutcome.APPLIED, receipt={'completed': True}),
    )


@pytest.mark.parametrize('persisted', [False, True])
def test_consumed_finding_edit_contracts_hydrate_fresh_and_resumed_pack(verifier, persisted):
    _, workspace, _, _, current, _ = verifier
    allowed = [name for name in current.allowed_capabilities if name not in (*EDIT_CAPABILITIES, ADD_FINDING_CAPABILITY)]
    if not persisted:
        allowed.extend(EDIT_CAPABILITIES)
    else:
        allowed.append(ADD_FINDING_CAPABILITY)
        workspace['bunshin_v2']['verification_tool_contract']['contract_version'] = '1'
        workspace['bunshin_v2']['verification_tool_contract']['allowed_capabilities'] = [
            *[name for name in workspace['bunshin_v2']['verification_tool_contract']['allowed_capabilities']
              if name not in EDIT_CAPABILITIES], ADD_FINDING_CAPABILITY,
        ]
    profile = {'profile_id': 'pinned-verifier', 'effective_capability_policy': {'mode': 'profile_only'}}
    pack = BunshinInvocationPack(
        invocation_id=workspace['invocation_id'], workspace=deepcopy(workspace),
        allowed_capabilities=allowed, resolved_profile=profile,
        metadata={'agent_session': {'session_id': 'unchanged-session', 'prompt_pack_hash': 'unchanged-prompt'}},
    )
    before = deepcopy(pack.to_dict())
    sessions = ToolSession()
    session = SimpleNamespace(pack=pack, artifacts=SimpleNamespace(produced_artifacts=[]),
                              tool_session=sessions,
                              control=SimpleNamespace(request_architecture_clarification=None, raise_if_cancel_requested=_check_cancel))
    base = ExecutionRuntime()
    runtime = _build_scoped_execution_runtime(session, SimpleNamespace(execution_runtime=base), workspace, None)
    try:
        contracts = _contracts(runtime)
        assert {'update_finding', 'remove_finding'}.issubset(contracts)
        assert {'update_finding', 'remove_finding'}.issubset(sessions.visible_capability_aliases)
        assert 'add_finding' not in contracts and 'add_finding' not in sessions.visible_capability_aliases
        assert 'add_finding' not in runtime.registry_generation.search_records
        assert runtime.get_capability_spec('add_finding') is None
        assert all(spec['canonical_path'] != ADD_FINDING_CAPABILITY for spec in runtime.list_capability_specs())
        assert all(spec['name'] != 'add_finding' for spec in runtime.base_runtime.runtime.list_tool_specs())
        for alias in ('update_finding', 'remove_finding'):
            assert 'before submission' in contracts[alias]['description']
            assert 'read_verification_draft_status' in contracts[alias]['description']
            assert 'finding_id' in contracts[alias]['input_schema']['properties']
        assert contracts['update_finding']['input_schema']['properties']['expected_revision']['minimum'] == 0
        assert pack.to_dict() == before
        assert base.role_capabilities(allowed) == allowed
    finally:
        runtime.base_runtime.runtime.shutdown()
        base.shutdown()


def test_consumed_finding_aliases_edit_then_delete_current_completed_finding(verifier):
    from pal.bunshin.v2.work_items import findings_from_work_items

    _, workspace, _, call, _, _ = verifier
    created = payload(call('update_finding', finding_id='probe-assertion', expected_revision=0,
                           finding_kind='verification_defect', priority='p1',
                           summary='Current probe uses an incorrect assertion'))
    changed = payload(call('update_finding', finding_id=created['finding_id'],
                           expected_revision=created['revision'], finding_kind='verification_defect',
                           priority='p2', disposition='advisory', summary='Optional probe coverage improvement'))
    assert changed['finding_id'] == created['finding_id']
    assert changed['revision'] == created['revision'] + 1
    assert findings_from_work_items(workspace)[0]['summary'] == 'Optional probe coverage improvement'
    removed = payload(call('remove_finding', finding_id=changed['finding_id'],
                           expected_revision=changed['revision'], reason='Existing coverage already establishes this behavior'))
    assert removed['removed'] and findings_from_work_items(workspace) == []


@pytest.mark.parametrize('deny_legacy', [False, True])
def test_legacy_add_replays_only_persisted_permission_without_live_exposure(verifier, deny_legacy):
    _, source, _, _, _, _ = verifier
    workspace = deepcopy(source)
    workspace['bunshin_v2']['verification_tool_contract'].update(
        contract_version='1', allowed_capabilities=[ADD_FINDING_CAPABILITY])
    policy = {'deny_capabilities': [ADD_FINDING_CAPABILITY]} if deny_legacy else {}
    pack = BunshinInvocationPack(invocation_id=workspace['invocation_id'], workspace=deepcopy(workspace),
        allowed_capabilities=[ADD_FINDING_CAPABILITY], resolved_profile={'effective_capability_policy': policy})
    before = deepcopy(pack.to_dict())
    base = ExecutionRuntime()
    runtime = BunshinScopedExecutionRuntime(base, pack.allowed_capabilities, workspace, capability_policy=policy)
    try:
        assert 'add_finding' not in _contracts(runtime)
        assert 'add_finding' not in runtime.registry_generation.search_records
        assert runtime.get_capability_spec('add_finding') is None
        assert runtime.base_runtime.runtime._read_generation_tool(runtime.registry_generation, 'add_finding') is None
        assert all(item['name'] != 'add_finding' for item in runtime.base_runtime.runtime.list_tool_specs())
        call = new_tool_call(name='add_finding', call_id='persisted-legacy-call', args={
            'finding_kind': 'verification_defect', 'priority': 'p1', 'summary': 'Legacy probe assertion needs correction'})
        first = asyncio.run(runtime.execute_tool_async(call))
        if deny_legacy:
            assert not first.ok
            assert {'update_finding', 'remove_finding'}.isdisjoint(_contracts(runtime))
        else:
            assert {'update_finding', 'remove_finding'}.issubset(_contracts(runtime))
            original = payload(first)
            replay = payload(asyncio.run(runtime.execute_tool_async(call)))
            assert original['finding_id'] == replay['finding_id'] and original['revision'] == replay['revision']
            context = SubmissionDraftContext.from_workspace(workspace, draft_kind='work_items')
            draft = SubmissionDraftStore(workspace['runtime_root']).read(context)
            assert len([item for item in draft.payload['items'] if item.get('item_id') == original['finding_id']]) == 1
        assert pack.to_dict() == before
    finally:
        runtime.base_runtime.runtime.shutdown()
        base.shutdown()


@pytest.mark.parametrize('policy,missing', [
    ({'deny_capabilities': [UPDATE_FINDING_CAPABILITY]}, {'update_finding'}),
    ({'deny_capabilities': [REMOVE_FINDING_CAPABILITY]}, {'remove_finding'}),
    ({'deny_prefixes': ['op_bunshin_update_finding']}, {'update_finding'}),
    ({'deny_fragments': ['remove_finding']}, {'remove_finding'}),
])
def test_persisted_profile_denials_survive_consumed_contract_hydration(verifier, policy, missing):
    _, workspace, _, _, current, _ = verifier
    pack = BunshinInvocationPack(invocation_id=workspace['invocation_id'],
        allowed_capabilities=[*current.allowed_capabilities, *EDIT_CAPABILITIES],
        resolved_profile={'effective_capability_policy': policy})
    before = deepcopy(pack.to_dict())
    session = SimpleNamespace(pack=pack, artifacts=SimpleNamespace(produced_artifacts=[]),
        tool_session=ToolSession(), control=SimpleNamespace(request_architecture_clarification=None, raise_if_cancel_requested=_check_cancel))
    base = ExecutionRuntime()
    runtime = _build_scoped_execution_runtime(session, SimpleNamespace(execution_runtime=base), workspace, None)
    try:
        assert missing.isdisjoint(_contracts(runtime))
        for alias in missing:
            result = asyncio.run(runtime.execute_tool_async(new_tool_call(name=alias, args={})))
            assert not result.ok
        assert pack.to_dict() == before
    finally:
        runtime.base_runtime.runtime.shutdown()
        base.shutdown()


@pytest.mark.parametrize('role,remove_binding', [
    ('reviewer', False), ('implementation', False), ('verifier', True),
])
def test_finding_edit_aliases_require_bound_verifier(
    verifier, role, remove_binding,
):
    _, source, _, _, _, _ = verifier
    workspace = deepcopy(source)
    workspace['bunshin_v2']['role'] = role
    if remove_binding:
        workspace['bunshin_v2'].pop('fencing_token')
    allowed = [*EDIT_CAPABILITIES, ADD_FINDING_CAPABILITY]
    base = ExecutionRuntime()
    runtime = BunshinScopedExecutionRuntime(base, allowed, workspace)
    try:
        assert {'update_finding', 'remove_finding'}.isdisjoint(_contracts(runtime))
        if role == 'reviewer':
            assert 'add_finding' in _contracts(runtime)
    finally:
        runtime.base_runtime.runtime.shutdown()
        base.shutdown()


@pytest.mark.parametrize('binding', [
    {'role': 'verifier', 'workflow_id': ''},
    {'role': 'verifier', 'lease_resource_key': 'partial-lease'},
    {'role': 'verifier', 'authoring_input_fingerprint': 'partial-input'},
])
def test_partial_verifier_assignment_cannot_bypass_writer_guard(verifier, binding):
    _, source, _, _, _, _ = verifier
    workspace = deepcopy(source)
    workspace['bunshin_v2'] = binding
    effects = []
    base = ExecutionRuntime()
    _mount(base, 'op_file_write', 'write_file', lambda _args: effects.append('write') or _completed())
    runtime = BunshinScopedExecutionRuntime(base, ['op_file_write'], workspace)
    try:
        result = asyncio.run(runtime.execute_tool_async(new_tool_call(name='write_file', args={})))
        assert not result.ok and result.structured['reason'] == 'verification_authoring_closed'
        assert effects == []
    finally:
        runtime.base_runtime.runtime.shutdown()
        base.shutdown()


@pytest.mark.parametrize('allowed,expected_edits', [
    ([], set()),
    (['op_bunshin_verification_draft_status'], set()),
    ([ADD_FINDING_CAPABILITY, UPDATE_FINDING_CAPABILITY, 'op_bunshin_verification_draft_status'], {'update_finding'}),
])
def test_hydration_keeps_invocation_evidence_contract_filter(verifier, allowed, expected_edits):
    _, workspace, _, _, current, _ = verifier
    bound = deepcopy(workspace)
    bound['bunshin_v2']['verification_tool_contract'].update(contract_version='2', allowed_capabilities=allowed)
    base = ExecutionRuntime()
    runtime = BunshinScopedExecutionRuntime(base, current.allowed_capabilities, bound)
    try:
        contracts = _contracts(runtime)
        assert ('read_verification_draft_status' in contracts) == ('op_bunshin_verification_draft_status' in allowed)
        assert 'run_verification_diff_risk' not in contracts
        assert {'update_finding', 'remove_finding'}.intersection(contracts) == expected_edits
    finally:
        runtime.base_runtime.runtime.shutdown()
        base.shutdown()


def test_submit_serializes_corpus_writer_across_runtime_views_and_rechecks_freeze(verifier, monkeypatch):
    from pal.bunshin import scoped_execution
    from pal.bunshin.v2.swe_verification import verification_outcome_readiness

    _, workspace, probe, _, current, _ = verifier
    payload(run_delta(verifier))
    original_submit = scoped_execution.swe_verification_tool_result
    original_content = probe.read_text()
    order = []

    async def scenario():
        validated, release = asyncio.Event(), asyncio.Event()

        async def held_submit(call, bound, artifacts):
            snapshot = SubmissionDraftStore(workspace['runtime_root']).read(
                SubmissionDraftContext.from_workspace(workspace, draft_kind='verification'))
            assert verification_outcome_readiness(bound, snapshot.payload, outcome='pass')['ready']
            order.append('validated')
            validated.set()
            await release.wait()
            result = original_submit(call, bound, artifacts)
            assert result.ok, result.llm_text
            order.append('receipt')
            return result

        def write(_args):
            order.append('write')
            probe.write_text('assert False\n')
            return _completed()

        monkeypatch.setattr(scoped_execution, 'swe_verification_tool_result', held_submit)
        base = ExecutionRuntime()
        _mount(base, 'op_file_write', 'write_file', write)
        submitter = BunshinScopedExecutionRuntime(base, current.allowed_capabilities, workspace)
        writer = BunshinScopedExecutionRuntime(base, [*current.allowed_capabilities, 'op_file_write'], deepcopy(workspace))
        try:
            submit = asyncio.create_task(submitter.execute_tool_async(new_tool_call(name='submit_verification_pass', args={})))
            await validated.wait()
            edit = asyncio.create_task(writer.execute_tool_async(new_tool_call(name='write_file', args={})))
            await asyncio.sleep(0)
            assert not edit.done() and order == ['validated']
            release.set()
            submitted, rejected = await asyncio.wait_for(asyncio.gather(submit, edit), 5)
            assert submitted.ok and not rejected.ok
            assert rejected.structured['reason'] == 'verification_authoring_closed'
            assert order == ['validated', 'receipt'] and probe.read_text() == original_content
        finally:
            submitter.base_runtime.runtime.shutdown()
            writer.base_runtime.runtime.shutdown()
            base.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 10))


@pytest.mark.parametrize('canonical,alias,output', [
    ('op_exec_shell', 'run_shell', {'returncode': 0, 'stdout': '', 'stderr': ''}),
    ('op_lsp_diagnostics', 'read_lsp_diagnostics', {'status': 'ok', 'diagnostics_state': 'fresh', 'diagnostics': []}),
])
def test_direct_execution_records_receipt_before_waiting_submit_enters(verifier, monkeypatch, canonical, alias, output):
    from pal.bunshin import scoped_execution
    from pal.bunshin.v2 import verification_readiness

    _, workspace, _, _, current, _ = verifier
    payload(run_delta(verifier))
    order = []
    original_submit = scoped_execution.swe_verification_tool_result
    original_record = verification_readiness.record_verification_execution

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()

        async def execute(_args):
            order.append('execution')
            started.set()
            await release.wait()
            return _completed(output)

        base = ExecutionRuntime()
        _mount(base, canonical, alias, execute, execution=DIRECT_CONTROL)
        runtime = BunshinScopedExecutionRuntime(base, [*current.allowed_capabilities, canonical], workspace)

        def record(*args):
            assert runtime._authoring_lock.locked()
            original_record(*args)
            order.append('execution receipt')

        def submit(*args):
            assert order == ['execution', 'execution receipt']
            order.append('submit')
            return original_submit(*args)

        monkeypatch.setattr(verification_readiness, 'record_verification_execution', record)
        monkeypatch.setattr(scoped_execution, 'swe_verification_tool_result', submit)
        try:
            if canonical == 'op_exec_shell':
                description = _contracts(runtime)[alias]['description']
                assert 'Complete corpus-writing commands in the foreground' in description
                assert 'external filesystem edits are outside' in description
            execution = asyncio.create_task(runtime.execute_tool_async(new_tool_call(name=alias, args={}, call_id='direct')))
            await started.wait()
            submitted = asyncio.create_task(runtime.execute_tool_async(new_tool_call(name='submit_verification_pass', args={})))
            await asyncio.sleep(0)
            assert not submitted.done()
            release.set()
            results = await asyncio.wait_for(asyncio.gather(execution, submitted), 5)
            assert all(result.ok for result in results), [result.llm_text for result in results]
            assert order == ['execution', 'execution receipt', 'submit']
            assert len([ref for ref in workspace['review_tool_evidence_refs'] if ref['call_id'] == 'direct']) == 1
        finally:
            runtime.base_runtime.runtime.shutdown()
            base.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_writer_first_invalidates_pass_before_receipt_acceptance(verifier):
    _, workspace, probe, _, current, _ = verifier
    payload(run_delta(verifier))

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()

        async def write(_args):
            entered.set()
            await release.wait()
            probe.write_text('assert 7 + 7 == 14\n')
            return _completed()

        base = ExecutionRuntime()
        _mount(base, 'op_file_write', 'write_file', write)
        runtime = BunshinScopedExecutionRuntime(base, [*current.allowed_capabilities, 'op_file_write'], workspace)
        try:
            edit = asyncio.create_task(runtime.execute_tool_async(new_tool_call(name='write_file', args={})))
            await entered.wait()
            submit = asyncio.create_task(runtime.execute_tool_async(new_tool_call(name='submit_verification_pass', args={})))
            await asyncio.sleep(0)
            assert not submit.done()
            release.set()
            written, rejected = await asyncio.wait_for(asyncio.gather(edit, submit), 5)
            assert written.ok and not rejected.ok
            assert 'fresh' in rejected.llm_text.lower() or 'stale' in rejected.llm_text.lower()
            store = SubmissionDraftStore(workspace['runtime_root'])
            context = SubmissionDraftContext.from_workspace(workspace, draft_kind='verification')
            assert store.read(context).status == 'active'
        finally:
            runtime.base_runtime.runtime.shutdown()
            base.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_nested_semantic_execution_uses_outer_lock_without_deadlock(verifier, monkeypatch):
    fixture, workspace, _, _, runtime, _ = verifier
    original = fixture.adapter.execute_tool_async
    observed = []

    async def nested(call, **kwargs):
        assert runtime._authoring_lock.locked()
        observed.append(call.name)
        return await original(call, **kwargs)

    monkeypatch.setattr(fixture.adapter, 'execute_tool_async', nested)
    result = asyncio.run(asyncio.wait_for(runtime.execute_tool_async(new_tool_call(
        name='run_verification_diff_risk', args={'name': 'delta', 'command': 'true'}, call_id='nested',
    )), 5))
    assert payload(result)['case']['status'] == 'PASS'
    assert observed == ['op_exec_shell']
    assert len(workspace['review_tool_evidence_refs']) == 1


@pytest.mark.parametrize('semantic', [False, True])
def test_native_session_completion_and_receipt_stay_inside_authoring_lock(verifier, semantic):
    from pal.bunshin.runner import BunshinRunner

    fixture, workspace, _, _, current, _ = verifier
    if not semantic:
        payload(run_delta(verifier))

    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        cancellation_checks = []

        async def check_cancel():
            cancellation_checks.append('checked')

        class Sessions:
            has_work = False
            delegations = 0

            def handles_capability(self, name):
                return name == 'op_exec_shell'

            def execution_delegate(self, inner, cancel):
                self.delegations += 1
                owner = self

                class Delegate:
                    async def execute_tool_async(self, call, **kwargs):
                        assert call.name == 'run_shell'
                        launched = await inner.execute_tool_async(call, **kwargs)
                        assert launched.ok and launched.structured['status'] == 'running'
                        owner.has_work = True
                        started.set()
                        await release.wait()
                        await cancel()
                        owner.has_work = False
                        return ToolExecutionResult(name=call.name, call_id=call.call_id, ok=True,
                            text='native execution finished', llm_text='native execution finished',
                            structured={'returncode': 0, 'stdout': '', 'stderr': ''})

                return Delegate()

        base = ExecutionRuntime()
        _mount(base, 'op_exec_shell', 'run_shell', lambda _args: _completed({'status': 'running'}),
               execution=DIRECT_CONTROL, input_model=BunshinScopedExecutionShellInput, examples=({'cmd': 'true'},))
        sessions = Sessions()
        runtime = BunshinScopedExecutionRuntime(base, [*current.allowed_capabilities, 'op_exec_shell'], workspace,
            role_execution_sessions=sessions, check_cancel=check_cancel)
        pack = BunshinInvocationPack(invocation_id=workspace['invocation_id'], workspace=workspace,
                                    allowed_capabilities=list(runtime.allowed_capabilities))
        async def noop(*_args, **_kwargs):
            pass
        runner = BunshinRunner(runtime_root=fixture.runtime_root, pack=pack, bunshin_id='native', run_id='native',
                               write_event=noop, read_decision=noop)
        runner.components.tool_session.bind_execution(sessions)
        refs = runner.components.review_evidence.review_tool_evidence_refs
        refs.extend(workspace['review_tool_evidence_refs'])
        runtime.workspace['review_tool_evidence_refs'] = refs
        before_count = len(refs)
        try:
            execution_call = new_tool_call(
                name='run_verification_diff_risk' if semantic else 'run_shell',
                args={'name': 'current delta', 'command': 'true'} if semantic else {'cmd': 'true'}, call_id='native-run')
            execution = asyncio.create_task(runner.components.tool_execution.execute_allowed_tool(runtime, execution_call))
            await started.wait()
            assert runtime._authoring_lock.locked()
            submitted = asyncio.create_task(runtime.execute_tool_async(new_tool_call(name='submit_verification_pass', args={})))
            await asyncio.sleep(0)
            assert not submitted.done() and len(refs) == before_count
            release.set()
            completed, receipt = await asyncio.wait_for(asyncio.gather(execution, submitted), 5)
            assert completed.ok and receipt.ok, (completed.llm_text, receipt.llm_text)
            assert len(refs) == before_count + 1 and refs[-1]['ok']
            assert sessions.delegations == 1 and cancellation_checks == ['checked']
        finally:
            runtime.base_runtime.runtime.shutdown()
            base.shutdown()

    asyncio.run(asyncio.wait_for(scenario(), 10))


def test_nonverifier_keeps_outer_native_delegation_and_review_receipts(verifier):
    from pal.bunshin.runner import BunshinRunner

    fixture, source, _, _, _, _ = verifier
    workspace = deepcopy(source)
    workspace['bunshin_v2'].update(role='reviewer', mode='standalone')
    delegated = []

    class Sessions:
        has_work = False

        def handles_capability(self, name):
            return name == 'op_exec_shell'

        def execution_delegate(self, inner, _cancel):
            delegated.append(inner)
            return inner

    base = ExecutionRuntime()
    _mount(base, 'op_exec_shell', 'run_shell', lambda _args: _completed({'returncode': 0}),
           execution=DIRECT_CONTROL, input_model=BunshinScopedExecutionShellInput, examples=({'cmd': 'true'},))
    sessions = Sessions()
    runtime = BunshinScopedExecutionRuntime(base, ['op_exec_shell'], workspace, role_execution_sessions=sessions)
    pack = BunshinInvocationPack(invocation_id=workspace['invocation_id'], workspace=workspace,
                                allowed_capabilities=['op_exec_shell'])
    async def noop(*_args, **_kwargs):
        pass
    runner = BunshinRunner(runtime_root=fixture.runtime_root, pack=pack, bunshin_id='reviewer', run_id='reviewer',
                           write_event=noop, read_decision=noop)
    runner.components.tool_session.bind_execution(sessions)
    try:
        result = asyncio.run(runner.components.tool_execution.execute_allowed_tool(runtime, new_tool_call(
            name='run_shell', args={'cmd': 'true'}, call_id='reviewer-run')))
        assert result.ok and delegated == [runtime]
        refs = runner.components.review_evidence.review_tool_evidence_refs
        assert len(refs) == 1 and 'verification_binding' not in refs[0]
    finally:
        runtime.base_runtime.runtime.shutdown()
        base.shutdown()


def test_lost_ack_freezes_execution_and_authoring_even_with_stale_displayed_status(verifier, monkeypatch):
    _, workspace, _, call, runtime, _ = verifier
    payload(run_delta(verifier))
    store = SubmissionDraftStore(workspace['runtime_root'])
    context = SubmissionDraftContext.from_workspace(workspace, draft_kind='verification')
    before = store.read(context)
    original = SubmissionDraftStore.mark_submitted

    def lose_ack(self, *args, **kwargs):
        original(self, *args, **kwargs)
        raise OSError('receipt committed, acknowledgement lost')

    monkeypatch.setattr(SubmissionDraftStore, 'mark_submitted', lose_ack)
    result = call('submit_verification_pass')
    assert not result.ok
    assert store.read(context).status == 'submitted'
    monkeypatch.setattr(SubmissionDraftStore, 'read', lambda *_args, **_kwargs: before)
    count = len(workspace['review_tool_evidence_refs'])
    for name, args in [
        ('run_verification_diff_risk', {'name': 'after submit', 'command': 'true'}),
        ('remove_verification_case', {'name': 'current delta', 'reason': 'withdraw'}),
        ('update_checklist', {'plan': [{'step': 'change audit', 'status': 'completed'}]}),
    ]:
        rejected = call(name, **args)
        assert not rejected.ok
        assert rejected.structured['reason'] == 'verification_authoring_closed'
    assert len(workspace['review_tool_evidence_refs']) == count


def test_pending_native_execution_prevents_submission(verifier):
    _, _, _, call, runtime, _ = verifier
    payload(run_delta(verifier))
    runtime.role_execution_sessions = SimpleNamespace(has_work=True, handles_capability=lambda _name: False)
    result = call('submit_verification_pass')
    assert not result.ok and result.structured['reason'] == 'verification_execution_pending'
    runtime.role_execution_sessions.has_work = False
    assert payload(call('submit_verification_pass'))['submitted']
