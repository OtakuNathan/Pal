from __future__ import annotations

import base64
import json
import mimetypes
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from pal.foundation.diagnostics import exception_report
from pal.artifact.contracts import (
    ARTIFACT_KIND_AUDIO,
    ARTIFACT_KIND_BINARY,
    ARTIFACT_KIND_IMAGE,
    ARTIFACT_KIND_PDF,
    ARTIFACT_KIND_TEXT,
    ARTIFACT_STATUS_PARTIAL,
    ARTIFACT_STATUS_READY,
    ArtifactPolicy,
    ArtifactRecord,
    ArtifactRepresentation,
    REPRESENTATION_METADATA,
    REPRESENTATION_NORMALIZED_IMAGE,
    REPRESENTATION_PAGE_IMAGE,
    REPRESENTATION_PAGE_TEXT,
    REPRESENTATION_TEXT,
)


class ArtifactProcessor(Protocol):
    kind: str

    def process(self, context: "ArtifactProcessingContext") -> ArtifactRecord:
        ...


@dataclass
class ArtifactProcessingContext:
    record: ArtifactRecord
    root: Path
    repository: object
    policy: ArtifactPolicy

    @property
    def original_path(self) -> Path:
        return Path(self.record.original_path)

    def representations_dir(self) -> Path:
        path = self.root / "representations"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def put_representation(self, representation: ArtifactRepresentation) -> None:
        self.repository.upsert_representation(representation)


@dataclass
class ArtifactProcessorRegistry:
    processors: dict[str, ArtifactProcessor]
    mime_kind_map: dict[str, str]
    suffix_kind_map: dict[str, str]

    @classmethod
    def defaults(cls) -> "ArtifactProcessorRegistry":
        processors: dict[str, ArtifactProcessor] = {
            ARTIFACT_KIND_IMAGE: ImageArtifactProcessor(),
            ARTIFACT_KIND_TEXT: TextArtifactProcessor(),
            ARTIFACT_KIND_PDF: PdfArtifactProcessor(),
            ARTIFACT_KIND_AUDIO: AudioArtifactProcessor(),
            ARTIFACT_KIND_BINARY: MetadataArtifactProcessor(),
        }
        return cls(
            processors=processors,
            mime_kind_map={
                "image/": ARTIFACT_KIND_IMAGE,
                "text/": ARTIFACT_KIND_TEXT,
                "application/json": ARTIFACT_KIND_TEXT,
                "application/xml": ARTIFACT_KIND_TEXT,
                "application/pdf": ARTIFACT_KIND_PDF,
                "audio/": ARTIFACT_KIND_AUDIO,
            },
            suffix_kind_map={
                ".txt": ARTIFACT_KIND_TEXT,
                ".md": ARTIFACT_KIND_TEXT,
                ".json": ARTIFACT_KIND_TEXT,
                ".csv": ARTIFACT_KIND_TEXT,
                ".log": ARTIFACT_KIND_TEXT,
                ".py": ARTIFACT_KIND_TEXT,
                ".js": ARTIFACT_KIND_TEXT,
                ".ts": ARTIFACT_KIND_TEXT,
                ".html": ARTIFACT_KIND_TEXT,
                ".css": ARTIFACT_KIND_TEXT,
                ".pdf": ARTIFACT_KIND_PDF,
                ".png": ARTIFACT_KIND_IMAGE,
                ".jpg": ARTIFACT_KIND_IMAGE,
                ".jpeg": ARTIFACT_KIND_IMAGE,
                ".webp": ARTIFACT_KIND_IMAGE,
                ".gif": ARTIFACT_KIND_IMAGE,
                ".mp3": ARTIFACT_KIND_AUDIO,
                ".wav": ARTIFACT_KIND_AUDIO,
                ".ogg": ARTIFACT_KIND_AUDIO,
                ".m4a": ARTIFACT_KIND_AUDIO,
            },
        )

    def resolve_kind(self, *, mime_type: str, file_name: str) -> str:
        normalized_mime = str(mime_type or "").strip().lower()
        for prefix, kind in self.mime_kind_map.items():
            if prefix.endswith("/") and normalized_mime.startswith(prefix):
                return kind
            if normalized_mime == prefix:
                return kind
        suffix = Path(file_name).suffix.lower()
        return self.suffix_kind_map.get(suffix, ARTIFACT_KIND_BINARY)

    def processor_for(self, kind: str) -> ArtifactProcessor:
        return self.processors.get(kind) or self.processors[ARTIFACT_KIND_BINARY]


