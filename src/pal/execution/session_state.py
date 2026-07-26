from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass, field
from typing import Any, Protocol


DEFAULT_RESULT_RETENTION_USER_TURNS = 5


@dataclass(frozen=True)
class LogicalExecutionContext:
    logical_session_id: str
    input_id: str
    current_user_turn: int
    context_epoch: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "logical_session_id": self.logical_session_id,
            "input_id": self.input_id,
            "current_user_turn": self.current_user_turn,
            "context_epoch": self.context_epoch,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LogicalExecutionContext":
        return cls(
            logical_session_id=str(value.get("logical_session_id") or ""),
            input_id=str(value.get("input_id") or ""),
            current_user_turn=max(0, int(value.get("current_user_turn") or 0)),
            context_epoch=max(1, int(value.get("context_epoch") or 1)),
        )


@dataclass(frozen=True)
class FileDeliverySpan:
    start_offset: int
    end_offset: int
    start_line: int
    end_line: int
    visible_start_in_line: int = 0
    visible_end_in_line: int = 0
    line_length: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "start_offset": self.start_offset,
            "end_offset": self.end_offset,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "visible_start_in_line": self.visible_start_in_line,
            "visible_end_in_line": self.visible_end_in_line,
            "line_length": self.line_length,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FileDeliverySpan":
        start_offset = max(0, int(value.get("start_offset") or 0))
        end_offset = max(0, int(value.get("end_offset") or 0))
        line_length = max(
            0,
            int(value.get("line_length") or (end_offset - start_offset)),
        )
        return cls(
            start_offset=start_offset,
            end_offset=end_offset,
            start_line=max(0, int(value.get("start_line") or 0)),
            end_line=max(0, int(value.get("end_line") or 0)),
            visible_start_in_line=max(
                0, int(value.get("visible_start_in_line") or 0)
            ),
            visible_end_in_line=max(
                0,
                int(
                    value.get("visible_end_in_line")
                    if value.get("visible_end_in_line") is not None
                    else line_length
                ),
            ),
            line_length=line_length,
        )


@dataclass(frozen=True)
class FileDeliveryManifest:
    file_key: str
    digest: str
    total_lines: int
    spans: tuple[FileDeliverySpan, ...] = ()
    empty_file: bool = False
    empty_marker: str = "(empty file)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "file_lines",
            "file_key": self.file_key,
            "digest": self.digest,
            "total_lines": self.total_lines,
            "spans": [span.to_dict() for span in self.spans],
            "empty_file": self.empty_file,
            "empty_marker": self.empty_marker,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None) -> "FileDeliveryManifest | None":
        payload = dict(value or {})
        if payload.get("kind") != "file_lines":
            return None
        return cls(
            file_key=str(payload.get("file_key") or ""),
            digest=str(payload.get("digest") or ""),
            total_lines=max(0, int(payload.get("total_lines") or 0)),
            spans=tuple(
                FileDeliverySpan.from_dict(dict(item))
                for item in list(payload.get("spans") or ())
                if isinstance(item, dict)
            ),
            empty_file=bool(payload.get("empty_file")),
            empty_marker=str(payload.get("empty_marker") or "(empty file)"),
        )

    def slice(self, start_offset: int, end_offset: int) -> "FileDeliveryManifest | None":
        selected: list[FileDeliverySpan] = []
        for span in self.spans:
            visible_start = max(span.start_offset, start_offset)
            visible_end = min(span.end_offset, end_offset)
            if visible_end <= visible_start:
                continue
            selected.append(
                FileDeliverySpan(
                    start_offset=visible_start - start_offset,
                    end_offset=visible_end - start_offset,
                    start_line=span.start_line,
                    end_line=span.end_line,
                    visible_start_in_line=(
                        span.visible_start_in_line
                        + visible_start
                        - span.start_offset
                    ),
                    visible_end_in_line=(
                        span.visible_start_in_line
                        + visible_end
                        - span.start_offset
                    ),
                    line_length=span.line_length,
                )
            )
        empty_visible = self.empty_file and start_offset == 0
        if not selected and not empty_visible:
            return None
        return FileDeliveryManifest(
            file_key=self.file_key,
            digest=self.digest,
            total_lines=self.total_lines,
            spans=tuple(selected),
            empty_file=empty_visible,
            empty_marker=self.empty_marker,
        )


