from pal.control import ControlPlane, register_with_core


class ControlPlugin:
    plugin_id = "control"
    version = "1.0.0"

    def start(self, scope):
        return register_with_core(scope.context, ControlPlane())


def build_plugin() -> ControlPlugin:
    return ControlPlugin()
