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

from pal.llm.contracts import ToolResultHandle


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


@dataclass
class ToolResultPagerStore:
    retention_user_turns: int = DEFAULT_TOOL_RESULT_RETENTION_USER_TURNS
    _handles: dict[str, ToolResultHandle] = field(default_factory=dict)
    _tool_names: dict[str, str] = field(default_factory=dict)
    _statuses: dict[str, str] = field(default_factory=dict)
    _ok: dict[str, bool] = field(default_factory=dict)
    _rendered: dict[str, str] = field(default_factory=dict)
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
        runtime_root: Path | None,
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
        with self._lock:
            path: Path | None = None
            rendered_text = str(rendered or "")
            if root is not None:
                self._cleanup_stale_root_once(root)
                turn_dir = root / _safe_file_name(normalized_turn_id)
                turn_dir.mkdir(parents=True, exist_ok=True)
                path = turn_dir / f"{safe_file}.txt"
                path.write_text(rendered_text, encoding="utf-8")
            else:
                self._rendered[normalized_ref] = rendered_text
            resolved_page_size = max(256, int(page_size or DEFAULT_TOOL_RESULT_PAGE_SIZE))
            original_size = len(rendered_text)
            page_count = max(1, math.ceil(original_size / resolved_page_size))
            turn_index = self._turn_indices.get(normalized_turn_id, self._current_user_turn_index)
            handle = ToolResultHandle(
                result_ref=normalized_ref,
                turn_id=normalized_turn_id,
                backing_path=str(path) if path is not None else "",
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

    def read_page(
        self,
        result_ref: str,
        *,
        page: int = 1,
        page_size: int | None = None,
        anchor: str = "head",
    ) -> ToolResultPage | None:
        normalized_ref = str(result_ref or "").strip()
        if not normalized_ref:
            return None
        normalized_anchor = _normalize_anchor(anchor)
        with self._lock:
            handle = self._handles.get(normalized_ref)
            if handle is None:
                return None
            if handle.backing_path:
                path = Path(handle.backing_path)
                if not path.is_file():
                    self._drop_handle(normalized_ref)
                    return None
                text = path.read_text(encoding="utf-8")
            else:
                text = self._rendered.get(normalized_ref)
                if text is None:
                    self._drop_handle(normalized_ref)
                    return None
            resolved_page_size = max(256, int(page_size or handle.page_size or DEFAULT_TOOL_RESULT_PAGE_SIZE))
            page_count = max(1, math.ceil(len(text) / resolved_page_size))
            requested_page = max(1, int(page or 1))
            anchor_page = requested_page
            absolute_page = requested_page
            if normalized_anchor == "tail":
                absolute_page = page_count - requested_page + 1
            if absolute_page < 1 or absolute_page > page_count:
                return ToolResultPage(
                    result_ref=normalized_ref,
                    content="",
                    page=absolute_page,
                    page_count=page_count,
                    has_more=False,
                    original_size=len(text),
                    page_size=resolved_page_size,
                    anchor=normalized_anchor,
                    anchor_page=anchor_page,
                    has_more_before=False,
                    has_more_after=False,
                    tool_name=self._tool_names.get(normalized_ref, ""),
                    status=self._statuses.get(normalized_ref, ""),
                    ok=self._ok.get(normalized_ref, True),
                )
            start = (absolute_page - 1) * resolved_page_size
            end = min(start + resolved_page_size, len(text))
            return ToolResultPage(
                result_ref=normalized_ref,
                content=text[start:end],
                page=absolute_page,
                page_count=page_count,
                has_more=absolute_page < page_count,
                original_size=len(text),
                page_size=resolved_page_size,
                anchor=normalized_anchor,
                anchor_page=anchor_page,
                has_more_before=absolute_page > 1,
                has_more_after=absolute_page < page_count,
                start_offset=start,
                end_offset=end,
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
        if handle is not None and handle.backing_path:
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
        self._rendered.pop(result_ref, None)

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
