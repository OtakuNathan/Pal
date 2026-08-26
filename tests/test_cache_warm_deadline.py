from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from pal.control.contracts import ControlRoute, InteractionResult
from pal.control.interactions import (
    cache_warm_deadline_delivery,
    cache_warm_deadline_expire_delivery,
)
from pal.control.service import ControlPlane
from pal.core.cache_warm_deadline import CacheWarmDeadlineManager
from pal.core.capabilities import CoreIntrospectionProvider
from pal.core.runtime import PalCore
from pal.shared import IntrospectionCall, RuntimeStatus


@dataclass
class _Settings:
    values: dict[str, str] = field(
        default_factory=lambda: {
            "cache_warm_deadline_enabled": "true",
            "cache_warm_deadline_lead_seconds": "30",
            "cache_warm_deadline_min_prefix_tokens": "1024",
        }
    )

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> str:
        self.values[key] = value
        return value


@dataclass
class _ControlledSleep:
    calls: list[float] = field(default_factory=list)
    gates: list[asyncio.Event] = field(default_factory=list)

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        gate = asyncio.Event()
        self.gates.append(gate)
        await gate.wait()

    async def release(self, index: int) -> None:
        self.gates[index].set()
        await asyncio.sleep(0)
        await asyncio.sleep(0)


def _route() -> ControlRoute:
    return ControlRoute(
        endpoint_id="telegram_main",
        channel_kind="telegram",
        reply_target={"chat_id": "42"},
        control_scope_key="telegram:42",
    )


def _snapshot(epoch: str = "epoch-a") -> dict[str, object]:
    return {
        "eligible": True,
        "anchor_epoch": epoch,
        "anchor_ttl": "90s",
        "anchor_ttl_seconds": 90,
        "prefix_tokens": 50_000,
        "dialect": "openai_responses_explicit",
    }


def test_deadline_uses_anchor_ttl_then_expires_and_deletes_notice() -> None:
    asyncio.run(_deadline_uses_anchor_ttl_then_expires_and_deletes_notice())


async def _deadline_uses_anchor_ttl_then_expires_and_deletes_notice() -> None:
    settings = _Settings()
    sleep = _ControlledSleep()
    delivered = []
    expired = []
    manager = CacheWarmDeadlineManager(
        cache_snapshot=_snapshot,
        settings_provider=lambda: settings,
        has_active_turn=lambda: False,
        deliver_notice=lambda notice: _capture(delivered, notice),
        expire_notice=lambda notice: _capture(expired, notice),
        sleep=sleep,
    )

    assert manager.schedule_after_turn_commit(route=_route(), turn_id="turn-1")
    await asyncio.sleep(0)
    assert sleep.calls == [60]

    await sleep.release(0)
    assert len(delivered) == 1
    assert delivered[0].ttl_seconds == 90
    assert delivered[0].lead_seconds == 30
    assert len(sleep.calls) == 2
    assert 89 <= sleep.calls[1] <= 90

    await sleep.release(1)
    assert expired == delivered
    assert manager.inspect()["timer_scheduled"] is False


def test_new_user_activity_removes_notice_and_allows_same_epoch_again() -> None:
    asyncio.run(_new_user_activity_removes_notice_and_allows_same_epoch_again())


async def _new_user_activity_removes_notice_and_allows_same_epoch_again() -> None:
    settings = _Settings()
    sleep = _ControlledSleep()
    delivered = []
    expired = []
    manager = CacheWarmDeadlineManager(
        cache_snapshot=_snapshot,
        settings_provider=lambda: settings,
        has_active_turn=lambda: False,
        deliver_notice=lambda notice: _capture(delivered, notice),
        expire_notice=lambda notice: _capture(expired, notice),
        sleep=sleep,
    )
    assert manager.schedule_after_turn_commit(route=_route(), turn_id="turn-1")
    await asyncio.sleep(0)
    await sleep.release(0)
    assert len(delivered) == 1

    await manager.clear_for_user_activity()
    assert expired == delivered
    assert manager.schedule_after_turn_commit(route=_route(), turn_id="turn-2")
    manager.close()


