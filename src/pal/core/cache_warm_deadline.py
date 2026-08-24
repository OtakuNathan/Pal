from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Mapping, Protocol

from pal.control.contracts import ControlRoute


CACHE_WARM_DEADLINE_ENABLED_KEY = "cache_warm_deadline_enabled"
CACHE_WARM_DEADLINE_LEAD_SECONDS_KEY = "cache_warm_deadline_lead_seconds"
CACHE_WARM_DEADLINE_MIN_PREFIX_TOKENS_KEY = (
    "cache_warm_deadline_min_prefix_tokens"
)

DEFAULT_CACHE_WARM_DEADLINE_ENABLED = True
DEFAULT_CACHE_WARM_DEADLINE_LEAD_SECONDS = 5 * 60
DEFAULT_CACHE_WARM_DEADLINE_MIN_PREFIX_TOKENS = 32_768


class RuntimeSettingsPort(Protocol):
    def get(self, setting_key: str) -> str | None: ...

    def set(self, setting_key: str, setting_value: str) -> Any: ...


@dataclass(frozen=True)
class CacheWarmDeadlineSettings:
    enabled: bool = DEFAULT_CACHE_WARM_DEADLINE_ENABLED
    lead_seconds: int = DEFAULT_CACHE_WARM_DEADLINE_LEAD_SECONDS
    min_prefix_tokens: int = DEFAULT_CACHE_WARM_DEADLINE_MIN_PREFIX_TOKENS


@dataclass(frozen=True)
class CacheWarmDeadlineNotice:
    route: ControlRoute
    epoch: str
    turn_id: str
    ttl_seconds: int
    lead_seconds: int
    prefix_tokens: int
    expires_at: str


CacheSnapshotProvider = Callable[[], Mapping[str, Any]]
SettingsProvider = Callable[[], RuntimeSettingsPort | None]
ActiveTurnProvider = Callable[[], bool]
NoticeDelivery = Callable[[CacheWarmDeadlineNotice], Awaitable[bool]]
Sleep = Callable[[float], Awaitable[None]]


