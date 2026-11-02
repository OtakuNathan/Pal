from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle

from .capabilities import AndroidProvider
from .client import AndroidHostClient
from .devices import AndroidDeviceRouter


@dataclass
class AndroidBundle:
    runtime_root: Path
    plugin_id: str = "android_browser"
    version: str = "0.5.0"

    def start(self, scope):
        path = self.runtime_root / "data" / self.plugin_id / "config.toml"
        config = tomllib.loads(path.read_text()) if path.exists() else {}
        unknown = set(config) - {"url", "token", "timeout", "protocol"}
        if unknown:
            raise ValueError(f"Unknown Android configuration keys: {sorted(unknown)}")
        provider = AndroidProvider(AndroidDeviceRouter(self.runtime_root, fallback=AndroidHostClient(**config) if config else None), runtime_root=self.runtime_root)
        handle = ModuleHandle(module_id=self.plugin_id, tier=MODULE_TIER_DETACHABLE,
                              detachable=True, introspection_provider=provider)
        scope.context.register_module(handle)
        return handle


def build_plugin(context):
    return AndroidBundle(Path(context.runtime_root))
