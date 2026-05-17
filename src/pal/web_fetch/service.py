from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any

from pal.llm.repository import RuntimeSettingRepository
from pal.web_fetch.browser_service import BrowserServiceManager, plain_http_fetch, playwright_python_available
from pal.web_fetch.contracts import (
    ACTIVE_WEB_FETCH_PROVIDER_SETTING_KEY,
    DEFAULT_WEB_FETCH_USER_AGENT,
    WebFetchDocument,
    WebFetchProviderPort,
    WebFetchRequest,
    WebFetchResult,
    WebScreenshotRequest,
    WebScreenshotResult,
)
from pal.web_fetch.models import WebFetchProviderModel
from pal.web_fetch.repository import WebFetchProviderRepository


@dataclass
class PlaywrightFetchProvider(WebFetchProviderPort):
    browser_manager: BrowserServiceManager
    provider_kind: str = "playwright_fetch"

    def read(self, record: WebFetchProviderModel, request: WebFetchRequest) -> WebFetchDocument | dict[str, object]:
        return self.browser_manager.fetch(
            request.url,
            timeout_ms=request.timeout_ms,
            max_chars=request.max_chars,
            max_raw_chars=request.max_raw_chars,
            max_links=request.max_links,
            user_agent=request.user_agent,
            settings=dict(record.settings_blob or {}),
        )

    def screenshot(self, record: WebFetchProviderModel, request: WebScreenshotRequest) -> dict[str, object]:
        return self.browser_manager.screenshot(
            request.url,
            timeout_ms=request.timeout_ms,
            full_page=request.full_page,
            viewport_width=request.viewport_width,
            viewport_height=request.viewport_height,
            user_agent=request.user_agent,
            settings=dict(record.settings_blob or {}),
        )


@dataclass
class PlainHTTPFetchProvider(WebFetchProviderPort):
    provider_kind: str = "plain_http_fetch"

    def read(self, record: WebFetchProviderModel, request: WebFetchRequest) -> WebFetchDocument | dict[str, object]:
        _ = record
        return plain_http_fetch(
            request.url,
            timeout_ms=request.timeout_ms,
            max_chars=request.max_chars,
            max_raw_chars=request.max_raw_chars,
            max_links=request.max_links,
            user_agent=request.user_agent,
        )


