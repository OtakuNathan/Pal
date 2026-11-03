from __future__ import annotations

import json
from uuid import UUID, uuid4

from . import native_wire
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


class AndroidError(RuntimeError):
    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.details = details or {}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class AndroidHostClient:
    """Single-attempt HTTP transport; writes must never be silently replayed."""

    def __init__(self, url: str = "http://127.0.0.1:8780", *, token: str = "",
                 timeout: float = 90.0, protocol: str = "legacy.droidline"):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ValueError("Android host URL must be an HTTP(S) origin without credentials")
        if timeout <= 0 or timeout > 600:
            raise ValueError("timeout must be in (0, 600] seconds")
        if protocol not in {"legacy.droidline", "pal.android.v1"}:
            raise ValueError("Unknown Android host protocol")
        self.protocol, self._descriptor = protocol, None
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout
        # Local phone control should not traverse HTTP_PROXY or follow redirects.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, command: str, args: dict | None = None, *, device: str = "_") -> dict:
        if self.protocol == "pal.android.v1":
            return self._native(command, args or {}, device)
        if command in {"screenshot", "focus"} or (command == "batch" and any(s.get("by") in {"focus", "clipboard"} for s in (args or {}).get("steps", []))):
            raise AndroidError("Pixel and editor input capabilities require the native Pal Android host")
        if command == "devices":
            path, body = "/devices", None
        else:
            if not device or device in {".", ".."}:
                raise ValueError("device must be a nonempty name or _")
            path = f"/devices/{quote(device, safe='')}/{quote(command, safe='')}"
            body = json.dumps({"offline_wait": 0, **(args or {})}).encode("utf-8")
        result = self._http(path, body)
        if type(result.get("ok")) is not bool:
            raise AndroidError("Invalid Droidline response envelope")
        if not result["ok"]:
            raise AndroidError(str(result.get("msg") or result.get("error") or "Host rejected request"), details=result)
        return result

    def _http(self, path: str, body: bytes | None) -> dict:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.url + path, data=body, headers=headers)
        try:
            try:
                response = self.opener.open(request, timeout=self.timeout)
            except HTTPError as exc:
                response = exc
            with response:
                raw = response.read(16 * 1024 * 1024 + 1)
                status = response.code
            if len(raw) > 16 * 1024 * 1024:
                raise AndroidError("Android response exceeds 16 MiB")
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise AndroidError("Invalid Android response envelope")
            if status != 200:
                raise AndroidError(str(result.get("msg") or result.get("error") or f"HTTP {status}"),
                                   details=result)
            return result
        except (URLError, OSError, ValueError, RecursionError) as exc:
            # No URL, token, request body, or entered text in transport diagnostics.
            raise AndroidError(f"Android transport failed ({type(exc).__name__})") from exc

    def _native(self, command: str, args: dict, device: str) -> dict:
        def descriptor():
            value = self._http("/v1/device", None)
            if type(value.get("v")) is not int or value["v"] != 1:
                raise AndroidError("Unsupported Pal Android descriptor")
            UUID(value["session"]); UUID(value["id"])
            self._descriptor = value
            return value

        try:
            if command == "devices":
                info = descriptor()
                readiness = info["ready"]
                return {"ok": True, "value": [{k: info[k] for k in ("id", "name", "model", "sdk")} | {
                    "online": True, "ready": {"a11y": readiness["accessibility"],
                    "workspace": readiness["workspace"], "paused": readiness["human_owned"]}}]}
            info = self._descriptor or descriptor()
            if device not in {"_", info["id"]}:
                raise AndroidError("Requested device does not own this host", details={"receipt_protocol": self.protocol, "effect": "none"})
            match command:
                case "dump":
                    operation = {"kind": "observe", "all_windows": args.get("all_windows", False)}
                case "find_all":
                    operation = {"kind": "query", "selector": native_wire.selector(args)}
                case "batch":
                    operation = {"kind": "execute", "actions": [native_wire.action(step) for step in args["steps"]]}
                case "wait_idle":
                    operation = {"kind": "execute", "actions": [native_wire.action({"cmd": "wait_idle", **args})]}
                case "focus":
                    operation = {"kind": "focus"}
                case "apps":
                    operation = {"kind": "apps"}
                case "screenshot":
                    edge = args.get("max_edge", 1920)
                    if type(edge) is not int or not 256 <= edge <= 1920:
                        raise ValueError("Invalid screenshot edge")
                    operation = {"kind": "screenshot", "max_edge": edge}
                case _:
                    raise ValueError("Undeclared native operation")
        except (KeyError, ValueError, TypeError) as exc:
            raise AndroidError("Native request could not be admitted", details={"receipt_protocol": self.protocol, "effect": "none"}) from exc
        request_id = str(uuid4())
        request = {"v": 1, "id": request_id, "session": info["session"], "operation": operation}
        payload = json.dumps(request, ensure_ascii=False, separators=(",", ":")).encode()
        if len(payload) > 65536:
            raise AndroidError("Native request exceeds 64 KiB", details={"receipt_protocol": self.protocol, "effect": "none"})
        result = self._http("/v1/control", payload)
        try:
            if type(result["v"]) is not int or result["v"] != 1 or result["id"] != request_id:
                raise ValueError("Invalid native response correlation")
            if result["kind"] == "rejected":
                effect = result["effect"]
                if effect not in {"none", "unknown"}:
                    raise ValueError("Unknown rejection effect")
                # Refresh is read-only and explicit on the next call. Never replay
                # a command if the server incarnation changed during this request.
                self._descriptor = None
                raise AndroidError(str(result["fault"]["message"]), details={**result, "receipt_protocol": self.protocol})
            if result["kind"] != "completed":
                raise ValueError("Unknown response kind")
            value = result["value"]
            match command:
                case "dump":
                    return native_wire.snapshot(value)
                case "find_all":
                    return {"ok": True, "value": [native_wire.node(item) for item in value["matches"]]}
                case "apps":
                    return {"ok": True, "value": value["apps"]}
                case "focus":
                    return native_wire.focus(value)
                case "screenshot":
                    image = native_wire.screenshot(value)
                    if max(image["width"], image["height"]) > args.get("max_edge", 1920):
                        raise ValueError("Screenshot exceeds the requested edge")
                    return image
                case _:
                    return native_wire.receipt(value)
        except (KeyError, ValueError, TypeError) as exc:
            raise AndroidError("Invalid native response; reconcile current state before retrying") from exc


# Existing third-party hosts retain their explicit legacy configuration.
DroidlineClient = AndroidHostClient
