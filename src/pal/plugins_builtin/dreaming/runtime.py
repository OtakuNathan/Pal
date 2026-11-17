from pal.memory.dreaming.runtime import register_dreaming
from pal.plugins.contracts import PluginBuildContext


class DreamingPlugin:
    plugin_id = "dreaming"
    version = "1.0.0"

    def __init__(self, runtime_root):
        self.runtime_root = runtime_root

    def start(self, scope):
        return register_dreaming(scope.context,
            scope.context.require_port("l3.sqlite_vec_l3:provider:sqlite_vec_l3"),
            scope.context.require_port("llm:llm"), self.runtime_root)


def build_plugin(context: PluginBuildContext) -> DreamingPlugin:
    return DreamingPlugin(context.runtime_root)
