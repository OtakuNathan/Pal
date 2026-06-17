from __future__ import annotations

import contextlib
import json
import math
import re
import shutil
import threading
from dataclasses import dataclass, field
from html import escape
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.llm.contracts import ToolResultHandle
from pal.shared import RuntimeStatus


DEFAULT_TOOL_RESULT_PAGE_SIZE = 4_000
DEFAULT_TOOL_RESULT_RETENTION_USER_TURNS = 5


@dataclass(frozen=True)
class ToolResultPage:
    result_ref: str
    content: str
    page: int
    page_count: int
    has_more: bool
    original_size: int
    page_size: int
    tool_name: str = ""
    status: str = ""
    ok: bool = True


@dataclass
class ToolResultPagerStore:
    retention_user_turns: int = DEFAULT_TOOL_RESULT_RETENTION_USER_TURNS
    _handles: dict[str, ToolResultHandle] = field(default_factory=dict)
    _tool_names: dict[str, str] = field(default_factory=dict)
    _statuses: dict[str, str] = field(default_factory=dict)
    _ok: dict[str, bool] = field(default_factory=dict)
    _turn_indices: dict[str, int] = field(default_factory=dict)
    _current_user_turn_index: int = 0
    _initialized_roots: set[str] = field(default_factory=set)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def begin_turn(
        self,
        *,
        runtime_root: Path | None,
        turn_id: str,
        scope_key: str = "",
        retention_user_turns: int | None = None,
    ) -> None:
        _ = scope_key
        if retention_user_turns is not None:
            self.retention_user_turns = max(1, int(retention_user_turns))
        with self._lock:
            self._current_user_turn_index += 1
            normalized_turn_id = str(turn_id or "").strip()
            if normalized_turn_id:
                self._turn_indices[normalized_turn_id] = self._current_user_turn_index
            root = self._ephemeral_root(runtime_root)
            if root is not None:
                self._cleanup_stale_root_once(root)
            self.prune(runtime_root=runtime_root)

    def store(
        self,
        *,
        runtime_root: Path,
        turn_id: str,
        result_ref: str,
        tool_name: str,
        status: str,
        ok: bool,
        rendered: str,
        page_size: int,
    ) -> ToolResultHandle:
        normalized_ref = str(result_ref or "").strip()
        if not normalized_ref:
            raise ValueError("result_ref is required")
        normalized_turn_id = str(turn_id or "").strip() or "unknown_turn"
        safe_file = _safe_file_name(normalized_ref)
        root = self._ephemeral_root(runtime_root)
        if root is None:
            raise ValueError("runtime_root is required")
        with self._lock:
            self._cleanup_stale_root_once(root)
            turn_dir = root / _safe_file_name(normalized_turn_id)
            turn_dir.mkdir(parents=True, exist_ok=True)
            path = turn_dir / f"{safe_file}.txt"
            path.write_text(str(rendered or ""), encoding="utf-8")
            resolved_page_size = max(256, int(page_size or DEFAULT_TOOL_RESULT_PAGE_SIZE))
            original_size = len(str(rendered or ""))
            page_count = max(1, math.ceil(original_size / resolved_page_size))
            turn_index = self._turn_indices.get(normalized_turn_id, self._current_user_turn_index)
            handle = ToolResultHandle(
                result_ref=normalized_ref,
                turn_id=normalized_turn_id,
                backing_path=str(path),
                page_size=resolved_page_size,
                original_size=original_size,
                page_count=page_count,
                created_user_turn_index=turn_index,
            )
            self._handles[normalized_ref] = handle
            self._tool_names[normalized_ref] = str(tool_name or "")
            self._statuses[normalized_ref] = str(status or "")
            self._ok[normalized_ref] = bool(ok)
            return handle

    def read_page(self, result_ref: str, *, page: int = 1, page_size: int | None = None) -> ToolResultPage | None:
        normalized_ref = str(result_ref or "").strip()
        if not normalized_ref:
            return None
        with self._lock:
            handle = self._handles.get(normalized_ref)
            if handle is None:
                return None
            path = Path(handle.backing_path)
            if not path.is_file():
                self._drop_handle(normalized_ref)
                return None
            text = path.read_text(encoding="utf-8")
            resolved_page_size = max(256, int(page_size or handle.page_size or DEFAULT_TOOL_RESULT_PAGE_SIZE))
            page_count = max(1, math.ceil(len(text) / resolved_page_size))
            requested_page = max(1, int(page or 1))
            if requested_page > page_count:
                return ToolResultPage(
                    result_ref=normalized_ref,
                    content="",
                    page=requested_page,
                    page_count=page_count,
                    has_more=False,
                    original_size=len(text),
                    page_size=resolved_page_size,
                    tool_name=self._tool_names.get(normalized_ref, ""),
                    status=self._statuses.get(normalized_ref, ""),
                    ok=self._ok.get(normalized_ref, True),
                )
            start = (requested_page - 1) * resolved_page_size
            end = min(start + resolved_page_size, len(text))
            return ToolResultPage(
                result_ref=normalized_ref,
                content=text[start:end],
                page=requested_page,
                page_count=page_count,
                has_more=requested_page < page_count,
                original_size=len(text),
                page_size=resolved_page_size,
                tool_name=self._tool_names.get(normalized_ref, ""),
                status=self._statuses.get(normalized_ref, ""),
                ok=self._ok.get(normalized_ref, True),
            )

    def prune(self, *, runtime_root: Path | None = None) -> None:
        threshold = self._current_user_turn_index - max(1, self.retention_user_turns)
        expired_refs = [
            result_ref
            for result_ref, handle in self._handles.items()
            if handle.created_user_turn_index and handle.created_user_turn_index <= threshold
        ]
        for result_ref in expired_refs:
            self.delete(result_ref)
        root = self._ephemeral_root(runtime_root)
        if root is None:
            return
        live_turns = {handle.turn_id for handle in self._handles.values()}
        for turn_dir in root.iterdir() if root.exists() else ():
            if turn_dir.is_dir() and turn_dir.name not in {_safe_file_name(turn_id) for turn_id in live_turns}:
                shutil.rmtree(turn_dir, ignore_errors=True)

    def delete(self, result_ref: str) -> None:
        normalized_ref = str(result_ref or "").strip()
        if not normalized_ref:
            return
        handle = self._handles.get(normalized_ref)
        self._drop_handle(normalized_ref)
        if handle is not None:
            path = Path(handle.backing_path)
            with contextlib.suppress(OSError):
                path.unlink()
            parent = path.parent
            with contextlib.suppress(OSError):
                if parent.exists() and not any(parent.iterdir()):
                    parent.rmdir()

    def _drop_handle(self, result_ref: str) -> None:
        self._handles.pop(result_ref, None)
        self._tool_names.pop(result_ref, None)
        self._statuses.pop(result_ref, None)
        self._ok.pop(result_ref, None)

    def _cleanup_stale_root_once(self, root: Path) -> None:
        key = str(root)
        if key in self._initialized_roots:
            return
        shutil.rmtree(root, ignore_errors=True)
        root.mkdir(parents=True, exist_ok=True)
        self._initialized_roots.add(key)

    @staticmethod
    def _ephemeral_root(runtime_root: Path | None) -> Path | None:
        if runtime_root is None:
            return None
        return Path(runtime_root) / "tool_results" / "ephemeral"


