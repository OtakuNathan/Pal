from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from pal.control import ControlAction
from pal.control.interactions import cache_warm_deadline_delivery
from pal.core.cache_warm_deadline import CacheWarmDeadlineManager
from pal.core.compaction_coordinator import CompactionTrigger
from pal.core.runtime import PalCore
from pal.llm import cache_reminder
from pal.llm.ir import LLMUsageIR, WireShape
from pal.llm.prompt_cache import PromptCacheCoordinator
from pal.llm.runtime import EndpointResolver, LLMRuntime
from pal.llm.shapes.base import ShapeContext
from tests.test_cache_warm_deadline import _ControlledSleep, _Settings, _route, _capture
from tests.test_llm_runtime_ir import _endpoint, _Settings as _LLMSettings
from tests.test_prompt_cache_policy import _request


def _context(**changes):
    return replace(ShapeContext(
        wire_shape=WireShape.OPENAI_COMPLETION, endpoint_id="ep", model_id="test-model",
        provider_id="test", base_url="https://example.test",
    ), **changes)


def _snapshot(coordinator, plan):
    return coordinator.reminder_snapshot(
        logical_scope_id=plan.reminder.logical_scope_id, binding=plan.reminder.binding,
    )


def test_fallback_and_provider_model_rules(monkeypatch):
    assert cache_reminder.reminder_ttl("zhipu", "glm-5.3") == (1800, "fallback_policy")
    assert cache_reminder.reminder_ttl("Anthropic", "unknown") == (300, "provider_policy")
    monkeypatch.setattr(cache_reminder, "MODEL_TTL_SECONDS", {("anthropic", "custom"): 2400})
    assert cache_reminder.reminder_ttl("Anthropic", "custom") == (2400, "model_policy")
    assert cache_reminder.reminder_ttl("anthropic", "other") == (300, "provider_policy")
    assert cache_reminder.reminder_ttl("anthropic", "custom", requested_ttls=("1h", "5m")) == (300, "request_cache_ttl")


