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


def screenshot_packet():
    import base64
    import hashlib
    png = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNg+P//PwAF/wL+Xg47rQAAAABJRU5ErkJggg==')
    return {'surface': {'epoch': 7, 'width': 1080, 'height': 1920},
            'app': {'package': 'com.example', 'activity': '.Canvas'}, 'frame_age_ms': 12,
            'image': {'mime': 'image/png', 'width': 1, 'height': 1,
                      'sha256': hashlib.sha256(png).hexdigest(), 'base64': base64.b64encode(png).decode()}}


def test_screenshot_native_wire_correlates_and_rejects_corruption_without_replay(android):
    from copy import deepcopy
    from uuid import uuid4
    packet = screenshot_packet()
    for mutation in ('none', 'hash', 'size', 'extra', 'base64', 'correlation'):
        client = android.client.AndroidHostClient(protocol='pal.android.v1')
        client._descriptor = {'id': str(uuid4()), 'session': str(uuid4())}
        value = deepcopy(packet)
        if mutation == 'hash': value['image']['sha256'] = '0' * 64
        if mutation == 'size': value['image']['width'] = 2
        if mutation == 'extra': value['surface']['display'] = 0
        if mutation == 'base64': value['image']['base64'] = 'invalid!'
        calls = []
        def http(path, body):
            calls.append(path)
            request = json.loads(body)
            assert request['operation'] == {'kind': 'screenshot', 'max_edge': 720}
            return {'v': 1, 'id': str(uuid4()) if mutation == 'correlation' else request['id'], 'kind': 'completed', 'value': value}
        client._http = http
        if mutation == 'none':
            result = client.request('screenshot', {'max_edge': 720})
            assert result['width'] == 1 and result['viewport_width'] == 1080
            assert result['png'].startswith(b'\x89PNG')
        else:
            with pytest.raises(android.client.AndroidError): client.request('screenshot', {'max_edge': 720})
        assert calls == ['/v1/control']


def test_screenshot_legacy_and_invalid_edge_never_submit_a_capture(android):
    from uuid import uuid4
    for protocol, edge in [('legacy.droidline', 720), ('pal.android.v1', True), ('pal.android.v1', 255), ('pal.android.v1', 1921)]:
        client = android.client.AndroidHostClient(protocol=protocol)
        client._descriptor = {'id': str(uuid4()), 'session': str(uuid4())}
        client._http = lambda *a: pytest.fail('must reject before HTTP')
        with pytest.raises(android.client.AndroidError): client.request('screenshot', {'max_edge': edge})


def test_screenshot_indirect_path_registers_real_conversation_artifact(android, tmp_path):
    import asyncio
    from pal.artifact import ArtifactManager, ArtifactRepository, ArtifactRecordModel, ArtifactRepresentationModel, ArtifactHotStateModel
    from pal.foundation import PalV2Database
    from pal.execution import register_with_core
    packet = screenshot_packet()
    wire = importlib.import_module('pal_android_browser.native_wire')
    client = FakeClient([wire.screenshot(packet)])
    database = PalV2Database(tmp_path / 'artifacts.sqlite3')
    database.initialize([ArtifactRecordModel, ArtifactRepresentationModel, ArtifactHotStateModel])
    core = _PalCoreBootstrap()
    register_with_core(core.context)
    core.publish_module_capabilities('execution')
    runtime = core.context.execution_runtime
    owner = ArtifactManager(runtime_root=tmp_path, repository=ArtifactRepository())
    runtime.provider_registry['artifact:artifact'] = owner
    from tests.turn_fakes import TurnIOFixture
    runtime.register_provider_ref('core:turn_io', TurnIOFixture(scope='phone-scope', capabilities={'supports_vision': True}))
    provider = android.caps.AndroidProvider(client, runtime_root=tmp_path)
    subtree = compile_provider_subtree(provider, module_id='android_browser', lifecycle_scope='runtime', detachable=True)
    runtime.mount_subtree(SimpleNamespace(mounted_subtree=subtree))
    try:
        result = asyncio.run(runtime.execute_tool_async(new_tool_call(name='call_tool', args={
            'name': 'android_screenshot', 'args': {'device': 'test-phone', 'max_edge': 720}}), turn_id='phone-shot'))
        assert result.ok, result.llm_text
        assert result.structured['registration'] == 'registered', result.llm_text
        assert Path(result.structured['path']).read_bytes() == wire.screenshot(packet)['png']
        assert packet['image']['base64'] not in result.llm_text
        assert 'base64' not in json.dumps(result.structured)
        assert result.context_messages and result.context_messages[0].artifact_ids == (result.structured['artifact_id'],)
        exposure = owner.select_prompt_exposure('phone-scope', 'phone-shot', 'inspect phone', {'supports_vision': True})
        assert exposure.inline_parts
        assert client.calls == [('screenshot', {'max_edge': 720}, 'test-phone')]
    finally:
        runtime.shutdown()
        database.close()


