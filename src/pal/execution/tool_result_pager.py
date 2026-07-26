from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

from pal.execution.session_state import (
    FileDeliveryManifest,
    InMemoryLogicalExecutionState,
    LogicalExecutionContext,
    LogicalExecutionStateBackend,
    PagerHandleManifest,
    page_count_for,
)


DEFAULT_TOOL_RESULT_PAGE_SIZE = 4_000
DEFAULT_TOOL_RESULT_RETENTION_USER_TURNS = 5
TOOL_RESULT_READER_ALIAS = "read_tool_result"


@dataclass(frozen=True)
class ToolResultPage:
    result_ref: str
    content: str
    page: int
    page_count: int
    has_more: bool
    original_size: int
    page_size: int
    anchor: str = "head"
    anchor_page: int = 1
    has_more_before: bool = False
    has_more_after: bool = False
    start_offset: int = 0
    end_offset: int = 0
    tool_name: str = ""
    status: str = ""
    ok: bool = True
    state: str = "ok"
    origin: dict[str, Any] = field(default_factory=dict)
    expires_at_user_turn: int = 0
    current_user_turn: int = 0
    context_delivery: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolResultPagerStore:
    retention_user_turns: int = DEFAULT_TOOL_RESULT_RETENTION_USER_TURNS
    storage_root: Path | None = None
    state_backend: LogicalExecutionStateBackend = field(
        default_factory=InMemoryLogicalExecutionState
    )
    _turn_contexts: dict[str, LogicalExecutionContext] = field(default_factory=dict)
    _last_context: LogicalExecutionContext | None = None
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def begin_turn(
        self,
        *,
        runtime_root: Path | None,
        turn_id: str,
        scope_key: str = "",
        retention_user_turns: int | None = None,
        input_id: str = "",
    ) -> LogicalExecutionContext:
        if retention_user_turns is not None:
            self.retention_user_turns = max(1, int(retention_user_turns))
        normalized_turn_id = str(turn_id or "").strip()
        session_id = str(scope_key or "").strip() or "local:default"
        semantic_input = str(input_id or "").strip() or normalized_turn_id or "input:default"
        context = self.state_backend.begin_input(
            logical_session_id=session_id,
            input_id=semantic_input,
            retention_user_turns=self.retention_user_turns,
        )
        with self._lock:
            if normalized_turn_id:
                self._turn_contexts[normalized_turn_id] = context
            self._last_context = context
        _ = runtime_root
        return context

    def context_for_turn(self, turn_id: str | None) -> LogicalExecutionContext:
        normalized = str(turn_id or "").strip()
        with self._lock:
            context = self._turn_contexts.get(normalized) if normalized else None
            if context is None:
                context = self._last_context
        if context is not None:
            return self.state_backend.context(context.logical_session_id)
        return self.begin_turn(
            runtime_root=None,
            turn_id=normalized or "local:default",
            scope_key="local:default",
            input_id=normalized or "input:default",
        )

    def store(
        self,
        *,
        runtime_root: Path | None,
        turn_id: str,
        result_ref: str,
        tool_name: str,
        status: str,
        ok: bool,
        rendered: str,
        page_size: int,
        output_json: str = "",
        origin: dict[str, Any] | None = None,
        context_delivery: dict[str, Any] | None = None,
    ) -> PagerHandleManifest:
        normalized_ref = str(result_ref or "").strip()
        if not normalized_ref:
            raise ValueError("result_ref is required")
        normalized_turn_id = str(turn_id or "").strip() or "unknown_turn"
        context = self.context_for_turn(normalized_turn_id)
        rendered_text = str(rendered or "")
        resolved_page_size = max(256, int(page_size or DEFAULT_TOOL_RESULT_PAGE_SIZE))
        path: Path | None = None
        root = self._ephemeral_root(runtime_root)
        if root is not None and isinstance(self.state_backend, InMemoryLogicalExecutionState):
            turn_dir = root / _safe_file_name(context.logical_session_id)
            turn_dir.mkdir(parents=True, exist_ok=True)
            path = turn_dir / f"{_safe_file_name(normalized_ref)}.txt"
            path.write_text(rendered_text, encoding="utf-8")
        manifest = PagerHandleManifest(
            result_ref=normalized_ref,
            logical_session_id=context.logical_session_id,
            tool_name=str(tool_name or ""),
            status=str(status or ""),
            ok=bool(ok),
            page_size=resolved_page_size,
            original_size=len(rendered_text),
            page_count=page_count_for(rendered_text, resolved_page_size),
            created_user_turn=context.current_user_turn,
            expires_at_user_turn=context.current_user_turn + self.retention_user_turns,
            output_json=str(output_json or ""),
            rendered=rendered_text,
            origin=dict(origin or {}),
            delivery_manifest=dict(context_delivery or {}),
            backing_path=str(path) if path is not None else "",
        )
        return self.state_backend.store_pager(manifest)

    def read_page(
        self,
        result_ref: str,
        *,
        page: int = 1,
        page_size: int | None = None,
        anchor: str = "head",
        turn_id: str | None = None,
        logical_session_id: str = "",
    ) -> ToolResultPage | None:
        normalized_ref = str(result_ref or "").strip()
        if not normalized_ref:
            return None
        normalized_anchor = _normalize_anchor(anchor)
        context = (
            self.state_backend.context(str(logical_session_id))
            if str(logical_session_id or "").strip()
            else self.context_for_turn(turn_id)
        )
        result = self.state_backend.read_pager(
            logical_session_id=context.logical_session_id,
            result_ref=normalized_ref,
            page=page,
            page_size=page_size,
            anchor=normalized_anchor,
        )
        manifest = result.manifest
        if result.state == "unknown_handle":
            return None
        return ToolResultPage(
            result_ref=normalized_ref,
            content=result.content,
            page=result.page,
            page_count=result.page_count,
            has_more=result.page < result.page_count,
            original_size=manifest.original_size if manifest is not None else 0,
            page_size=result.page_size,
            anchor=result.anchor,
            anchor_page=result.anchor_page,
            has_more_before=result.page > 1 and result.state == "ok",
            has_more_after=result.page < result.page_count and result.state == "ok",
            start_offset=result.start_offset,
            end_offset=result.end_offset,
            tool_name=manifest.tool_name if manifest is not None else "",
            status=manifest.status if manifest is not None else "",
            ok=manifest.ok if manifest is not None else False,
            state=result.state,
            origin=dict(manifest.origin) if manifest is not None else {},
            expires_at_user_turn=(
                manifest.expires_at_user_turn if manifest is not None else 0
            ),
            current_user_turn=context.current_user_turn,
            context_delivery=dict(result.delivery_manifest),
        )

    def prune(self, *, runtime_root: Path | None = None) -> None:
        _ = runtime_root

    def delete(self, result_ref: str) -> None:
        _ = result_ref

    def _ephemeral_root(self, runtime_root: Path | None) -> Path | None:
        if self.storage_root is not None:
            return Path(self.storage_root)
        if runtime_root is None:
            return None
        return Path(runtime_root) / "data" / "tool_results" / "ephemeral"


