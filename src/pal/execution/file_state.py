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


def resolve_file_path(file_path: str | Path) -> Path:
    """Return one filesystem identity for every accepted path spelling."""
    return Path(file_path).expanduser().resolve()


def file_cache_key(file_path: str | Path) -> str:
    """Return the canonical key shared by all read-before-mutate tools."""
    try:
        return str(resolve_file_path(file_path))
    except (OSError, RuntimeError, ValueError):
        # Keep cache inspection and invalidation total even for malformed or
        # temporarily unresolvable paths.  expanduser still runs when possible
        # so ``~`` never gains a second, cwd-relative cache identity.
        try:
            return str(Path(file_path).expanduser().absolute())
        except (OSError, RuntimeError, ValueError):
            return str(file_path)


@dataclass(frozen=True)
class FileState:
    """Snapshot of a file as seen when it was read."""

    content: str
    mtime_ns: int
    full_view: bool = True
    last_view: tuple[int, int] | None = None


class FileStateCache:
    """LRU cache (max 1000 entries) that tracks file read state.

    Keys are user-expanded, resolved absolute paths.  Each entry
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

    def mark_read(
        self,
        file_path: str | Path,
        content: str,
        *,
        full_view: bool = True,
        view: tuple[int, int] | None = None,
    ) -> None:
        """Record that *file_path* has been read with the given *content*.

        The resolved path and current mtime are captured.  If the cache is
        full the least-recently-used entry is evicted.
        """
        key = self._resolve(file_path)
        mtime_ns = self._get_mtime_ns(key)
        previous = self._cache.get(key)
        if (
            previous is not None
            and previous.mtime_ns == mtime_ns
            and previous.content == content
        ):
            full_view = full_view or previous.full_view
        entry = FileState(
            content=content,
            mtime_ns=mtime_ns,
            full_view=full_view,
            last_view=view,
        )
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
        entry = self.get_valid_state(file_path)
        return entry.content if entry is not None else None

    def get_valid_full(self, file_path: str | Path) -> str | None:
        """Return content only when the caller has seen the complete file."""
        entry = self.get_valid_state(file_path)
        if entry is None or not entry.full_view:
            return None
        return entry.content

    def get_valid_state(self, file_path: str | Path) -> FileState | None:
        """Return the current cached state, evicting stale snapshots."""
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
        return entry

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
        return file_cache_key(file_path)

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
    def invoke(self, args: dict[str, Any]) -> CapabilityResult:
        file_path = str(args.get("file_path") or "").strip()
        payload: dict[str, Any] = {"cached_file_count": len(self.cache)}
        if not file_path:
            text = f"file state cache contains {len(self.cache)} cached file(s)"
            return CapabilityResult(status=RuntimeStatus.OK, text=text, llm_text=text, structured=payload)

        resolved = FileStateCache._resolve(file_path)
        was_cached = file_path in self.cache
        cached_state = self.cache.get_valid_state(file_path)
        valid = cached_state is not None
        payload.update(
            {
                "file_path": resolved,
                "cached": was_cached,
                "valid": valid,
                "full_view": bool(cached_state and cached_state.full_view),
                "content_length": len(cached_state.content) if cached_state is not None else 0,
            }
        )
        if valid:
            completeness = "complete" if cached_state and cached_state.full_view else "partial"
            text = f"file state cache has a current {completeness} read snapshot for {resolved}"
        elif was_cached:
            text = f"file state cache had a stale snapshot for {resolved}; read the file again before editing"
        else:
            text = f"file state cache has no read snapshot for {resolved}; read the file before editing"
        return CapabilityResult(status=RuntimeStatus.OK, text=text, llm_text=text, structured=payload)

    async def ainvoke(self, args: dict[str, Any], **kwargs: Any) -> CapabilityResult:
        _ = kwargs
        return self.invoke(args)
