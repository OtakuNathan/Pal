from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from croniter import croniter


def cron_occurrence(expression: str, timezone_name: str, reference: datetime, *, previous=False) -> datetime:
    """Compute one occurrence without consulting runtime state or the clock."""
    iterator = croniter(expression, reference.astimezone(ZoneInfo(timezone_name)))
    occurrence = iterator.get_prev(datetime) if previous else iterator.get_next(datetime)
    return occurrence.astimezone(timezone.utc)
