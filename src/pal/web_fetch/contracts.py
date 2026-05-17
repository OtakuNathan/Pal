from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from pal.web_fetch.models import WebFetchProviderModel


ACTIVE_WEB_FETCH_PROVIDER_SETTING_KEY = "active_web_fetch_provider_id"
DEFAULT_WEB_FETCH_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class WebFetchLink:
    href: str
    text: str = ""
    rel: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "href": self.href,
            "text": self.text,
            "rel": self.rel,
        }


@dataclass(frozen=True)
class WebFetchDocument:
    requested_url: str
    final_url: str
    title: str = ""
    text: str = ""
    raw_content: str = ""
    raw_content_truncated: bool = False
    status_code: int | None = None
    content_type: str = ""
    content_length: int | None = None
    text_truncated: bool = False
    links: tuple[WebFetchLink, ...] = ()
    metadata: dict[str, str] | None = None
    response_headers: dict[str, str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "title": self.title,
            "text": self.text,
            "raw_content": self.raw_content,
            "raw_content_truncated": self.raw_content_truncated,
            "status_code": self.status_code,
            "content_type": self.content_type,
            "content_length": self.content_length,
            "text_truncated": self.text_truncated,
            "links": [link.to_dict() for link in self.links],
            "metadata": dict(self.metadata or {}),
            "response_headers": dict(self.response_headers or {}),
        }

    @classmethod
    def from_mapping(cls, payload: dict[str, Any], *, requested_url: str = "") -> "WebFetchDocument":
        links = tuple(_coerce_link(item) for item in list(payload.get("links") or []) if isinstance(item, dict))
        status_code = payload.get("status_code")
        content_length = payload.get("content_length")
        return cls(
            requested_url=str(payload.get("requested_url") or requested_url),
            final_url=str(payload.get("final_url") or payload.get("requested_url") or requested_url),
            title=str(payload.get("title") or ""),
            text=str(payload.get("text") or ""),
            raw_content=str(payload.get("raw_content") or ""),
            raw_content_truncated=bool(payload.get("raw_content_truncated")),
            status_code=int(status_code) if status_code is not None and str(status_code).strip() else None,
            content_type=str(payload.get("content_type") or ""),
            content_length=int(content_length) if content_length is not None and str(content_length).strip() else None,
            text_truncated=bool(payload.get("text_truncated")),
            links=links,
            metadata={str(key): str(value) for key, value in dict(payload.get("metadata") or {}).items()},
            response_headers={str(key): str(value) for key, value in dict(payload.get("response_headers") or {}).items()},
        )


def _coerce_link(payload: dict[str, Any]) -> WebFetchLink:
    return WebFetchLink(
        href=str(payload.get("href") or ""),
        text=str(payload.get("text") or ""),
        rel=str(payload.get("rel") or ""),
    )


@dataclass(frozen=True)
class WebFetchRequest:
    url: str
    timeout_ms: int = 15000
    max_chars: int = 12000
    max_raw_chars: int = 50000
    max_links: int = 80
    user_agent: str = DEFAULT_WEB_FETCH_USER_AGENT


@dataclass(frozen=True)
class WebScreenshotRequest:
    url: str
    timeout_ms: int = 15000
    full_page: bool = False
    viewport_width: int = 1280
    viewport_height: int = 900
    user_agent: str = DEFAULT_WEB_FETCH_USER_AGENT


@dataclass(frozen=True)
class WebScreenshotResult:
    requested_url: str
    final_url: str
    png_bytes: bytes
    title: str = ""
    configured_provider_id: str | None = None
    effective_provider_id: str | None = None
    fallback_used: bool = False
    status_code: int | None = None
    full_page: bool = False
    viewport_width: int = 1280
    viewport_height: int = 900
    user_agent: str = DEFAULT_WEB_FETCH_USER_AGENT


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
    raw_content: str = ""
    raw_content_truncated: bool = False
    status_code: int | None = None
    content_type: str = ""
    content_length: int | None = None
    text_truncated: bool = False
    links: tuple[WebFetchLink, ...] = ()
    metadata: dict[str, str] | None = None
    response_headers: dict[str, str] | None = None
    user_agent: str = DEFAULT_WEB_FETCH_USER_AGENT

    def document_payload(self) -> dict[str, Any]:
        return {
            "status_code": self.status_code,
            "content_type": self.content_type,
            "content_length": self.content_length,
            "text_truncated": self.text_truncated,
            "raw_content_available": bool(self.raw_content),
            "raw_content_truncated": self.raw_content_truncated,
            "links": [link.to_dict() for link in self.links],
            "metadata": dict(self.metadata or {}),
            "response_headers": dict(self.response_headers or {}),
            "user_agent": self.user_agent,
        }


class WebFetchProviderPort(Protocol):
    provider_kind: str

    def read(self, record: WebFetchProviderModel, request: WebFetchRequest) -> WebFetchDocument | dict[str, Any]:
        ...
