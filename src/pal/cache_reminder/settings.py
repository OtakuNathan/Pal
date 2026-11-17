"""Persistent cache-reminder policy and input validation."""
from __future__ import annotations

from dataclasses import dataclass
from pydantic import BaseModel, ConfigDict, Field
from pal.llm.settings_contracts import RuntimeSettingsPort


class CacheReminderPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    lead_seconds: int | None = Field(default=None, ge=30)
    min_prefix_tokens: int | None = Field(default=None, ge=1024)


CACHE_WARM_DEADLINE_ENABLED_KEY = "cache_warm_deadline_enabled"
CACHE_WARM_DEADLINE_LEAD_SECONDS_KEY = "cache_warm_deadline_lead_seconds"
CACHE_WARM_DEADLINE_MIN_PREFIX_TOKENS_KEY = (
    "cache_warm_deadline_min_prefix_tokens"
)

DEFAULT_CACHE_WARM_DEADLINE_ENABLED = True
DEFAULT_CACHE_WARM_DEADLINE_LEAD_SECONDS = 5 * 60
DEFAULT_CACHE_WARM_DEADLINE_MIN_PREFIX_TOKENS = 32_768


@dataclass(frozen=True)
class CacheWarmDeadlineSettings:
    enabled: bool = DEFAULT_CACHE_WARM_DEADLINE_ENABLED
    lead_seconds: int = DEFAULT_CACHE_WARM_DEADLINE_LEAD_SECONDS
    min_prefix_tokens: int = DEFAULT_CACHE_WARM_DEADLINE_MIN_PREFIX_TOKENS


def read_cache_reminder_settings(repository: RuntimeSettingsPort | None) -> CacheWarmDeadlineSettings:
    if repository is None:
        return CacheWarmDeadlineSettings()
    return CacheWarmDeadlineSettings(
        enabled=_setting_bool(repository.get(CACHE_WARM_DEADLINE_ENABLED_KEY), DEFAULT_CACHE_WARM_DEADLINE_ENABLED),
        lead_seconds=_setting_int(repository.get(CACHE_WARM_DEADLINE_LEAD_SECONDS_KEY), DEFAULT_CACHE_WARM_DEADLINE_LEAD_SECONDS, minimum=30),
        min_prefix_tokens=_setting_int(repository.get(CACHE_WARM_DEADLINE_MIN_PREFIX_TOKENS_KEY), DEFAULT_CACHE_WARM_DEADLINE_MIN_PREFIX_TOKENS, minimum=1024),
    )


def configure_cache_reminder_settings(repository: RuntimeSettingsPort, *, enabled: bool | None = None,
                                     lead_seconds: int | None = None,
                                     min_prefix_tokens: int | None = None) -> CacheWarmDeadlineSettings:
    # Validate the entire patch before storing any value, including enabled.
    updates = {}
    if lead_seconds is not None:
        if int(lead_seconds) < 30:
            raise ValueError("lead_seconds must be at least 30")
        updates[CACHE_WARM_DEADLINE_LEAD_SECONDS_KEY] = str(int(lead_seconds))
    if min_prefix_tokens is not None:
        if int(min_prefix_tokens) < 1024:
            raise ValueError("min_prefix_tokens must be at least 1024")
        updates[CACHE_WARM_DEADLINE_MIN_PREFIX_TOKENS_KEY] = str(int(min_prefix_tokens))
    if enabled is not None:
        updates[CACHE_WARM_DEADLINE_ENABLED_KEY] = "true" if bool(enabled) else "false"
    with repository.selection_transaction():
        for key, value in updates.items():
            repository.set(key, value)
    return read_cache_reminder_settings(repository)


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