def test_accessibility_fault_dict_survives_indirect_output_validation(android):
    fault = {'code': 'NO_ACCESSIBILITY', 'message': 'service disabled'}
    client = FakeClient([{'ok': True, 'results': [{'ok': True}], 'screen': None, 'snapshot_error': fault}])
    subtree = compile_provider_subtree(android.caps.AndroidProvider(client), module_id='android_browser', lifecycle_scope='runtime', detachable=True)
    runtime = ExecutionRuntime()
    try:
        runtime.mount_subtree(SimpleNamespace(mounted_subtree=subtree))
        result = runtime.invoke_indirect_tool(new_tool_call(name='android_act', args={'steps': [{'cmd': 'tap', 'x': 10, 'y': 20}]}))
        assert isinstance(result, CompleteResult), result
        assert result.output['snapshot_error'] == fault
        assert len(client.calls) == 1
    finally:
        runtime.shutdown()


def test_focused_input_and_focus_observation_use_the_indirect_contract(android):
    from uuid import uuid4
    client = android.client.AndroidHostClient(protocol='pal.android.v1')
    client._descriptor = {'id': str(uuid4()), 'session': str(uuid4())}
    editor = str(uuid4())
    calls = []
    def http(path, body):
        request = json.loads(body); operation = request['operation']; calls.append(operation)
        if operation['kind'] == 'focus':
            value = {'kind': 'ready', 'editor': editor, 'package': 'com.example', 'field_id': 0}
        else:
            assert operation['actions'] == [{'kind': 'write_focused', 'editor': editor, 'package': 'com.example', 'text': '你好😀'}]
            value = {'steps': [{'kind': 'applied'}], 'observation': {'kind': 'unavailable', 'fault': {'code': 'TIMEOUT', 'message': 'no nodes'}}}
        return {'v': 1, 'id': request['id'], 'kind': 'completed', 'value': value}
    client._http = http
    subtree = compile_provider_subtree(android.caps.AndroidProvider(client), module_id='android_browser', lifecycle_scope='runtime', detachable=True)
    runtime = ExecutionRuntime()
    try:
        runtime.mount_subtree(SimpleNamespace(mounted_subtree=subtree))
        focus = runtime.invoke_indirect_tool(new_tool_call(name='android_focus', args={}))
        assert isinstance(focus, CompleteResult), focus
        assert focus.output['focus']['editor'] == editor
        result = runtime.invoke_indirect_tool(new_tool_call(name='android_act', args={'steps': [
            {'cmd': 'input', 'by': 'focus', 'package': 'com.example', 'editor': editor, 'text': '你好😀'}]}))
        assert isinstance(result, CompleteResult), result
        assert result.output['snapshot_error']['code'] == 'TIMEOUT'
        assert len(calls) == 2
    finally:
        runtime.shutdown()


@pytest.mark.parametrize('change', [
    {'editor': None}, {'package': None}, {'append': True}, {'mode': 'replace'}, {'text': ''},
])
def test_focused_input_requires_an_explicit_lease_and_has_no_silent_modes(android, change):
    step = {'cmd': 'input', 'by': 'focus', 'package': 'com.example', 'editor': 'lease', 'text': 'hello'} | change
    step = {k: v for k, v in step.items() if v is not None}
    with pytest.raises(ValidationError):
        android.models.ActInput.model_validate({'steps': [step]})


def test_legacy_host_refuses_editor_operations_before_http(android):
    client = android.client.AndroidHostClient(protocol='legacy.droidline')
    client._http = lambda *a: pytest.fail('must not submit an editor command to legacy host')
    for command, args in [('focus', {}), ('batch', {'steps': [{'cmd': 'input', 'by': 'focus', 'package': 'com.example', 'editor': 'lease', 'text': 'hello'}]})]:
        with pytest.raises(android.client.AndroidError): client.request(command, args)


def test_unavailable_focus_preserves_the_reason_without_inventing_a_lease(android):
    fault = {'code': 'FOCUS_SCOPE', 'message': 'same app on another display'}
    client = FakeClient([{'ok': True, 'focus': {'kind': 'unavailable', 'fault': fault}}])
    result = android.caps.AndroidProvider(client).focus(CapabilityCall('android_focus'))
    assert result.structured == {'focus': {'kind': 'unavailable', 'fault': fault}}
    assert 'editor' not in result.structured['focus']