def render_tool_result_page_for_llm(page: ToolResultPage, *, tag: str = "tool_result") -> str:
    attrs = {
        "result_ref": page.result_ref,
        "anchor": page.anchor,
        "page": str(page.page),
        "anchor_page": str(page.anchor_page),
        "page_count": str(page.page_count),
        "has_more": "true" if page.has_more else "false",
        "has_more_before": "true" if page.has_more_before else "false",
        "has_more_after": "true" if page.has_more_after else "false",
    }
    if page.status:
        attrs["status"] = page.status
    open_tag = f"<{tag} " + " ".join(f'{key}="{escape(value, quote=True)}"' for key, value in attrs.items()) + ">"
    parts = [open_tag, page.content.rstrip()]
    if page.anchor == "tail":
        if page.anchor_page > 1 and page.has_more_after:
            parts.append(
                "newer_page: "
                f"{TOOL_RESULT_READER_ALIAS}(result_ref={json.dumps(page.result_ref)}, "
                f"anchor=\"tail\", page={page.anchor_page - 1})"
            )
        if page.has_more_before:
            parts.append(
                "older_page: "
                f"{TOOL_RESULT_READER_ALIAS}(result_ref={json.dumps(page.result_ref)}, "
                f"anchor=\"tail\", page={page.anchor_page + 1})"
            )
    else:
        if page.page > 1:
            parts.append(
                f"previous_page: {TOOL_RESULT_READER_ALIAS}(result_ref={json.dumps(page.result_ref)}, page={page.page - 1})"
            )
        if page.has_more_after:
            parts.append(
                f"next_page: {TOOL_RESULT_READER_ALIAS}(result_ref={json.dumps(page.result_ref)}, page={page.page + 1})"
            )
        if page.page_count > 1 and page.page != page.page_count:
            parts.append(
                f"tail_page: {TOOL_RESULT_READER_ALIAS}(result_ref={json.dumps(page.result_ref)}, anchor=\"tail\")"
            )
    parts.append(f"</{tag}>")
    return "\n".join(part for part in parts if part).strip()


def _normalize_anchor(value: object) -> str:
    return "tail" if str(value or "").strip().lower() in {"tail", "end", "last", "bottom"} else "head"


def _safe_file_name(value: str) -> str:
    text = str(value or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")
    return safe or "result"
