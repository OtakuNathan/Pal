from pal.failure import FailureRuntime, register_with_core


class FailurePlugin:
    plugin_id = "failure"
    version = "1.0.0"

    def start(self, scope):
        core = scope.core_context.require_port("core:core")
        return register_with_core(core, FailureRuntime(), context=scope.context)


def build_plugin() -> FailurePlugin:
    return FailurePlugin()
