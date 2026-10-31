from __future__ import annotations

import json
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


class DroidlineClient:
    """Single-attempt HTTP transport; writes must never be silently replayed."""

    def __init__(self, url: str = "http://127.0.0.1:8780", *, token: str = "",
                 timeout: float = 90.0):
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment
                or parsed.path not in {"", "/"}):
            raise ValueError("Droidline URL must be an HTTP(S) origin without credentials")
        if timeout <= 0 or timeout > 600:
            raise ValueError("timeout must be in (0, 600] seconds")
        self.url, self.token, self.timeout = url.rstrip("/"), token, timeout
        # Local phone control should not traverse HTTP_PROXY or follow redirects.
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def request(self, command: str, args: dict | None = None, *, device: str = "_") -> dict:
        if command == "devices":
            path, body = "/devices", None
        else:
            if not device or device in {".", ".."}:
                raise ValueError("device must be a nonempty name or _")
            path = f"/devices/{quote(device, safe='')}/{quote(command, safe='')}"
            body = json.dumps({"offline_wait": 0, **(args or {})}).encode("utf-8")
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
                raw = response.read(8 * 1024 * 1024 + 1)
                status = response.code
            if len(raw) > 8 * 1024 * 1024:
                raise AndroidError("Droidline response exceeds 8 MiB")
            result = json.loads(raw)
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                raise AndroidError("Invalid Droidline response envelope")
            if status != 200 or not result["ok"]:
                raise AndroidError(str(result.get("msg") or result.get("error") or f"HTTP {status}"),
                                   details=result)
            return result
        except (URLError, OSError, ValueError) as exc:
            # No URL, token, request body, or entered text in transport diagnostics.
            raise AndroidError(f"Droidline transport failed ({type(exc).__name__})") from exc
