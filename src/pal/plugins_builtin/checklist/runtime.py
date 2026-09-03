from pal.checklist import ChecklistService, register_with_core


class ChecklistPlugin:
    plugin_id = "checklist"
    version = "1.0.0"

    def start(self, scope):
        return register_with_core(scope.context, ChecklistService())


def build_plugin() -> ChecklistPlugin:
    return ChecklistPlugin()
