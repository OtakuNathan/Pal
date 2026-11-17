from __future__ import annotations
from tests.core_factory import build_core
from tests.turn_fakes import continuation as make_continuation

import asyncio
import threading
from types import SimpleNamespace

import pytest

from pal.core.runtime import PalCore
from pal.execution import register_with_core
from pal.execution.contracts import CapabilityCall
from pal.packages.jobs import PackageJobs
from pal.packages.notifications import PackageCompletionSource
from pal.packages.process import PackageError
from pal.plugins.capabilities import register_with_core as register_plugins
from pal.plugins.host import PluginHost
from pal.shared.tool_protocol import new_tool_call


def test_completion_program_returns_committable_turn_outcome():
    from pal.core.turns import EffectResult, LLMPreflightEffect, LLMRequestEffect, MailboxReplyEffect
    from pal.foundation import EventEnvelope
    from pal.llm.ir import LLMMessageIR, MessageRole, TextPartIR
    from pal.memory import L1MessageKind
    from pal.packages.notifications import completion_program
    from pal.shared import RuntimeStatus

    event = EventEnvelope(event_kind='packages.job.completed', source_kind='packages',
                          payload=LLMMessageIR(role=MessageRole.USER, parts=(TextPartIR('Job completed'),)))
    program = completion_program(event, core_mode='normal', max_output_tokens=1024)
    assert isinstance(next(program), LLMPreflightEffect)
    assert isinstance(program.send(EffectResult(status=RuntimeStatus.OK)), LLMRequestEffect)
    reply = program.send(EffectResult(status=RuntimeStatus.OK))
    assert isinstance(reply, MailboxReplyEffect)
    with pytest.raises(StopIteration) as stopped:
        program.send(EffectResult(status=RuntimeStatus.QUEUED))
    outcome = stopped.value.value
    assert outcome.turn_id == event.event_id
    assert outcome.final_reply == reply.text
    assert outcome.commit_payload.transcript[0].kind == L1MessageKind.RUNTIME_CONTEXT_ARTIFACT
    assert outcome.commit_payload.transcript[0].content == 'Job completed'
    assert outcome.commit_payload.transcript[-1].content == reply.text


@pytest.fixture
def jobs(tmp_path):
    core = build_core()
    manager = PackageJobs(PluginHost(core.context, tmp_path))
    manager.service = SimpleNamespace(status=lambda: {})
    yield manager
    manager.shutdown()
    core.close()
    core.context.execution_runtime.shutdown()


@pytest.mark.parametrize('failed', [False, True])
def test_short_job_returns_terminal_result_without_notification(jobs, failed):
    notices = []
    def prepare():
        if failed:
            raise PackageError('dependency missing')
        return {'activation': 'attached'}
    jobs.service.prepare = prepare
    result = jobs.start('prepare', wait_ms=1000, on_complete=notices.append)
    assert result['status'] == ('failed' if failed else 'ready')
    assert result['notification'] == 'not_needed'
    assert notices == []


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('notify_failed', [False, True])
def test_background_completion_notifies_once_and_preserves_operation_result(jobs, failed, notify_failed):
    release = threading.Event()
    notices = []
    def prepare():
        assert release.wait(3)
        if failed:
            raise PackageError('dependency missing')
        return {'activation': 'installed_inactive'}
    def notify(state):
        notices.append(state)
        if notify_failed:
            raise RuntimeError('channel unavailable')
    jobs.service.prepare = prepare
    result = jobs.start('prepare', on_complete=notify)
    assert result['status'] == 'running'
    assert result['notification'] == 'scheduled'
    assert not notices
    release.set()
    final = jobs.status(result['job_id'], wait_ms=1000)['jobs'][0]
    assert final['status'] == ('failed' if failed else 'ready')
    jobs.threads[result['job_id']][0].join(3)
    final = jobs.status(result['job_id'])['jobs'][0]
    assert final['notification'] == ('failed' if notify_failed else 'queued')
    assert len(notices) == 1
    assert 'next_step' not in final


def test_wait_without_delivery_binding_uses_same_job(jobs):
    release = threading.Event()
    calls = []
    def prepare():
        calls.append(1)
        assert release.wait(3)
        return {}
    jobs.service.prepare = prepare
    result = jobs.start('prepare')
    assert result['notification'] == 'unavailable'
    with pytest.raises(PackageError, match='requires job_id'):
        jobs.status(wait_ms=1)
    release.set()
    assert jobs.status(result['job_id'], wait_ms=1000)['jobs'][0]['status'] == 'ready'
    assert calls == [1]


