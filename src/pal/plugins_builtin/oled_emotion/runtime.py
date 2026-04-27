from __future__ import annotations

from pal.oled_emotion.introspection import OledEmotionIntrospectionProvider, register_with_core


def build_plugin():
    class OledEmotionBuiltinBundle:
        plugin_id = "oled_emotion"
        version = "0.1.0"

        def register_with_core(self, context):
            return register_with_core(context)

    return OledEmotionBuiltinBundle()
