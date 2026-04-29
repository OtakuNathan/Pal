from __future__ import annotations

from dataclasses import dataclass

from pal.mcp.plugin import build_mcp_plugin
from pal.plugins.contracts import PluginBuildContext


@dataclass
class McpBuiltinBundle:
    runtime_root: object
    plugin_id: str = "mcp"
    version: str = "0.1.0"

    def register_with_core(self, context):
        return build_mcp_plugin(runtime_root=self.runtime_root).register_with_core(context)


def build_plugin(context: PluginBuildContext) -> McpBuiltinBundle:
    return McpBuiltinBundle(runtime_root=context.runtime_root)
