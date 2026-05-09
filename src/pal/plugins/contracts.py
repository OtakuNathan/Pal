from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

if False:  # pragma: no cover
    from pal.core.main_context import MainContext
    from pal.core.module_registry import ModuleHandle


PLUGIN_SOURCE_FIRST_PARTY = "first_party"
PLUGIN_SOURCE_THIRD_PARTY = "third_party"

PLUGIN_STATUS_DISCOVERED = "discovered"
PLUGIN_STATUS_LOADED = "loaded"
PLUGIN_STATUS_ATTACHED = "attached"
PLUGIN_STATUS_DETACHED = "detached"
PLUGIN_STATUS_DISABLED = "disabled"
PLUGIN_STATUS_LOAD_FAILED = "load_failed"
PLUGIN_STATUS_UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class PluginManifest:
    plugin_id: str
    entrypoint: str
    version: str
    enabled_by_default: bool = True
    filesystem_path: str = ""
    subscribed_events: tuple[str, ...] = ()
    reload_modules: tuple[str, ...] = ()


@dataclass
class PluginRecord:
    plugin_id: str
    source: str
    entrypoint: str
    version: str
    filesystem_path: str
    enabled: bool
    attached: bool
    last_load_status: str
    last_error: str | None = None
    module_id: str | None = None
    config: dict[str, Any] = field(default_factory=dict)


class FirstPartyPluginBundle(Protocol):
    plugin_id: str
    version: str

    def register_with_core(self, context: "MainContext") -> "ModuleHandle":
        ...


@dataclass(frozen=True)
class PluginBuildContext:
    runtime_root: Path
    services: dict[str, Any] = field(default_factory=dict)
    plugin_dir: Path | None = None
