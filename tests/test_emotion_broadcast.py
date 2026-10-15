from dataclasses import dataclass, field

import pytest

from pal.core import PalCore
from pal.emotion import EmotionReceiver, build_emotion_handle
from pal.execution import register_with_core
from pal.shared.tool_protocol import new_tool_call


@dataclass
class Scope:
    published: bool = True
    handle: object = None
    cleanups: list = field(default_factory=list)

    def defer(self, cleanup):
        self.cleanups.append(cleanup)

    def close(self):
        self.published = False
        for cleanup in reversed(self.cleanups):
            cleanup()


@pytest.fixture
def emotions():
    core = PalCore()
    register_with_core(core.context)
    handle = build_emotion_handle(core.context)
    core.publish_module_capabilities("execution")
    core.publish_module_capabilities("emotion")
    yield core.context.execution_runtime, handle.ports["emotion"]
    core.close()


def test_single_tool_schema_uses_union_and_removes_retired_registration(emotions):
    runtime, registry = emotions
    first, second = Scope(), Scope()
    registry.register(first, "face", ("happy", "sleeping"), lambda: [])
    registry.register(second, "desktop", ("happy", "sleepy", "wink"), lambda: [])
    schema = runtime.get_tool_spec("show_emotion")["input_schema"]
    assert set(schema["properties"]) == {"emotion"}
    assert set(schema["properties"]["emotion"]["enum"]) == {"happy", "sleeping", "sleepy", "wink"}
    assert "show_emotion" in runtime.registry_generation.direct_aliases
    first.close()
    schema = runtime.get_tool_spec("show_emotion")["input_schema"]
    assert set(schema["properties"]["emotion"]["enum"]) == {"happy", "sleepy", "wink"}
    second.close()
    result = runtime.execute_tool(new_tool_call(name="show_emotion", args={"emotion": "happy"}))
    assert not result.ok
    assert result.structured["details"]["outcome"] == "unavailable"


def test_broadcast_skips_unsupported_and_attempts_every_receiver_after_failure(emotions):
    runtime, registry = emotions
    calls = []
    def fail(emotion):
        calls.append(("broken", emotion))
        raise RuntimeError("transport unavailable")
    registry.register(Scope(), "face", ("happy", "face_only"), lambda: [
        EmotionReceiver("face", frozenset({"happy", "face_only"}), lambda e: calls.append(("face", e)))])
    registry.register(Scope(), "desktop", ("happy", "desktop_only"), lambda: [
        EmotionReceiver("broken", frozenset({"happy", "desktop_only"}), fail),
        EmotionReceiver("desktop-2", frozenset({"happy", "desktop_only"}), lambda e: calls.append(("desktop-2", e)))])
    result = runtime.execute_tool(new_tool_call(name="show_emotion", args={"emotion": "face_only"}))
    assert result.ok
    assert calls == [("face", "face_only")]
    assert {item["renderer"] for item in result.structured["skipped"]} == {"broken", "desktop-2"}
    result = runtime.execute_tool(new_tool_call(name="show_emotion", args={"emotion": "happy"}))
    assert result.ok
    assert result.structured["outcome"] == "partial"
    assert result.structured["accepted"] == ["face", "desktop-2"]
    assert result.structured["failures"] == [{"renderer": "broken", "reason": "transport unavailable"}]
    assert calls.count(("desktop-2", "happy")) == 1


def test_all_unsupported_is_not_claimed_as_accepted(emotions):
    runtime, registry = emotions
    calls = []
    registry.register(Scope(), "varying", ("happy", "wink"), lambda: [
        EmotionReceiver("current", frozenset({"wink"}), lambda e: calls.append(e))])
    result = runtime.execute_tool(new_tool_call(name="show_emotion", args={"emotion": "happy"}))
    assert not result.ok
    assert result.structured["details"]["outcome"] == "unsupported"
    assert calls == []


