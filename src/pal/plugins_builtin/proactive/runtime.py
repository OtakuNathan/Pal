
from pal.plugins.contracts import PluginBuildContext
from pal.proactive import ProactiveManager, ProactiveRepository, register_with_core


class ProactivePlugin:
    plugin_id = "proactive"
    version = "1.0.0"

    def start(self, scope):
        repository = ProactiveRepository()
        manager = ProactiveManager(repository=repository)
        for stored in repository.list_definitions():
            manager.hydrate(stored.definition, next_due_at_utc=stored.next_due_at_utc)
        return register_with_core(scope.context, manager)



def build_plugin(context: PluginBuildContext) -> ProactivePlugin:
    return ProactivePlugin()
