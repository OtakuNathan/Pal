from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


DEFAULT_RESULT_RETENTION_USER_TURNS = 5


@dataclass(frozen=True)
class LogicalExecutionContext:
    execution_lifetime_id: str
    input_id: str
    current_user_turn: int
    retention_user_turns: int = DEFAULT_RESULT_RETENTION_USER_TURNS

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_lifetime_id": self.execution_lifetime_id,
            "input_id": self.input_id,
            "current_user_turn": self.current_user_turn,
            "retention_user_turns": self.retention_user_turns,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LogicalExecutionContext":
        return cls(
            execution_lifetime_id=str(value.get("execution_lifetime_id") or ""),
            input_id=str(value.get("input_id") or ""),
            current_user_turn=max(0, int(value.get("current_user_turn") or 0)),
            retention_user_turns=max(
                1,
                int(
                    value.get("retention_user_turns")
                    or DEFAULT_RESULT_RETENTION_USER_TURNS
                ),
            ),
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_key": self.file_key,
            "digest": self.digest,
            "total_lines": self.total_lines,
            "complete": self.complete,
            "created_user_turn": self.created_user_turn,
            "expires_at_user_turn": self.expires_at_user_turn,
            "source": self.source,
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
        )


@dataclass(frozen=True)
class FileResultLease:
    """One retained tool result's contribution to file authority."""

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

    def record_delivery(
        self,
        *,
        execution_lifetime_id: str,
        delivery: dict[str, Any],
    ) -> LogicalExecutionContext:
        """Commit authority owned by one delivered tool result."""
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
    input_ids: dict[str, int] = field(default_factory=dict)
    retention_user_turns: int = DEFAULT_RESULT_RETENTION_USER_TURNS
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
            self._expire_file_snapshots(state)
            return self._context(session_id, semantic_input, state)

    def context(self, execution_lifetime_id: str) -> LogicalExecutionContext:
        session_id = _required(execution_lifetime_id, "execution_lifetime_id")
        with self._lock:
            state = self._sessions.setdefault(session_id, _SessionState())
            return self._context(session_id, "", state)

    def record_delivery(
        self,
        *,
        execution_lifetime_id: str,
        delivery: dict[str, Any],
    ) -> LogicalExecutionContext:
        """Commit authority owned by a just-delivered result."""

        session_id = _required(execution_lifetime_id, "execution_lifetime_id")
        with self._lock:
            state = self._sessions.setdefault(session_id, _SessionState())
            if state.retired:
                raise RuntimeError("logical execution session is retired")
            self._apply_delivery(state, dict(delivery), replace_existing=False)
            return self._context(session_id, "", state)

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
            active = state.file_results
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
            # logical coroutine. File snapshots and result leases
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
                        "input_ids": dict(state.input_ids),
                        "retention_user_turns": state.retention_user_turns,
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
                "context_epoch",  # accepted and ignored from older snapshots
                "input_ids",
                "retention_user_turns",
                "projection",  # accepted and ignored from older snapshots
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
                input_ids=input_ids,
                retention_user_turns=max(
                    1,
                    int(
                        raw.get("retention_user_turns")
                        or DEFAULT_RESULT_RETENTION_USER_TURNS
                    ),
                ),
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
            # new source reads must establish result-owned leases.
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
                or state.snapshots
                or state.file_results
            ):
                raise ValueError(
                    "retired execution lifetime retained owned runtime state"
                )
            restored[normalized_session_id] = state
        for state in restored.values():
            self._expire_file_snapshots(state)
        return restored

    def install_prepared_state(self, prepared: dict[str, _SessionState]) -> None:
        with self._lock:
            self._sessions = prepared

    def retire_results(
        self,
        *,
        execution_lifetime_id: str,
        result_ids: tuple[str, ...],
    ) -> tuple[str, ...]:
        """Retire file authority owned by removed results."""

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

    def reset_state(self) -> None:
        with self._lock:
            self._sessions.clear()

    @staticmethod
    def _context(session_id: str, input_id: str, state: _SessionState) -> LogicalExecutionContext:
        return LogicalExecutionContext(
            execution_lifetime_id=session_id,
            input_id=input_id,
            current_user_turn=state.current_user_turn,
            retention_user_turns=state.retention_user_turns,
        )

    @staticmethod
    def _expire_file_snapshots(state: _SessionState) -> None:
        state.snapshots = {
            key: snapshot
            for key, snapshot in state.snapshots.items()
            if state.current_user_turn < snapshot.expires_at_user_turn
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
        )


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
