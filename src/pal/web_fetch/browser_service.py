from __future__ import annotations

import importlib.util
import json
import os
import base64
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

from pal.web_fetch.contracts import DEFAULT_WEB_FETCH_USER_AGENT, WebFetchDocument, WebFetchLink


_LAYOUT_STYLE_PROPERTIES = (
    "display",
    "position",
    "box-sizing",
    "width",
    "height",
    "min-width",
    "min-height",
    "max-width",
    "max-height",
    "margin-top",
    "margin-right",
    "margin-bottom",
    "margin-left",
    "padding-top",
    "padding-right",
    "padding-bottom",
    "padding-left",
    "gap",
    "row-gap",
    "column-gap",
    "white-space",
    "line-height",
    "font-size",
    "overflow",
    "overflow-x",
    "overflow-y",
    "align-items",
    "justify-content",
    "flex-direction",
    "grid-template-columns",
    "grid-template-rows",
    "list-style-position",
    "list-style-type",
    "visibility",
    "opacity",
    "transform",
)


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
        self.links: list[dict[str, str]] = []
        self.metadata: dict[str, str] = {}
        self._in_title = False
        self._skip_depth = 0
        self._anchor_stack: list[int] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        attr_map = {str(key).lower(): str(value or "") for key, value in list(attrs or [])}
        lowered = tag.lower()
        if lowered == "title":
            self._in_title = True
        if lowered == "a" and attr_map.get("href"):
            self.links.append(
                {
                    "href": attr_map["href"].strip(),
                    "text": "",
                    "rel": attr_map.get("rel", "").strip(),
                }
            )
            self._anchor_stack.append(len(self.links) - 1)
        if lowered == "link" and "canonical" in attr_map.get("rel", "").lower() and attr_map.get("href"):
            self.metadata["canonical_url"] = attr_map["href"].strip()
        if lowered == "html" and attr_map.get("lang"):
            self.metadata["language"] = attr_map["lang"].strip()
        if lowered == "meta":
            name = (attr_map.get("name") or attr_map.get("property") or "").lower()
            content = attr_map.get("content", "").strip()
            if content and name in {"description", "og:description", "og:title"}:
                self.metadata[name.replace("og:", "open_graph_")] = content
        if lowered in {"script", "style", "noscript"}:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "title":
            self._in_title = False
        if lowered == "a" and self._anchor_stack:
            self._anchor_stack.pop()
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
        if self._anchor_stack:
            index = self._anchor_stack[-1]
            existing = self.links[index].get("text", "")
            self.links[index]["text"] = f"{existing} {text}".strip()
        self.text_parts.append(text)


def plain_http_fetch(
    url: str,
    *,
    timeout_ms: int = 15000,
    max_chars: int = 12000,
    max_raw_chars: int = 50000,
    max_links: int = 80,
    user_agent: str = DEFAULT_WEB_FETCH_USER_AGENT,
) -> WebFetchDocument:
    request = Request(str(url), headers={"User-Agent": str(user_agent or DEFAULT_WEB_FETCH_USER_AGENT)})
    with urlopen(request, timeout=max(timeout_ms, 1000) / 1000.0) as response:  # noqa: S310
        final_url = response.geturl()
        content_type = str(response.headers.get("Content-Type") or "")
        headers = _headers_to_dict(response.headers)
        raw = response.read()
        body = raw.decode(_coerce_charset(content_type), errors="replace")
        raw_content_truncated = len(body) > max_raw_chars
        raw_content = body[:max_raw_chars].rstrip() if raw_content_truncated else body
        status_code = int(getattr(response, "status", 0) or 0) or None
    parser = _HTMLTextParser()
    if "html" in content_type.lower():
        parser.feed(body)
        title = " ".join(parser.title_parts).strip()
        text = "\n".join(parser.text_parts).strip()
    else:
        title = ""
        text = body.strip()
    text_truncated = len(text) > max_chars
    if text_truncated:
        text = text[:max_chars].rstrip()
    links = tuple(
        WebFetchLink(href=item["href"], text=item.get("text", ""), rel=item.get("rel", ""))
        for item in _dedupe_links(parser.links)[: max(0, int(max_links))]
    )
    return WebFetchDocument(
        requested_url=str(url),
        final_url=str(final_url or url),
        title=title,
        text=text,
        raw_content=raw_content,
        raw_content_truncated=raw_content_truncated,
        status_code=status_code,
        content_type=content_type,
        content_length=len(raw),
        text_truncated=text_truncated,
        links=links,
        metadata=parser.metadata,
        response_headers=_safe_response_headers(headers),
    )