@dataclass
class ToolResultPageTool:
    runtime: object
    name: str = "op_tool_result_page"
    display_name: str = "Tool Result Page"
    family: str = "discovery"
    description: str = (
        "Read a later page of a prior tool result only when that tool result explicitly provides a next_page call. "
        "Use the original tool_call_id as result_ref."
    )
    tags: tuple[str, ...] = ("tool-result", "pager", "read")
    keywords: tuple[str, ...] = ("page", "result", "tool", "next")
    args_schema: dict[str, object] = field(default_factory=dict)
    result_schema: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.args_schema:
            self.args_schema = {
                "type": "object",
                "properties": {
                    "result_ref": {
                        "type": "string",
                        "description": "The result_ref shown in a prior tool result; this is the original tool_call_id.",
                    },
                    "page": {"type": "integer", "minimum": 1, "description": "1-based page number to read."},
                    "page_size": {"type": "integer", "minimum": 256, "description": "Optional character page size."},
                },
                "required": ["result_ref", "page"],
            }
        if not self.result_schema:
            self.result_schema = {
                "type": "object",
                "properties": {
                    "result_ref": {"type": "string"},
                    "page": {"type": "integer"},
                    "page_count": {"type": "integer"},
                    "has_more": {"type": "boolean"},
                    "original_size": {"type": "integer"},
                    "page_size": {"type": "integer"},
                },
            }

    def invoke(self, args: dict[str, object]) -> CapabilityResult:
        result_ref = str(args.get("result_ref") or "").strip()
        page = _positive_int(args.get("page"), default=1)
        page_size = _positive_int(args.get("page_size"), default=0)
        if not result_ref:
            return _expired_page_result(result_ref=result_ref, reason="missing_result_ref")
        page_result = self.runtime.read_tool_result_page(
            result_ref=result_ref,
            page=page or 1,
            page_size=page_size or None,
        )
        if page_result is None:
            return _expired_page_result(result_ref=result_ref, reason="not_found_or_expired")
        if page_result.page > page_result.page_count:
            text = f"tool result page {page_result.page} is out of range; last page is {page_result.page_count}."
            return CapabilityResult(
                status=RuntimeStatus.INVALID,
                text=text,
                llm_text=text,
                structured={
                    "reason": "page_out_of_range",
                    "result_ref": result_ref,
                    "page": page_result.page,
                    "page_count": page_result.page_count,
                },
            )
        llm_text = render_tool_result_page_for_llm(page_result, tag="tool_result_page")
        return CapabilityResult(
            status=RuntimeStatus.OK,
            text=page_result.content,
            llm_text=llm_text,
            structured={
                "result_ref": page_result.result_ref,
                "page": page_result.page,
                "page_count": page_result.page_count,
                "has_more": page_result.has_more,
                "original_size": page_result.original_size,
                "page_size": page_result.page_size,
            },
        )

    async def ainvoke(self, args: dict[str, object], **kwargs: object) -> CapabilityResult:
        _ = kwargs
        return self.invoke(args)


