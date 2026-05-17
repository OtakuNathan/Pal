from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def resolve_timezone_name(value: str | None, *, fallback: str = "UTC") -> str:
    candidate = str(value or "").strip() or fallback
    try:
        ZoneInfo(candidate)
        return candidate
    except ZoneInfoNotFoundError:
        return fallback


def normalize_proactive_schedule(
    schedule: dict[str, Any] | None,
    *,
    default_timezone: str = "UTC",
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    payload = dict(schedule or {})
    cadence = str(payload.get("cadence") or "").strip().lower() or "manual"
    if cadence not in {"manual", "cron", "once"}:
        cadence = "manual"
    timezone_name = resolve_timezone_name(payload.get("timezone"), fallback=default_timezone)

    normalized: dict[str, Any] = {
        "cadence": cadence,
        "timezone": timezone_name,
    }
    if cadence == "manual":
        return normalized
    if cadence == "cron":
        cron_expr = str(payload.get("cron") or "").strip()
        if not cron_expr:
            normalized["cadence"] = "manual"
            return normalized
        normalized["cron"] = cron_expr
        return normalized
    # once
    run_at = str(payload.get("run_at_utc") or "").strip()
    if not run_at:
        normalized["cadence"] = "manual"
        return normalized
    normalized["run_at_utc"] = run_at
    return normalized


def compute_next_proactive_run_at_utc(
    schedule: dict[str, Any] | None,
    *,
    now_utc: datetime | None = None,
) -> str | None:
    normalized = normalize_proactive_schedule(schedule, now_utc=now_utc)
    cadence = str(normalized.get("cadence") or "manual")
    reference = now_utc or utc_now_dt()
    if cadence == "manual":
        return None
    if cadence == "cron":
        return _next_cron_run(normalized, reference).isoformat()
    # once
    return _next_once_run(normalized, reference)


def _parse_user_datetime(value: str, *, timezone_name: str | None = None) -> datetime:
    normalized = str(value or "").strip().replace("Z", "+00:00")
    if not normalized:
        raise ValueError("timestamp is required")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    tz = ZoneInfo(resolve_timezone_name(timezone_name))
    return parsed.replace(tzinfo=tz).astimezone(timezone.utc)


def _next_cron_run(schedule: dict[str, Any], now_utc: datetime) -> datetime:
    import croniter

    cron_expr = str(schedule.get("cron") or "")
    tz_name = resolve_timezone_name(schedule.get("timezone"))
    tz = ZoneInfo(tz_name)
    local_now = now_utc.astimezone(tz)
    try:
        cron = croniter.croniter(cron_expr, local_now)
    except (ValueError, KeyError):
        return now_utc + __import__("datetime").timedelta(hours=1)
    next_local = cron.get_next(datetime)
    return next_local.astimezone(timezone.utc)


def _next_once_run(schedule: dict[str, Any], now_utc: datetime) -> str | None:
    raw = str(schedule.get("run_at_utc") or "").strip()
    if not raw:
        return None
    try:
        target = _parse_user_datetime(raw)
    except ValueError:
        return None
    if target <= now_utc:
        return None
    return target.isoformat()
