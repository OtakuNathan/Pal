from pal.identity import IdentityRepository, IdentityService, register_with_core


class IdentityPlugin:
    plugin_id = "identity"
    version = "1.0.0"

    def start(self, scope):
        service = IdentityService(repository=IdentityRepository())
        return register_with_core(scope.context, service)


def build_plugin() -> IdentityPlugin:
    return IdentityPlugin()
