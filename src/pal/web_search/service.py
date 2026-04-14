from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote_plus
from urllib.request import Request, urlopen

from pal.llm.repository import RuntimeSettingRepository
from pal.web_search.contracts import (
    ACTIVE_WEB_SEARCH_PROVIDER_SETTING_KEY,
    WebSearchItem,
    WebSearchProviderPort,
    WebSearchQuery,
    WebSearchQueryResult,
)
from pal.web_search.models import WebSearchProviderModel
from pal.web_search.repository import WebSearchProviderRepository


def _http_json(url: str, *, headers: dict[str, str] | None = None, timeout_seconds: float = 20.0) -> dict[str, Any]:
    request = Request(url, headers={**{"User-Agent": "PalV2/0.1"}, **dict(headers or {})})
    with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    if not isinstance(payload, dict):
        raise RuntimeError("search provider returned invalid JSON")
    return payload


@dataclass
class BraveSearchProvider(WebSearchProviderPort):
    provider_kind: str = "brave_search"
    base_url: str = "https://api.search.brave.com/res/v1/web/search"

    def search(self, record: WebSearchProviderModel, query: WebSearchQuery) -> list[WebSearchItem]:
        api_key = str((record.auth_material_blob or {}).get("api_key") or "").strip()
        if not api_key:
            raise RuntimeError("brave api key missing")
        url = f"{self.base_url}?q={quote_plus(query.query)}&count={max(1, min(int(query.limit), 10))}"
        if query.region:
            url = f"{url}&country={quote_plus(query.region)}"
        if query.safe_search:
            url = f"{url}&safesearch={quote_plus(query.safe_search)}"
        payload = _http_json(
            url,
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": api_key,
            },
        )
        results = payload.get("web", {}).get("results", [])
        if not isinstance(results, list):
            raise RuntimeError("brave search returned invalid results")
        items: list[WebSearchItem] = []
        for index, item in enumerate(results[: max(1, min(int(query.limit), 10))], start=1):
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url_value = str(item.get("url") or "").strip()
            snippet = str(item.get("description") or item.get("snippet") or "").strip()
            if not title or not url_value:
                continue
            items.append(
                WebSearchItem(
                    title=title,
                    url=url_value,
                    snippet=snippet,
                    provider_id=record.provider_id,
                    provider_kind=record.provider_kind,
                    rank=index,
                )
            )
        return items


@dataclass
class DuckDuckGoSearchProvider(WebSearchProviderPort):
    provider_kind: str = "duckduckgo_search"
    base_url: str = "https://api.duckduckgo.com/"

    def search(self, record: WebSearchProviderModel, query: WebSearchQuery) -> list[WebSearchItem]:
        payload = _http_json(
            f"{self.base_url}?q={quote_plus(query.query)}&format=json&no_html=1&skip_disambig=1",
        )
        items: list[WebSearchItem] = []
        heading = str(payload.get("Heading") or query.query).strip() or query.query
        abstract_text = str(payload.get("AbstractText") or "").strip()
        abstract_url = str(payload.get("AbstractURL") or "").strip()
        if abstract_text:
            items.append(
                WebSearchItem(
                    title=heading,
                    url=abstract_url or f"https://duckduckgo.com/?q={quote_plus(query.query)}",
                    snippet=abstract_text,
                    provider_id=record.provider_id,
                    provider_kind=record.provider_kind,
                    rank=1,
                )
            )
        related = payload.get("RelatedTopics")
        if isinstance(related, list):
            for item in related:
                if len(items) >= max(1, min(int(query.limit), 5)):
                    break
                if not isinstance(item, dict):
                    continue
                nested_topics = item.get("Topics")
                if isinstance(nested_topics, list):
                    for nested in nested_topics:
                        if len(items) >= max(1, min(int(query.limit), 5)):
                            break
                        if not isinstance(nested, dict):
                            continue
                        text = str(nested.get("Text") or "").strip()
                        url_value = str(nested.get("FirstURL") or "").strip()
                        if text and url_value:
                            items.append(
                                WebSearchItem(
                                    title=text.split(" - ", 1)[0].strip() or heading,
                                    url=url_value,
                                    snippet=text,
                                    provider_id=record.provider_id,
                                    provider_kind=record.provider_kind,
                                    rank=len(items) + 1,
                                )
                            )
                    continue
                text = str(item.get("Text") or "").strip()
                url_value = str(item.get("FirstURL") or "").strip()
                if text and url_value:
                    items.append(
                        WebSearchItem(
                            title=text.split(" - ", 1)[0].strip() or heading,
                            url=url_value,
                            snippet=text,
                            provider_id=record.provider_id,
                            provider_kind=record.provider_kind,
                            rank=len(items) + 1,
                        )
                    )
        return items[: max(1, min(int(query.limit), 5))]


