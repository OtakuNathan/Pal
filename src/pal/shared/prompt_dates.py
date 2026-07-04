from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def today_for_timezone(timezone_name: str | None, *, now_utc: datetime | None = None) -> str:
    current = now_utc or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    tz_name = str(timezone_name or "").strip()
    if tz_name:
        try:
            current = current.astimezone(ZoneInfo(tz_name))
        except ZoneInfoNotFoundError:
            current = current.astimezone()
    else:
        current = current.astimezone()
    return current.date().isoformat()
