from __future__ import annotations

import importlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pal.core import PalCore as _PalCoreBootstrap
from pal.execution.capability_compiler import compile_provider_subtree
from pal.execution.contracts import CapabilityCall
from pal.execution.runtime import ExecutionRuntime
from pal.execution.tool_facade import ToolExecutionError
from pal.shared.tool_protocol import CompleteResult, EffectOutcome, RetryDirective, new_tool_call


@pytest.fixture
def android(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "plugins/android_browser"))
    return SimpleNamespace(
        caps=importlib.import_module("pal_android_browser.capabilities"),
        client=importlib.import_module("pal_android_browser.client"),
        models=importlib.import_module("pal_android_browser.models"),
        plugin=importlib.import_module("pal_android_browser.plugin"),
    )


def snapshot():
    return {"ok": True, "package": "com.android.settings", "activity": ".Settings",
            "width": 1080, "height": 2400,
            "tree": {"class": "Layout", "enabled": True, "clickable": False,
                     "children": [{"text": "Settings", "id": "title", "bounds": [0, 0, 300, 60]},
                                  {"text": "secret", "desc": "secret", "password": True,
                                   "editable": True, "enabled": False}]}}


class FakeClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, command, args=None, *, device="_"):
        self.calls.append((command, args, device))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def test_snapshot_is_self_contained_and_preserves_context(android):
    provider = android.caps.AndroidProvider(FakeClient([snapshot()]))
    result = provider.observe(CapabilityCall("android_observe"))
    data = result.structured
    assert data["package"] == "com.android.settings"
    assert data["tree"]["class"] == "Layout"
    assert data["tree"]["children"][0]["id"] == "title"
    password = data["tree"]["children"][1]
    assert password["text"] == password["desc"] == "[password]"
    assert password["enabled"] is False
    assert "secret" not in result.llm_text
    assert "clickable" not in data["tree"]


def test_all_four_aliases_compile_and_invoke_through_indirect_path(android):
    client = FakeClient([{"ok": True, "value": [{"id": "test", "online": True}]},
                         snapshot(), {"ok": True, "value": []},
                         {"ok": True, "results": [{"ok": True}]}, {"ok": True}, snapshot()])
    subtree = compile_provider_subtree(android.caps.AndroidProvider(client), module_id="android_browser",
                                       lifecycle_scope="runtime", detachable=True)
    runtime = ExecutionRuntime()
    try:
        runtime.mount_subtree(SimpleNamespace(mounted_subtree=subtree))
        for alias, args in [("android_devices", {}), ("android_observe", {}),
                            ("android_query", {"by": "id", "value": "title"}),
                            ("android_act", {"steps": [{"cmd": "home"}]})]:
            result = runtime.invoke_indirect_tool(new_tool_call(name=alias, args=args))
            assert isinstance(result, CompleteResult), result
        assert result.output["screen"]["package"] == "com.android.settings"
    finally:
        runtime.shutdown()


def test_uncertain_write_never_retries(android):
    client = FakeClient([android.client.AndroidError("connection lost")])
    provider = android.caps.AndroidProvider(client)
    with pytest.raises(ToolExecutionError) as caught:
        provider.act(CapabilityCall("android_act", {"steps": [{"cmd": "home"}]}))
    assert caught.value.effect_receipt.outcome == EffectOutcome.UNKNOWN
    assert caught.value.retry == RetryDirective.RECONCILE_FIRST
    assert len(client.calls) == 1


def test_partial_batch_preserves_receipts_and_current_screen(android):
    steps = [{"ok": True}, {"ok": False, "error": "NOT_FOUND"}]
    client = FakeClient([{"ok": True, "results": steps}, {"ok": True}, snapshot()])
    with pytest.raises(ToolExecutionError) as caught:
        android.caps.AndroidProvider(client).act(CapabilityCall("android_act", {
            "steps": [{"cmd": "home"}, {"cmd": "touch", "by": "id", "value": "missing"}]}))
    assert caught.value.details["results"] == steps
    assert caught.value.details["screen"]["package"] == "com.android.settings"
    assert caught.value.retry == RetryDirective.RECONCILE_FIRST


