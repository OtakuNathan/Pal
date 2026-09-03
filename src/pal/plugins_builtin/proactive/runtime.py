from pal.proactive import ProactiveManager, ProactiveRepository, ProactiveRunner, register_with_core


class ProactivePlugin:
    plugin_id = "proactive"
    version = "1.0.0"

    def start(self, scope):
        repository = ProactiveRepository()
        manager = ProactiveManager(repository=repository)
        runner = ProactiveRunner(repository=repository)
        for stored in repository.list_definitions():
            manager.hydrate(stored.definition, next_due_at_utc=stored.next_due_at_utc)
        handle = register_with_core(scope.context, manager, runner)
        for handlers in handle.event_handlers.values():
            for handler in handlers:
                if hasattr(handler, "lifecycle_gate"):
                    handler.lifecycle_gate = scope.core_context.execution_runtime.lifecycle_gate
                tasks = getattr(handler, "tasks", None)
                if tasks is not None:
                    scope.defer(lambda tasks=tasks: [task.cancel() for task in tuple(tasks) if not task.done()])
        return handle


def build_plugin() -> ProactivePlugin:
    return ProactivePlugin()