@dataclass
class MetadataArtifactProcessor:
    kind: str = ARTIFACT_KIND_BINARY

    def process(self, context: ArtifactProcessingContext) -> ArtifactRecord:
        record = context.record
        context.put_representation(
            ArtifactRepresentation(
                representation_id=_representation_id(record.artifact_id, REPRESENTATION_METADATA, "default"),
                artifact_id=record.artifact_id,
                representation_kind=REPRESENTATION_METADATA,
                mime_type=record.original_mime_type,
                size_bytes=record.original_size_bytes,
                summary=f"{record.file_name} ({record.original_mime_type or 'unknown MIME'})",
                status=ARTIFACT_STATUS_READY,
                metadata={
                    "file_name": record.file_name,
                    "mime_type": record.original_mime_type,
                    "size_bytes": record.original_size_bytes,
                },
            )
        )
        return replace(
            record,
            summary=f"{record.file_name} ({record.kind})",
            status=ARTIFACT_STATUS_READY if record.kind == ARTIFACT_KIND_BINARY else ARTIFACT_STATUS_PARTIAL,
        )


@dataclass
class TextArtifactProcessor:
    kind: str = ARTIFACT_KIND_TEXT

    def process(self, context: ArtifactProcessingContext) -> ArtifactRecord:
        record = context.record
        raw = context.original_path.read_bytes()
        text = _decode_text(raw)
        # Reuse UTF-8 source bytes when no encoding conversion is needed.
        try:
            unchanged = raw.decode("utf-8") == text
        except UnicodeDecodeError:
            unchanged = False
        text_path = context.original_path if unchanged else context.representations_dir() / "normalized.txt"
        if not unchanged:
            text_path.write_text(text, encoding="utf-8")
        preview = _preview(text)
        context.put_representation(
            ArtifactRepresentation(
                representation_id=_representation_id(record.artifact_id, REPRESENTATION_TEXT, "full"),
                artifact_id=record.artifact_id,
                representation_kind=REPRESENTATION_TEXT,
                path=str(text_path),
                mime_type="text/plain",
                size_bytes=text_path.stat().st_size,
                text_preview=preview,
                summary=preview,
                status=ARTIFACT_STATUS_READY,
            )
        )
        return replace(
            record,
            normalized_path=str(text_path),
            normalized_mime_type="text/plain",
            normalized_size_bytes=text_path.stat().st_size,
            summary=preview or f"text artifact {record.file_name}",
            status=ARTIFACT_STATUS_READY,
        )


@dataclass
class ImageArtifactProcessor:
    kind: str = ARTIFACT_KIND_IMAGE

    def process(self, context: ArtifactProcessingContext) -> ArtifactRecord:
        record = context.record
        output_path = context.representations_dir() / "normalized.jpg"
        normalized, normalized_mime_type = prepare_inline_image_file(
            context.original_path,
            output_path,
            policy=context.policy,
        )
        context.put_representation(
            ArtifactRepresentation(
                representation_id=_representation_id(record.artifact_id, REPRESENTATION_NORMALIZED_IMAGE, "default"),
                artifact_id=record.artifact_id,
                representation_kind=REPRESENTATION_NORMALIZED_IMAGE,
                path=str(normalized),
                mime_type=normalized_mime_type,
                size_bytes=normalized.stat().st_size,
                summary=f"normalized image {record.file_name}",
                status=ARTIFACT_STATUS_READY,
                metadata={"base64_size_bytes": _base64_size(normalized.stat().st_size)},
            )
        )
        return replace(
            record,
            normalized_path=str(normalized),
            normalized_mime_type=normalized_mime_type,
            normalized_size_bytes=normalized.stat().st_size,
            summary=f"image artifact {record.file_name}",
            status=ARTIFACT_STATUS_READY,
        )


