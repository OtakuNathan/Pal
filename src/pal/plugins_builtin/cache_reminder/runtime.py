from pal.cache_reminder.runtime import register_with_core


class CacheReminderPlugin:
    def start(self, scope):
        return register_with_core(scope.context)


def build_plugin(context):
    return CacheReminderPlugin()
