from pal.emotion import build_emotion_handle


class EmotionBundle:
    plugin_id = "emotion"
    version = "0.1.0"

    def start(self, scope):
        handle = build_emotion_handle(scope.context)
        # The registry outlives its renderer contributions and owns only its
        # own capability subtree. Use the resident context for later refreshes.
        handle.ports["emotion"].context = scope.core_context
        return handle


def build_plugin(context):
    return EmotionBundle()