def test_compaction_removes_visible_notice_without_rearming_epoch() -> None:
    asyncio.run(_compaction_removes_visible_notice_without_rearming_epoch())


async def _compaction_removes_visible_notice_without_rearming_epoch() -> None:
    sleep = _ControlledSleep()
    delivered = []
    expired = []
    manager = CacheWarmDeadlineManager(
        cache_snapshot=_snapshot,
        settings_provider=lambda: _Settings(),
        has_active_turn=lambda: False,
        deliver_notice=lambda notice: _capture(delivered, notice),
        expire_notice=lambda notice: _capture(expired, notice),
        sleep=sleep,
    )
    assert manager.schedule_after_turn_commit(route=_route(), turn_id="turn-1")
    await asyncio.sleep(0)
    await sleep.release(0)

    await manager.clear_for_compaction()

    assert expired == delivered
    assert not manager.schedule_after_turn_commit(
        route=_route(),
        turn_id="turn-2",
    )


def test_reminder_compaction_claim_is_one_shot_and_leaves_ui_to_action() -> None:
    asyncio.run(_reminder_compaction_claim_is_one_shot_and_leaves_ui_to_action())


async def _reminder_compaction_claim_is_one_shot_and_leaves_ui_to_action() -> None:
    sleep = _ControlledSleep()
    delivered = []
    expired = []
    manager = CacheWarmDeadlineManager(
        cache_snapshot=_snapshot,
        settings_provider=lambda: _Settings(),
        has_active_turn=lambda: False,
        deliver_notice=lambda notice: _capture(delivered, notice),
        expire_notice=lambda notice: _capture(expired, notice),
        sleep=sleep,
    )
    assert manager.schedule_after_turn_commit(route=_route(), turn_id="turn-1")
    await asyncio.sleep(0)
    await sleep.release(0)

    assert manager.claim_compaction("epoch-a")
    assert not manager.claim_compaction("epoch-a")
    await asyncio.sleep(0)

    assert expired == []
    assert manager.inspect()["timer_scheduled"] is False
    assert manager.inspect()["consumed_epoch"] == "epoch-a"


def test_active_turn_defers_notice_instead_of_dropping_it() -> None:
    asyncio.run(_active_turn_defers_notice_instead_of_dropping_it())


async def _active_turn_defers_notice_instead_of_dropping_it() -> None:
    settings = _Settings()
    sleep = _ControlledSleep()
    active = [True]
    delivered = []
    manager = CacheWarmDeadlineManager(
        cache_snapshot=_snapshot,
        settings_provider=lambda: settings,
        has_active_turn=lambda: active[0],
        deliver_notice=lambda notice: _capture(delivered, notice),
        expire_notice=_discard,
        sleep=sleep,
    )
    assert manager.schedule_after_turn_commit(route=_route(), turn_id="turn-1")
    await asyncio.sleep(0)

    await sleep.release(0)
    assert delivered == []
    assert sleep.calls[1] == 5.0

    active[0] = False
    await sleep.release(1)
    assert len(delivered) == 1
    manager.close()


def test_configuration_is_persistent_but_timer_state_is_not() -> None:
    settings = _Settings()
    manager = CacheWarmDeadlineManager(
        cache_snapshot=_snapshot,
        settings_provider=lambda: settings,
        has_active_turn=lambda: False,
        deliver_notice=_discard,
        expire_notice=_discard,
    )

    snapshot = manager.configure(
        enabled=False,
        lead_seconds=120,
        min_prefix_tokens=64_000,
    )

    assert snapshot["enabled"] is False
    assert snapshot["lead_seconds"] == 120
    assert snapshot["min_prefix_tokens"] == 64_000
    assert settings.values["cache_warm_deadline_enabled"] == "false"
    assert snapshot["timer_scheduled"] is False


def test_expired_anchor_remaining_ttl_does_not_fall_back_to_full_ttl() -> None:
    snapshot = {
        **_snapshot(),
        "anchor_remaining_ttl_seconds": 0,
    }
    manager = CacheWarmDeadlineManager(
        cache_snapshot=lambda: snapshot,
        settings_provider=lambda: _Settings(),
        has_active_turn=lambda: False,
        deliver_notice=_discard,
        expire_notice=_discard,
    )

    assert not manager.schedule_after_turn_commit(
        route=_route(),
        turn_id="turn-expired",
    )
    assert manager.inspect()["timer_scheduled"] is False


