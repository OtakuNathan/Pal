from dataclasses import dataclass

from pal.artifact import ArtifactManager, ArtifactRepository, register_with_core
from pal.plugins.contracts import PluginBuildContext


@dataclass
class ArtifactPlugin:
    runtime_root: object
    plugin_id: str = "artifact"
    version: str = "1.0.0"

    def start(self, scope):
        service = ArtifactManager(runtime_root=self.runtime_root, repository=ArtifactRepository())
        service.recover_lifecycle()
        return register_with_core(scope.context, service)


def build_plugin(context: PluginBuildContext) -> ArtifactPlugin:
    return ArtifactPlugin(context.runtime_root)
