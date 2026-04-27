from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


ARTIFACT_KIND_IMAGE = "image"
ARTIFACT_KIND_TEXT = "text"
ARTIFACT_KIND_PDF = "pdf"
ARTIFACT_KIND_AUDIO = "audio"
ARTIFACT_KIND_BINARY = "binary"

ARTIFACT_STATUS_READY = "ready"
ARTIFACT_STATUS_PARTIAL = "partial"
ARTIFACT_STATUS_FAILED = "failed"
ARTIFACT_STATUS_PENDING = "pending"

REPRESENTATION_METADATA = "metadata"
REPRESENTATION_TEXT = "text"
REPRESENTATION_CHUNK_TEXT = "chunk_text"
REPRESENTATION_PAGE_TEXT = "page_text"
REPRESENTATION_PAGE_IMAGE = "page_image"
REPRESENTATION_NORMALIZED_IMAGE = "normalized_image"
REPRESENTATION_TRANSCRIPT = "transcript"


@dataclass(frozen=True)
class ArtifactLifecyclePolicy:
    hot_ttl_seconds: int = 2 * 60 * 60
    refresh_seconds: int = 2 * 60 * 60
    hard_cap_seconds: int = 24 * 60 * 60


@dataclass(frozen=True)
class ImageArtifactPolicy:
    inline_base64_budget_bytes: int = 4 * 1024 * 1024
    max_edge_px: int = 1568
    quality_ladder: tuple[int, ...] = (85, 75, 65, 55)
    max_inline_images: int = 4


@dataclass(frozen=True)
class TextArtifactPolicy:
    inline_budget_chars: int = 12_000
    read_default_max_chars: int = 12_000
    read_hard_max_chars: int = 50_000
    chunk_chars: int = 8_000


@dataclass(frozen=True)
class PdfArtifactPolicy:
    max_pages: int = 200
    eager_fallback_page_images: int = 8
    textless_min_chars: int = 40


@dataclass(frozen=True)
class ArtifactLimits:
    max_original_bytes: int = 50 * 1024 * 1024


@dataclass(frozen=True)
class ArtifactExposurePolicy:
    max_hot_prompt_refs: int = 5
    default_prompt_actions: tuple[str, ...] = ("info", "read", "search")
    relevance_terms: tuple[str, ...] = (
        "this",
        "that",
        "attached",
        "attachment",
        "file",
        "document",
        "pdf",
        "image",
        "photo",
        "picture",
        "artifact",
        "look",
        "see",
        "read",
        "shown",
        "\u521a\u624d",
        "\u4e4b\u524d",
        "\u8fd9\u4e2a",
        "\u8fd9\u4efd",
        "\u8fd9\u5f20",
        "\u90a3\u4e2a",
        "\u90a3\u5f20",
        "\u9644\u4ef6",
        "\u6587\u4ef6",
        "\u56fe",
        "\u56fe\u7247",
        "\u7167\u7247",
        "\u770b\u5230",
        "\u770b\u4e00\u4e0b",
        "\u8bfb\u53d6",
        "audio",
        "voice",
        "刚才",
        "这个",
        "这份",
        "这张",
        "附件",
        "文件",
        "图片",
        "照片",
        "语音",
    )


@dataclass(frozen=True)
class ArtifactPolicy:
    lifecycle: ArtifactLifecyclePolicy = field(default_factory=ArtifactLifecyclePolicy)
    image: ImageArtifactPolicy = field(default_factory=ImageArtifactPolicy)
    text: TextArtifactPolicy = field(default_factory=TextArtifactPolicy)
    pdf: PdfArtifactPolicy = field(default_factory=PdfArtifactPolicy)
    limits: ArtifactLimits = field(default_factory=ArtifactLimits)
    exposure: ArtifactExposurePolicy = field(default_factory=ArtifactExposurePolicy)


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    scope_key: str
    turn_id: str
    kind: str
    source_channel: str
    file_name: str
    original_path: str
    original_mime_type: str
    original_size_bytes: int
    normalized_path: str = ""
    normalized_mime_type: str = ""
    normalized_size_bytes: int = 0
    summary: str = ""
    status: str = ARTIFACT_STATUS_PENDING
    notes: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ArtifactRepresentation:
    representation_id: str
    artifact_id: str
    representation_kind: str
    selector: dict[str, Any] = field(default_factory=dict)
    path: str = ""
    mime_type: str = ""
    size_bytes: int = 0
    text_preview: str = ""
    summary: str = ""
    status: str = ARTIFACT_STATUS_READY
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class ArtifactHotState:
    hot_id: str
    artifact_id: str
    scope_key: str
    last_accessed_at: str
    expires_at: str
    hard_expires_at: str
    access_count: int = 0


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    kind: str
    file_name: str
    summary: str
    status: str
    available_actions: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "kind": self.kind,
            "file_name": self.file_name,
            "summary": self.summary,
            "status": self.status,
            "available_actions": list(self.available_actions),
        }


@dataclass(frozen=True)
class ArtifactInlinePart:
    part_type: str
    artifact_id: str
    representation_id: str
    mime_type: str = ""
    source_url: str = ""

    def to_message_part(self) -> dict[str, Any]:
        part: dict[str, Any] = {
            "type": self.part_type,
            "artifact_id": self.artifact_id,
            "representation_id": self.representation_id,
            "mime_type": self.mime_type,
        }
        if self.source_url:
            part["source_url"] = self.source_url
        return part


@dataclass(frozen=True)
class ArtifactPromptExposure:
    text: str = ""
    inline_parts: tuple[ArtifactInlinePart, ...] = ()


@dataclass(frozen=True)
class ArtifactReadResult:
    artifact_id: str
    ok: bool
    kind: str
    representation: str
    text: str = ""
    truncated: bool = False
    selection: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    next_actions: tuple[str, ...] = ()
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "ok": self.ok,
            "kind": self.kind,
            "representation": self.representation,
            "text": self.text,
            "truncated": self.truncated,
            "selection": dict(self.selection),
            "metadata": dict(self.metadata),
            "next_actions": list(self.next_actions),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ArtifactSearchResult:
    artifact_id: str
    score: float
    kind: str
    file_name: str
    summary: str
    received_at: str
    last_accessed_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "score": self.score,
            "kind": self.kind,
            "file_name": self.file_name,
            "summary": self.summary,
            "received_at": self.received_at,
            "last_accessed_at": self.last_accessed_at,
        }


@dataclass(frozen=True)
class ArtifactContentSearchResult:
    artifact_id: str
    representation_id: str
    representation: str
    score: float
    text: str
    selector: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "representation_id": self.representation_id,
            "representation": self.representation,
            "score": self.score,
            "text": self.text,
            "selector": dict(self.selector),
        }


class ArtifactTranscriberPort(Protocol):
    def transcribe(self, path: Path, *, mime_type: str = "") -> str | None:
        ...
