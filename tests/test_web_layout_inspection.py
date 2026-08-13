from __future__ import annotations

from types import SimpleNamespace

from pal.shared import IntrospectionCall, RuntimeStatus
from pal.shared.tool_routing import TOOL_ROUTING_SYSTEM_GUIDANCE
from pal.web_fetch import (
    BrowserServiceManager,
    WebFetchIntrospectionProvider,
    WebFetchService,
    WebLayoutInspectionRequest,
    WebLayoutInspectionResult,
)


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


def test_browser_manager_sends_bounded_layout_request(tmp_path) -> None:
    manager = BrowserServiceManager(runtime_root=tmp_path)
    calls = []
    manager._ensure_started = lambda **_kwargs: None  # type: ignore[method-assign]

    def request_json(method, path, payload, *, timeout_seconds):
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
