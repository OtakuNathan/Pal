"""Plugin source locations, independent of host lifecycle and package services."""
from pathlib import Path


def _source_plugins_root() -> Path:
    return Path(__file__).resolve().parents[1] / "plugins_builtin"
