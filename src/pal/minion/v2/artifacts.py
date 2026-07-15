from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol
from uuid import uuid4

from pal.minion.v2.paths import artifact_store_root


@dataclass(frozen=True)
class ArtifactRef:
    sha256: str
    artifact_type: str
    schema_version: str
    media_type: str
    byte_size: int
    durable: bool = True

    @property
    def uri(self) -> str:
        return f"sha256:{self.sha256}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "artifact_type": self.artifact_type,
            "schema_version": self.schema_version,
            "media_type": self.media_type,
            "byte_size": self.byte_size,
            "durable": self.durable,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ArtifactRef":
        return cls(
            sha256=str(value.get("sha256") or ""),
            artifact_type=str(value.get("artifact_type") or ""),
            schema_version=str(value.get("schema_version") or ""),
            media_type=str(value.get("media_type") or "application/octet-stream"),
            byte_size=int(value.get("byte_size") or 0),
            durable=bool(value.get("durable")),
        )


class ArtifactMetadataPort(Protocol):
    def record_artifact(
        self,
        ref: ArtifactRef,
        *,
        storage_path: Path,
        provenance: Mapping[str, Any],
        metadata: Mapping[str, Any],
        child_refs: tuple[tuple[str, str], ...],
    ) -> None:
        ...

    def read_artifact_record(self, sha256: str) -> Mapping[str, Any] | None:
        ...


@dataclass
class ContentAddressedArtifactStore:
    runtime_root: Path
    metadata_repository: ArtifactMetadataPort
    root: Path = field(init=False)

    def __post_init__(self) -> None:
        self.root = artifact_store_root(self.runtime_root)

    def put_json(
        self,
        value: Any,
        *,
        artifact_type: str,
        schema_version: str = "1",
        provenance: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        child_refs: tuple[tuple[str, str], ...] = (),
    ) -> ArtifactRef:
        data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return self.put_bytes(
            data,
            artifact_type=artifact_type,
            schema_version=schema_version,
            media_type="application/json",
            provenance=provenance,
            metadata=metadata,
            child_refs=child_refs,
        )

    def put_bytes(
        self,
        data: bytes,
        *,
        artifact_type: str,
        schema_version: str = "1",
        media_type: str = "application/octet-stream",
        provenance: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
        child_refs: tuple[tuple[str, str], ...] = (),
    ) -> ArtifactRef:
        if not artifact_type.strip():
            raise ValueError("artifact_type is required")
        normalized_type = artifact_type.strip()
        normalized_schema = str(schema_version or "1")
        normalized_media = str(media_type or "application/octet-stream")
        digest = self._typed_digest(
            data,
            artifact_type=normalized_type,
            schema_version=normalized_schema,
            media_type=normalized_media,
        )
        destination = self._path_for_digest(digest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            temporary = destination.parent / f".{digest}.{uuid4().hex}.tmp"
            try:
                with temporary.open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
                self._fsync_directory(destination.parent)
            finally:
                if temporary.exists():
                    temporary.unlink()
        elif self._typed_digest(
            destination.read_bytes(),
            artifact_type=normalized_type,
            schema_version=normalized_schema,
            media_type=normalized_media,
        ) != digest:
            raise IOError(f"artifact digest collision or corruption: {destination}")
        ref = ArtifactRef(
            sha256=digest,
            artifact_type=normalized_type,
            schema_version=normalized_schema,
            media_type=normalized_media,
            byte_size=len(data),
            durable=True,
        )
        self.metadata_repository.record_artifact(
            ref,
            storage_path=destination,
            provenance=dict(provenance or {}),
            metadata=dict(metadata or {}),
            child_refs=tuple(child_refs),
        )
        return ref

    def read_bytes(self, ref: ArtifactRef | Mapping[str, Any] | str) -> bytes:
        digest = self._digest_from_ref(ref)
        metadata = self._metadata_from_ref(ref, digest=digest)
        path = self._path_for_digest(digest)
        data = path.read_bytes()
        if self._typed_digest(data, **metadata) != digest:
            raise IOError(f"artifact failed digest verification: {digest}")
        return data

    def read_json(self, ref: ArtifactRef | Mapping[str, Any] | str) -> Any:
        return json.loads(self.read_bytes(ref).decode("utf-8"))

    def _path_for_digest(self, digest: str) -> Path:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("artifact sha256 must be 64 lowercase hexadecimal characters")
        return self.root / digest[:2] / digest

    def _digest_from_ref(self, ref: ArtifactRef | Mapping[str, Any] | str) -> str:
        if isinstance(ref, ArtifactRef):
            return ref.sha256
        if isinstance(ref, Mapping):
            return str(ref.get("sha256") or "")
        text = str(ref)
        return text.removeprefix("sha256:")

    def _metadata_from_ref(
        self,
        ref: ArtifactRef | Mapping[str, Any] | str,
        *,
        digest: str,
    ) -> dict[str, str]:
        if isinstance(ref, ArtifactRef):
            values: Mapping[str, Any] = ref.to_dict()
        elif isinstance(ref, Mapping):
            values = ref
        else:
            record = self.metadata_repository.read_artifact_record(digest)
            if record is None:
                raise ValueError(f"artifact metadata is unavailable: {digest}")
            values = record
        artifact_type = str(values.get("artifact_type") or "")
        schema_version = str(values.get("schema_version") or "")
        media_type = str(values.get("media_type") or "")
        if not artifact_type or not schema_version or not media_type:
            raise ValueError("typed artifact ref requires artifact_type, schema_version, and media_type")
        return {
            "artifact_type": artifact_type,
            "schema_version": schema_version,
            "media_type": media_type,
        }

    @staticmethod
    def _typed_digest(
        data: bytes,
        *,
        artifact_type: str,
        schema_version: str,
        media_type: str,
    ) -> str:
        digest = hashlib.sha256()
        for value in (artifact_type, schema_version, media_type):
            digest.update(value.encode("utf-8"))
            digest.update(b"\0")
        digest.update(data)
        return digest.hexdigest()

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
