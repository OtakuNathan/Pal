"""Object-scoped diagnostic snapshots with independently available sections."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from pal.foundation.diagnostics import exception_report


def inspection_snapshot(
    state: Mapping[str, Any],
    *,
    sections: Mapping[str, Callable[[], Any]] | None = None,
    source: str = "resident",
) -> dict[str, Any]:
    """Keep known state when an independent diagnostic source is unavailable.

    Readers must be passive and scoped to the inspected object. Availability is
    separate from health: a successful read alone does not prove health.
    """
    payload = dict(state)
    unavailable = {}
    for name, reader in (sections or {}).items():
        try:
            value = reader()
            payload[name] = value
            if isinstance(value, Mapping):
                status = value.get("inspection_status", value.get("status"))
                if isinstance(status, str) and status in {"error", "failed", "invalid", "not_found", "unavailable"}:
                    unavailable[name] = value.get("reason") or status
        except Exception as exc:
            payload[name] = {"status": "unavailable", "error": exception_report(exc)}
            unavailable[name] = f"{type(exc).__name__}: section_unavailable"
    payload["diagnostics"] = {
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "partial": bool(unavailable),
        "unavailable_sections": unavailable,
    }
    return payload
