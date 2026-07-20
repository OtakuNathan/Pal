from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence
from uuid import uuid4

from pal.minion.v2.artifacts import ArtifactRef, ContentAddressedArtifactStore
from pal.minion.v2.paths import runtime_spool_root


TASK_SOURCE_BUNDLE_ARTIFACT = "TaskSourceBundleArtifact"
TASK_SOURCE_DOCUMENT_ARTIFACT = "TaskSourceDocumentArtifact"
TASK_SOURCE_AMENDMENT_ARTIFACT = "TaskSourceAmendmentArtifact"

_MAX_SOURCE_BYTES = 1024 * 1024
_SAFE_STAMP = re.compile(r"[^0-9A-Za-z._-]+")


@dataclass(frozen=True)
class MaterializedTaskSources:
    root: Path
    files: tuple[str, ...]


class TaskSourceBundleService:
    """Store and materialize the user's exact task sources without interpreting them."""

    def __init__(self, runtime_root: Path, artifacts: ContentAddressedArtifactStore) -> None:
        self.runtime_root = Path(runtime_root)
        self.artifacts = artifacts

    def publish(
        self,
        *,
        title: str,
        request_text: str,
        workspace: Mapping[str, Any],
        source_files: Sequence[str] = (),
        actor: str,
        source_channel: str,
    ) -> ArtifactRef:
        if not request_text.strip():
            raise ValueError("task request must not be empty")
        documents: list[dict[str, Any]] = []
        child_refs: list[tuple[str, str]] = []
        request_ref = self._put_source(
            request_text.encode("utf-8"),
            name="request.md",
            media_type="text/markdown",
            artifact_type=TASK_SOURCE_DOCUMENT_ARTIFACT,
            provenance={
                "actor": actor,
                "source_channel": source_channel,
                "origin": "user_request",
            },
        )
        documents.append(self._entry("request.md", "user_request", request_ref))
        child_refs.append((request_ref.sha256, "source:request.md"))

        root = self._workspace_root(workspace) if source_files else None
        seen = {"request.md"}
        for raw_path in source_files:
            relative = _safe_relative_path(raw_path)
            semantic_name = f"sources/{relative}"
            if semantic_name in seen:
                continue
            seen.add(semantic_name)
            if root is None:
                raise ValueError("source_files require a workspace repo_path")
            target = (root / relative).resolve(strict=True)
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"task source escapes the workspace: {relative}") from exc
            if not target.is_file():
                raise ValueError(f"task source is not a regular file: {relative}")
            data = target.read_bytes()
            _validate_source_bytes(data, name=relative)
            media_type = _media_type(target)
            source_ref = self._put_source(
                data,
                name=semantic_name,
                media_type=media_type,
                artifact_type=TASK_SOURCE_DOCUMENT_ARTIFACT,
                provenance={
                    "actor": actor,
                    "source_channel": source_channel,
                    "origin": "workspace_file",
                    "workspace": str(root),
                    "path": relative,
                },
            )
            documents.append(self._entry(semantic_name, "workspace_file", source_ref))
            child_refs.append((source_ref.sha256, f"source:{semantic_name}"))

        payload = {
            "schema_version": "1",
            "title": str(title or "Task").strip() or "Task",
            "documents": documents,
            "amendments": [],
        }
        return self.artifacts.put_json(
            payload,
            artifact_type=TASK_SOURCE_BUNDLE_ARTIFACT,
            provenance={"actor": actor, "source_channel": source_channel},
            child_refs=tuple(child_refs),
        )

    def append_amendment(
        self,
        *,
        base_ref: ArtifactRef | Mapping[str, Any],
        amendment_text: str,
        workspace: Mapping[str, Any],
        source_files: Sequence[str] = (),
        actor: str,
        source_channel: str,
        observed_at: str | None = None,
    ) -> ArtifactRef:
        if not amendment_text.strip() and not source_files:
            raise ValueError("requirements edit requires amendment text or source files")
        base = validate_task_source_bundle(self.artifacts.read_json(base_ref))
        stamp = observed_at or datetime.now(UTC).isoformat()
        amendments = [dict(item) for item in list(base.get("amendments") or [])]
        documents = [dict(item) for item in list(base.get("documents") or [])]
        child_refs = [
            (str(dict(item["artifact_ref"])["sha256"]), f"source:{item['name']}")
            for item in [*documents, *amendments]
        ]

        sequence = len(amendments) + 1
        if amendment_text.strip():
            name = f"amendments/{sequence:03d}-human-edit.md"
            amendment_ref = self._put_source(
                amendment_text.encode("utf-8"),
                name=name,
                media_type="text/markdown",
                artifact_type=TASK_SOURCE_AMENDMENT_ARTIFACT,
                provenance={
                    "actor": actor,
                    "source_channel": source_channel,
                    "origin": "human_edit",
                    "observed_at": stamp,
                },
            )
            entry = self._entry(name, "human_edit", amendment_ref, observed_at=stamp)
            amendments.append(entry)
            child_refs.append((amendment_ref.sha256, f"source:{name}"))

        root = self._workspace_root(workspace) if source_files else None
        for index, raw_path in enumerate(source_files, start=1):
            relative = _safe_relative_path(raw_path)
            if root is None:
                raise ValueError("source_files require a workspace repo_path")
            target = (root / relative).resolve(strict=True)
            try:
                target.relative_to(root)
            except ValueError as exc:
                raise ValueError(f"task source escapes the workspace: {relative}") from exc
            if not target.is_file():
                raise ValueError(f"task source is not a regular file: {relative}")
            data = target.read_bytes()
            _validate_source_bytes(data, name=relative)
            suffix = target.suffix or ".txt"
            name = f"amendments/{sequence:03d}-source-{index:02d}-{target.stem}{suffix}"
            amendment_ref = self._put_source(
                data,
                name=name,
                media_type=_media_type(target),
                artifact_type=TASK_SOURCE_AMENDMENT_ARTIFACT,
                provenance={
                    "actor": actor,
                    "source_channel": source_channel,
                    "origin": "human_edit_file",
                    "workspace": str(root),
                    "path": relative,
                    "observed_at": stamp,
                },
            )
            entry = self._entry(name, "human_edit_file", amendment_ref, observed_at=stamp)
            amendments.append(entry)
            child_refs.append((amendment_ref.sha256, f"source:{name}"))

        return self.artifacts.put_json(
            {
                "schema_version": "1",
                "title": str(base.get("title") or "Task"),
                "documents": documents,
                "amendments": amendments,
            },
            artifact_type=TASK_SOURCE_BUNDLE_ARTIFACT,
            provenance={"actor": actor, "source_channel": source_channel, "observed_at": stamp},
            child_refs=tuple(child_refs),
        )

    def append_existing_amendments(
        self,
        *,
        base_ref: ArtifactRef | Mapping[str, Any],
        amendment_refs: Sequence[ArtifactRef | Mapping[str, Any]],
        actor: str,
        source_channel: str,
    ) -> ArtifactRef:
        refs = [
            item if isinstance(item, ArtifactRef) else ArtifactRef.from_mapping(item)
            for item in amendment_refs
        ]
        if not refs:
            return base_ref if isinstance(base_ref, ArtifactRef) else ArtifactRef.from_mapping(base_ref)
        base = validate_task_source_bundle(self.artifacts.read_json(base_ref))
        documents = [dict(item) for item in list(base.get("documents") or [])]
        amendments = [dict(item) for item in list(base.get("amendments") or [])]
        known = {
            str(dict(item.get("artifact_ref") or {}).get("sha256") or "")
            for item in amendments
        }
        child_refs = [
            (str(dict(item["artifact_ref"])["sha256"]), f"source:{item['name']}")
            for item in [*documents, *amendments]
        ]
        for ref in refs:
            if ref.sha256 in known:
                continue
            sequence = len(amendments) + 1
            name = f"amendments/{sequence:03d}-architect-clarification.md"
            record = self.artifacts.metadata_repository.read_artifact_record(ref.sha256) or {}
            observed_at = str(dict(record.get("provenance") or {}).get("observed_at") or "")
            entry = self._entry(
                name,
                "architect_user_clarification",
                ref,
                **({"observed_at": observed_at} if observed_at else {}),
            )
            amendments.append(entry)
            child_refs.append((ref.sha256, f"source:{name}"))
            known.add(ref.sha256)
        return self.artifacts.put_json(
            {
                "schema_version": "1",
                "title": str(base.get("title") or "Task"),
                "documents": documents,
                "amendments": amendments,
            },
            artifact_type=TASK_SOURCE_BUNDLE_ARTIFACT,
            provenance={"actor": actor, "source_channel": source_channel},
            child_refs=tuple(child_refs),
        )

    def materialize(
        self,
        bundle_ref: ArtifactRef | Mapping[str, Any],
    ) -> MaterializedTaskSources:
        ref = bundle_ref if isinstance(bundle_ref, ArtifactRef) else ArtifactRef.from_mapping(bundle_ref)
        bundle = validate_task_source_bundle(self.artifacts.read_json(ref))
        root = runtime_spool_root(self.runtime_root) / "task-sources" / ref.sha256
        complete = root / ".complete"
        if complete.is_file():
            return MaterializedTaskSources(root=root, files=_bundle_names(bundle))
        temporary = root.with_name(f".{root.name}.{uuid4().hex}.tmp")
        temporary.mkdir(parents=True, exist_ok=False)
        try:
            for entry in _bundle_entries(bundle):
                relative = _safe_relative_path(str(entry["name"]))
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                data = self.artifacts.read_bytes(dict(entry["artifact_ref"]))
                _write_exclusive(destination, data)
            _write_exclusive(temporary / ".complete", b"ok\n")
            root.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(temporary, root)
            except OSError:
                if not complete.is_file():
                    raise
        finally:
            if temporary.exists():
                for path in sorted(temporary.rglob("*"), reverse=True):
                    if path.is_file() or path.is_symlink():
                        path.unlink()
                    elif path.is_dir():
                        path.rmdir()
                temporary.rmdir()
        return MaterializedTaskSources(root=root, files=_bundle_names(bundle))

    def source_attachments(
        self,
        bundle_ref: ArtifactRef | Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        bundle = validate_task_source_bundle(self.artifacts.read_json(bundle_ref))
        return [
            {
                "name": str(entry["name"]),
                "media_type": str(dict(entry["artifact_ref"]).get("media_type") or "text/plain"),
                "artifact_ref": dict(entry["artifact_ref"]),
            }
            for entry in _bundle_entries(bundle)
        ]

    def materialize_artifact(
        self,
        ref: ArtifactRef,
        *,
        semantic_name: str,
    ) -> Path:
        """Project an immutable artifact to a stable, human-readable filename."""

        safe_name = _SAFE_STAMP.sub("_", semantic_name.strip()).strip("._") or "input"
        suffix = _media_suffix(ref.media_type)
        root = runtime_spool_root(self.runtime_root) / "semantic-inputs" / ref.sha256
        target = root / f"{safe_name}{suffix}"
        if target.is_file():
            return target
        root.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{uuid4().hex}.tmp")
        _write_exclusive(temporary, self.artifacts.read_bytes(ref))
        try:
            os.replace(temporary, target)
        finally:
            if temporary.exists():
                temporary.unlink()
        return target

    def _put_source(
        self,
        data: bytes,
        *,
        name: str,
        media_type: str,
        artifact_type: str,
        provenance: Mapping[str, Any],
    ) -> ArtifactRef:
        _validate_source_bytes(data, name=name)
        return self.artifacts.put_bytes(
            data,
            artifact_type=artifact_type,
            media_type=media_type,
            provenance=provenance,
            metadata={"name": name},
        )

    @staticmethod
    def _entry(
        name: str,
        origin: str,
        ref: ArtifactRef,
        *,
        observed_at: str = "",
    ) -> dict[str, Any]:
        value: dict[str, Any] = {
            "name": name,
            "origin": origin,
            "artifact_ref": ref.to_dict(),
        }
        if observed_at:
            value["observed_at"] = observed_at
        return value

    @staticmethod
    def _workspace_root(workspace: Mapping[str, Any]) -> Path:
        root_value = str(workspace.get("repo_path") or workspace.get("cwd") or "").strip()
        if not root_value:
            raise ValueError("source_files require a workspace repo_path")
        root = Path(root_value).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise ValueError("workspace repo_path must be a directory")
        return root


def validate_task_source_bundle(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("TaskSourceBundleArtifact must be an object")
    payload = dict(value)
    if str(payload.get("schema_version") or "") != "1":
        raise ValueError("unsupported TaskSourceBundleArtifact schema_version")
    documents = list(payload.get("documents") or [])
    if not documents:
        raise ValueError("TaskSourceBundleArtifact must contain request.md")
    names: set[str] = set()
    for entry in _bundle_entries(payload):
        name = _safe_relative_path(str(entry.get("name") or ""))
        if name in names:
            raise ValueError(f"duplicate task source name: {name}")
        names.add(name)
        ref = dict(entry.get("artifact_ref") or {})
        if not str(ref.get("sha256") or ""):
            raise ValueError(f"task source {name!r} has no durable artifact reference")
    if "request.md" not in names:
        raise ValueError("TaskSourceBundleArtifact must contain request.md")
    return payload


def _bundle_entries(bundle: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(item)
        for item in [*list(bundle.get("documents") or []), *list(bundle.get("amendments") or [])]
        if isinstance(item, Mapping)
    ]


def _bundle_names(bundle: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(item["name"]) for item in _bundle_entries(bundle))


def _safe_relative_path(value: str) -> str:
    text = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(text)
    if not text or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"task source path must be a safe relative path: {value!r}")
    return path.as_posix()


def _validate_source_bytes(data: bytes, *, name: str) -> None:
    if len(data) > _MAX_SOURCE_BYTES:
        raise ValueError(f"task source exceeds {_MAX_SOURCE_BYTES} bytes: {name}")
    if b"-----BEGIN " in data and b"PRIVATE KEY-----" in data:
        raise ValueError(f"task source contains private key material: {name}")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"task source must be UTF-8 text: {name}") from exc


def _media_type(path: Path) -> str:
    return "text/markdown" if path.suffix.lower() in {".md", ".markdown"} else "text/plain"


def _media_suffix(media_type: str) -> str:
    normalized = str(media_type or "").lower()
    if normalized == "application/json":
        return ".json"
    if "markdown" in normalized:
        return ".md"
    if "diff" in normalized or "patch" in normalized:
        return ".diff"
    return ".txt"


def _write_exclusive(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
