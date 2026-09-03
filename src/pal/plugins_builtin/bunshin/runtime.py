from __future__ import annotations

from dataclasses import dataclass

from pal.bunshin import register_with_core
from pal.plugins.contracts import PluginBuildContext


@dataclass
class BunshinBuiltinBundle:
    runtime_root: object
    harness_registry: object | None = None
    runtime_db_path: object | None = None
    plugin_id: str = "bunshin"
    version: str = "0.1.0"

    def start(self, scope):
        handle = register_with_core(
            scope.context,
            runtime_root=self.runtime_root,
            runtime_db_path=self.runtime_db_path,
            harness_registry=self.harness_registry,
        )
        handle.ports["bunshin"].attach_manager()
        return handle


def build_plugin(context: PluginBuildContext) -> BunshinBuiltinBundle:
    return BunshinBuiltinBundle(
        runtime_root=context.runtime_root,
        harness_registry=context.services.get("bunshin_harness_registry"),
        runtime_db_path=context.services.get("runtime_db_path"),
    )
