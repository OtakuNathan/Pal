from __future__ import annotations

import json
import mimetypes
import re
import shutil
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pal.artifact.contracts import (
    ARTIFACT_KIND_AUDIO,
    ARTIFACT_KIND_IMAGE,
    ARTIFACT_KIND_PDF,
    ARTIFACT_KIND_TEXT,
    ARTIFACT_STATUS_FAILED,
    ARTIFACT_STATUS_READY,
    ArtifactContentSearchResult,
    ArtifactHotState,
    ArtifactInlinePart,
    ArtifactPolicy,
    ArtifactPromptExposure,
    ArtifactReadResult,
    ArtifactRecord,
    ArtifactRef,
    ArtifactRepresentation,
    ArtifactSearchResult,
    ArtifactTranscriberPort,
    REPRESENTATION_CHUNK_TEXT,
    REPRESENTATION_METADATA,
    REPRESENTATION_NORMALIZED_IMAGE,
    REPRESENTATION_PAGE_IMAGE,
    REPRESENTATION_PAGE_TEXT,
    REPRESENTATION_TEXT,
    REPRESENTATION_TRANSCRIPT,
)
from pal.artifact.processors import ArtifactProcessingContext, ArtifactProcessorRegistry, image_data_url
from pal.artifact.repository import ArtifactRepository
from pal.foundation import StoredArtifact


@dataclass
class NoopArtifactTranscriber:
    def transcribe(self, path: Path, *, mime_type: str = "") -> str | None:
        _ = path
        _ = mime_type
        return None


