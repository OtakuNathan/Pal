from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULT_SERVICE_SCHEDULE_TIME = (9, 0)

WEEKDAY_ALIASES = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tues": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def resolve_timezone_name(value: str | None, *, fallback: str = "UTC") -> str:
    candidate = str(value or "").strip() or fallback
    try:
        ZoneInfo(candidate)
        return candidate
    except ZoneInfoNotFoundError:
        return fallback


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _normalize_weekdays(raw_value: Any) -> list[int]:
    if raw_value is None:
        return []
    if isinstance(raw_value, (str, int)):
        raw_items = [raw_value]
    elif isinstance(raw_value, list):
        raw_items = raw_value
    else:
        return []
    values: list[int] = []
    for item in raw_items:
        if isinstance(item, int):
            weekday = item if 0 <= item <= 6 else None
        else:
            weekday = WEEKDAY_ALIASES.get(str(item).strip().lower())
        if weekday is not None and weekday not in values:
            values.append(weekday)
    return sorted(values)


def normalize_service_schedule(
    schedule: dict[str, Any] | None,
    *,
    default_timezone: str = "UTC",
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    payload = dict(schedule or {})
    cadence = str(payload.get("cadence") or "").strip().lower() or "manual"
    if cadence == "secondly":
        cadence = "seconds"
    if cadence not in {"manual", "seconds", "hourly", "daily", "weekly", "monitoring"}:
        cadence = "manual"
    interval = max(1, _safe_int(payload.get("interval"), 1))
    timezone_name = resolve_timezone_name(payload.get("timezone"), fallback=default_timezone)
    reference = (now_utc or utc_now_dt()).astimezone(ZoneInfo(timezone_name))

    normalized: dict[str, Any] = {
        "cadence": cadence,
        "interval": interval,
        "timezone": timezone_name,
    }
    if cadence == "manual":
        return normalized
    if cadence in {"seconds", "hourly", "monitoring"}:
        normalized["anchor_at_utc"] = str(payload.get("anchor_at_utc") or (now_utc or utc_now_dt()).isoformat())
        return normalized

    default_hour, default_minute = DEFAULT_SERVICE_SCHEDULE_TIME
    hour = min(23, max(0, _safe_int(payload.get("hour"), default_hour)))
    minute = min(59, max(0, _safe_int(payload.get("minute"), default_minute)))
    normalized["hour"] = hour
    normalized["minute"] = minute
    anchor_local_date = str(payload.get("anchor_local_date") or reference.date().isoformat()).strip()
    try:
        date.fromisoformat(anchor_local_date)
    except ValueError:
        anchor_local_date = reference.date().isoformat()
    normalized["anchor_local_date"] = anchor_local_date
    if cadence == "weekly":
        weekdays = _normalize_weekdays(payload.get("weekdays") or payload.get("weekday"))
        if not weekdays:
            weekdays = [reference.weekday()]
        normalized["weekdays"] = weekdays
    return normalized


def compute_next_service_run_at_utc(
    schedule: dict[str, Any] | None,
    *,
    now_utc: datetime | None = None,
) -> str | None:
    normalized = normalize_service_schedule(schedule, now_utc=now_utc)
    cadence = str(normalized.get("cadence") or "manual")
    reference = now_utc or utc_now_dt()
    if cadence == "manual":
        return None
    if cadence == "seconds":
        return _next_seconds_run(normalized, reference).isoformat()
    if cadence in {"hourly", "monitoring"}:
        return _next_hourly_run(normalized, reference).isoformat()
    timezone_name = resolve_timezone_name(normalized.get("timezone"))
    tz = ZoneInfo(timezone_name)
    local_now = reference.astimezone(tz)
    if cadence == "weekly":
        return _next_weekly_run(normalized, local_now).astimezone(timezone.utc).isoformat()
    return _next_daily_run(normalized, local_now).astimezone(timezone.utc).isoformat()


def _parse_user_datetime(value: str, *, timezone_name: str | None = None) -> datetime:
    normalized = str(value or "").strip().replace("Z", "+00:00")
    if not normalized:
        raise ValueError("timestamp is required")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    tz = ZoneInfo(resolve_timezone_name(timezone_name))
    return parsed.replace(tzinfo=tz).astimezone(timezone.utc)


def _next_seconds_run(schedule: dict[str, Any], now_utc: datetime) -> datetime:
    interval = max(1, _safe_int(schedule.get("interval"), 1))
    anchor = _parse_user_datetime(str(schedule.get("anchor_at_utc") or now_utc.isoformat()), timezone_name="UTC")
    if anchor > now_utc:
        return anchor
    elapsed = max(0.0, (now_utc - anchor).total_seconds())
    steps = int(elapsed // interval) + 1
    return anchor + timedelta(seconds=steps * interval)


def _next_hourly_run(schedule: dict[str, Any], now_utc: datetime) -> datetime:
    interval = max(1, _safe_int(schedule.get("interval"), 1))
    anchor = _parse_user_datetime(str(schedule.get("anchor_at_utc") or now_utc.isoformat()), timezone_name="UTC")
    if anchor > now_utc:
        return anchor
    delta_seconds = interval * 3600
    elapsed = max(0.0, (now_utc - anchor).total_seconds())
    steps = int(elapsed // delta_seconds) + 1
    return anchor + timedelta(seconds=steps * delta_seconds)


def _next_daily_run(schedule: dict[str, Any], local_now: datetime) -> datetime:
    interval = max(1, _safe_int(schedule.get("interval"), 1))
    hour = min(23, max(0, _safe_int(schedule.get("hour"), DEFAULT_SERVICE_SCHEDULE_TIME[0])))
    minute = min(59, max(0, _safe_int(schedule.get("minute"), DEFAULT_SERVICE_SCHEDULE_TIME[1])))
    anchor = _anchor_date(schedule, local_now.date())
    candidate_date = local_now.date()
    while True:
        delta_days = (candidate_date - anchor).days
        if delta_days >= 0 and delta_days % interval == 0:
            candidate = datetime.combine(candidate_date, time(hour=hour, minute=minute), tzinfo=local_now.tzinfo)
            if candidate > local_now:
                return candidate
        candidate_date += timedelta(days=1)


def _next_weekly_run(schedule: dict[str, Any], local_now: datetime) -> datetime:
    interval = max(1, _safe_int(schedule.get("interval"), 1))
    hour = min(23, max(0, _safe_int(schedule.get("hour"), DEFAULT_SERVICE_SCHEDULE_TIME[0])))
    minute = min(59, max(0, _safe_int(schedule.get("minute"), DEFAULT_SERVICE_SCHEDULE_TIME[1])))
    anchor = _anchor_date(schedule, local_now.date())
    anchor_week_start = anchor - timedelta(days=anchor.weekday())
    weekdays = _normalize_weekdays(schedule.get("weekdays"))
    if not weekdays:
        weekdays = [local_now.weekday()]
    candidate_date = local_now.date()
    for _ in range(370):
        week_start = candidate_date - timedelta(days=candidate_date.weekday())
        week_delta = (week_start - anchor_week_start).days // 7
        if week_delta >= 0 and week_delta % interval == 0 and candidate_date.weekday() in weekdays:
            candidate = datetime.combine(candidate_date, time(hour=hour, minute=minute), tzinfo=local_now.tzinfo)
            if candidate > local_now:
                return candidate
        candidate_date += timedelta(days=1)
    return datetime.combine(
        local_now.date() + timedelta(days=7 * interval),
        time(hour=hour, minute=minute),
        tzinfo=local_now.tzinfo,
    )


def _anchor_date(schedule: dict[str, Any], fallback: date) -> date:
    raw = str(schedule.get("anchor_local_date") or "").strip()
    if not raw:
        return fallback
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return fallback
