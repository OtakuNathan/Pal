from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.foundation import ArtifactIngestor
from pal.shared import RuntimeStatus
from pal.shared.result_rendering import render_titled_structured_for_llm
from pal.web_fetch.contracts import DEFAULT_WEB_FETCH_USER_AGENT, WebScreenshotRequest
from pal.web_fetch.service import WebFetchService


@dataclass
class WebScreenshotTool:
    service: WebFetchService
    artifact_manager: Any | None = None
    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        _ = args
        return CapabilityResult(
            status=RuntimeStatus.INVALID,
            text="web_screenshot requires async turn context",
            structured={"reason": "async_required"},
            llm_text="web_screenshot requires async turn context.",
        )

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        url = str(args.get("url") or "").strip()
        if not url:
            return _result(RuntimeStatus.INVALID, "Web screenshot failed", {"reason": "url_required"}, text="url is required")
        try:
            request = WebScreenshotRequest(
                url=url,
                timeout_ms=max(1000, int(args.get("timeout_ms") or 15000)),
                full_page=bool(args.get("full_page", False)),
                viewport_width=max(320, int(args.get("viewport_width") or 1280)),
                viewport_height=max(320, int(args.get("viewport_height") or 900)),
                user_agent=str(args.get("user_agent") or DEFAULT_WEB_FETCH_USER_AGENT),
            )
        except (TypeError, ValueError):
            return _result(RuntimeStatus.INVALID, "Web screenshot failed", {"reason": "invalid_numeric_argument"})

        try:
            screenshot = self.service.screenshot(request)
            runtime = kwargs.get("runtime")
            turn_id = str(kwargs.get("turn_id") or "").strip() or "manual"
            runtime_root = _runtime_root(runtime, self.service)
            file_name = _screenshot_file_name(screenshot.final_url or url)
            stored = ArtifactIngestor(runtime_root).store_bytes(
                channel_kind="web_fetch",
                bucket_id=turn_id,
                file_name=file_name,
                content=screenshot.png_bytes,
                mime_type="image/png",
            )
            payload: dict[str, Any] = {
                "artifact_id": stored.artifact_id,
                "stored_artifact_id": stored.artifact_id,
                "local_cached_path": stored.local_cached_path,
                "mime_type": stored.mime_type or "image/png",
                "size_bytes": stored.size_bytes,
                "sha256": stored.sha256,
                "requested_url": screenshot.requested_url,
                "final_url": screenshot.final_url,
                "title": screenshot.title,
                "status_code": screenshot.status_code,
                "configured_provider_id": screenshot.configured_provider_id,
                "effective_provider_id": screenshot.effective_provider_id,
                "fallback_used": screenshot.fallback_used,
                "full_page": screenshot.full_page,
                "viewport_width": screenshot.viewport_width,
                "viewport_height": screenshot.viewport_height,
                "registered_artifact": False,
            }
            self._register_conversation_artifact(payload, stored, runtime=runtime, turn_id=turn_id, source_url=url)
            return _result(RuntimeStatus.OK, "Web screenshot saved", payload, text="web screenshot saved")
        except Exception as exc:
            return _result(
                RuntimeStatus.ERROR,
                "Web screenshot failed",
                {"url": url, "error": str(exc), "error_type": exc.__class__.__name__},
                text="web screenshot failed",
            )

    def _register_conversation_artifact(
        self,
        payload: dict[str, Any],
        stored: Any,
        *,
        runtime: Any,
        turn_id: str,
        source_url: str,
    ) -> None:
        manager = self.artifact_manager or _artifact_manager_from_runtime(runtime)
        register_ingested = getattr(manager, "register_ingested", None)
        if not callable(register_ingested):
            return
        try:
            scope_key = _scope_from_runtime(runtime, turn_id)
            ref = register_ingested(
                {
                    "local_cached_path": stored.local_cached_path,
                    "file_name": Path(stored.local_cached_path).name,
                    "mime_type": stored.mime_type or "image/png",
                },
                scope_key=scope_key,
                turn_id=turn_id,
                source_channel="web_fetch",
                metadata={
                    "source_url": source_url,
                    "final_url": payload.get("final_url"),
                    "capture_kind": "op_web_screenshot",
                    "stored_artifact_id": stored.artifact_id,
                },
            )
            ref_payload = ref.to_dict() if hasattr(ref, "to_dict") else dict(ref)
            payload["artifact"] = ref_payload
            payload["artifact_id"] = ref_payload.get("artifact_id") or payload["artifact_id"]
            payload["registered_artifact"] = True
            info = getattr(manager, "info", lambda *_args, **_kwargs: {})(payload["artifact_id"], scope_key)
            local_file = dict(dict(info.get("artifact") or {}).get("metadata", {}).get("local_file") or {})
            preferred = str(local_file.get("preferred_path") or "").strip()
            if preferred:
                payload["local_cached_path"] = preferred
        except Exception as exc:
            payload["artifact_registration_error"] = f"{exc.__class__.__name__}: {exc}"


def _runtime_root(runtime: Any, service: WebFetchService) -> Path:
    root = getattr(runtime, "runtime_root", None)
    if root is not None:
        return Path(root)
    return Path(service.browser_manager.runtime_root)


def _artifact_manager_from_runtime(runtime: Any) -> Any | None:
    registry = getattr(runtime, "provider_registry", {}) if runtime is not None else {}
    if isinstance(registry, dict):
        return registry.get("artifact:artifact")
    return None


def _scope_from_runtime(runtime: Any, turn_id: str) -> str:
    registry = getattr(runtime, "provider_registry", {}) if runtime is not None else {}
    turn_io = registry.get("core:turn_io") if isinstance(registry, dict) else None
    scope_for_turn = getattr(turn_io, "artifact_scope_for_turn", None)
    if callable(scope_for_turn):
        scope = scope_for_turn(turn_id)
        if scope:
            return str(scope)
    raise KeyError("artifact_scope_unavailable")


def _screenshot_file_name(url: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(url or "web_page")).strip("._")
    return f"{(cleaned or 'web_page')[:80]}.png"


def _result(status: str, title: str, structured: dict[str, Any], text: str = "") -> CapabilityResult:
    return CapabilityResult(
        status=status,
        text=text or title,
        structured=structured,
        llm_text=render_titled_structured_for_llm(title, structured),
    )