def test_only_successful_completion_starts_clock_and_duplicates_do_not_extend(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(cache_reminder, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    coordinator = PromptCacheCoordinator()
    request, context = _request(), _context()
    first = coordinator.plan(request, context)
    clock[0] += 600  # Time spent generating the response is not idle time.
    assert not _snapshot(coordinator, first)["eligible"]
    coordinator.record_attempt(first, status="failed", error="offline")
    coordinator.record_attempt(first, status="cancelled")
    assert not _snapshot(coordinator, first)["eligible"]
    coordinator.record_success(first, LLMUsageIR(input_tokens=50_000, reported_fields=("input_tokens",)))
    snapshot = _snapshot(coordinator, first)
    assert snapshot["anchor_remaining_ttl_seconds"] == 1800
    assert snapshot["context_tokens"] == 50_000
    assert snapshot["tokens_source"] == "provider_usage"
    assert snapshot["response_returned_at"].endswith("+00:00")
    clock[0] += 60
    coordinator.record_success(first, LLMUsageIR())
    assert _snapshot(coordinator, first)["anchor_remaining_ttl_seconds"] == 1740
    next_plan = coordinator.plan(request, context)
    coordinator.record_attempt(next_plan, status="failed", error="offline")
    assert _snapshot(coordinator, first)["anchor_remaining_ttl_seconds"] == 1740
    clock[0] += 20
    coordinator.record_success(next_plan, LLMUsageIR())
    refreshed = _snapshot(coordinator, next_plan)
    assert refreshed["anchor_epoch"] != snapshot["anchor_epoch"]
    assert refreshed["anchor_remaining_ttl_seconds"] == 1800
    clock[0] += 30
    coordinator.record_success(first, LLMUsageIR())  # Late older request.
    assert _snapshot(coordinator, next_plan)["anchor_remaining_ttl_seconds"] == 1770
    clock[0] += 1771
    assert not _snapshot(coordinator, next_plan)["eligible"]
    # An estimate is never evidence for the confirmed-hot entry point.
    assert not coordinator.warm_deadline_snapshot()["eligible"]
    assert coordinator.confirmed_anchor_request() == {}


@pytest.mark.parametrize("change", [
    {"endpoint_id": "other"}, {"model_id": "other"}, {"provider_id": "other"},
    {"base_url": "https://other.test"}, {"wire_shape": WireShape.OPENAI_RESPONSE},
])
def test_binding_and_worker_scopes_do_not_refresh_resident(change):
    coordinator = PromptCacheCoordinator()
    resident = coordinator.plan(_request(), _context())
    worker = coordinator.plan(replace(_request(), logical_scope_id="bunshin:one"), _context())
    coordinator.record_success(worker, LLMUsageIR())
    other = coordinator.plan(_request(), _context(**change))
    coordinator.record_success(other, LLMUsageIR())
    assert not _snapshot(coordinator, resident)["eligible"]
    assert _snapshot(coordinator, worker)["eligible"]
    assert _snapshot(coordinator, other)["eligible"]


def test_disabled_cache_and_bounded_metadata():
    coordinator = PromptCacheCoordinator(max_scope_count=2)
    disabled = coordinator.plan(_request(), _context(capabilities={"prompt_cache": {"enabled": False}}))
    assert disabled.reminder is None
    coordinator.record_success(disabled, LLMUsageIR())
    plans = [coordinator.plan(_request(), _context(endpoint_id=str(i))) for i in range(3)]
    for plan in plans:
        coordinator.record_success(plan, LLMUsageIR())
    assert not _snapshot(coordinator, plans[0])["eligible"]
    assert _snapshot(coordinator, plans[-1])["eligible"]
    assert _snapshot(coordinator, plans[-1])["tokens_source"] == "local_estimate"


def test_runtime_reads_only_current_endpoint_and_honors_disabled_policy():
    endpoint = _endpoint()
    coordinator = PromptCacheCoordinator()
    runtime = LLMRuntime(
        EndpointResolver(endpoints=(endpoint,)), _LLMSettings(),
        endpoint_invoker=SimpleNamespace(prompt_cache=coordinator),
    )
    plan = coordinator.plan(_request(), _context())
    assert not runtime.prompt_cache_reminder_snapshot()["eligible"]
    coordinator.record_success(plan, LLMUsageIR(input_tokens=50_000, reported_fields=("input_tokens",)))
    assert runtime.prompt_cache_reminder_snapshot()["eligible"]
    core = PalCore()
    core.context.port_registry["llm:llm"] = runtime
    assert core._prompt_cache_warm_deadline_snapshot()["estimated"]
    assert not runtime.prompt_cache_warm_deadline_snapshot()["eligible"]
    endpoint.base_url = "https://replacement.test"
    assert not runtime.prompt_cache_reminder_snapshot()["eligible"]
    endpoint.base_url = "https://example.test"
    assert runtime.prompt_cache_reminder_snapshot()["eligible"]
    endpoint.capabilities_blob = {"prompt_cache": {"enabled": False}}
    assert runtime.prompt_cache_reminder_snapshot()["reason"] == "cache_disabled"


def test_estimated_timer_uses_remaining_response_lifetime_and_one_shot_claim():
    async def scenario():
        sleep, delivered = _ControlledSleep(), []
        snapshot = {"eligible": True, "estimated": True, "anchor_epoch": "estimate",
                    "anchor_ttl_seconds": 1800, "anchor_remaining_ttl_seconds": 1500.5,
                    "context_tokens": 50_000}
        manager = CacheWarmDeadlineManager(
            cache_snapshot=lambda: snapshot, settings_provider=lambda: None,
            has_active_turn=lambda: False, deliver_notice=lambda notice: _capture(delivered, notice),
            expire_notice=lambda notice: _capture([], notice), sleep=sleep,
        )
        assert manager.schedule_after_turn_commit(route=_route(), turn_id="one")
        await asyncio.sleep(0)
        assert sleep.calls == [1200.5]  # 30m minus commit delay and 5m lead.
        await sleep.release(0)
        assert delivered[0].estimated
        assert manager.is_estimated_notice("estimate")
        assert not manager.is_estimated_notice("forged")
        assert manager.claim_compaction("estimate")
        assert not manager.claim_compaction("estimate")
        manager.close()
    asyncio.run(scenario())


def test_estimated_card_does_not_promise_cache_hit():
    delivery = cache_warm_deadline_delivery(
        _route(), epoch="estimate", prefix_tokens=50_000, lead_seconds=300,
        expires_at="2026-10-16T00:00:00+00:00", estimated=True,
    )
    assert "预计" in delivery.interaction.text
    assert "不保证缓存仍可复用" in delivery.interaction.text
    assert delivery.interaction.buttons[0][0].action_args == {"cache_epoch": "estimate"}


def test_estimated_click_uses_manual_compaction_without_hot_authorization():
    from pal.memory import register_with_core as register_memory
    from tests.test_runtime_compaction import _memory_with_turns
    from unittest.mock import AsyncMock

    async def scenario():
        core = PalCore()
        register_memory(core.context, _memory_with_turns(2))
        sleep = _ControlledSleep()
        snapshot = {"eligible": True, "estimated": True, "anchor_epoch": "estimate",
                    "anchor_ttl_seconds": 1800, "anchor_remaining_ttl_seconds": 1800,
                    "context_tokens": 50_000}
        manager = core.cache_warm_deadline
        manager.cache_snapshot = lambda: snapshot
        manager.settings_provider = lambda: _Settings()
        manager.sleep = sleep
        manager.deliver_notice = lambda notice: _capture([], notice)
        assert manager.schedule_after_turn_commit(route=_route(), turn_id="one")
        await asyncio.sleep(0)
        await sleep.release(0)
        calls = []

        async def compact(ticket, **kwargs):
            calls.append((ticket.trigger, kwargs))
            return SimpleNamespace(success=False, status="test_stop", user_notice="")

        core._run_control_compaction_async = compact
        core._deliver_control_delivery_async = AsyncMock(return_value=True)
        core._flush_control_status_async = AsyncMock()
        core._complete_compact_reply_async = AsyncMock()
        action = ControlAction(action_kind="compact_memory", target_scope="memory",
                               route=_route(), args={"cache_epoch": "estimate"})
        await core._handle_compact_memory_async(action)
        await core._handle_compact_memory_async(action)
        assert calls == [(CompactionTrigger.MANUAL, {"max_attempts": None, "cache_epoch": ""})]
        assert not manager.claim_compaction("estimate")
        manager.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["success", "failed", "cancelled"])
def test_offline_response_adapter_updates_reminder_only_after_success(outcome, tmp_path, monkeypatch):
    from pal.llm.shapes.base import _JSONFrame
    from tests.test_prompt_cache_v2_runtime import endpoint, response_payload, runtime

    clock = [100.0]
    monkeypatch.setattr(cache_reminder, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    class Transport:
        def frames(self, *_args):
            clock[0] += 600
            if outcome == "failed":
                raise TimeoutError("offline fixture")
            if outcome == "cancelled":
                raise asyncio.CancelledError()
            yield _JSONFrame(0, response_payload())

    llm = runtime(endpoint(), Transport(), tmp_path)
    assert not llm.prompt_cache_reminder_snapshot()["eligible"]
    if outcome == "success":
        llm.generate(_request())
        snapshot = llm.prompt_cache_reminder_snapshot()
        assert snapshot["eligible"]
        assert snapshot["anchor_remaining_ttl_seconds"] == 1800
        assert snapshot["context_tokens"] == 100_000
        core = PalCore()
        core.context.port_registry["llm:llm"] = llm
        assert core._prompt_cache_warm_deadline_snapshot()["anchor_epoch"] == snapshot["anchor_epoch"]
    elif outcome == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            llm.generate(_request())
        assert not llm.prompt_cache_reminder_snapshot()["eligible"]
    else:
        result = llm.generate(_request())
        assert result.response.finish_reason.value == "error"
        assert not llm.prompt_cache_reminder_snapshot()["eligible"]