@dataclass
class PdfArtifactProcessor:
    kind: str = ARTIFACT_KIND_PDF

    def process(self, context: ArtifactProcessingContext) -> ArtifactRecord:
        record = context.record
        try:
            import fitz  # type: ignore
        except Exception as exc:
            return replace(record, status=ARTIFACT_STATUS_PARTIAL, notes="PyMuPDF is not available: " + exception_report(exc))

        doc = fitz.open(str(context.original_path))
        try:
            page_locations = []
            page_count = len(doc)
            max_pages = min(max(0, int(context.policy.pdf.max_pages)), page_count)
            pages_dir = context.representations_dir() / "pages"
            pages_dir.mkdir(parents=True, exist_ok=True)
            text_chars = 0
            for index in range(page_count):
                location = {"page": index + 1, "status": "not_processed", "has_text": False}
                page_locations.append(location)
                if index >= max_pages:
                    location["reason"] = "configured page limit"
                    continue
                errors = []
                try:
                    text = str(doc.load_page(index).get_text("text") or "").strip()
                    page_path = pages_dir / f"page_{index + 1:04d}.txt"
                    page_path.write_text(text, encoding="utf-8")
                    location.update(file_path=str(page_path), extension=".txt", has_text=bool(text),
                                    text_line_count=text.count("\n") + 1 if text else 0)
                    text_chars += len(text)
                    context.put_representation(ArtifactRepresentation(
                        representation_id=_representation_id(record.artifact_id, REPRESENTATION_PAGE_TEXT, str(index + 1)),
                        artifact_id=record.artifact_id, representation_kind=REPRESENTATION_PAGE_TEXT,
                        selector={"page": index + 1}, path=str(page_path), mime_type="text/plain",
                        size_bytes=page_path.stat().st_size, text_preview=_preview(text),
                        summary=_preview(text), status=ARTIFACT_STATUS_READY,
                    ))
                except Exception as exc:
                    errors.append("text: " + exception_report(exc))
                try:
                    images = _render_pdf_page_images(context, record, doc, page_indices=(index,))
                    location["image_file_path"] = images[index + 1]
                    location["image_extension"] = Path(images[index + 1]).suffix.lower()
                except Exception as exc:
                    errors.append("image: " + exception_report(exc))
                location["status"] = "partial" if errors else "ready"
                if errors:
                    location["errors"] = errors
            index_path = context.representations_dir() / "page_index.json"
            partial = any(page["status"] != "ready" for page in page_locations)
            page_access = {"page_index_file_path": str(index_path),
                           "page_file_pattern": str(pages_dir / "page_{page:04d}.txt"),
                           "page_image_file_pattern": str(context.representations_dir() / "page_images" / "page_{page:04d}.jpg"),
                           "page_count": page_count, "processed_pages": max_pages,
                           "extracted_pages": sum("file_path" in page for page in page_locations),
                           "extraction_truncated": max_pages < page_count}
            index_path.write_text(json.dumps({"source_file_name": record.file_name, "source_extension": ".pdf",
                                             **page_access, "pages": page_locations},
                                             ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            doc.close()
        return replace(record,
            summary=f"PDF artifact {record.file_name}, {page_count} page(s)",
            status=ARTIFACT_STATUS_PARTIAL if partial else ARTIFACT_STATUS_READY,
            metadata={**dict(record.metadata), "extracted_text_chars": text_chars, **page_access})


@dataclass
class AudioArtifactProcessor:
    kind: str = ARTIFACT_KIND_AUDIO

    def process(self, context: ArtifactProcessingContext) -> ArtifactRecord:
        record = context.record
        summary = f"audio artifact {record.file_name}; not transcribed"
        context.put_representation(
            ArtifactRepresentation(
                representation_id=_representation_id(record.artifact_id, REPRESENTATION_METADATA, "audio"),
                artifact_id=record.artifact_id,
                representation_kind=REPRESENTATION_METADATA,
                mime_type=record.original_mime_type,
                size_bytes=record.original_size_bytes,
                summary=summary,
                status=ARTIFACT_STATUS_READY,
                metadata={"transcription_status": "not_requested"},
            )
        )
        return replace(record, summary=summary, status=ARTIFACT_STATUS_READY)


def prepare_inline_image_file(input_path: Path, output_path: Path, *, policy: ArtifactPolicy) -> tuple[Path, str]:
    original_mime = mimetypes.guess_type(input_path.name)[0] or ""
    if _can_preserve_original_image(input_path, original_mime, policy=policy):
        suffix = input_path.suffix.lower() or _suffix_for_mime(original_mime)
        preserved_path = output_path.with_name(f"normalized{suffix or '.img'}")
        preserved_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_path, preserved_path)
        return preserved_path, original_mime or "application/octet-stream"
    normalized = normalize_image_file(input_path, output_path, policy=policy)
    return normalized, "image/jpeg"


def normalize_image_file(input_path: Path, output_path: Path, *, policy: ArtifactPolicy) -> Path:
    from PIL import Image, ImageOps  # type: ignore

    with Image.open(input_path) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode not in {"RGB", "L"}:
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "RGBA":
                background.paste(img, mask=img.getchannel("A"))
            else:
                background.paste(img.convert("RGB"))
            img = background
        elif img.mode == "L":
            img = img.convert("RGB")
        max_edge = max(img.size)
        if max_edge > policy.image.max_edge_px:
            scale = policy.image.max_edge_px / float(max_edge)
            new_size = (max(1, int(img.size[0] * scale)), max(1, int(img.size[1] * scale)))
            img = img.resize(new_size, Image.Resampling.LANCZOS)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        best_path = output_path
        for quality in policy.image.quality_ladder:
            img.save(best_path, format="JPEG", quality=int(quality), optimize=True)
            if _base64_size(best_path.stat().st_size) <= policy.image.inline_base64_budget_bytes:
                return best_path
        width, height = img.size
        while _base64_size(best_path.stat().st_size) > policy.image.inline_base64_budget_bytes and max(width, height) > 512:
            width = max(1, int(width * 0.8))
            height = max(1, int(height * 0.8))
            img = img.resize((width, height), Image.Resampling.LANCZOS)
            img.save(best_path, format="JPEG", quality=int(policy.image.quality_ladder[-1]), optimize=True)
        return best_path


def _can_preserve_original_image(input_path: Path, mime_type: str, *, policy: ArtifactPolicy) -> bool:
    if mime_type not in {"image/jpeg", "image/png", "image/webp"}:
        return False
    if _base64_size(input_path.stat().st_size) > policy.image.inline_base64_budget_bytes:
        return False
    from PIL import Image  # type: ignore

    with Image.open(input_path) as img:
        within_limit = max(img.size) <= policy.image.max_edge_px
        img.verify()
        return within_limit


def _suffix_for_mime(mime_type: str) -> str:
    if mime_type == "image/jpeg":
        return ".jpg"
    if mime_type == "image/png":
        return ".png"
    if mime_type == "image/webp":
        return ".webp"
    return ""


def image_data_url(path: Path, *, mime_type: str = "image/jpeg") -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type or mimetypes.guess_type(path.name)[0] or 'application/octet-stream'};base64,{encoded}"


