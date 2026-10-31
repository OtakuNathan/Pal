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
