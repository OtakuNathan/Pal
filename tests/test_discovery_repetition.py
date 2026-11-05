"""Repeated discovery warns before the existing stagnation stop boundary."""
import asyncio
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from pal.core import PalCore
from pal.core.turns import ToolCallEffect
from pal.execution import register_with_core
from pal.shared.tool_protocol import ToolExecutionResult, new_tool_call
from tests.turn_fakes import continuation


@pytest.mark.parametrize('tool_name', ['search_tools', 'inspect_execution_state'])
def test_second_unchanged_search_warns_and_third_still_stops(tool_name):
    async def run():
        core = PalCore()
        register_with_core(core.context)
        core.publish_module_capabilities('execution')
        executor = core.turn_executor
        executor._ensure_not_interrupted = lambda _: None
        executor._log_tool_call_start = lambda *args: None
        executor._log_tool_call_result = lambda *args: None
        executor._append_l1_tool_result_async = AsyncMock()
        executor._append_l1_tool_context_messages_async = AsyncMock()
        executor._checkpoint_resident_async = AsyncMock()
        executor._after_tool_batch = None
        executor.include_live_execution_context = False
        owner_result = ToolExecutionResult(name=tool_name, ok=True, text='search results',
            structured={'hits': [], 'available_domains': ['shell']}, llm_text='original search results')
        execute = executor._execute_tool_async = AsyncMock(return_value=owner_result)
        turn = continuation(turn_id='repeat')
        try:
            for index in range(3):
                call = new_tool_call(name=tool_name, args={'domain': 'shell'}, call_id=f'call-{index}')
                turn.pending_tool_call_batch = [call]
                result = (await executor._handle_tool_call(ToolCallEffect(tool_call=call), turn)).payload
                warned = tool_name == 'search_tools' and index >= 1
                assert ('Discovery repetition:' in result.llm_text) == warned
                assert result.llm_text.startswith(owner_result.llm_text)
                assert result.structured == owner_result.structured
                assert result.ok == owner_result.ok
                assert executor._append_l1_tool_result_async.call_args.args[-1] is result
                assert turn.finalization_only == (index == 2)
            assert execute.await_count == 3
            history = core.turn_manager.guard.history_by_turn[turn.turn_id]
            assert len({item.result_fingerprint for item in history}) == 1
            assert owner_result.llm_text == 'original search results'
        finally:
            core.close()
    asyncio.run(run())


def test_changed_result_arguments_intervening_tool_and_new_turn_do_not_warn():
    async def run():
        core = PalCore()
        register_with_core(core.context)
        core.publish_module_capabilities('execution')
        executor = core.turn_executor
        executor._ensure_not_interrupted = lambda _: None
        executor._log_tool_call_start = lambda *args: None
        executor._log_tool_call_result = lambda *args: None
        original = ToolExecutionResult(name='search_tools', ok=True, text='search results',
            structured={'hits': [], 'available_domains': ['shell']}, llm_text='original search results')
        changed = replace(original, structured={'hits': [], 'available_domains': ['shell', 'browser']})
        execute = executor._execute_tool_async = AsyncMock()
        turn = continuation(turn_id='changed')
        cases = [
            ('search_tools', {'domain': 'shell'}, original, turn),
            ('search_tools', {'domain': 'shell'}, changed, turn),
            ('search_tools', {'domain': 'browser'}, changed, turn),
            ('read_tool', {'name': 'run_shell'}, original, turn),
            ('search_tools', {'domain': 'browser'}, changed, turn),
            ('search_tools', {'domain': 'browser'}, changed, continuation(turn_id='new')),
        ]
        try:
            for name, args, observation, current in cases:
                execute.return_value = observation
                result = await executor._handle_tool_call(
                    ToolCallEffect(tool_call=new_tool_call(name=name, args=args)), current)
                assert 'Discovery repetition:' not in result.payload.llm_text
                assert not current.finalization_only
            core.turn_manager.guard.clear(turn.turn_id)
            assert not core.turn_manager.guard.last_result_repeated(turn.turn_id)
        finally:
            core.close()
    asyncio.run(run())
