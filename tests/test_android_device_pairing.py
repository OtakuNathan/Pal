from __future__ import annotations

import importlib
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from uuid import uuid4

import pytest


def test_registered_phones_route_explicitly_and_refresh_without_reload(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "plugins/android_browser"))
    routing = importlib.import_module("pal_android_browser.devices")
    native = importlib.import_module("pal_android_browser.client")
    registry = tmp_path / "data/android_companion/devices.json"
    registry.parent.mkdir(parents=True)
    devices = [str(uuid4()), str(uuid4())]
    calls = []
    servers = []
    real_client = native.AndroidHostClient

    def server(identifier, token):
        session = str(uuid4())
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def reply(self, value):
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            def do_GET(self):
                assert self.headers["Authorization"] == "Bearer " + token
                self.reply({"v": 1, "session": session, "id": identifier, "name": "Phone", "model": "test", "sdk": 36,
                            "ready": {"accessibility": True, "workspace": True, "human_owned": False}})
            def do_POST(self):
                assert self.headers["Authorization"] == "Bearer " + token
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert body["session"] == session
                calls.append((identifier, body["operation"]))
                self.reply({"v": 1, "id": body["id"], "kind": "completed", "value": {"apps": [{"package": identifier, "label": "fixture"}]}})
        instance = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        servers.append((instance, thread))
        return f"http://127.0.0.1:{instance.server_port}"

    origins = {"http://first.tail123.ts.net:8780": server(devices[0], "a" * 64),
               "http://second.tail123.ts.net:8780": server(devices[1], "b" * 64)}
    def resolve(url, **kwargs):
        return real_client(origins.get(url, url), **kwargs)
    monkeypatch.setattr(routing, "AndroidHostClient", resolve)
    router = routing.AndroidDeviceRouter(tmp_path)
    entries = [{"device_id": identifier, "name": name, "node_id": "node-" + name,
                "dns_name": name + ".tail123.ts.net", "rpc_token": token * 64}
               for identifier, name, token in zip(devices, ("first", "second"), ("a", "b"))]
    def save(rows):
        registry.write_text(json.dumps({"v": 1, "devices": rows}))
    try:
        save(entries[:1])
        assert router.request("apps")["value"][0]["package"] == devices[0]
        save(entries)  # Adding a phone never requires reloading the capability.
        assert {item["id"] for item in router.request("devices")["value"]} == set(devices)
        before = len(calls)
        with pytest.raises(native.AndroidError, match="explicit device") as error:
            router.request("batch", {"steps": [{"cmd": "home"}]})
        assert error.value.details["effect"] == "none" and len(calls) == before
        assert router.request("apps", device=devices[1])["value"][0]["package"] == devices[1]
        assert calls[-1] == (devices[1], {"kind": "apps"})
        with pytest.raises(native.AndroidError, match="Unknown paired"):
            router.request("apps", device=str(uuid4()))
        # A stale DNS target must fail before POST and cannot switch phones.
        origins["http://first.tail123.ts.net:8780"] = origins["http://second.tail123.ts.net:8780"]
        # Use the matching credential to isolate descriptor identity checking.
        entries[0]["rpc_token"] = "b" * 64
        save(entries)
        before = len(calls)
        with pytest.raises(native.AndroidError, match="does not own"):
            router.request("apps", device=devices[0])
        assert len(calls) == before
        registry.write_text('{"v":1,"devices":')
        with pytest.raises(native.AndroidError, match="Invalid paired"):
            router.request("apps", device=devices[1])
        assert len(calls) == before
    finally:
        for instance, thread in servers:
            instance.shutdown()
            instance.server_close()
            thread.join(2)
