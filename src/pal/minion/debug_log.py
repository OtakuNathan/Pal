from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def minion_debug_log_enabled(metadata: Mapping[str, Any] | None) -> bool:
    data = dict(metadata or {})
    debug_log = data.get("debug_log")
    if isinstance(debug_log, Mapping) and "enabled" in debug_log:
        return bool(debug_log.get("enabled"))
    return bool(data.get("minion_debug_log_enabled") or data.get("prompt_log_enabled"))
