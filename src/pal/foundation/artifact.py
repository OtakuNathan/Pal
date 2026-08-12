from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class StoredArtifact:
    artifact_id: str
    local_cached_path: str
    sha256: str
    size_bytes: int
    mime_type: str | None
    owned_by_pal: bool = True
    metadata: dict[str, Any] = field(default_factory=dict, compare=False, hash=False, repr=False)


class ArtifactIngestor:
    def __init__(self, runtime_root: Path) -> None:
        self._runtime_root = runtime_root

    def _artifact_dir(self, *, channel_kind: str, bucket_id: str, artifact_id: str) -> Path:
        return (
            self._runtime_root
            / "artifacts"
            / _safe_path_component(channel_kind, default="channel")
            / _safe_path_component(bucket_id, default="bucket")
            / artifact_id
        )

    def store_bytes(
        self,
        *,
        channel_kind: str,
        bucket_id: str,
        file_name: str | None,
        content: bytes,
        mime_type: str | None = None,
    ) -> StoredArtifact:
        artifact_id = f"artifact_{uuid4().hex[:16]}"
        artifact_dir = self._artifact_dir(
            channel_kind=channel_kind,
            bucket_id=bucket_id,
            artifact_id=artifact_id,
        )
        artifact_dir.mkdir(parents=True, exist_ok=True)
        final_name = _safe_file_name(file_name)
        path = artifact_dir / final_name
        path.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        detected_mime = mime_type or mimetypes.guess_type(final_name)[0]
        return StoredArtifact(
            artifact_id=artifact_id,
            local_cached_path=str(path),
            sha256=digest,
            size_bytes=len(content),
            mime_type=detected_mime,
        )


def _safe_path_component(value: str | None, *, default: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in str(value or "")
    ).strip(".")
    return _truncate_utf8(cleaned, max_bytes=96) or default


def _safe_file_name(value: str | None) -> str:
    # Provider-controlled names are labels, never paths. Treat both POSIX and
    # Windows separators as such even when Pal runs on the other platform.
    leaf = str(value or "payload.bin").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(
        character if character.isalnum() or character in {"-", "_", "."} else "_"
        for character in leaf
    ).strip(".")
    return _truncate_utf8(cleaned, max_bytes=180) or "payload.bin"


def _truncate_utf8(value: str, *, max_bytes: int) -> str:
    return value.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
