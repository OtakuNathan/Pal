from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pal.web_fetch.models import WebFetchProviderModel


ACTIVE_WEB_FETCH_PROVIDER_SETTING_KEY = "active_web_fetch_provider_id"


@dataclass(frozen=True)
class WebFetchRequest:
    url: str
    timeout_ms: int = 15000
    max_chars: int = 12000


@dataclass(frozen=True)
class WebFetchResult:
    requested_url: str
    final_url: str
    title: str
    text: str
    configured_provider_id: str | None
    effective_provider_id: str | None
    fetch_mode: str
    fallback_used: bool


class WebFetchProviderPort(Protocol):
    provider_kind: str

    def read(self, record: WebFetchProviderModel, request: WebFetchRequest) -> dict[str, str]:
        ...
