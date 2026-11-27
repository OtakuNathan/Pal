"""Optional reminders use generic Core events, timers and compaction leases."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any
import logging

from pal.cache_reminder.settings import CacheReminderPatch

from pal.cache_reminder.cache_warm_deadline import CacheWarmDeadlineManager, CacheWarmDeadlineNotice
from pal.cache_reminder.presentation import cache_warm_deadline_delivery, cache_warm_deadline_expire_delivery
from pal.control.contracts import ControlAction, ControlCommandInvocation, ControlCommandSpec, ControlDelivery
from pal.control.interactions import terminal_delivery_for_action
from pal.control.runtime_contracts import CORE_CONTROL, ControlBusyError
from pal.core.control_action_registry import ControlActionSpec
from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle
from pal.foundation.diagnostics import exception_reason
from pal.llm.contracts import LLM_RUNTIME
from pal.llm.settings_contracts import LLM_SETTINGS
from pal.shared import EventKind, SourceKind

LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pal.core.main_context import MainContext


class CacheReminder:
    def __init__(self, context: MainContext) -> None:
        self._host = context.port_registry.observe(CORE_CONTROL)
        self._llm = context.port_registry.observe(LLM_RUNTIME)
        self._settings = context.port_registry.observe(LLM_SETTINGS)
        self.timers = self._host.current.open_timer_scope()
        self._auto_compacting = False
        self.manager = CacheWarmDeadlineManager(
            cache_snapshot=lambda: self._llm.current.prompt_cache_reminder_snapshot(),
            settings_provider=lambda: self._settings.current,
            has_active_turn=lambda: bool(self._host.current.busy_reason()),
            deliver_notice=self.deliver_notice, expire_notice=self.expire_notice,
            timers=self.timers,
            keepalive_snapshot=lambda: self._llm.current.prompt_cache_keepalive_snapshot(),
            keepalive_step=self.keepalive_step,
        )

    async def deliver_notice(self, notice: CacheWarmDeadlineNotice) -> bool:
        return await self._host.current.deliver(cache_warm_deadline_delivery(
            notice.route, epoch=notice.epoch, prefix_tokens=notice.prefix_tokens,
            lead_seconds=notice.lead_seconds, expires_at=notice.expires_at.isoformat(),
            estimated=notice.estimated), require_provider=True)

    async def expire_notice(self, notice: CacheWarmDeadlineNotice) -> bool:
        return await self._host.current.deliver(cache_warm_deadline_expire_delivery(
            notice.route, epoch=notice.epoch), require_provider=True)

    async def turn_settled(self, payload: dict[str, Any]) -> None:
        if payload["event_kind"] == EventKind.USER_MESSAGE and payload["source_kind"] == SourceKind.CHANNEL:
            self.manager.schedule_after_turn_commit(route=payload["route"], turn_id=payload["turn_id"])

    async def user_activity(self, payload: dict[str, Any]) -> None:
        await self.manager.clear_for_user_activity()

    async def context_change(self, payload: dict[str, Any]) -> None:
        await self.manager.clear_for_compaction()
        if not self._auto_compacting:
            self._llm.current.clear_keepalive()

    async def keepalive_step(self, notice: CacheWarmDeadlineNotice, decision: str) -> bool:
        host, llm = self._host.current, self._llm.current
        if host.busy_reason():
            return False
        release = host.retain_modules(("cache_reminder", "llm"))
        try:
            if decision == "keepalive":
                return await llm.keepalive_once(notice.epoch)
            if decision != "compact":
                return False
            def claim() -> None:
                if not llm.claim_keepalive_compact(notice.epoch):
                    raise ValueError("Warm cache no longer eligible")
            try:
                lease = await host.begin_context_change("manual_hot", claim=claim, max_attempts=1)
            except (ControlBusyError, ValueError):
                await host.deliver(ControlDelivery("reply", route=notice.route,
                    text="Cache keep-alive ended; warm compact is no longer available. Use /compact if needed."), require_provider=True)
                return False
            self._auto_compacting = True
            try:
                result = await lease.compact(max_attempts=1, force_semantic=True,
                    keepalive_compact_epoch=notice.epoch,
                    replay_guard=lambda: llm.keepalive_compact_valid(notice.epoch))
                text = ("Context compacted after cache keep-alive." if result.success else
                        f"Cache keep-alive ended; automatic warm compact did not complete ({result.status}). Use /compact if needed.")
                await host.deliver(ControlDelivery("reply", route=notice.route, text=text), require_provider=True)
                if result.success and result.memory_result is not None:
                    await host.deliver_compact_candidates(ControlAction("cache_warm_deadline_compact", "cache_reminder", route=notice.route), result.memory_result)
            except Exception as exc:
                LOGGER.exception("Automatic warm compact failed")
                await host.deliver(ControlDelivery("reply", route=notice.route,
                    text=f"Cache keep-alive ended; compact failed: {exception_reason(exc)}"), require_provider=True)
            finally:
                self._auto_compacting = False
                llm.clear_keepalive()
                await lease.close()
            return False
        finally:
            release()

    async def cache_policy(self, action: ControlAction) -> None:
        mode = action.args.get("mode")
        if mode is not None:
            self.manager.configure(mode=str(mode))
        state = self.manager.inspect()
        keep = state["keepalive"]
        text = f"Cache policy: {state['mode']} ({'enabled' if state['enabled'] else 'disabled'})"
        if state['mode'] == 'keep-alive':
            text += (f"\nKeep-alive: {keep.get('renewals', 0)}/{keep.get('max_actions', 0)}; "
                     f"estimated cost ${float(keep.get('estimated_cost', 0)):.6f}; {keep.get('reason', 'unavailable')}")
        if state['seconds_until_notice'] is not None:
            text += f"\nNext action in {state['seconds_until_notice']}s."
        await self._host.current.reply(action, text)

    async def ignore(self, action: ControlAction) -> None:
        ignored = self.manager.ignore(str(action.args.get("cache_epoch") or ""))
        await self._host.current.reply(action, "Dismissed this warm cache compaction reminder."
                                       if ignored else "This warm cache reminder has expired.")

    async def disable(self, action: ControlAction) -> None:
        try:
            self.manager.configure(enabled=False)
            message = "Compaction reminders before cache expiry are disabled."
        except (RuntimeError, TypeError, ValueError) as exc:
            message = f"Could not disable cache reminders: {exception_reason(exc)}"
        await self._host.current.reply(action, message)

    async def compact(self, action: ControlAction) -> None:
        epoch = str(action.args.get("cache_epoch") or "")
        estimated = self.manager.is_estimated_notice(epoch)
        host = self._host.current

        def claim() -> None:
            if self.manager.is_estimated_notice(epoch) != estimated or not self.manager.claim_compaction(epoch):
                raise ValueError("This cache compaction reminder has already been handled or has expired.")

        try:
            lease = await host.begin_context_change(
                "manual" if estimated else "manual_hot", claim=claim,
                max_attempts=None if estimated else 3)
        except (ControlBusyError, ValueError) as exc:
            await host.reply(action, f"Compaction is unavailable: {exception_reason(exc)}")
            return

        def warm_anchor() -> bool:
            snapshot = self._llm.current.prompt_cache_warm_deadline_snapshot()
            return bool(snapshot.get("eligible") and snapshot.get("anchor_epoch") == epoch
                        and int(snapshot.get("anchor_remaining_ttl_seconds") or 0) > 0)

        result = None
        try:
            # Transport failure does not undo the consumed reminder or release admission.
            try:
                await host.deliver(terminal_delivery_for_action(
                    action, "Compacting..." if estimated else "Compacting with the warm cache...",
                    delivery_kind="interactive_update"))
                await host.flush(action.route)
            except Exception:
                LOGGER.exception("Cache reminder compact status delivery failed")
            result = await lease.compact(
                max_attempts=None if estimated else 3,
                replay_guard=None if estimated else warm_anchor)
        except Exception as exc:
            LOGGER.exception("Cache reminder compaction failed")
            await host.reply(action, f"Compaction failed: {exception_reason(exc)}")
            return
        finally:
            await lease.close()
        if result.success:
            text = "Context compacted."
            if result.memory_result is not None and result.memory_result.summary:
                text += " Continuity summary updated."
        elif result.status == "hot_cache_unavailable" and not estimated:
            text = ("The warm cache has expired or cannot be reused. Automatic attempts have stopped. "
                    "The original context is preserved; run /compact manually if needed.")
        else:
            text = f"Compaction did not complete ({result.status})."
        if result.user_notice:
            text += "\n" + result.user_notice
        await host.reply(action, text)
        if result.success and result.memory_result is not None:
            await host.deliver_compact_candidates(action, result.memory_result)

    async def inspect(self, params: dict[str, Any]) -> dict[str, Any]:
        if params:
            raise ValueError("inspect takes no arguments")
        return {"status": "ok", "activation": "live", "state": self.manager.inspect()}

    async def configure(self, params: dict[str, Any]) -> dict[str, Any]:
        patch = CacheReminderPatch.model_validate(params).model_dump(exclude_unset=True)
        if not patch:
            raise ValueError("configure requires at least one setting")
        return {"status": "ok", "activation": "live", "state": self.manager.configure(**patch)}

    async def close(self) -> None:
        await self.manager.clear_for_compaction()
        self.manager.close()


def _cache_policy_command(invocation: ControlCommandInvocation) -> ControlAction:
    if len(invocation.argv) > 1 or (invocation.argv and invocation.argv[0] not in {"reminder", "keep-alive"}):
        return ControlAction("invalid_command", "control", route=invocation.route,
                             notes="Use /cache_policy [reminder|keep-alive].")
    return ControlAction("cache_policy", "cache_reminder", route=invocation.route,
                         args={"mode": invocation.argv[0]} if invocation.argv else {})


def register_with_core(context: MainContext) -> ModuleHandle:
    feature = CacheReminder(context)
    handle = ModuleHandle(
        module_id="cache_reminder", tier=MODULE_TIER_DETACHABLE, detachable=True,
        ports={"reminder": feature.manager}, timer_scope=feature.timers,
        configuration_handlers={"cache_reminder.inspect": feature.inspect,
                                "cache_reminder.configure": feature.configure},
        control_observers={"core.turn.settled": feature.turn_settled,
                           "core.user_activity": feature.user_activity,
                           "core.context_change.started": feature.context_change},
        control_commands=(ControlCommandSpec(name="cache_policy", handler=_cache_policy_command,
            description="Show or switch idle cache policy.", usage="/cache_policy [reminder|keep-alive]", show_in_panel=True),),
        control_action_handlers={
            "cache_policy": ControlActionSpec(feature.cache_policy),
            "cache_warm_deadline_ignore": ControlActionSpec(feature.ignore),
            "cache_warm_deadline_disable": ControlActionSpec(feature.disable),
            "cache_warm_deadline_compact": ControlActionSpec(feature.compact),
        },
        shutdown_async=feature.close,
    )
    try:
        context.register_module(handle)
    except BaseException:
        feature.manager.close()
        raise
    return handle
