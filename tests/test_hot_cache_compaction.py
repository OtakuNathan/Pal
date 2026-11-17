from tests.core_factory import build_core
from tests.cache_reminder_factory import attach_cache_reminder
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from pal.cache_reminder.cache_warm_deadline import CacheWarmDeadlineManager
from pal.core.runtime import PalCore
from pal.llm import generation_result_from_values
from pal.llm.ir import LLMFinishReason
from tests.test_cache_warm_deadline import _ControlledTimers, _Settings, _route, _snapshot
from tests.test_runtime_compaction import _ScriptedLLM, _attach_hot_cache, _memory_with_turns, _valid_pal_payload


async def discard(notice):
    return True


def manager_for(snapshot, *, busy=lambda: False):
    sleep = _ControlledTimers()
    manager = CacheWarmDeadlineManager(
        cache_snapshot=lambda: snapshot, settings_provider=lambda: _Settings(),
        has_active_turn=busy, deliver_notice=discard, expire_notice=discard, timers=sleep,
    )
    return manager, sleep


def test_timer_restarts_full_idle_interval_after_interaction():
    async def scenario():
        live = {**_snapshot(), 'anchor_remaining_ttl_seconds': 20}
        manager, sleep = manager_for(live)
        assert manager.schedule_after_turn_commit(route=_route(), turn_id='one')
        await asyncio.sleep(0)
        assert sleep.calls == [60, 90]
        await manager.clear_for_user_activity()
        assert manager.schedule_after_turn_commit(route=_route(), turn_id='two')
        await asyncio.sleep(0)
        assert sleep.calls == [60, 90, 60, 90]
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
            manager._active_notice = replace(manager._active_notice, expires_at=(datetime.now(timezone.utc) - timedelta(seconds=1)))
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
    core = build_core()
    service = _memory_with_turns(2)
    live = _attach_hot_cache(llm, service)
    core.context.port_registry['llm:llm'] = llm
    return core.turn_executor, service, live


def compact(executor, service):
    return asyncio.run(executor.compaction.run(
        service, target_input_budget=8192, reserved_output_tokens=2048,
        replay_guard=lambda: bool(executor.llm.current.prompt_cache_warm_deadline_snapshot().get('eligible')
            and executor.llm.current.prompt_cache_warm_deadline_snapshot().get('anchor_epoch') == 'epoch-a'
            and executor.llm.current.prompt_cache_warm_deadline_snapshot().get('anchor_remaining_ttl_seconds', 0) > 0),
        max_attempts=3,
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
    generate = llm.respond
    async def expires(request):
        live['anchor_remaining_ttl_seconds'] = 0
        return await generate(request)
    llm.respond = expires
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

    core = build_core()
    core.turn_manager.latest_active_turn_id = Mock(return_value='busy')
    manager = attach_cache_reminder(core)
    manager.claim_compaction = Mock()
    core._complete_compact_reply_async = AsyncMock()
    action = ControlAction(
        action_kind='cache_warm_deadline_compact', target_scope='cache_reminder', route=_route(),
        args={'cache_epoch': 'epoch-a'},
    )
    core._deliver_control_delivery_async = AsyncMock(return_value=True)
    asyncio.run(core.handle_control_action_async(action))
    manager.claim_compaction.assert_not_called()
    core._deliver_control_delivery_async.assert_awaited_once()
