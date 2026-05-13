from __future__ import annotations

import importlib.util
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen


def playwright_python_available() -> bool:
    return importlib.util.find_spec("playwright") is not None


def _coerce_charset(content_type: str) -> str:
    for part in str(content_type or "").split(";"):
        key, _, value = part.strip().partition("=")
        if key.lower() == "charset" and value:
            return value.strip()
    return "utf-8"


class _HTMLTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self._in_title = False
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        _ = attrs
        lowered = tag.lower()
        if lowered == "title":
            self._in_title = True
        if lowered in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "title":
            self._in_title = False
        if lowered in {"script", "style", "noscript"} and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        text = str(data or "").strip()
        if not text:
            return
        if self._in_title:
            self.title_parts.append(text)
        self.text_parts.append(text)


def plain_http_fetch(url: str, *, timeout_ms: int = 15000, max_chars: int = 12000) -> dict[str, str]:
    request = Request(str(url), headers={"User-Agent": "PalV2/0.1"})
    with urlopen(request, timeout=max(timeout_ms, 1000) / 1000.0) as response:  # noqa: S310
        final_url = response.geturl()
        content_type = str(response.headers.get("Content-Type") or "")
        body = response.read().decode(_coerce_charset(content_type), errors="replace")
    parser = _HTMLTextParser()
    if "html" in content_type.lower():
        parser.feed(body)
        title = " ".join(parser.title_parts).strip()
        text = "\n".join(parser.text_parts).strip()
    else:
        title = ""
        text = body.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip()
    return {
        "requested_url": str(url),
        "final_url": str(final_url or url),
        "title": title,
        "text": text,
    }


class _BrowserFetchWorker:
    def __init__(self, *, max_concurrency: int) -> None:
        self.semaphore = threading.BoundedSemaphore(max(1, int(max_concurrency)))
        self.last_activity_at = time.monotonic()
        self.in_flight = 0
        self._lock = threading.Lock()

    def fetch(self, url: str, *, timeout_ms: int, max_chars: int) -> dict[str, str]:
        with self.semaphore:
            with self._lock:
                self.in_flight += 1
                self.last_activity_at = time.monotonic()
            try:
                return self._fetch_playwright(url, timeout_ms=timeout_ms, max_chars=max_chars)
            finally:
                with self._lock:
                    self.in_flight = max(0, self.in_flight - 1)
                    self.last_activity_at = time.monotonic()

    def _fetch_playwright(self, url: str, *, timeout_ms: int, max_chars: int) -> dict[str, str]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - env dependent
            raise RuntimeError(f"playwright unavailable: {exc}") from exc

        with sync_playwright() as playwright:
            launch_opts: dict[str, Any] = {"headless": True}
            proxy_url = os.environ.get("https_proxy") or os.environ.get("http_proxy") or ""
            if proxy_url:
                launch_opts["proxy"] = {"server": proxy_url}
            browser = playwright.chromium.launch(**launch_opts)
            context = browser.new_context()
            page = context.new_page()
            try:
                page.goto(str(url), wait_until="domcontentloaded", timeout=max(1000, int(timeout_ms)))
                try:
                    page.wait_for_load_state("networkidle", timeout=min(max(1000, int(timeout_ms)), 5000))
                except Exception:
                    pass
                title = str(page.title() or "").strip()
                final_url = str(page.url or url)
                text = str(page.evaluate("() => document.body ? document.body.innerText : ''") or "").strip()
                if len(text) > max_chars:
                    text = text[:max_chars].rstrip()
                return {
                    "requested_url": str(url),
                    "final_url": final_url,
                    "title": title,
                    "text": text,
                }
            finally:
                context.close()
                browser.close()


