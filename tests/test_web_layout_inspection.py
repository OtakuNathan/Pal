from __future__ import annotations

from types import SimpleNamespace

from pal.shared import IntrospectionCall, RuntimeStatus
from pal.shared.tool_routing import TOOL_ROUTING_SYSTEM_GUIDANCE
from pal.web_fetch import BrowserServiceManager, WebFetchIntrospectionProvider
from pal.web_fetch.browser_service import _layout_script


def test_browser_manager_sends_action_envelope(tmp_path) -> None:
    manager = BrowserServiceManager(runtime_root=tmp_path)
    resource = SimpleNamespace(process=SimpleNamespace(poll=lambda: None), host="127.0.0.1", port=1, token="x")
    calls = []
    manager._ensure_started = lambda: resource  # type: ignore[method-assign]

    def request_json(method, path, payload, *, timeout_seconds, resource):
        calls.append((method, path, payload, timeout_seconds, resource))
        return {"ok": True, "result": {"inspection": {"matched_count": 1}}}

    manager._request_json = request_json  # type: ignore[method-assign]
    result = manager.execute(
        session_key="a" * 64,
        action="inspect_layout",
        args={"selector": ".bubble li", "max_elements": 20},
        persistent=True,
        timeout_ms=4000,
    )

    assert result["inspection"]["matched_count"] == 1
    assert calls[0][0:2] == ("POST", "/action")
    assert calls[0][2]["action"] == "inspect_layout"
    assert calls[0][2]["args"]["selector"] == ".bubble li"
    assert calls[0][2]["session_key"] == "a" * 64


class _CapabilityService:
    def __init__(self) -> None:
        self.calls = []

    def execute(self, **kwargs):
        self.calls.append(dict(kwargs))
        return {
            "inspection": {
                "selector": kwargs["args"]["selector"],
                "matched_count": 1,
                "elements": [{"geometry": {"height": 20.0}, "computed_styles": {"line-height": "20px"}}],
            },
            "page": {"url": "https://example.com", "title": "Example"},
        }


def test_layout_capability_returns_text_consumable_visual_evidence() -> None:
    service = _CapabilityService()
    provider = WebFetchIntrospectionProvider(service=service)  # type: ignore[arg-type]

    result = provider.inspect_layout(
        IntrospectionCall(
            name="browser_inspect_layout",
            args={"selector": ".bubble li", "max_elements": 4},
            meta={"turn_id": "layout-turn"},
        )
    )

    assert result.status == RuntimeStatus.OK
    assert result.structured["inspection"]["matched_count"] == 1
    assert result.structured["inspection"]["elements"][0]["geometry"]["height"] == 20.0
    assert "computed_styles" in result.llm_text
    assert service.calls[0]["action"] == "inspect_layout"
    assert len(service.calls[0]["session_key"]) == 64


def test_layout_script_json_encodes_untrusted_selector() -> None:
    script = _layout_script(selector='div"; throw new Error("boom") //', limit=3)

    assert 'div\\"; throw new Error(\\"boom\\") //' in script
    assert 'const args = {"selector":' in script


def test_ui_layout_verification_is_stable_system_guidance() -> None:
    assert "normalized page text/HTML" in TOOL_ROUTING_SYSTEM_GUIDANCE
    assert "computed styles" in TOOL_ROUTING_SYSTEM_GUIDANCE
    assert "rendered-layout inspection capability" in TOOL_ROUTING_SYSTEM_GUIDANCE
