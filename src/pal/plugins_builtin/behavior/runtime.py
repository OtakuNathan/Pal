from pal.behavior import BehaviorRepository, BehaviorService, register_with_core
from pal.skill import SkillRepository


class BehaviorPlugin:
    plugin_id = "behavior"
    version = "1.0.0"

    def start(self, scope):
        skill = scope.core_context.port_registry.get("skill:skill")
        skill_repository = getattr(skill, "repository", None) or SkillRepository()
        repository = BehaviorRepository(skill_repository=skill_repository)
        service = BehaviorService(
            repository=repository,
            skill_repository=skill_repository,
            execution_runtime=scope.core_context.execution_runtime,
        )
        return register_with_core(scope.context, service)


def build_plugin() -> BehaviorPlugin:
    return BehaviorPlugin()