def render_tool_result_page_for_llm(page: ToolResultPage, *, tag: str = "tool_result") -> str:
    attrs = {
        "result_ref": page.result_ref,
        "page": str(page.page),
        "page_count": str(page.page_count),
        "has_more": "true" if page.has_more else "false",
    }
    if page.status:
        attrs["status"] = page.status
    open_tag = f"<{tag} " + " ".join(f'{key}="{escape(value, quote=True)}"' for key, value in attrs.items()) + ">"
    parts = [open_tag, page.content.rstrip()]
    if page.has_more:
        parts.append(
            f"next_page: op_tool_result_page(result_ref={json.dumps(page.result_ref)}, page={page.page + 1})"
        )
    parts.append(f"</{tag}>")
    return "\n".join(part for part in parts if part).strip()


def _expired_page_result(*, result_ref: str, reason: str) -> CapabilityResult:
    text = "tool result page not found or expired; rerun the original tool if needed"
    return CapabilityResult(
        status=RuntimeStatus.NOT_FOUND,
        text=text,
        llm_text=text,
        structured={"reason": reason, "result_ref": result_ref},
    )


def _positive_int(value: object, *, default: int) -> int:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _safe_file_name(value: str) -> str:
    text = str(value or "").strip()
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", text).strip("._")
    return safe or "result"
