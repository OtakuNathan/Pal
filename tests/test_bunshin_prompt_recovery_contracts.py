from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from pal.core import PalCore
from pal.bunshin.prompt_adapter import render_bunshin_task_prompt
from pal.bunshin.runner_components.agent_runtime import AgentRuntime
from pal.bunshin.runner_components.llm_rounds import LlmRounds
from pal.bunshin.runner_components.prompt_context import PromptContext
from pal.core.turns import EffectResult
from pal.llm.ir import GenerationPolicyIR, LLMRequestIR
from pal.shared import BunshinInvocationPack, LLMFinishReason, RuntimeStatus
from pal.bunshin.runner_components.llm_settings import _bunshin_generation_result
from pal.web_fetch.browser_service import BrowserServiceError
from pal.web_fetch.capabilities import WebFetchIntrospectionProvider
from pal.execution.contracts import CapabilityCall
from pal.execution.tool_facade import ToolExecutionError


def pack(role):
    return BunshinInvocationPack(invocation_id='recovery-audit', goal='Complete the bound work',
                                metadata={'bunshin_v2': {'role': role}})


@pytest.mark.parametrize('role', ['architect', 'implementation', 'reviewer', 'verifier'])
def test_truncation_recovery_matches_the_bound_role_and_refreshes_old_notes(role):
    value = pack(role)
    events = []

    async def emit_progress(*args, **kwargs):
        events.append((args, kwargs))

    rounds = LlmRounds(completion=None, control=None, heartbeat=None, prompt_context=None,
                       reporter=SimpleNamespace(emit_progress=emit_progress),
                       status=None, text_deliverables=None, tool_session=None, pack=value)
    state = SimpleNamespace(llm_round_count=1, output_length_recovery_count=0,
                            pending_output_length_recovery_note='')
    outcome = _bunshin_generation_result('unfinished reasoning', finish_reason=LLMFinishReason.LENGTH)
    asyncio.run(rounds.postprocess_bunshin_llm_round(
        state, EffectResult(status=RuntimeStatus.OK, payload=outcome)))
    assert state.llm_round_count == 0
    assert state.output_length_recovery_count == 1
    note = state.pending_output_length_recovery_note
    assert 'Read missing decisive evidence' in note
    assert 'write the smallest compiling/valid scaffold' not in note
    assert 'prerequisites are satisfied' in note
    if role in {'reviewer', 'verifier'}:
        assert 'do not repair the candidate' in note or 'do not implement or repair the candidate' in note
    # A pre-upgrade checkpoint must not restore the old role-inappropriate text.
    state.pending_output_length_recovery_note = 'write the smallest compiling/valid scaffold'
    assert rounds.build_bunshin_retry_note(outcome, [], 1, state=state) == note


@pytest.mark.parametrize('role', ['architect', 'implementation', 'reviewer', 'verifier'])
@pytest.mark.parametrize('has_tools', [False, True])
def test_recovery_request_keeps_the_role_surface_and_allows_reading(monkeypatch, role, has_tools):
    # Exercise the callbacks used by the live agent runtime, not a separate filter.
    tools = [{'function': {'name': name}} for name in ('read_file', 'search_tools', 'submit_review')] if has_tools else []
    execution = SimpleNamespace(build_llm_tool_contracts=lambda: tools)
    state = SimpleNamespace(execution_runtime=execution, memory_service=None,
                            pending_output_length_recovery_note='checkpoint recovery', llm_round_count=1)
    captured = {}

    def build(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr('pal.bunshin.runner_components.agent_runtime.AgentTurnRuntime.build', build)
    runtime = AgentRuntime(
        prompt_context=SimpleNamespace(prompt_scaffold=lambda: {}, render_durable_role_context=lambda: ''),
        reporter=SimpleNamespace(), tool_observation=SimpleNamespace(heartbeat=None),
        pack=pack(role), run_id='audit')
    runtime.build_bunshin_agent_runtime(SimpleNamespace(llm_runtime=None, config=None), state, None)
    assert captured['build_llm_tool_contracts']() == tools
    # LLMRequestIR itself does not transform the supplied tool records here.
    request = LLMRequestIR(messages=(), tools=tuple(tools), policy=GenerationPolicyIR(max_output_tokens=100))
    adapted = captured['request_adapter'](request)
    assert adapted.tools == request.tools
    assert adapted.policy.tool_choice == ('required' if has_tools else request.policy.tool_choice)


@pytest.mark.parametrize('role', ['architect', 'implementation', 'reviewer', 'verifier'])
def test_role_boundary_and_retry_rules_in_the_rendered_task(role):
    rendered = render_bunshin_task_prompt(pack(role))
    if role == 'architect':
        assert 'Author or revise only the bound architecture contract' in rendered
        assert 'Do not create architecture' not in rendered
    else:
        assert 'Do not create or revise architecture outside the bound role' in rendered
    assert 'Do not invent acceptance authority' in rendered
    assert 'retry=safe permits a bounded retry with the same arguments' in rendered
    assert 'after any stated wait or readiness condition' in rendered
    assert 'retry=correct_input requires correcting the input' in rendered
    assert 'retry=do_not_retry forbids' in rendered
    assert 'requires reconciliation before retrying a mutation' in rendered


@pytest.mark.parametrize('code,retryable,unknown,expected_retry', [
    ('dependency_installing', True, False, 'safe'),
    ('dependency_install_failed', False, False, 'do_not_retry'),
    ('navigation_failed', True, True, 'reconcile_first'),
    ('browser_unavailable', False, False, 'do_not_retry'),
])
def test_worker_browser_errors_preserve_cause_but_route_recovery_to_available_authority(code, retryable, unknown, expected_retry):
    def execute(**kwargs):
        raise BrowserServiceError('specific browser cause', code=code, retryable=retryable, state_unknown=unknown)

    provider = WebFetchIntrospectionProvider(service=SimpleNamespace(execute=execute))
    for worker in (False, True):
        meta = {'broker_run_id': 'role-attempt'} if worker else {'turn_id': 'resident-turn'}
        with pytest.raises(ToolExecutionError) as caught:
            provider.read(CapabilityCall(name='read', args={'url': 'https://example.com'}, meta=meta))
        error = caught.value
        assert error.error_code == code
        assert error.retry.value == expected_retry
        assert 'specific browser cause' in str(error)
        if worker:
            assert 'Manager' in error.recovery_hint
            assert 'inspect_browser_status' not in error.recovery_hint
            assert 'curl' not in error.recovery_hint
        elif code == 'dependency_installing':
            assert 'inspect_browser_status' in error.recovery_hint


@pytest.mark.parametrize('original', [
    'Manager validates and records the bound files after the harness finishes.',
    'call submit_contract with no arguments and wait for successful Manager acceptance before finishing.',
    'call submit_contract with an empty argument object ({}) and wait for successful Manager acceptance before finishing.',
    'Custom instructions: call submit_contract when ready.',
    'Custom instructions: complete the declared output files.',
])
def test_current_legacy_and_custom_architect_contracts_receive_one_submission_rule(original):
    value = pack('architect')
    value.metadata['bunshin_v2']['submission_receipt_required'] = True
    value.resolved_profile['output_contract_fragment'] = original
    context = PromptContext(SimpleNamespace(visible_capability_aliases=['submit_contract']), value, Path('/tmp'))
    result = context.output_contract()
    assert result.count('submit_contract') == 1
    assert result.count('durable submission receipt') == 1
    assert 'after the harness finishes' not in result
    assert 'A final response does not submit the files' in result
    assert value.resolved_profile['output_contract_fragment'] == original
    if original.startswith('Custom'):
        assert original in result
