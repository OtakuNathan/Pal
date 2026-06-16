"""File state cache with LRU eviction and mtime-based invalidation.

Provides the safety layer that ensures file_edit only modifies files
that have been explicitly read and are still current (not modified on disk
since the last read).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.shared import RuntimeStatus


@dataclass(frozen=True)
class FileState:
    """Snapshot of a file as seen when it was read."""

    content: str
    mtime_ns: int


class FileStateCache:
    """LRU cache (max 1000 entries) that tracks file read state.

    Keys are resolved absolute paths (``Path.resolve()``).  Each entry
    stores the content and the nanosecond-resolution modification time
    (``st_mtime_ns``) so that ``get_valid`` can automatically invalidate
    stale entries when the on-disk mtime has changed.
    """

    MAX_ENTRIES = 1000

    def __init__(self, max_entries: int = MAX_ENTRIES) -> None:
        self._max = max(max_entries, 1)
        self._cache: OrderedDict[str, FileState] = OrderedDict()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def mark_read(self, file_path: str | Path, content: str) -> None:
        """Record that *file_path* has been read with the given *content*.

        The resolved path and current mtime are captured.  If the cache is
        full the least-recently-used entry is evicted.
        """
        key = self._resolve(file_path)
        mtime_ns = self._get_mtime_ns(key)
        entry = FileState(content=content, mtime_ns=mtime_ns)
        self._cache[key] = entry
        self._cache.move_to_end(key)
        self._evict_if_needed()

    def get_valid(self, file_path: str | Path) -> str | None:
        """Return the cached content if the file is still valid, else ``None``.

        A cached entry is considered *valid* when:
        1. The file exists on disk, AND
        2. Its ``st_mtime_ns`` matches the recorded mtime.

        If the file has been modified (or deleted) since the read, the
        entry is automatically removed and ``None`` is returned.
        """
        key = self._resolve(file_path)
        entry = self._cache.get(key)
        if entry is None:
            return None

        current_mtime = self._get_mtime_ns(key)
        if current_mtime != entry.mtime_ns:
            # Stale — evict automatically.
            self._cache.pop(key, None)
            return None

        # Touch for LRU ordering.
        self._cache.move_to_end(key)
        return entry.content

    def invalidate(self, file_path: str | Path) -> None:
        """Remove the cached entry for *file_path* (no-op if absent)."""
        key = self._resolve(file_path)
        self._cache.pop(key, None)

    def clear(self) -> None:
        """Remove all cached entries."""
        self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)

    def __contains__(self, file_path: str | Path) -> bool:
        return self._resolve(file_path) in self._cache

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _resolve(file_path: str | Path) -> str:
        try:
            return str(Path(file_path).resolve())
        except (OSError, ValueError):
            return str(file_path)

    @staticmethod
    def _get_mtime_ns(resolved_path: str) -> int:
        """Return st_mtime_ns for the path, or ``-1`` if unreadable."""
        try:
            return Path(resolved_path).stat().st_mtime_ns
        except OSError:
            return -1

    def _evict_if_needed(self) -> None:
        while len(self._cache) > self._max:
            self._cache.popitem(last=False)


@dataclass
class FileStateTool:
    """Inspect the shared read-before-edit file cache."""

    cache: FileStateCache = field(default_factory=FileStateCache)
    name: str = "op_file_state"
    display_name: str = "File State"
    family: str = "system"
    description: str = (
        "Inspect whether a file has a current cached read snapshot for safe file_edit use. "
        "This does not return cached file contents."
    )
    tags: tuple[str, ...] = ("file", "state", "cache", "system")
    keywords: tuple[str, ...] = ("file", "state", "cache", "read", "edit", "stale")
    args_schema: dict[str, Any] = field(default_factory=dict)
    result_schema: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.args_schema:
            self.args_schema = {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "Optional file path to check against the read-before-edit cache.",
                    },
                },
            }
        if not self.result_schema:
            self.result_schema = {
                "type": "object",
                "properties": {
                    "cached_file_count": {"type": "integer"},
                    "file_path": {"type": "string"},
                    "cached": {"type": "boolean"},
                    "valid": {"type": "boolean"},
                    "content_length": {"type": "integer"},
                },
            }

    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        file_path = str(args.get("file_path") or "").strip()
        payload: dict[str, Any] = {"cached_file_count": len(self.cache)}
        if not file_path:
            text = f"file state cache contains {len(self.cache)} cached file(s)"
            return CapabilityResult(status=RuntimeStatus.OK, text=text, llm_text=text, structured=payload)

        resolved = FileStateCache._resolve(file_path)
        was_cached = file_path in self.cache
        cached_content = self.cache.get_valid(file_path)
        valid = cached_content is not None
        payload.update(
            {
                "file_path": resolved,
                "cached": was_cached,
                "valid": valid,
                "content_length": len(cached_content) if cached_content is not None else 0,
            }
        )
        if valid:
            text = f"file state cache has a current read snapshot for {resolved}"
        elif was_cached:
            text = f"file state cache had a stale snapshot for {resolved}; read the file again before editing"
        else:
            text = f"file state cache has no read snapshot for {resolved}; read the file before editing"
        return CapabilityResult(status=RuntimeStatus.OK, text=text, llm_text=text, structured=payload)

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        return self.invoke(args)
