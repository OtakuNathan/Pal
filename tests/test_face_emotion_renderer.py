"""Versioned face contribution can be checked without a display or sidecar."""
import importlib
from pathlib import Path
from types import SimpleNamespace

from pal.shared import IntrospectionCall


def test_face_maps_alias_locally_and_contributes_no_duplicate_action(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parents[1] / "plugins/st7789_face"))
    module = importlib.import_module("face_introspection")
    commands = []
    monkeypatch.setattr(module, "_send_command", lambda command, path: commands.append(command) or "OK")
    provider = module.FaceIntrospectionProvider(sidecar_manager=SimpleNamespace(sidecar_socket=Path("/unused")))
    result = provider.show_face(IntrospectionCall(name="internal_renderer", args={"emotion": "stretch"}))
    assert result.status == "ok"
    assert commands == [module.MANUAL_STATE_PREFIX + "greeting"]
    from pal.execution.capability_compiler import compile_provider_subtree
    subtree = compile_provider_subtree(provider, module_id="st7789_face", lifecycle_scope="module", detachable=True)
    assert {alias for d in subtree.descriptors for alias in d.aliases} == {"st7789_face_status"}
    result = provider.show_face(IntrospectionCall(name="internal_renderer", args={"emotion": "unknown"}))
    assert result.status == "invalid"
    assert len(commands) == 1
