from __future__ import annotations

from pathlib import Path

# NOTE: this module must NOT be called ``introspection``. The oled_emotion plugin
# ships a top-level module with that exact name and Python caches imports by
# module name, so a bare ``from introspection import ...`` here silently handed
# back the OLED provider's handle (load failed with "manifest module_id
# 'st7789_face' != handle 'oled_emotion'").
from face_introspection import register_with_core


def build_plugin(context):
    # Pal plugin host calls build_plugin(context=PluginBuildContext);
    # the old (*, plugin_dir: Path) signature fails with TypeError.
    plugin_dir = context.plugin_dir

    class St7789FaceCommunityBundle:
        plugin_id = "st7789_face"
        version = "0.1.1"

        def start(self, scope):
            handle = register_with_core(scope.context, plugin_dir=plugin_dir)
            provider = handle.introspection_provider
            result = (
                provider.sidecar_manager.ensure_running()
                if provider.sidecar_manager
                else "ERR: no sidecar manager"
            )
            provider.mounted = result.startswith("already") or not result.startswith("ERR")
            provider.degraded = not provider.mounted
            if not provider.mounted:
                raise RuntimeError(result)
            provider.start_observations(scope)
            scope.defer(provider.stop_observations)
            from pal.emotion import EmotionReceiver
            from face_introspection import ACCEPTED
            registry = scope.context.require_port("emotion:emotion")

            def send(emotion):
                from pal.shared import IntrospectionCall, RuntimeStatus
                result = provider.show_face(IntrospectionCall(name="face_renderer", args={"emotion": emotion}))
                if result.status != RuntimeStatus.OK:
                    raise RuntimeError("Face panel did not accept the request; inspect st7789_face_status")

            registry.register(scope, "st7789_face", ACCEPTED, lambda: [
                EmotionReceiver("st7789_face", frozenset(ACCEPTED), send)])
            return handle

    return St7789FaceCommunityBundle()
