from pal.memory import L3ProviderSelector, MemoryService, register_with_core


class MemoryPlugin:
    plugin_id = "memory"
    version = "1.0.0"

    def start(self, scope):
        registry = scope.core_context.execution_runtime.l3_plugin_registry
        service = MemoryService(l3_selector=L3ProviderSelector(resolver=registry.require))
        return register_with_core(scope.context, service)


def build_plugin() -> MemoryPlugin:
    return MemoryPlugin()
