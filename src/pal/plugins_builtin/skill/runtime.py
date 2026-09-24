from pal.skill import SkillRepository, SkillService, register_with_core


class SkillPlugin:
    plugin_id = "skill"
    version = "1.0.0"

    def start(self, scope):
        repository = SkillRepository()
        service = SkillService(
            repository=repository,
            llm_runtime=scope.core_context.port_registry.get("llm:llm"),
            runtime_root=scope.core_context.execution_runtime.runtime_root,
        )
        return register_with_core(scope.context, service)


def build_plugin() -> SkillPlugin:
    return SkillPlugin()