@dataclass
class ArtifactRepresentationRegistry:
    text_kinds: tuple[str, ...] = (
        REPRESENTATION_TEXT,
        REPRESENTATION_CHUNK_TEXT,
        REPRESENTATION_PAGE_TEXT,
        REPRESENTATION_TRANSCRIPT,
    )
    image_kinds: tuple[str, ...] = (
        REPRESENTATION_NORMALIZED_IMAGE,
        REPRESENTATION_PAGE_IMAGE,
    )
    auto_priority: dict[str, tuple[str, ...]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.auto_priority is None:
            self.auto_priority = {
                ARTIFACT_KIND_TEXT: (REPRESENTATION_TEXT, REPRESENTATION_CHUNK_TEXT, REPRESENTATION_METADATA),
                ARTIFACT_KIND_PDF: (REPRESENTATION_CHUNK_TEXT, REPRESENTATION_PAGE_TEXT, REPRESENTATION_PAGE_IMAGE, REPRESENTATION_METADATA),
                ARTIFACT_KIND_AUDIO: (REPRESENTATION_TRANSCRIPT, REPRESENTATION_METADATA),
                ARTIFACT_KIND_IMAGE: (REPRESENTATION_METADATA, REPRESENTATION_NORMALIZED_IMAGE),
            }

    def is_textual(self, representation_kind: str) -> bool:
        return representation_kind in self.text_kinds

    def is_image(self, representation_kind: str) -> bool:
        return representation_kind in self.image_kinds

    def auto_candidates(self, artifact_kind: str) -> tuple[str, ...]:
        return self.auto_priority.get(artifact_kind, (REPRESENTATION_METADATA,))


@dataclass
class ArtifactManager:
    runtime_root: Path
    repository: ArtifactRepository = None  # type: ignore[assignment]
    policy: ArtifactPolicy = None  # type: ignore[assignment]
    processor_registry: ArtifactProcessorRegistry = None  # type: ignore[assignment]
    representation_registry: ArtifactRepresentationRegistry = None  # type: ignore[assignment]
    transcriber: ArtifactTranscriberPort | None = None

    def __post_init__(self) -> None:
        if self.repository is None:
            self.repository = ArtifactRepository()
        if self.policy is None:
            self.policy = ArtifactPolicy()
        if self.processor_registry is None:
            self.processor_registry = ArtifactProcessorRegistry.defaults()
        if self.representation_registry is None:
            self.representation_registry = ArtifactRepresentationRegistry()
        if self.transcriber is None:
            self.transcriber = NoopArtifactTranscriber()

    def register_ingested(
        self,
        stored_or_path: Any,
        *,
        scope_key: str,
        turn_id: str,
        source_channel: str,
        metadata: dict[str, Any] | None = None,
    ) -> ArtifactRef:
        source_path, file_name, mime_type, source_metadata = _extract_ingested_source(stored_or_path)
        merged_metadata = {**source_metadata, **dict(metadata or {})}
        size = source_path.stat().st_size if source_path.is_file() else 0
        artifact_id = f"art_{uuid4().hex[:16]}"
        kind = self.processor_registry.resolve_kind(mime_type=mime_type, file_name=file_name)
        artifact_root = self._artifact_root(scope_key, artifact_id)
        original_dir = artifact_root / "original"
        original_dir.mkdir(parents=True, exist_ok=True)
        original_path = original_dir / _safe_file_name(file_name or source_path.name or "payload.bin")
        status = ARTIFACT_STATUS_READY
        notes = ""
        if not source_path.is_file():
            status = ARTIFACT_STATUS_FAILED
            notes = "source file not found"
        elif size > self.policy.limits.max_original_bytes:
            status = ARTIFACT_STATUS_FAILED
            notes = f"artifact exceeds max size {self.policy.limits.max_original_bytes}"
        else:
            shutil.copy2(source_path, original_path)
        record = ArtifactRecord(
            artifact_id=artifact_id,
            scope_key=scope_key,
            turn_id=turn_id,
            kind=kind,
            source_channel=source_channel,
            file_name=file_name or original_path.name,
            original_path=str(original_path if status != ARTIFACT_STATUS_FAILED else source_path),
            original_mime_type=mime_type or mimetypes.guess_type(file_name)[0] or "",
            original_size_bytes=size,
            summary=file_name or source_path.name,
            status=status,
            notes=notes,
            metadata=merged_metadata,
        )
        record = self.repository.upsert_record(record)
        if status != ARTIFACT_STATUS_FAILED:
            processor = self.processor_registry.processor_for(kind)
            context = ArtifactProcessingContext(
                record=record,
                root=artifact_root,
                repository=self.repository,
                policy=self.policy,
                transcriber=self.transcriber,
            )
            try:
                record = processor.process(context)
            except Exception as exc:
                record = replace(record, status=ARTIFACT_STATUS_FAILED, notes=f"{exc.__class__.__name__}: {exc}")
            record = self.repository.upsert_record(record)
        self._refresh_hot(record.artifact_id, scope_key)
        return self._ref_from_record(record)

    def list_hot(self, scope_key: str, *, query_context: str | None = None) -> tuple[ArtifactRef, ...]:
        _ = query_context
        now = _utc_now_dt()
        refs: list[ArtifactRef] = []
        for state in self.repository.list_hot_states(scope_key=scope_key):
            if _parse_dt(state.expires_at) <= now:
                continue
            record = self.repository.get_record(state.artifact_id)
            if record is not None:
                refs.append(self._ref_from_record(record))
        return tuple(refs)

    def info(self, artifact_id: str, scope_key: str) -> dict[str, Any]:
        record = self._require_visible_record(artifact_id, scope_key)
        self._refresh_hot(record.artifact_id, scope_key)
        representations = self.repository.list_representations(record.artifact_id)
        return {
            "artifact": self._record_dict(record),
            "representations": [self._representation_dict(item) for item in representations],
        }

    def read(
        self,
        artifact_id: str,
        scope_key: str,
        *,
        representation: str = "auto",
        page: int | None = None,
        chunk: int | None = None,
        max_chars: int | None = None,
    ) -> ArtifactReadResult:
        record = self._require_visible_record(artifact_id, scope_key)
        self._refresh_hot(record.artifact_id, scope_key)
        max_chars = _clamp_max_chars(max_chars, self.policy)
        selected = self._select_representation(record, representation=representation, page=page, chunk=chunk)
        if selected is None:
            return ArtifactReadResult(
                artifact_id=record.artifact_id,
                ok=False,
                kind=record.kind,
                representation=representation,
                metadata=self._record_dict(record),
                reason="representation_unavailable",
                next_actions=("Use op_artifact_info to inspect available representations.",),
            )
        if selected.representation_kind == REPRESENTATION_METADATA:
            return ArtifactReadResult(
                artifact_id=record.artifact_id,
                ok=True,
                kind=record.kind,
                representation=selected.representation_kind,
                text=selected.summary or record.summary,
                metadata={**self._record_dict(record), "representation": self._representation_dict(selected)},
                next_actions=_next_actions_for(record),
            )
        if not self.representation_registry.is_textual(selected.representation_kind):
            return ArtifactReadResult(
                artifact_id=record.artifact_id,
                ok=False,
                kind=record.kind,
                representation=selected.representation_kind,
                metadata={**self._record_dict(record), "representation": self._representation_dict(selected)},
                reason="representation_not_text_readable",
                next_actions=("Use a vision-capable model for image representations.", "Use op_artifact_info for metadata."),
            )
        text = Path(selected.path).read_text(encoding="utf-8", errors="replace") if selected.path else selected.text_preview
        truncated = len(text) > max_chars
        return ArtifactReadResult(
            artifact_id=record.artifact_id,
            ok=True,
            kind=record.kind,
            representation=selected.representation_kind,
            text=text[:max_chars],
            truncated=truncated,
            selection=dict(selected.selector),
            metadata={**self._record_dict(record), "representation": self._representation_dict(selected)},
            next_actions=_next_actions_for(record),
        )

    def artifact_search(
        self,
        scope_key: str,
        *,
        query: str = "",
        kind: str | None = None,
        time_hint: str = "recent",
        limit: int = 5,
    ) -> tuple[ArtifactSearchResult, ...]:
        _ = time_hint
        query_terms = _terms(query)
        now = _utc_now_dt()
        scored: list[ArtifactSearchResult] = []
        hot_by_artifact = {state.artifact_id: state for state in self.repository.list_hot_states(scope_key=scope_key)}
        for record in self.repository.list_records(scope_key=scope_key):
            state = hot_by_artifact.get(record.artifact_id)
            if state is None or _parse_dt(state.expires_at) <= now:
                continue
            if kind and record.kind != kind:
                continue
            haystack = " ".join(
                [
                    record.file_name,
                    record.kind,
                    record.original_mime_type,
                    record.summary,
                    str(record.metadata.get("source_text") or ""),
                    str(record.metadata.get("caption") or ""),
                ]
            ).lower()
            lexical = sum(1.0 for term in query_terms if term in haystack)
            age_seconds = max(0.0, (now - _parse_dt(state.last_accessed_at)).total_seconds())
            recency = max(0.0, 1.0 - (age_seconds / (24 * 60 * 60)))
            score = lexical + (0.1 * recency)
            if not query_terms:
                score = 0.1 * recency
            if score <= 0:
                continue
            scored.append(
                ArtifactSearchResult(
                    artifact_id=record.artifact_id,
                    score=score,
                    kind=record.kind,
                    file_name=record.file_name,
                    summary=record.summary,
                    received_at=record.created_at,
                    last_accessed_at=state.last_accessed_at,
                )
            )
        scored.sort(key=lambda item: (-item.score, item.file_name, item.artifact_id))
        return tuple(scored[: max(1, int(limit or 5))])

    def select(self, artifact_id: str, scope_key: str) -> dict[str, Any]:
        record = self._require_visible_record(artifact_id, scope_key)
        hot = self._refresh_hot(record.artifact_id, scope_key)
        return {"artifact": self._record_dict(record), "hot_state": hot.__dict__}

    def content_search(
        self,
        artifact_id: str,
        scope_key: str,
        *,
        query: str,
        top_k: int = 5,
        max_chars_per_result: int = 2_000,
    ) -> tuple[ArtifactContentSearchResult, ...]:
        record = self._require_visible_record(artifact_id, scope_key)
        self._refresh_hot(record.artifact_id, scope_key)
        terms = _terms(query)
        if not terms:
            return ()
        results: list[ArtifactContentSearchResult] = []
        max_chars = max(200, min(10_000, int(max_chars_per_result or 2_000)))
        for rep in self.repository.list_representations(record.artifact_id):
            if not self.representation_registry.is_textual(rep.representation_kind):
                continue
            text = Path(rep.path).read_text(encoding="utf-8", errors="replace") if rep.path else rep.text_preview
            lowered = text.lower()
            score = sum(1.0 for term in terms if term in lowered)
            if score <= 0:
                continue
            snippet = _snippet(text, terms, max_chars=max_chars)
            results.append(
                ArtifactContentSearchResult(
                    artifact_id=record.artifact_id,
                    representation_id=rep.representation_id,
                    representation=rep.representation_kind,
                    score=score,
                    text=snippet,
                    selector=dict(rep.selector),
                )
            )
        results.sort(key=lambda item: (-item.score, item.representation_id))
        return tuple(results[: max(1, int(top_k or 5))])

    def select_prompt_exposure(
        self,
        scope_key: str,
        turn_id: str,
        user_text: str,
        llm_capabilities: dict[str, Any] | None,
        *,
        artifact_ids: tuple[str, ...] = (),
    ) -> ArtifactPromptExposure:
        capabilities = dict(llm_capabilities or {})
        supports_vision = bool(capabilities.get("supports_vision"))
        records = self._visible_records_for_prompt(
            scope_key=scope_key,
            turn_id=turn_id,
            user_text=user_text,
            artifact_ids=tuple(artifact_ids or ()),
        )
        if not records:
            return ArtifactPromptExposure()
        inline_parts: list[ArtifactInlinePart] = []
        manifest_lines: list[str] = []
        inline_count = 0
        needs_tool_handling = False
        for record in records:
            inlined = False
            if supports_vision and inline_count < self.policy.image.max_inline_images:
                image_rep = self._first_image_representation(record)
                source_url = _resolve_source_url(record)
                if image_rep is not None and (source_url or _representation_base64_fits(image_rep, self.policy)):
                    inline_parts.append(
                        ArtifactInlinePart(
                            part_type="artifact_image",
                            artifact_id=record.artifact_id,
                            representation_id=image_rep.representation_id,
                            mime_type=image_rep.mime_type,
                            source_url=source_url,
                        )
                    )
                    inline_count += 1
                    inlined = True
                    manifest_lines.append(
                        f"- artifact_id: {record.artifact_id}\n"
                        f"  file_name: {record.file_name}\n"
                        f"  kind: {record.kind}\n"
                        f"  visual_content: attached_inline\n"
                        f"  summary: {record.summary}\n"
                        f"  use: answer from the attached image pixels directly\n"
                        f"  optional_tools: {', '.join(_prompt_actions_for(record, image_inlined=True))}"
                    )
            text_rep = self._first_short_text_representation(record)
            if text_rep is not None:
                manifest_lines.append(
                    f"- artifact_id: {record.artifact_id}\n"
                    f"  file_name: {record.file_name}\n"
                    f"  kind: {record.kind}\n"
                    f"  included_text: {text_rep.text_preview}"
                )
                continue
            if not inlined:
                manifest_lines.append(self._tool_handling_manifest(record))
                needs_tool_handling = True
        if not manifest_lines and inline_parts:
            text = "Attached artifact content is included in this user message."
        elif manifest_lines:
            handling_reminder = ""
            if needs_tool_handling:
                handling_reminder = (
                    "Some artifact content below is not directly attached or readable by this model. "
                    "Use the metadata to check the current capability/tool surface for a suitable processor. "
                    "If one exists, call it with the supported path, URL, base64 payload, or representation. "
                    "If none exists, say the current runtime cannot process that artifact content directly. "
                )
            text = (
                "These are short-lived conversation artifacts Pal can read by artifact_id. "
                "Use artifact tools only when the current user request depends on them. "
                "Do not treat artifact_id as a local path. "
                "Use local_file.preferred_path only with a tool/capability that explicitly accepts local paths. "
                "If visual_content is attached_inline, image pixels are already attached to this same user message; "
                "answer from vision directly. Do not search for or call artifact tools to inspect visual image content. "
                + handling_reminder
                + "\n"
                + "\n".join(manifest_lines)
            )
        else:
            text = ""
        return ArtifactPromptExposure(text=text, inline_parts=tuple(inline_parts))

    def to_data_url(self, representation_id: str) -> str | None:
        representation = self.repository.get_representation(str(representation_id or ""))
        if representation is None or not representation.path:
            return None
        path = Path(representation.path)
        if not path.is_file():
            return None
        return image_data_url(path, mime_type=representation.mime_type or "image/jpeg")

    def _visible_records_for_prompt(
        self,
        *,
        scope_key: str,
        turn_id: str,
        user_text: str,
        artifact_ids: tuple[str, ...] = (),
    ) -> list[ArtifactRecord]:
        now = _utc_now_dt()
        hot = {
            state.artifact_id: state
            for state in self.repository.list_hot_states(scope_key=scope_key)
            if _parse_dt(state.expires_at) > now
        }
        records = [record for record in self.repository.list_records(scope_key=scope_key) if record.artifact_id in hot]
        explicit_ids = tuple(str(item or "").strip() for item in artifact_ids if str(item or "").strip())
        if explicit_ids:
            by_id = {record.artifact_id: record for record in records}
            return [by_id[artifact_id] for artifact_id in explicit_ids if artifact_id in by_id]
        current = [record for record in records if record.turn_id == turn_id]
        if current:
            return current
        if not str(user_text or "").strip():
            return []
        if not _looks_artifact_relevant(user_text, self.policy):
            return []
        return records[: max(1, int(self.policy.exposure.max_hot_prompt_refs))]

    def _tool_handling_manifest(self, record: ArtifactRecord) -> str:
        local_file = _local_file_metadata(record)
        representations = [
            _representation_prompt_dict(item)
            for item in self.repository.list_representations(record.artifact_id)
        ]
        lines = [
            f"- artifact_id: {record.artifact_id}",
            f"  file_name: {_prompt_scalar(record.file_name)}",
            f"  kind: {record.kind}",
            f"  mime_type: {_prompt_scalar(record.original_mime_type)}",
            f"  size_bytes: {record.original_size_bytes}",
            f"  status: {record.status}",
            f"  summary: {_prompt_scalar(record.summary)}",
            "  direct_content: unavailable",
            "  handling: inspect current capabilities/tools for a suitable processor using the metadata below",
        ]
        if local_file:
            lines.extend(
                [
                    "  local_file:",
                    f"    preferred_path: {_prompt_scalar(local_file.get('preferred_path', ''))}",
                    f"    preferred_path_kind: {_prompt_scalar(local_file.get('preferred_path_kind', ''))}",
                    f"    preferred_mime_type: {_prompt_scalar(local_file.get('preferred_mime_type', ''))}",
                    f"    preferred_size_bytes: {int(local_file.get('preferred_size_bytes') or 0)}",
                ]
            )
        if representations:
            lines.append("  representations:")
            for item in representations:
                lines.extend(
                    [
                        f"    - representation_kind: {_prompt_scalar(item.get('representation_kind', ''))}",
                        f"      mime_type: {_prompt_scalar(item.get('mime_type', ''))}",
                        f"      size_bytes: {int(item.get('size_bytes') or 0)}",
                        f"      status: {_prompt_scalar(item.get('status', ''))}",
                    ]
                )
                summary = str(item.get("summary") or "").strip()
                if summary:
                    lines.append(f"      summary: {_prompt_scalar(summary)}")
                selector = item.get("selector")
                if isinstance(selector, dict) and selector:
                    lines.append(f"      selector: {_prompt_scalar(selector)}")
        return "\n".join(lines)

    def _first_image_representation(self, record: ArtifactRecord) -> ArtifactRepresentation | None:
        for kind in (REPRESENTATION_NORMALIZED_IMAGE, REPRESENTATION_PAGE_IMAGE):
            reps = self.repository.list_representations(record.artifact_id, representation_kind=kind)
            if reps:
                return reps[0]
        return None

    def _first_short_text_representation(self, record: ArtifactRecord) -> ArtifactRepresentation | None:
        if record.kind not in {ARTIFACT_KIND_TEXT, ARTIFACT_KIND_AUDIO}:
            return None
        for kind in (REPRESENTATION_TEXT, REPRESENTATION_TRANSCRIPT):
            for rep in self.repository.list_representations(record.artifact_id, representation_kind=kind):
                if rep.text_preview and len(rep.text_preview) <= self.policy.text.inline_budget_chars:
                    return rep
        return None

    def _select_representation(
        self,
        record: ArtifactRecord,
        *,
        representation: str,
        page: int | None,
        chunk: int | None,
    ) -> ArtifactRepresentation | None:
        if page is not None:
            return _find_by_selector(
                self.repository.list_representations(record.artifact_id, representation_kind=REPRESENTATION_PAGE_TEXT),
                "page",
                int(page),
            )
        if chunk is not None:
            return _find_by_selector(
                self.repository.list_representations(record.artifact_id, representation_kind=REPRESENTATION_CHUNK_TEXT),
                "chunk",
                int(chunk),
            )
        if representation and representation != "auto":
            reps = self.repository.list_representations(record.artifact_id, representation_kind=representation)
            return reps[0] if reps else None
        for candidate_kind in self.representation_registry.auto_candidates(record.kind):
            reps = self.repository.list_representations(record.artifact_id, representation_kind=candidate_kind)
            if reps:
                return reps[0]
        return None

    def _require_visible_record(self, artifact_id: str, scope_key: str) -> ArtifactRecord:
        record = self.repository.get_record(str(artifact_id or "").strip())
        if record is None or record.scope_key != scope_key:
            raise KeyError("artifact_not_found")
        state = self.repository.get_hot_state(_hot_id(scope_key, record.artifact_id))
        if state is None or _parse_dt(state.expires_at) <= _utc_now_dt():
            raise KeyError("artifact_expired")
        return record

    def _refresh_hot(self, artifact_id: str, scope_key: str) -> ArtifactHotState:
        now = _utc_now_dt()
        hot_id = _hot_id(scope_key, artifact_id)
        existing = self.repository.get_hot_state(hot_id)
        hard_expires_at = (
            _parse_dt(existing.hard_expires_at)
            if existing is not None
            else now + timedelta(seconds=self.policy.lifecycle.hard_cap_seconds)
        )
        desired = now + timedelta(seconds=self.policy.lifecycle.refresh_seconds)
        expires_at = min(desired, hard_expires_at)
        state = ArtifactHotState(
            hot_id=hot_id,
            artifact_id=artifact_id,
            scope_key=scope_key,
            last_accessed_at=now.isoformat(),
            expires_at=expires_at.isoformat(),
            hard_expires_at=hard_expires_at.isoformat(),
            access_count=(existing.access_count + 1) if existing is not None else 1,
        )
        return self.repository.upsert_hot_state(state)

    def _artifact_root(self, scope_key: str, artifact_id: str) -> Path:
        return self.runtime_root / "artifacts" / "managed" / _safe_scope(scope_key) / artifact_id

    def _ref_from_record(self, record: ArtifactRecord) -> ArtifactRef:
        reps = self.repository.list_representations(record.artifact_id)
        actions = ["info"]
        if any(self.representation_registry.is_textual(rep.representation_kind) for rep in reps):
            actions.append("read")
            actions.append("content_search")
        if record.kind == ARTIFACT_KIND_AUDIO and not any(rep.representation_kind == REPRESENTATION_TRANSCRIPT for rep in reps):
            actions.append("transcribe")
        return ArtifactRef(
            artifact_id=record.artifact_id,
            kind=record.kind,
            file_name=record.file_name,
            summary=record.summary,
            status=record.status,
            available_actions=tuple(actions),
        )

    def _record_dict(self, record: ArtifactRecord) -> dict[str, Any]:
        metadata = _sanitize_metadata_for_llm(record.metadata)
        local_file = _local_file_metadata(record)
        if local_file:
            metadata["local_file"] = local_file
        return {
            "artifact_id": record.artifact_id,
            "kind": record.kind,
            "file_name": record.file_name,
            "summary": record.summary,
            "status": record.status,
            "source_channel": record.source_channel,
            "mime_type": record.original_mime_type,
            "size_bytes": record.original_size_bytes,
            "created_at": record.created_at,
            "updated_at": record.updated_at,
            "metadata": metadata,
        }

    def _representation_dict(self, representation: ArtifactRepresentation) -> dict[str, Any]:
        return {
            "representation_id": representation.representation_id,
            "representation_kind": representation.representation_kind,
            "selector": dict(representation.selector),
            "mime_type": representation.mime_type,
            "size_bytes": representation.size_bytes,
            "summary": representation.summary,
            "status": representation.status,
            "metadata": dict(representation.metadata),
        }


def _extract_ingested_source(value: Any) -> tuple[Path, str, str, dict[str, Any]]:
    if isinstance(value, StoredArtifact):
        path = Path(value.local_cached_path)
        return path, path.name, str(value.mime_type or ""), {
            "sha256": value.sha256,
            "size_bytes": value.size_bytes,
        }
    if isinstance(value, (str, Path)):
        path = Path(value).expanduser()
        return path, path.name, mimetypes.guess_type(str(path))[0] or "", {}
    if isinstance(value, dict):
        raw_path = value.get("local_cached_path") or value.get("path")
        path = Path(str(raw_path or "")).expanduser()
        return (
            path,
            str(value.get("file_name") or path.name or "payload.bin"),
            str(value.get("mime_type") or mimetypes.guess_type(str(path))[0] or ""),
            dict(value),
        )
    path = Path(str(value or "")).expanduser()
    return path, path.name, mimetypes.guess_type(str(path))[0] or "", {}


def _clamp_max_chars(value: int | None, policy: ArtifactPolicy) -> int:
    if value is None:
        return policy.text.read_default_max_chars
    return max(1, min(int(value), policy.text.read_hard_max_chars))


def _find_by_selector(representations: tuple[ArtifactRepresentation, ...], key: str, value: int) -> ArtifactRepresentation | None:
    for rep in representations:
        try:
            if int(rep.selector.get(key)) == value:
                return rep
        except Exception:
            continue
    return None


def _representation_base64_fits(representation: ArtifactRepresentation, policy: ArtifactPolicy) -> bool:
    value = representation.metadata.get("base64_size_bytes")
    try:
        return int(value) <= policy.image.inline_base64_budget_bytes
    except Exception:
        return False


def _resolve_source_url(record: ArtifactRecord) -> str:
    source_url = str(
        (record.metadata.get("source_metadata") or {}).get("source_url") or ""
    ).strip()
    if source_url.startswith(("http://", "https://")):
        return source_url
    return ""


def _representation_prompt_dict(representation: ArtifactRepresentation) -> dict[str, Any]:
    return {
        "representation_kind": representation.representation_kind,
        "selector": dict(representation.selector),
        "mime_type": representation.mime_type,
        "size_bytes": representation.size_bytes,
        "summary": representation.summary,
        "status": representation.status,
    }


def _local_file_metadata(record: ArtifactRecord) -> dict[str, Any]:
    original = _existing_path_text(record.original_path)
    normalized = _existing_path_text(record.normalized_path)
    preferred = normalized or original
    if not preferred:
        return {}
    preferred_kind = "normalized" if normalized else "original"
    payload: dict[str, Any] = {
        "preferred_path": preferred,
        "preferred_path_kind": preferred_kind,
        "preferred_mime_type": record.normalized_mime_type if normalized else record.original_mime_type,
        "preferred_size_bytes": record.normalized_size_bytes if normalized else record.original_size_bytes,
    }
    if original:
        payload["original_path"] = original
        payload["original_mime_type"] = record.original_mime_type
        payload["original_size_bytes"] = record.original_size_bytes
    if normalized:
        payload["normalized_path"] = normalized
        payload["normalized_mime_type"] = record.normalized_mime_type
        payload["normalized_size_bytes"] = record.normalized_size_bytes
    return payload


def _existing_path_text(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_file():
        return ""
    return str(path.resolve())


_LLVM_HIDDEN_METADATA_KEYS = frozenset({"local_cached_path", "path", "telegram_file_path", "source_url"})
_LLVM_HIDDEN_NESTED_KEYS = frozenset({"source_url", "telegram_file_path"})
_URL_LIKE_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_EN_ARTIFACT_REFERENCE_RE = re.compile(
    r"\b(attachments?|artifacts?|images?|photos?|pictures?|screenshots?|pdfs?|documents?|audio|voices?)\b",
    re.IGNORECASE,
)
_EN_FILE_REFERENCE_RE = re.compile(
    r"\b(this|that|the|previous|last|above|recent|attached)\s+files?\b",
    re.IGNORECASE,
)


def _prompt_scalar(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _sanitize_metadata_for_llm(metadata: dict[str, Any]) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in metadata.items():
        if key in _LLVM_HIDDEN_METADATA_KEYS:
            continue
        if isinstance(value, dict):
            cleaned[key] = {k: v for k, v in value.items() if k not in _LLVM_HIDDEN_NESTED_KEYS}
        else:
            cleaned[key] = value
    return cleaned


def _looks_artifact_relevant(user_text: str, policy: ArtifactPolicy) -> bool:
    text = str(user_text or "").strip().lower()
    if not text:
        return True
    text_without_urls = _URL_LIKE_RE.sub(" ", text)
    if _EN_ARTIFACT_REFERENCE_RE.search(text_without_urls) or _EN_FILE_REFERENCE_RE.search(text_without_urls):
        return True
    return any(
        str(term).lower() in text
        for term in policy.exposure.relevance_terms
        if any(ord(char) > 127 for char in str(term))
    )


def _prompt_actions_for(record: ArtifactRecord, *, image_inlined: bool) -> tuple[str, ...]:
    if record.kind == ARTIFACT_KIND_IMAGE:
        if image_inlined:
            return ("info",)
        return ("info",)
    if record.kind in {ARTIFACT_KIND_TEXT, ARTIFACT_KIND_PDF}:
        return ("info", "read", "content_search")
    if record.kind == ARTIFACT_KIND_AUDIO:
        return ("info", "transcribe")
    return ("info",)


def _terms(text: str) -> list[str]:
    return [term for term in str(text or "").lower().replace("_", " ").split() if term]


def _snippet(text: str, terms: list[str], *, max_chars: int) -> str:
    lowered = text.lower()
    first = min((lowered.find(term) for term in terms if term in lowered), default=0)
    start = max(0, first - max_chars // 4)
    return text[start:start + max_chars]


def _next_actions_for(record: ArtifactRecord) -> tuple[str, ...]:
    if record.kind == ARTIFACT_KIND_PDF:
        return ("Use op_artifact_content_search for specific terms.", "Use op_artifact_read with page or chunk for focused reading.")
    if record.kind == ARTIFACT_KIND_AUDIO:
        return ("Use op_artifact_transcribe if a transcript is needed.",)
    if record.kind == ARTIFACT_KIND_IMAGE:
        return ("Use a vision-capable model for image content.",)
    return ()


def _safe_file_name(value: str) -> str:
    name = Path(str(value or "payload.bin")).name
    return "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in name) or "payload.bin"


def _safe_scope(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in str(value or "scope"))[:96] or "scope"


def _hot_id(scope_key: str, artifact_id: str) -> str:
    return f"{_safe_scope(scope_key)}:{artifact_id}"


def _utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def _parse_dt(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except Exception:
        return datetime.fromtimestamp(0, tz=timezone.utc)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
