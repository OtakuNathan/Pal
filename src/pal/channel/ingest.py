from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4


@dataclass(frozen=True)
class StoredArtifact:
    artifact_id: str
    local_cached_path: str
    sha256: str
    size_bytes: int
    mime_type: str | None


class ArtifactIngestor:
    def __init__(self, runtime_root: Path) -> None:
        self._runtime_root = runtime_root

    def _artifact_dir(self, *, channel_kind: str, bucket_id: str, artifact_id: str) -> Path:
        return self._runtime_root / "artifacts" / channel_kind / bucket_id / artifact_id

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
        final_name = file_name or "payload.bin"
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