@dataclass
class WebSearchService:
    repository: WebSearchProviderRepository
    settings_repository: RuntimeSettingRepository
    providers: dict[str, WebSearchProviderPort] = field(default_factory=dict)
    last_errors: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.providers:
            self.providers = {
                "brave_search": BraveSearchProvider(),
                "duckduckgo_search": DuckDuckGoSearchProvider(),
            }

    def list_providers(self) -> list[WebSearchProviderModel]:
        return self.repository.list_all()

    def get_provider(self, provider_id: str) -> WebSearchProviderModel | None:
        return self.repository.get(provider_id)

    def configured_active_provider_id(self) -> str | None:
        stored = str(self.settings_repository.get(ACTIVE_WEB_SEARCH_PROVIDER_SETTING_KEY) or "").strip()
        if stored:
            return stored
        primary = self.repository.list_enabled()
        if primary:
            return primary[0].provider_id
        return None

    def effective_active_provider_id(self) -> str | None:
        candidates = self._provider_candidates(self.configured_active_provider_id())
        return candidates[0].provider_id if candidates else None

    def set_active_provider(self, provider_id: str) -> WebSearchProviderModel | None:
        record = self.repository.get(provider_id)
        if record is None:
            return None
        self.settings_repository.set(ACTIVE_WEB_SEARCH_PROVIDER_SETTING_KEY, provider_id)
        return record

    def set_enabled(self, provider_id: str, enabled: bool) -> WebSearchProviderModel | None:
        return self.repository.set_enabled(provider_id, enabled)

    def set_config(self, provider_id: str, patch: dict[str, object]) -> WebSearchProviderModel | None:
        return self.repository.merge_settings(provider_id, patch)

    def set_auth_material(self, provider_id: str, patch: dict[str, object]) -> WebSearchProviderModel | None:
        return self.repository.merge_auth_material(provider_id, patch)

    def provider_auth_state(self, record: WebSearchProviderModel) -> dict[str, object]:
        auth_blob = dict(record.auth_material_blob or {})
        if record.provider_kind == "brave_search":
            return {
                "provider_id": record.provider_id,
                "provider_kind": record.provider_kind,
                "authorized": bool(str(auth_blob.get("api_key") or "").strip()),
                "api_key_present": bool(str(auth_blob.get("api_key") or "").strip()),
            }
        return {
            "provider_id": record.provider_id,
            "provider_kind": record.provider_kind,
            "authorized": True,
            "api_key_present": False,
        }

    def provider_health(self, record: WebSearchProviderModel) -> dict[str, object]:
        last_error = str(self.last_errors.get(record.provider_id) or "")
        auth_state = self.provider_auth_state(record)
        healthy = bool(record.enabled)
        reason = "idle"
        if not record.enabled:
            healthy = False
            reason = "provider_disabled"
        elif record.provider_kind == "brave_search" and not bool(auth_state.get("authorized")):
            healthy = False
            reason = "auth_missing"
        elif last_error:
            healthy = False
            reason = "last_request_failed"
        return {
            "provider_id": record.provider_id,
            "provider_kind": record.provider_kind,
            "healthy": healthy,
            "enabled": bool(record.enabled),
            "reason": reason,
            "last_error": last_error,
        }

    def query(self, request: WebSearchQuery) -> WebSearchQueryResult:
        configured_provider_id = self.configured_active_provider_id()
        candidates = self._provider_candidates(configured_provider_id)
        if not candidates:
            raise RuntimeError("no enabled web search provider available")
        last_error = "search failed"
        for index, record in enumerate(candidates):
            provider = self.providers.get(record.provider_kind)
            if provider is None:
                self.last_errors[record.provider_id] = "provider runtime unavailable"
                last_error = "provider runtime unavailable"
                continue
            try:
                items = provider.search(record, request)
            except Exception as exc:
                self.last_errors[record.provider_id] = str(exc)
                last_error = str(exc)
                continue
            self.last_errors[record.provider_id] = ""
            return WebSearchQueryResult(
                items=items,
                configured_provider_id=configured_provider_id,
                effective_provider_id=record.provider_id,
                fallback_used=index > 0,
            )
        raise RuntimeError(last_error)

    def _provider_candidates(self, preferred_provider_id: str | None) -> list[WebSearchProviderModel]:
        enabled = list(self.repository.list_enabled())
        if not enabled:
            return []
        ordered: list[WebSearchProviderModel] = []
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
