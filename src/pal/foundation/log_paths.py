from __future__ import annotations

import os
from pathlib import Path


PAL_LOG_ROOT_ENV = "PAL_LOG_ROOT"
DEFAULT_PAL_LOG_ROOT = Path("/tmp/pal")


def pal_log_root(runtime_root: Path | str | None = None) -> Path:
    _ = runtime_root
    override = str(os.environ.get(PAL_LOG_ROOT_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return DEFAULT_PAL_LOG_ROOT


def pal_log_path(runtime_root: Path | str | None = None) -> Path:
    return pal_log_root(runtime_root) / "pal.log"


def pal_debug_log_path(runtime_root: Path | str | None = None) -> Path:
    return pal_log_root(runtime_root) / "pal-debug.log"


def pal_component_log_path(runtime_root: Path | str | None, *parts: str) -> Path:
    return pal_log_root(runtime_root).joinpath(*[str(part) for part in parts if str(part or "").strip()])
