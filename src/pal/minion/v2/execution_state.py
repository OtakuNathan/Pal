from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from pal.execution.session_state import (
    DEFAULT_RESULT_RETENTION_USER_TURNS,
    FileDeliveryManifest,
    FileGrant,
    FileSnapshot,
    LogicalExecutionContext,
    PagerHandleManifest,
    PagerRead,
)
from pal.minion.ipc import MinionRoleGatewayClient
from pal.minion.v2.service import MinionV2WorkflowService


@dataclass
class ManagerLogicalExecutionState:
    """Manager-side durable state operations for one logical role session."""

    service: MinionV2WorkflowService
    logical_session_id: str

    @property
    def repository(self):
        return self.service.repository

    def context(self) -> LogicalExecutionContext:
        state = self.repository.read_role_execution_state(self.logical_session_id)
        return _context(self.logical_session_id, "", state)

    def begin_input(
        self,
        *,
        input_id: str,
        retention_user_turns: int = DEFAULT_RESULT_RETENTION_USER_TURNS,
    ) -> LogicalExecutionContext:
        value = self.repository.begin_role_execution_input(
            self.logical_session_id,
            input_id=str(input_id),
            retention_user_turns=retention_user_turns,
        )
        return LogicalExecutionContext.from_dict(dict(value))

    def reconcile_projection(
        self,
        *,
        projection: tuple[str, ...],
        deliveries: tuple[dict[str, Any], ...],
    ) -> LogicalExecutionContext:
        normalized_projection = tuple(str(item) for item in projection)

        def mutate(
            state: dict[str, Any],
        ) -> tuple[dict[str, Any], LogicalExecutionContext]:
            _ensure_active(state)
            previous = tuple(str(item) for item in state.get("projection") or ())
            append_only = (
                len(normalized_projection) >= len(previous)
                and normalized_projection[: len(previous)] == previous
            )
            if previous and not append_only:
                state["context_epoch"] = max(
                    1, int(state.get("context_epoch") or 1)
                ) + 1
                state["grants"] = {}
            state["projection"] = list(normalized_projection)
            for delivery in deliveries:
                _apply_delivery(state, dict(delivery))
            return state, _context(self.logical_session_id, "", state)

        return self.repository.mutate_role_execution_state(
            self.logical_session_id, mutate
        )

    def record_delivery(
        self,
        *,
        logical_session_id: str | None = None,
        delivery: dict[str, Any],
    ) -> LogicalExecutionContext:
        """Commit one delivered tool result without changing projection."""

        requested = str(logical_session_id or self.logical_session_id)
        if requested != self.logical_session_id:
            raise ValueError("logical execution state does not match the role session")

        def mutate(
            state: dict[str, Any],
        ) -> tuple[dict[str, Any], LogicalExecutionContext]:
            _ensure_active(state)
            _apply_delivery(state, dict(delivery))
            return state, _context(self.logical_session_id, "", state)

        return self.repository.mutate_role_execution_state(
            self.logical_session_id, mutate
        )

    def store_pager(
        self, manifest: PagerHandleManifest
    ) -> PagerHandleManifest:
        if manifest.logical_session_id != self.logical_session_id:
            raise ValueError("pager handle belongs to a different logical session")
        payload_ref = self.service.artifacts.put_json(
            {
                "output_json": manifest.output_json,
                "rendered": manifest.rendered,
            },
            artifact_type="LogicalToolResultArtifact",
            schema_version="1",
            provenance={"logical_session_id": self.logical_session_id},
        )

        def mutate(
            current: dict[str, Any],
        ) -> tuple[dict[str, Any], PagerHandleManifest]:
            _ensure_active(current)
            current_turn = max(
                0, int(current.get("current_user_turn") or 0)
            )
            retention = max(
                1,
                int(
                    current.get("retention_user_turns")
                    or DEFAULT_RESULT_RETENTION_USER_TURNS
                ),
            )
            stored = PagerHandleManifest(
                result_ref=manifest.result_ref,
                logical_session_id=self.logical_session_id,
                tool_name=manifest.tool_name,
                status=manifest.status,
                ok=manifest.ok,
                page_size=manifest.page_size,
                original_size=manifest.original_size,
                page_count=manifest.page_count,
                created_user_turn=current_turn,
                expires_at_user_turn=current_turn + retention,
                output_json=manifest.output_json,
                rendered=manifest.rendered,
                origin=dict(manifest.origin),
                delivery_manifest=dict(manifest.delivery_manifest),
            )
            handles = dict(current.get("handles") or {})
            existing = dict(handles.get(stored.result_ref) or {})
            if existing:
                existing_ref = dict(existing.get("payload_ref") or {})
                if str(existing_ref.get("sha256") or "") != payload_ref.sha256:
                    raise ValueError(
                        "result_ref was reused with different validated output"
                    )
                return current, _manifest_from_record(existing)
            record = stored.to_dict(include_payload=False)
            record["payload_ref"] = payload_ref.to_dict()
            handles[stored.result_ref] = record
            current["handles"] = handles
            return current, stored

        return self.repository.mutate_role_execution_state(
            self.logical_session_id, mutate
        )

    def read_pager(
        self,
        *,
        result_ref: str,
        page: int,
        page_size: int | None,
        anchor: str,
    ) -> PagerRead:
        state = self.repository.read_role_execution_state(self.logical_session_id)
        record = dict(dict(state.get("handles") or {}).get(str(result_ref)) or {})
        if not record:
            return PagerRead(state="unknown_handle")
        manifest = _manifest_from_record(record)
        if bool(state.get("retired")) or int(
            state.get("current_user_turn") or 0
        ) >= manifest.expires_at_user_turn:
            return PagerRead(state="expired_handle", manifest=manifest)
        payload = self.service.artifacts.read_json(
            dict(record.get("payload_ref") or {})
        )
        if not isinstance(payload, Mapping):
            raise ValueError("stored tool result payload is malformed")
        text = str(payload.get("rendered") or "")
        hydrated = PagerHandleManifest(
            **{
                **manifest.__dict__,
                "output_json": str(payload.get("output_json") or ""),
                "rendered": text,
            }
        )
        size = max(256, int(page_size or hydrated.page_size))
        page_count = max(1, math.ceil(len(text) / size))
        anchor_value = "tail" if str(anchor).lower() == "tail" else "head"
        anchor_page = max(1, int(page or 1))
        absolute_page = (
            page_count - anchor_page + 1
            if anchor_value == "tail"
            else anchor_page
        )
        if absolute_page < 1 or absolute_page > page_count:
            return PagerRead(
                state="page_out_of_range",
                manifest=hydrated,
                page=absolute_page,
                page_count=page_count,
                page_size=size,
                anchor=anchor_value,
                anchor_page=anchor_page,
            )
        start = (absolute_page - 1) * size
        end = min(start + size, len(text))
        delivery = FileDeliveryManifest.from_dict(hydrated.delivery_manifest)
        sliced = delivery.slice(start, end) if delivery is not None else None
        return PagerRead(
            state="ok",
            manifest=hydrated,
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

    def file_grant(self, *, file_key: str, digest: str) -> FileGrant | None:
        state = self.repository.read_role_execution_state(self.logical_session_id)
        if bool(state.get("retired")):
            return None
        epoch = max(1, int(state.get("context_epoch") or 1))
        value = dict(
            dict(state.get("grants") or {}).get(_grant_key(epoch, file_key)) or {}
        )
        if not value:
            return None
        if str(digest) and str(value.get("digest") or "") != str(digest):
            return None
        return _file_grant_from_dict(value)

    def file_snapshot(self, *, file_key: str, digest: str) -> FileSnapshot | None:
        state = self.repository.read_role_execution_state(self.logical_session_id)
        if bool(state.get("retired")):
            return None
        value = dict(
            dict(state.get("snapshots") or {}).get(str(file_key)) or {}
        )
        if not value:
            return None
        snapshot = FileSnapshot.from_dict(value)
        if int(state.get("current_user_turn") or 0) >= snapshot.expires_at_user_turn:
            return None
        if str(digest) and snapshot.digest != str(digest):
            return None
        return snapshot

    def set_file_snapshot(
        self,
        *,
        file_key: str,
        digest: str,
        total_lines: int,
        complete: bool,
        source: str = "mutation",
    ) -> None:
        def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
            _ensure_active(state)
            total = max(0, int(total_lines))
            current_turn = max(0, int(state.get("current_user_turn") or 0))
            retention = max(
                1,
                int(
                    state.get("retention_user_turns")
                    or DEFAULT_RESULT_RETENTION_USER_TURNS
                ),
            )
            snapshots = dict(state.get("snapshots") or {})
            snapshots[str(file_key)] = {
                "file_key": str(file_key),
                "digest": str(digest),
                "total_lines": total,
                "complete": bool(complete),
                "created_user_turn": current_turn,
                "expires_at_user_turn": current_turn + retention,
                "source": (
                    "mutation" if str(source) == "mutation" else "delivery"
                ),
            }
            state["snapshots"] = snapshots
            return state, None

        self.repository.mutate_role_execution_state(
            self.logical_session_id, mutate
        )

    def invalidate_file(self, *, file_key: str) -> None:
        def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
            snapshots = dict(state.get("snapshots") or {})
            snapshots.pop(str(file_key), None)
            state["snapshots"] = snapshots
            grants = {
                key: value
                for key, value in dict(state.get("grants") or {}).items()
                if str(dict(value).get("file_key") or "") != str(file_key)
            }
            state["grants"] = grants
            return state, None

        self.repository.mutate_role_execution_state(
            self.logical_session_id, mutate
        )

    def retire(self) -> None:
        def mutate(state: dict[str, Any]) -> tuple[dict[str, Any], None]:
            state["retired"] = True
            return state, None

        self.repository.mutate_role_execution_state(
            self.logical_session_id, mutate
        )


@dataclass
class RoleGatewayLogicalExecutionState:
    """Worker-side facade over Manager-owned role-session execution state."""

    client: MinionRoleGatewayClient

    def begin_input(
        self,
        *,
        logical_session_id: str,
        input_id: str,
        retention_user_turns: int = DEFAULT_RESULT_RETENTION_USER_TURNS,
    ) -> LogicalExecutionContext:
        value = self.client.request_sync(
            "execution_begin_input",
            {
                "logical_session_id": str(logical_session_id),
                "input_id": str(input_id),
                "retention_user_turns": int(retention_user_turns),
            },
        )
        return LogicalExecutionContext.from_dict(dict(value.get("context") or {}))

    def context(self, logical_session_id: str) -> LogicalExecutionContext:
        value = self.client.request_sync(
            "execution_context",
            {"logical_session_id": str(logical_session_id)},
        )
        return LogicalExecutionContext.from_dict(dict(value.get("context") or {}))

    def reconcile_projection(
        self,
        *,
        logical_session_id: str,
        projection: tuple[str, ...],
        deliveries: tuple[dict[str, Any], ...],
    ) -> LogicalExecutionContext:
        value = self.client.request_sync(
            "execution_reconcile_projection",
            {
                "logical_session_id": str(logical_session_id),
                "projection": list(projection),
                "deliveries": [dict(item) for item in deliveries],
            },
        )
        return LogicalExecutionContext.from_dict(dict(value.get("context") or {}))

    def record_delivery(
        self,
        *,
        logical_session_id: str,
        delivery: dict[str, Any],
    ) -> LogicalExecutionContext:
        value = self.client.request_sync(
            "execution_record_delivery",
            {
                "logical_session_id": str(logical_session_id),
                "delivery": dict(delivery),
            },
        )
        return LogicalExecutionContext.from_dict(dict(value.get("context") or {}))

    def store_pager(self, manifest: PagerHandleManifest) -> PagerHandleManifest:
        value = self.client.request_sync(
            "execution_store_pager",
            {"manifest": manifest.to_dict(include_payload=True)},
        )
        return PagerHandleManifest.from_dict(dict(value.get("manifest") or {}))

    def read_pager(
        self,
        *,
        logical_session_id: str,
        result_ref: str,
        page: int,
        page_size: int | None,
        anchor: str,
    ) -> PagerRead:
        value = self.client.request_sync(
            "execution_read_pager",
            {
                "logical_session_id": str(logical_session_id),
                "result_ref": str(result_ref),
                "page": int(page),
                "page_size": int(page_size) if page_size is not None else None,
                "anchor": str(anchor),
            },
        )
        manifest_value = value.get("manifest")
        return PagerRead(
            state=str(value.get("state") or "unknown_handle"),
            manifest=(
                PagerHandleManifest.from_dict(dict(manifest_value))
                if isinstance(manifest_value, Mapping)
                else None
            ),
            content=str(value.get("content") or ""),
            page=int(value.get("page") or 1),
            page_count=int(value.get("page_count") or 1),
            page_size=int(value.get("page_size") or 0),
            anchor=str(value.get("anchor") or "head"),
            anchor_page=int(value.get("anchor_page") or 1),
            start_offset=int(value.get("start_offset") or 0),
            end_offset=int(value.get("end_offset") or 0),
            delivery_manifest=dict(value.get("delivery_manifest") or {}),
        )

    def file_grant(
        self,
        *,
        logical_session_id: str,
        file_key: str,
        digest: str,
    ) -> FileGrant | None:
        value = self.client.request_sync(
            "execution_file_grant",
            {
                "logical_session_id": str(logical_session_id),
                "file_key": str(file_key),
                "digest": str(digest),
            },
        )
        grant = value.get("grant")
        return (
            _file_grant_from_dict(dict(grant))
            if isinstance(grant, Mapping)
            else None
        )

    def file_snapshot(
        self,
        *,
        logical_session_id: str,
        file_key: str,
        digest: str,
    ) -> FileSnapshot | None:
        value = self.client.request_sync(
            "execution_file_snapshot",
            {
                "logical_session_id": str(logical_session_id),
                "file_key": str(file_key),
                "digest": str(digest),
            },
        )
        snapshot = value.get("snapshot")
        return (
            FileSnapshot.from_dict(dict(snapshot))
            if isinstance(snapshot, Mapping)
            else None
        )

    def set_file_snapshot(
        self,
        *,
        logical_session_id: str,
        file_key: str,
        digest: str,
        total_lines: int,
        complete: bool,
        source: str = "mutation",
    ) -> None:
        self.client.request_sync(
            "execution_set_file_snapshot",
            {
                "logical_session_id": str(logical_session_id),
                "file_key": str(file_key),
                "digest": str(digest),
                "total_lines": int(total_lines),
                "complete": bool(complete),
                "source": str(source),
            },
        )

    def invalidate_file(self, *, logical_session_id: str, file_key: str) -> None:
        self.client.request_sync(
            "execution_invalidate_file",
            {
                "logical_session_id": str(logical_session_id),
                "file_key": str(file_key),
            },
        )

    def retire_session(self, logical_session_id: str) -> None:
        self.client.request_sync(
            "execution_retire",
            {"logical_session_id": str(logical_session_id)},
        )


def pager_read_to_dict(value: PagerRead) -> dict[str, Any]:
    return {
        "state": value.state,
        "manifest": (
            value.manifest.to_dict(include_payload=False)
            if value.manifest is not None
            else None
        ),
        "content": value.content,
        "page": value.page,
        "page_count": value.page_count,
        "page_size": value.page_size,
        "anchor": value.anchor,
        "anchor_page": value.anchor_page,
        "start_offset": value.start_offset,
        "end_offset": value.end_offset,
        "delivery_manifest": dict(value.delivery_manifest),
    }


def _context(
    session_id: str, input_id: str, state: Mapping[str, Any]
) -> LogicalExecutionContext:
    return LogicalExecutionContext(
        logical_session_id=str(session_id),
        input_id=str(input_id),
        current_user_turn=max(0, int(state.get("current_user_turn") or 0)),
        context_epoch=max(1, int(state.get("context_epoch") or 1)),
    )


def _ensure_active(state: Mapping[str, Any]) -> None:
    if bool(state.get("retired")):
        raise RuntimeError("logical execution session is retired")


def _manifest_from_record(record: Mapping[str, Any]) -> PagerHandleManifest:
    return PagerHandleManifest.from_dict(dict(record))


def _grant_key(epoch: int, file_key: str) -> str:
    return f"{max(1, int(epoch))}\0{str(file_key)}"


def _file_grant_from_dict(value: Mapping[str, Any]) -> FileGrant:
    return FileGrant(
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


def _apply_delivery(state: dict[str, Any], delivery: dict[str, Any]) -> None:
    manifest = FileDeliveryManifest.from_dict(delivery)
    if manifest is None or not manifest.file_key or not manifest.digest:
        return
    epoch = max(1, int(state.get("context_epoch") or 1))
    grants = dict(state.get("grants") or {})
    key = _grant_key(epoch, manifest.file_key)
    previous_value = dict(grants.get(key) or {})
    previous = (
        _file_grant_from_dict(previous_value)
        if previous_value
        and str(previous_value.get("digest") or "") == manifest.digest
        else None
    )
    ranges = list(previous.covered_ranges) if previous is not None else []
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
    fragments = list(previous.line_fragments) if previous is not None else []
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
    grants[key] = {
        "file_key": manifest.file_key,
        "digest": manifest.digest,
        "total_lines": manifest.total_lines,
        "covered_ranges": [list(item) for item in _merge_ranges(ranges)],
        "empty_file": manifest.empty_file
        or bool(previous is not None and previous.empty_file),
        "line_fragments": [list(item) for item in merged_fragments],
    }
    state["grants"] = grants
    current_turn = max(0, int(state.get("current_user_turn") or 0))
    retention = max(
        1,
        int(
            state.get("retention_user_turns")
            or DEFAULT_RESULT_RETENTION_USER_TURNS
        ),
    )
    snapshots = dict(state.get("snapshots") or {})
    previous_snapshot = FileSnapshot.from_dict(
        dict(snapshots.get(manifest.file_key) or {})
    ) if snapshots.get(manifest.file_key) else None
    delivered = _file_grant_from_dict(grants[key])
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
        and not delivered.complete
    ):
        return
    snapshots[manifest.file_key] = FileSnapshot(
        file_key=manifest.file_key,
        digest=manifest.digest,
        total_lines=manifest.total_lines,
        complete=(
            delivered.complete
            or bool(
                previous_snapshot is not None
                and previous_snapshot.digest == manifest.digest
                and previous_snapshot.complete
            )
        ),
        created_user_turn=current_turn,
        expires_at_user_turn=current_turn + retention,
        source="delivery",
    ).to_dict()
    state["snapshots"] = snapshots


def _merge_ranges(
    ranges: list[tuple[int, int]],
) -> tuple[tuple[int, int], ...]:
    normalized = sorted(
        (max(1, int(start)), max(1, int(end)))
        for start, end in ranges
        if int(end) >= int(start)
    )
    merged: list[tuple[int, int]] = []
    for start, end in normalized:
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
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
    result: list[tuple[int, int, int, int]] = []
    completed: list[int] = []
    for (line, length), ranges in sorted(grouped.items()):
        merged: list[tuple[int, int]] = []
        for start, end in sorted(ranges):
            if not merged or start > merged[-1][1]:
                merged.append((start, end))
            else:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        result.extend((line, start, end, length) for start, end in merged)
        if merged and merged[0][0] == 0 and merged[0][1] >= length:
            completed.append(line)
    return tuple(result), tuple(completed)
