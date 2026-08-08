from __future__ import annotations

import hashlib
import json
import math
import threading
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Protocol


DEFAULT_RESULT_RETENTION_USER_TURNS = 5


@dataclass(frozen=True)
class LogicalExecutionContext:
    execution_lifetime_id: str
    input_id: str
    current_user_turn: int
    context_epoch: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_lifetime_id": self.execution_lifetime_id,
            "input_id": self.input_id,
            "current_user_turn": self.current_user_turn,
            "context_epoch": self.context_epoch,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LogicalExecutionContext":
        return cls(
            execution_lifetime_id=str(value.get("execution_lifetime_id") or ""),
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
    def from_dict(cls, value: Mapping[str, Any]) -> "FileDeliverySpan":
        start_offset = max(0, int(value.get("start_offset") or 0))
        end_offset = max(0, int(value.get("end_offset") or 0))
        line_length = max(
            0,
            int(value.get("line_length") or (end_offset - start_offset)),
        )
        raw_visible_end = value.get("visible_end_in_line")
        visible_end = line_length if raw_visible_end is None else int(raw_visible_end)
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
                visible_end,
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
    replay_result_ref: str = ""
    operation: str = "read"
    before_digest: str = ""
    inherited_ranges: tuple[tuple[int, int], ...] = ()
    parent_result_ids: tuple[str, ...] = ()
    complete_file: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": "file_lines",
            "file_key": self.file_key,
            "digest": self.digest,
            "total_lines": self.total_lines,
            "spans": [span.to_dict() for span in self.spans],
            "empty_file": self.empty_file,
            "empty_marker": self.empty_marker,
            "replay_result_ref": self.replay_result_ref,
            "operation": self.operation,
            "before_digest": self.before_digest,
            "inherited_ranges": [list(item) for item in self.inherited_ranges],
            "parent_result_ids": list(self.parent_result_ids),
            "complete_file": self.complete_file,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "FileDeliveryManifest | None":
        payload = dict(value or {})
        if payload.get("kind") != "file_lines":
            return None
        return cls(
            file_key=str(payload.get("file_key") or ""),
            digest=str(payload.get("digest") or ""),
            total_lines=max(0, int(payload.get("total_lines") or 0)),
            spans=tuple(
                FileDeliverySpan.from_dict(item)
                for item in list(payload.get("spans") or ())
                if isinstance(item, Mapping)
            ),
            empty_file=bool(payload.get("empty_file")),
            empty_marker=str(payload.get("empty_marker") or "(empty file)"),
            replay_result_ref=str(payload.get("replay_result_ref") or ""),
            operation=str(payload.get("operation") or "read"),
            before_digest=str(payload.get("before_digest") or ""),
            inherited_ranges=tuple(
                (int(item[0]), int(item[1]))
                for item in list(payload.get("inherited_ranges") or ())
                if isinstance(item, (list, tuple)) and len(item) == 2
            ),
            parent_result_ids=tuple(
                str(item)
                for item in list(payload.get("parent_result_ids") or ())
                if str(item)
            ),
            complete_file=bool(payload.get("complete_file")),
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
        proof_end = max((span.end_offset for span in self.spans), default=0)
        full_proof_visible = start_offset == 0 and (
            proof_end == 0 or end_offset >= proof_end
        )
        empty_visible = self.empty_file and full_proof_visible
        complete_visible = self.complete_file and full_proof_visible
        inherited_visible = self.inherited_ranges if full_proof_visible else ()
        if not selected and not empty_visible and not complete_visible and not inherited_visible:
            return None
        return FileDeliveryManifest(
            file_key=self.file_key,
            digest=self.digest,
            total_lines=self.total_lines,
            spans=tuple(selected),
            empty_file=empty_visible,
            empty_marker=self.empty_marker,
            replay_result_ref=self.replay_result_ref,
            operation=self.operation,
            before_digest=self.before_digest,
            inherited_ranges=inherited_visible,
            parent_result_ids=(
                self.parent_result_ids if inherited_visible else ()
            ),
            complete_file=complete_visible,
        )


@dataclass(frozen=True)
class PagerHandleManifest:
    result_ref: str
    execution_lifetime_id: str
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
            "execution_lifetime_id": self.execution_lifetime_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "ok": self.ok,
            "origin": dict(self.origin),
            "delivery_manifest": dict(self.delivery_manifest),
        }
        if include_payload:
            payload["output_json"] = self.output_json
            payload["rendered"] = self.rendered
        return payload

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "PagerHandleManifest":
        return cls(
            result_ref=str(value.get("result_ref") or ""),
            execution_lifetime_id=str(value.get("execution_lifetime_id") or ""),
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
    result_ids: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        if self.total_lines == 0:
            return self.empty_file
        return any(start <= 1 and end >= self.total_lines for start, end in self.covered_ranges)

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_key": self.file_key,
            "digest": self.digest,
            "total_lines": self.total_lines,
            "covered_ranges": [list(item) for item in self.covered_ranges],
            "empty_file": self.empty_file,
            "line_fragments": [list(item) for item in self.line_fragments],
            "result_ids": list(self.result_ids),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FileGrant":
        return cls(
            file_key=str(value.get("file_key") or ""),
            digest=str(value.get("digest") or ""),
            total_lines=max(0, int(value.get("total_lines") or 0)),
            covered_ranges=tuple(
                (int(item[0]), int(item[1]))
                for item in list(value.get("covered_ranges") or ())
                if isinstance(item, (list, tuple)) and len(item) == 2
            ),
            empty_file=bool(value.get("empty_file")),
            line_fragments=tuple(
                (int(item[0]), int(item[1]), int(item[2]), int(item[3]))
                for item in list(value.get("line_fragments") or ())
                if isinstance(item, (list, tuple)) and len(item) == 4
            ),
            result_ids=tuple(
                str(item)
                for item in list(value.get("result_ids") or ())
                if str(item)
            ),
        )


@dataclass(frozen=True)
class FileSnapshot:
    file_key: str
    digest: str
    total_lines: int
    complete: bool
    created_user_turn: int
    expires_at_user_turn: int
    source: str = "delivery"
    replay_result_ref: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_key": self.file_key,
            "digest": self.digest,
            "total_lines": self.total_lines,
            "complete": self.complete,
            "created_user_turn": self.created_user_turn,
            "expires_at_user_turn": self.expires_at_user_turn,
            "source": self.source,
            "replay_result_ref": self.replay_result_ref,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FileSnapshot":
        return cls(
            file_key=str(value.get("file_key") or ""),
            digest=str(value.get("digest") or ""),
            total_lines=max(0, int(value.get("total_lines") or 0)),
            complete=bool(value.get("complete")),
            created_user_turn=max(0, int(value.get("created_user_turn") or 0)),
            expires_at_user_turn=max(0, int(value.get("expires_at_user_turn") or 0)),
            source=(
                "mutation"
                if str(value.get("source") or "") == "mutation"
                else "delivery"
            ),
            replay_result_ref=str(value.get("replay_result_ref") or ""),
        )


@dataclass(frozen=True)
class FileResultLease:
    """One tool result's RAII contribution to file mutation authority."""

    result_id: str
    file_key: str
    digest: str
    total_lines: int
    standalone_ranges: tuple[tuple[int, int], ...]
    inherited_ranges: tuple[tuple[int, int], ...] = ()
    parent_result_ids: tuple[str, ...] = ()
    empty_file: bool = False
    complete_file: bool = False
    operation: str = "read"
    before_digest: str = ""
    created_user_turn: int = 0
    expires_at_user_turn: int = 0
    projected: bool = True
    replay_result_ref: str = ""
    line_fragments: tuple[tuple[int, int, int, int], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "result_id": self.result_id,
            "file_key": self.file_key,
            "digest": self.digest,
            "total_lines": self.total_lines,
            "standalone_ranges": [list(item) for item in self.standalone_ranges],
            "inherited_ranges": [list(item) for item in self.inherited_ranges],
            "parent_result_ids": list(self.parent_result_ids),
            "empty_file": self.empty_file,
            "complete_file": self.complete_file,
            "operation": self.operation,
            "before_digest": self.before_digest,
            "created_user_turn": self.created_user_turn,
            "expires_at_user_turn": self.expires_at_user_turn,
            "projected": self.projected,
            "replay_result_ref": self.replay_result_ref,
            "line_fragments": [list(item) for item in self.line_fragments],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "FileResultLease":
        def ranges(name: str) -> tuple[tuple[int, int], ...]:
            return tuple(
                (int(item[0]), int(item[1]))
                for item in list(value.get(name) or ())
                if isinstance(item, (list, tuple)) and len(item) == 2
            )

        return cls(
            result_id=str(value.get("result_id") or ""),
            file_key=str(value.get("file_key") or ""),
            digest=str(value.get("digest") or ""),
            total_lines=max(0, int(value.get("total_lines") or 0)),
            standalone_ranges=ranges("standalone_ranges"),
            inherited_ranges=ranges("inherited_ranges"),
            parent_result_ids=tuple(
                str(item)
                for item in list(value.get("parent_result_ids") or ())
                if str(item)
            ),
            empty_file=bool(value.get("empty_file")),
            complete_file=bool(value.get("complete_file")),
            operation=str(value.get("operation") or "read"),
            before_digest=str(value.get("before_digest") or ""),
            created_user_turn=max(0, int(value.get("created_user_turn") or 0)),
            expires_at_user_turn=max(0, int(value.get("expires_at_user_turn") or 0)),
            projected=bool(value.get("projected", True)),
            replay_result_ref=str(value.get("replay_result_ref") or ""),
            line_fragments=tuple(
                (int(item[0]), int(item[1]), int(item[2]), int(item[3]))
                for item in list(value.get("line_fragments") or ())
                if isinstance(item, (list, tuple)) and len(item) == 4
            ),
        )


class LogicalExecutionStateBackend(Protocol):
    def begin_input(
        self,
        *,
        execution_lifetime_id: str,
        input_id: str,
        retention_user_turns: int = DEFAULT_RESULT_RETENTION_USER_TURNS,
    ) -> LogicalExecutionContext:
        ...

    def context(self, execution_lifetime_id: str) -> LogicalExecutionContext:
        ...

    def reconcile_projection(
        self,
        *,
        execution_lifetime_id: str,
        projection: tuple[str, ...],
        deliveries: tuple[dict[str, Any], ...],
    ) -> LogicalExecutionContext:
        ...

    def record_delivery(
        self,
        *,
        execution_lifetime_id: str,
        delivery: dict[str, Any],
    ) -> LogicalExecutionContext:
        """Commit one tool-result delivery without changing the projection."""
        ...

    def store_pager(self, manifest: PagerHandleManifest) -> PagerHandleManifest:
        ...

    def read_pager(
        self,
        *,
        execution_lifetime_id: str,
        result_ref: str,
        page: int,
        page_size: int | None,
        anchor: str,
    ) -> PagerRead:
        ...

    def pager_lifetime(self, result_ref: str) -> str | None:
        ...

    def file_grant(
        self,
        *,
        execution_lifetime_id: str,
        file_key: str,
        digest: str,
    ) -> FileGrant | None:
        ...

    def file_snapshot(
        self,
        *,
        execution_lifetime_id: str,
        file_key: str,
        digest: str,
    ) -> FileSnapshot | None:
        ...

    def set_file_snapshot(
        self,
        *,
        execution_lifetime_id: str,
        file_key: str,
        digest: str,
        total_lines: int,
        complete: bool,
        source: str = "mutation",
    ) -> None:
        ...

    def invalidate_file(self, *, execution_lifetime_id: str, file_key: str) -> None:
        ...

    def retire_session(self, execution_lifetime_id: str) -> None:
        ...

    def retire_results(
        self,
        *,
        execution_lifetime_id: str,
        result_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        ...


@dataclass
class _SessionState:
    current_user_turn: int = 0
    context_epoch: int = 1
    input_ids: dict[str, int] = field(default_factory=dict)
    retention_user_turns: int = DEFAULT_RESULT_RETENTION_USER_TURNS
    projection: tuple[str, ...] = ()
    handles: dict[str, PagerHandleManifest] = field(default_factory=dict)
    expired_handles: dict[str, PagerHandleManifest] = field(default_factory=dict)
    snapshots: dict[str, FileSnapshot] = field(default_factory=dict)
    file_results: dict[str, FileResultLease] = field(default_factory=dict)
    retired: bool = False


class InMemoryLogicalExecutionState:
    """Reference implementation used by the host runtime and focused tests."""

    def __init__(self) -> None:
        self._sessions: dict[str, _SessionState] = {}
        self._lock = threading.RLock()

    def begin_input(
        self,
        *,
        execution_lifetime_id: str,
        input_id: str,
        retention_user_turns: int = DEFAULT_RESULT_RETENTION_USER_TURNS,
    ) -> LogicalExecutionContext:
        session_id = _required(execution_lifetime_id, "execution_lifetime_id")
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
            self._expire_file_results(state)
            return self._context(session_id, semantic_input, state)

    def context(self, execution_lifetime_id: str) -> LogicalExecutionContext:
        session_id = _required(execution_lifetime_id, "execution_lifetime_id")
        with self._lock:
            state = self._sessions.setdefault(session_id, _SessionState())
            return self._context(session_id, "", state)

    def reconcile_projection(
        self,
        *,
        execution_lifetime_id: str,
        projection: tuple[str, ...],
        deliveries: tuple[dict[str, Any], ...],
    ) -> LogicalExecutionContext:
        session_id = _required(execution_lifetime_id, "execution_lifetime_id")
        normalized_projection = tuple(str(item) for item in projection)
        with self._lock:
            state = self._sessions.setdefault(session_id, _SessionState())
            if state.retired:
                raise RuntimeError("logical execution session is retired")
            self._expire_file_results(state)
            previous = state.projection
            if normalized_projection != previous:
                state.context_epoch += 1
            state.projection = normalized_projection
            state.file_results = {
                result_id: replace(lease, projected=False)
                for result_id, lease in state.file_results.items()
                if result_id in normalized_projection
            }
            applied_result_ids: set[str] = set()
            for delivery_index, delivery in enumerate(deliveries):
                normalized_delivery = dict(delivery)
                if not (
                    normalized_delivery.get("result_id")
                    or normalized_delivery.get("_result_id")
                ):
                    parsed_delivery = FileDeliveryManifest.from_dict(
                        normalized_delivery
                    )
                    replay_owner = str(
                        getattr(parsed_delivery, "replay_result_ref", "") or ""
                    )
                    if replay_owner in normalized_projection:
                        normalized_delivery["result_id"] = replay_owner
                    elif delivery_index < len(normalized_projection):
                        # Compatibility for callers predating explicit result
                        # ownership: deliveries are positional to projection.
                        normalized_delivery["result_id"] = normalized_projection[
                            delivery_index
                        ]
                delivery_result_id = _delivery_result_id(normalized_delivery)
                if delivery_result_id not in normalized_projection:
                    continue
                self._apply_delivery(
                    state,
                    normalized_delivery,
                    replace_existing=delivery_result_id not in applied_result_ids,
                )
                applied_result_ids.add(delivery_result_id)
            # A projection may retain a result whose immutable evidence was
            # committed earlier.  Re-activate only the exact owner IDs named
            # by the current projection; previews omit their delivery and
            # therefore remain inactive.
            delivered_ids = applied_result_ids
            for result_id in normalized_projection:
                if result_id in delivered_ids and result_id in state.file_results:
                    state.file_results[result_id] = replace(
                        state.file_results[result_id],
                        projected=True,
                    )
            return self._context(session_id, "", state)

    def record_delivery(
        self,
        *,
        execution_lifetime_id: str,
        delivery: dict[str, Any],
    ) -> LogicalExecutionContext:
        """Commit a just-delivered result while preserving projection state."""

        session_id = _required(execution_lifetime_id, "execution_lifetime_id")
        with self._lock:
            state = self._sessions.setdefault(session_id, _SessionState())
            if state.retired:
                raise RuntimeError("logical execution session is retired")
            self._expire_file_results(state)
            self._apply_delivery(state, dict(delivery), replace_existing=False)
            return self._context(session_id, "", state)

    def store_pager(self, manifest: PagerHandleManifest) -> PagerHandleManifest:
        with self._lock:
            state = self._sessions.setdefault(manifest.execution_lifetime_id, _SessionState())
            if state.retired:
                raise RuntimeError("logical execution session is retired")
            if manifest.result_ref in state.expired_handles:
                raise ValueError("result_ref belongs to a retired pager handle")
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
        execution_lifetime_id: str,
        result_ref: str,
        page: int,
        page_size: int | None,
        anchor: str,
    ) -> PagerRead:
        with self._lock:
            state = self._sessions.get(execution_lifetime_id)
            if state is None:
                return PagerRead(state="unknown_handle")
            self._expire_handles(state)
            manifest = state.handles.get(str(result_ref))
            if manifest is None:
                expired = state.expired_handles.get(str(result_ref))
                if expired is not None:
                    return PagerRead(state="expired_handle", manifest=expired)
                return PagerRead(state="unknown_handle")
            if state.retired:
                return PagerRead(state="expired_handle")
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

    def pager_lifetime(self, result_ref: str) -> str | None:
        """Resolve an exact globally unambiguous handle for direct callers."""

        normalized = str(result_ref or "").strip()
        if not normalized:
            return None
        with self._lock:
            matches = [
                execution_lifetime_id
                for execution_lifetime_id, state in self._sessions.items()
                if normalized in state.handles or normalized in state.expired_handles
            ]
        return matches[0] if len(matches) == 1 else None

    def file_grant(
        self,
        *,
        execution_lifetime_id: str,
        file_key: str,
        digest: str,
    ) -> FileGrant | None:
        with self._lock:
            state = self._sessions.get(execution_lifetime_id)
            if state is None or state.retired:
                return None
            self._expire_file_results(state)
            active = {
                result_id: lease
                for result_id, lease in state.file_results.items()
                if lease.projected
            }
            candidates = [
                lease
                for lease in active.values()
                if lease.file_key == str(file_key)
                and (not str(digest) or lease.digest == str(digest))
            ]
            if not candidates:
                return None
            selected_digest = str(digest) or max(
                candidates,
                key=lambda lease: (lease.created_user_turn, lease.result_id),
            ).digest
            selected = [lease for lease in candidates if lease.digest == selected_digest]
            ranges: list[tuple[int, int]] = []
            fragments: list[tuple[int, int, int, int]] = []
            result_ids: set[str] = set()
            empty_file = False
            total_lines = max((lease.total_lines for lease in selected), default=0)
            for lease in selected:
                ranges.extend(lease.standalone_ranges)
                fragments.extend(lease.line_fragments)
                result_ids.add(lease.result_id)
                empty_file = empty_file or lease.empty_file
                if lease.complete_file and lease.total_lines > 0:
                    ranges.append((1, lease.total_lines))
                parent_leases = tuple(
                    active.get(parent) for parent in lease.parent_result_ids
                )
                if lease.parent_result_ids and all(
                    parent is not None
                    and parent.file_key == lease.file_key
                    for parent in parent_leases
                ) and any(
                    parent is not None and parent.digest == lease.before_digest
                    for parent in parent_leases
                ):
                    ranges.extend(lease.inherited_ranges)
                    result_ids.update(lease.parent_result_ids)
            merged_fragments, completed_lines = _merge_line_fragments(fragments)
            ranges.extend((line, line) for line in completed_lines)
            merged = _merge_ranges(ranges)
            if not merged and not empty_file and not fragments:
                return None
            return FileGrant(
                file_key=str(file_key),
                digest=selected_digest,
                total_lines=total_lines,
                covered_ranges=merged,
                empty_file=empty_file,
                line_fragments=merged_fragments,
                result_ids=tuple(sorted(result_ids)),
            )

    def file_snapshot(
        self,
        *,
        execution_lifetime_id: str,
        file_key: str,
        digest: str,
    ) -> FileSnapshot | None:
        with self._lock:
            state = self._sessions.get(execution_lifetime_id)
            if state is None or state.retired:
                return None
            snapshot = state.snapshots.get(str(file_key))
            if snapshot is None:
                return None
            if state.current_user_turn >= snapshot.expires_at_user_turn:
                return None
            if str(digest) and snapshot.digest != str(digest):
                return None
            return snapshot

    def set_file_snapshot(
        self,
        *,
        execution_lifetime_id: str,
        file_key: str,
        digest: str,
        total_lines: int,
        complete: bool,
        source: str = "mutation",
    ) -> None:
        with self._lock:
            state = self._sessions.setdefault(execution_lifetime_id, _SessionState())
            total = max(0, int(total_lines))
            state.snapshots[str(file_key)] = FileSnapshot(
                file_key=str(file_key),
                digest=str(digest),
                total_lines=total,
                complete=bool(complete),
                created_user_turn=state.current_user_turn,
                expires_at_user_turn=(
                    state.current_user_turn + state.retention_user_turns
                ),
                source=(
                    "mutation" if str(source) == "mutation" else "delivery"
                ),
                replay_result_ref="",
            )

    def invalidate_file(self, *, execution_lifetime_id: str, file_key: str) -> None:
        with self._lock:
            state = self._sessions.get(execution_lifetime_id)
            if state is None:
                return
            state.snapshots.pop(str(file_key), None)
            state.file_results = {
                result_id: lease
                for result_id, lease in state.file_results.items()
                if lease.file_key != str(file_key)
            }

    def retire_session(self, execution_lifetime_id: str) -> None:
        with self._lock:
            # Preserve only a tombstone so late work cannot resurrect the
            # logical coroutine. Pager payloads, file snapshots, and result leases
            # all die at the same ownership boundary.
            self._sessions[str(execution_lifetime_id)] = _SessionState(
                retired=True
            )

    def snapshot_state(self) -> dict[str, Any]:
        """Serialize the execution lifetime without exposing live locks."""

        with self._lock:
            return {
                "sessions": {
                    session_id: {
                        "current_user_turn": state.current_user_turn,
                        "context_epoch": state.context_epoch,
                        "input_ids": dict(state.input_ids),
                        "retention_user_turns": state.retention_user_turns,
                        "projection": list(state.projection),
                        "handles": {
                            key: manifest.to_dict(include_payload=True)
                            for key, manifest in state.handles.items()
                        },
                        "expired_handles": {
                            key: manifest.to_dict(include_payload=False)
                            for key, manifest in state.expired_handles.items()
                        },
                        "snapshots": {
                            key: snapshot.to_dict()
                            for key, snapshot in state.snapshots.items()
                        },
                        "file_results": {
                            result_id: lease.to_dict()
                            for result_id, lease in state.file_results.items()
                        },
                        "retired": state.retired,
                    }
                    for session_id, state in self._sessions.items()
                }
            }

    def prepare_restore_state(self, payload: dict[str, Any]) -> dict[str, _SessionState]:
        raw_sessions = payload.get("sessions")
        if not isinstance(raw_sessions, dict):
            raise ValueError("execution runtime snapshot sessions must be an object")
        restored: dict[str, _SessionState] = {}
        for session_id, raw in raw_sessions.items():
            normalized_session_id = str(session_id or "").strip()
            if not normalized_session_id or not isinstance(raw, dict):
                raise ValueError("execution runtime snapshot contains an invalid session")
            allowed_fields = {
                "current_user_turn",
                "context_epoch",
                "input_ids",
                "retention_user_turns",
                "projection",
                "handles",
                "expired_handles",
                "snapshots",
                "grants",
                "file_results",
                "retired",
            }
            if extras := sorted(set(raw) - allowed_fields):
                raise ValueError(
                    f"execution runtime snapshot session has unknown fields: {extras}"
                )
            current_user_turn = max(0, int(raw.get("current_user_turn") or 0))
            input_ids = {
                str(key): int(value)
                for key, value in dict(raw.get("input_ids") or {}).items()
            }
            if any(
                not key or value <= 0 or value > current_user_turn
                for key, value in input_ids.items()
            ):
                raise ValueError(
                    "execution runtime snapshot contains an invalid semantic input"
                )
            raw_handles = dict(raw.get("handles") or {})
            raw_expired_handles = dict(raw.get("expired_handles") or {})
            handles = {
                str(key): PagerHandleManifest.from_dict(dict(value))
                for key, value in raw_handles.items()
                if isinstance(value, dict)
            }
            expired_handles = {
                str(key): PagerHandleManifest.from_dict(dict(value))
                for key, value in raw_expired_handles.items()
                if isinstance(value, dict)
            }
            if len(handles) != len(raw_handles) or len(expired_handles) != len(
                raw_expired_handles
            ):
                raise ValueError("execution runtime snapshot contains an invalid pager")
            if set(handles) & set(expired_handles):
                raise ValueError("execution runtime snapshot pager state overlaps")
            for result_ref, manifest in {**handles, **expired_handles}.items():
                if (
                    not result_ref
                    or manifest.result_ref != result_ref
                    or manifest.execution_lifetime_id != normalized_session_id
                    or manifest.expires_at_user_turn < manifest.created_user_turn
                ):
                    raise ValueError(
                        "execution runtime snapshot pager identity mismatch"
                    )
            raw_snapshots = dict(raw.get("snapshots") or {})
            snapshots = {
                str(key): FileSnapshot.from_dict(dict(value))
                for key, value in raw_snapshots.items()
                if isinstance(value, dict)
            }
            if len(snapshots) != len(raw_snapshots) or any(
                not key or snapshot.file_key != key
                for key, snapshot in snapshots.items()
            ):
                raise ValueError(
                    "execution runtime snapshot file snapshot identity mismatch"
                )
            state = _SessionState(
                current_user_turn=current_user_turn,
                context_epoch=max(1, int(raw.get("context_epoch") or 1)),
                input_ids=input_ids,
                retention_user_turns=max(
                    1,
                    int(
                        raw.get("retention_user_turns")
                        or DEFAULT_RESULT_RETENTION_USER_TURNS
                    ),
                ),
                projection=tuple(str(item) for item in list(raw.get("projection") or ())),
                handles=handles,
                expired_handles=expired_handles,
                snapshots=snapshots,
                file_results={
                    str(key): FileResultLease.from_dict(dict(value))
                    for key, value in dict(raw.get("file_results") or {}).items()
                    if isinstance(value, Mapping)
                },
                retired=bool(raw.get("retired")),
            )
            # Legacy epoch grants are intentionally not restored.  They have
            # no result owner and therefore cannot satisfy the v2 RAII model;
            # retained pager payloads can still be replayed to obtain a fresh
            # result-owned lease.
            if any(
                not result_id
                or lease.result_id != result_id
                or not lease.file_key
                or not lease.digest
                for result_id, lease in state.file_results.items()
            ):
                raise ValueError("execution runtime snapshot file result identity mismatch")
            if state.retired and (
                state.input_ids
                or state.projection
                or state.handles
                or state.expired_handles
                or state.snapshots
                or state.file_results
            ):
                raise ValueError(
                    "retired execution lifetime retained owned runtime state"
                )
            restored[normalized_session_id] = state
        for state in restored.values():
            self._expire_handles(state)
            self._expire_file_results(state)
        return restored

    def install_prepared_state(self, prepared: dict[str, _SessionState]) -> None:
        with self._lock:
            self._sessions = prepared

    def retire_pagers(
        self,
        *,
        execution_lifetime_id: str,
        result_refs: tuple[str, ...],
    ) -> tuple[PagerHandleManifest, ...]:
        with self._lock:
            state = self._sessions.get(str(execution_lifetime_id))
            if state is None:
                return ()
            retired = tuple(
                manifest
                for result_ref in result_refs
                if (manifest := state.handles.pop(str(result_ref), None)) is not None
            )
            for manifest in retired:
                state.expired_handles[manifest.result_ref] = replace(
                    manifest,
                    output_json="",
                    rendered="",
                    origin={},
                    delivery_manifest={},
                )
            retired_refs = {manifest.result_ref for manifest in retired}
            for result_ref in retired_refs:
                state.file_results.pop(result_ref, None)
            for file_key, snapshot in tuple(state.snapshots.items()):
                if snapshot.replay_result_ref in retired_refs:
                    state.snapshots.pop(file_key, None)
            return retired

    def retire_results(
        self,
        *,
        execution_lifetime_id: str,
        result_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Retire authority owners without necessarily deleting pager data."""

        with self._lock:
            state = self._sessions.get(str(execution_lifetime_id))
            if state is None:
                return ()
            retired = tuple(
                result_id
                for result_id in (str(item) for item in result_ids)
                if result_id in state.file_results
            )
            for result_id in retired:
                state.file_results.pop(result_id, None)
            return retired

    def expire_pagers(
        self,
        *,
        execution_lifetime_id: str,
    ) -> tuple[PagerHandleManifest, ...]:
        with self._lock:
            state = self._sessions.get(str(execution_lifetime_id))
            if state is None:
                return ()
            expired_refs = tuple(
                result_ref
                for result_ref, manifest in state.handles.items()
                if state.current_user_turn >= manifest.expires_at_user_turn
            )
        return self.retire_pagers(
            execution_lifetime_id=execution_lifetime_id,
            result_refs=expired_refs,
        )

    def reset_state(self) -> None:
        with self._lock:
            self._sessions.clear()

    @staticmethod
    def _context(session_id: str, input_id: str, state: _SessionState) -> LogicalExecutionContext:
        return LogicalExecutionContext(
            execution_lifetime_id=session_id,
            input_id=input_id,
            current_user_turn=state.current_user_turn,
            context_epoch=state.context_epoch,
        )

    @staticmethod
    def _expire_handles(state: _SessionState) -> None:
        expired_refs = tuple(
            result_ref
            for result_ref, manifest in state.handles.items()
            if state.current_user_turn >= manifest.expires_at_user_turn
        )
        if not expired_refs:
            state.snapshots = {
                key: snapshot
                for key, snapshot in state.snapshots.items()
                if state.current_user_turn < snapshot.expires_at_user_turn
            }
            return
        expired = {
            result_ref: state.handles.pop(result_ref)
            for result_ref in expired_refs
        }
        for result_ref, manifest in expired.items():
            state.expired_handles[result_ref] = replace(
                manifest,
                output_json="",
                rendered="",
                origin={},
                delivery_manifest={},
            )
        state.snapshots = {
            key: snapshot
            for key, snapshot in state.snapshots.items()
            if (
                state.current_user_turn < snapshot.expires_at_user_turn
                and snapshot.replay_result_ref not in expired
            )
        }

    @staticmethod
    def _expire_file_results(state: _SessionState) -> None:
        state.file_results = {
            result_id: lease
            for result_id, lease in state.file_results.items()
            if state.current_user_turn < lease.expires_at_user_turn
        }

    @staticmethod
    def _apply_delivery(
        state: _SessionState,
        delivery: dict[str, Any],
        *,
        replace_existing: bool,
    ) -> None:
        manifest = FileDeliveryManifest.from_dict(delivery)
        if manifest is None or not manifest.file_key or not manifest.digest:
            return
        result_id = _delivery_result_id(delivery, manifest=manifest)
        previous_lease = state.file_results.get(result_id)
        merge_previous = bool(
            not replace_existing
            and previous_lease is not None
            and previous_lease.digest == manifest.digest
            and previous_lease.file_key == manifest.file_key
        )
        ranges = (
            list(previous_lease.standalone_ranges)
            if merge_previous and previous_lease is not None
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
            list(previous_lease.line_fragments)
            if merge_previous and previous_lease is not None
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
        if manifest.complete_file and manifest.total_lines > 0:
            ranges.append((1, manifest.total_lines))
        inherited = list(manifest.inherited_ranges)
        parents = list(manifest.parent_result_ids)
        if merge_previous and previous_lease is not None:
            inherited.extend(previous_lease.inherited_ranges)
            parents.extend(previous_lease.parent_result_ids)
        state.file_results[result_id] = FileResultLease(
            result_id=result_id,
            file_key=manifest.file_key,
            digest=manifest.digest,
            total_lines=manifest.total_lines,
            standalone_ranges=_merge_ranges(ranges),
            inherited_ranges=_merge_ranges(inherited),
            parent_result_ids=tuple(sorted(set(parents))),
            empty_file=(
                manifest.empty_file
                or bool(merge_previous and previous_lease and previous_lease.empty_file)
            ),
            complete_file=(
                manifest.complete_file
                or bool(merge_previous and previous_lease and previous_lease.complete_file)
            ),
            operation=manifest.operation,
            before_digest=manifest.before_digest,
            created_user_turn=state.current_user_turn,
            expires_at_user_turn=state.current_user_turn + state.retention_user_turns,
            projected=True,
            replay_result_ref=manifest.replay_result_ref,
            line_fragments=merged_fragments,
        )
        previous_snapshot = state.snapshots.get(manifest.file_key)
        if (
            previous_snapshot is not None
            and previous_snapshot.source == "mutation"
            and previous_snapshot.digest != manifest.digest
            and manifest.operation == "read"
        ):
            return
        delivered_ranges = _merge_ranges(ranges)
        delivered_complete = bool(
            manifest.empty_file
            or manifest.complete_file
            or (
                manifest.total_lines > 0
                and any(
                    start <= 1 and end >= manifest.total_lines
                    for start, end in delivered_ranges
                )
            )
        )
        state.snapshots[manifest.file_key] = FileSnapshot(
            file_key=manifest.file_key,
            digest=manifest.digest,
            total_lines=manifest.total_lines,
            complete=delivered_complete,
            created_user_turn=state.current_user_turn,
            expires_at_user_turn=(
                state.current_user_turn + state.retention_user_turns
            ),
            source=(
                "mutation"
                if manifest.operation in {"edit", "write"}
                else "delivery"
            ),
            replay_result_ref=manifest.replay_result_ref,
        )


def page_count_for(rendered: str, page_size: int) -> int:
    return max(1, math.ceil(len(str(rendered or "")) / max(256, int(page_size))))


def content_digest(content: str) -> str:
    return hashlib.sha256(str(content).encode("utf-8")).hexdigest()


def count_text_lines(content: str) -> int:
    return len(str(content).splitlines(keepends=True))


def _delivery_result_id(
    delivery: Mapping[str, Any],
    *,
    manifest: FileDeliveryManifest | None = None,
) -> str:
    parsed = manifest or FileDeliveryManifest.from_dict(delivery)
    replay_result_ref = parsed.replay_result_ref if parsed is not None else ""
    explicit = str(
        delivery.get("result_id")
        or delivery.get("_result_id")
        or replay_result_ref
        or ""
    )
    if explicit:
        return explicit
    identity = {
        "file_key": parsed.file_key if parsed is not None else "",
        "digest": parsed.digest if parsed is not None else "",
        "operation": parsed.operation if parsed is not None else "read",
    }
    return "legacy:" + hashlib.sha256(
        json.dumps(identity, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


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
