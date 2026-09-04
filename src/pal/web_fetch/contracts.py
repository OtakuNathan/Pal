from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_WEB_FETCH_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux aarch64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class WebFetchLink:
    href: str
    text: str = ""
    rel: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"href": self.href, "text": self.text, "rel": self.rel}


@dataclass(frozen=True)
class WebFetchDocument:
    requested_url: str
    final_url: str
    title: str = ""
    text: str = ""
    text_truncated: bool = False
    links: tuple[WebFetchLink, ...] = ()
    content_type: str = ""
    metadata: dict[str, str] | None = None

    @classmethod
    def from_mapping(cls, payload: dict[str, Any]) -> "WebFetchDocument":
        return cls(
            requested_url=str(payload.get("requested_url") or payload.get("final_url") or ""),
            final_url=str(payload.get("final_url") or payload.get("requested_url") or ""),
            title=str(payload.get("title") or ""),
            text=str(payload.get("text") or ""),
            text_truncated=bool(payload.get("text_truncated")),
            links=tuple(
                WebFetchLink(
                    href=str(item.get("href") or ""),
                    text=str(item.get("text") or ""),
                    rel=str(item.get("rel") or ""),
                )
                for item in list(payload.get("links") or [])
                if isinstance(item, dict)
            ),
            content_type=str(payload.get("content_type") or ""),
            metadata={str(key): str(value) for key, value in dict(payload.get("metadata") or {}).items()},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "title": self.title,
            "text": self.text,
            "text_truncated": self.text_truncated,
            "links": [item.to_dict() for item in self.links],
            "content_type": self.content_type,
            "metadata": dict(self.metadata or {}),
        }
