from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from pal.core.module_registry import MODULE_TIER_DETACHABLE, ModuleHandle

from .capabilities import AndroidProvider
from .client import DroidlineClient


@dataclass
class AndroidBundle:
    runtime_root: Path
    plugin_id: str = "android_browser"
    version: str = "0.1.0"

    def start(self, scope):
        path = self.runtime_root / "data" / self.plugin_id / "config.toml"
        config = tomllib.loads(path.read_text()) if path.exists() else {}
        unknown = set(config) - {"url", "token", "timeout"}
        if unknown:
            raise ValueError(f"Unknown Android configuration keys: {sorted(unknown)}")
        provider = AndroidProvider(DroidlineClient(**config))
        handle = ModuleHandle(module_id=self.plugin_id, tier=MODULE_TIER_DETACHABLE,
                              detachable=True, introspection_provider=provider)
        scope.context.register_module(handle)
        return handle


def build_plugin(context):
    return AndroidBundle(Path(context.runtime_root))