@dataclass(frozen=True)
class PagerHandleManifest:
    result_ref: str
    logical_session_id: str
    tool_name: str
    status: str
    ok: bool
    page_size: int
    original_size: int
    page_count: int
    created_user_turn: int
    expires_at_user_turn: int
    output_json: str
    rendered: str
    origin: dict[str, Any] = field(default_factory=dict)
    delivery_manifest: dict[str, Any] = field(default_factory=dict)
    backing_path: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "result_ref": self.result_ref,
            "page_size": self.page_size,
            "original_size": self.original_size,
            "page_count": self.page_count,
            "created_user_turn": self.created_user_turn,
            "expires_at_user_turn": self.expires_at_user_turn,
        }

    def to_dict(self, *, include_payload: bool = True) -> dict[str, Any]:
        payload = {
            **self.public_dict(),
            "logical_session_id": self.logical_session_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "ok": self.ok,
            "origin": dict(self.origin),
            "delivery_manifest": dict(self.delivery_manifest),
            "backing_path": self.backing_path,
        }
        if include_payload:
            payload["output_json"] = self.output_json
            payload["rendered"] = self.rendered
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PagerHandleManifest":
        return cls(
            result_ref=str(value.get("result_ref") or ""),
            logical_session_id=str(value.get("logical_session_id") or ""),
            tool_name=str(value.get("tool_name") or ""),
            status=str(value.get("status") or ""),
            ok=bool(value.get("ok", True)),
            page_size=max(256, int(value.get("page_size") or 256)),
            original_size=max(0, int(value.get("original_size") or 0)),
            page_count=max(1, int(value.get("page_count") or 1)),
            created_user_turn=max(0, int(value.get("created_user_turn") or 0)),
            expires_at_user_turn=max(0, int(value.get("expires_at_user_turn") or 0)),
            output_json=str(value.get("output_json") or ""),
            rendered=str(value.get("rendered") or ""),
            origin=dict(value.get("origin") or {}),
            delivery_manifest=dict(value.get("delivery_manifest") or {}),
            backing_path=str(value.get("backing_path") or ""),
        )


@dataclass(frozen=True)
class PagerRead:
    state: str
    manifest: PagerHandleManifest | None = None
    content: str = ""
    page: int = 1
    page_count: int = 1
    page_size: int = 0
    anchor: str = "head"
    anchor_page: int = 1
    start_offset: int = 0
    end_offset: int = 0
    delivery_manifest: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FileGrant:
    file_key: str
    digest: str
    total_lines: int
    covered_ranges: tuple[tuple[int, int], ...]
    empty_file: bool = False
    line_fragments: tuple[tuple[int, int, int, int], ...] = ()

    @property
    def complete(self) -> bool:
        if self.total_lines == 0:
            return self.empty_file
        return any(start <= 1 and end >= self.total_lines for start, end in self.covered_ranges)


class LogicalExecutionStateBackend(Protocol):
    def begin_input(
        self,
        *,
        logical_session_id: str,
        input_id: str,
        retention_user_turns: int = DEFAULT_RESULT_RETENTION_USER_TURNS,
    ) -> LogicalExecutionContext:
        ...

    def context(self, logical_session_id: str) -> LogicalExecutionContext:
        ...

    def reconcile_projection(
        self,
        *,
        logical_session_id: str,
        projection: tuple[str, ...],
        deliveries: tuple[dict[str, Any], ...],
    ) -> LogicalExecutionContext:
        ...

    def store_pager(self, manifest: PagerHandleManifest) -> PagerHandleManifest:
        ...

    def read_pager(
        self,
        *,
        logical_session_id: str,
        result_ref: str,
        page: int,
        page_size: int | None,
        anchor: str,
    ) -> PagerRead:
        ...

    def file_grant(
        self,
        *,
        logical_session_id: str,
        file_key: str,
        digest: str,
    ) -> FileGrant | None:
        ...

    def set_file_full(
        self,
        *,
        logical_session_id: str,
        file_key: str,
        digest: str,
        total_lines: int,
    ) -> None:
        ...

    def invalidate_file(self, *, logical_session_id: str, file_key: str) -> None:
        ...

    def retire_session(self, logical_session_id: str) -> None:
        ...


@dataclass
class _SessionState:
    current_user_turn: int = 0
    context_epoch: int = 1
    input_ids: dict[str, int] = field(default_factory=dict)
    retention_user_turns: int = DEFAULT_RESULT_RETENTION_USER_TURNS
    projection: tuple[str, ...] = ()
    handles: dict[str, PagerHandleManifest] = field(default_factory=dict)
    grants: dict[tuple[int, str], FileGrant] = field(default_factory=dict)
    retired: bool = False


