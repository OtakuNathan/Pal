from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping

from pal.llm.settings_contracts import RuntimeSettingsPort
from pal.foundation.diagnostics import exception_reason
from pal.shared.timers import TimerHandlePort, TimerScopePort

from pal.control.contracts import ControlRoute


from pal.cache_reminder.settings import (
    CacheWarmDeadlineSettings, read_cache_reminder_settings, configure_cache_reminder_settings,
)


@dataclass(frozen=True)
class CacheWarmDeadlineNotice:
    route: ControlRoute
    epoch: str
    turn_id: str
    ttl_seconds: int
    lead_seconds: int
    prefix_tokens: int
    expires_at: datetime
    estimated: bool = False


CacheSnapshotProvider = Callable[[], Mapping[str, Any]]
SettingsProvider = Callable[[], RuntimeSettingsPort | None]
ActiveTurnProvider = Callable[[], bool]
NoticeDelivery = Callable[[CacheWarmDeadlineNotice], Awaitable[bool]]


@dataclass
class CacheWarmDeadlineManager:
    cache_snapshot: CacheSnapshotProvider
    settings_provider: SettingsProvider
    has_active_turn: ActiveTurnProvider
    deliver_notice: NoticeDelivery
    expire_notice: NoticeDelivery
    timers: TimerScopePort
    keepalive_snapshot: CacheSnapshotProvider = lambda: {}
    keepalive_step: Callable[[CacheWarmDeadlineNotice, str], Awaitable[bool]] | None = None
    _keepalive_task: asyncio.Task[None] | None = None
    _last_route: ControlRoute | None = None
    _last_turn_id: str = ""
    _notice_timer: TimerHandlePort | None = None
    _expiry_timer: TimerHandlePort | None = None
    _generation: int = 0
    _scheduled_epoch: str = ""
    _scheduled_turn_id: str = ""
    _scheduled_route: ControlRoute | None = None
    _scheduled_deadline_at: datetime | None = None
    _cache_expires_at: datetime | None = None
    _notified_epoch: str = ""
    _ignored_epoch: str = ""
    _consumed_epoch: str = ""
    _active_notice: CacheWarmDeadlineNotice | None = None
    _last_error: str = ""

    def settings(self) -> CacheWarmDeadlineSettings:
        return read_cache_reminder_settings(self.settings_provider())

    def configure(
        self,
        *,
        enabled: bool | None = None,
        mode: str | None = None,
        lead_seconds: int | None = None,
        min_prefix_tokens: int | None = None,
    ) -> dict[str, Any]:
        repository = self.settings_provider()
        if repository is None:
            raise RuntimeError("runtime settings repository is unavailable")
        previous = self.settings()
        resolved = configure_cache_reminder_settings(repository, enabled=enabled, mode=mode,
            lead_seconds=lead_seconds, min_prefix_tokens=min_prefix_tokens)
        if not resolved.enabled:
            notice = self._active_notice
            self.cancel()
            if notice is not None:
                async def expire_disabled() -> None:
                    await self._expire_active_notice(notice)
                self.timers.schedule(0, expire_disabled)
        elif previous.mode != resolved.mode or not previous.enabled:
            notice = self._active_notice
            self.cancel()
            if notice is not None:
                async def expire_switched() -> None:
                    await self._expire_active_notice(notice)
                self.timers.schedule(0, expire_switched)
            self._notified_epoch = ""
            self._ignored_epoch = ""
            if self._last_route is not None:
                self.schedule_after_turn_commit(route=self._last_route, turn_id=self._last_turn_id)
        return self.inspect()

    def _keepalive_for(self, epoch: str, turn_id: str) -> dict[str, Any]:
        if self.settings().mode != "keep-alive" or self.keepalive_step is None:
            return {}
        state = dict(self.keepalive_snapshot() or {})
        return state if (state.get("eligible") and state.get("anchor_epoch") == epoch
                         and state.get("turn_id") == turn_id) else {}

    def schedule_after_turn_commit(
        self,
        *,
        route: ControlRoute,
        turn_id: str,
    ) -> bool:
        self.cancel()
        self._last_route, self._last_turn_id = route, turn_id
        settings = self.settings()
        snapshot = dict(self.cache_snapshot() or {})
        epoch = str(snapshot.get("anchor_epoch") or "").strip()
        ttl_seconds = max(0, int(snapshot.get("anchor_ttl_seconds") or 0))
        remaining_ttl = snapshot.get("anchor_remaining_ttl_seconds")
        estimated = bool(snapshot.get("estimated"))
        prefix_tokens = _context_tokens(snapshot)
        if (
            not settings.enabled
            or not bool(snapshot.get("eligible"))
            or not epoch
            or ttl_seconds <= 0
            or (remaining_ttl is not None and int(remaining_ttl) <= 0)
            or prefix_tokens < settings.min_prefix_tokens
            or epoch == self._notified_epoch
            or epoch == self._ignored_epoch
        ):
            return False
        # Estimates start when the response returns, including time spent
        # committing/delivering the turn. Never restart that clock here.
        lifetime = float(remaining_ttl) if estimated and remaining_ttl is not None else ttl_seconds
        keep = self._keepalive_for(epoch, turn_id)
        lead = settings.lead_seconds
        if keep and int(keep.get("prefix_tokens") or 0) >= settings.min_prefix_tokens:
            ttl_seconds = int(keep["anchor_ttl_seconds"])
            lifetime = float(keep["anchor_remaining_ttl_seconds"])
            lead = min(lead, ttl_seconds // 5)
        delay_seconds = max(0, lifetime - lead)
        self._generation += 1
        generation = self._generation
        self._scheduled_epoch = epoch
        self._scheduled_turn_id = str(turn_id or "")
        self._scheduled_route = route
        scheduled_at = datetime.now(timezone.utc)
        self._scheduled_deadline_at = scheduled_at + timedelta(
            seconds=delay_seconds
        )
        self._cache_expires_at = scheduled_at + timedelta(seconds=lifetime)
        notice = CacheWarmDeadlineNotice(
            route=route, epoch=epoch, turn_id=turn_id, ttl_seconds=ttl_seconds,
            lead_seconds=lead, prefix_tokens=prefix_tokens,
            expires_at=self._cache_expires_at, estimated=estimated,
        )

        async def notify() -> None:
            await self._notify_due(notice, generation)

        async def expire() -> None:
            if generation == self._generation:
                active = self._active_notice
                self.cancel()
                await self._expire_active_notice(active)

        self._notice_timer = self.timers.schedule(delay_seconds, notify)
        self._expiry_timer = self.timers.schedule(lifetime, expire)
        return True

    def cancel(self) -> None:
        self._generation += 1
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None and not task.done():
            try:
                current = asyncio.current_task()
            except RuntimeError:
                current = None
            if task is not current:
                task.cancel()
        for timer in (self._notice_timer, self._expiry_timer):
            if timer is not None:
                timer.cancel()
        self._notice_timer = self._expiry_timer = None
        self._active_notice = None
        self._clear_schedule()

    async def clear_for_user_activity(self) -> None:
        notice = self._active_notice
        self.cancel()
        self._last_route, self._last_turn_id = None, ""
        self._notified_epoch = ""
        self._ignored_epoch = ""
        self._consumed_epoch = ""
        await self._expire_active_notice(notice)

    async def clear_for_compaction(self) -> None:
        notice = self._active_notice
        self.cancel()
        self._last_route, self._last_turn_id = None, ""
        await self._expire_active_notice(notice)

    async def _expire_active_notice(
        self,
        notice: CacheWarmDeadlineNotice | None,
    ) -> None:
        if notice is not None:
            try:
                await self.expire_notice(notice)
            except Exception as exc:
                self._last_error = exception_reason(exc)

    def ignore(self, epoch: str) -> bool:
        normalized = str(epoch or "").strip()
        if not normalized or normalized != self._scheduled_epoch:
            return False
        self._ignored_epoch = normalized
        self.cancel()
        return True

    def is_estimated_notice(self, epoch: str) -> bool:
        notice = self._active_notice
        return bool(notice and notice.epoch == epoch and notice.estimated)

    def claim_compaction(self, epoch: str) -> bool:
        """Atomically consume the currently visible compact reminder."""

        normalized = str(epoch or "").strip()
        notice = self._active_notice
        if (
            not normalized
            or notice is None
            or notice.epoch != normalized
            or self._consumed_epoch == normalized
        ):
            return False
        snapshot = dict(self.cache_snapshot() or {})
        settings = self.settings()
        if (
            self.has_active_turn()
            or not settings.enabled
            or datetime.now(timezone.utc) >= notice.expires_at
            or not bool(snapshot.get("eligible"))
            or str(snapshot.get("anchor_epoch") or "") != normalized
            or int(snapshot.get("anchor_remaining_ttl_seconds") or 0) <= 0
            or _context_tokens(snapshot) < settings.min_prefix_tokens
        ):
            return False
        self._consumed_epoch = normalized
        # The action now owns the interaction lifecycle.  Cancel the TTL task
        # without expiring the message so its pending/final updates can reuse
        # the same provider-native UI object.
        self.cancel()
        return True

    def inspect(self) -> dict[str, Any]:
        settings = self.settings()
        snapshot = dict(self.cache_snapshot() or {})
        deadline = self._scheduled_deadline_at
        remaining = None
        if deadline is not None:
            remaining = max(
                0,
                int((deadline - datetime.now(timezone.utc)).total_seconds()),
            )
        scheduled = any(timer is not None and timer.pending for timer in (self._notice_timer, self._expiry_timer))
        reason = None
        if not scheduled:
            if not settings.enabled:
                reason = "reminder_disabled"
            elif not snapshot.get("eligible"):
                reason = "cache_not_eligible"
            elif not snapshot.get("anchor_epoch"):
                reason = "cache_anchor_missing"
            elif int(snapshot.get("anchor_ttl_seconds") or 0) <= 0:
                reason = "provider_ttl_unavailable"
            elif snapshot.get("anchor_remaining_ttl_seconds") is not None and int(snapshot["anchor_remaining_ttl_seconds"]) <= 0:
                reason = "cache_expired"
            elif _context_tokens(snapshot) < settings.min_prefix_tokens:
                reason = "prefix_below_minimum"
            elif str(snapshot.get("anchor_epoch")) in {self._notified_epoch, self._ignored_epoch}:
                reason = "anchor_already_notified_or_ignored"
            else:
                reason = "no_pending_reminder"
        return {
            "unscheduled_reason": reason,
            "enabled": settings.enabled,
            "mode": settings.mode,
            "keepalive": dict(self.keepalive_snapshot() or {}),
            "lead_seconds": settings.lead_seconds,
            "min_prefix_tokens": settings.min_prefix_tokens,
            "timer_scheduled": scheduled,
            "scheduled_turn_id": self._scheduled_turn_id,
            "scheduled_epoch": self._scheduled_epoch,
            "scheduled_deadline_at": deadline.isoformat() if deadline else None,
            "cache_expires_at": (
                self._cache_expires_at.isoformat()
                if self._cache_expires_at is not None
                else None
            ),
            "seconds_until_notice": remaining,
            "last_notified_epoch": self._notified_epoch,
            "ignored_epoch": self._ignored_epoch,
            "consumed_epoch": self._consumed_epoch,
            "last_error": self._last_error,
            "cache": snapshot,
        }

    def close(self) -> None:
        self.cancel()
        self.timers.close()

    async def _notify_due(self, notice: CacheWarmDeadlineNotice, generation: int) -> None:
        if generation != self._generation:
            return
        try:
            expires_at = notice.expires_at
            remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                return
            if self.has_active_turn():
                async def retry() -> None:
                    await self._notify_due(notice, generation)
                self._notice_timer = self.timers.schedule(min(5.0, remaining), retry)
                return
            settings = self.settings()
            snapshot = dict(self.cache_snapshot())
            ttl = snapshot.get("anchor_remaining_ttl_seconds")
            if (not settings.enabled or not snapshot.get("eligible")
                    or snapshot.get("anchor_epoch") != notice.epoch
                    or (ttl is not None and ttl <= 0)
                    or _context_tokens(snapshot) < settings.min_prefix_tokens
                    or notice.epoch in {self._ignored_epoch, self._notified_epoch}):
                return
            keep = self._keepalive_for(notice.epoch, notice.turn_id)
            if keep and int(keep.get("prefix_tokens") or 0) >= settings.min_prefix_tokens:
                self._keepalive_task = asyncio.create_task(self._run_keepalive(notice, generation, str(keep["decision"])))
                return
            if await self.deliver_notice(notice):
                if generation != self._generation:
                    await self._expire_active_notice(notice)
                    return
                self._notified_epoch = notice.epoch
                self._active_notice = notice
        except Exception as exc:
            self._last_error = exception_reason(exc)

    async def _run_keepalive(self, notice: CacheWarmDeadlineNotice, generation: int, decision: str) -> None:
        try:
            assert self.keepalive_step is not None
            renewed = await self.keepalive_step(notice, decision)
            if generation != self._generation:
                return
            if renewed:
                self.schedule_after_turn_commit(route=notice.route, turn_id=notice.turn_id)
            elif decision == "keepalive" and datetime.now(timezone.utc) < notice.expires_at:
                # Delivery must settle even if the user starts a turn while
                # it is in flight. A stale delivered notice is then expired.
                if self._keepalive_task is asyncio.current_task():
                    self._keepalive_task = None
                # A failed probe must not be retried.
                if await self.deliver_notice(notice):
                    if generation != self._generation:
                        await self._expire_active_notice(notice)
                        return
                    self._notified_epoch = notice.epoch
                    self._active_notice = notice
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = exception_reason(exc)
        finally:
            if self._keepalive_task is asyncio.current_task():
                self._keepalive_task = None

    def _clear_schedule(self) -> None:
        self._scheduled_epoch = ""
        self._scheduled_turn_id = ""
        self._scheduled_route = None
        self._scheduled_deadline_at = None
        self._cache_expires_at = None


def _context_tokens(snapshot: Mapping[str, Any]) -> int:
    return max(0, int(snapshot.get("context_tokens", snapshot.get("prefix_tokens", 0)) or 0))