def test_snapshot_failure_does_not_erase_applied_actions(android):
    client = FakeClient([{"ok": True, "results": [{"ok": True}]},
                         android.client.AndroidError("animating"), android.client.AndroidError("offline")])
    result = android.caps.AndroidProvider(client).act(CapabilityCall("android_act", {"steps": [{"cmd": "home"}]}))
    assert result.effect_receipt.outcome == EffectOutcome.APPLIED
    assert result.structured["snapshot_error"] == "offline"
    assert result.structured["screen"] is None


@pytest.mark.parametrize("steps", [[], [{"cmd": "shell", "command": "anything"}],
                                  [{"cmd": "home", "text": "unused"}], [{"cmd": "touch", "by": "id"}],
                                  [{"cmd": "home"}] * 9])
def test_invalid_or_unbounded_actions_rejected_before_transport(android, steps):
    client = FakeClient([])
    with pytest.raises(ValidationError):
        android.caps.AndroidProvider(client).act(CapabilityCall("android_act", {"steps": steps}))
    assert client.calls == []


def test_http_error_redirect_and_device_path_handling(android):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            seen.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))) )
            if self.path.endswith("/redirect"):
                self.send_response(307)
                self.send_header("Location", "/devices/other/touch")
                self.end_headers()
                self.wfile.write(b'{"ok":false,"error":"REDIRECT"}')
                return
            self.send_response(409)
            self.end_headers()
            self.wfile.write(b'{"ok":false,"error":"DEVICE_OFFLINE","msg":"offline"}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = android.client.DroidlineClient(f"http://127.0.0.1:{server.server_port}")
        with pytest.raises(android.client.AndroidError) as caught:
            client.request("dump", device="phone/a ?")
        assert caught.value.details["error"] == "DEVICE_OFFLINE"
        assert seen[0] == ("/devices/phone%2Fa%20%3F/dump", {"offline_wait": 0})
        with pytest.raises(android.client.AndroidError):
            client.request("redirect")
        assert len(seen) == 2
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_bundle_build_has_no_network_or_side_effects(android, tmp_path):
    bundle = android.plugin.build_plugin(SimpleNamespace(runtime_root=tmp_path))
    registered = []
    handle = bundle.start(SimpleNamespace(context=SimpleNamespace(register_module=registered.append)))
    assert handle.module_id == "android_browser"
    assert registered == [handle]
    assert not list(tmp_path.iterdir())


def test_phone_embedded_snapshot_uses_one_request(android):
    state = snapshot()
    state.update(workspace="pal", generation=7, truncated=True)
    client = FakeClient([{"ok": True, "results": [{"ok": True}], "screen": state, "snapshot_error": None}])
    result = android.caps.AndroidProvider(client).act(CapabilityCall("android_act", args={"steps": [{"cmd": "back"}]}))
    assert [item[0] for item in client.calls] == ["batch"]
    assert result.structured["screen"]["generation"] == 7
    assert result.structured["screen"]["truncated"] is True
    assert result.effect_receipt.outcome == EffectOutcome.APPLIED


def test_embedded_snapshot_failure_keeps_applied_receipt(android):
    client = FakeClient([{"ok": True, "results": [{"ok": True}], "screen": None, "snapshot_error": "workspace lost"}])
    result = android.caps.AndroidProvider(client).act(CapabilityCall("android_act", args={"steps": [{"cmd": "back"}]}))
    assert len(client.calls) == 1
    assert result.effect_receipt.outcome == EffectOutcome.APPLIED
    assert result.structured["snapshot_error"] == "workspace lost"


def test_apps_discovery_returns_labels_and_packages(android):
    apps = [{"package": "com.example", "label": "Example"}]
    client = FakeClient([{"ok": True, "value": apps}])
    result = android.caps.AndroidProvider(client).apps(CapabilityCall("android_apps"))
    assert result.structured == {"apps": apps}
    assert client.calls[0][0] == "apps"


def test_native_protocol_uses_correlated_rpc_and_keeps_ordinal_explicit(android):
    from uuid import uuid4
    calls = []
    descriptor = {"v": 1, "id": str(uuid4()), "session": str(uuid4())}
    client = android.client.AndroidHostClient(protocol="pal.android.v1")

    def http(path, body):
        calls.append((path, body))
        if path == "/v1/device":
            return descriptor
        packet = json.loads(body)
        assert packet["session"] == descriptor["session"]
        actions = packet["operation"]["actions"]
        assert "ordinal" not in actions[0]["target"]
        assert actions[1]["target"]["ordinal"] == 0
        return {"v": 1, "id": packet["id"], "kind": "completed", "value": {
            "steps": [{"kind": "applied"}, {"kind": "applied"}],
            "observation": {"kind": "unavailable", "fault": {"code": "NO_A11Y", "message": "offline"}}}}
    client._http = http
    result = android.caps.AndroidProvider(client).act(CapabilityCall("android_act", {"steps": [
        {"cmd": "touch", "by": "text", "value": "确认"},
        {"cmd": "input", "by": "id", "value": "field", "text": "你好", "nth": 0}]}))
    assert result.effect_receipt.outcome == EffectOutcome.APPLIED
    assert result.structured["snapshot_error"]["code"] == "NO_A11Y"
    assert [c[0] for c in calls] == ["/v1/device", "/v1/control"]


@pytest.mark.parametrize("steps", [[{"cmd": "home"}, {"cmd": "recents"}]])
def test_native_unsupported_batch_is_rejected_before_any_write(android, steps):
    from uuid import uuid4
    client = android.client.AndroidHostClient(protocol="pal.android.v1")
    calls = []
    def http(path, body):
        calls.append(path)
        return {"v": 1, "id": str(uuid4()), "session": str(uuid4())}
    client._http = http
    with pytest.raises(ToolExecutionError) as caught:
        android.caps.AndroidProvider(client).act(CapabilityCall("android_act", {"steps": steps}))
    assert calls == ["/v1/device"]
    assert caught.value.effect_receipt.outcome == EffectOutcome.NOT_APPLIED


@pytest.mark.parametrize("receipt,outcome", [
    ([{"kind": "rejected", "fault": {"code": "AMBIGUOUS_TARGET"}}], EffectOutcome.NOT_APPLIED),
    ([{"kind": "applied"}, {"kind": "rejected", "fault": {"code": "NOT_FOUND"}}], EffectOutcome.APPLIED),
    ([{"kind": "indeterminate", "fault": {"code": "TIMEOUT"}}], EffectOutcome.UNKNOWN),
])
def test_native_partial_effect_receipts_are_preserved(android, receipt, outcome):
    from uuid import uuid4
    client = android.client.AndroidHostClient(protocol="pal.android.v1")
    client._descriptor = {"id": str(uuid4()), "session": str(uuid4())}
    seen = []
    def http(path, body):
        seen.append(path)
        return {"v": 1, "id": json.loads(body)["id"], "kind": "completed", "value": {
            "steps": receipt, "observation": {"kind": "unavailable", "fault": {"code": "LOST"}}}}
    client._http = http
    with pytest.raises(ToolExecutionError) as caught:
        android.caps.AndroidProvider(client).act(CapabilityCall("android_act", {"steps": [{"cmd": "home"}, {"cmd": "back"}]}))
    assert caught.value.effect_receipt.outcome == outcome
    assert seen == ["/v1/control"]


def test_native_wrong_correlation_is_unknown_and_never_replayed(android):
    from uuid import uuid4
    client = android.client.AndroidHostClient(protocol="pal.android.v1")
    client._descriptor = {"id": str(uuid4()), "session": str(uuid4())}
    seen = []
    def http(path, body):
        seen.append(path)
        return {"v": 1, "id": str(uuid4()), "kind": "rejected", "effect": "none"}
    client._http = http
    with pytest.raises(ToolExecutionError) as caught:
        android.caps.AndroidProvider(client).act(CapabilityCall("android_act", {"steps": [{"cmd": "home"}]}))
    assert caught.value.effect_receipt.outcome == EffectOutcome.UNKNOWN
    assert seen == ["/v1/control"]