def test_disabling_from_capability_worker_cancels_timer_on_owner_loop() -> None:
    asyncio.run(_disabling_from_capability_worker_cancels_timer_on_owner_loop())


async def _disabling_from_capability_worker_cancels_timer_on_owner_loop() -> None:
    settings = _Settings()
    sleep = _ControlledSleep()
    manager = CacheWarmDeadlineManager(
        cache_snapshot=_snapshot,
        settings_provider=lambda: settings,
        has_active_turn=lambda: False,
        deliver_notice=_discard,
        expire_notice=_discard,
        sleep=sleep,
    )
    assert manager.schedule_after_turn_commit(route=_route(), turn_id="turn-1")
    await asyncio.sleep(0)

    configured = await asyncio.to_thread(manager.configure, enabled=False)
    await asyncio.sleep(0)

    assert configured["enabled"] is False
    assert manager.inspect()["timer_scheduled"] is False


def test_interaction_has_control_actions_and_delete_expiration() -> None:
    delivery = cache_warm_deadline_delivery(
        _route(),
        epoch="epoch-a",
        prefix_tokens=50_000,
        lead_seconds=300,
        expires_at="2026-08-24T15:00:00+00:00",
    )
    assert delivery.delivery_kind == "interactive_open"
    assert delivery.interaction is not None
    assert delivery.interaction.expires_at == "2026-08-24T15:00:00+00:00"
    buttons = [button for row in delivery.interaction.buttons for button in row]
    assert [button.label for button in buttons] == [
        "立即 Compact",
        "本轮忽略",
        "关闭此提醒",
    ]
    assert buttons[0].action_key == "control.compact.run"

    expired = cache_warm_deadline_expire_delivery(_route(), epoch="epoch-a")
    assert expired.delivery_kind == "interactive_expire"
    assert expired.payload == {"delete": True}

    plane = ControlPlane()
    ignore = plane.handle_interaction(
        InteractionResult(
            interaction_id=delivery.interaction.interaction_id,
            interaction_kind=delivery.interaction.interaction_kind,
            action_key=buttons[1].action_key,
            action_args=buttons[1].action_args,
            route=_route(),
        )
    )
    assert ignore is not None
    assert ignore.action_kind == "cache_warm_deadline_ignore"
    assert ignore.args["cache_epoch"] == "epoch-a"

    compact = plane.handle_interaction(
        InteractionResult(
            interaction_id=delivery.interaction.interaction_id,
            interaction_kind=delivery.interaction.interaction_kind,
            action_key=buttons[0].action_key,
            action_args=buttons[0].action_args,
            route=_route(),
        )
    )
    assert compact is not None
    assert compact.action_kind == "compact_memory"
    assert compact.args["cache_epoch"] == "epoch-a"


def test_core_capabilities_inspect_and_persist_deadline_configuration() -> None:
    settings = _Settings()
    core = PalCore()

    class _LLMRuntime:
        settings_repository = settings

        @staticmethod
        def prompt_cache_warm_deadline_snapshot():
            return _snapshot()

    core.context.port_registry["llm:llm"] = _LLMRuntime()
    provider = CoreIntrospectionProvider(core=core)

    configured = provider.configure_cache_warm_deadline(
        IntrospectionCall(
            name="core_configure_cache_warm_deadline",
            args={
                "enabled": True,
                "lead_seconds": 180,
                "min_prefix_tokens": 48_000,
            },
        )
    )
    observed = provider.cache_warm_deadline(
        IntrospectionCall(name="core_cache_warm_deadline")
    )

    assert configured.status == RuntimeStatus.OK
    assert observed.status == RuntimeStatus.OK
    assert observed.structured is not None
    assert observed.structured["enabled"] is True
    assert observed.structured["lead_seconds"] == 180
    assert observed.structured["min_prefix_tokens"] == 48_000
    core.close()


async def _capture(bucket: list, notice) -> bool:
    bucket.append(notice)
    return True


async def _discard(_notice) -> bool:
    return True
