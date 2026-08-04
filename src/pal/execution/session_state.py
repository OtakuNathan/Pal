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
    replay_result_ref: str = ""

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
            replay_result_ref=self.replay_result_ref,
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
            previous = state.projection
            if normalized_projection != previous:
                state.context_epoch += 1
                state.grants = {}
            state.projection = normalized_projection
            for delivery in deliveries:
                self._apply_delivery(state, dict(delivery))
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
            self._apply_delivery(state, dict(delivery))
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
            grant = state.grants.get((state.context_epoch, str(file_key)))
            if grant is None or (str(digest) and grant.digest != str(digest)):
                return None
            return grant

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
            for key in [candidate for candidate in state.grants if candidate[1] == str(file_key)]:
                state.grants.pop(key, None)

    def retire_session(self, execution_lifetime_id: str) -> None:
        with self._lock:
            # Preserve only a tombstone so late work cannot resurrect the
            # logical coroutine. Pager payloads, file snapshots, and grants
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
                        "grants": [
                            {
                                "context_epoch": epoch,
                                "file_key": file_key,
                                "grant": grant.to_dict(),
                            }
                            for (epoch, file_key), grant in state.grants.items()
                        ],
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
                retired=bool(raw.get("retired")),
            )
            for item in list(raw.get("grants") or ()):
                if not isinstance(item, dict) or not isinstance(item.get("grant"), dict):
                    raise ValueError("execution runtime snapshot contains an invalid file grant")
                epoch = max(1, int(item.get("context_epoch") or 1))
                file_key = str(item.get("file_key") or "")
                grant = FileGrant.from_dict(dict(item["grant"]))
                if not file_key or grant.file_key != file_key:
                    raise ValueError("execution runtime snapshot file grant identity mismatch")
                if epoch != state.context_epoch:
                    raise ValueError("execution runtime snapshot file grant epoch mismatch")
                state.grants[(epoch, file_key)] = grant
            if state.retired and (
                state.input_ids
                or state.projection
                or state.handles
                or state.expired_handles
                or state.snapshots
                or state.grants
            ):
                raise ValueError(
                    "retired execution lifetime retained owned runtime state"
                )
            restored[normalized_session_id] = state
        for state in restored.values():
            self._expire_handles(state)
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
            remaining_file_keys = {
                str(manifest.delivery_manifest.get("file_key") or "")
                for manifest in state.handles.values()
                if manifest.delivery_manifest
            }
            state.grants = {
                key: grant
                for key, grant in state.grants.items()
                if not any(
                    str(manifest.delivery_manifest.get("file_key") or "")
                    == grant.file_key
                    and grant.file_key not in remaining_file_keys
                    for manifest in retired
                    if manifest.result_ref in retired_refs
                )
            }
            for file_key, snapshot in tuple(state.snapshots.items()):
                if snapshot.replay_result_ref in retired_refs:
                    state.snapshots.pop(file_key, None)
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
        expired_file_keys = {
            str(manifest.delivery_manifest.get("file_key") or "")
            for manifest in expired.values()
            if manifest.delivery_manifest
        }
        remaining_file_keys = {
            str(manifest.delivery_manifest.get("file_key") or "")
            for manifest in state.handles.values()
            if manifest.delivery_manifest
        }
        state.grants = {
            key: grant
            for key, grant in state.grants.items()
            if not (
                grant.file_key in expired_file_keys
                and grant.file_key not in remaining_file_keys
            )
        }
        state.snapshots = {
            key: snapshot
            for key, snapshot in state.snapshots.items()
            if (
                state.current_user_turn < snapshot.expires_at_user_turn
                and snapshot.replay_result_ref not in expired
            )
        }

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
        previous_snapshot = state.snapshots.get(manifest.file_key)
        delivered_grant = state.grants[key]
        if (
            previous_snapshot is not None
            and previous_snapshot.source == "mutation"
            and previous_snapshot.digest != manifest.digest
        ):
            return
        if (
            previous_snapshot is not None
            and previous_snapshot.digest == manifest.digest
            and previous_snapshot.complete
            and not delivered_grant.complete
        ):
            return
        state.snapshots[manifest.file_key] = FileSnapshot(
            file_key=manifest.file_key,
            digest=manifest.digest,
            total_lines=manifest.total_lines,
            complete=(
                delivered_grant.complete
                or bool(
                    previous_snapshot is not None
                    and previous_snapshot.digest == manifest.digest
                    and previous_snapshot.complete
                )
            ),
            created_user_turn=state.current_user_turn,
            expires_at_user_turn=(
                state.current_user_turn + state.retention_user_turns
            ),
            source="delivery",
            replay_result_ref=manifest.replay_result_ref,
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
