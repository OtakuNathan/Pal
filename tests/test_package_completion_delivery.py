import asyncio
from types import SimpleNamespace

import pytest

from pal.core import PalCore
from pal.core.core_events import TURN_TOOL_RESULT_COMMITTED
from pal.execution import register_with_core
from pal.packages.notifications import PackageCompletionSource
from pal.plugins.capabilities import register_with_core as register_plugins
from pal.shared import EndpointConfig, ResponseHandle, TurnDeliveryBinding
from pal.shared.tool_protocol import new_tool_call


@pytest.mark.parametrize('notify_first', [False, True])
@pytest.mark.parametrize('delivery', ['complete', 'failed', 'incomplete', 'other_scope'])
def test_only_committed_terminal_result_in_owning_scope_consumes_event(notify_first, delivery):
    async def run():
        core = PalCore()
        register_with_core(core.context)
        core.context.port_registry['core:core'] = core
        def append(*args):
            if delivery == 'failed':
                raise RuntimeError('persist failed')
        core.context.port_registry['memory:memory'] = SimpleNamespace(
            begin_l1_turn=lambda *args, **kw: None, append_l1_tool_result=append)
        def binding(scope):
            return TurnDeliveryBinding(EndpointConfig('tg', 'telegram', 'tg'),
                                       ResponseHandle('tg', {'chat_id': scope}), scope)
        opening = SimpleNamespace(delivery_binding=binding('original'), turn_id='opening')
        current = SimpleNamespace(delivery_binding=binding('other' if delivery == 'other_scope' else 'original'),
                                  turn_id='current')
        core.state.active_turns.update(opening=opening, current=current)
        source = PackageCompletionSource(core.context)
        notify = source.notifier('opening')
        state = {'job_id': 'job', 'status': 'ready', 'finished_at': 123, 'result': {'activation': 'attached'}}
        tool_call = new_tool_call(name='call_tool', args={'name': 'package_status', 'args': {'job_id': 'job'}})
        handle = register_plugins(core.context, SimpleNamespace(context=core.context))
        handle.introspection_provider.completion_events = source
        handle.introspection_provider.package_jobs = SimpleNamespace(
            status=lambda **kw: {'jobs': [state]}, shutdown=lambda: None)
        core.publish_module_capabilities('execution')
        core.publish_module_capabilities('plugins')
        try:
            if notify_first:
                notify(state)
                core.state.active_turns.clear()
                event, = source.drain(core.context)
                core.state.active_turns.update(opening=opening, current=current)
            result = await core.context.execution_runtime.execute_tool_async(tool_call, turn_id='current')
            assert result.ok, result.llm_text
            assert ('current', tool_call.call_id) in source.deliveries
            if delivery == 'incomplete':
                # Failed/budgeted delivery cannot consume a complete observation.
                core.context.core_event_bus.emit(TURN_TOOL_RESULT_COMMITTED,
                    {'turn_id': 'current', 'call_id': tool_call.call_id, 'complete': False})
            else:
                try:
                    await core.turn_executor._append_l1_tool_result_async(current, tool_call, result)
                except RuntimeError:
                    assert delivery == 'failed'
            if not notify_first:
                notify(state)
            assert ('job' not in source.pending) is (delivery == 'complete')
            if notify_first and delivery == 'complete':
                assert 'job' not in source.claimed
                await source.handle(event, core.context)
                assert not source.tasks
            notify(state)
            assert ('job' not in source.pending) is (delivery == 'complete')
        finally:
            source.close()
            core.context.execution_runtime.shutdown()
    asyncio.run(run())
