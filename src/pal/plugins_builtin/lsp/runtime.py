from __future__ import annotations

from dataclasses import dataclass

from pal.lsp.plugin import build_lsp_plugin
from pal.plugins.contracts import PluginBuildContext


@dataclass
class LspBuiltinBundle:
    runtime_root: object
    plugin_id: str = "lsp"
    version: str = "0.1.0"

    def start(self, scope):
        handle = build_lsp_plugin(runtime_root=self.runtime_root).register_with_core(scope.context)
        handle.ports["lsp"].start_manager()
        return handle


def build_plugin(context: PluginBuildContext) -> LspBuiltinBundle:
    return LspBuiltinBundle(runtime_root=context.runtime_root)