def _render_pdf_page_images(context: ArtifactProcessingContext, record: ArtifactRecord, doc, *, page_indices=None) -> dict[int, str]:
    if page_indices is None:
        page_indices = range(min(int(context.policy.pdf.max_pages), len(doc)))
    images_dir = context.representations_dir() / "page_images"
    images_dir.mkdir(parents=True, exist_ok=True)
    images = {}
    for index in page_indices:
        page = doc.load_page(index)
        pix = page.get_pixmap(alpha=False)
        raw_path = images_dir / f"page_{index + 1:04d}.png"
        pix.save(str(raw_path))
        normalized_path = images_dir / f"page_{index + 1:04d}.jpg"
        normalized = normalize_image_file(raw_path, normalized_path, policy=context.policy)
        context.put_representation(
            ArtifactRepresentation(
                representation_id=_representation_id(record.artifact_id, REPRESENTATION_PAGE_IMAGE, str(index + 1)),
                artifact_id=record.artifact_id,
                representation_kind=REPRESENTATION_PAGE_IMAGE,
                selector={"page": index + 1},
                path=str(normalized),
                mime_type="image/jpeg",
                size_bytes=normalized.stat().st_size,
                summary=f"PDF page image {index + 1}",
                status=ARTIFACT_STATUS_READY,
                metadata={"base64_size_bytes": _base64_size(normalized.stat().st_size)},
            )
        )

        images[index + 1] = str(normalized)
    return images


def _representation_id(artifact_id: str, representation_kind: str, selector: str) -> str:
    safe_selector = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in selector) or "default"
    return f"{artifact_id}:{representation_kind}:{safe_selector}"


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _preview(text: str, *, limit: int = 240) -> str:
    normalized = " ".join(str(text or "").split())
    if len(normalized) <= limit:
        return normalized
    return normalized[:limit].rstrip() + "..."


def _base64_size(size_bytes: int) -> int:
    return ((int(size_bytes) + 2) // 3) * 4