@dataclass
class CacheWarmDeadlineManager:
    cache_snapshot: CacheSnapshotProvider
    settings_provider: SettingsProvider
    has_active_turn: ActiveTurnProvider
    deliver_notice: NoticeDelivery
    expire_notice: NoticeDelivery
    sleep: Sleep = asyncio.sleep
    _task: asyncio.Task[None] | None = None
    _task_loop: asyncio.AbstractEventLoop | None = None
    _generation: int = 0
    _scheduled_epoch: str = ""
    _scheduled_turn_id: str = ""
    _scheduled_route: ControlRoute | None = None
    _scheduled_deadline_at: datetime | None = None
    _cache_expires_at: datetime | None = None
    _notified_epoch: str = ""
    _ignored_epoch: str = ""
    _active_notice: CacheWarmDeadlineNotice | None = None
    _last_error: str = ""

    def settings(self) -> CacheWarmDeadlineSettings:
        repository = self.settings_provider()
        if repository is None:
            return CacheWarmDeadlineSettings()
        return CacheWarmDeadlineSettings(
            enabled=_setting_bool(
                repository.get(CACHE_WARM_DEADLINE_ENABLED_KEY),
                DEFAULT_CACHE_WARM_DEADLINE_ENABLED,
            ),
            lead_seconds=_setting_int(
                repository.get(CACHE_WARM_DEADLINE_LEAD_SECONDS_KEY),
                DEFAULT_CACHE_WARM_DEADLINE_LEAD_SECONDS,
                minimum=30,
            ),
            min_prefix_tokens=_setting_int(
                repository.get(CACHE_WARM_DEADLINE_MIN_PREFIX_TOKENS_KEY),
                DEFAULT_CACHE_WARM_DEADLINE_MIN_PREFIX_TOKENS,
                minimum=1_024,
            ),
        )

    def configure(
        self,
        *,
        enabled: bool | None = None,
        lead_seconds: int | None = None,
        min_prefix_tokens: int | None = None,
    ) -> dict[str, Any]:
        repository = self.settings_provider()
        if repository is None:
            raise RuntimeError("runtime settings repository is unavailable")
        if enabled is not None:
            repository.set(
                CACHE_WARM_DEADLINE_ENABLED_KEY,
                "true" if bool(enabled) else "false",
            )
        if lead_seconds is not None:
            normalized_lead = int(lead_seconds)
            if normalized_lead < 30:
                raise ValueError("lead_seconds must be at least 30")
            repository.set(
                CACHE_WARM_DEADLINE_LEAD_SECONDS_KEY,
                str(normalized_lead),
            )
        if min_prefix_tokens is not None:
            normalized_minimum = int(min_prefix_tokens)
            if normalized_minimum < 1_024:
                raise ValueError("min_prefix_tokens must be at least 1024")
            repository.set(
                CACHE_WARM_DEADLINE_MIN_PREFIX_TOKENS_KEY,
                str(normalized_minimum),
            )
        resolved = self.settings()
        if not resolved.enabled:
            self.cancel()
        return self.inspect()

    def schedule_after_turn_commit(
        self,
        *,
        route: ControlRoute,
        turn_id: str,
    ) -> bool:
        self.cancel()
        settings = self.settings()
        snapshot = dict(self.cache_snapshot() or {})
        epoch = str(snapshot.get("anchor_epoch") or "").strip()
        remaining_ttl = snapshot.get("anchor_remaining_ttl_seconds")
        ttl_seconds = max(
            0,
            int(
                remaining_ttl
                if remaining_ttl is not None
                else snapshot.get("anchor_ttl_seconds") or 0
            ),
        )
        prefix_tokens = max(0, int(snapshot.get("prefix_tokens") or 0))
        if (
            not settings.enabled
            or not bool(snapshot.get("eligible"))
            or not epoch
            or ttl_seconds <= settings.lead_seconds
            or prefix_tokens < settings.min_prefix_tokens
            or epoch == self._notified_epoch
            or epoch == self._ignored_epoch
        ):
            return False
        delay_seconds = ttl_seconds - settings.lead_seconds
        self._generation += 1
        generation = self._generation
        self._scheduled_epoch = epoch
        self._scheduled_turn_id = str(turn_id or "")
        self._scheduled_route = route
        scheduled_at = datetime.now(timezone.utc)
        self._scheduled_deadline_at = scheduled_at + timedelta(
            seconds=delay_seconds
        )
        self._cache_expires_at = scheduled_at + timedelta(seconds=ttl_seconds)
        self._task = asyncio.create_task(
            self._wait_and_notify(
                generation=generation,
                route=route,
                turn_id=str(turn_id or ""),
                epoch=epoch,
                ttl_seconds=ttl_seconds,
                lead_seconds=settings.lead_seconds,
                prefix_tokens=prefix_tokens,
                delay_seconds=delay_seconds,
                expires_at=self._cache_expires_at,
            ),
            name="pal.cache_warm_deadline",
        )
        self._task_loop = self._task.get_loop()
        return True

    def cancel(self) -> None:
        self._generation += 1
        task = self._task
        task_loop = self._task_loop
        self._task = None
        self._task_loop = None
        if task is not None and not task.done():
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            if task_loop is not None and task_loop.is_running() and running_loop is not task_loop:
                task_loop.call_soon_threadsafe(task.cancel)
            else:
                task.cancel()
        self._active_notice = None
        self._clear_schedule()

    async def clear_for_user_activity(self) -> None:
        notice = self._active_notice
        self.cancel()
        self._notified_epoch = ""
        self._ignored_epoch = ""
        await self._expire_active_notice(notice)

    async def clear_for_compaction(self) -> None:
        notice = self._active_notice
        self.cancel()
        await self._expire_active_notice(notice)

    async def _expire_active_notice(
        self,
        notice: CacheWarmDeadlineNotice | None,
    ) -> None:
        if notice is not None:
            try:
                await self.expire_notice(notice)
            except Exception as exc:
                self._last_error = f"{exc.__class__.__name__}: {exc}"

    def ignore(self, epoch: str) -> bool:
        normalized = str(epoch or "").strip()
        if not normalized or normalized != self._scheduled_epoch:
            return False
        self._ignored_epoch = normalized
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
        return {
            "enabled": settings.enabled,
            "lead_seconds": settings.lead_seconds,
            "min_prefix_tokens": settings.min_prefix_tokens,
            "timer_scheduled": bool(self._task and not self._task.done()),
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
            "last_error": self._last_error,
            "cache": snapshot,
        }

    def close(self) -> None:
        self.cancel()

    async def _wait_and_notify(
        self,
        *,
        generation: int,
        route: ControlRoute,
        turn_id: str,
        epoch: str,
        ttl_seconds: int,
        lead_seconds: int,
        prefix_tokens: int,
        delay_seconds: int,
        expires_at: datetime,
    ) -> None:
        try:
            await self.sleep(delay_seconds)
            if generation != self._generation:
                return
            while self.has_active_turn():
                remaining_seconds = (
                    expires_at - datetime.now(timezone.utc)
                ).total_seconds()
                if remaining_seconds <= 0:
                    return
                await self.sleep(min(5.0, remaining_seconds))
                if generation != self._generation:
                    return
            if datetime.now(timezone.utc) >= expires_at:
                return
            settings = self.settings()
            snapshot = dict(self.cache_snapshot() or {})
            remaining_ttl = snapshot.get("anchor_remaining_ttl_seconds")
            if (
                not settings.enabled
                or str(snapshot.get("anchor_epoch") or "") != epoch
                or not bool(snapshot.get("eligible"))
                or (
                    remaining_ttl is not None
                    and int(remaining_ttl) <= 0
                )
                or int(snapshot.get("prefix_tokens") or 0)
                < settings.min_prefix_tokens
                or epoch in {self._ignored_epoch, self._notified_epoch}
            ):
                return
            notice = CacheWarmDeadlineNotice(
                route=route,
                epoch=epoch,
                turn_id=turn_id,
                ttl_seconds=ttl_seconds,
                lead_seconds=lead_seconds,
                prefix_tokens=max(
                    0,
                    int(snapshot.get("prefix_tokens") or prefix_tokens),
                ),
                expires_at=expires_at.isoformat(),
            )
            if await self.deliver_notice(notice):
                self._notified_epoch = epoch
                self._active_notice = notice
                remaining_seconds = max(
                    0.0,
                    (expires_at - datetime.now(timezone.utc)).total_seconds(),
                )
                await self.sleep(remaining_seconds)
                if generation == self._generation:
                    await self.expire_notice(notice)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._last_error = f"{exc.__class__.__name__}: {exc}"
        finally:
            if generation == self._generation:
                self._task = None
                self._task_loop = None
                self._active_notice = None
                self._clear_schedule()

    def _clear_schedule(self) -> None:
        self._scheduled_epoch = ""
        self._scheduled_turn_id = ""
        self._scheduled_route = None
        self._scheduled_deadline_at = None
        self._cache_expires_at = None


def _setting_bool(value: str | None, default: bool) -> bool:
    normalized = str(value or "").strip().lower()
    if not normalized:
        return bool(default)
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return bool(default)


def _setting_int(
    value: str | None,
    default: int,
    *,
    minimum: int,
) -> int:
    try:
        return max(minimum, int(value)) if value is not None else int(default)
    except (TypeError, ValueError):
        return int(default)