def _json_response(handler: BaseHTTPRequestHandler, status_code: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status_code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def run_browser_service_cli(
    *,
    runtime_root: Path,
    host: str,
    port: int,
    token: str,
    idle_timeout_seconds: int,
    max_concurrency: int,
) -> int:
    _ = runtime_root
    worker = _BrowserFetchWorker(max_concurrency=max_concurrency)

    class BrowserHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:  # noqa: A003
            _ = format
            _ = args

        def _authorized(self) -> bool:
            header = str(self.headers.get("Authorization") or "")
            expected = f"Bearer {token}"
            return header == expected

        def _read_json(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or "0")
            payload = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else "{}"
            decoded = json.loads(payload or "{}")
            return decoded if isinstance(decoded, dict) else {}

        def do_GET(self) -> None:  # noqa: N802
            if not self._authorized():
                _json_response(self, 401, {"ok": False, "error": "unauthorized"})
                return
            if self.path != "/health":
                _json_response(self, 404, {"ok": False, "error": "not_found"})
                return
            worker.last_activity_at = time.monotonic()
            _json_response(
                self,
                200,
                {
                    "ok": True,
                    "service": "playwright_fetch",
                    "playwright_available": playwright_python_available(),
                    "in_flight": worker.in_flight,
                },
            )

        def do_POST(self) -> None:  # noqa: N802
            if not self._authorized():
                _json_response(self, 401, {"ok": False, "error": "unauthorized"})
                return
            payload = self._read_json()
            worker.last_activity_at = time.monotonic()
            if self.path == "/shutdown":
                _json_response(self, 200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if self.path != "/fetch":
                _json_response(self, 404, {"ok": False, "error": "not_found"})
                return
            url = str(payload.get("url") or "").strip()
            if not url:
                _json_response(self, 400, {"ok": False, "error": "url is required"})
                return
            try:
                result = worker.fetch(
                    url,
                    timeout_ms=max(1000, int(payload.get("timeout_ms") or 15000)),
                    max_chars=max(1000, int(payload.get("max_chars") or 12000)),
                )
            except Exception as exc:
                _json_response(self, 500, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "result": result})

    server = ThreadingHTTPServer((host, int(port)), BrowserHandler)

    def idle_monitor() -> None:
        while True:
            time.sleep(1.0)
            if worker.in_flight > 0:
                continue
            if time.monotonic() - worker.last_activity_at < max(5, int(idle_timeout_seconds)):
                continue
            server.shutdown()
            break

    threading.Thread(target=idle_monitor, daemon=True).start()
    server.serve_forever(poll_interval=0.5)
    server.server_close()
    return 0


@dataclass
class BrowserServiceManager:
    runtime_root: Path
    process: subprocess.Popen[bytes] | None = None
    host: str = "127.0.0.1"
    port: int | None = None
    token: str = ""
    last_error: str = ""

    def fetch(self, url: str, *, timeout_ms: int, max_chars: int, settings: dict[str, Any] | None = None) -> dict[str, str]:
        self._ensure_started(settings=settings)
        payload = self._request_json(
            "POST",
            "/fetch",
            {"url": url, "timeout_ms": timeout_ms, "max_chars": max_chars},
            timeout_seconds=max(timeout_ms, 1000) / 1000.0 + 5.0,
        )
        if not bool(payload.get("ok")):
            raise RuntimeError(str(payload.get("error") or "browser fetch failed"))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("browser fetch returned invalid payload")
        self.last_error = ""
        return {
            "requested_url": str(result.get("requested_url") or url),
            "final_url": str(result.get("final_url") or url),
            "title": str(result.get("title") or ""),
            "text": str(result.get("text") or ""),
        }

    def health(self, *, settings: dict[str, Any] | None = None) -> dict[str, Any]:
        running = self._process_running()
        payload = {
            "service_running": running,
            "playwright_available": playwright_python_available(),
            "host": self.host,
            "port": self.port,
            "last_error": self.last_error,
            "idle_timeout_seconds": int((settings or {}).get("idle_timeout_seconds") or 60),
            "max_concurrency": int((settings or {}).get("max_concurrency") or 2),
        }
        if not running:
            payload["healthy"] = bool(payload["playwright_available"])
            payload["reason"] = "idle" if payload["playwright_available"] else "playwright_missing"
            return payload
        try:
            health = self._request_json("GET", "/health", None, timeout_seconds=0.5)
            payload["healthy"] = bool(health.get("ok"))
            payload["in_flight"] = int(health.get("in_flight") or 0)
            payload["reason"] = "running"
            self.last_error = ""
        except Exception as exc:
            self.last_error = str(exc)
            payload["healthy"] = False
            payload["reason"] = "health_check_failed"
        payload["last_error"] = self.last_error
        return payload

    def stop_sync(self) -> None:
        process = self.process
        if process is None:
            return
        try:
            if self._process_running():
                try:
                    self._request_json("POST", "/shutdown", {}, timeout_seconds=0.5)
                except Exception:
                    pass
                process.wait(timeout=1.0)
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=1.0)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass
        finally:
            self.process = None

    async def shutdown_async(self) -> None:
        self.stop_sync()

    def _ensure_started(self, *, settings: dict[str, Any] | None = None) -> None:
        if self._process_running():
            return
        if not playwright_python_available():
            raise RuntimeError("playwright package is not available")
        self.stop_sync()
        self.port = self._choose_port()
        self.token = secrets.token_urlsafe(24)
        idle_timeout_seconds = max(5, int((settings or {}).get("idle_timeout_seconds") or 60))
        max_concurrency = max(1, int((settings or {}).get("max_concurrency") or 2))
        command = [
            sys.executable,
            "-m",
            "pal.main",
            "browser-service",
            "--runtime-root",
            str(self.runtime_root),
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--token",
            self.token,
            "--idle-timeout-seconds",
            str(idle_timeout_seconds),
            "--max-concurrency",
            str(max_concurrency),
        ]
        self.process = subprocess.Popen(  # noqa: S603
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(self.runtime_root.parent),
        )
        started = False
        for _ in range(40):
            if not self._process_running():
                break
            try:
                payload = self._request_json("GET", "/health", None, timeout_seconds=0.25)
            except Exception:
                time.sleep(0.1)
                continue
            if bool(payload.get("ok")):
                started = True
                self.last_error = ""
                break
        if started:
            return
        self.last_error = "browser service failed to start"
        self.stop_sync()
        raise RuntimeError(self.last_error)

    def _request_json(self, method: str, path: str, payload: dict[str, Any] | None, *, timeout_seconds: float) -> dict[str, Any]:
        if self.port is None or not self.token:
            raise RuntimeError("browser service is not configured")
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "PalV2/0.1",
        }
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = Request(f"http://{self.host}:{self.port}{path}", data=data, method=method, headers=headers)
        with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
            decoded = json.loads(response.read().decode("utf-8", errors="replace"))
        if not isinstance(decoded, dict):
            raise RuntimeError("browser service returned invalid JSON")
        return decoded

    def _process_running(self) -> bool:
        if self.process is None:
            return False
        if self.process.poll() is not None:
            return False
        return True

    def _choose_port(self) -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((self.host, 0))
            sock.listen(1)
            return int(sock.getsockname()[1])
