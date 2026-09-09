"""Runtime selection shared by public CLI commands."""
from __future__ import annotations

import os
from pathlib import Path

RUNTIME_ROOT_HELP = "Pal runtime root (default: ~/.pal if present, otherwise $PAL_HOME)"
RUNTIME_ROOT_HINT = "Set PAL_HOME to your runtime directory or pass --runtime-root /actual/path."


def default_runtime_root() -> Path:
    conventional = Path.home() / '.pal'
    if conventional.is_dir():
        return conventional
    configured = os.environ.get('PAL_HOME', '').strip()
    return Path(configured).expanduser().resolve() if configured else conventional