def test_unpublished_renderer_never_receives_calls(emotions):
    runtime, registry = emotions
    calls = []
    scope = Scope(published=False)
    registry.register(scope, "pending", ("happy", "wink"), lambda: [
        EmotionReceiver("pending", frozenset({"happy"}), lambda e: calls.append(e))])
    assert not runtime.execute_tool(new_tool_call(name="show_emotion", args={"emotion": "happy"})).ok
    assert calls == []
    scope.published = True
    assert runtime.execute_tool(new_tool_call(name="show_emotion", args={"emotion": "happy"})).ok
    assert calls == ["happy"]


def test_single_value_schema_and_registration_cleanup(emotions):
    runtime, registry = emotions
    scope = Scope()
    registry.register(scope, "one", ("happy",), lambda: [])
    schema = runtime.get_tool_spec("show_emotion")["input_schema"]
    assert schema["properties"]["emotion"]["const"] == "happy"
    scope.close()
    assert registry.vocabulary() == []


def test_real_plugin_reload_and_detach_replace_renderer_generation(tmp_path, monkeypatch):
    import shutil
    from pathlib import Path
    from pal.plugins import PluginHost
    builtin = tmp_path / "builtin"
    builtin.mkdir()
    shutil.copytree(Path(__file__).parents[1] / "src/pal/plugins_builtin/emotion", builtin / "emotion")
    renderer = builtin / "test_emotion_renderer"
    renderer.mkdir()
    (renderer / "plugin.toml").write_text('''plugin_id = "test_emotion_renderer"
entrypoint = "test_emotion_renderer_runtime"
version = "1.0.0"
enabled_by_default = true
lifecycle_protocol = "raii.v1"
module_id = "test_emotion_renderer"
requires_plugins = ["emotion"]
requires_ports = ["emotion:emotion"]
''')
    (builtin / "test_emotion_renderer_runtime.py").write_text('''
from pal.core.module_registry import ModuleHandle, MODULE_TIER_DETACHABLE
from pal.emotion import EmotionReceiver
class Plugin:
    def __init__(self, ledger): self.ledger = ledger
    def start(self, scope):
        token = object()
        self.ledger.append(("start", token))
        handle = ModuleHandle(module_id="test_emotion_renderer", tier=MODULE_TIER_DETACHABLE, detachable=True)
        scope.context.register_module(handle)
        registry = scope.context.require_port("emotion:emotion")
        registry.register(scope, "test", ("happy", "wink"), lambda: [EmotionReceiver("test", frozenset({"happy", "wink"}), lambda e: self.ledger.append((e, token)))])
        return handle
def build_plugin(context): return Plugin(context.services["ledger"])
''')
    monkeypatch.syspath_prepend(str(builtin))
    ledger = []
    core = PalCore()
    register_with_core(core.context)
    core.publish_module_capabilities("execution")
    host = PluginHost(context=core.context, runtime_root=tmp_path, builtin_root=builtin, services={"ledger": ledger})
    try:
        host.rescan()
        assert host.enable("test_emotion_renderer")["status"] == "ok"
        runtime = core.context.execution_runtime
        def broadcast(): return runtime.execute_tool(new_tool_call(name="show_emotion", args={"emotion": "happy"}))
        assert broadcast().ok
        original = ledger[-1][1]
        assert host.reattach("emotion")["status"] == "ok"
        assert broadcast().ok
        assert ledger[-1][1] is not original
        assert sum(event == "happy" for event, token in ledger) == 2
        assert host.detach("test_emotion_renderer")["status"] == "ok"
        assert not broadcast().ok
        assert core.context.require_port("emotion:emotion").vocabulary() == []
        assert host.attach("test_emotion_renderer")["status"] == "ok"
        assert host.detach("emotion")["status"] == "ok"
        assert runtime.get_tool_spec("show_emotion") is None
        assert "test_emotion_renderer" not in host.generations
    finally:
        host.shutdown()
        core.close()
