"""File state cache with LRU eviction and mtime-based invalidation.

Provides the safety layer that ensures file_edit only modifies files
that have been explicitly read and are still current (not modified on disk
since the last read).
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
import fcntl
import hashlib
import os
from pathlib import Path
import tempfile
import threading
from typing import Any

from pal.execution.contracts import CapabilityResult
from pal.execution.session_state import (
    LogicalExecutionContext,
    LogicalExecutionStateBackend,
    content_digest,
    count_text_lines,
)
from pal.shared import RuntimeStatus

UTF8_BOM = "\ufeff"


class FileContentChangedError(RuntimeError):
    """The path no longer contains the bytes authorized by the caller."""


MUTATION_LOCK_BUCKET_COUNT = 256
_MUTATION_LOCKS = tuple(
    threading.RLock() for _ in range(MUTATION_LOCK_BUCKET_COUNT)
)


def resolve_file_path(file_path: str | Path) -> Path:
    """Return one filesystem identity for every accepted path spelling."""
    return Path(file_path).expanduser().resolve()


def read_utf8_text_exact(file_path: str | Path) -> str:
    """Read UTF-8 text without universal-newline normalization."""

    with Path(file_path).open("r", encoding="utf-8", newline="") as handle:
        return handle.read()


def atomic_compare_and_swap_utf8(
    file_path: str | Path,
    *,
    expected_content: str | None,
    new_content: str,
    create_parents: bool = False,
) -> None:
    """Atomically replace a UTF-8 file if its exact pre-image still matches.

    ``expected_content=None`` means that the path must still be absent.
    Bounded process-lock and POSIX-lock stripes close races among Pal workers;
    the exact byte-equivalent text comparison runs while that lock is held.
    ``os.replace`` makes the commit atomic.
    Non-cooperating external writers cannot be forced to honor an advisory
    lock, but an observed pre-commit path replacement is rejected.
    """

    resolved = resolve_file_path(file_path)
    key = file_cache_key(resolved)
    lock_bucket = _mutation_lock_bucket(key)
    process_lock = _MUTATION_LOCKS[lock_bucket]
    with process_lock:
        lock_root = Path(tempfile.gettempdir()) / "pal-file-locks"
        lock_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_name = f"bucket-{lock_bucket:03d}.lock"
        with (lock_root / lock_name).open("a+b") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            _atomic_compare_and_swap_locked(
                resolved,
                expected_content=expected_content,
                new_content=new_content,
                create_parents=create_parents,
            )


def _atomic_compare_and_swap_locked(
    resolved: Path,
    *,
    expected_content: str | None,
    new_content: str,
    create_parents: bool,
) -> None:
    parent = resolved.parent
    if create_parents:
        parent.mkdir(parents=True, exist_ok=True)
    if not parent.is_dir():
        raise NotADirectoryError(parent)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{resolved.name}.pal-",
        dir=str(parent),
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(new_content.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())

        # Prepare the replacement first, then perform exactly one final
        # pre-image check while both the in-process stripe and POSIX bucket
        # lock are held. Cooperative Pal writers cannot enter this interval.
        if expected_content is None:
            if resolved.exists():
                raise FileContentChangedError(f"path appeared before commit: {resolved}")
        else:
            if not resolved.exists() or not resolved.is_file():
                raise FileContentChangedError(
                    f"path disappeared or changed kind: {resolved}"
                )
            try:
                current = read_utf8_text_exact(resolved)
            except (OSError, UnicodeError) as exc:
                raise FileContentChangedError(f"could not revalidate {resolved}") from exc
            if current != expected_content:
                raise FileContentChangedError(f"file changed before commit: {resolved}")
            os.chmod(temporary, resolved.stat().st_mode & 0o7777)

        os.replace(temporary, resolved)
        try:
            directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            directory_fd = -1
        if directory_fd >= 0:
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _mutation_lock_bucket(file_key: str) -> int:
    digest = hashlib.sha256(str(file_key).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") % MUTATION_LOCK_BUCKET_COUNT


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


def text_line_starts(content: str) -> tuple[int, ...]:
    """Return character offsets for the logical lines used by file_read."""

    starts: list[int] = []
    cursor = 0
    for line in content.splitlines(keepends=True):
        starts.append(cursor)
        cursor += len(line)
    return tuple(starts)


def line_number_at_offset(content: str, offset: int) -> int:
    """Return the 1-based logical line containing an insertion point."""

    lines = content.splitlines(keepends=True)
    if not lines:
        return 1
    position = min(max(0, int(offset)), len(content))
    cursor = 0
    for index, line in enumerate(lines, start=1):
        end = cursor + len(line)
        if position < end:
            return index
        cursor = end
    return len(lines)


def line_range_for_offsets(
    content: str,
    start_offset: int,
    end_offset: int,
) -> tuple[int, int]:
    """Map an exact character interval to every logical line it can affect."""

    lines = content.splitlines(keepends=True)
    if not lines:
        return (1, 1)
    start = min(max(0, int(start_offset)), len(content))
    end = min(max(start, int(end_offset)), len(content))
    start_line = line_number_at_offset(content, start)
    if end == start:
        return (start_line, start_line)
    end_line = line_number_at_offset(content, end - 1)

    # Consuming a line terminator can join or restructure the following line.
    # splitlines(keepends=True) recognizes LF, CRLF, CR and the other Unicode
    # line boundaries, unlike counting only ``\n``.
    cursor = 0
    for index, line in enumerate(lines, start=1):
        cursor += len(line)
        if end == cursor and index < len(lines):
            if line.endswith(("\n", "\r", "\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")):
                end_line = index + 1
            break
    return (start_line, max(start_line, end_line))


@dataclass(frozen=True)
class FileState:
    """Snapshot of a file as seen when it was read."""

    content: str
    mtime_ns: int
    full_view: bool = True
    covered_ranges: tuple[tuple[int, int], ...] = ()
    authority_result_ids: tuple[str, ...] = ()

    def covers_lines(self, start_line: int, end_line: int) -> bool:
        """Return whether every affected line was delivered to the caller."""

        if self.full_view:
            return True
        return any(
            covered_start <= start_line and covered_end >= end_line
            for covered_start, covered_end in self.covered_ranges
        )


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
        covered_ranges: tuple[tuple[int, int], ...] = (),
    ) -> None:
        """Record that *file_path* has been read with the given *content*.

        The resolved path and current mtime are captured.  If the cache is
        full the least-recently-used entry is evicted.
        """
        key = self._resolve(file_path)
        mtime_ns = self._get_mtime_ns(key)
        previous = self._cache.get(key)
        total_lines = count_text_lines(content)
        visible_ranges = (
            ((1, total_lines),)
            if full_view and total_lines > 0
            else _merge_line_ranges(list(covered_ranges))
        )
        if (
            previous is not None
            and previous.mtime_ns == mtime_ns
            and previous.content == content
        ):
            full_view = full_view or previous.full_view
            visible_ranges = _merge_line_ranges(
                [*previous.covered_ranges, *visible_ranges]
            )
        if full_view and total_lines > 0:
            visible_ranges = ((1, total_lines),)
        entry = FileState(
            content=content,
            mtime_ns=mtime_ns,
            full_view=full_view,
            covered_ranges=visible_ranges,
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
class SessionFileStateCache:
    """Read-before-mutate view backed by one logical execution session."""

    backend: LogicalExecutionStateBackend
    context: LogicalExecutionContext

    def mark_read(
        self,
        file_path: str | Path,
        content: str,
        *,
        full_view: bool = True,
        covered_ranges: tuple[tuple[int, int], ...] = (),
    ) -> None:
        _ = covered_ranges
        if not full_view:
            return
        self.backend.set_file_snapshot(
            execution_lifetime_id=self.context.execution_lifetime_id,
            file_key=file_cache_key(file_path),
            digest=content_digest(content),
            total_lines=count_text_lines(content),
            complete=bool(full_view),
            source="mutation",
        )

    def get_valid(self, file_path: str | Path) -> str | None:
        state = self.get_valid_state(file_path)
        return state.content if state is not None else None

    def get_valid_full(self, file_path: str | Path) -> str | None:
        state = self.get_valid_state(file_path)
        if state is None or not state.full_view:
            return None
        return state.content

    def get_valid_state(self, file_path: str | Path) -> FileState | None:
        resolved = resolve_file_path(file_path)
        try:
            content = read_utf8_text_exact(resolved)
            mtime_ns = resolved.stat().st_mtime_ns
        except (OSError, UnicodeError):
            return None
        digest = content_digest(content)
        grant = self.backend.file_grant(
            execution_lifetime_id=self.context.execution_lifetime_id,
            file_key=file_cache_key(resolved),
            digest=digest,
        )
        if grant is None:
            return None
        return FileState(
            content=content,
            mtime_ns=mtime_ns,
            full_view=grant.complete,
            covered_ranges=grant.covered_ranges,
            authority_result_ids=grant.result_ids,
        )

    def invalidate(self, file_path: str | Path) -> None:
        self.backend.invalidate_file(
            execution_lifetime_id=self.context.execution_lifetime_id,
            file_key=file_cache_key(file_path),
        )

    def retire_if_observed_digest_changed(
        self,
        file_path: str | Path,
        *,
        observed_digest: str,
    ) -> bool:
        """Retire older result state only when it names different bytes."""

        file_key = file_cache_key(file_path)
        snapshot = self.backend.file_snapshot(
            execution_lifetime_id=self.context.execution_lifetime_id,
            file_key=file_key,
            digest="",
        )
        grant = self.backend.file_grant(
            execution_lifetime_id=self.context.execution_lifetime_id,
            file_key=file_key,
            digest="",
        )
        known_digests = {
            candidate.digest
            for candidate in (snapshot, grant)
            if candidate is not None and candidate.digest
        }
        if not known_digests or known_digests == {str(observed_digest)}:
            return False
        self.invalidate(file_path)
        return True

    def clear(self) -> None:
        # Logical session state is retired by its lifecycle owner; a file tool
        # cannot clear unrelated file grants.
        return

    def __len__(self) -> int:
        return 0

    def __contains__(self, file_path: str | Path) -> bool:
        return (
            self.backend.file_grant(
                execution_lifetime_id=self.context.execution_lifetime_id,
                file_key=file_cache_key(file_path),
                digest="",
            )
            is not None
        )


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


def _merge_line_ranges(
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
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return tuple(merged)
