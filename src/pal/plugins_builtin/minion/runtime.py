from __future__ import annotations

from dataclasses import dataclass

from pal.minion import register_with_core
from pal.plugins.contracts import PluginBuildContext


@dataclass
class MinionBuiltinBundle:
    runtime_root: object
    plugin_id: str = "minion"
    version: str = "0.1.0"

    def register_with_core(self, context):
        return register_with_core(context, runtime_root=self.runtime_root)


def build_plugin(context: PluginBuildContext) -> MinionBuiltinBundle:
    return MinionBuiltinBundle(runtime_root=context.runtime_root)
