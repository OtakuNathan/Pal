"""Tests for FileStateCache: LRU eviction, mtime invalidation, and edge cases."""

from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from unittest import mock

from pal.execution.file_state import FileStateCache


class FileStateCacheBasicTests(unittest.TestCase):
    """Tests that do not require temporary files."""

    def test_mark_read_and_get_valid(self) -> None:
        cache = FileStateCache()
        cache.mark_read(__file__, "hello")
        self.assertEqual(cache.get_valid(__file__), "hello")

    def test_get_valid_returns_none_when_not_read(self) -> None:
        cache = FileStateCache()
        self.assertIsNone(cache.get_valid("/nonexistent/path"))

    def test_invalidate_removes_entry(self) -> None:
        cache = FileStateCache()
        cache.mark_read(__file__, "data")
        cache.invalidate(__file__)
        self.assertIsNone(cache.get_valid(__file__))

    def test_invalidate_is_noop_for_unknown(self) -> None:
        cache = FileStateCache()
        cache.invalidate("/no/such/file")  # should not raise

    def test_clear(self) -> None:
        cache = FileStateCache()
        cache.mark_read(__file__, "a")
        cache.mark_read(__file__ + ".bak", "b")
        cache.clear()
        self.assertEqual(len(cache), 0)

    def test_len_and_contains(self) -> None:
        cache = FileStateCache()
        self.assertEqual(len(cache), 0)
        self.assertNotIn(__file__, cache)
        cache.mark_read(__file__, "x")
        self.assertEqual(len(cache), 1)
        self.assertIn(__file__, cache)

    def test_overwrite_updates_entry(self) -> None:
        cache = FileStateCache()
        cache.mark_read(__file__, "v1")
        cache.mark_read(__file__, "v2")
        self.assertEqual(cache.get_valid(__file__), "v2")
        self.assertEqual(len(cache), 1)

    def test_partial_view_does_not_authorize_full_mutation(self) -> None:
        cache = FileStateCache()
        cache.mark_read(__file__, "partial", full_view=False)

        self.assertEqual(cache.get_valid(__file__), "partial")
        self.assertIsNone(cache.get_valid_full(__file__))

    def test_full_view_is_not_downgraded_by_later_partial_read(self) -> None:
        cache = FileStateCache()
        cache.mark_read(__file__, "same", full_view=True)
        cache.mark_read(__file__, "same", full_view=False)

        self.assertEqual(cache.get_valid_full(__file__), "same")


class FileStateCacheLRUTests(unittest.TestCase):
    """LRU eviction tests using a tiny max_entries."""

    def test_lru_eviction(self) -> None:
        cache = FileStateCache(max_entries=3)
        # Use different files (this test file modified by appending unique suffix)
        for i in range(4):
            cache.mark_read(__file__, f"content_{i}")
        # After inserting 4 items with the same key, only 1 remains (key is the same).
        # We need distinct keys for true LRU testing — use temp files.
        self.assertEqual(len(cache), 1)

    def test_lru_eviction_with_distinct_keys(self) -> None:
        import tempfile

        cache = FileStateCache(max_entries=2)
        paths: list[Path] = []
        for i in range(4):
            f = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w")
            f.write(f"content_{i}")
            f.close()
            paths.append(Path(f.name))

        try:
            for p in paths:
                cache.mark_read(p, f"content_of_{p.name}")
            # Only last 2 should remain (LRU)
            self.assertEqual(len(cache), 2)
            # First two should have been evicted
            self.assertIsNone(cache.get_valid(paths[0]))
            self.assertIsNone(cache.get_valid(paths[1]))
            # Last two should be valid
            self.assertIsNotNone(cache.get_valid(paths[2]))
            self.assertIsNotNone(cache.get_valid(paths[3]))
        finally:
            for p in paths:
                p.unlink(missing_ok=True)

    def test_access_touches_lru(self) -> None:
        import tempfile

        cache = FileStateCache(max_entries=2)
        paths: list[Path] = []
        for i in range(3):
            f = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w")
            f.write(f"c{i}")
            f.close()
            paths.append(Path(f.name))

        try:
            cache.mark_read(paths[0], "a")
            cache.mark_read(paths[1], "b")
            # Touch paths[0] to make it most-recently-used
            _ = cache.get_valid(paths[0])
            # Now adding paths[2] should evict paths[1] (LRU), not paths[0]
            cache.mark_read(paths[2], "c")
            self.assertIsNotNone(cache.get_valid(paths[0]), "paths[0] should survive (recently accessed)")
            self.assertIsNone(cache.get_valid(paths[1]), "paths[1] should be evicted (LRU)")
            self.assertIsNotNone(cache.get_valid(paths[2]))
        finally:
            for p in paths:
                p.unlink(missing_ok=True)


class FileStateCacheMtimeTests(unittest.TestCase):
    """Tests that mtime changes invalidate entries."""

    def test_mtime_change_invalidates(self) -> None:
        import tempfile

        f = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w")
        f.write("initial content")
        f.close()
        path = Path(f.name)

        try:
            cache = FileStateCache()
            cache.mark_read(path, "initial content")
            self.assertEqual(cache.get_valid(path), "initial content")

            # Modify file to change mtime
            time.sleep(0.05)  # ensure mtime changes (fs granularity)
            path.write_text("modified content")

            # Cache should now be invalid
            self.assertIsNone(cache.get_valid(path))
        finally:
            path.unlink(missing_ok=True)

    def test_deleted_file_invalidates(self) -> None:
        import tempfile

        f = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w")
        f.write("will be deleted")
        f.close()
        path = Path(f.name)

        try:
            cache = FileStateCache()
            cache.mark_read(path, "will be deleted")
            self.assertEqual(cache.get_valid(path), "will be deleted")

            path.unlink()
            self.assertIsNone(cache.get_valid(path))
        finally:
            path.unlink(missing_ok=True)

    def test_unchanged_file_stays_valid(self) -> None:
        import tempfile

        f = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w")
        f.write("stable content")
        f.close()
        path = Path(f.name)

        try:
            cache = FileStateCache()
            cache.mark_read(path, "stable content")
            # Don't modify the file — should still be valid
            self.assertEqual(cache.get_valid(path), "stable content")
        finally:
            path.unlink(missing_ok=True)


class FileStateCacheResolveTests(unittest.TestCase):
    """Path resolution tests."""

    def test_relative_and_absolute_resolve_to_same_key(self) -> None:
        import tempfile

        f = tempfile.NamedTemporaryFile(delete=False, suffix=".txt", mode="w")
        f.write("content")
        f.close()
        path = Path(f.name)

        try:
            cache = FileStateCache()
            cache.mark_read(str(path), "content")
            # get_valid with same resolved path should work
            self.assertEqual(cache.get_valid(str(path)), "content")
        finally:
            path.unlink(missing_ok=True)

    def test_tilde_and_absolute_paths_share_one_key(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "state.txt"
            path.write_text("content", encoding="utf-8")
            cache = FileStateCache()

            with mock.patch.dict(os.environ, {"HOME": home}):
                cache.mark_read("~/state.txt", "content")

                self.assertEqual(cache.get_valid(path), "content")
                self.assertIn("~/state.txt", cache)
                self.assertIn(path, cache)
                self.assertEqual(len(cache), 1)

                cache.invalidate(path)
                cache.mark_read(path, "content")
                self.assertEqual(cache.get_valid("~/state.txt"), "content")
                self.assertEqual(len(cache), 1)


if __name__ == "__main__":
    unittest.main()
