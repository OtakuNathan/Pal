from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from pal.web_search.models import WebSearchProviderModel


ACTIVE_WEB_SEARCH_PROVIDER_SETTING_KEY = "active_web_search_provider_id"


@dataclass(frozen=True)
class WebSearchQuery:
    query: str
    limit: int = 5
    region: str = ""
    safe_search: str = ""


@dataclass(frozen=True)
class WebSearchItem:
    title: str
    url: str
    snippet: str
    provider_id: str
    provider_kind: str
    rank: int


@dataclass(frozen=True)
class WebSearchQueryResult:
    items: list[WebSearchItem] = field(default_factory=list)
    configured_provider_id: str | None = None
    effective_provider_id: str | None = None
    fallback_used: bool = False


class WebSearchProviderPort(Protocol):
    provider_kind: str

    def search(self, record: WebSearchProviderModel, query: WebSearchQuery) -> list[WebSearchItem]:
        ...