def _headers_to_dict(headers: Any) -> dict[str, str]:
    items = getattr(headers, "items", None)
    if callable(items):
        return {str(key): str(value) for key, value in items()}
    return {}


def _safe_response_headers(headers: dict[str, str]) -> dict[str, str]:
    allowed = {
        "cache-control",
        "content-language",
        "content-length",
        "content-type",
        "etag",
        "last-modified",
        "location",
        "server",
    }
    return {key.lower(): value for key, value in headers.items() if key.lower() in allowed}


def _dedupe_links(links: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    result: list[dict[str, str]] = []
    for link in links:
        href = str(link.get("href") or "").strip()
        if not href or href in seen:
            continue
        seen.add(href)
        result.append(
            {
                "href": href,
                "text": " ".join(str(link.get("text") or "").split()),
                "rel": " ".join(str(link.get("rel") or "").split()),
            }
        )
    return result


class _BrowserFetchWorker:
    def __init__(self, *, max_concurrency: int) -> None:
        self.semaphore = threading.BoundedSemaphore(max(1, int(max_concurrency)))
        self.last_activity_at = time.monotonic()
        self.in_flight = 0
        self._lock = threading.Lock()

    def fetch(
        self,
        url: str,
        *,
        timeout_ms: int,
        max_chars: int,
        max_raw_chars: int,
        max_links: int,
        user_agent: str,
    ) -> WebFetchDocument:
        with self.semaphore:
            with self._lock:
                self.in_flight += 1
                self.last_activity_at = time.monotonic()
            try:
                return self._fetch_playwright(
                    url,
                    timeout_ms=timeout_ms,
                    max_chars=max_chars,
                    max_raw_chars=max_raw_chars,
                    max_links=max_links,
                    user_agent=user_agent,
                )
            finally:
                with self._lock:
                    self.in_flight = max(0, self.in_flight - 1)
                    self.last_activity_at = time.monotonic()

    def screenshot(
        self,
        url: str,
        *,
        timeout_ms: int,
        full_page: bool,
        viewport_width: int,
        viewport_height: int,
        user_agent: str,
    ) -> dict[str, Any]:
        with self.semaphore:
            with self._lock:
                self.in_flight += 1
                self.last_activity_at = time.monotonic()
            try:
                return self._screenshot_playwright(
                    url,
                    timeout_ms=timeout_ms,
                    full_page=full_page,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    user_agent=user_agent,
                )
            finally:
                with self._lock:
                    self.in_flight = max(0, self.in_flight - 1)
                    self.last_activity_at = time.monotonic()

    def inspect_layout(
        self,
        url: str,
        *,
        selector: str,
        timeout_ms: int,
        viewport_width: int,
        viewport_height: int,
        max_elements: int,
        user_agent: str,
    ) -> dict[str, Any]:
        with self.semaphore:
            with self._lock:
                self.in_flight += 1
                self.last_activity_at = time.monotonic()
            try:
                return self._inspect_layout_playwright(
                    url,
                    selector=selector,
                    timeout_ms=timeout_ms,
                    viewport_width=viewport_width,
                    viewport_height=viewport_height,
                    max_elements=max_elements,
                    user_agent=user_agent,
                )
            finally:
                with self._lock:
                    self.in_flight = max(0, self.in_flight - 1)
                    self.last_activity_at = time.monotonic()

    def _fetch_playwright(
        self,
        url: str,
        *,
        timeout_ms: int,
        max_chars: int,
        max_raw_chars: int,
        max_links: int,
        user_agent: str,
    ) -> WebFetchDocument:
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
            context = browser.new_context(user_agent=str(user_agent or DEFAULT_WEB_FETCH_USER_AGENT))
            page = context.new_page()
            try:
                response = page.goto(str(url), wait_until="domcontentloaded", timeout=max(1000, int(timeout_ms)))
                try:
                    page.wait_for_load_state("networkidle", timeout=min(max(1000, int(timeout_ms)), 5000))
                except Exception:
                    pass
                title = str(page.title() or "").strip()
                final_url = str(page.url or url)
                extracted = page.evaluate(
                    """
                    (maxLinks) => {
                      const metadata = {};
                      const canonical = document.querySelector('link[rel~="canonical"]');
                      if (canonical && canonical.href) metadata.canonical_url = canonical.href;
                      if (document.documentElement && document.documentElement.lang) {
                        metadata.language = document.documentElement.lang;
                      }
                      for (const selector of [
                        ['description', 'meta[name="description"]'],
                        ['open_graph_description', 'meta[property="og:description"]'],
                        ['open_graph_title', 'meta[property="og:title"]']
                      ]) {
                        const node = document.querySelector(selector[1]);
                        if (node && node.content) metadata[selector[0]] = node.content;
                      }
                      const links = Array.from(document.querySelectorAll('a[href]'))
                        .slice(0, maxLinks)
                        .map((a) => ({
                          href: a.href || '',
                          text: (a.innerText || a.textContent || '').trim(),
                          rel: a.rel || ''
                        }));
                      return {
                        text: document.body ? document.body.innerText || '' : '',
                        content_type: document.contentType || '',
                        metadata,
                        links
                      };
                    }
                    """,
                    max(0, int(max_links)),
                )
                if not isinstance(extracted, dict):
                    extracted = {}
                text = str(extracted.get("text") or "").strip()
                text_truncated = len(text) > max_chars
                if text_truncated:
                    text = text[:max_chars].rstrip()
                raw_content = str(page.content() or "")
                raw_content_truncated = len(raw_content) > max_raw_chars
                if raw_content_truncated:
                    raw_content = raw_content[:max_raw_chars].rstrip()
                headers = _safe_response_headers(dict(getattr(response, "headers", {}) or {})) if response is not None else {}
                links = tuple(
                    WebFetchLink(
                        href=str(item.get("href") or ""),
                        text=str(item.get("text") or ""),
                        rel=str(item.get("rel") or ""),
                    )
                    for item in _dedupe_links(
                        [item for item in list(extracted.get("links") or []) if isinstance(item, dict)]
                    )[: max(0, int(max_links))]
                )
                status_code = int(response.status) if response is not None else None
                return WebFetchDocument(
                    requested_url=str(url),
                    final_url=final_url,
                    title=title,
                    text=text,
                    raw_content=raw_content,
                    raw_content_truncated=raw_content_truncated,
                    status_code=status_code,
                    content_type=str(extracted.get("content_type") or headers.get("content-type") or ""),
                    content_length=None,
                    text_truncated=text_truncated,
                    links=links,
                    metadata={str(key): str(value) for key, value in dict(extracted.get("metadata") or {}).items()},
                    response_headers=headers,
                )
            finally:
                context.close()
                browser.close()

    def _screenshot_playwright(
        self,
        url: str,
        *,
        timeout_ms: int,
        full_page: bool,
        viewport_width: int,
        viewport_height: int,
        user_agent: str,
    ) -> dict[str, Any]:
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
            context = browser.new_context(
                user_agent=str(user_agent or DEFAULT_WEB_FETCH_USER_AGENT),
                viewport={
                    "width": max(320, int(viewport_width)),
                    "height": max(320, int(viewport_height)),
                },
            )
            page = context.new_page()
            try:
                response = page.goto(str(url), wait_until="domcontentloaded", timeout=max(1000, int(timeout_ms)))
                try:
                    page.wait_for_load_state("networkidle", timeout=min(max(1000, int(timeout_ms)), 5000))
                except Exception:
                    pass
                png = page.screenshot(type="png", full_page=bool(full_page))
                return {
                    "requested_url": str(url),
                    "final_url": str(page.url or url),
                    "title": str(page.title() or "").strip(),
                    "status_code": int(response.status) if response is not None else None,
                    "png_base64": base64.b64encode(png).decode("ascii"),
                    "full_page": bool(full_page),
                    "viewport_width": max(320, int(viewport_width)),
                    "viewport_height": max(320, int(viewport_height)),
                }
            finally:
                context.close()
                browser.close()

    def _inspect_layout_playwright(
        self,
        url: str,
        *,
        selector: str,
        timeout_ms: int,
        viewport_width: int,
        viewport_height: int,
        max_elements: int,
        user_agent: str,
    ) -> dict[str, Any]:
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover - env dependent
            raise RuntimeError(f"playwright unavailable: {exc}") from exc

        width = min(4096, max(320, int(viewport_width)))
        height = min(4096, max(320, int(viewport_height)))
        limit = min(20, max(1, int(max_elements)))
        with sync_playwright() as playwright:
            launch_opts: dict[str, Any] = {"headless": True}
            proxy_url = os.environ.get("https_proxy") or os.environ.get("http_proxy") or ""
            if proxy_url:
                launch_opts["proxy"] = {"server": proxy_url}
            browser = playwright.chromium.launch(**launch_opts)
            context = browser.new_context(
                user_agent=str(user_agent or DEFAULT_WEB_FETCH_USER_AGENT),
                viewport={"width": width, "height": height},
            )
            page = context.new_page()
            try:
                response = page.goto(str(url), wait_until="domcontentloaded", timeout=max(1000, int(timeout_ms)))
                try:
                    page.wait_for_load_state("networkidle", timeout=min(max(1000, int(timeout_ms)), 5000))
                except Exception:
                    pass
                inspected = page.evaluate(
                    """
                    (args) => {
                      const round = (value) => Math.round(Number(value) * 1000) / 1000;
                      const rectPayload = (rect) => ({
                        x: round(rect.x),
                        y: round(rect.y),
                        top: round(rect.top),
                        right: round(rect.right),
                        bottom: round(rect.bottom),
                        left: round(rect.left),
                        width: round(rect.width),
                        height: round(rect.height)
                      });
                      const all = document.querySelectorAll(args.selector);
                      const selected = [];
                      for (let index = 0; index < Math.min(all.length, args.limit); index += 1) {
                        selected.push(all[index]);
                      }
                      const elements = selected.map((node, index) => {
                        const rect = node.getBoundingClientRect();
                        const computed = window.getComputedStyle(node);
                        const styles = {};
                        for (const property of args.properties) {
                          styles[property] = String(computed.getPropertyValue(property) || '').slice(0, 240);
                        }
                        const previous = node.previousElementSibling;
                        const next = node.nextElementSibling;
                        const parent = node.parentElement;
                        const previousRect = previous ? previous.getBoundingClientRect() : null;
                        const nextRect = next ? next.getBoundingClientRect() : null;
                        const parentRect = parent ? parent.getBoundingClientRect() : null;
                        return {
                          index,
                          tag: String(node.tagName || '').toLowerCase(),
                          id: String(node.id || '').slice(0, 160),
                          classes: String(node.getAttribute('class') || '').split(/\\s+/).filter(Boolean).slice(0, 20).map((value) => value.slice(0, 80)),
                          role: String(node.getAttribute('role') || '').slice(0, 80),
                          text: String(node.innerText || node.textContent || '').replace(/\\s+/g, ' ').trim().slice(0, 240),
                          geometry: rectPayload(rect),
                          parent_geometry: parentRect ? rectPayload(parentRect) : null,
                          previous_sibling_vertical_gap_px: previousRect ? round(rect.top - previousRect.bottom) : null,
                          next_sibling_vertical_gap_px: nextRect ? round(nextRect.top - rect.bottom) : null,
                          computed_styles: styles
                        };
                      });
                      return {
                        selector: args.selector,
                        matched_count: all.length,
                        truncated: all.length > args.limit,
                        elements
                      };
                    }
                    """,
                    {
                        "selector": str(selector),
                        "limit": limit,
                        "properties": list(_LAYOUT_STYLE_PROPERTIES),
                    },
                )
                if not isinstance(inspected, dict):
                    inspected = {}
                return {
                    "requested_url": str(url),
                    "final_url": str(page.url or url),
                    "title": str(page.title() or "").strip()[:500],
                    "status_code": int(response.status) if response is not None else None,
                    "selector": str(selector),
                    "matched_count": int(inspected.get("matched_count") or 0),
                    "truncated": bool(inspected.get("truncated")),
                    "elements": [item for item in list(inspected.get("elements") or []) if isinstance(item, dict)],
                    "viewport_width": width,
                    "viewport_height": height,
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
            if self.path not in {"/fetch", "/inspect", "/screenshot"}:
                _json_response(self, 404, {"ok": False, "error": "not_found"})
                return
            url = str(payload.get("url") or "").strip()
            if not url:
                _json_response(self, 400, {"ok": False, "error": "url is required"})
                return
            if self.path == "/screenshot":
                try:
                    result = worker.screenshot(
                        url,
                        timeout_ms=max(1000, int(payload.get("timeout_ms") or 15000)),
                        full_page=bool(payload.get("full_page", False)),
                        viewport_width=max(320, int(payload.get("viewport_width") or 1280)),
                        viewport_height=max(320, int(payload.get("viewport_height") or 900)),
                        user_agent=str(payload.get("user_agent") or DEFAULT_WEB_FETCH_USER_AGENT),
                    )
                except Exception as exc:
                    _json_response(self, 500, {"ok": False, "error": str(exc)})
                    return
                _json_response(self, 200, {"ok": True, "result": result})
                return
            if self.path == "/inspect":
                selector = str(payload.get("selector") or "").strip()
                if not selector:
                    _json_response(self, 400, {"ok": False, "error": "selector is required"})
                    return
                if len(selector) > 500:
                    _json_response(self, 400, {"ok": False, "error": "selector exceeds 500 characters"})
                    return
                try:
                    result = worker.inspect_layout(
                        url,
                        selector=selector,
                        timeout_ms=min(120000, max(1000, int(payload.get("timeout_ms") or 15000))),
                        viewport_width=min(4096, max(320, int(payload.get("viewport_width") or 1280))),
                        viewport_height=min(4096, max(320, int(payload.get("viewport_height") or 900))),
                        max_elements=min(20, max(1, int(payload.get("max_elements") or 20))),
                        user_agent=str(payload.get("user_agent") or DEFAULT_WEB_FETCH_USER_AGENT),
                    )
                except Exception as exc:
                    _json_response(self, 500, {"ok": False, "error": str(exc)})
                    return
                _json_response(self, 200, {"ok": True, "result": result})
                return
            try:
                result = worker.fetch(
                    url,
                    timeout_ms=max(1000, int(payload.get("timeout_ms") or 15000)),
                    max_chars=max(1000, int(payload.get("max_chars") or 12000)),
                    max_raw_chars=max(0, int(payload.get("max_raw_chars") or 50000)),
                    max_links=max(0, int(payload.get("max_links") or 80)),
                    user_agent=str(payload.get("user_agent") or DEFAULT_WEB_FETCH_USER_AGENT),
                )
            except Exception as exc:
                _json_response(self, 500, {"ok": False, "error": str(exc)})
                return
            _json_response(self, 200, {"ok": True, "result": result.to_dict()})

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

    def fetch(
        self,
        url: str,
        *,
        timeout_ms: int,
        max_chars: int,
        max_raw_chars: int = 50000,
        max_links: int = 80,
        user_agent: str = DEFAULT_WEB_FETCH_USER_AGENT,
        settings: dict[str, Any] | None = None,
    ) -> WebFetchDocument:
        self._ensure_started(settings=settings)
        payload = self._request_json(
            "POST",
            "/fetch",
            {
                "url": url,
                "timeout_ms": timeout_ms,
                "max_chars": max_chars,
                "max_raw_chars": max_raw_chars,
                "max_links": max_links,
                "user_agent": str(user_agent or DEFAULT_WEB_FETCH_USER_AGENT),
            },
            timeout_seconds=max(timeout_ms, 1000) / 1000.0 + 5.0,
        )
        if not bool(payload.get("ok")):
            raise RuntimeError(str(payload.get("error") or "browser fetch failed"))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("browser fetch returned invalid payload")
        self.last_error = ""
        return WebFetchDocument.from_mapping(result, requested_url=url)

    def screenshot(
        self,
        url: str,
        *,
        timeout_ms: int,
        full_page: bool,
        viewport_width: int,
        viewport_height: int,
        user_agent: str = DEFAULT_WEB_FETCH_USER_AGENT,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_started(settings=settings)
        payload = self._request_json(
            "POST",
            "/screenshot",
            {
                "url": url,
                "timeout_ms": timeout_ms,
                "full_page": bool(full_page),
                "viewport_width": max(320, int(viewport_width)),
                "viewport_height": max(320, int(viewport_height)),
                "user_agent": str(user_agent or DEFAULT_WEB_FETCH_USER_AGENT),
            },
            timeout_seconds=max(timeout_ms, 1000) / 1000.0 + 5.0,
        )
        if not bool(payload.get("ok")):
            raise RuntimeError(str(payload.get("error") or "browser screenshot failed"))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("browser screenshot returned invalid payload")
        self.last_error = ""
        return result

    def inspect_layout(
        self,
        url: str,
        *,
        selector: str,
        timeout_ms: int,
        viewport_width: int,
        viewport_height: int,
        max_elements: int,
        user_agent: str = DEFAULT_WEB_FETCH_USER_AGENT,
        settings: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._ensure_started(settings=settings)
        payload = self._request_json(
            "POST",
            "/inspect",
            {
                "url": url,
                "selector": str(selector),
                "timeout_ms": min(120000, max(1000, int(timeout_ms))),
                "viewport_width": min(4096, max(320, int(viewport_width))),
                "viewport_height": min(4096, max(320, int(viewport_height))),
                "max_elements": min(20, max(1, int(max_elements))),
                "user_agent": str(user_agent or DEFAULT_WEB_FETCH_USER_AGENT),
            },
            timeout_seconds=max(timeout_ms, 1000) / 1000.0 + 5.0,
        )
        if not bool(payload.get("ok")):
            raise RuntimeError(str(payload.get("error") or "browser layout inspection failed"))
        result = payload.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("browser layout inspection returned invalid payload")
        self.last_error = ""
        return result

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