@dataclass
class WebFetchService:
    repository: WebFetchProviderRepository
    settings_repository: RuntimeSettingRepository
    browser_manager: BrowserServiceManager
    providers: dict[str, WebFetchProviderPort] = field(default_factory=dict)
    last_errors: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.providers:
            self.providers = {
                "playwright_fetch": PlaywrightFetchProvider(browser_manager=self.browser_manager),
                "plain_http_fetch": PlainHTTPFetchProvider(),
            }

    def list_providers(self) -> list[WebFetchProviderModel]:
        return self.repository.list_all()

    def get_provider(self, provider_id: str) -> WebFetchProviderModel | None:
        return self.repository.get(provider_id)

    def configured_active_provider_id(self) -> str | None:
        stored = str(self.settings_repository.get(ACTIVE_WEB_FETCH_PROVIDER_SETTING_KEY) or "").strip()
        if stored:
            return stored
        primary = self.repository.list_enabled()
        if primary:
            return primary[0].provider_id
        return None

    def effective_active_provider_id(self) -> str | None:
        candidates = self._provider_candidates(self.configured_active_provider_id())
        return candidates[0].provider_id if candidates else None

    def set_active_provider(self, provider_id: str) -> WebFetchProviderModel | None:
        record = self.repository.get(provider_id)
        if record is None:
            return None
        self.settings_repository.set(ACTIVE_WEB_FETCH_PROVIDER_SETTING_KEY, provider_id)
        return record

    def set_enabled(self, provider_id: str, enabled: bool) -> WebFetchProviderModel | None:
        record = self.repository.set_enabled(provider_id, enabled)
        if record is not None and record.provider_kind == "playwright_fetch" and not enabled:
            self.browser_manager.stop_sync()
        return record

    def set_config(self, provider_id: str, patch: dict[str, object]) -> WebFetchProviderModel | None:
        record = self.repository.merge_settings(provider_id, patch)
        if record is not None and record.provider_kind == "playwright_fetch":
            self.browser_manager.stop_sync()
        return record

    def set_auth_material(self, provider_id: str, patch: dict[str, object]) -> WebFetchProviderModel | None:
        return self.repository.merge_auth_material(provider_id, patch)

    def provider_auth_state(self, record: WebFetchProviderModel) -> dict[str, object]:
        return {
            "provider_id": record.provider_id,
            "provider_kind": record.provider_kind,
            "authorized": True,
            "auth_required": False,
        }

    def provider_health(self, record: WebFetchProviderModel) -> dict[str, object]:
        last_error = str(self.last_errors.get(record.provider_id) or "")
        if record.provider_kind == "playwright_fetch":
            payload = self.browser_manager.health(settings=dict(record.settings_blob or {}))
            payload.update(
                {
                    "provider_id": record.provider_id,
                    "provider_kind": record.provider_kind,
                    "enabled": bool(record.enabled),
                }
            )
            if not record.enabled:
                payload["healthy"] = False
                payload["reason"] = "provider_disabled"
            payload["last_error"] = last_error or str(payload.get("last_error") or "")
            return payload
        healthy = bool(record.enabled and not last_error)
        reason = "idle"
        if not record.enabled:
            healthy = False
            reason = "provider_disabled"
        elif last_error:
            healthy = False
            reason = "last_request_failed"
        return {
            "provider_id": record.provider_id,
            "provider_kind": record.provider_kind,
            "enabled": bool(record.enabled),
            "healthy": healthy,
            "reason": reason,
            "last_error": last_error,
        }

    def read(self, request: WebFetchRequest) -> WebFetchResult:
        configured_provider_id = self.configured_active_provider_id()
        candidates = self._provider_candidates(configured_provider_id)
        if not candidates:
            raise RuntimeError("no enabled web fetch provider available")
        last_error = "web fetch failed"
        for index, record in enumerate(candidates):
            provider = self.providers.get(record.provider_kind)
            if provider is None:
                self.last_errors[record.provider_id] = "provider runtime unavailable"
                last_error = "provider runtime unavailable"
                continue
            try:
                result = self._coerce_document(provider.read(record, request), request=request)
            except Exception as exc:
                self.last_errors[record.provider_id] = str(exc)
                last_error = str(exc)
                continue
            self.last_errors[record.provider_id] = ""
            return WebFetchResult(
                requested_url=result.requested_url or request.url,
                final_url=result.final_url or request.url,
                title=result.title,
                text=result.text,
                raw_content=result.raw_content,
                raw_content_truncated=result.raw_content_truncated,
                configured_provider_id=configured_provider_id,
                effective_provider_id=record.provider_id,
                fetch_mode="browser" if record.provider_kind == "playwright_fetch" else "http",
                fallback_used=index > 0,
                status_code=result.status_code,
                content_type=result.content_type,
                content_length=result.content_length,
                text_truncated=result.text_truncated,
                links=result.links,
                metadata=result.metadata,
                response_headers=result.response_headers,
                user_agent=request.user_agent or DEFAULT_WEB_FETCH_USER_AGENT,
            )
        raise RuntimeError(last_error)

    def screenshot(self, request: WebScreenshotRequest) -> WebScreenshotResult:
        configured_provider_id = self.configured_active_provider_id()
        candidates = self._provider_candidates(configured_provider_id)
        if not candidates:
            raise RuntimeError("no enabled web fetch provider available")
        last_error = "web screenshot failed"
        for index, record in enumerate(candidates):
            if record.provider_kind != "playwright_fetch":
                continue
            provider = self.providers.get(record.provider_kind)
            screenshot = getattr(provider, "screenshot", None)
            if not callable(screenshot):
                self.last_errors[record.provider_id] = "provider runtime cannot capture screenshots"
                last_error = "provider runtime cannot capture screenshots"
                continue
            try:
                payload = screenshot(record, request)
                result = self._coerce_screenshot(payload, request=request)
            except Exception as exc:
                self.last_errors[record.provider_id] = str(exc)
                last_error = str(exc)
                continue
            self.last_errors[record.provider_id] = ""
            return WebScreenshotResult(
                requested_url=result.requested_url or request.url,
                final_url=result.final_url or request.url,
                title=result.title,
                png_bytes=result.png_bytes,
                configured_provider_id=configured_provider_id,
                effective_provider_id=record.provider_id,
                fallback_used=index > 0,
                status_code=result.status_code,
                full_page=result.full_page,
                viewport_width=result.viewport_width,
                viewport_height=result.viewport_height,
                user_agent=request.user_agent or DEFAULT_WEB_FETCH_USER_AGENT,
            )
        raise RuntimeError(last_error)

    async def shutdown_async(self) -> None:
        await self.browser_manager.shutdown_async()

    def shutdown_sync(self) -> None:
        self.browser_manager.stop_sync()

    def _provider_candidates(self, preferred_provider_id: str | None) -> list[WebFetchProviderModel]:
        enabled = list(self.repository.list_enabled())
        if not enabled:
            return []
        ordered: list[WebFetchProviderModel] = []
        seen: set[str] = set()
        if preferred_provider_id:
            preferred = next((item for item in enabled if item.provider_id == preferred_provider_id), None)
            if preferred is not None:
                ordered.append(preferred)
                seen.add(preferred.provider_id)
        for item in enabled:
            if item.provider_id in seen:
                continue
            ordered.append(item)
        return ordered

    def _coerce_document(self, result: WebFetchDocument | dict[str, Any], *, request: WebFetchRequest) -> WebFetchDocument:
        if isinstance(result, WebFetchDocument):
            return result
        if not isinstance(result, dict):
            raise RuntimeError("web fetch provider returned invalid payload")
        return WebFetchDocument.from_mapping(result, requested_url=request.url)

    def _coerce_screenshot(self, result: WebScreenshotResult | dict[str, Any], *, request: WebScreenshotRequest) -> WebScreenshotResult:
        if isinstance(result, WebScreenshotResult):
            return result
        if not isinstance(result, dict):
            raise RuntimeError("web screenshot provider returned invalid payload")
        encoded = str(result.get("png_base64") or "")
        if not encoded:
            raise RuntimeError("web screenshot provider returned empty image")
        return WebScreenshotResult(
            requested_url=str(result.get("requested_url") or request.url),
            final_url=str(result.get("final_url") or request.url),
            title=str(result.get("title") or ""),
            png_bytes=base64.b64decode(encoded.encode("ascii")),
            status_code=int(result["status_code"]) if result.get("status_code") is not None else None,
            full_page=bool(result.get("full_page", request.full_page)),
            viewport_width=int(result.get("viewport_width") or request.viewport_width),
            viewport_height=int(result.get("viewport_height") or request.viewport_height),
            user_agent=request.user_agent or DEFAULT_WEB_FETCH_USER_AGENT,
        )