class InMemoryLogicalExecutionState:
    """Reference implementation used by the host runtime and focused tests."""

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionState] = {}
        self._lock = threading.RLock()

    def begin_input(
        self,
        *,
        logical_session_id: str,
        input_id: str,
        retention_user_turns: int = DEFAULT_RESULT_RETENTION_USER_TURNS,
    ) -> LogicalExecutionContext:
        session_id = _required(logical_session_id, "logical_session_id")
        semantic_input = _required(input_id, "input_id")
        with self._lock:
            state = self._sessions.setdefault(session_id, _SessionState())
            if state.retired:
                raise RuntimeError("logical execution session is retired")
            state.retention_user_turns = max(1, int(retention_user_turns))
            if semantic_input not in state.input_ids:
                state.current_user_turn += 1
                state.input_ids[semantic_input] = state.current_user_turn
            self._expire_handles(state)
            return self._context(session_id, semantic_input, state)

    def context(self, logical_session_id: str) -> LogicalExecutionContext:
        session_id = _required(logical_session_id, "logical_session_id")
        with self._lock:
            state = self._sessions.setdefault(session_id, _SessionState())
            return self._context(session_id, "", state)

    def reconcile_projection(
        self,
        *,
        logical_session_id: str,
        projection: tuple[str, ...],
        deliveries: tuple[dict[str, Any], ...],
    ) -> LogicalExecutionContext:
        session_id = _required(logical_session_id, "logical_session_id")
        normalized_projection = tuple(str(item) for item in projection)
        with self._lock:
            state = self._sessions.setdefault(session_id, _SessionState())
            if state.retired:
                raise RuntimeError("logical execution session is retired")
            previous = state.projection
            append_only = len(normalized_projection) >= len(previous) and normalized_projection[: len(previous)] == previous
            if previous and not append_only:
                state.context_epoch += 1
                state.grants = {
                    key: grant
                    for key, grant in state.grants.items()
                    if key[0] == state.context_epoch
                }
            state.projection = normalized_projection
            for delivery in deliveries:
                self._apply_delivery(state, dict(delivery))
            return self._context(session_id, "", state)

    def store_pager(self, manifest: PagerHandleManifest) -> PagerHandleManifest:
        with self._lock:
            state = self._sessions.setdefault(manifest.logical_session_id, _SessionState())
            if state.retired:
                raise RuntimeError("logical execution session is retired")
            existing = state.handles.get(manifest.result_ref)
            if existing is not None:
                old_hash = hashlib.sha256(
                    (existing.output_json + "\0" + existing.rendered).encode("utf-8")
                ).hexdigest()
                new_hash = hashlib.sha256(
                    (manifest.output_json + "\0" + manifest.rendered).encode("utf-8")
                ).hexdigest()
                if old_hash != new_hash:
                    raise ValueError("result_ref was reused with different output")
                return existing
            state.handles[manifest.result_ref] = manifest
            return manifest

    def read_pager(
        self,
        *,
        logical_session_id: str,
        result_ref: str,
        page: int,
        page_size: int | None,
        anchor: str,
    ) -> PagerRead:
        with self._lock:
            state = self._sessions.get(logical_session_id)
            if state is None:
                return PagerRead(state="unknown_handle")
            manifest = state.handles.get(str(result_ref))
            if manifest is None:
                return PagerRead(state="unknown_handle")
            if state.retired or state.current_user_turn >= manifest.expires_at_user_turn:
                return PagerRead(state="expired_handle", manifest=manifest)
            size = max(256, int(page_size or manifest.page_size))
            text = manifest.rendered
            page_count = max(1, math.ceil(len(text) / size))
            anchor_value = "tail" if str(anchor).lower() == "tail" else "head"
            anchor_page = max(1, int(page or 1))
            absolute_page = page_count - anchor_page + 1 if anchor_value == "tail" else anchor_page
            if absolute_page < 1 or absolute_page > page_count:
                return PagerRead(
                    state="page_out_of_range",
                    manifest=manifest,
                    page=absolute_page,
                    page_count=page_count,
                    page_size=size,
                    anchor=anchor_value,
                    anchor_page=anchor_page,
                )
            start = (absolute_page - 1) * size
            end = min(start + size, len(text))
            delivery = FileDeliveryManifest.from_dict(manifest.delivery_manifest)
            sliced = delivery.slice(start, end) if delivery is not None else None
            return PagerRead(
                state="ok",
                manifest=manifest,
                content=text[start:end],
                page=absolute_page,
                page_count=page_count,
                page_size=size,
                anchor=anchor_value,
                anchor_page=anchor_page,
                start_offset=start,
                end_offset=end,
                delivery_manifest=sliced.to_dict() if sliced is not None else {},
            )

    def file_grant(
        self,
        *,
        logical_session_id: str,
        file_key: str,
        digest: str,
    ) -> FileGrant | None:
        with self._lock:
            state = self._sessions.get(logical_session_id)
            if state is None or state.retired:
                return None
            grant = state.grants.get((state.context_epoch, str(file_key)))
            if grant is None or (str(digest) and grant.digest != str(digest)):
                return None
            return grant

    def set_file_full(
        self,
        *,
        logical_session_id: str,
        file_key: str,
        digest: str,
        total_lines: int,
    ) -> None:
        with self._lock:
            state = self._sessions.setdefault(logical_session_id, _SessionState())
            total = max(0, int(total_lines))
            state.grants[(state.context_epoch, str(file_key))] = FileGrant(
                file_key=str(file_key),
                digest=str(digest),
                total_lines=total,
                covered_ranges=((1, total),) if total else (),
                empty_file=total == 0,
            )

    def invalidate_file(self, *, logical_session_id: str, file_key: str) -> None:
        with self._lock:
            state = self._sessions.get(logical_session_id)
            if state is None:
                return
            for key in [candidate for candidate in state.grants if candidate[1] == str(file_key)]:
                state.grants.pop(key, None)

    def retire_session(self, logical_session_id: str) -> None:
        with self._lock:
            state = self._sessions.setdefault(str(logical_session_id), _SessionState())
            state.retired = True

    @staticmethod
    def _context(session_id: str, input_id: str, state: _SessionState) -> LogicalExecutionContext:
        return LogicalExecutionContext(
            logical_session_id=session_id,
            input_id=input_id,
            current_user_turn=state.current_user_turn,
            context_epoch=state.context_epoch,
        )

    @staticmethod
    def _expire_handles(state: _SessionState) -> None:
        # Keep manifests so callers can distinguish an expired handle from an
        # unknown or cross-session reference and produce a safe affordance.
        _ = state

    @staticmethod
    def _apply_delivery(state: _SessionState, delivery: dict[str, Any]) -> None:
        manifest = FileDeliveryManifest.from_dict(delivery)
        if manifest is None or not manifest.file_key or not manifest.digest:
            return
        key = (state.context_epoch, manifest.file_key)
        previous = state.grants.get(key)
        ranges = (
            list(previous.covered_ranges)
            if previous is not None and previous.digest == manifest.digest
            else []
        )
        ranges.extend(
            (span.start_line, span.end_line)
            for span in manifest.spans
            if (
                span.start_line > 0
                and span.end_line >= span.start_line
                and span.visible_start_in_line <= 0
                and span.visible_end_in_line >= span.line_length
            )
        )
        fragments = (
            list(previous.line_fragments)
            if previous is not None and previous.digest == manifest.digest
            else []
        )
        fragments.extend(
            (
                span.start_line,
                span.visible_start_in_line,
                span.visible_end_in_line,
                span.line_length,
            )
            for span in manifest.spans
            if span.start_line == span.end_line and span.line_length > 0
        )
        merged_fragments, completed_lines = _merge_line_fragments(fragments)
        ranges.extend((line, line) for line in completed_lines)
        state.grants[key] = FileGrant(
            file_key=manifest.file_key,
            digest=manifest.digest,
            total_lines=manifest.total_lines,
            covered_ranges=_merge_ranges(ranges),
            empty_file=manifest.empty_file or bool(previous and previous.empty_file),
            line_fragments=merged_fragments,
        )