@pytest.mark.parametrize('failed', [False, True])
@pytest.mark.parametrize('turn_failed', [False, True])
def test_completion_wakes_pal_once_when_idle_with_original_binding(failed, turn_failed):
    from pal.shared import EndpointConfig, ResponseHandle, TurnDeliveryBinding
    from unittest.mock import AsyncMock
    async def run():
        core = build_core()
        register_with_core(core.context)
        core.context.port_registry['core:core'] = core
        from tests.llm_fakes import NonStreamingLLM
        from pal.llm import generation_result_from_values
        from pal.memory.service import MemoryService
        class CompletionLLM(NonStreamingLLM):
            async def respond(self, request, **options):
                return generation_result_from_values(text="Package result processed.")
        memory = MemoryService()
        core.context.port_registry['memory:memory'] = memory
        core.context.port_registry['llm:llm'] = CompletionLLM()
        if turn_failed:
            from unittest.mock import Mock
            core.turn_manager.resume = Mock(side_effect=RuntimeError('turn execution failed'))
        binding = TurnDeliveryBinding(EndpointConfig('tg', 'telegram', 'tg'),
                                      ResponseHandle('tg', {'chat_id': 123}), 'scope')
        core.state.active_turns['opening'] = make_continuation(turn_id='opening', delivery_binding=binding)
        source = PackageCompletionSource(core.context)
        core.run_turn_continuation_async = AsyncMock(wraps=core.run_turn_continuation_async)
        try:
            assert source.notifier('missing') is None
            notify = source.notifier('opening')
            state = {'job_id': 'test', 'operation': 'install', 'status': 'failed' if failed else 'ready',
                     'result': {'id': 'demo', 'activation': 'installed_inactive'}}
            notify(state)
            notify(state)
            state['result']['activation'] = 'mutated'
            assert not source.prepare(core.context)  # Original turn is still busy.
            assert not memory.history.turns
            core.state.active_turns.clear()
            assert source.prepare(core.context)
            event, = source.drain(core.context)
            assert source.drain(core.context) == []
            # A user turn can win admission after drain; the observation stays queued.
            core.state.active_turns['another'] = SimpleNamespace()
            await source.handle(event, core.context)
            assert not memory.history.turns
            core.state.active_turns.clear()
            event, = source.drain(core.context)
            await source.handle(event, core.context)
            await asyncio.gather(*source.tasks)
            await asyncio.sleep(0)
            turn, = memory.history.turns
            messages = turn.messages
            assert messages
            assert 'installed_inactive' in messages[0].text and 'mutated' not in messages[0].text
            assert messages[0].semantic_kind == 'runtime_context_artifact'
            assert 'opening_turn_id' not in messages[0].text
            assert messages[0].metadata['opening_turn_id'] == 'opening'
            assert 'test' in messages[0].text
            continuation = core.run_turn_continuation_async.call_args.args[0]
            assert continuation.correlation_id == 'opening'
            assert continuation.delivery_binding is binding
            assert continuation.delivery_binding.response_handle.reply_target == {'chat_id': 123}
            assert not core.state.active_turns and not core.state.turn_tasks
            notify(state)
            assert source.drain(core.context) == []
            assert any(item.get('kind') == 'packages.job.completed.failed'
                       for item in core.state.diagnostics) is turn_failed
        finally:
            source.close()
            core.context.execution_runtime.shutdown()
    asyncio.run(run())


def test_closed_completion_source_does_not_enqueue_or_register_again():
    core = build_core()
    source = PackageCompletionSource(core.context)
    source.close()
    assert source.notifier('missing') is None
    assert not source.pending


@pytest.mark.parametrize('asynchronous', [False, True])
@pytest.mark.parametrize('canonical', [False, True])
def test_package_wait_allows_worker_activation_through_real_dispatch(tmp_path, asynchronous, canonical):
    core = build_core()
    register_with_core(core.context)
    host = PluginHost(core.context, tmp_path)
    register_plugins(core.context, host)
    for module in ('execution', 'plugins'):
        core.publish_module_capabilities(module)
    runtime = core.context.execution_runtime
    # Use the real package owner, replacing only the expensive installation.
    from unittest.mock import patch
    def prepare(service, **kwargs):
        with runtime.lifecycle_gate.write():
            return {'activation': 'attached'}
    try:
        with patch('pal.packages.service.PackageService.prepare', prepare):
            if canonical:
                call = CapabilityCall(name='op_plugin_package_prepare', args={'name': 'demo', 'wait_ms': 1000}, meta={})
                result = asyncio.run(runtime.call_registered_async(call)) if asynchronous else runtime.call_registered(call)
                payload = result.structured
            else:
                call = new_tool_call(name='call_tool', args={'name': 'prepare_package', 'args': {'name': 'demo', 'wait_ms': 1000}})
                result = asyncio.run(runtime.execute_tool_async(call)) if asynchronous else runtime.execute_tool(call)
                assert result.ok
                payload = result.structured
            assert payload['status'] == 'ready', payload
    finally:
        runtime.shutdown()


def test_completion_cancelled_before_start_clears_owner_state_without_opening_l1():
    from pal.memory.service import MemoryService
    from pal.shared import EndpointConfig, ResponseHandle, TurnDeliveryBinding

    async def run():
        core = build_core()
        register_with_core(core.context)
        memory = MemoryService()
        core.context.port_registry["memory:memory"] = memory
        binding = TurnDeliveryBinding(EndpointConfig("tg", "telegram", "tg"), ResponseHandle("tg", {}), "scope")
        core.state.active_turns["opening"] = make_continuation(turn_id="opening", delivery_binding=binding)
        source = PackageCompletionSource(core.context)
        try:
            source.notifier("opening")({"job_id": "cancelled", "status": "ready"})
            core.state.active_turns.clear()
            event, = source.drain(core.context)
            await source.handle(event, core.context)
            tasks = tuple(source.tasks)
            assert tasks
            source.close()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(0)
            assert not memory.history.turns
            assert not source.pending and not source.claimed and not source.started
            assert not core.state.active_turns and not core.state.turn_tasks
            assert core.state.scheduler.state.owner is None
        finally:
            source.close()
            core.close()
            core.context.execution_runtime.shutdown()
    asyncio.run(run())
