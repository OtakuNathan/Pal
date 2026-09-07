import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pal.core.cache_warm_deadline import CacheWarmDeadlineManager
from pal.core.runtime import PalCore
from pal.llm import generation_result_from_values
from pal.llm.ir import LLMFinishReason
from tests.test_cache_warm_deadline import _ControlledSleep, _Settings, _route, _snapshot
from tests.test_runtime_compaction import _ScriptedLLM, _attach_hot_cache, _memory_with_turns, _valid_pal_payload


async def discard(notice):
    return True


def manager_for(snapshot, *, busy=lambda: False):
    sleep = _ControlledSleep()
    manager = CacheWarmDeadlineManager(
        cache_snapshot=lambda: snapshot, settings_provider=lambda: _Settings(),
        has_active_turn=busy, deliver_notice=discard, expire_notice=discard, sleep=sleep,
    )
    return manager, sleep


def test_timer_restarts_full_idle_interval_after_interaction():
    async def scenario():
        live = {**_snapshot(), 'anchor_remaining_ttl_seconds': 20}
        manager, sleep = manager_for(live)
        assert manager.schedule_after_turn_commit(route=_route(), turn_id='one')
        await asyncio.sleep(0)
        assert sleep.calls == [60]
        await manager.clear_for_user_activity()
        assert manager.schedule_after_turn_commit(route=_route(), turn_id='two')
        await asyncio.sleep(0)
        assert sleep.calls == [60, 60]
        manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('context_tokens,expected', [(500, False), (50000, True)])
def test_context_threshold_does_not_depend_on_previous_compaction_flag(context_tokens, expected):
    async def scenario():
        manager, _ = manager_for({**_snapshot(), 'prefix_tokens': 50000 if not expected else 500,
                                  'context_tokens': context_tokens})
        assert manager.schedule_after_turn_commit(route=_route(), turn_id='t') is expected
        manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('invalid', ['expired_notice', 'expired_cache', 'epoch_changed', 'small_context', 'busy'])
def test_claim_rechecks_lifetime_and_does_not_consume_invalid_notice(invalid):
    async def scenario():
        live = _snapshot()
        busy = [False]
        manager, sleep = manager_for(live, busy=lambda: busy[0])
        manager.schedule_after_turn_commit(route=_route(), turn_id='t')
        await asyncio.sleep(0)
        await sleep.release(0)
        if invalid == 'expired_notice':
            manager._active_notice = replace(manager._active_notice, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat())
        elif invalid == 'expired_cache':
            live['anchor_remaining_ttl_seconds'] = 0
        elif invalid == 'epoch_changed':
            live['anchor_epoch'] = 'epoch-b'
        elif invalid == 'small_context':
            live['context_tokens'] = 500
        else:
            busy[0] = True
        assert not manager.claim_compaction('epoch-a')
        assert manager.inspect()['consumed_epoch'] == ''
        manager.close()
    asyncio.run(scenario())


def hot_executor(llm):
    core = PalCore()
    service = _memory_with_turns(2)
    live = _attach_hot_cache(llm, service)
    core.context.port_registry['llm:llm'] = llm
    return core.turn_executor, service, live


def compact(executor, service):
    return asyncio.run(executor.compact_memory_async(
        service, target_input_budget=8192, reserved_output_tokens=2048,
        cache_epoch='epoch-a', max_attempts=3,
    ))


def test_hot_compact_stops_after_three_failures_without_changing_memory():
    llm = _ScriptedLLM([RuntimeError('offline')] * 4)
    executor, service, _ = hot_executor(llm)
    before = list(service.l1_store.items)
    result = compact(executor, service)
    assert not result.success
    assert result.attempts == 3
    assert len(llm.generate_requests) == 3
    assert list(service.l1_store.items) == before


def test_hot_compact_can_succeed_on_third_attempt():
    llm = _ScriptedLLM([RuntimeError('offline'), RuntimeError('offline'),
                        generation_result_from_values(text=_valid_pal_payload())])
    executor, service, _ = hot_executor(llm)
    result = compact(executor, service)
    assert result.success
    assert result.attempts == 3


def test_expired_hot_compact_never_generates_a_cold_request():
    llm = _ScriptedLLM([])
    executor, service, live = hot_executor(llm)
    live['anchor_remaining_ttl_seconds'] = 0
    before = list(service.l1_store.items)
    result = compact(executor, service)
    assert result.status == 'hot_cache_unavailable'
    assert llm.generate_requests == []
    assert list(service.l1_store.items) == before


def test_hot_compact_rechecks_ttl_between_attempts():
    llm = _ScriptedLLM([RuntimeError('offline')])
    executor, service, live = hot_executor(llm)
    generate = llm.agenerate
    async def expires(request):
        live['anchor_remaining_ttl_seconds'] = 0
        return await generate(request)
    llm.agenerate = expires
    result = compact(executor, service)
    assert result.status == 'hot_cache_unavailable'
    assert result.attempts == 1
    assert len(llm.generate_requests) == 1


def test_hot_compact_does_not_fall_back_after_context_rejection():
    llm = _ScriptedLLM([generation_result_from_values(text='', finish_reason=LLMFinishReason.COMPACT_REQUIRED)])
    executor, service, _ = hot_executor(llm)
    result = compact(executor, service)
    assert result.status == 'hot_cache_unavailable'
    assert result.attempts == 1
    assert len(llm.generate_requests) == 1


def test_hot_compact_rechecks_after_awaited_preflight():
    from pal.llm import LLMPreflightAdvice
    from pal.shared import LLMPreflightStatus

    llm = _ScriptedLLM([])
    executor, service, live = hot_executor(llm)
    before = list(service.l1_store.items)

    def expire_during_preflight(request):
        live['anchor_remaining_ttl_seconds'] = 0
        return LLMPreflightAdvice(status=LLMPreflightStatus.READY)

    llm.preflight_hook = expire_during_preflight
    result = compact(executor, service)
    assert result.status == 'hot_cache_unavailable'
    assert llm.generate_requests == []
    assert list(service.l1_store.items) == before


def test_busy_core_does_not_claim_compact_reminder():
    from unittest.mock import AsyncMock, Mock
    from pal.control.contracts import ControlAction

    core = PalCore()
    core.turn_manager.latest_active_turn_id = Mock(return_value='busy')
    core.cache_warm_deadline.claim_compaction = Mock()
    core._complete_compact_reply_async = AsyncMock()
    action = ControlAction(
        action_kind='compact_memory', target_scope='memory', route=_route(),
        args={'cache_epoch': 'epoch-a'},
    )
    asyncio.run(core._handle_compact_memory_async(action))
    core.cache_warm_deadline.claim_compaction.assert_not_called()
    core._complete_compact_reply_async.assert_awaited_once()