def page_count_for(rendered: str, page_size: int) -> int:
    return max(1, math.ceil(len(str(rendered or "")) / max(256, int(page_size))))


def content_digest(content: str) -> str:
    return hashlib.sha256(str(content).encode("utf-8")).hexdigest()


def count_text_lines(content: str) -> int:
    return len(str(content).splitlines(keepends=True))


def projection_hash(call_id: str, content: str) -> str:
    return hashlib.sha256(
        json.dumps(
            {"call_id": str(call_id), "content": str(content)},
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _merge_ranges(ranges: list[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    normalized = sorted(
        (max(1, int(start)), max(1, int(end)))
        for start, end in ranges
        if int(end) >= int(start)
    )
    merged: list[tuple[int, int]] = []
    for start, end in normalized:
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)


def _merge_line_fragments(
    fragments: list[tuple[int, int, int, int]],
) -> tuple[tuple[tuple[int, int, int, int], ...], tuple[int, ...]]:
    grouped: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for line, start, end, length in fragments:
        if line <= 0 or length <= 0 or end <= start:
            continue
        grouped.setdefault((line, length), []).append(
            (max(0, start), min(length, end))
        )
    merged_output: list[tuple[int, int, int, int]] = []
    completed: list[int] = []
    for (line, length), ranges in sorted(grouped.items()):
        merged = _merge_zero_based_ranges(ranges)
        merged_output.extend((line, start, end, length) for start, end in merged)
        if merged and merged[0][0] == 0 and merged[0][1] >= length:
            completed.append(line)
    return tuple(merged_output), tuple(completed)


def _merge_zero_based_ranges(
    ranges: list[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    normalized = sorted(
        (max(0, int(start)), max(0, int(end)))
        for start, end in ranges
        if int(end) > int(start)
    )
    merged: list[tuple[int, int]] = []
    for start, end in normalized:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)


def _required(value: str, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized
