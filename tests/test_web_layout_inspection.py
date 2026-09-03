from __future__ import annotations

import threading
from subprocess import CompletedProcess
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pal.shared import IntrospectionCall, RuntimeStatus
from pal.shared.tool_routing import TOOL_ROUTING_SYSTEM_GUIDANCE
from pal.web_fetch import (
    BrowserServiceManager,
    WebFetchIntrospectionProvider,
    WebFetchService,
    WebLayoutInspectionRequest,
    WebLayoutInspectionResult,
)
from pal.web_fetch import browser_service


class _Settings:
    def __init__(self, active: str = "") -> None:
        self.active = active

    def get(self, _key: str):
        return self.active

    def set(self, _key: str, value: str) -> None:
        self.active = value


class _Repository:
    def __init__(self, records) -> None:
        self.records = list(records)

    def list_enabled(self):
        return list(self.records)


class _TextOnlyProvider:
    provider_kind = "plain_http_fetch"


class _LayoutProvider:
    provider_kind = "playwright_fetch"

    def __init__(self) -> None:
        self.requests = []

    def inspect_layout(self, record, request):
        self.requests.append((record, request))
        return {
            "requested_url": request.url,
            "final_url": "https://example.com/final",
            "title": "Example",
            "status_code": 200,
            "selector": request.selector,
            "matched_count": 3,
            "truncated": True,
            "viewport_width": request.viewport_width,
            "viewport_height": request.viewport_height,
            "elements": [
                {
                    "index": 0,
                    "tag": "li",
                    "geometry": {"height": 20.0},
                    "next_sibling_vertical_gap_px": 2.0,
                    "computed_styles": {"white-space": "pre-wrap", "margin-top": "0px"},
                }
            ],
        }


def test_browser_missing_executable_self_heals_once_and_retries_launch() -> None:
    worker = browser_service._BrowserFetchWorker(max_concurrency=1)
    chromium = Mock()
    browser = object()
    chromium.launch.side_effect = [
        RuntimeError("Executable doesn't exist; run playwright install"),
        browser,
    ]

    with patch.object(browser_service, "self_heal_install_browsers", return_value=True) as install:
        launched = worker._launch_chromium(SimpleNamespace(chromium=chromium), {})

    assert launched is browser
    assert chromium.launch.call_count == 2
    install.assert_called_once_with(reason="chromium build missing")
    assert worker.last_launch_error == ""


def test_browser_self_heal_install_is_single_flight_per_process() -> None:
    original = dict(browser_service._BROWSER_SELF_HEAL_STATE)
    browser_service._BROWSER_SELF_HEAL_STATE.update(
        attempted=False,
        install_ok=False,
        last_result="",
    )
    try:
        completed = CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        with patch.object(browser_service.subprocess, "run", return_value=completed) as run:
            assert browser_service.self_heal_install_browsers(reason="test")
            assert browser_service.self_heal_install_browsers(reason="test again")
        run.assert_called_once()
        assert browser_service.browser_self_heal_state()["last_result"] == "ok"
    finally:
        browser_service._BROWSER_SELF_HEAL_STATE.clear()
        browser_service._BROWSER_SELF_HEAL_STATE.update(original)


def test_browser_manager_health_propagates_launch_failure(tmp_path) -> None:
    manager = BrowserServiceManager(runtime_root=tmp_path)
    manager._process = object()  # type: ignore[assignment]
    manager._process_running = lambda _resource=None: True  # type: ignore[method-assign]
    manager._request_json = lambda *_args, **_kwargs: {  # type: ignore[method-assign]
        "ok": True,
        "healthy": False,
        "reason": "last_launch_failed",
        "last_launch_error": "missing chromium",
        "self_heal": {"attempted": True, "last_result": "failed"},
    }

    health = manager.health()

    assert health["healthy"] is False
    assert health["reason"] == "last_launch_failed"
    assert health["last_launch_error"] == "missing chromium"
    assert health["self_heal"]["attempted"] is True


def test_browser_manager_sends_bounded_layout_request(tmp_path) -> None:
    manager = BrowserServiceManager(runtime_root=tmp_path)
    calls = []
    manager._ensure_started = lambda **_kwargs: None  # type: ignore[method-assign]

    def request_json(method, path, payload, *, timeout_seconds, resource=None):
        _ = resource
        calls.append((method, path, payload, timeout_seconds))
        return {
            "ok": True,
            "result": {
                "selector": payload["selector"],
                "matched_count": 30,
                "truncated": True,
                "elements": [],
            },
        }

    manager._request_json = request_json  # type: ignore[method-assign]

    result = manager.inspect_layout(
        "https://example.com",
        selector=".bubble li",
        timeout_ms=4000,
        viewport_width=9000,
        viewport_height=7000,
        max_elements=99,
    )

    assert result["matched_count"] == 30
    assert calls[0][0:2] == ("POST", "/inspect")
    assert calls[0][2]["max_elements"] == 20
    assert calls[0][2]["viewport_width"] == 4096
    assert calls[0][2]["viewport_height"] == 4096
    assert calls[0][2]["selector"] == ".bubble li"


def test_browser_stop_serializes_generation_replacement(tmp_path) -> None:
    manager = BrowserServiceManager(runtime_root=tmp_path)

    class Process:
        pid = 900_100
        returncode = None

        def poll(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

        def wait(self, timeout=None):
            _ = timeout
            self.returncode = self.returncode if self.returncode is not None else 0
            return self.returncode

    old_resource = SimpleNamespace(process=Process(), host="127.0.0.1", port=1, token="old")
    new_resource = SimpleNamespace(process=Process(), host="127.0.0.1", port=2, token="new")
    manager._process = old_resource
    shutdown_started = threading.Event()
    release_shutdown = threading.Event()
    replacement_attempted = threading.Event()
    observed_resources = []

    manager._process_running = lambda resource=None: True  # type: ignore[method-assign]

    def request_json(
        _method,
        _path,
        _payload,
        *,
        timeout_seconds,
        resource=None,
    ):
        _ = timeout_seconds
        observed_resources.append(resource)
        shutdown_started.set()
        assert release_shutdown.wait(timeout=2.0)
        return {"ok": True}

    manager._request_json = request_json  # type: ignore[method-assign]
    stop_thread = threading.Thread(target=manager.stop_sync)

    def replace_generation() -> None:
        replacement_attempted.set()
        with manager._start_lock:
            manager._process = new_resource

    replace_thread = threading.Thread(target=replace_generation)
    stop_thread.start()
    assert shutdown_started.wait(timeout=1.0)
    replace_thread.start()
    assert replacement_attempted.wait(timeout=1.0)
    assert manager._process is old_resource

    release_shutdown.set()
    stop_thread.join(timeout=2.0)
    replace_thread.join(timeout=2.0)

    assert not stop_thread.is_alive()
    assert not replace_thread.is_alive()
    assert observed_resources == [old_resource]
    assert manager._process is new_resource


def test_service_skips_text_provider_and_reports_browser_fallback(tmp_path) -> None:
    plain = SimpleNamespace(
        provider_id="plain",
        provider_kind="plain_http_fetch",
        enabled=True,
        settings_blob={},
    )
    browser = SimpleNamespace(
        provider_id="browser",
        provider_kind="playwright_fetch",
        enabled=True,
        settings_blob={},
    )
    layout_provider = _LayoutProvider()
    service = WebFetchService(
        repository=_Repository([plain, browser]),  # type: ignore[arg-type]
        settings_repository=_Settings("plain"),  # type: ignore[arg-type]
        browser_manager=BrowserServiceManager(runtime_root=tmp_path),
        providers={
            "plain_http_fetch": _TextOnlyProvider(),  # type: ignore[dict-item]
            "playwright_fetch": layout_provider,  # type: ignore[dict-item]
        },
    )

    result = service.inspect_layout(
        WebLayoutInspectionRequest(
            url="https://example.com",
            selector=".bubble li",
            viewport_width=1000,
            viewport_height=700,
            max_elements=1,
        )
    )

    assert result.configured_provider_id == "plain"
    assert result.effective_provider_id == "browser"
    assert result.fallback_used is True
    assert result.matched_count == 3
    assert result.elements[0]["computed_styles"]["white-space"] == "pre-wrap"
    assert layout_provider.requests[0][1].selector == ".bubble li"


class _CapabilityService:
    def inspect_layout(self, request):
        return WebLayoutInspectionResult(
            requested_url=request.url,
            final_url=request.url,
            title="Example",
            status_code=200,
            configured_provider_id="browser",
            effective_provider_id="browser",
            selector=request.selector,
            matched_count=1,
            truncated=False,
            elements=(
                {
                    "index": 0,
                    "tag": "li",
                    "geometry": {"height": 20.0},
                    "computed_styles": {"line-height": "20px"},
                },
            ),
            viewport_width=request.viewport_width,
            viewport_height=request.viewport_height,
        )


def test_layout_capability_returns_text_consumable_visual_evidence() -> None:
    provider = WebFetchIntrospectionProvider(service=_CapabilityService())  # type: ignore[arg-type]

    result = provider.inspect_layout(
        IntrospectionCall(
            name="inspect_web_layout",
            args={"url": "https://example.com", "selector": ".bubble li", "max_elements": 4},
        )
    )

    assert result.status == RuntimeStatus.OK
    assert result.structured["returned_count"] == 1
    assert result.structured["elements"][0]["geometry"]["height"] == 20.0
    assert "computed_styles" in result.llm_text


def test_ui_layout_verification_is_stable_system_guidance() -> None:
    assert "normalized page text/HTML" in TOOL_ROUTING_SYSTEM_GUIDANCE
    assert "computed styles" in TOOL_ROUTING_SYSTEM_GUIDANCE
    assert "rendered-layout inspection capability" in TOOL_ROUTING_SYSTEM_GUIDANCE
